from unittest.mock import AsyncMock
from xml.sax.saxutils import escape

import httpx
import pytest

from personal_bot.errors import BotError
from personal_bot.net import PublicHttp
from personal_bot.tools import ToolRunner


class TrackedHttp(PublicHttp):
    def __init__(self, handler):
        super().__init__(httpx.MockTransport(handler))
        self.returned = []

    async def request(self, *args, **kwargs):
        client, response = await super().request(*args, **kwargs)
        client.aclose = AsyncMock(wraps=client.aclose)
        response.aclose = AsyncMock(wraps=response.aclose)
        self.returned.append((client, response))
        return client, response

    def assert_closed(self):
        assert len(self.returned) == 1
        client, response = self.returned[0]
        client.aclose.assert_awaited_once()
        response.aclose.assert_awaited_once()
        assert client.is_closed and response.is_closed


def rss(items=()):
    return ('<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            '<title>Bing Search</title><link>https://cn.bing.com/search</link>'
            '<description>Search results</description>'+''.join(items)+'</channel></rss>').encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_host", [None, "cn.bing.com", "www.bing.com"])
async def test_search_uses_direct_restricted_host_and_returns_bounded_rss_results(service,monkeypatch,configured_host):
    if configured_host is None:
        monkeypatch.delenv("PERSONAL_BOT_SEARCH_BING_HOST", raising=False)
    else:
        monkeypatch.setenv("PERSONAL_BOT_SEARCH_BING_HOST", configured_host)
    query = 'Python 文档 & x=1? /官方'
    requests = []
    def handle(request):
        requests.append(request)
        items = [f'<item><title>结果 {i}</title><link>{escape(f"https://www.python.org/doc/?n={i}&amp=1")}</link>'
                 f'<description>摘要 &amp; 资料 {i}</description></item>' for i in range(12)]
        return httpx.Response(200,content=rss(items),headers={"Content-Type":"application/rss+xml; charset=utf-8"})
    runner = ToolRunner(service)
    runner.http = TrackedHttp(handle)
    result = await runner.execute("web_search",{"query":query},"task","attempt","run")
    assert len(requests) == 1
    assert requests[0].url.host == (configured_host or "cn.bing.com")
    assert requests[0].url.scheme == "https" and requests[0].url.path == "/search"
    assert dict(requests[0].url.params) == {"format":"rss","q":query}
    assert result["query"] == query and result["retrieved_at"]
    assert len(result["items"]) == 10
    assert result["items"][0] == {"title":"结果 0","url":"https://www.python.org/doc/?n=0&amp=1","snippet":"摘要 & 资料 0"}
    assert result["items"][-1]["title"] == "结果 9"
    runner.http.assert_closed()


@pytest.mark.asyncio
async def test_search_accepts_valid_empty_rss_channel(service,monkeypatch):
    monkeypatch.delenv("PERSONAL_BOT_SEARCH_BING_HOST", raising=False)
    runner = ToolRunner(service)
    runner.http = TrackedHttp(lambda request: httpx.Response(200,content=rss()))
    result = await runner.execute("web_search",{"query":"no matches"},"task","attempt","run")
    assert result["items"] == []
    runner.http.assert_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,content", [
    (503,b"unavailable"),
    (200,b"not XML"),
    (200,b"<html><body>Verify you are human</body></html>"),
    (200,b"<rss version='2.0'/ >"),
    (200,b"<rss version='2.0'/>"),
    (200,b"<rss><not-channel/></rss>"),
    (200,b"<html><channel/></html>"),
    (200,b'<?xml version="1.0" encoding="not-an-encoding"?><rss><channel/></rss>'),
    (200,b"x"*(2*1024*1024+1)),
    (200,rss(['<item><title>Missing source URL</title></item>'])),
    (200,rss(['<item><title>Bad source URL</title><link>javascript:alert(1)</link></item>'])),
    (200,rss(['<item><title>Bad source URL</title><link>https://[bad</link></item>'])),
])
async def test_search_bad_responses_are_durable_controlled_failures(service,monkeypatch,status,content):
    monkeypatch.delenv("PERSONAL_BOT_SEARCH_BING_HOST", raising=False)
    service.accept({"client_request_id":"search-failure","mode":"new","text":"Search public docs"})
    task = service.claim("runtime-test")
    runner = ToolRunner(service)
    runner.http = TrackedHttp(lambda request: httpx.Response(status,content=content))
    result = await runner.invoke({"task_id":task["id"],"attempt_id":task["attempt_id"],"runtime_id":"runtime-test",
        "revision":task["revision"],"model_call_id":"model-1","call_id":"search-1","name":"web_search",
        "arguments":{"query":"public docs"}})
    assert result["status"] == "failed" and result["halted"] is False
    assert result["result"]["error"]["code"] == "search_unavailable"
    saved = service.s.one("SELECT status,error FROM tool_calls WHERE id=?",(result["id"],))
    assert saved == {"status":"failed","error":"search_unavailable"}
    runner.http.assert_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["https://cn.bing.com", "127.0.0.1", "cn.bing.com@127.0.0.1", "other.example"])
async def test_search_rejects_unapproved_host_configuration_before_network(service,monkeypatch,host):
    monkeypatch.setenv("PERSONAL_BOT_SEARCH_BING_HOST",host)
    def handle(request):
        pytest.fail("unapproved search host must never receive a request")
    runner = ToolRunner(service)
    runner.http = TrackedHttp(handle)
    with pytest.raises(BotError) as error:
        await runner.execute("web_search",{"query":"docs"},"task","attempt","run")
    assert error.value.code == "search_unavailable"
    assert runner.http.returned == []


@pytest.mark.asyncio
async def test_search_keeps_public_http_redirect_refusal(service,monkeypatch):
    monkeypatch.setenv("PERSONAL_BOT_SEARCH_BING_HOST","www.bing.com")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302,headers={"Location":"https://127.0.0.1/private"})
    transport = httpx.MockTransport(handle)
    transport.aclose = AsyncMock(wraps=transport.aclose)
    runner = ToolRunner(service)
    runner.http = PublicHttp(transport)
    with pytest.raises(BotError) as error:
        await runner.execute("web_search",{"query":"docs"},"task","attempt","run")
    assert error.value.code == "redirect_refused"
    assert len(requests) == 1
    transport.aclose.assert_awaited_once()
