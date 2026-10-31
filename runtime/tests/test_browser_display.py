"""Display ownership, grace and epoch regressions without starting native apps."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from personal_bot import browser as browser_module
from personal_bot import broker_support


class Process:
    def __init__(self):
        self.returncode=None
        self.terminated=0

    def terminate(self):
        self.terminated+=1
        self.returncode=0

    def kill(self):
        self.returncode=-9

    async def wait(self):
        return self.returncode


class SocketPath:
    def __init__(self): self.bound=False
    def __str__(self): return '/private/display.sock'
    def exists(self): return self.bound
    def is_socket(self): return self.bound
    def unlink(self): self.bound=False
    def chmod(self,mode): assert mode==0o600


@pytest.fixture
def display(monkeypatch):
    state=browser_module.Browser()
    state.context=SimpleNamespace(close=AsyncMock())
    state.page=SimpleNamespace(url='https://example.com/#unsubmitted-form')
    state.xauthority=Path('/private/authority')
    state.vnc_socket=SocketPath()
    state.ensure=AsyncMock()
    state.test_remotes=[]
    spawned=[]

    async def spawn(*args,**kwargs):
        assert state.lock.locked() and args[0]=='x11vnc'
        assert '-rfbport' in args and args[args.index('-rfbport')+1]=='0'
        process=Process()
        spawned.append(process)
        state.vnc_socket.bound=True
        return process

    class Remote:
        def __init__(self): self.closed=False; self.finished=False
        def write(self,data): pass
        async def drain(self): pass
        def close(self): self.closed=True
        async def wait_closed(self):
            await asyncio.sleep(0)
            self.finished=True

    async def connect(path):
        reader=asyncio.StreamReader()
        reader.feed_data(b'frame')
        remote=Remote()
        state.test_remotes.append(remote)
        return reader,remote

    monkeypatch.setattr(asyncio,'create_subprocess_exec',spawn)
    monkeypatch.setattr(asyncio,'open_unix_connection',connect,raising=False)
    captured={}
    monkeypatch.setattr(browser_module,'Browser',lambda:state)
    monkeypatch.setattr(broker_support,'serve',lambda app,path:captured.update(app=app))
    browser_module.main()
    return state,captured['app'],spawned


def ticket(client,device='device',session='session'):
    return client.post('/view-sessions',json={'owner_id':'owner','device_id':device,'session_id':session}).json()


async def settled(predicate,timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate(): await asyncio.sleep(.002)


def disconnect_viewer(ws,remote):
    # TestClient.__exit__ cancels its ASGI scope immediately after sending
    # disconnect. Let the real broker finish first; otherwise cancellation of
    # an in-progress finally can race future.result() on faster Linux loops.
    ws.close()
    ws.portal.call(settled,lambda:remote.closed and remote.finished)


def test_tickets_and_passive_status_do_not_start_display(display):
    state,app,spawned=display
    with TestClient(app) as client:
        client.get('/status')
        ticket(client)
        status=client.get('/status').json()
        assert status['display_running'] is False and status['viewer_count']==0
        assert status['running'] is True
        assert spawned==[]


@pytest.mark.parametrize('change',[
    {'used':True},{'expires':0},{'epoch':0},{'owner_id':'forged'},{'session_id':'forged'},
])
def test_invalid_viewer_never_starts_display(display,change):
    state,app,spawned=display
    state.sessions['ticket']={'used':False,'expires':time.monotonic()+60,'epoch':state.epoch,
                             'owner_id':'owner','session_id':'session','device_id':'device',**change}
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/view/ticket',headers={'x-owner-id':'owner','x-session-id':'session'}):
                pytest.fail('invalid viewer accepted')
        assert denied.value.code==1008 and spawned==[]


def test_shared_bridge_reconnect_cancels_old_grace_and_preserves_page(display,monkeypatch):
    state,app,spawned=display
    assert browser_module.DISPLAY_IDLE_SECONDS==30
    monkeypatch.setattr(browser_module,'DISPLAY_IDLE_SECONDS',.08)
    page,context=state.page,state.context
    with TestClient(app) as client:
        one= ticket(client)
        two= ticket(client,device='reader',session='reader-session')
        with client.websocket_connect(one['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}) as first:
            assert first.receive_bytes()==b'frame'
            first_remote=state.test_remotes[-1]
            with client.websocket_connect(two['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'reader-session'}) as second:
                assert second.receive_bytes()==b'frame'
                assert len(spawned)==1
                assert client.get('/status').json()['viewer_count']==2
                disconnect_viewer(second,state.test_remotes[-1])
                assert len(state.viewers)==1
            assert state.display_idle_task is None
            disconnect_viewer(first,first_remote)
            assert not state.viewers
        assert state.display_idle_task is not None
        pending=state.display_idle_task
        again=ticket(client)
        with client.websocket_connect(again['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}) as resumed:
            assert resumed.receive_bytes()==b'frame'
            resumed.portal.call(asyncio.sleep,.12)
            assert pending.done() and state.display_idle_task is None
            assert len(spawned)==1 and spawned[0].terminated==0
            assert state.page is page and state.context is context
            disconnect_viewer(resumed,state.test_remotes[-1])
            assert not state.viewers
            resumed.portal.call(settled,lambda:state.vnc is None)
        assert spawned[0].terminated==1
        assert state.context is context and state.page is page
        context.close.assert_not_awaited()
        assert client.get('/status').json()['display_running'] is False
        third=ticket(client)
        with client.websocket_connect(third['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}) as restarted:
            assert restarted.receive_bytes()==b'frame' and len(spawned)==2
            disconnect_viewer(restarted,state.test_remotes[-1])


@pytest.mark.parametrize('action',['takeover','transfer','release'])
def test_control_epoch_stops_old_bridge_without_starting_an_unused_one(display,action):
    state,app,spawned=display
    state.mode='human'
    state.device='device'
    state.owner_session='session'
    with TestClient(app) as client:
        opened=ticket(client)
        with client.websocket_connect(opened['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}) as ws:
            assert ws.receive_bytes()==b'frame'
            old_remote=state.test_remotes[-1]
            epoch=state.epoch
            response=client.post('/control',json={'action':action,'device_id':'device','session_id':'session','browser_epoch':epoch})
            assert response.status_code==200
            assert response.json()['browser_epoch']==epoch+1
            assert response.json()['display_running'] is False and response.json()['viewer_count']==0
            assert len(spawned)==1 and spawned[0].terminated==1
            with pytest.raises(WebSocketDisconnect): ws.receive_bytes()
            disconnect_viewer(ws,old_remote)
        replacement=ticket(client)
        with client.websocket_connect(replacement['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}) as newer:
            assert newer.receive_bytes()==b'frame' and len(spawned)==2
            disconnect_viewer(newer,state.test_remotes[-1])


def test_revoking_last_reader_retires_display_without_affecting_human_owner(display,monkeypatch):
    state,app,spawned=display
    monkeypatch.setattr(browser_module,'DISPLAY_IDLE_SECONDS',.02)
    state.mode='human'
    state.device='controller'
    state.owner_session='controller-session'
    with TestClient(app) as client:
        opened=ticket(client,device='reader',session='reader-session')
        with client.websocket_connect(opened['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'reader-session'}) as ws:
            assert ws.receive_bytes()==b'frame'
            response=client.post('/revoke-session',json={'session_id':'reader-session'})
            assert response.status_code==200 and response.json()['viewer_count']==0
            ws.portal.call(settled,lambda:state.vnc is None)
            assert spawned[0].terminated==1 and state.owner_session=='controller-session'
            assert state.device=='controller' and state.mode=='human' and state.context is not None
            with pytest.raises(WebSocketDisconnect): ws.receive_bytes()
            disconnect_viewer(ws,state.test_remotes[-1])


@pytest.mark.asyncio
async def test_cancelled_viewer_reaps_bridge_tasks_closes_remote_and_retires_registration(display):
    state,app,spawned=display
    state.sessions['ticket']={'used':False,'expires':time.monotonic()+60,'epoch':state.epoch,
                             'owner_id':'owner','session_id':'session','device_id':'device'}
    received=asyncio.Event()

    class Viewer:
        path_params={'identity':'ticket'}
        headers={'x-owner-id':'owner','x-session-id':'session'}
        async def accept(self): pass
        async def receive_bytes(self): await asyncio.Event().wait()
        async def send_bytes(self,data):
            assert data==b'frame'
            received.set()
        async def close(self,**kwargs): pass

    endpoint=next(route.endpoint for route in app.routes if route.path=='/view/{identity}')
    before=asyncio.all_tasks()
    running=asyncio.create_task(endpoint(Viewer()))
    await asyncio.wait_for(received.wait(),1)
    require_tasks=[task for task in asyncio.all_tasks()-before if task is not running and not task.done()]
    assert len(require_tasks)==2 and state.snapshot()['viewer_count']==1
    running.cancel()
    with pytest.raises(asyncio.CancelledError): await running
    assert all(task.done() for task in require_tasks)
    assert state.test_remotes[-1].closed and state.test_remotes[-1].finished
    assert not state.sessions and not state.viewers and state.display_idle_task is not None
    async with state.lock: await state.close()
    assert state.vnc is None and spawned[0].terminated==1


def test_accept_failure_retires_unused_bridge(display,monkeypatch):
    state,app,spawned=display
    monkeypatch.setattr(browser_module,'DISPLAY_IDLE_SECONDS',.02)

    async def fail_accept(self):
        raise RuntimeError('viewer handshake failed')

    monkeypatch.setattr(browser_module.WebSocket,'accept',fail_accept)
    with TestClient(app) as client:
        opened=ticket(client)
        with pytest.raises(RuntimeError,match='viewer handshake failed'):
            with client.websocket_connect(opened['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'session'}):
                pytest.fail('failed handshake accepted')
        client.portal.call(settled,lambda:state.vnc is None)
        assert len(spawned)==1 and spawned[0].terminated==1 and not state.sessions and not state.viewers


@pytest.mark.asyncio
async def test_close_cancels_pending_display_retirement_without_orphan(display):
    state,app,spawned=display
    async with state.lock:
        await state.start_vnc()
        state.schedule_display_idle()
        pending=state.display_idle_task
        await state.close()
    await asyncio.gather(pending,return_exceptions=True)
    assert pending.cancelled() and state.display_idle_task is None
    assert state.context is None and state.vnc is None and spawned[0].terminated==1


@pytest.mark.asyncio
async def test_chromium_ensure_does_not_start_a_display(tmp_path,monkeypatch):
    from playwright import async_api
    state=browser_module.Browser()
    state.profile=tmp_path/'profile'
    private=tmp_path/'private'
    private.mkdir()
    monkeypatch.setattr(browser_module.tempfile,'mkdtemp',lambda **kwargs:str(private))
    old_exists=Path.exists
    monkeypatch.setattr(Path,'exists',lambda self:True if str(self).replace('\\','/')=='/tmp/.X11-unix/X99' else old_exists(self))
    page=SimpleNamespace(url='about:blank')
    context=SimpleNamespace(pages=[page],set_default_timeout=lambda value:None,close=AsyncMock())
    launch=AsyncMock(return_value=context)
    playwright=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch),stop=AsyncMock())
    monkeypatch.setattr(async_api,'async_playwright',lambda:SimpleNamespace(start=AsyncMock(return_value=playwright)))
    create=AsyncMock(return_value=Process())
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    proxy=SimpleNamespace(close=lambda:None,wait_closed=AsyncMock())
    monkeypatch.setattr(asyncio,'start_server',AsyncMock(return_value=proxy))
    state.start_vnc=AsyncMock()
    try:
        await state.ensure()
        state.start_vnc.assert_not_awaited()
        assert create.await_args.args[0]=='Xvfb' and create.await_count==1
        assert state.snapshot()['display_running'] is False
        assert state.snapshot()['browser_start_seconds']>=0
        assert launch.await_args.kwargs['chromium_sandbox'] is True
    finally:
        # The test's private folder is pytest-owned, not the production /tmp.
        state.private_dir=None
        await state.close()


@pytest.mark.parametrize('role',['browser','sandbox'])
def test_fresh_brokers_do_not_import_httpx(role):
    script=f"import sys,json; from personal_bot import {role}; print(json.dumps('httpx' in sys.modules))"
    result=subprocess.run([sys.executable,'-c',script],check=True,capture_output=True,text=True,timeout=20)
    assert json.loads(result.stdout) is False
