from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from croniter import croniter

from .connections import Connections
from .errors import BotError
from .store import Store, dumps, now, uid

TERMINAL = ("succeeded", "failed", "cancelled")
TASK_SELECT = """SELECT t.*,
 (SELECT COUNT(*) FROM tool_calls c WHERE c.task_id=t.id AND c.status IN ('dispatched','unknown')) AS unresolved_effect_count,
 EXISTS(SELECT 1 FROM model_calls m WHERE m.task_id=t.id AND m.usage='null') AS token_usage_estimated
 FROM tasks t"""
TASK_SUMMARY_SELECT = """SELECT t.id,t.conversation_id,substr(t.title,1,64) AS title,t.status,t.revision,
 t.created_at,t.updated_at,t.execution_mode,t.parent_task_id,t.schedule_id,
 substr(t.wait_reason,1,240) AS wait_reason,t.tool_count,t.token_count,t.active_seconds,
 (SELECT COUNT(*) FROM tool_calls c WHERE c.task_id=t.id AND c.status IN ('dispatched','unknown')) AS unresolved_effect_count,
 EXISTS(SELECT 1 FROM model_calls m WHERE m.task_id=t.id AND m.usage='null') AS token_usage_estimated
 FROM tasks t"""
CONVERSATION_SELECT = """SELECT c.id,c.title,c.created_at,c.updated_at,
 (SELECT t.id FROM tasks t WHERE t.conversation_id=c.id ORDER BY t.created_at DESC,t.id DESC LIMIT 1) AS latest_task_id,
 (SELECT t.status FROM tasks t WHERE t.conversation_id=c.id ORDER BY t.created_at DESC,t.id DESC LIMIT 1) AS latest_task_status
 FROM conversations c"""


def page_cursor(kind: str, created_at: str, identity: str, task_id: str | None = None) -> str:
    value = {"v":1,"kind":kind,"created_at":created_at,"id":identity,"task_id":task_id}
    return base64.urlsafe_b64encode(dumps(value).encode()).decode().rstrip("=")


def read_page_cursor(value: str, kind: str, task_id: str | None = None) -> tuple[str, str]:
    try:
        if not value or len(value)>1024:
            raise ValueError("Invalid cursor size")
        decoded = json.loads(base64.b64decode(value + "="*(-len(value)%4),altchars=b"-_",validate=True))
        if (not isinstance(decoded,dict) or type(decoded.get("v")) is not int or decoded.get("v") != 1 or decoded.get("kind") != kind
                or decoded.get("task_id") != task_id):
            raise ValueError("Cursor scope changed")
        stamp,identity = decoded.get("created_at"),decoded.get("id")
        if not isinstance(stamp,str) or not stamp or len(stamp)>64 or not isinstance(identity,str) or not identity or len(identity)>128:
            raise ValueError("Invalid cursor values")
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("Cursor timestamp must be UTC")
        return stamp,identity
    except (ValueError,TypeError,binascii.Error,UnicodeError):
        raise BotError("invalid_cursor","分页游标无效或不属于当前列表") from None


def page_limit(value: int, maximum: int = 100) -> int:
    if type(value) is not int or not 1<=value<=maximum:
        raise BotError("invalid_limit",f"每页数量须为 1 到 {maximum} 的整数")
    return value


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":"),ensure_ascii=False).encode()).hexdigest()


def next_schedule(expression: str, zone: str, base: datetime | None = None) -> str:
    tz=ZoneInfo(zone)
    base=base or datetime.now(timezone.utc)
    iterator=croniter(expression,base.astimezone(tz).replace(tzinfo=None))
    for _ in range(10000):
        local=iterator.get_next(datetime)
        first=local.replace(tzinfo=tz,fold=0)
        # Skip nonexistent wall times; ambiguous times use their first fold.
        roundtrip=datetime.fromtimestamp(first.timestamp(),tz).replace(tzinfo=None)
        if roundtrip==local and first.timestamp()>base.timestamp():
            return first.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    raise ValueError("No valid schedule occurrence within search bound")


class Service:
    def __init__(self, store: Store):
        self.s = store
        self.connections = Connections(store)
        self.leases: dict[str, dict] = {}
        self.tool_lock = asyncio.Lock()
        self.last_progress = time.monotonic()
        self.last_activity_stamp = self.s.meta("last_activity")
        self.maintenance_id = None

    @contextlib.asynccontextmanager
    async def work(self, kind: str, identity: str, maximum=600):
        lease_id = uid()
        with self.s.tx() as db:
            self.s.writable(db, intake=True)
            self.leases[lease_id] = {"id":lease_id,"kind":kind,"owner":identity,"started":time.monotonic(),"maximum":maximum}
            self.s.activity(db)
        try:
            async with asyncio.timeout(maximum):
                yield
        finally:
            self.leases.pop(lease_id,None)
            self.last_progress = time.monotonic()
            with self.s.tx() as db:
                self.s.activity(db)

    def task(self, identity: str, db=None):
        conn = db or self.s.db
        row = conn.execute(TASK_SELECT + " WHERE t.id=?", (identity,)).fetchone()
        if row is None:
            raise BotError("not_found", "任务不存在", 404)
        return self.task_flags(dict(row))

    @staticmethod
    def task_flags(task):
        task["token_usage_estimated"] = bool(task["token_usage_estimated"])
        task["cancel_requested"],task["pause_requested"] = bool(task["cancel_requested"]),bool(task["pause_requested"])
        return task

    def list_tasks(self):
        with self.s.lock:
            rows = self.s.db.execute(TASK_SELECT + " ORDER BY t.created_at DESC LIMIT 500").fetchall()
            return [self.task_flags(dict(row)) for row in rows]

    def require_task(self, identity: str):
        # Passive reads need only prove existence, not load a potentially large result.
        if self.s.one("SELECT 1 FROM tasks WHERE id=?",(identity,)) is None:
            raise BotError("not_found","任务不存在",404)

    def task_summaries(self, *, limit: int = 50, cursor: str | None = None):
        page_limit(limit)
        args: tuple = ()
        where = ""
        if cursor is not None:
            args = read_page_cursor(cursor,"tasks")
            where = " WHERE (t.created_at,t.id)<(?,?)"
        rows = self.s.all(TASK_SUMMARY_SELECT+where+" ORDER BY t.created_at DESC,t.id DESC LIMIT ?",(*args,limit+1))
        more = len(rows)>limit
        items = rows[:limit]
        for item in items:
            item["token_usage_estimated"] = bool(item["token_usage_estimated"])
        next_cursor = page_cursor("tasks",items[-1]["created_at"],items[-1]["id"]) if more else None
        return {"items":items,"next_cursor":next_cursor,"has_more":more}

    def message_page(self, task_id: str, *, limit: int = 50, before: str | None = None):
        page_limit(limit)
        self.require_task(task_id)
        args: tuple = (task_id,)
        where = ""
        if before is not None:
            cursor = read_page_cursor(before,"messages",task_id)
            args = (*args,*cursor)
            where = " AND (created_at,id)<(?,?)"
        rows = self.s.all("SELECT id,task_id,role,content,created_at FROM messages WHERE task_id=?"+where+
                          " ORDER BY created_at DESC,id DESC LIMIT ?",(*args,limit+1))
        more = len(rows)>limit
        items = list(reversed(rows[:limit]))
        next_before = page_cursor("messages",items[0]["created_at"],items[0]["id"],task_id) if more else None
        return {"items":items,"next_before":next_before,"has_more":more}

    def require_conversation(self, identity: str, db=None):
        row = (db or self.s.db).execute(CONVERSATION_SELECT+" WHERE c.id=?",(identity,)).fetchone()
        if row is None:
            raise BotError("not_found","会话不存在",404)
        return dict(row)

    def conversations(self, *, limit: int = 50, cursor: str | None = None):
        page_limit(limit)
        args: tuple = ()
        where = ""
        if cursor is not None:
            args = read_page_cursor(cursor,"conversations")
            where = " WHERE (c.created_at,c.id)<(?,?)"
        rows = self.s.all(CONVERSATION_SELECT+where+" ORDER BY c.created_at DESC,c.id DESC LIMIT ?",(*args,limit+1))
        more,items = len(rows)>limit,rows[:limit]
        next_cursor = page_cursor("conversations",items[-1]["created_at"],items[-1]["id"]) if more else None
        return {"items":items,"next_cursor":next_cursor,"has_more":more}

    def conversation(self, identity: str, *, limit: int = 50, before: str | None = None):
        page_limit(limit)
        with self.s.lock:
            result = self.require_conversation(identity)
            args: tuple = (identity,)
            where = ""
            if before is not None:
                args = (*args,*read_page_cursor(before,"conversation_tasks",identity))
                where = " AND (t.created_at,t.id)<(?,?)"
            rows = self.s.all(TASK_SUMMARY_SELECT+" WHERE t.conversation_id=?"+where+
                              " ORDER BY t.created_at DESC,t.id DESC LIMIT ?",(*args,limit+1))
            more = len(rows)>limit
            items = list(reversed(rows[:limit]))
            for item in items:
                item["token_usage_estimated"] = bool(item["token_usage_estimated"])
            next_before = page_cursor("conversation_tasks",items[0]["created_at"],items[0]["id"],identity) if more else None
            return {**result,"tasks":items,"next_before":next_before,"has_more":more}

    def conversation_messages(self, identity: str, *, limit: int = 50, before: str | None = None):
        page_limit(limit)
        with self.s.lock:
            self.require_conversation(identity)
            args: tuple = (identity,)
            where = ""
            if before is not None:
                args = (*args,*read_page_cursor(before,"conversation_messages",identity))
                where = " AND (m.created_at,m.id)<(?,?)"
            rows = self.s.all("SELECT m.id,m.task_id,m.role,m.content,m.created_at FROM messages m "
                              "JOIN tasks t ON t.id=m.task_id WHERE t.conversation_id=?"+where+
                              " ORDER BY m.created_at DESC,m.id DESC LIMIT ?",(*args,limit+1))
            more = len(rows)>limit
            items = list(reversed(rows[:limit]))
            next_before = page_cursor("conversation_messages",items[0]["created_at"],items[0]["id"],identity) if more else None
            return {"items":items,"next_before":next_before,"has_more":more}

    @staticmethod
    def request_receipt(row, db):
        task = db.execute("SELECT conversation_id FROM tasks WHERE id=?",(row["task_id"],)).fetchone()
        return {"accepted":True,"request_id":row["id"],"task_id":row["task_id"],
                "task_revision":row["task_revision"],"conversation_id":task["conversation_id"]}

    def command_receipt(self,db,body,task_id,kind):
        identity=body.get("command_id")
        if not isinstance(identity,str) or not identity or len(identity)>128:
            raise BotError("missing_command_id","操作需要稳定的命令编号")
        old=db.execute("SELECT task_id,response FROM commands WHERE id=?",(identity,)).fetchone()
        if old:
            hashed=db.execute("SELECT digest FROM command_digests WHERE id=?",(identity,)).fetchone()
            if old["task_id"] != task_id or hashed and hashed[0] != digest({"kind":kind,"body":body}):
                raise BotError("idempotency_conflict","命令编号已用于不同的操作",409)
            return json.loads(old["response"])
        return None

    def command_record(self,db,body,task_id,kind,result):
        db.execute("INSERT INTO commands VALUES(?,?,?)",(body["command_id"],task_id,dumps(result)))
        db.execute("INSERT INTO command_digests VALUES(?,?)",(body["command_id"],digest({"kind":kind,"body":body})))

    def new_task(self, db, text, *, connection=None, parent=None, schedule=None, files=None,role="agent",conversation=None,allow_conversation_queue=False) -> dict:
        identity, stamp = uid(), now()
        if conversation is None and parent is not None:
            conversation = self.task(parent,db)["conversation_id"]
        if conversation is not None:
            self.require_conversation(conversation,db)
            if not allow_conversation_queue and db.execute("SELECT 1 FROM tasks WHERE conversation_id=? AND status NOT IN ('succeeded','failed','cancelled') LIMIT 1",(conversation,)).fetchone():
                raise BotError("conversation_busy","当前会话还有未结束的任务，请补充指令或先处理当前任务",409)
            if db.execute("SELECT 1 FROM tool_calls x JOIN tasks t ON t.id=x.task_id WHERE t.conversation_id=? AND x.status IN ('dispatched','unknown') LIMIT 1",(conversation,)).fetchone():
                raise BotError("reconciliation_required","请先核对本会话中结果不确定的外部操作",409)
        else:
            conversation = uid()
            db.execute("INSERT INTO conversations VALUES(?,?,?,?)",(conversation,text[:64],stamp,stamp))
        if connection is None:
            connection = self.connections.bind(role,schedule=bool(schedule))
        self.connections.allowed(connection,role,schedule=bool(schedule))
        budgets = json.loads(self.s.meta("budgets", '{"max_tools":30,"max_tokens":100000,"max_active_seconds":900}',db=db))
        budgets = self.connections.enroll_overage_task(connection,identity,budgets,db,role=role,schedule=bool(schedule))
        db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,connection_id,connection_revision,model_id,parent_task_id,schedule_id,max_tools,max_tokens,max_active_seconds,conversation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(identity,text[:64],text,"queued",stamp,stamp,connection["id"],connection["revision"],connection["model_id"],parent,schedule,budgets["max_tools"],budgets["max_tokens"],budgets["max_active_seconds"],conversation))
        db.execute("UPDATE tasks SET execution_mode=? WHERE id=?",(role,identity))
        # A follow-up can read active files explicitly granted to this same
        # conversation. Deleted grants/files and unrelated conversations never
        # confer access; this transfers no approval to perform external effects.
        db.execute("INSERT OR IGNORE INTO task_file_grants(task_id,file_id) "
                   "SELECT DISTINCT ?,g.file_id FROM task_file_grants g JOIN tasks t ON t.id=g.task_id "
                   "JOIN files f ON f.id=g.file_id WHERE t.conversation_id=? AND f.status IN ('uploaded','generated')",
                   (identity,conversation))
        for file_id in files or []:
            row = db.execute("SELECT status FROM files WHERE id=?",(file_id,)).fetchone()
            if row is None or row[0] not in ("uploaded","generated"):
                raise BotError("file_unavailable","附加文件不存在或已删除",409)
            db.execute("INSERT OR IGNORE INTO task_file_grants VALUES(?,?)",(identity,file_id))
        db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(uid(),identity,"user",text,stamp))
        db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(stamp,conversation))
        self.s.event(db,identity,"task.queued",{"revision":1})
        self.s.activity(db)
        return self.task(identity,db)

    def accept(self, body: dict) -> dict:
        request_id, text = body.get("client_request_id"),str(body.get("text","")).strip()
        if not request_id or len(str(request_id)) > 128 or not text or len(text) > 100000:
            raise BotError("invalid_request","请提供请求编号及有效内容")
        conversation_id = body.get("conversation_id")
        if conversation_id is not None and (not isinstance(conversation_id,str) or not conversation_id or len(conversation_id)>128):
            raise BotError("invalid_request","会话编号格式无效")
        files = body.get("file_ids",[])
        if not isinstance(files,list) or any(not isinstance(identity,str) or not identity or len(identity)>128 for identity in files):
            raise BotError("invalid_request","附件编号格式无效")
        request_digest = digest(body)
        with self.s.tx() as db:
            existing = db.execute("SELECT * FROM requests WHERE id=?",(request_id,)).fetchone()
            if existing:
                previous = db.execute("SELECT digest FROM request_digests WHERE id=?",(request_id,)).fetchone()
                if previous and previous[0] != request_digest:
                    raise BotError("idempotency_conflict","同一请求编号的内容不同",409)
                return self.request_receipt(existing,db)
            self.s.writable(db,intake=True)
            mode = body.get("mode","new")
            if mode == "new":
                role = body.get("execution_mode","agent")
                if role not in ("chat","agent"):
                    raise BotError("invalid_execution_mode","请选择仅回答或自动执行")
                parent = None
                if conversation_id is not None:
                    parent = self.require_conversation(conversation_id,db)["latest_task_id"]
                task = self.new_task(db,text,files=files,role=role,parent=parent,conversation=conversation_id)
            elif mode == "supplement" and body.get("task_id"):
                task = self.task(body["task_id"],db)
                if conversation_id is not None and task["conversation_id"] != conversation_id:
                    raise BotError("conversation_mismatch","任务不属于当前会话",409)
                if task["status"] in TERMINAL:
                    raise BotError("task_closed","已结束任务不能补充，请新建关联任务",409)
                rev = task["revision"]+1
                for file_id in body.get("file_ids",[]):
                    file = db.execute("SELECT status FROM files WHERE id=?",(file_id,)).fetchone()
                    if not file or file[0] not in ("uploaded","generated"):
                        raise BotError("file_unavailable","附加文件不可用",409)
                    db.execute("INSERT OR IGNORE INTO task_file_grants VALUES(?,?)",(task["id"],file_id))
                db.execute("UPDATE tasks SET revision=?,status='queued',attempt_id=NULL,checkpoint_id=NULL,context_generation=context_generation+1,updated_at=? WHERE id=?",(rev,now(),task["id"]))
                db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')",(task["id"],))
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(uid(),task["id"],"user",text,now()))
                self.s.invalidate_wait(db,task)
                self.s.event(db,task["id"],"task.supplement",{"revision":rev})
                db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now(),task["conversation_id"]))
                self.s.activity(db)
                task = self.task(task["id"],db)
            else:
                raise BotError("invalid_mode","请求模式无效")
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?)",(request_id,task["id"],task["revision"],text,now()))
            db.execute("INSERT INTO request_digests VALUES(?,?)",(request_id,request_digest))
            return {"accepted":True,"request_id":request_id,"task_id":task["id"],"task_revision":task["revision"],"conversation_id":task["conversation_id"]}

    def command(self, identity: str, body: dict) -> dict:
        with self.s.tx() as db:
            previous=self.command_receipt(db,body,identity,"task.command")
            if previous is not None:
                return previous
            self.s.writable(db,intake=True)
            task = self.task(identity,db)
            if body.get("expected_revision") != task["revision"]:
                raise BotError("revision_conflict","任务状态已更新，请刷新",409)
            action = body.get("action")
            if not body.get("command_id"):
                raise BotError("missing_command_id","命令缺少幂等编号")
            if action == "rerun":
                files = [r[0] for r in db.execute("SELECT g.file_id FROM task_file_grants g JOIN files f ON f.id=g.file_id WHERE g.task_id=? AND f.status IN ('uploaded','generated')",(identity,))]
                connection = None
                if self.s.meta("overage_task:"+identity,db=db):
                    connection = self.connections.get(task["connection_id"],revision=task["connection_revision"])
                    self.connections.task_overage_approval(connection,task)
                # Explicit rerun retains its existing ability to queue another
                # linked execution and consumes its own billing slot. Ordinary
                # conversation follow-ups cannot bypass the unfinished task.
                result = self.new_task(db,body.get("text") or task["prompt"],connection=connection,parent=identity,files=files,role=task["execution_mode"],allow_conversation_queue=True)
            else:
                if task["status"] in TERMINAL:
                    raise BotError("task_closed","任务已结束，不能继续或改变其状态",409)
                if action == "continue":
                    if task["unresolved_effect_count"]:
                        raise BotError("reconciliation_required","请先核对未确定的外部操作",409)
                    if task["status"] == "waiting_approval":
                        raise BotError("approval_required","请先处理结构化审批",409)
                    if body.get("connection_id"):
                        connection = self.connections.get(body["connection_id"])
                        self.connections.allowed(connection,task["execution_mode"],schedule=bool(task["schedule_id"]))
                        self.connections.task_overage_approval(connection,task)
                        db.execute("UPDATE tasks SET connection_id=?,connection_revision=?,model_id=?,context_generation=context_generation+1,checkpoint_id=NULL WHERE id=?",(connection["id"],connection["revision"],connection["model_id"],identity))
                    else:
                        connection = self.connections.get(task["connection_id"],revision=task["connection_revision"])
                        self.connections.task_overage_approval(connection,task)
                    if task["token_count"] >= task["max_tokens"] or task["tool_count"] >= task["max_tools"] or task["active_seconds"] >= task["max_active_seconds"]:
                        raise BotError("budget_exhausted","请先调整当前任务预算再继续",409)
                    status,pause,cancel = "queued",0,0
                elif action == "pause":
                    status,pause,cancel = "paused",1,0
                elif action == "cancel":
                    status,pause,cancel = "cancelled",0,1
                else:
                    raise BotError("invalid_action","任务控制操作无效")
                db.execute("UPDATE tasks SET status=?,pause_requested=?,cancel_requested=?,revision=revision+1,attempt_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",(status,pause,cancel,now(),identity))
                db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')",(identity,))
                self.s.invalidate_wait(db,task)
                self.s.event(db,identity,"task."+action,{"revision":task["revision"]+1})
                self.s.activity(db)
                result = self.task(identity,db)
            self.command_record(db,body,identity,"task.command",result)
            return result

    def claim(self, runtime_id: str) -> dict | None:
        if runtime_id != self.s.config.runtime_id:
            raise BotError("stale_runtime","运行环境已失效",409)
        # An empty poll needs no write reservation. All admission conditions
        # are rechecked under the original transaction when work exists.
        with self.s.lock:
            if not self.s.db.execute("SELECT 1 FROM tasks WHERE status='queued' LIMIT 1").fetchone():
                return None
        with self.s.tx() as db:
            if self.s.meta("intake",db=db) != "open" or self.s.meta("maintenance",db=db):
                return None
            if any(lease["kind"]=="task" for lease in self.leases.values()):
                return None
            if db.execute("SELECT 1 FROM tasks WHERE status='running'").fetchone():
                return None
            row = db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            task = dict(row)
            if db.execute("SELECT 1 FROM tool_calls WHERE task_id=? AND status IN ('dispatched','unknown')",(task["id"],)).fetchone():
                db.execute("UPDATE tasks SET status='needs_review',wait_reason='先核对不确定外部操作' WHERE id=?",(task["id"],))
                return None
            attempt = uid()
            db.execute("UPDATE tasks SET status='running',attempt_id=?,runtime_id=?,lease_until=?,updated_at=? WHERE id=?",(attempt,runtime_id,time.time()+30,now(),task["id"]))
            self.s.event(db,task["id"],"task.running",{"attempt_id":attempt,"revision":task["revision"]})
            self.s.activity(db)
            task = self.task(task["id"],db)
            task["messages"] = [dict(r) for r in db.execute(
                "SELECT m.role,m.content FROM messages m JOIN tasks t ON t.id=m.task_id "
                "WHERE t.conversation_id=? AND ((t.created_at,t.id)<(?,?) OR t.id=?) "
                "AND (t.id=? OR m.role IN ('user','assistant')) ORDER BY m.created_at,m.rowid",
                (task["conversation_id"],task["created_at"],task["id"],task["id"],task["id"]))]
            task["prior_turn_tools"] = [dict(r) for r in db.execute(
                "SELECT x.id,x.task_id,x.name,x.result FROM tool_calls x JOIN tasks t ON t.id=x.task_id "
                "WHERE t.conversation_id=? AND (t.created_at,t.id)<(?,?) AND x.status='succeeded' "
                "ORDER BY x.created_at,x.id",(task["conversation_id"],task["created_at"],task["id"]))]
            task["completed_tools"] = [dict(r) for r in db.execute("SELECT id,name,result FROM tool_calls WHERE task_id=? AND status='succeeded'",(task["id"],))]
            task["files"] = [dict(r) for r in db.execute("SELECT f.id,f.name,f.media_type,f.version,f.size FROM files f JOIN task_file_grants g ON f.id=g.file_id WHERE g.task_id=? AND f.status IN ('uploaded','generated')",(task["id"],))]
            return task

    def heartbeat(self, body):
        with self.s.tx() as db:
            self.s.guard_attempt(db,body["task_id"],body["attempt_id"],body["runtime_id"])
            db.execute("UPDATE tasks SET lease_until=? WHERE id=?",(time.time()+30,body["task_id"]))
        return {"ok":True}

    def finish(self, body):
        with self.s.tx() as db:
            task = self.task(body["task_id"],db)
            if task["attempt_id"] != body["attempt_id"] or task["runtime_id"] != body["runtime_id"]:
                return {"ignored":True}
            if task["status"] != "running":
                return {"ignored":True}
            status = body.get("status","succeeded")
            if status not in ("succeeded","failed","paused","needs_review"):
                raise BotError("invalid_status","执行结束状态无效")
            if task["unresolved_effect_count"]:
                status = "needs_review"
            result = str(body.get("result",""))[:500000]
            db.execute("UPDATE tasks SET status=?,result=?,summary=?,attempt_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",(status,result,result[:250],now(),task["id"]))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?",(now(),task["conversation_id"]))
            if result:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?)",(uid(),task["id"],"assistant",result,now()))
            self.s.event(db,task["id"],"task."+status,{"result":result})
            self.s.notify(db,task["id"],status)
            self.s.activity(db)
        return {"ok":True}

    def list_tools(self, task_id):
        self.task(task_id)
        items = self.s.all("SELECT t.*,COALESCE(r.revision,1) AS revision,r.reconciliation FROM tool_calls t LEFT JOIN tool_revisions r ON t.id=r.id WHERE t.task_id=? ORDER BY t.created_at",(task_id,))
        for item in items:
            for key in ("arguments","result","reconciliation"):
                if item[key] is not None:
                    item[key] = json.loads(item[key])
        return items

    def decision(self, identity, body):
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            approval = db.execute("SELECT * FROM approvals WHERE id=?",(identity,)).fetchone()
            if not approval:
                raise BotError("not_found","审批不存在",404)
            previous=self.command_receipt(db,body,approval["task_id"],"approval:"+identity)
            if previous is not None:
                return previous
            task = self.task(approval["task_id"],db)
            if approval["status"] != "pending" or approval["task_revision"] != task["revision"] or body.get("expected_revision") != task["revision"]:
                raise BotError("approval_stale","审批已失效，请检查新指令",409)
            if body.get("decision") not in ("approve","reject"):
                raise BotError("invalid_decision","审批决定无效")
            approved = body["decision"] == "approve"
            db.execute("UPDATE approvals SET status=?,decided_at=? WHERE id=?",("approved" if approved else "rejected",now(),identity))
            db.execute("UPDATE tasks SET status=?,wait_reason=NULL,attempt_id=NULL WHERE id=?",("queued" if approved else "paused",task["id"]))
            self.s.event(db,task["id"],"approval.decided",{"approval_id":identity,"decision":body["decision"]})
            self.s.activity(db)
            result = {"accepted":True,"approval_id":identity,"status":"approved" if approved else "rejected"}
            self.command_record(db,body,task["id"],"approval:"+identity,result)
            return result

    def reconcile(self, task_id, call_id, body):
        with self.s.tx() as db:
            previous=self.command_receipt(db,body,task_id,"reconcile:"+call_id)
            if previous is not None:
                return previous
            self.s.writable(db,intake=True)
            task = self.task(task_id,db)
            call = db.execute("SELECT t.*,r.revision AS tool_revision FROM tool_calls t JOIN tool_revisions r ON r.id=t.id WHERE t.id=? AND t.task_id=?",(call_id,task_id)).fetchone()
            if not call:
                raise BotError("not_found","工具记录不存在",404)
            if task["revision"] != body.get("expected_task_revision") or call["tool_revision"] != body.get("expected_tool_revision"):
                raise BotError("revision_conflict","核对记录已更新",409)
            if call["status"] not in ("unknown","dispatched"):
                raise BotError("not_uncertain","当前操作不需要结果核对",409)
            outcome = body.get("outcome")
            if outcome not in ("confirmed_done","confirmed_not_done","unresolved") or not body.get("evidence"):
                raise BotError("invalid_reconciliation","请提供有效核对结果和证据")
            status = {"confirmed_done":"succeeded","confirmed_not_done":"failed","unresolved":"unknown"}[outcome]
            evidence = {"source":"owner","outcome":outcome,"evidence":body["evidence"],"at":now()}
            db.execute("UPDATE tool_revisions SET revision=revision+1,reconciliation=? WHERE id=?",(dumps(evidence),call_id))
            db.execute("UPDATE tool_calls SET status=?,result=?,finished_at=? WHERE id=?",(status,dumps(evidence),now(),call_id))
            db.execute("UPDATE tasks SET revision=revision+1 WHERE id=?",(task_id,))
            db.execute("UPDATE approvals SET status='invalidated' WHERE tool_call_id=? AND status IN ('approved','pending')",(call_id,))
            self.s.event(db,task_id,"tool.reconciled",{"tool_call_id":call_id,**evidence})
            result = self.task(task_id,db)
            self.command_record(db,body,task_id,"reconcile:"+call_id,result)
            return result

    def save_schedule(self, body, identity=None):
        old = self.s.one("SELECT * FROM schedules WHERE id=?",(identity,)) if identity else None
        if identity and not old:
            raise BotError("not_found","排程不存在",404)
        if old and body.get("expected_revision") != old["revision"]:
            raise BotError("revision_conflict","排程已更新",409)
        value = {**(old or {}),**body}
        expression = value.get("cron","")
        zone = value.get("timezone","Asia/Shanghai")
        try:
            tz = ZoneInfo(zone)
            if len(expression.split()) != 5 or not croniter.is_valid(expression):
                raise ValueError()
            next_at = next_schedule(expression,zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise BotError("invalid_schedule","请使用五字段 cron 与有效 IANA 时区")
        if not value.get("prompt") or not value.get("title"):
            raise BotError("invalid_schedule","排程需要标题和任务说明")
        enabled = bool(value.get("enabled",True))
        if enabled:
            self.connections.bind("agent",schedule=True)
        identity, revision = identity or uid(), (old["revision"]+1 if old else 1)
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            db.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,prompt=excluded.prompt,cron=excluded.cron,timezone=excluded.timezone,enabled=excluded.enabled,revision=excluded.revision,registration_status='pending',next_fire_at=excluded.next_fire_at",(identity,value["title"],value["prompt"],expression,zone,int(enabled),revision,"pending",next_at,now()))
            self.s.outbox(db,"schedule.upsert" if enabled else "schedule.disable",{"schedule_id":identity,"revision":revision,"enabled":enabled,"cron":expression,"timezone":zone,"next_fire_at":next_at if enabled else None})
            self.s.activity(db)
        result=self.s.one("SELECT * FROM schedules WHERE id=?",(identity,))
        result["enabled"]=bool(result["enabled"])
        return result

    def dispatch(self, body):
        identity = body.get("event_id")
        if not identity:
            raise BotError("invalid_event","缺少事件编号")
        hashed = digest(body)
        with self.s.tx() as db:
            old = db.execute("SELECT response FROM dispatches WHERE event_id=?",(identity,)).fetchone()
            if old:
                check = db.execute("SELECT digest FROM dispatch_digests WHERE id=?",(identity,)).fetchone()
                if check and check[0] != hashed:
                    raise BotError("idempotency_conflict","事件编号对应不同内容",409)
                return json.loads(old[0])
            self.s.writable(db,intake=True)
            payload, kind = body.get("payload",{}),body.get("kind")
            result = {"accepted":True,"event_id":identity,"skipped":True}
            if kind in ("schedule","schedule.fire"):
                schedule = db.execute("SELECT * FROM schedules WHERE id=?",(payload.get("schedule_id"),)).fetchone()
                if schedule and schedule["enabled"] and schedule["revision"] == payload.get("revision"):
                    blocked = db.execute("SELECT 1 FROM tasks t WHERE t.schedule_id=? AND (t.status NOT IN ('succeeded','failed','cancelled') OR EXISTS(SELECT 1 FROM tool_calls c WHERE c.task_id=t.id AND c.status IN ('unknown','dispatched')))",(schedule["id"],)).fetchone()
                    if not blocked:
                        task = self.new_task(db,schedule["prompt"],schedule=schedule["id"])
                        result.update({"task_id":task["id"],"skipped":False})
                    else:
                        result["reason"] = "previous_task_or_external_effect_unresolved"
            elif kind in ("model.wait","model.wake","model.wait.upsert"):
                task = db.execute("SELECT * FROM tasks WHERE id=?",(payload.get("task_id"),)).fetchone()
                if task and task["status"] == "waiting_model" and task["wait_generation"] == payload.get("wait_generation") and task["connection_revision"] == payload.get("connection_revision"):
                    db.execute("UPDATE tasks SET status='queued',wait_reason=NULL WHERE id=?",(task["id"],))
                    result.update({"task_id":task["id"],"skipped":False})
            else:
                raise BotError("invalid_event_kind","事件类型不支持")
            db.execute("INSERT INTO dispatches VALUES(?,?,?)",(identity,dumps(result),now()))
            db.execute("INSERT INTO dispatch_digests VALUES(?,?)",(identity,hashed))
            return result

    async def flush_outbox(self):
        records = self.s.all("SELECT * FROM outbox WHERE acked_at IS NULL ORDER BY created_at LIMIT 100")
        for record in records:
            try:
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.s.config.bridge_socket),base_url="http://host",timeout=5) as client:
                    response = await client.post("/internal/events",json={"event_id":record["id"],"kind":record["kind"],"payload":json.loads(record["payload"]),"runtime_id":self.s.config.runtime_id,"deployment_epoch":self.s.config.deployment_epoch})
                    if response.status_code != 200 or response.json() != {"accepted":True,"event_id":record["id"]}:
                        break
                with self.s.tx() as db:
                    db.execute("UPDATE outbox SET acked_at=?,attempts=attempts+1 WHERE id=?",(now(),record["id"]))
                    if record["kind"].startswith("schedule."):
                        payload = json.loads(record["payload"])
                        db.execute("UPDATE schedules SET registration_status='registered' WHERE id=? AND revision=?",(payload["schedule_id"],payload["revision"]))
            except (httpx.HTTPError,ValueError,OSError):
                with self.s.tx() as db:
                    db.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?",(record["id"],))
                break

    async def activity(self):
        from . import browser
        status = await browser.status()
        count = self.s.one("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running')")["n"]
        human = status.get("mode") == "human"
        stamp = self.s.meta("last_activity")
        if stamp != self.last_activity_stamp:
            self.last_progress,self.last_activity_stamp = time.monotonic(),stamp
        return {"runtime_id":self.s.config.runtime_id,"executable_tasks":count,"human_control":human,
                "last_activity_at":stamp,"idle_seconds":max(0,time.monotonic()-self.last_progress),"safe_idle":not count and not human and not self.leases,
                "intake_revision":int(self.s.meta("intake_revision","1")),"active_work_leases":[{k:v for k,v in row.items() if k != "started"} for row in self.leases.values()]}

    async def quiesce(self, body):
        from . import browser
        if self.s.config.production or body.get("runtime_id") is not None:
            if body.get("runtime_id") != self.s.config.runtime_id or body.get("deployment_epoch") != self.s.config.deployment_epoch:
                raise BotError("stale_runtime","回收命令指向不同的运行环境或部署代号",409)
        if not body.get("drain_id"):
            raise BotError("missing_drain_id","缺少回收代号")
        activity = await self.activity()
        with self.s.tx() as db:
            existing = self.s.meta("drain_id",db=db)
            if existing:
                if existing != body["drain_id"]:
                    raise BotError("drain_conflict","旧环境已在另一次回收中",409)
            else:
                revision = int(self.s.meta("intake_revision","1",db=db))
                if body.get("expected_intake_revision") != revision:
                    raise BotError("revision_conflict","入口版本已更新",409)
                count = db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')").fetchone()[0]
                if count or self.leases or activity["human_control"]:
                    return {"safe_to_remove":False,"runtime_id":self.s.config.runtime_id,"reason":"active_work"}
                self.s.setmeta("intake","closed",db)
                self.s.setmeta("drain_id",body["drain_id"],db)
                self.s.setmeta("intake_revision",str(revision+1),db)
        await browser.quiesce()
        await self.flush_outbox()
        pending = self.s.one("SELECT COUNT(*) AS n FROM outbox WHERE acked_at IS NULL")["n"]
        external = self.s.one("SELECT COUNT(*) AS n FROM tool_calls WHERE status='dispatched'")["n"]
        return {"safe_to_remove":not pending and not external and not self.leases,
                "deployment_epoch":self.s.config.deployment_epoch,"runtime_id":self.s.config.runtime_id,
                "drain_id":body["drain_id"],"intake_revision":int(self.s.meta("intake_revision")),
                "pending_external_actions":external,"pending_outbox":pending}

    async def maintenance(self, body):
        identity,stage = body.get("maintenance_id"),body.get("stage")
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise BotError("invalid_maintenance_id","维护代号必须为 UUID")
        if stage == "drain":
            with self.s.tx() as db:
                current = self.s.meta("maintenance",db=db)
                if current and not current.startswith(identity+":"):
                    raise BotError("maintenance_conflict","另一次维护正在进行",409)
                self.s.setmeta("maintenance",identity+":drain",db)
            await self.flush_outbox()
            return {"drained":not self.leases and not self.s.one("SELECT 1 FROM tasks WHERE status='running'"),"maintenance_id":identity}
        if stage == "freeze":
            if self.s.meta("maintenance") != identity+":drain" or self.leases or self.s.one("SELECT 1 FROM tasks WHERE status='running'") or self.s.one("SELECT 1 FROM outbox WHERE acked_at IS NULL"):
                raise BotError("maintenance_not_drained","维护尚未完成任务和事件交接",409)
            from . import browser
            await browser.quiesce()
            folder = self.s.config.app_dir/"maintenance"/identity
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            path = folder/"app.sqlite"
            import sqlite3
            with self.s.lock, sqlite3.connect(path) as target:
                self.s.db.backup(target)
            with self.s.tx() as db:
                self.s.setmeta("maintenance",identity+":freeze",db)
            return {"frozen":True,"snapshot_path":str(path)}
        if stage == "release":
            with self.s.tx() as db:
                current = self.s.meta("maintenance",db=db)
                if current and not current.startswith(identity+":"):
                    raise BotError("maintenance_conflict","维护代号不匹配",409)
                self.s.setmeta("maintenance","",db)
            return {"released":True}
        raise BotError("invalid_maintenance_stage","维护阶段无效")
