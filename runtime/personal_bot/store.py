from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .config import Config
from .errors import BotError


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def uid() -> str:
    return str(uuid.uuid4())


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,prompt TEXT NOT NULL,status TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 connection_id TEXT,connection_revision INTEGER,model_id TEXT,attempt_id TEXT,runtime_id TEXT,
 lease_until REAL,checkpoint_id TEXT,wait_generation INTEGER NOT NULL DEFAULT 0,
 wait_reason TEXT,result TEXT,summary TEXT,parent_task_id TEXT,schedule_id TEXT,
 tool_count INTEGER NOT NULL DEFAULT 0,token_count INTEGER NOT NULL DEFAULT 0,
 active_seconds REAL NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0,
 pause_requested INTEGER NOT NULL DEFAULT 0,context_generation INTEGER NOT NULL DEFAULT 1,
 max_tools INTEGER NOT NULL DEFAULT 30,max_tokens INTEGER NOT NULL DEFAULT 100000,
 max_active_seconds REAL NOT NULL DEFAULT 900,
 conversation_id TEXT REFERENCES conversations(id)
);
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
 task_revision INTEGER NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,response TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
 role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL REFERENCES tasks(id),
 kind TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_task ON task_events(task_id,seq);
CREATE TABLE IF NOT EXISTS checkpoints(task_id TEXT NOT NULL,namespace TEXT NOT NULL,checkpoint_id TEXT NOT NULL,
 parent_id TEXT,checkpoint TEXT NOT NULL,metadata TEXT NOT NULL,created_at TEXT NOT NULL,
 context_generation INTEGER NOT NULL,PRIMARY KEY(task_id,namespace,checkpoint_id));
CREATE TABLE IF NOT EXISTS checkpoint_writes(task_id TEXT NOT NULL,namespace TEXT NOT NULL,checkpoint_id TEXT NOT NULL,
 writer_id TEXT NOT NULL,idx INTEGER NOT NULL,channel TEXT NOT NULL,value TEXT NOT NULL,
 PRIMARY KEY(task_id,namespace,checkpoint_id,writer_id,idx));
CREATE TABLE IF NOT EXISTS tool_calls(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
 attempt_id TEXT NOT NULL,task_revision INTEGER NOT NULL,name TEXT NOT NULL,arguments TEXT NOT NULL,
 binding_hash TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error TEXT,approval_id TEXT,
 created_at TEXT NOT NULL,dispatched_at TEXT,finished_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,task_revision INTEGER NOT NULL,
 tool_call_id TEXT NOT NULL,binding_hash TEXT NOT NULL,action TEXT NOT NULL,status TEXT NOT NULL,
 created_at TEXT NOT NULL,decided_at TEXT);
CREATE TABLE IF NOT EXISTS model_connections(id TEXT PRIMARY KEY,display_name TEXT NOT NULL,provider TEXT NOT NULL,
 region TEXT NOT NULL,billing_mode TEXT NOT NULL,protocol TEXT NOT NULL,base_url TEXT NOT NULL,
 model_id TEXT NOT NULL,secret TEXT NOT NULL,capabilities TEXT NOT NULL,options TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,enabled INTEGER NOT NULL DEFAULT 1,validated_at TEXT,
 quota_group TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY,task_id TEXT,connection_id TEXT NOT NULL,
 connection_revision INTEGER NOT NULL,status TEXT NOT NULL,usage TEXT,started_at TEXT NOT NULL,
 finished_at TEXT,error_code TEXT);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,name TEXT NOT NULL,media_type TEXT NOT NULL,size INTEGER NOT NULL,
 status TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,task_id TEXT,path TEXT NOT NULL,
 sha256 TEXT NOT NULL,created_at TEXT NOT NULL,deleted_at TEXT);
CREATE TABLE IF NOT EXISTS task_file_grants(task_id TEXT NOT NULL REFERENCES tasks(id),file_id TEXT NOT NULL REFERENCES files(id),
 PRIMARY KEY(task_id,file_id));
CREATE TABLE IF NOT EXISTS parse_cache(file_id TEXT PRIMARY KEY,content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,content TEXT NOT NULL,status TEXT NOT NULL,
 source TEXT NOT NULL,item_key TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,
 UNIQUE(source,item_key));
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED,content);
CREATE TABLE IF NOT EXISTS memory_suppression(source TEXT NOT NULL,item_key TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(source,item_key));
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,title TEXT NOT NULL,prompt TEXT NOT NULL,
 cron TEXT NOT NULL,timezone TEXT NOT NULL,enabled INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 registration_status TEXT NOT NULL,next_fire_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
 created_at TEXT NOT NULL,acked_at TEXT,attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dispatches(event_id TEXT PRIMARY KEY,response TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_accounts(id TEXT PRIMARY KEY,provider TEXT NOT NULL,label TEXT NOT NULL,region TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,key_type TEXT NOT NULL,secret_revision INTEGER NOT NULL,secret TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS billing_entries(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,product TEXT NOT NULL,billing_mode TEXT NOT NULL,entitlements TEXT NOT NULL,overage_state TEXT NOT NULL,overage_kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS endpoints(id TEXT PRIMARY KEY,credential_id TEXT NOT NULL,billing_entry_id TEXT NOT NULL,protocol TEXT NOT NULL,base_url TEXT NOT NULL,region TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS model_profiles(id TEXT PRIMARY KEY,endpoint_id TEXT NOT NULL,model_id TEXT NOT NULL,parameter_policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quota_groups(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,subscription_ref TEXT NOT NULL,unit TEXT NOT NULL,limit_source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quota_bindings(connection_id TEXT NOT NULL,quota_group_id TEXT NOT NULL,model_ids TEXT NOT NULL,PRIMARY KEY(connection_id,quota_group_id));
CREATE TABLE IF NOT EXISTS quota_windows(quota_group_id TEXT NOT NULL,name TEXT NOT NULL,remaining REAL,reset_at TEXT,source TEXT NOT NULL,observed_at TEXT NOT NULL,trusted INTEGER NOT NULL,PRIMARY KEY(quota_group_id,name));
CREATE TABLE IF NOT EXISTS connection_details(connection_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,credential_id TEXT NOT NULL,billing_entry_id TEXT NOT NULL,endpoint_id TEXT NOT NULL,model_profile_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS connection_versions(connection_id TEXT NOT NULL,revision INTEGER NOT NULL,config TEXT NOT NULL,PRIMARY KEY(connection_id,revision));
CREATE TABLE IF NOT EXISTS model_steps(task_id TEXT NOT NULL,step TEXT NOT NULL,context_generation INTEGER NOT NULL,response TEXT NOT NULL,PRIMARY KEY(task_id,step,context_generation));
CREATE TABLE IF NOT EXISTS tool_revisions(id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 1,reconciliation TEXT);
CREATE TABLE IF NOT EXISTS request_digests(id TEXT PRIMARY KEY,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_digests(id TEXT PRIMARY KEY,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_digests(id TEXT PRIMARY KEY,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS restore_audit(id TEXT PRIMARY KEY,deployment_epoch TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS restored_outbox(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,archived_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tasks_status_created ON tasks(status,created_at);
CREATE INDEX IF NOT EXISTS tasks_created_id ON tasks(created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS messages_task_created_id ON messages(task_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS tools_task_status ON tool_calls(task_id,status);
CREATE INDEX IF NOT EXISTS models_task_unknown_usage ON model_calls(task_id) WHERE usage='null';
"""


class Store:
    def __init__(self, config: Config):
        self.config = config
        if config.production and sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite >= 3.51.3 required")
        if config.production and (not config.runtime_id or not config.deployment_epoch):
            raise RuntimeError("Runtime and deployment identity required")
        config.app_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.files_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(config.app_dir, 0o700)
        self.path = config.app_dir / "app.sqlite"
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        if "execution_mode" not in {row[1] for row in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'agent'")
        # Additive migration: preserve every old task and its audit/checkpoints.
        # Historical parent links are not reliable conversation membership.
        with self.tx() as db:
            if "conversation_id" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}:
                db.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT REFERENCES conversations(id)")
            db.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at) "
                       "SELECT id,substr(title,1,64),created_at,updated_at FROM tasks WHERE conversation_id IS NULL")
            db.execute("UPDATE tasks SET conversation_id=id WHERE conversation_id IS NULL")
            db.execute("CREATE INDEX IF NOT EXISTS conversations_created_id ON conversations(created_at DESC,id DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_conversation_created_id ON tasks(conversation_id,created_at DESC,id DESC)")
        keypath = config.app_dir / "master.key"
        if not keypath.exists():
            if self.db.execute("SELECT 1 FROM model_connections WHERE secret<>'' LIMIT 1").fetchone():
                raise RuntimeError("Existing credentials require the original master key")
            fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
        self.cipher = Fernet(keypath.read_bytes())
        marker_path = config.app_dir / ".restore-required.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
        if marker and marker.get("deployment_epoch") != config.deployment_epoch:
            raise RuntimeError("Restore marker does not match current deployment identity")
        with self.tx() as db:
            previous = self.meta("deployment_epoch", db=db)
            if marker or (previous and previous != config.deployment_epoch):
                db.execute("UPDATE tasks SET status='needs_review',attempt_id=NULL,lease_until=NULL WHERE status NOT IN ('succeeded','failed','cancelled')")
                db.execute("UPDATE tasks SET checkpoint_id=NULL,context_generation=context_generation+1")
                db.execute("UPDATE schedules SET enabled=0,revision=revision+1,registration_status='pending'")
                db.execute("UPDATE approvals SET status='invalidated' WHERE status IN ('pending','approved')")
                db.execute("UPDATE tool_calls SET status='unknown' WHERE status IN ('dispatched','prepared')")
                db.execute("DELETE FROM checkpoints")
                db.execute("DELETE FROM checkpoint_writes")
                db.execute("DELETE FROM model_steps")
                db.execute("INSERT OR REPLACE INTO restored_outbox SELECT id,kind,payload,created_at,? FROM outbox WHERE acked_at IS NULL",(now(),))
                db.execute("DELETE FROM outbox WHERE acked_at IS NULL")
                for schedule in db.execute("SELECT * FROM schedules").fetchall():
                    self.outbox(db,"schedule.disable",{"schedule_id":schedule["id"],"revision":schedule["revision"],"enabled":False,"cron":schedule["cron"],"timezone":schedule["timezone"],"next_fire_at":None})
                for task in db.execute("SELECT * FROM tasks WHERE status='needs_review'").fetchall():
                    self.invalidate_wait(db,dict(task))
                    self.event(db,task["id"],"restore.needs_review",{"deployment_epoch":config.deployment_epoch})
                db.execute("INSERT OR IGNORE INTO restore_audit VALUES(?,?,?,?)",((marker or {}).get("maintenance_id") or uid(),config.deployment_epoch,dumps(marker or {"previous_epoch":previous}),now()))
            self.setmeta("deployment_epoch", config.deployment_epoch, db)
            # The host confirms the old container ended before creating this instance.
            stale = db.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
            for row in stale:
                uncertain = db.execute("SELECT 1 FROM tool_calls WHERE task_id=? AND status IN ('dispatched','unknown')", (row["id"],)).fetchone()
                status = "needs_review" if uncertain else "queued"
                db.execute("UPDATE tasks SET status=?,attempt_id=NULL,lease_until=NULL WHERE id=?", (status, row["id"]))
                self.event(db, row["id"], "task.recovered", {"status": status})
            db.execute("UPDATE tool_calls SET status='unknown' WHERE status='dispatched'")
            db.execute("UPDATE model_calls SET status='unknown',usage='null' WHERE status='dispatched'")
            # A drain belongs to one physical runtime generation. Reopening
            # this same generation must never reopen its committed gate; a
            # new container, after host reconciliation, gets a fresh gate.
            same_intake_runtime = (not marker and previous == config.deployment_epoch
                                   and self.meta("intake_runtime_id", db=db) == config.runtime_id)
            if not same_intake_runtime:
                self.setmeta("intake", "open", db)
                self.setmeta("intake_revision", "1", db)
                self.setmeta("drain_id", "", db)
            self.setmeta("intake_runtime_id", config.runtime_id, db)
            self.setmeta("maintenance", "", db)
            self.setmeta("last_activity", now(), db)
        self.reconciled = True
        if marker:
            marker_path.unlink()

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def one(self, query: str, args=()) -> dict | None:
        with self.lock:
            row = self.db.execute(query, args).fetchone()
            return dict(row) if row else None

    def all(self, query: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(query, args).fetchall()]

    def meta(self, key: str, default: str = "", *, db=None) -> str:
        row = (db or self.db).execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def setmeta(key: str, value: str, db) -> None:
        db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def writable(self, db, *, intake=False) -> None:
        if self.meta("maintenance", db=db):
            raise BotError("maintenance", "系统正在维护，请稍后重试", 503, True)
        if intake and self.meta("intake", db=db) != "open":
            raise BotError("runtime_draining", "运行环境正在回收，请沿原请求编号重试", 503, True)

    def activity(self, db) -> None:
        self.setmeta("last_activity", now(), db)

    @staticmethod
    def event(db, task_id: str, kind: str, data: dict) -> None:
        db.execute("INSERT INTO task_events(task_id,kind,data,created_at) VALUES(?,?,?,?)", (task_id, kind, dumps(data), now()))

    @staticmethod
    def outbox(db, kind: str, payload: dict) -> str:
        identity = uid()
        db.execute("INSERT INTO outbox(id,kind,payload,created_at) VALUES(?,?,?,?)", (identity, kind, dumps(payload), now()))
        return identity

    def task(self, task_id: str, db=None) -> dict:
        row = (db or self.db).execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise BotError("not_found", "任务不存在", 404)
        return dict(row)

    def guard_attempt(self, db, task_id: str, attempt_id: str, runtime_id: str, revision=None) -> dict:
        row = self.task(task_id, db)
        if (runtime_id != self.config.runtime_id or row["runtime_id"] != runtime_id
                or row["attempt_id"] != attempt_id or row["status"] != "running"
                or row["cancel_requested"] or row["pause_requested"]
                or row["lease_until"] is None or row["lease_until"] < time.time()):
            raise BotError("stale_attempt", "执行授权已失效", 409)
        if revision is not None and row["revision"] != revision:
            raise BotError("revision_conflict", "任务指令已更新，请重新读取上下文", 409)
        return row

    def notify(self, db, task_id: str, category: str) -> None:
        self.outbox(db, "notification", {"notification_id": uid(), "task_id": task_id, "category": category})

    def invalidate_wait(self, db, task: dict) -> None:
        generation = task["wait_generation"] + 1
        db.execute("UPDATE tasks SET wait_generation=? WHERE id=?", (generation, task["id"]))
        self.outbox(db, "model.wait.upsert", {"task_id": task["id"], "wait_generation": generation,
                    "connection_revision": task["connection_revision"] or 0, "enabled": False, "resume_at": None})

    def close(self):
        self.db.close()
