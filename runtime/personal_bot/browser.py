"""Separate browser identity, public-only egress and epoch-checked RFB input."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import ipaddress
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import tempfile
import time
from urllib.parse import urlsplit, quote

from starlette.websockets import WebSocket

SOCKET = os.environ.get('PERSONAL_BOT_BROWSER_SOCKET','/run/personal-bot/browser.sock')
DISPLAY_IDLE_SECONDS = 30
_ELEMENT_IDENTITY_JS = 'e=>({tag:e.tagName,type:e.getAttribute("type"),name:e.getAttribute("name"),href:e.getAttribute("href"),formaction:e.getAttribute("formaction"),form:e.form?{action:e.form.action,method:e.form.method}:null,text:(e.innerText||"").slice(0,160)})'
_ELEMENT_OBSERVATION_JS = '''e=>({
    identity:('''+_ELEMENT_IDENTITY_JS+''')(e),
    tag:e.tagName.toLowerCase()
})'''


class BrowserTaskChanged(ValueError):
    """The caller's conversation is no longer the active browser task."""

    def __init__(self):
        super().__init__('浏览器已切换到其他任务，请返回当前会话重新打开')

    def detail(self):
        return {'code':'browser_task_changed','message':str(self)}


async def request(path: str, body: dict | None = None) -> dict:
    import httpx
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=SOCKET),timeout=65) as client:
        response=await client.request('GET' if body is None else 'POST','http://browser'+path,json=body)
        if response.status_code==409:
            # Preserve this actionable conflict through the API rather than
            # converting the broker's task mismatch into an internal error.
            try: detail=response.json().get('detail')
            except (ValueError,AttributeError): detail=None
            if isinstance(detail,dict) and detail.get('code')=='browser_task_changed':
                from .errors import BotError
                raise BotError('browser_task_changed',detail['message'],409)
        response.raise_for_status()
        return response.json()


async def status() -> dict:
    return await request('/status')


async def control(action: str, device_id: str, browser_epoch: int | None = None, *, session_id: str | None = None,
                  expected_task_id: str | None = None) -> dict:
    body=dict(action=action,device_id=device_id,browser_epoch=browser_epoch,session_id=session_id)
    if expected_task_id is not None: body['expected_task_id']=expected_task_id
    return await request('/control',body)


async def perform(action: dict, task_id: str, attempt_id: str, approval: dict | None = None, *, conversation_id: str | None = None) -> dict:
    body=dict(action=action,task_id=task_id,attempt_id=attempt_id,approval=approval)
    if conversation_id is not None: body['conversation_id']=conversation_id
    return await request('/perform',body)


async def quiesce() -> dict:
    return await request('/quiesce',{})


async def create_view_session(device_id: str, owner_id: str, *, session_id: str | None = None,
                              expected_task_id: str | None = None) -> dict:
    body=dict(device_id=device_id,owner_id=owner_id,session_id=session_id)
    if expected_task_id is not None: body['expected_task_id']=expected_task_id
    return await request('/view-sessions',body)


async def revoke_session(session_id: str) -> dict:
    return await request('/revoke-session',{'session_id':session_id})


async def proxy_view(websocket, view_session_id: str, owner_id: str, *, session_id: str | None = None) -> None:
    from websockets.asyncio.client import unix_connect
    from starlette.websockets import WebSocketDisconnect
    try:
        async with unix_connect(SOCKET,uri='ws://browser/view/'+view_session_id,
                                additional_headers={'X-Owner-ID':owner_id,'X-Session-ID':session_id or ''},max_size=4*1024**2) as upstream:
            await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols',[]) else None)
            async def inward():
                while True:
                    message=await websocket.receive()
                    if message['type']=='websocket.disconnect':
                        return
                    data=message.get('bytes')
                    if data is not None:
                        await upstream.send(data)
            async def outward():
                async for data in upstream:
                    if isinstance(data,bytes):
                        await websocket.send_bytes(data)
            tasks=[asyncio.create_task(inward()),asyncio.create_task(outward())]
            _,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    except (OSError,WebSocketDisconnect):
        await websocket.close(code=1011)


async def public_addresses(host: str, port: int) -> list[str]:
    if port not in {80,443} or not host or host.endswith(('.local','.internal','.localhost')):
        raise ValueError('Only public HTTP(S) targets on ports 80/443 are allowed')
    records=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
    addresses=list(dict.fromkeys(record[4][0] for record in records))
    if not addresses or any(not ipaddress.ip_address(value.split('%')[0]).is_global for value in addresses):
        raise ValueError('Non-public network destination refused')
    return addresses


async def egress_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote=None
    try:
        header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),15)
        if len(header)>32768: raise ValueError('Proxy header limit')
        lines=header.decode('latin-1').split('\r\n')
        method,target,version=lines[0].split(' ',2)
        if method=='CONNECT':
            parsed=urlsplit('//'+target)
            host,port=parsed.hostname,parsed.port or 443
        else:
            parsed=urlsplit(target)
            if parsed.scheme!='http' or parsed.username or parsed.password:
                raise ValueError('Invalid public proxy URL')
            host,port=parsed.hostname,parsed.port or 80
        addresses=await public_addresses(host,port)
        upstream,remote=await asyncio.wait_for(asyncio.open_connection(addresses[0],port),15)
        if method=='CONNECT':
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
        else:
            path=parsed.path or '/'
            if parsed.query: path+='?'+parsed.query
            # No forwarding of proxy auth, and no absolute-form target upstream.
            headers=[line for line in lines[1:] if line and not line.lower().startswith(('proxy-authorization:','proxy-connection:','connection:'))]
            remote.write((f'{method} {path} {version}\r\n'+'\r\n'.join(headers)+'\r\nConnection: close\r\n\r\n').encode('latin-1'))
            await remote.drain()
        await writer.drain()
        async def copy(source,sink):
            while chunk:=await source.read(65536):
                sink.write(chunk)
                await sink.drain()
        tasks=[asyncio.create_task(copy(reader,remote)),asyncio.create_task(copy(upstream,writer))]
        _,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in pending: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    except (OSError,ValueError,TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError):
        if remote is None:
            writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
    finally:
        if remote is not None: remote.close()
        writer.close()


class Browser:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.context=None
        self.playwright=None
        self.page=None
        self.epoch=1
        self.mode='idle'
        self.device=None
        self.owner_session=None
        self.task=None
        self.conversation=None
        self.last_input=0.0
        self.sessions={}
        self.viewers={}
        self.refs={}
        self.observation=None
        self.observed_url=None
        self.proxy=None
        self.xvfb=None
        self.vnc=None
        self.error=None
        self.profile=Path(os.environ.get('PERSONAL_BOT_BROWSER_PROFILE','/data/browser'))
        self.identity_salt=None
        self.private_dir=None
        self.xauthority=None
        self.vnc_socket=None
        self.display_idle_task=None
        self.browser_start_seconds=None

    def snapshot(self) -> dict:
        deadline=datetime.fromtimestamp(time.time()+max(0,1800-(time.monotonic()-self.last_input)),timezone.utc).isoformat() if self.mode=='human' else None
        return {'available':True,'running':self.context is not None,'mode':self.mode,'browser_epoch':self.epoch,
                'device_id':self.device,'task_id':self.task,'human_idle_deadline':deadline,'width':1280,'height':720,
                'display_running':self.vnc is not None and self.vnc.returncode is None,'viewer_count':len(self.viewers),
                'browser_start_seconds':self.browser_start_seconds,
                'url':self.page.url if self.page else None,'error':self.error}

    async def ensure(self):
        if self.context: return
        started=time.monotonic()
        from playwright.async_api import async_playwright
        self.profile.mkdir(parents=True,exist_ok=True,mode=0o700)
        salt=self.profile/'.personal-bot-identity-salt'
        if not salt.exists():
            salt.write_bytes(secrets.token_bytes(32))
            salt.chmod(0o600)
        self.identity_salt=salt.read_bytes()
        self.private_dir=Path(tempfile.mkdtemp(prefix='personal-bot-browser-',dir='/tmp'))
        self.xauthority=self.private_dir/'Xauthority'
        self.vnc_socket=self.private_dir/'vnc.sock'
        # FamilyWild allows local X clients to find this fresh display cookie
        # without exposing its value in argv, environment or shared files.
        fields=[b'',b'99',b'MIT-MAGIC-COOKIE-1',secrets.token_bytes(16)]
        self.xauthority.write_bytes(struct.pack('!H',65535)+b''.join(struct.pack('!H',len(value))+value for value in fields))
        self.xauthority.chmod(0o600)
        try:
            self.xvfb=await asyncio.create_subprocess_exec('Xvfb',':99','-screen','0','1280x720x24','-nolisten','tcp',
                '-auth',str(self.xauthority),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            for _ in range(50):
                if Path('/tmp/.X11-unix/X99').exists(): break
                await asyncio.sleep(.1)
            self.proxy=await asyncio.start_server(egress_proxy,'127.0.0.1',18888,limit=32768)
            self.playwright=await async_playwright().start()
            self.context=await self.playwright.chromium.launch_persistent_context(str(self.profile),headless=False,
                env={**os.environ,'DISPLAY':':99','XAUTHORITY':str(self.xauthority),
                     'HOME':str(self.private_dir),'XDG_CONFIG_HOME':str(self.private_dir/'config'),
                     'XDG_CACHE_HOME':str(self.private_dir/'cache')},viewport={'width':1280,'height':720},chromium_sandbox=True,
                proxy={'server':'http://127.0.0.1:18888','bypass':''},
                args=['--proxy-bypass-list=<-loopback>','--disable-quic','--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                      '--disable-extensions','--disable-background-networking'])
            self.context.set_default_timeout(15000)
            self.page=self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.browser_start_seconds=time.monotonic()-started
            from .telemetry import emit_timing
            emit_timing('browser_started',self.browser_start_seconds)
            self.error=None
        except BaseException:
            await self.close()
            raise

    async def start_vnc(self):
        # Caller holds the browser lock. One bridge serves all current viewers.
        self.cancel_display_idle()
        if not self.vnc_socket or not self.xauthority:
            raise RuntimeError('Private browser transport is not initialized')
        if self.vnc and self.vnc.returncode is None:
            if self.vnc_socket.is_socket(): return
            await self.stop_vnc()
        if self.vnc_socket.exists():
            if not self.vnc_socket.is_socket():
                raise RuntimeError('Unexpected private VNC path')
            self.vnc_socket.unlink()
        try:
            self.vnc=await asyncio.create_subprocess_exec('x11vnc','-display',':99','-auth',str(self.xauthority),
                '-unixsock',str(self.vnc_socket),'-rfbport','0','-rfbportv6','0','-forever','-shared','-nopw',
                '-noclipboard','-nosetclipboard','-noprimary','-nosetprimary','-clear_keys','-quiet',stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            for _ in range(50):
                if self.vnc.returncode is not None:
                    raise RuntimeError('Private VNC transport failed to start')
                if self.vnc_socket.is_socket():
                    self.vnc_socket.chmod(0o600)
                    return
                await asyncio.sleep(.1)
            raise RuntimeError('Private VNC transport did not become ready')
        except BaseException:
            await self.stop_vnc()
            raise

    def cancel_display_idle(self):
        task,self.display_idle_task=self.display_idle_task,None
        if task is not None and task is not asyncio.current_task(): task.cancel()

    def schedule_display_idle(self):
        # Only a connected viewer owns display lifetime; unused tickets and
        # passive status reads do not keep the bridge or the runtime alive.
        if self.viewers or not self.vnc or self.vnc.returncode is not None: return
        self.cancel_display_idle()
        async def retire():
            current=asyncio.current_task()
            try:
                await asyncio.sleep(DISPLAY_IDLE_SECONDS)
                async with self.lock:
                    if self.display_idle_task is current and not self.viewers:
                        self.display_idle_task=None
                        await self.stop_vnc()
            finally:
                if self.display_idle_task is current: self.display_idle_task=None
        self.display_idle_task=asyncio.create_task(retire())

    async def stop_vnc(self):
        self.cancel_display_idle()
        if self.vnc and self.vnc.returncode is None:
            self.vnc.terminate()
            try: await asyncio.wait_for(self.vnc.wait(),5)
            except TimeoutError:
                self.vnc.kill()
                await self.vnc.wait()
        self.vnc=None
        if self.vnc_socket and self.vnc_socket.is_socket():
            self.vnc_socket.unlink()

    async def close(self):
        self.cancel_display_idle()
        self.epoch+=1
        self.refs.clear()
        self.observation=None
        self.observed_url=None
        self.mode='idle'
        self.device=None
        self.owner_session=None
        self.task=None
        self.conversation=None
        self.sessions.clear()
        for ws in list(self.viewers.values()):
            await ws.close(code=1001)
        self.viewers.clear()
        if self.context:
            await self.context.close()
            self.context=None
        self.page=None
        if self.playwright:
            await self.playwright.stop()
            self.playwright=None
        await self.stop_vnc()
        for process in [self.xvfb]:
            if process and process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(),5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self.vnc=self.xvfb=None
        if self.private_dir:
            import shutil
            if self.private_dir.parent != Path('/tmp') or not self.private_dir.name.startswith('personal-bot-browser-'):
                raise RuntimeError('Unexpected private browser directory')
            shutil.rmtree(self.private_dir)
            self.private_dir=None
            self.xauthority=None
            self.vnc_socket=None
        if self.proxy:
            self.proxy.close()
            await self.proxy.wait_closed()
            self.proxy=None

    async def identity(self):
        parsed=urlsplit(self.page.url)
        origin=f'{parsed.scheme}://{parsed.netloc}'
        cookies=await self.context.cookies([origin]) if parsed.scheme in {'http','https'} else []
        storage=await self.page.evaluate('()=>Object.fromEntries(Object.entries(localStorage))') if parsed.scheme in {'http','https'} else {}
        payload=json.dumps({'origin':origin,'cookies':sorted(cookies,key=lambda c:(c['domain'],c['path'],c['name'])),'storage':storage},sort_keys=True,separators=(',',':'))
        fingerprint=hmac.new(self.identity_salt,payload.encode(),hashlib.sha256).hexdigest()
        return {'origin':origin,'account_fingerprint':fingerprint,'account_status':'requires_owner_verification'}

    def require_task(self,body):
        # Omission remains compatible with local engineering clients. Every
        # issued ticket still binds to the current task, including no task.
        if 'expected_task_id' in body:
            expected=body['expected_task_id']
            if not isinstance(expected,str) or not expected or expected!=self.task:
                raise BrowserTaskChanged()

    def current_view(self,identity,record):
        return (self.sessions.get(identity) is record and record['epoch']==self.epoch
                and record.get('task_id')==self.task)

    async def switch_task(self,task_id,conversation_id=None):
        # Caller holds the browser lock. Revoke every transport before a new
        # task can see or use the shared page; old queued X11 input must die
        # with its bridge even if its socket's ASGI cleanup is still pending.
        if self.task==task_id: return
        self.epoch+=1
        self.refs.clear()
        self.observation=None
        self.observed_url=None
        self.sessions.clear()
        viewers=list(self.viewers.values())
        self.viewers.clear()
        for ws in viewers: await ws.close(code=1012)
        await self.stop_vnc()
        self.device=None
        self.owner_session=None
        # Cookies/profile belong to this owner, while a live page belongs to
        # one conversation. Never show an old conversation's tab on observe.
        # Same-conversation follow-up tasks can continue looking at the page.
        if conversation_id is not None and conversation_id != self.conversation:
            if not isinstance(conversation_id,str) or not conversation_id:
                raise ValueError('Conversation identity required')
            if self.context is not None:
                # Navigating to blank retains back/forward history. A fresh
                # tab removes that path to another conversation's page.
                fresh=await self.context.new_page()
                try:
                    for page in list(self.context.pages):
                        if page is not fresh: await page.close()
                except BaseException:
                    await fresh.close()
                    raise
                self.page=fresh
        self.conversation=conversation_id
        self.task=task_id

    async def element_identity(self,node):
        return await node.evaluate(_ELEMENT_IDENTITY_JS)

    async def observe(self):
        if self.mode=='human': raise ValueError('Agent collection paused during human control')
        self.observation=secrets.token_hex(12)
        self.observed_url=self.page.url
        self.refs.clear()
        elements=[]
        locator=self.page.locator('a,button,input:not([type=password]),textarea,select,[role=button]')
        for i in range(min(await locator.count(),100)):
            node=locator.nth(i)
            if not await node.is_visible(): continue
            ref=f'e{i}'
            # Only combine the existing main-world reads. Playwright's text
            # and attribute APIs use its isolated world, so keep those calls
            # immune to page-defined DOM getters/methods.
            observed=await node.evaluate(_ELEMENT_OBSERVATION_JS)
            self.refs[ref]=(node,observed['identity'])
            elements.append({'ref':ref,'tag':observed['tag'],'text':(await node.inner_text())[:160],
                             'label':await node.get_attribute('aria-label'),'type':await node.get_attribute('type')})
        return {**self.snapshot(),**await self.identity(),'observation_id':self.observation,'title':await self.page.title(),
                'text':(await self.page.locator('body').inner_text())[:40000],'elements':elements}

    async def action(self,body):
        if self.mode=='human': raise ValueError('Browser waits for human release')
        action=body['action']
        kind=action.get('type',action.get('action'))
        if kind=='status':
            status=self.snapshot()
            if body.get('conversation_id') is not None and body['conversation_id']!=self.conversation:
                # A passive tool read must neither reveal another chat's URL
                # nor change its live page or viewing/control transports.
                status.update(running=False,mode='closed',device_id=None,task_id=None,
                              human_idle_deadline=None,url=None,error=None,
                              display_running=False,viewer_count=0,browser_start_seconds=None)
            return status
        await self.ensure()
        await self.switch_task(body['task_id'],body.get('conversation_id'))
        self.mode='agent'
        if kind=='navigate':
            parsed=urlsplit(action['url'])
            if parsed.scheme not in {'http','https'} or parsed.username or parsed.password:
                raise ValueError('Only public web navigation is supported')
            await public_addresses(parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80))
            await self.page.goto(action['url'],wait_until='domcontentloaded',timeout=30000)
            return await self.observe()
        if kind=='observe': return await self.observe()
        approval=body.get('approval')
        if not approval or approval.get('status') not in {'approved','consumed'} or approval.get('task_id') != body['task_id']:
            raise ValueError('Structured approval required for browser mutation')
        approved=json.loads(approval['action']) if isinstance(approval.get('action'),str) else approval.get('action',{})
        if approved.get('arguments',{}).get('action') != action:
            raise ValueError('Approval parameters differ from current action')
        if action.get('browser_epoch') != self.epoch or action.get('observation_id') != self.observation:
            raise ValueError('Fresh browser observation required after state/control change')
        if self.observed_url is None or self.page.url != self.observed_url:
            raise ValueError('Observed page changed; re-observe and reapprove')
        identity=await self.identity()
        if action.get('origin') != identity['origin'] or action.get('account_fingerprint') != identity['account_fingerprint']:
            raise ValueError('Site/account state changed or is unbound; re-observe and reapprove')
        if action.get('url') and action['url'] != self.page.url:
            raise ValueError('Approval target page changed')
        bound=self.refs.get(action.get('ref'))
        if kind in {'click','fill','select'} and bound is None:
            raise ValueError('Unknown observation reference')
        node=bound[0] if bound else None
        if node is not None and await self.element_identity(node)!=bound[1]:
            raise ValueError('Target element or form changed; re-observe and reapprove')
        if kind=='click': await node.click()
        elif kind=='fill':
            if await node.get_attribute('type') in {'password','file'}:
                raise ValueError('Sensitive inputs require human takeover')
            await node.fill(str(action['value']))
        elif kind=='select': await node.select_option(str(action['value']))
        elif kind=='scroll': await self.page.mouse.wheel(0,min(max(int(action.get('dy',500)),-2000),2000))
        elif kind=='back': await self.page.go_back(wait_until='domcontentloaded')
        else: raise ValueError('Unsupported browser action; arbitrary JS/CDP is disabled')
        self.refs.clear()
        self.observation=None
        self.observed_url=None
        return await self.observe()

    async def takeover(self,body):
        self.require_task(body)
        action=body['action']
        if action not in {'takeover','release','transfer'}: raise ValueError('Invalid control action')
        if body.get('browser_epoch') is not None and body['browser_epoch'] != self.epoch:
            raise ValueError('Browser control revision changed')
        if action=='takeover' and self.mode=='human' and self.device != body['device_id']:
            raise ValueError('Use explicit transfer for another device')
        if action=='release' and (self.device != body['device_id'] or self.owner_session != body.get('session_id')):
            raise ValueError('Only the controlling session can release')
        await self.ensure()
        self.epoch+=1
        self.refs.clear()
        self.observation=None
        self.observed_url=None
        # Existing viewer sockets may not carry inputs from an older epoch.
        viewers=list(self.viewers.values())
        self.viewers.clear()
        for ws in viewers: await ws.close(code=1012)
        self.sessions.clear()
        # Reap the old X11 input bridge before granting another epoch. Its
        # socket queues cannot deliver old-device events to the next owner.
        await self.stop_vnc()
        if action=='release':
            self.mode='idle'
            self.device=None
            self.owner_session=None
        else:
            self.mode='human'
            self.device=body['device_id']
            self.owner_session=body.get('session_id')
            self.last_input=time.monotonic()
        return self.snapshot()

    async def expire(self):
        while True:
            await asyncio.sleep(5)
            if self.mode=='human' and time.monotonic()-self.last_input>=1800:
                async with self.lock: await self.close()


class RFBGuard:
    """Frame complete client messages so permissions are checked per input."""
    def __init__(self):
        self.buffer=bytearray()
        self.handshake=0

    def feed(self,data: bytes) -> list[tuple[bytes,bool]]:
        self.buffer.extend(data)
        if len(self.buffer)>1024*1024: raise ValueError('RFB buffer limit')
        messages=[]
        while self.buffer:
            control=False
            discard=False
            if self.handshake<3:
                length=[12,1,1][self.handshake]
                if len(self.buffer)<length: break
                value=bytes(self.buffer[:length])
                if self.handshake==0 and value != b'RFB 003.008\n': raise ValueError('RFB 3.8 required')
                if self.handshake==1 and value!=b'\x01': raise ValueError('Unexpected RFB security type')
                if self.handshake==2 and value!=b'\x01': raise ValueError('Shared RFB sessions required')
                self.handshake+=1
            else:
                kind=self.buffer[0]
                lengths={0:20,3:10,4:8,5:6,150:10,255:12}
                if kind==2:
                    if len(self.buffer)<4: break
                    count=struct.unpack('!H',self.buffer[2:4])[0]
                    if count>256: raise ValueError('Too many RFB encodings')
                    length=4+count*4
                elif kind==6:
                    if len(self.buffer)<8: break
                    # Extended clipboard frames encode their bounded payload
                    # length as a negative signed integer. noVNC sends this
                    # capability handshake even with clipboard use disabled.
                    # Consume and discard both forms; never forward contents.
                    size=abs(struct.unpack('!i',self.buffer[4:8])[0])
                    if size>65536: raise ValueError('Clipboard disabled')
                    length=8+size
                    discard=True
                elif kind in lengths: length=lengths[kind]
                else: raise ValueError('Unsupported RFB extension')
                control=kind in {4,5,255}
                if len(self.buffer)<length: break
            value=bytes(self.buffer[:length])
            del self.buffer[:length]
            if not discard: messages.append((value,control))
        return messages


def main():
    from contextlib import asynccontextmanager
    from email.message import Message
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from starlette.routing import Route,WebSocketRoute
    from starlette.websockets import WebSocketDisconnect
    from .broker_support import serve
    browser=Browser()

    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(browser.expire())
        yield
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await browser.close()

    async def http_exception(request,exc):
        return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=exc.headers)

    async def object_body(request):
        # This private broker accepts untyped JSON objects. Keep its existing
        # validation envelope without loading FastAPI/Pydantic into this role.
        try:
            raw=await request.body()
            value=raw
            if raw:
                content_type=Message()
                content_type['content-type']=request.headers.get('content-type','')
                subtype=content_type.get_content_subtype()
                if content_type.get_content_maintype()=='application' and (subtype=='json' or subtype.endswith('+json')):
                    value=await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(422,[{'type':'json_invalid','loc':['body',exc.pos],'msg':'JSON decode error',
                                     'input':{},'ctx':{'error':exc.msg}}]) from exc
        except Exception as exc:
            raise HTTPException(400,'There was an error parsing the body') from exc
        if not raw:
            raise HTTPException(422,[{'type':'missing','loc':['body'],'msg':'Field required','input':None}])
        if not isinstance(value,dict):
            raise HTTPException(422,[{'type':'dict_type','loc':['body'],'msg':'Input should be a valid dictionary',
                                     'input':value.decode() if isinstance(value,bytes) else value}])
        return value

    async def get_status(request): return JSONResponse(browser.snapshot())

    async def set_control(request):
        body=await object_body(request)
        async with browser.lock:
            try: result=await browser.takeover(body)
            except BrowserTaskChanged as exc: raise HTTPException(409,exc.detail()) from exc
            except (ValueError,TimeoutError) as exc: raise HTTPException(409,str(exc)) from exc
        return JSONResponse(result)

    async def action(request):
        body=await object_body(request)
        async with browser.lock:
            try: result=await browser.action(body)
            except Exception as exc:
                # A structured failure reaches the ledger; raw browser traces do not.
                raise HTTPException(409,{'code':'browser_action_failed','message':str(exc).split('\n')[0][:200]}) from exc
        return JSONResponse(result)

    async def stop(request):
        async with browser.lock: await browser.close()
        return JSONResponse({'stopped':True,**browser.snapshot()})

    async def revoke(request):
        body=await object_body(request)
        async with browser.lock:
            if browser.owner_session==body['session_id']:
                # Revoke input; do not automatically return control to the Agent.
                browser.device=None
                browser.owner_session=None
                browser.epoch+=1
                browser.refs.clear()
                browser.observation=None
                browser.observed_url=None
                # Controller revocation invalidates every viewer epoch. Detach
                # registrations before closing so each stream is closed once.
                viewers=list(browser.viewers.values())
                browser.viewers.clear()
                browser.sessions.clear()
                for ws in viewers: await ws.close(code=1008)
                await browser.stop_vnc()
            else:
                for identity,value in list(browser.sessions.items()):
                    if value['session_id']==body['session_id']:
                        browser.sessions.pop(identity,None)
                        ws=browser.viewers.pop(identity,None)
                        if ws is not None: await ws.close(code=1008)
                browser.schedule_display_idle()
        return JSONResponse(browser.snapshot())

    async def view_session(request):
        body=await object_body(request)
        async with browser.lock:
            try: browser.require_task(body)
            except BrowserTaskChanged as exc: raise HTTPException(409,exc.detail()) from exc
            await browser.ensure()
            identity=secrets.token_urlsafe(24)
            browser.sessions[identity]={**body,'task_id':browser.task,'epoch':browser.epoch,'expires':time.monotonic()+60,'used':False}
            result={'view_session_id':identity,'runtime_id':os.environ.get('PERSONAL_BOT_RUNTIME_ID',''),
                    'browser_epoch':browser.epoch,'ws_path':'/api/browser/view/'+identity,
                    'task_id':browser.task,'expires_at':datetime.fromtimestamp(time.time()+60,timezone.utc).isoformat(),'width':1280,'height':720}
        return JSONResponse(result)

    async def view(ws: WebSocket):
        identity=ws.path_params['identity']
        async with browser.lock:
            record=browser.sessions.get(identity)
            if not record or record['used'] or record['expires']<time.monotonic() or not browser.current_view(identity,record) or record['owner_id']!=ws.headers.get('x-owner-id') or (record.get('session_id') or '')!=ws.headers.get('x-session-id',''):
                await ws.close(code=1008)
                return
            record['used']=True
            try:
                await browser.start_vnc()
                await asyncio.wait_for(ws.accept(),5)
            except BaseException:
                browser.sessions.pop(identity,None)
                browser.schedule_display_idle()
                raise
            browser.viewers[identity]=ws
        remote=None
        tasks=[]
        def current_record():
            # The 60-second ticket deadline governs its initial handshake;
            # revocation and epochs govern an established stream.
            return browser.current_view(identity,record)
        try:
            for attempt in range(30):
                try:
                    reader,remote=await asyncio.open_unix_connection(str(browser.vnc_socket))
                    break
                except OSError:
                    if attempt==29: raise
                    await asyncio.sleep(.1)
            guard=RFBGuard()
            async def inward():
                while True:
                    data=await ws.receive_bytes()
                    for frame,is_input in guard.feed(data):
                        async with browser.lock:
                            if not current_record(): return
                            if is_input:
                                if browser.mode!='human' or browser.device!=record['device_id'] or browser.owner_session!=record.get('session_id'):
                                    continue
                                browser.last_input=time.monotonic()
                            remote.write(frame)
                            await asyncio.wait_for(remote.drain(),5)
            async def outward():
                while data:=await reader.read(65536):
                    async with browser.lock:
                        if not current_record(): return
                        await asyncio.wait_for(ws.send_bytes(data),10)
            tasks=[asyncio.create_task(inward()),asyncio.create_task(outward())]
            _,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()
            results=await asyncio.gather(*tasks,return_exceptions=True)
        except (OSError,WebSocketDisconnect,ValueError):
            await ws.close(code=1008)
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            if tasks: await asyncio.gather(*tasks,return_exceptions=True)
            async with browser.lock:
                if browser.viewers.get(identity) is ws:
                    browser.viewers.pop(identity,None)
                    browser.schedule_display_idle()
                if browser.sessions.get(identity) is record: browser.sessions.pop(identity,None)
            if remote:
                remote.close()
                await remote.wait_closed()

    status_route=Route('/status',get_status,methods=['GET'])
    # Route normally adds HEAD to GET, but this broker has always been GET-only.
    status_route.methods={'GET'}
    app=Starlette(lifespan=lifespan,exception_handlers={HTTPException:http_exception},routes=[
        status_route,Route('/control',set_control,methods=['POST']),
        Route('/perform',action,methods=['POST']),Route('/quiesce',stop,methods=['POST']),
        Route('/revoke-session',revoke,methods=['POST']),Route('/view-sessions',view_session,methods=['POST']),
        WebSocketRoute('/view/{identity}',view),
    ])
    serve(app,SOCKET)


if __name__=='__main__':
    main()
