from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from .errors import BotError


def normalize_base(value: str, protocol: str) -> str:
    value = value.strip().rstrip("/")
    suffix = "/responses" if protocol == "responses" else "/chat/completions"
    other = "/chat/completions" if protocol == "responses" else "/responses"
    if value.endswith(other):
        raise BotError("endpoint_protocol_mismatch", "完整调用路径与所选协议不一致")
    if value.endswith(suffix):
        value = value[:-len(suffix)]
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BotError("unsafe_endpoint", "模型地址须为不含凭证、查询参数及片段的 HTTPS 地址")
    if parsed.port not in (None, 443):
        raise BotError("unsafe_endpoint", "仅允许 HTTPS 默认端口")
    return value


async def safe_url(url: str) -> tuple[str, dict, dict]:
    """Pin a validated public DNS result for this connection; retain TLS SNI."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise BotError("unsafe_endpoint", "目标地址不在允许范围")
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, 443, 0, socket.SOCK_STREAM)
        ips = list(dict.fromkeys(item[4][0] for item in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise BotError("unsafe_endpoint", "目标解析到未开放的内网、metadata 或保留地址")
        selected = ips[0]
    except socket.gaierror as exc:
        raise BotError("dns_failure", "无法解析目标地址", 502, True) from exc
    authority = f"[{selected}]" if ":" in selected else selected
    target = parsed._replace(netloc=authority).geturl()
    return target, {"Host": parsed.hostname}, {"sni_hostname": parsed.hostname}


class PublicHttp:
    def __init__(self, test_transport: httpx.AsyncBaseTransport | None = None):
        self.test_transport = test_transport

    async def request(self, method: str, url: str, *, headers=None, json=None, stream=False, before_send=None):
        if self.test_transport is not None:
            target, host, extensions = url, {}, {}
        else:
            target, host, extensions = await safe_url(url)
        client = httpx.AsyncClient(transport=self.test_transport, follow_redirects=False,
                                   trust_env=False, timeout=httpx.Timeout(30, connect=15))
        request = client.build_request(method, target, headers={**host, **(headers or {})}, json=json, extensions=extensions)
        try:
            # DNS and caller-side queues can await. Recheck synchronous caller
            # authorization at the final send boundary, after those waits.
            if before_send is not None:
                before_send()
            response = await client.send(request, stream=True)
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise BotError("redirect_refused", "拒绝向重定向地址转发凭证", 502)
            if not stream:
                content=bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content)>8*1024*1024:
                        await response.aclose()
                        raise BotError("response_too_large", "远端响应超过限制", 502)
                await response.aclose()
                response=httpx.Response(response.status_code,headers=response.headers,content=bytes(content),request=response.request,extensions=response.extensions)
            return client, response
        except BaseException:
            await client.aclose()
            raise
