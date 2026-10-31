"""Opt-in native performance fixtures, with no provider calls or owner data.

Run the core fixture as API UID and browser fixture as browser UID inside the
normal runtime. Browser fixture requires an unused display and uses a fresh
temporary profile; it never changes the owner profile or exposes script tools.
"""
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


def measured(operation, count):
    values = []
    for _ in range(count):
        start = time.perf_counter()
        operation()
        values.append((time.perf_counter() - start) * 1000)
    return {"samples": count, "median_ms": statistics.median(values), "max_ms": max(values)}


def core():
    from personal_bot.config import Config
    from personal_bot.service import Service
    from personal_bot.store import Store, dumps
    from personal_bot import tools
    with tempfile.TemporaryDirectory(prefix="personal-bot-perf-") as folder:
        root = Path(folder)
        config = Config(root / "app", root / "files", "perf", "perf", root / "public.pem",
                        str(root / "worker.sock"), str(root / "bridge.sock"), 31002, 31000, False)
        store = Store(config)
        service = Service(store)
        statements = []
        store.db.set_trace_callback(statements.append)
        empty = measured(lambda: service.claim("perf"), 100)
        empty["sql_per_poll"] = len(statements) / 100
        empty["write_transactions"] = sum(q.startswith("BEGIN IMMEDIATE") for q in statements)
        store.db.set_trace_callback(None)
        with store.tx() as db:
            for i in range(500):
                identity = f"fixture-{i:04d}"
                db.execute("INSERT INTO tasks(id,title,prompt,status,created_at,updated_at,result) VALUES(?,?,?,?,?,?,?)",
                           (identity, identity, "fixture " * 1000, "succeeded", identity, identity, "result"))
                for j in range(5):
                    db.execute("INSERT INTO tool_calls(id,task_id,attempt_id,task_revision,name,arguments,binding_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (identity + str(j), identity, "fixture", 1, "memory_search", "{}", "fixture", "unknown" if j == 0 else "succeeded", identity))
                    db.execute("INSERT INTO model_calls(id,task_id,connection_id,connection_revision,status,usage,started_at) VALUES(?,?,?,?,?,?,?)",
                               (identity + str(j), identity, "fixture", 1, "succeeded", "null" if j == 0 else "{}", identity))
        def listing():
            if hasattr(service, "list_tasks"):
                return service.list_tasks()
            return [service.task(row["id"]) for row in store.all("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 500")]
        statements.clear()
        store.db.set_trace_callback(statements.append)
        result = listing()
        query_count = len(statements)
        store.db.set_trace_callback(None)
        assert len(result) == 500 and all(t["unresolved_effect_count"] == 1 and t["token_usage_estimated"] for t in result)
        listings = measured(listing, 8)
        listings["sql_per_listing"] = query_count
        listings["json_bytes"] = len(dumps(result).encode())
        def validate():
            if hasattr(tools, "validate_arguments"):
                tools.validate_arguments("read_file", {"file_id": "fixture", "offset": 0, "limit": 20000})
            else:
                tools.validate({"file_id": "fixture", "offset": 0, "limit": 20000}, tools.SCHEMAS["read_file"])
        validate()
        schemas = measured(validate, 500)
        summary = None
        if hasattr(service, "task_summaries"):
            summary_page = service.task_summaries()
            assert len(summary_page["items"]) == 50 and summary_page["has_more"]
            assert all("prompt" not in item and "result" not in item for item in summary_page["items"])
            summary = measured(service.task_summaries, 8)
            summary["json_bytes"] = len(dumps(summary_page).encode("utf-8"))
            summary["items"] = len(summary_page["items"])
            assert summary["json_bytes"] <= 64*1024
        store.close()
    return {"idle_claim": empty, "task_list_500": listings, "task_summary_50": summary, "tool_schema": schemas}


async def browser():
    from personal_bot.browser import Browser
    assert os.getuid() == 31003
    assert not Path("/tmp/.X11-unix/X99").exists(), "Browser benchmark requires an unused display"
    with tempfile.TemporaryDirectory(prefix="personal-bot-perf-profile-") as folder:
        os.environ["PERSONAL_BOT_BROWSER_PROFILE"] = folder
        instance = Browser()
        try:
            start = time.perf_counter()
            await instance.ensure()
            cold = time.perf_counter() - start
            # Fixed trusted test fixture only; no script supplied by any model.
            await instance.page.set_content("<html><head><title>Performance fixture</title></head><body>" +
                                            "".join(f'<button aria-label="Item {i}" name="item-{i}">Item {i}</button>' for i in range(100)) +
                                            "</body></html>")
            values = []
            for _ in range(6):
                start = time.perf_counter()
                result = await instance.observe()
                values.append((time.perf_counter() - start) * 1000)
                assert len(result["elements"]) == 100 and len(instance.refs) == 100
            return {"browser_start_seconds": cold, "observe_100": {"samples": len(values),
                    "median_ms": statistics.median(values), "max_ms": max(values)}, "real_chromium": True}
        finally:
            await instance.close()


if __name__ == "__main__":
    assert os.environ.get("PERSONAL_BOT_ACCEPTANCE_PROBE") == "1"
    mode = sys.argv[1]
    value = core() if mode == "core" else asyncio.run(browser())
    print(json.dumps({"mode": mode, "metrics": value, "model_calls": 0}))
