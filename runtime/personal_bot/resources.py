from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import shutil
from pathlib import Path

from .errors import BotError
from .service import Service, digest
from .store import dumps, now, uid

MAX_UPLOAD = 25*1024*1024


class Resources:
    def __init__(self, service: Service):
        self.service, self.s = service, service.s

    async def upload(self, file) -> dict:
        self.capacity()
        identity = uid()
        temporary = self.s.config.files_dir/(identity+".partial")
        final = self.s.config.files_dir/identity
        size, checksum = 0,hashlib.sha256()
        try:
            async with self.service.work("transfer",identity,600):
                with temporary.open("xb") as target:
                    while True:
                        async with __import__('asyncio').timeout(60):
                            chunk = await file.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            raise BotError("upload_too_large","单文件不能超过 25 MiB",413)
                        target.write(chunk)
                        checksum.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary,final)
                if os.name != "nt":
                    os.chmod(final,0o600)
                with self.s.tx() as db:
                    self.s.writable(db,intake=True)
                    db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?)",(identity,Path(file.filename or "file").name,file.content_type or "application/octet-stream",size,"uploaded",1,None,str(final),checksum.hexdigest(),now(),None))
                return self.file(identity)
        except BaseException:
            temporary.unlink(missing_ok=True)
            # Unpublished files are not formal user data.
            if not self.s.one("SELECT 1 FROM files WHERE id=?",(identity,)):
                final.unlink(missing_ok=True)
            raise

    def file(self, identity, *, include_path=False, task_id=None):
        row = self.s.one("SELECT * FROM files WHERE id=?",(identity,))
        if row is None:
            raise BotError("not_found","文件不存在",404)
        if task_id and not self.s.one("SELECT 1 FROM task_file_grants WHERE task_id=? AND file_id=?",(task_id,identity)):
            raise BotError("file_not_granted","任务未获得此文件的读取授权",403)
        if not include_path:
            row.pop("path",None)
            row.pop("sha256",None)
        return row

    def delete_file(self, identity):
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            file = db.execute("SELECT * FROM files WHERE id=?",(identity,)).fetchone()
            if not file:
                raise BotError("not_found","文件不存在",404)
            tasks = [r[0] for r in db.execute("SELECT task_id FROM task_file_grants WHERE file_id=?",(identity,))]
            if any(lease["owner"] in tasks for lease in self.service.leases.values()):
                raise BotError("file_in_use","关联运行正在停止，请先暂停任务并待当前步骤结束后重试",409,True)
            for task_id in tasks:
                task = self.s.task(task_id,db)
                if task["status"] not in ("succeeded","failed","cancelled"):
                    db.execute("UPDATE tasks SET status='paused',attempt_id=NULL,lease_until=NULL,context_generation=context_generation+1,checkpoint_id=NULL,revision=revision+1,wait_reason='关联文件已删除，请重新确认上下文' WHERE id=?",(task_id,))
                db.execute("DELETE FROM checkpoints WHERE task_id=?",(task_id,))
                db.execute("DELETE FROM checkpoint_writes WHERE task_id=?",(task_id,))
                db.execute("DELETE FROM model_steps WHERE task_id=?",(task_id,))
                for call in db.execute("SELECT id,arguments FROM tool_calls WHERE task_id=?",(task_id,)).fetchall():
                    arguments=json.loads(call["arguments"])
                    references=arguments.get("file_ids",[])+([arguments["file_id"]] if arguments.get("file_id") else [])
                    if identity in references:
                        db.execute("UPDATE tool_calls SET result=? WHERE id=?",(dumps({"redacted":True,"reason":"source_file_deleted","file_id":identity}),call["id"]))
                db.execute("UPDATE approvals SET status='invalidated' WHERE task_id=? AND status IN ('pending','approved')",(task_id,))
                self.s.event(db,task_id,"file.deleted",{"file_id":identity})
            db.execute("DELETE FROM parse_cache WHERE file_id=?",(identity,))
            db.execute("DELETE FROM task_file_grants WHERE file_id=?",(identity,))
            db.execute("UPDATE files SET status='trashed',deleted_at=? WHERE id=?",(now(),identity))
            self.s.activity(db)
        return {"deleted":True,"affected_task_ids":tasks,"trash_days":7,
                "retained":"可见聊天、独立产物及已确认记忆可能仍保留引用或内容，可分别清理；已发送给模型的内容无法撤回"}

    def restore_file(self, identity):
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            file = self.file(identity,include_path=True)
            if file["status"] != "trashed" or not Path(file["path"]).is_file():
                raise BotError("not_restorable","文件不在可恢复回收站中",409)
            db.execute("UPDATE files SET status=?,deleted_at=NULL WHERE id=?",("generated" if file["task_id"] else "uploaded",identity))
            self.s.activity(db)
        return self.file(identity)

    def publish(self, task_id, path: Path, name: str, media_type: str) -> dict:
        self.capacity()
        path = path.resolve(strict=True) if not path.is_symlink() else path
        scratch = Path(os.getenv("PERSONAL_BOT_OUTPUT_ROOT","/scratch/outputs")).resolve()
        if not path.is_relative_to(scratch) or path.is_symlink():
            raise BotError("unsafe_artifact","产物路径或链接不在允许范围",403)
        flags = os.O_RDONLY | getattr(os,"O_NOFOLLOW",0)
        descriptor = os.open(path,flags)
        identity, checksum = uid(),hashlib.sha256()
        final = self.s.config.files_dir/identity
        size = 0
        try:
            with os.fdopen(descriptor,"rb") as source, final.open("xb") as target:
                attributes = os.fstat(source.fileno())
                if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
                    raise BotError("unsafe_artifact","产物必须为无硬链接的普通文件")
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > 200*1024*1024:
                        raise BotError("artifact_too_large","产物超过 200 MiB 上限")
                    target.write(chunk)
                    checksum.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            with self.s.tx() as db:
                self.s.writable(db)
                db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?)",(identity,Path(name).name,media_type,size,"generated",1,task_id,str(final),checksum.hexdigest(),now(),None))
                db.execute("INSERT INTO task_file_grants VALUES(?,?)",(task_id,identity))
                self.s.event(db,task_id,"artifact.published",{"file_id":identity,"name":Path(name).name})
            try:
                path.unlink()
                if path.parent != scratch:
                    path.parent.rmdir()
            except OSError:
                pass  # Runtime teardown removes any remaining scratch files.
            return self.file(identity)
        except BaseException:
            final.unlink(missing_ok=True)
            raise

    def memory(self, body, identity=None, *, candidate=False):
        old = self.s.one("SELECT * FROM memories WHERE id=?",(identity,)) if identity else None
        if identity and not old:
            raise BotError("not_found","记忆不存在",404)
        if old and body.get("expected_revision") != old["revision"]:
            raise BotError("revision_conflict","记忆已更新",409)
        value = {**(old or {}),**body}
        content = str(value.get("content","")).strip()
        if not content or len(content)>20000:
            raise BotError("invalid_memory","记忆内容无效")
        source = str(value.get("source","owner"))
        item_key = str(value.get("item_key",digest(content)))
        status = "candidate" if candidate else value.get("status","confirmed")
        if status not in ("candidate","confirmed"):
            raise BotError("invalid_memory_status","记忆状态无效")
        identity = identity or uid()
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            suppressed = db.execute("SELECT 1 FROM memory_suppression WHERE source=? AND item_key=?",(source,item_key)).fetchone()
            if suppressed and candidate:
                return {"suppressed":True}
            if suppressed and body.get("restore_suppressed") is not True:
                raise BotError("memory_suppressed","同一来源的该条内容已拒绝或删除；重新记住须明确恢复",409)
            if suppressed:
                db.execute("DELETE FROM memory_suppression WHERE source=? AND item_key=?",(source,item_key))
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET content=excluded.content,status=excluded.status,revision=memories.revision+1",(identity,content,status,source,item_key,1,now()))
            db.execute("DELETE FROM memory_fts WHERE id=?",(identity,))
            if status == "confirmed":
                db.execute("INSERT INTO memory_fts(id,content) VALUES(?,?)",(identity,content))
            self.s.activity(db)
        return self.s.one("SELECT * FROM memories WHERE id=?",(identity,))

    def delete_memory(self, identity, expected_revision=None):
        with self.s.tx() as db:
            self.s.writable(db,intake=True)
            old = db.execute("SELECT * FROM memories WHERE id=?",(identity,)).fetchone()
            if not old:
                raise BotError("not_found","记忆不存在",404)
            if expected_revision is not None and old["revision"] != expected_revision:
                raise BotError("revision_conflict","记忆已更新",409)
            db.execute("INSERT OR IGNORE INTO memory_suppression VALUES(?,?,?)",(old["source"],old["item_key"],now()))
            db.execute("DELETE FROM memory_fts WHERE id=?",(identity,))
            db.execute("DELETE FROM memories WHERE id=?",(identity,))
        return {"deleted":True}

    def search_memories(self, query):
        words = [w.replace('"','""') for w in str(query).split()[:10] if w]
        if not words:
            return []
        match = " OR ".join('"'+word+'"' for word in words)
        return self.s.all("SELECT m.* FROM memory_fts f JOIN memories m ON f.id=m.id WHERE memory_fts MATCH ? AND m.status='confirmed' ORDER BY rank LIMIT 20",(match,))

    def cleanup_trash(self):
        for file in self.s.all("SELECT id,path,deleted_at FROM files WHERE status='trashed'"):
            if time.time()-__import__('datetime').datetime.fromisoformat(file["deleted_at"].replace("Z","+00:00")).timestamp() > 7*86400:
                Path(file["path"]).unlink(missing_ok=True)
                with self.s.tx() as db:
                    db.execute("UPDATE files SET status='purged' WHERE id=? AND status='trashed'",(file["id"],))

    def capacity(self):
        if shutil.disk_usage(self.s.config.files_dir).free < 256*1024*1024:
            raise BotError("storage_pressure","磁盘可用空间不足，已限制新增文件；仍可下载、查询和取消",507)
