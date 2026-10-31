import json

import httpx
import pytest

from personal_bot.errors import BotError
from personal_bot.models import ModelError, Models, classify, parse_stream, responses_result
from personal_bot.net import PublicHttp, normalize_base, safe_url


@pytest.mark.asyncio
async def test_chat_stream_assembles_tool_arguments_only_after_finish():
    chunks = [
      {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-a","function":{"name":"echo","arguments":"{\"value\":"}}]}}]},
      {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"x\"}"}}]},"finish_reason":"tool_calls"}]},
      {"choices":[],"usage":{"total_tokens":11}},
    ]
    response = httpx.Response(200,content="".join("data: "+json.dumps(x)+"\n\n" for x in chunks)+"data: [DONE]\n\n")
    result = await parse_stream(response,"chat_completions")
    assert result["tool_calls"] == [{"id":"call-a","name":"echo","arguments":{"value":"x"}}]
    assert result["usage"]["total_tokens"] == 11
    partial = httpx.Response(200,content="data: "+json.dumps(chunks[0])+"\n\n")
    with pytest.raises(ModelError,match="完整结束"):
        await parse_stream(partial,"chat_completions")


def test_responses_keeps_opaque_items_and_call_identity():
    opaque = {"type":"reasoning","id":"rs1","encrypted_content":"opaque-state"}
    output = responses_result({"status":"completed","output":[opaque,{"type":"function_call","id":"fc1","call_id":"call1","name":"echo","arguments":"{\"x\":1}"}]})
    assert output["raw_output"][0] == opaque
    assert output["tool_calls"][0]["id"] == "call1"


def test_error_classification_does_not_retry_quota_as_rate_limit():
    quota = classify(httpx.Response(429,headers={"retry-after":"60"}),{"error":{"code":"insufficient_quota"}})
    rate = classify(httpx.Response(429,headers={"retry-after":"60"}),{"error":{"code":"rate_limit_error"}})
    assert quota.code == "quota_exhausted" and not quota.retryable and quota.resume_at is None
    assert rate.retryable and rate.resume_at


@pytest.mark.parametrize("code", [None,"","supplier_specific_code"])
def test_balance_type_is_not_hidden_by_missing_or_other_error_code(code):
    error = classify(httpx.Response(429,headers={"retry-after":"60"}),
                     {"error":{"code":code,"type":"insufficient_balance_error"}})
    assert error.code == "quota_exhausted"
    assert error.retryable is False and error.resume_at is None


@pytest.mark.parametrize("kind", ["payment_required","insufficient_balance_error_other",None])
def test_http_402_without_known_balance_identifier_is_not_classified_as_quota(kind):
    error = classify(httpx.Response(402),{"error":{"code":None,"type":kind}})
    assert error.code == "model_request_rejected"
    assert error.retryable is False and error.resume_at is None


@pytest.mark.asyncio
async def test_validation_reports_balance_failure_after_one_request(service):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(402,json={"error":{"code":None,"type":"insufficient_balance_error"}})
    models = Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    result = await models.validate(service.connections.bind()["id"])
    assert len(requests) == 1
    assert result["checks"] == {"auth":"failed","text":"failed","stream":"not_tested","tools":"not_tested","vision":"not_tested"}
    assert len(result["errors"]) == 1 and result["errors"][0]["code"] == "quota_exhausted"


def test_base_preserves_product_paths_and_refuses_protocol_mismatch():
    assert normalize_base("https://a.example/compatible-mode/v1/chat/completions","chat_completions") == "https://a.example/compatible-mode/v1"
    assert normalize_base("https://a.example/api/plan/v3","chat_completions") == "https://a.example/api/plan/v3"
    with pytest.raises(BotError):
        normalize_base("https://a.example/v1/responses","chat_completions")


@pytest.mark.asyncio
async def test_metadata_and_loopback_are_refused():
    for url in ("https://169.254.169.254/latest/meta-data/","https://127.0.0.1/","https://[::1]/"):
        with pytest.raises(BotError) as error:
            await safe_url(url)
        assert error.value.code == "unsafe_endpoint"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,options", [
    ("custom", {}),
    ("minimax", {"max_completion_tokens":8192,"reasoning_effort":"low"}),
    ("minimax", {}),
])
async def test_protocol_validation_uses_isolated_mock_not_real_supplier(service,provider,options):
    counts = []
    tool_message = {"role":"assistant","content":None,"reasoning_content":"opaque supplier reasoning",
                    "tool_calls":[{"id":"echo-call","type":"function","function":{"name":"compat_echo","arguments":"{\"value\":\"personal-bot-test\"}"}}]}
    def handle(request):
        body = json.loads(request.content)
        counts.append(body)
        if body.get("tools"):
            message = tool_message
            finish = "tool_calls"
        else:
            message = {"role":"assistant","content":"personal-bot-test" if any(m.get("role") == "tool" for m in body["messages"]) else "OK"}
            finish = "stop"
        if body["stream"]:
            delta = {"choices":[{"index":0,"delta":message,"finish_reason":finish}]}
            return httpx.Response(200,content="data: "+json.dumps(delta)+"\n\ndata: [DONE]\n\n",headers={"Content-Type":"text/event-stream"})
        return httpx.Response(200,json={"choices":[{"message":message,"finish_reason":finish}]})
    models = Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    connection = service.connections.save({"provider":provider,"billing_mode":"api","protocol":"chat_completions",
        "base_url":"https://model.example/v1" if provider == "custom" else "", "model_id":"mock",
        "api_key":"test-key-only","options":options})
    result = await models.validate(connection["id"])
    assert result["checks"]["tools"] == "passed"
    assert result["real_provider_test"] is False
    assert len(counts) == 4
    for body in counts:
        field = "max_completion_tokens" if provider == "minimax" else "max_tokens"
        assert body[field] == 2048
        assert set(body).intersection({"max_tokens","max_completion_tokens","max_output_tokens"}) == {field}
        if "reasoning_effort" in options:
            assert body["reasoning_effort"] == "low"
        if provider == "minimax" and body["stream"]:
            assert body["stream_options"] == {"include_usage":True}
        else:
            assert "stream_options" not in body
    # The next request must retain the entire assistant reasoning/tool message.
    expected = {**tool_message,"content":""}
    assert counts[3]["messages"][-2] == expected
    assert service.connections.get(connection["id"])["options"] == options
    if provider == "minimax":
        assert connection["base_url"] == "https://api.minimax.cn/v1"


@pytest.mark.asyncio
async def test_responses_validation_preserves_reasoning_and_lower_output_budget(service):
    bodies = []
    opaque = {"type":"reasoning","id":"rs-validation","encrypted_content":"opaque-state"}
    call = {"type":"function_call","id":"fc-validation","call_id":"echo-call","name":"compat_echo","arguments":"{\"value\":\"personal-bot-test\"}"}
    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("tools"):
            output = [opaque,call]
        else:
            text = "personal-bot-test" if any(item.get("type") == "function_call_output" for item in body["input"]) else "OK"
            output = [{"type":"message","role":"assistant","content":[{"type":"output_text","text":text}]}]
        response = {"status":"completed","output":output}
        if body["stream"]:
            event = {"type":"response.completed","response":response}
            return httpx.Response(200,content="data: "+json.dumps(event)+"\n\n",headers={"Content-Type":"text/event-stream"})
        return httpx.Response(200,json=response)
    options = {"max_output_tokens":1024,"reasoning":{"effort":"low"}}
    connection = service.connections.save({"provider":"custom","billing_mode":"api","protocol":"responses",
        "base_url":"https://model.example/v1","model_id":"mock","api_key":"test-key-only","options":options})
    models = Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    result = await models.validate(connection["id"])
    assert result["checks"] == {"auth":"passed","text":"passed","stream":"passed","tools":"passed","vision":"not_tested"}
    assert len(bodies) == 4
    assert all(body["max_output_tokens"] == 1024 and body["reasoning"] == {"effort":"low"} and
               "max_tokens" not in body and "max_completion_tokens" not in body and "stream_options" not in body for body in bodies)
    assert bodies[3]["input"][-3:] == [opaque,call,{"type":"function_call_output","call_id":"echo-call","output":"personal-bot-test"}]
    assert service.connections.get(connection["id"])["options"] == options


@pytest.mark.asyncio
async def test_validation_rejects_truncated_tool_call_even_with_complete_arguments(service):
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {"role":"assistant","content":"OK"}
        finish = "stop"
        if body.get("tools"):
            message["tool_calls"] = [{"id":"truncated-call","type":"function","function":{"name":"compat_echo","arguments":"{\"value\":\"personal-bot-test\"}"}}]
            finish = "length"
        if body["stream"]:
            event = {"choices":[{"index":0,"delta":message,"finish_reason":finish}]}
            return httpx.Response(200,content="data: "+json.dumps(event)+"\n\ndata: [DONE]\n\n")
        return httpx.Response(200,json={"choices":[{"message":message,"finish_reason":finish}]})
    models = Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    result = await models.validate(service.connections.bind()["id"])
    assert len(requests) == 3
    assert result["checks"]["tools"] == "failed"
    assert result["errors"][0]["code"] == "model_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,options", [
    ("chat_completions", {"max_tokens":2048,"max_completion_tokens":2048}),
    ("chat_completions", {"max_output_tokens":2048}),
    ("responses", {"max_tokens":2048}),
    ("responses", {"max_output_tokens":True}),
    ("chat_completions", {"max_tokens":0}),
])
async def test_validation_refuses_invalid_budget_without_provider_request(service,protocol,options):
    def handle(request):
        pytest.fail("invalid output parameters must not reach the supplier")
    connection = service.connections.save({"provider":"custom","billing_mode":"api","protocol":protocol,
        "base_url":"https://model.example/v1","model_id":"mock","api_key":"test-key-only","options":options})
    models = Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    with pytest.raises(BotError) as error:
        await models.validate(connection["id"])
    assert error.value.code == "invalid_validation_options"
    assert all(value == "not_tested" for value in service.connections.get(connection["id"])["capabilities"].values())


@pytest.mark.asyncio
async def test_retry_attempts_have_separate_audits_and_unknown_usage(service,monkeypatch):
    from personal_bot import models as model_module
    receipt=service.accept({"client_request_id":"retry","text":"answer","mode":"new"})
    task=service.claim("runtime-test")
    count=0
    def handle(request):
        nonlocal count
        count+=1
        if count==1:
            return httpx.Response(503,json={"error":{"code":"server_is_overloaded"}},headers={"retry-after":"0"})
        payload={"choices":[{"index":0,"delta":{"role":"assistant","content":"answer"},"finish_reason":"stop"}],"usage":{"total_tokens":5}}
        return httpx.Response(200,content="data: "+json.dumps(payload)+"\n\ndata: [DONE]\n\n")
    async def no_sleep(seconds):pass
    monkeypatch.setattr(model_module.asyncio,"sleep",no_sleep)
    models=Models(service.s,service.connections,PublicHttp(httpx.MockTransport(handle)))
    result=await models.task_call(task["id"],task["attempt_id"],"runtime-test",1,"0",[{"role":"user","content":"answer"}],[])
    audits=service.s.all("SELECT * FROM model_calls ORDER BY started_at")
    assert len(audits)==2 and {a["status"] for a in audits}=={"unknown","succeeded"}
    assert service.task(task["id"])["token_usage_estimated"] is True
    assert service.task(task["id"])["token_count"]>5
    cached=await models.task_call(task["id"],task["attempt_id"],"runtime-test",1,"0",[{"role":"user","content":"answer"}],[])
    assert cached==result and count==2
