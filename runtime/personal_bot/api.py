from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import verify_owner, verify_worker
from .config import Config
from .errors import BotError
from .models import Models
from .presets import PRESETS
from .resources import Resources
from .service import Service, page_limit
from .store import Store, dumps, now
from .telemetry import emit_timing
from .tools import TOOLS, ToolRunner


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app):
        initialized_at = time.perf_counter()
        store = Store(config)
        service = Service(store)
        app.state.store,app.state.service = store,service
        app.state.models,app.state.resources = Models(store,service.connections),Resources(service)
        app.state.tools = ToolRunner(service)
        app.state.fatal = False
        async def background():
            counter = 0
            while True:
                await service.flush_outbox()
                expired = store.one("SELECT id FROM tasks WHERE status='running' AND lease_until<?",(time.time(),))
                if expired:
                    with store.tx() as db:
                        db.execute("UPDATE tasks SET status='needs_review',attempt_id=NULL WHERE status='running'")
                        db.execute("UPDATE tool_calls SET status='unknown' WHERE status='dispatched'")
                    app.state.fatal = True  # server stops both listeners; no hot reassignment
                    return
                counter += 1
                if counter%720 == 0 and not store.meta("maintenance"):
                    app.state.resources.cleanup_trash()
                await asyncio.sleep(5)
        process = asyncio.create_task(background())
        try:
            emit_timing("api_initialized",time.perf_counter()-initialized_at,runtime_id=config.runtime_id)
            yield
        finally:
            process.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process
            store.close()

    app = FastAPI(title="Personal Bot",version="0.1.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

    @app.exception_handler(BotError)
    async def bot_error(request,exc):
        return JSONResponse(exc.payload(),status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        error = BotError("invalid_request","请求字段格式不正确",422)
        return JSONResponse(error.payload(),status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request,exc):
        error = BotError("data_conflict","记录已存在或关联状态发生变化",409)
        return JSONResponse(error.payload(),status_code=409)

    @app.exception_handler(Exception)
    async def internal_error(request,exc):
        error = BotError("internal_error","内部处理失败，请使用诊断编号排查",500)
        return JSONResponse(error.payload(),status_code=500)

    @app.middleware("http")
    async def authenticate(request,call_next):
        try:
            if request.url.path.startswith("/health/"):
                return await call_next(request)
            if request.url.path.startswith("/worker/"):
                verify_worker(request,config)
            else:
                role = "control" if request.url.path.startswith("/internal/") else "owner"
                request.state.claims = verify_owner(request,config,role)
                if role == "owner" and request.method == "GET" and request.headers.get("X-Personal-Bot-Passive") != "1" and not request.url.path.endswith("/events"):
                    with app.state.store.tx() as db:
                        app.state.store.activity(db)
            if app.state.fatal:
                raise BotError("runtime_faulted","旧执行器租约失效，等待环境整体恢复",503,True)
            if request.method in ("POST","PATCH","PUT"):
                limit=25*1024*1024+65536 if request.url.path=="/api/files" else 8*1024*1024
                try:
                    if int(request.headers.get("Content-Length","0"))>limit:
                        raise BotError("request_too_large","请求正文超过允许大小",413)
                except ValueError:
                    raise BotError("invalid_content_length","正文长度格式无效")
                original_receive=request._receive
                received=0
                async def receive_limited():
                    nonlocal received
                    async with asyncio.timeout(60):
                        message=await original_receive()
                    received+=len(message.get("body",b""))
                    if received>limit:
                        raise BotError("request_too_large","请求正文超过允许大小",413)
                    return message
                request._receive=receive_limited
                if request.url.path=="/api/files":
                    async with app.state.service.work("transfer","upload:"+request.state.claims["request_id"],600):
                        return await call_next(request)
            return await call_next(request)
        except BotError as exc:
            return JSONResponse(exc.payload(),status_code=exc.status)

    @app.get("/health/live")
    async def live():
        return {"live":True}

    @app.get("/health/ready")
    async def ready():
        store = app.state.store
        is_ready = store.reconciled and not app.state.fatal
        return JSONResponse({"ready":is_ready,"runtime_id":config.runtime_id,"deployment_epoch":config.deployment_epoch,
            "reconciled":store.reconciled,"intake_revision":int(store.meta("intake_revision","1")),"schema_version":1,"version":"0.1.0"},status_code=200 if is_ready else 503)

    @app.post("/api/requests")
    async def accept(body: dict):
        return app.state.service.accept(body)

    @app.get("/api/requests/{identity}")
    async def receipt(identity: str):
        row = app.state.store.one("SELECT * FROM requests WHERE id=?",(identity,))
        if not row:
            raise BotError("not_found","请求尚未被持久接收",404)
        return app.state.service.request_receipt(row,app.state.store.db)

    @app.get("/api/conversations")
    async def conversations(limit: int = 50,cursor: str | None = None):
        return app.state.service.conversations(limit=limit,cursor=cursor)

    @app.get("/api/conversations/{identity}")
    async def conversation(identity: str,limit: int = 50,before: str | None = None):
        return app.state.service.conversation(identity,limit=limit,before=before)

    @app.get("/api/conversations/{identity}/messages")
    async def conversation_messages(identity: str,limit: int = 50,before: str | None = None):
        return app.state.service.conversation_messages(identity,limit=limit,before=before)

    @app.get("/api/tasks")
    async def tasks(view: str | None = None,limit: int = 50,cursor: str | None = None):
        if view == "summary":
            return app.state.service.task_summaries(limit=limit,cursor=cursor)
        if view is not None or cursor is not None:
            raise BotError("invalid_view","任务分页须使用 view=summary")
        return {"items":app.state.service.list_tasks()}

    @app.get("/api/tasks/{identity}")
    async def task(identity: str):
        return app.state.service.task(identity)

    @app.post("/api/tasks/{identity}/commands")
    async def command(identity: str,body: dict):
        return app.state.service.command(identity,body)

    def events(identity,after,limit=1000):
        app.state.service.require_task(identity)
        rows = app.state.store.all("SELECT * FROM task_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",(identity,after,limit))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    @app.get("/api/tasks/{identity}/history")
    async def history(identity: str,after: int = 0,limit: int = 1000):
        page_limit(limit,1000)
        if after<0:
            raise BotError("invalid_cursor","事件游标不能为负数")
        rows = events(identity,after,limit+1)
        items = rows[:limit]
        return {"items":items,"next_after":items[-1]["seq"] if items else after,"has_more":len(rows)>limit}

    @app.get("/api/tasks/{identity}/events")
    async def event_stream(identity: str,request: Request,after: int = 0):
        try:
            cursor = max(after,int(request.headers.get("Last-Event-ID","0")))
            if cursor<0:
                raise ValueError("Negative cursor")
        except ValueError:
            raise BotError("invalid_cursor","事件游标格式错误")
        app.state.service.require_task(identity)
        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                if app.state.store.meta("intake") != "open":
                    yield "event: runtime_sleeping\ndata: {}\n\n"
                    return
                rows = events(identity,cursor)
                for row in rows:
                    cursor = row["seq"]
                    yield f"id: {cursor}\nevent: task_event\ndata: {dumps(row)}\n\n"
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(generate(),media_type="text/event-stream",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

    @app.get("/api/messages")
    async def messages(task_id: str,view: str | None = None,limit: int = 50,before: str | None = None):
        if view == "page":
            return app.state.service.message_page(task_id,limit=limit,before=before)
        if view is not None or before is not None:
            raise BotError("invalid_view","消息分页须使用 view=page")
        app.state.service.require_task(task_id)
        return {"items":app.state.store.all("SELECT * FROM messages WHERE task_id=? ORDER BY created_at,rowid",(task_id,))}

    @app.get("/api/tasks/{identity}/tool-calls")
    async def calls(identity: str):
        return {"items":app.state.service.list_tools(identity)}

    @app.post("/api/tasks/{identity}/tool-calls/{call_id}/reconciliation")
    async def reconcile(identity: str,call_id: str,body: dict):
        return app.state.service.reconcile(identity,call_id,body)

    @app.get("/api/model-presets")
    async def presets():
        return {"items":PRESETS}

    @app.get("/api/model-connections")
    async def connections():
        return {"items":[app.state.service.connections.get(x["id"]) for x in app.state.store.all("SELECT id FROM model_connections ORDER BY created_at")]}

    @app.post("/api/model-connections")
    async def add_connection(body: dict):
        return app.state.service.connections.save(body)

    @app.patch("/api/model-connections/{identity}")
    async def edit_connection(identity: str,body: dict):
        return app.state.service.connections.save(body,identity)

    @app.delete("/api/model-connections/{identity}")
    async def delete_connection(identity: str,expected_revision: int):
        if any(lease["kind"] == "model_validation" and lease["owner"] == identity
               for lease in app.state.service.leases.values()):
            raise BotError("connection_in_use","该连接正在验证，请等待验证结束后再删除",409)
        return app.state.service.connections.delete(identity,expected_revision)

    @app.post("/api/model-connections/{identity}/overage-approval")
    async def approve_overage(identity: str,body: dict):
        return app.state.service.connections.approve_overage(identity,body)

    @app.delete("/api/model-connections/{identity}/overage-approval")
    async def revoke_overage(identity: str):
        return app.state.service.connections.revoke_overage(identity)

    @app.post("/api/model-connections/{identity}/validate")
    async def validate_connection(identity: str):
        async with app.state.service.work("model_validation",identity,120):
            return await app.state.models.validate(identity)

    @app.get("/api/quota-groups")
    async def quota_groups():
        groups = app.state.store.all("SELECT * FROM quota_groups")
        for group in groups:
            group["windows"] = app.state.store.all("SELECT * FROM quota_windows WHERE quota_group_id=?",(group["id"],))
            group["remaining_source"] = "供应商未提供自动查询时为未知；本地用量不等于账户余额"
        return {"items":groups}

    @app.patch("/api/quota-groups/{identity}")
    async def quota_update(identity: str,body: dict):
        from datetime import datetime
        store = app.state.store
        if not store.one("SELECT 1 FROM quota_groups WHERE id=?",(identity,)):
            raise BotError("not_found","额度组不存在",404)
        with store.tx() as db:
            store.writable(db)
            for window in body.get("windows",[]):
                name,remaining = window.get("name"),window.get("remaining")
                if not isinstance(name,str) or not name or remaining is not None and (not isinstance(remaining,(int,float)) or remaining<0):
                    raise BotError("invalid_quota_window","额度窗口格式无效")
                reset = window.get("reset_at")
                if reset:
                    try:
                        datetime.fromisoformat(reset.replace("Z","+00:00"))
                    except (ValueError,TypeError):
                        raise BotError("invalid_reset","额度恢复时间格式无效")
                # Manual records remain untrusted for automatic wake scheduling.
                db.execute("INSERT OR REPLACE INTO quota_windows VALUES(?,?,?,?,?,?,?)",(identity,name,remaining,reset,"owner_reported",now(),0))
        return {"items":store.all("SELECT * FROM quota_windows WHERE quota_group_id=?",(identity,))}

    @app.get("/api/settings")
    async def settings():
        store = app.state.store
        return {"default_chat_connection":store.meta("default_chat_connection") or None,
                "default_agent_connection":store.meta("default_agent_connection") or None,
                "budgets":json.loads(store.meta("budgets",'{"max_tools":30,"max_tokens":100000,"max_active_seconds":900}')),
                "timezone":"Asia/Shanghai"}

    @app.post("/api/settings/default-model")
    async def default_model(body: dict):
        role = body.get("role","agent")
        if role not in ("chat","agent"):
            raise BotError("invalid_role","模型角色无效")
        connection = app.state.service.connections.get(body["connection_id"])
        app.state.service.connections.allowed(connection,role)
        with app.state.store.tx() as db:
            app.state.store.writable(db)
            app.state.store.setmeta("default_"+role+"_connection",connection["id"],db)
        return await settings()

    def budget_values(body,defaults):
        values = {key:body.get(key,value) for key,value in defaults.items()}
        for key in values:
            if not isinstance(values[key],int) or not 1 <= values[key] <= {"max_tools":1000,"max_tokens":10000000,"max_active_seconds":86400}[key]:
                raise BotError("invalid_budget","任务预算超出允许范围")
        return values

    @app.patch("/api/settings/task-budgets")
    async def default_budgets(body: dict):
        current = await settings()
        values = budget_values(body,current["budgets"])
        with app.state.store.tx() as db:
            app.state.store.writable(db)
            app.state.store.setmeta("budgets",dumps(values),db)
        return await settings()

    @app.patch("/api/tasks/{identity}/budgets")
    async def task_budgets(identity: str,body: dict):
        with app.state.store.tx() as db:
            previous=app.state.service.command_receipt(db,body,identity,"task.budgets")
            if previous is not None:
                return previous
            app.state.store.writable(db)
            task = app.state.service.task(identity,db)
            if task["revision"] != body.get("expected_revision"):
                raise BotError("revision_conflict","任务版本已更新",409)
            values = budget_values(body,{key:task[key] for key in ("max_tools","max_tokens","max_active_seconds")})
            values = app.state.service.connections.cap_task_budgets(task,values)
            status=task["status"] if task["status"] in ("succeeded","failed","cancelled") else "paused"
            db.execute("UPDATE tasks SET max_tools=?,max_tokens=?,max_active_seconds=?,revision=revision+1,status=?,attempt_id=NULL,lease_until=NULL WHERE id=?",(values["max_tools"],values["max_tokens"],values["max_active_seconds"],status,identity))
            db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')",(identity,))
            app.state.store.invalidate_wait(db,task)
            app.state.store.event(db,identity,"task.budgets_changed",values)
            result=app.state.service.task(identity,db)
            app.state.service.command_record(db,body,identity,"task.budgets",result)
        return result

    @app.get("/api/files")
    async def files():
        return {"items":[app.state.resources.file(row["id"]) for row in app.state.store.all("SELECT id FROM files ORDER BY created_at DESC")]}

    @app.post("/api/files")
    async def upload(file: UploadFile = File(...)):
        return await app.state.resources.upload(file)

    @app.get("/api/files/{identity}/download")
    async def download(identity: str):
        file = app.state.resources.file(identity,include_path=True)
        if file["status"] not in ("uploaded","generated"):
            raise BotError("file_unavailable","文件已删除或不可用",410)
        async def chunks():
            async with app.state.service.work("transfer",identity,600):
                with open(file["path"],"rb") as source:
                    while chunk := source.read(65536):
                        async with asyncio.timeout(60):
                            yield chunk
        from urllib.parse import quote
        return StreamingResponse(chunks(),media_type="application/octet-stream",headers={"Content-Disposition":"attachment; filename*=UTF-8''"+quote(file["name"]),"X-Content-Type-Options":"nosniff","Cache-Control":"no-store"})

    @app.delete("/api/files/{identity}")
    async def delete_file(identity: str):
        return app.state.resources.delete_file(identity)

    @app.post("/api/files/{identity}/restore")
    async def restore_file(identity: str):
        return app.state.resources.restore_file(identity)

    @app.get("/api/memories")
    async def memories(q: str | None = None):
        return {"items":app.state.resources.search_memories(q) if q else app.state.store.all("SELECT * FROM memories ORDER BY created_at DESC")}

    @app.post("/api/memories")
    async def add_memory(body: dict):
        return app.state.resources.memory(body)

    @app.patch("/api/memories/{identity}")
    async def edit_memory(identity: str,body: dict):
        return app.state.resources.memory(body,identity)

    @app.delete("/api/memories/{identity}")
    async def delete_memory(identity: str,body: dict | None = None):
        return app.state.resources.delete_memory(identity,(body or {}).get("expected_revision"))

    @app.get("/api/schedules")
    async def schedules():
        rows=app.state.store.all("SELECT * FROM schedules ORDER BY created_at DESC")
        for row in rows:
            row["enabled"]=bool(row["enabled"])
        return {"items":rows}

    @app.post("/api/schedules")
    async def add_schedule(body: dict):
        return app.state.service.save_schedule(body)

    @app.patch("/api/schedules/{identity}")
    async def edit_schedule(identity: str,body: dict):
        return app.state.service.save_schedule(body,identity)

    @app.delete("/api/schedules/{identity}")
    async def delete_schedule(identity: str,body: dict | None = None,expected_revision: int | None = None):
        revision = (body or {}).get("expected_revision",expected_revision)
        return app.state.service.save_schedule({"enabled":False,"expected_revision":revision},identity)

    @app.get("/api/approvals")
    async def approvals(task_id: str | None = None):
        rows = app.state.store.all("SELECT * FROM approvals"+(" WHERE task_id=?" if task_id else "")+" ORDER BY created_at DESC",(task_id,) if task_id else ())
        for row in rows:
            row["action"] = json.loads(row["action"])
        return {"items":rows}

    @app.post("/api/approvals/{identity}/decision")
    async def decision(identity: str,body: dict):
        return app.state.service.decision(identity,body)

    @app.get("/api/browser/status")
    async def browser_status():
        from . import browser
        return await browser.status()

    @app.post("/api/browser/control")
    async def browser_control(request: Request,body: dict):
        from . import browser
        expected_task_id=body.get("expected_task_id")
        if "expected_task_id" in body:
            if not isinstance(expected_task_id,str) or not expected_task_id:
                raise BotError("invalid_request","浏览器操作需要当前任务标识")
            app.state.service.require_task(expected_task_id)
        with app.state.store.tx() as db:
            app.state.store.writable(db,intake=True)
            app.state.store.activity(db)
        result=await browser.control(body["action"],body["device_id"],body.get("browser_epoch"),session_id=request.state.claims.get("session_id"),expected_task_id=expected_task_id)
        task_id=result.get("task_id")
        if body["action"] in ("takeover","transfer") and task_id:
            with app.state.store.tx() as db:
                task=db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
                if task and task["status"] not in ("succeeded","failed","cancelled"):
                    db.execute("UPDATE tasks SET status='waiting_human',revision=revision+1,attempt_id=NULL,lease_until=NULL,context_generation=context_generation+1,checkpoint_id=NULL,wait_reason='浏览器已由所有者接管，交还后请明确继续' WHERE id=?",(task_id,))
                    db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')",(task_id,))
                    app.state.store.invalidate_wait(db,dict(task))
                    app.state.store.event(db,task_id,"browser.human_control",{"device_id":body["device_id"],"browser_epoch":result.get("browser_epoch")})
        return result

    @app.post("/api/browser/view-sessions")
    async def browser_view(request: Request,body: dict):
        from . import browser
        expected_task_id=body.get("expected_task_id")
        if "expected_task_id" in body:
            if not isinstance(expected_task_id,str) or not expected_task_id:
                raise BotError("invalid_request","观看浏览器需要当前任务标识")
            app.state.service.require_task(expected_task_id)
        return await browser.create_view_session(body["device_id"],request.state.claims["sub"],session_id=request.state.claims.get("session_id"),expected_task_id=expected_task_id)

    @app.websocket("/api/browser/view/{identity}")
    async def browser_ws(websocket: WebSocket,identity: str):
        from . import browser
        try:
            # Gateway performs browser cookie/Origin checks and signs this hop.
            claims = verify_owner(websocket,config)
            await browser.proxy_view(websocket,identity,claims["sub"],session_id=claims.get("session_id"))
        except BotError:
            await websocket.close(code=4403)

    @app.get("/internal/activity")
    async def activity():
        return await app.state.service.activity()

    @app.get("/internal/diagnostics")
    async def diagnostics():
        import shutil
        store=app.state.store
        disk=shutil.disk_usage(config.files_dir)
        return {"version":"0.1.0","schema_version":1,"sqlite_version":sqlite3.sqlite_version,
                "runtime_id":config.runtime_id,"deployment_epoch":config.deployment_epoch,
                "reconciled":store.reconciled,"fatal":app.state.fatal,"intake":store.meta("intake"),
                "task_counts":{row["status"]:row["n"] for row in store.all("SELECT status,COUNT(*) AS n FROM tasks GROUP BY status")},
                "pending_outbox":store.one("SELECT COUNT(*) AS n FROM outbox WHERE acked_at IS NULL")["n"],
                "unresolved_effects":store.one("SELECT COUNT(*) AS n FROM tool_calls WHERE status IN ('unknown','dispatched')")["n"],
                "storage":{"free_bytes":disk.free,"pressure":disk.free<256*1024*1024},"generated_at":now()}

    @app.post("/internal/quiesce")
    async def quiesce(body: dict):
        result = await app.state.service.quiesce(body)
        return JSONResponse(result,status_code=200 if result.get("safe_to_remove") else (503 if app.state.store.meta("intake") != "open" else 409))

    @app.post("/internal/dispatch")
    async def dispatch(body: dict):
        return app.state.service.dispatch(body)

    @app.post("/internal/maintenance/stage")
    async def maintenance(body: dict):
        return await app.state.service.maintenance(body)

    @app.post("/internal/revoke-session")
    async def revoke(body: dict):
        from . import browser
        return await browser.revoke_session(body["session_id"])

    @app.post("/worker/claim")
    async def claim(body: dict):
        task = app.state.service.claim(body["runtime_id"])
        return {"task":task,"tools":TOOLS if task else []}

    @app.post("/worker/heartbeat")
    async def heartbeat(body: dict):
        return app.state.service.heartbeat(body)

    @app.post("/worker/finish")
    async def finish(body: dict):
        return app.state.service.finish(body)

    @app.post("/worker/model")
    async def model(body: dict):
        async with app.state.service.work("task",body["task_id"],550):
            try:
                return await app.state.models.task_call(body["task_id"],body["attempt_id"],body["runtime_id"],body["revision"],body["step"],body["messages"],TOOLS if body.get("use_tools",True) else [])
            except BotError as exc:
                if exc.code in ("stale_attempt","revision_conflict"):
                    raise
                store=app.state.store
                with store.tx() as db:
                    task=store.guard_attempt(db,body["task_id"],body["attempt_id"],body["runtime_id"],body["revision"])
                    generation=task["wait_generation"]+1
                    db.execute("UPDATE tasks SET status='waiting_model',wait_reason=?,wait_generation=?,attempt_id=NULL,lease_until=NULL WHERE id=?",(exc.message,generation,task["id"]))
                    store.outbox(db,"model.wait.upsert",{"task_id":task["id"],"wait_generation":generation,"connection_revision":task["connection_revision"],"enabled":False,"resume_at":None})
                    store.event(db,task["id"],"task.waiting_model",{"code":exc.code})
                return {"halted":True}

    @app.post("/worker/tool")
    async def run_tool(body: dict):
        return await app.state.tools.invoke(body)

    @app.post("/worker/checkpoints/get")
    async def checkpoint_get(body: dict):
        store = app.state.store
        with store.tx() as db:
            task = store.guard_attempt(db,body["task_id"],body["attempt_id"],body["runtime_id"])
            namespace = str(task["context_generation"])+":"+body.get("namespace","")
            checkpoint_id = body.get("checkpoint_id")
            query = "SELECT * FROM checkpoints WHERE task_id=? AND namespace=?"
            args = [task["id"],namespace]
            if checkpoint_id:
                query += " AND checkpoint_id=?"
                args.append(checkpoint_id)
            row = db.execute(query+" ORDER BY checkpoint_id DESC LIMIT 1",args).fetchone()
            if not row:
                return {"checkpoint":None}
            value = dict(row)
            value["writes"] = [dict(r) for r in db.execute("SELECT * FROM checkpoint_writes WHERE task_id=? AND namespace=? AND checkpoint_id=? ORDER BY writer_id,idx",(task["id"],namespace,value["checkpoint_id"]))]
            return {"checkpoint":value}

    @app.post("/worker/checkpoints/put")
    async def checkpoint_put(body: dict):
        store = app.state.store
        with store.tx() as db:
            task = store.guard_attempt(db,body["task_id"],body["attempt_id"],body["runtime_id"],body["revision"])
            namespace = str(task["context_generation"])+":"+body.get("namespace","")
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?)",(task["id"],namespace,body["checkpoint_id"],body.get("parent_id"),dumps(body["checkpoint"]),dumps(body["metadata"]),now(),task["context_generation"]))
            db.execute("UPDATE tasks SET checkpoint_id=? WHERE id=?",(body["checkpoint_id"],task["id"]))
        return {"ok":True}

    @app.post("/worker/checkpoints/writes")
    async def checkpoint_writes(body: dict):
        store = app.state.store
        with store.tx() as db:
            task = store.guard_attempt(db,body["task_id"],body["attempt_id"],body["runtime_id"],body["revision"])
            namespace = str(task["context_generation"])+":"+body.get("namespace","")
            for index,write in enumerate(body["writes"]):
                write_index=write.get("index",index)
                statement="INSERT OR REPLACE" if write_index<0 else "INSERT OR IGNORE"
                db.execute(statement+" INTO checkpoint_writes VALUES(?,?,?,?,?,?,?)",(task["id"],namespace,body["checkpoint_id"],body["writer_id"],write_index,write["channel"],dumps(write["value"])))
        return {"ok":True}

    return app


app = create_app()
