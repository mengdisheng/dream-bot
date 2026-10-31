import io
from pathlib import Path

import pytest
from starlette.datastructures import UploadFile

from personal_bot.resources import Resources
from personal_bot.tools import ToolRunner


@pytest.mark.asyncio
async def test_parser_paging_cache_and_delete_cannot_reinject_old_body(service,monkeypatch):
    from personal_bot import sandbox
    resources=Resources(service)
    uploaded=await resources.upload(UploadFile(file=io.BytesIO(b'input'),filename='source.txt'))
    receipt=service.accept({"client_request_id":"file-task","mode":"new","text":"read file","file_ids":[uploaded["id"]]})
    task=service.claim("runtime-test")
    count=0
    async def execute(**kwargs):
        nonlocal count
        count+=1
        assert kwargs["input_files"][0]["id"]==uploaded["id"]
        return {"status":"succeeded","text":"0123456789abcdef","documents":[{"truncated":True}],"parser_truncated":True,"artifacts":[]}
    monkeypatch.setattr(sandbox,"execute",execute)
    runner=ToolRunner(service)
    envelope={"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test","revision":1,"model_call_id":"m1","name":"read_file"}
    first=await runner.invoke({**envelope,"call_id":"p1","arguments":{"file_id":uploaded["id"],"limit":5}})
    second=await runner.invoke({**envelope,"call_id":"p2","arguments":{"file_id":uploaded["id"],"offset":5,"limit":5}})
    assert first["result"]["text"]=="01234" and first["result"]["next_offset"]==5
    assert second["result"]["text"]=="56789" and second["result"]["extraction_incomplete"]
    assert count==1
    service.command(task["id"],{"command_id":"pause-input-task","expected_revision":1,"action":"pause"})
    resources.delete_file(uploaded["id"])
    assert service.s.one("SELECT COUNT(*) AS n FROM parse_cache")["n"]==0
    assert service.s.one("SELECT COUNT(*) AS n FROM task_file_grants")["n"]==0
    assert all(tool["result"].get("redacted") for tool in service.list_tools(task["id"]))
    resources.restore_file(uploaded["id"])
    assert service.task(task["id"])["status"]=="paused"
    assert service.s.one("SELECT COUNT(*) AS n FROM task_file_grants")["n"]==0


def test_artifact_publication_removes_only_scratch_source(service,tmp_path,monkeypatch):
    root=tmp_path/"outputs"
    source=root/"run-1"/"result"
    source.parent.mkdir(parents=True)
    source.write_text("complete artifact")
    monkeypatch.setenv("PERSONAL_BOT_OUTPUT_ROOT",str(root))
    receipt=service.accept({"client_request_id":"artifact","text":"write","mode":"new"})
    resources=Resources(service)
    artifact=resources.publish(receipt["task_id"],source,"safe.html","text/html")
    assert not source.exists()
    assert root.exists()
    formal=resources.file(artifact["id"],include_path=True)
    assert Path(formal["path"]).read_text()=="complete artifact"
