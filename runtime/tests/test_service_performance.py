import contextlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from personal_bot.errors import BotError
from personal_bot.tools import _validator, validate_arguments


def test_idle_poll_does_not_open_write_transaction(service):
    statements = []
    service.s.db.set_trace_callback(statements.append)
    try:
        assert service.claim("runtime-test") is None
    finally:
        service.s.db.set_trace_callback(None)
    assert len(statements) == 1
    assert not any(query.startswith("BEGIN") for query in statements)
    with pytest.raises(BotError) as error:
        service.claim("stale")
    assert error.value.code == "stale_runtime"


def test_claim_rechecks_drain_between_probe_and_transaction(service, monkeypatch):
    service.accept({"client_request_id": "fixture", "text": "fixture"})
    original = service.s.tx

    @contextlib.contextmanager
    def drained():
        with original() as db:
            service.s.setmeta("intake", "draining", db)
        with original() as db:
            yield db

    monkeypatch.setattr(service.s, "tx", drained)
    assert service.claim("runtime-test") is None
    assert service.s.one("SELECT status FROM tasks")["status"] == "queued"


def test_concurrent_claim_has_one_owner(service):
    service.accept({"client_request_id": "fixture", "text": "fixture"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.claim("runtime-test"), range(2)))
    assert sum(task is not None for task in results) == 1
    assert len(service.s.all("SELECT * FROM task_events WHERE kind='task.running'")) == 1


def test_list_is_equivalent_and_uses_one_query(service):
    for i in range(3):
        service.accept({"client_request_id": str(i), "text": "fixture " + str(i)})
    tasks = service.s.all("SELECT id FROM tasks ORDER BY created_at DESC")
    with service.s.tx() as db:
        db.execute("INSERT INTO model_calls(id,task_id,connection_id,connection_revision,status,usage,started_at) VALUES(?,?,?,?,?,?,?)",
                   ("call", tasks[0]["id"], "fixture", 1, "unknown", "null", "fixture"))
        db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (tasks[1]["id"],))
        for index, status in enumerate(("dispatched", "unknown", "succeeded")):
            db.execute("INSERT INTO tool_calls(id,task_id,attempt_id,task_revision,name,arguments,binding_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       ("tool" + str(index), tasks[0]["id"], "fixture", 1, "memory_search", "{}", "fixture", status, "fixture"))
    expected = [service.task(task["id"]) for task in tasks]
    statements = []
    service.s.db.set_trace_callback(statements.append)
    try:
        assert service.list_tasks() == expected
    finally:
        service.s.db.set_trace_callback(None)
    assert len(statements) == 1
    assert expected[0]["token_usage_estimated"] is True
    assert expected[0]["unresolved_effect_count"] == 2
    assert expected[1]["unresolved_effect_count"] == 0
    assert expected[1]["cancel_requested"] is True
    with pytest.raises(BotError) as error:
        service.task("missing")
    assert error.value.status == 404


def test_summary_queries_use_task_indexes(service):
    checks = [
        ("SELECT 1 FROM tasks WHERE status='queued' LIMIT 1", "tasks_status_created"),
        ("SELECT COUNT(*) FROM tool_calls WHERE task_id='fixture' AND status IN ('dispatched','unknown')", "tools_task_status"),
        ("SELECT 1 FROM model_calls WHERE task_id='fixture' AND usage='null'", "models_task_unknown_usage"),
    ]
    for query, index in checks:
        plan = service.s.db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        assert any(index in row[3] for row in plan)


@pytest.mark.parametrize("arguments", [
    {"file_id": "fixture", "offset": True},
    {"file_id": "fixture", "offset": -1},
    {"file_id": "fixture", "limit": 50001},
    {"file_id": "fixture", "unexpected": "field"},
    {"offset": 0},
    {"file_id": 7},
])
def test_cached_schema_keeps_argument_rejections(arguments):
    with pytest.raises(BotError) as error:
        validate_arguments("read_file", arguments)
    assert error.value.code == "invalid_tool_arguments"


def test_fixed_validator_is_reused_and_nested_types_checked():
    validate_arguments("read_file", {"file_id": "fixture", "offset": 0, "limit": 20000})
    assert _validator("read_file") is _validator("read_file")
    with pytest.raises(BotError):
        validate_arguments("python", {"code": "pass", "file_ids": [42]})
