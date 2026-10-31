"""Opt-in Linux acceptance probe; only the model HTTP response is simulated.

Run as root INSIDE the normal runtime, with its real executor/browser brokers:
    PYTHONPATH=/opt/personal-bot/runtime python /path/to/native_flow_probe.py

This is not evidence of provider connectivity or model quality. It uses a private
API database, real API/Worker UIDs, actual SO_PEERCRED, the actual LangGraph, and
the actual isolation broker. No production task or connection is created.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

API_UID, WORKER_UID, IPC_GID = 31001, 31002, 31000
CSV = b"item,amount\nalpha,3\nbeta,7\n"
TXT = b"Only the granted CSV rows belong in this report.\n"
SUMMARY = "Rows: 2; total: 10. Generated from the two granted fixtures.\n"
FINAL = "Native acceptance report completed."


class ProbeFailure(Exception):
    """Its code is a static test category; never include runtime data."""


def require(condition: bool, category: str) -> None:
    if not condition:
        raise ProbeFailure(category)


def failure(exc: BaseException) -> dict:
    return {"status": "FAIL", "error_type": str(exc) if isinstance(exc, ProbeFailure) else type(exc).__name__}


def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def assertion(private, epoch: str, method: str, path: str) -> str:
    claims = {"sub": "native-probe", "role": "owner", "method": method,
              "path": path, "exp": time.time() + 30,
              "request_id": uuid.uuid4().hex, "deployment_epoch": epoch}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    signed = base64.urlsafe_b64encode(private.sign(encoded.encode())).decode().rstrip("=")
    return encoded + "." + signed


async def api_mode(folder: Path) -> None:
    import httpx
    import uvicorn
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from starlette.datastructures import UploadFile, Headers
    from personal_bot.api import create_app
    from personal_bot.auth import PeerCredentialProtocol
    from personal_bot.config import Config
    from personal_bot.net import PublicHttp
    from personal_bot.store import dumps
    from personal_bot import browser

    require(os.getuid() == API_UID, "ApiIdentityError")
    private = Ed25519PrivateKey.generate()
    public = folder / "gateway.pem"
    public.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    socket_path = str(folder / "worker.sock")
    identity = "native-probe-" + folder.name.rsplit("-", 1)[-1]
    config = Config(folder / "app", Path("/data/files"), identity, identity,
                    public, socket_path, str(folder / "absent-host.sock"),
                    WORKER_UID, IPC_GID, True)
    app = create_app(config)
    server = None
    running = None
    async with app.router.lifespan_context(app):
        try:
            uploaded = []
            for name, content, media in (("native-fixture.csv", CSV, "text/csv"),
                                         ("native-fixture.txt", TXT, "text/plain")):
                upload = UploadFile(io.BytesIO(content), filename=name,
                                    headers=Headers({"content-type": media}))
                uploaded.append(await app.state.resources.upload(upload))
            connection = app.state.service.connections.save({
                "provider": "custom", "protocol": "chat_completions",
                "billing_mode": "api", "model_id": "deterministic-native-probe",
                "base_url": "https://model.example/v1", "api_key": "test-only-not-a-real-key"})
            with app.state.store.tx() as db:
                db.execute("UPDATE model_connections SET capabilities=? WHERE id=?",
                           (dumps({k: "passed" for k in ("auth", "text", "stream", "tools")}), connection["id"]))
                app.state.store.setmeta("default_agent_connection", connection["id"], db)

            model_calls = 0
            verified_tool_outputs = 0
            sent_input_counts = []

            def utf8_size(value):
                return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))

            def model_response(request):
                nonlocal model_calls, verified_tool_outputs
                body = json.loads(request.content)
                model_calls += 1
                # Count the actual mock HTTP payload independently of the API
                # metric helper; do not keep a second prompt/content copy.
                sent_input_counts.append({
                    "input_utf8_bytes": utf8_size(body["messages"]),
                    "tool_schema_utf8_bytes": utf8_size(body["tools"]) if "tools" in body else 0,
                    "request_json_utf8_bytes": utf8_size(body),
                    "message_count": len(body["messages"]) - 1,
                })
                outputs = [json.loads(m["content"]) for m in body["messages"] if m.get("role") == "tool"]
                index = len(outputs)
                require(model_calls == index + 1, "ModelSequenceError")
                if index >= 1:
                    require(outputs[0].get("source") == "sandbox_parser" and outputs[0].get("text") == CSV.decode(), "CsvParseEvidenceError")
                if index >= 2:
                    require(outputs[1].get("source") == "sandbox_parser" and outputs[1].get("text") == TXT.decode(), "TextParseEvidenceError")
                if index >= 3:
                    result = outputs[2]
                    security = result.get("security", {})
                    require(result.get("status") == "succeeded" and security.get("seccomp")
                            and security.get("landlock_abi", 0) >= 3 and security.get("uid") == 31004
                            and len(result.get("artifacts", [])) == 1,
                            "PythonIsolationEvidenceError")
                    artifact = app.state.resources.file(result["artifacts"][0]["id"], include_path=True)
                    require(artifact["name"] == "analysis.json" and json.loads(Path(artifact["path"]).read_bytes()) == {"rows": 2, "total": 10}, "PythonArtifactEvidenceError")
                if index >= 4:
                    artifact = app.state.resources.file(outputs[3]["id"], include_path=True)
                    require(artifact["name"] == "summary.txt" and Path(artifact["path"]).read_text() == SUMMARY, "SummaryArtifactEvidenceError")
                verified_tool_outputs = max(verified_tool_outputs, index)
                operations = [
                    ("read_file", {"file_id": uploaded[0]["id"]}),
                    ("read_file", {"file_id": uploaded[1]["id"]}),
                    ("python", {"file_ids": [x["id"] for x in uploaded], "code":
                        "import csv, json\nfrom pathlib import Path\n"
                        "p = next(f['path'] for f in input_files if f['name'].endswith('.csv'))\n"
                        "with open(p, newline='') as source:\n    rows = list(csv.DictReader(source))\n"
                        "Path('analysis.json').write_text(json.dumps({'rows':len(rows),'total':sum(int(r['amount']) for r in rows)}))\n"}),
                    ("write_file", {"name": "summary.txt", "content": SUMMARY, "media_type": "text/plain"}),
                ]
                if index < len(operations):
                    name, args = operations[index]
                    delta = {"role": "assistant", "content": "", "tool_calls": [{
                        "index": 0, "id": "native-tool-" + str(index), "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)}}]}
                    finish = "tool_calls"
                else:
                    require(index == len(operations), "UnexpectedToolOutputError")
                    delta, finish = {"role": "assistant", "content": FINAL}, "stop"
                payload = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
                return httpx.Response(200, content="data: " + json.dumps(payload) + "\n\ndata: [DONE]\n\n")

            # The one deliberate simulation: provider HTTP replies only.
            app.state.models.http = PublicHttp(httpx.MockTransport(model_response))
            receipt = app.state.service.accept({"client_request_id": identity, "mode": "new",
                "text": "Read both granted fixtures, compute the CSV total in isolated Python, and write a summary.",
                "file_ids": [x["id"] for x in uploaded]})
            browser_state = await browser.status()
            require(isinstance(browser_state, dict) and bool(browser_state), "BrowserBrokerStatusError")
            server = uvicorn.Server(uvicorn.Config(app, uds=socket_path, http=PeerCredentialProtocol,
                lifespan="off", log_level="critical", access_log=False))
            running = asyncio.create_task(server.serve())
            for _ in range(200):
                if server.started:
                    break
                require(not running.done(), "ApiListenerStartupError")
                await asyncio.sleep(0.025)
            require(server.started, "ApiListenerTimeoutError")
            os.chmod(socket_path, 0o660)
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://api") as client:
                denied = await client.post("/worker/claim", json={"runtime_id": identity}, headers={"X-Worker-Uid": str(WORKER_UID)})
                require(denied.status_code == 403, "ForgedPeerHeaderAcceptedError")
            emit({"status": "READY"})
            command = await asyncio.to_thread(sys.stdin.readline)
            require(command.strip() == "verify", "ControllerAbortedError")
            task = app.state.service.task(receipt["task_id"])
            require(task["status"] == "succeeded" and task["result"] == FINAL, "TaskCompletionError")
            require(task["token_count"] == 100 and model_calls == 5, "CumulativeTokenAccountingError")
            measured = app.state.store.all("SELECT data FROM task_events WHERE task_id=? AND kind='model.input_measured' ORDER BY seq", (task["id"],))
            require(len(measured) == len(sent_input_counts) == model_calls, "ModelInputMeasurementCountError")
            allowed_metrics = {"model_call_id", "attempt", "measurement", "raw_messages_utf8_bytes",
                               "input_utf8_bytes", "tool_schema_utf8_bytes", "request_json_utf8_bytes", "message_count"}
            metric_totals = {key: 0 for key in ("input_utf8_bytes", "tool_schema_utf8_bytes", "request_json_utf8_bytes")}
            for row, actual in zip(measured, sent_input_counts, strict=True):
                metric = json.loads(row["data"])
                require(set(metric) == allowed_metrics and metric["measurement"] == "compact_json_utf8"
                        and metric["attempt"] == 0 and metric["raw_messages_utf8_bytes"] > 0,
                        "ModelInputMeasurementShapeError")
                require(all(metric[key] == value for key, value in actual.items()), "ModelInputPayloadByteMismatchError")
                for key in metric_totals:
                    metric_totals[key] += metric[key]
            recorded_calls = app.state.store.all("SELECT id,status,usage FROM model_calls WHERE task_id=? ORDER BY started_at,rowid", (task["id"],))
            require([json.loads(row["data"])["model_call_id"] for row in measured] == [row["id"] for row in recorded_calls],
                    "ModelInputCallCorrelationError")
            expected_usage = {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            require(all(row["status"] == "succeeded" and json.loads(row["usage"]) == expected_usage for row in recorded_calls),
                    "SimulatedUsagePersistenceError")
            tools = app.state.store.all("SELECT name,status FROM tool_calls WHERE task_id=? ORDER BY created_at,rowid", (task["id"],))
            require([t["name"] for t in tools] == ["read_file", "read_file", "python", "write_file"]
                    and all(t["status"] == "succeeded" for t in tools) and verified_tool_outputs == 4, "ToolLedgerEvidenceError")
            checkpoints = app.state.store.one("SELECT COUNT(*) AS n FROM checkpoints WHERE task_id=?", (task["id"],))["n"]
            writes = app.state.store.one("SELECT COUNT(*) AS n FROM checkpoint_writes WHERE task_id=?", (task["id"],))["n"]
            require(checkpoints >= 10 and writes > 0 and task["checkpoint_id"], "CheckpointPersistenceError")
            generated = app.state.store.all("SELECT * FROM files WHERE task_id=? AND status='generated'", (task["id"],))
            require(len(generated) == 2, "ArtifactCountError")
            downloaded = 0
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://api") as client:
                for artifact in generated:
                    path = "/api/files/" + artifact["id"] + "/download"
                    response = await client.get(path, headers={"X-Personal-Bot-Assertion": assertion(private, identity, "GET", path)})
                    require(response.status_code == 200 and response.content == Path(artifact["path"]).read_bytes()
                            and response.headers.get("cache-control") == "no-store", "ArtifactDownloadError")
                    downloaded += 1
            emit({"status": "PASS", "model_http_simulated": True, "model_calls": model_calls,
                  "tokens": task["token_count"], "tools": len(tools), "checkpoints": checkpoints,
                  "checkpoint_writes": writes, "downloaded_artifacts": downloaded,
                  "real_browser_status": 1, "forged_peer_denials": 1,
                  "input_measurements": len(measured), "input_measurements_match_payload": True,
                  "input_size_totals": metric_totals,
                  "simulated_usage": {"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100}})
        finally:
            if server is not None:
                server.should_exit = True
            if running is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(running, 10)
            # Only records in this fresh private API database are eligible.
            for record in app.state.store.all("SELECT id,path FROM files"):
                path = Path(record["path"])
                require(path.parent == Path("/data/files") and path.name == str(uuid.UUID(record["id"])), "CleanupScopeError")
                path.unlink(missing_ok=True)


async def worker_mode(folder: Path) -> None:
    import httpx
    from personal_bot.worker import IPC, run_claimed

    require(os.getuid() == WORKER_UID, "WorkerIdentityError")
    denied = 0
    for path in (folder / "app" / "app.sqlite", folder / "app" / "master.key",
                 Path("/data/app/app.sqlite"), Path("/data/app/master.key")):
        try:
            with path.open("rb") as stream:
                stream.read(1)
        except PermissionError:
            denied += 1
        else:
            raise ProbeFailure("WorkerSecretReadableError")
    # Worker cannot bypass API policy by calling the execution broker directly.
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/run/personal-bot/executor.sock")) as client:
        response = await client.post("http://executor/execute", json={"action": "selftest", "input_files": [], "limits": {}})
        require(response.status_code == 403, "WorkerBrokerBypassError")
    socket_path = str(folder / "worker.sock")
    identity = "native-probe-" + folder.name.rsplit("-", 1)[-1]
    ipc = IPC(socket_path=socket_path)
    try:
        task = (await ipc.call("claim", {"runtime_id": identity}))["task"]
        require(task is not None, "WorkerClaimError")
        captured = io.StringIO()
        # Exercise the production Worker lifecycle, including its real Agent,
        # TaskIPC, heartbeat, finish and timing emitter. Only model HTTP replies
        # are simulated by the API process. No task or model body is logged.
        with contextlib.redirect_stderr(captured):
            await run_claimed(ipc, task)
        records = []
        for line in captured.getvalue().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("kind") == "performance":
                records.append(record)
        require(len(records) == 2 and {row.get("stage") for row in records} == {"task_initialized", "first_tool_completed"},
                "WorkerTimingStageError")
        require(all(set(row) == {"kind", "stage", "seconds", "task_id"} and row["task_id"] == task["id"]
                    and isinstance(row["seconds"], (int, float)) and math.isfinite(row["seconds"]) and row["seconds"] >= 0
                    for row in records), "WorkerTimingShapeError")
        timings = {row["stage"]: row["seconds"] for row in records}
        require(timings["first_tool_completed"] >= timings["task_initialized"], "WorkerTimingOrderError")
        emit({"status": "PASS", "worker_private_read_denials": denied, "worker_broker_denials": 1,
              "worker_execution": "run_claimed", "worker_timing_records": len(records), "worker_timings_seconds": timings})
    finally:
        await ipc.client.aclose()


def read_line(process, timeout: float) -> dict:
    require(process.stdout is not None, "ProbePipeError")
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    require(bool(readable), "ProbeProcessTimeoutError")
    line = process.stdout.readline()
    require(bool(line), "ProbeProcessExitedError")
    result = json.loads(line)
    require(isinstance(result, dict), "ProbeResultFormatError")
    return result


def root_mode() -> int:
    require(sys.platform == "linux" and os.geteuid() == 0, "LinuxRootRequiredError")
    require(Path("/run/personal-bot/executor.sock").is_socket()
            and Path("/run/personal-bot/browser.sock").is_socket(), "RealBrokersRequiredError")
    folder = Path(tempfile.mkdtemp(prefix="nf-", dir="/run/personal-bot"))
    folder.chmod(0o750)
    os.chown(folder, API_UID, IPC_GID)
    api = None
    worker = None
    try:
        api = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--api", str(folder)],
            user=API_UID, group=IPC_GID, extra_groups=[], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        ready = read_line(api, 90)
        require(ready.get("status") == "READY", ready.get("error_type", "ApiStartupError"))
        worker = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--worker", str(folder)],
            user=WORKER_UID, group=IPC_GID, extra_groups=[], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        worker_result = read_line(worker, 450)
        worker.wait(timeout=10)
        require(worker_result.get("status") == "PASS" and worker.returncode == 0,
                worker_result.get("error_type", "WorkerProcessError"))
        require(api.stdin is not None, "ProbePipeError")
        api.stdin.write("verify\n")
        api.stdin.flush()
        result = read_line(api, 90)
        api.wait(timeout=20)
        require(result.get("status") == "PASS" and api.returncode == 0,
                result.get("error_type", "ApiVerificationError"))
        result.update({k: v for k, v in worker_result.items() if k != "status"})
        emit(result)
        return 0
    finally:
        if worker is not None and worker.poll() is None:
            worker.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                worker.wait(timeout=10)
            if worker.poll() is None:
                worker.kill()
                worker.wait()
        if api is not None and api.poll() is None:
            if api.stdin is not None:
                with contextlib.suppress(BrokenPipeError):
                    api.stdin.write("abort\n")
                    api.stdin.flush()
            with contextlib.suppress(subprocess.TimeoutExpired):
                api.wait(timeout=20)
            if api.poll() is None:
                api.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    api.wait(timeout=10)
            if api.poll() is None:
                api.kill()
                api.wait()
        # Root-owned temporary test subtree only; never remove data directories.
        require(folder.parent == Path("/run/personal-bot") and folder.name.startswith("nf-"), "CleanupScopeError")
        shutil.rmtree(folder)


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] in ("--api", "--worker"):
            operation = api_mode if sys.argv[1] == "--api" else worker_mode
            asyncio.run(operation(Path(sys.argv[2])))
            return 0
        require(len(sys.argv) == 1, "InvalidProbeArgumentsError")
        return root_mode()
    except BaseException as exc:
        emit(failure(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
