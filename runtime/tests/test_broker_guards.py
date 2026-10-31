import asyncio
import struct
import pytest

from personal_bot.browser import RFBGuard, public_addresses


def test_rfb_fragmented_handshake_and_input_classification():
    guard=RFBGuard()
    assert guard.feed(b'RFB 003.')==[]
    output=guard.feed(b'008\n\x01\x01'+b'\x03\x00'+struct.pack('!HHHH',0,0,1280,720))
    assert [len(frame) for frame,_ in output]==[12,1,1,10]
    assert not any(is_input for _,is_input in output)
    assert guard.feed(b'\x04\x01\x00')==[]
    output=guard.feed(b'\x00\x00\x00\x00A')
    assert output==[(b'\x04\x01\x00\x00\x00\x00\x00A',True)]


def test_rfb_clipboard_is_not_forwarded_and_unknown_extensions_rejected():
    guard=RFBGuard()
    guard.feed(b'RFB 003.008\n\x01\x01')
    assert guard.feed(b'\x06\x00\x00\x00'+struct.pack('!I',6)+b'secret')==[]
    with pytest.raises(ValueError): guard.feed(b'\xfb')


def test_novnc_extended_clipboard_handshake_is_discarded_without_losing_next_frame():
    guard=RFBGuard()
    guard.feed(b'RFB 003.008\n\x01\x01')
    clipboard=bytes.fromhex('06000000fffffff81f00000100000000')
    update=b'\x03\x00'+struct.pack('!HHHH',0,0,1280,720)
    assert guard.feed(clipboard[:10])==[]
    assert guard.feed(clipboard[10:]+update)==[(update,False)]
    for length in (65537,-65537,-2147483648):
        bounded=RFBGuard()
        bounded.feed(b'RFB 003.008\n\x01\x01')
        with pytest.raises(ValueError):
            bounded.feed(b'\x06\x00\x00\x00'+struct.pack('!i',length))


@pytest.mark.asyncio
async def test_browser_egress_rejects_mixed_public_private_dns(monkeypatch):
    loop=asyncio.get_running_loop()
    async def resolve(*args,**kwargs):
        return [(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))]
    monkeypatch.setattr(loop,'getaddrinfo',resolve)
    with pytest.raises(ValueError): await public_addresses('example.org',443)
    with pytest.raises(ValueError): await public_addresses('example.org',8091)


@pytest.mark.asyncio
async def test_browser_egress_returns_pinned_public_ip(monkeypatch):
    loop=asyncio.get_running_loop()
    async def resolve(*args,**kwargs): return [(2,1,6,'',('93.184.216.34',443))]
    monkeypatch.setattr(loop,'getaddrinfo',resolve)
    assert await public_addresses('example.org',443)==['93.184.216.34']
