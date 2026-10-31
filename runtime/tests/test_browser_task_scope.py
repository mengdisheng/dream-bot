"""Conversation/task binding regressions with actual broker HTTP/WS routes.

No Chromium, native process, model request or external network is started.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from personal_bot import browser as browser_module
from personal_bot import broker_support
from personal_bot.errors import BotError


@pytest.fixture
def scoped_browser(monkeypatch):
    state=browser_module.Browser()
    state.context=SimpleNamespace(close=AsyncMock())
    state.page=SimpleNamespace(url='https://example.com/')
    state.task='task-a'
    state.mode='agent'
    state.ensure=AsyncMock()
    state.start_vnc=AsyncMock()
    state.stop_vnc=AsyncMock()
    state.vnc_socket='/private/display.sock'
    state.test_remotes=[]

    async def observe(): return state.snapshot()
    state.observe=AsyncMock(side_effect=observe)

    class Remote:
        def __init__(self): self.frames=[]; self.closed=False
        def write(self,data): self.frames.append(data)
        async def drain(self): pass
        def close(self): self.closed=True
        async def wait_closed(self): pass

    async def connect(path):
        reader=asyncio.StreamReader()
        reader.feed_data(b'frame')
        remote=Remote()
        state.test_remotes.append(remote)
        return reader,remote

    monkeypatch.setattr(asyncio,'open_unix_connection',connect,raising=False)
    captured={}
    monkeypatch.setattr(browser_module,'Browser',lambda:state)
    monkeypatch.setattr(broker_support,'serve',lambda app,path:captured.update(app=app))
    browser_module.main()
    return state,captured['app']


def view_body(task='task-a'):
    return {'owner_id':'owner','device_id':'device','session_id':'login-session','expected_task_id':task}


async def settled(predicate):
    async with asyncio.timeout(1):
        while not predicate(): await asyncio.sleep(.002)


@pytest.mark.parametrize('expected',['task-b','',None,False])
def test_mismatched_viewer_does_not_initialize_browser(scoped_browser,expected):
    state,app=scoped_browser
    with TestClient(app) as client:
        response=client.post('/view-sessions',json=view_body(expected))
        assert response.status_code==409
        assert response.json()['detail']['code']=='browser_task_changed'
        assert not state.sessions
        state.ensure.assert_not_awaited()
        state.start_vnc.assert_not_awaited()


@pytest.mark.parametrize('action',['takeover','release','transfer'])
def test_delayed_control_cannot_target_a_different_task(scoped_browser,action):
    state,app=scoped_browser
    with TestClient(app) as client:
        response=client.post('/control',json={**view_body('task-b'),'action':action,'browser_epoch':state.epoch})
        assert response.status_code==409
        assert response.json()['detail']['code']=='browser_task_changed'
        assert state.task=='task-a' and state.mode=='agent' and state.epoch==1
        state.ensure.assert_not_awaited()
        state.stop_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_revokes_tickets_and_reaps_old_input_before_new_task_observes(scoped_browser):
    state,app=scoped_browser
    events=[]
    state.refs={'old':object()}
    state.observation='old-observation'
    state.observed_url=state.page.url
    state.device='old-device'
    state.owner_session='old-login'
    state.sessions={'unused':{'task_id':'task-a'}}

    class Viewer:
        async def close(self,*,code):
            assert code==1012 and state.lock.locked()
            assert state.task=='task-a' and state.sessions=={} and state.viewers=={}
            events.append('close-old-viewer')
    state.viewers={'old-viewer':Viewer()}

    async def stop():
        assert state.task=='task-a' and not state.sessions and not state.viewers
        events.append('stop-old-input-bridge')
    state.stop_vnc.side_effect=stop

    async def observe():
        assert state.task=='task-b' and state.mode=='agent'
        assert state.device is None and state.owner_session is None
        assert not state.refs and state.observation is None and state.observed_url is None
        events.append('observe-new-task')
        return state.snapshot()
    state.observe.side_effect=observe
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        response=await client.post('/perform',json={'task_id':'task-b','attempt_id':'attempt-b','action':{'type':'observe'}})
        assert response.status_code==200 and response.json()['task_id']=='task-b'
        assert response.json()['browser_epoch']==2
        repeated=await client.post('/perform',json={'task_id':'task-b','attempt_id':'attempt-b','action':{'type':'observe'}})
        assert repeated.status_code==200 and repeated.json()['browser_epoch']==2
    assert events==['close-old-viewer','stop-old-input-bridge','observe-new-task','observe-new-task']
    state.stop_vnc.assert_awaited_once()


def test_switch_disconnects_live_viewer_and_rejects_unused_ticket(scoped_browser):
    state,app=scoped_browser
    with TestClient(app) as client:
        opened=client.post('/view-sessions',json=view_body()).json()
        unused=client.post('/view-sessions',json=view_body()).json()
        assert opened['task_id']=='task-a'
        with client.websocket_connect(opened['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'login-session'}) as ws:
            assert ws.receive_bytes()==b'frame'
            remote=state.test_remotes[-1]
            response=client.post('/perform',json={'task_id':'task-b','attempt_id':'b','action':{'type':'observe'}})
            assert response.status_code==200 and response.json()['task_id']=='task-b'
            assert state.epoch==2 and not state.sessions and not state.viewers
            with pytest.raises(WebSocketDisconnect): ws.receive_bytes()
            ws.close()
            ws.portal.call(settled,lambda:remote.closed)
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(unused['ws_path'].removeprefix('/api/browser'),headers={'x-owner-id':'owner','x-session-id':'login-session'}):
                pytest.fail('old task ticket accepted')
        assert denied.value.code==1008
        later=client.post('/view-sessions',json=view_body()).json()
        assert later['detail']['code']=='browser_task_changed'
        replacement=client.post('/view-sessions',json=view_body('task-b')).json()
        assert replacement['task_id']=='task-b' and replacement['browser_epoch']==2
        state.start_vnc.assert_awaited_once()


def test_ticket_consumption_checks_task_even_when_epoch_is_unchanged(scoped_browser):
    state,app=scoped_browser
    state.sessions['old']={'used':False,'expires':time.monotonic()+60,'epoch':state.epoch,'task_id':'task-b',
                           'owner_id':'owner','session_id':'login-session','device_id':'device'}
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/view/old',headers={'x-owner-id':'owner','x-session-id':'login-session'}):
                pytest.fail('mismatched task ticket accepted')
        assert denied.value.code==1008
        state.start_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_is_rechecked_before_each_live_input_frame(scoped_browser):
    state,app=scoped_browser
    state.mode='human'
    state.device='device'
    state.owner_session='login-session'
    ready=asyncio.Event()
    received=asyncio.Queue()
    record={'used':False,'expires':time.monotonic()+60,'epoch':state.epoch,'task_id':'task-a',
            'owner_id':'owner','session_id':'login-session','device_id':'device'}
    state.sessions['ticket']=record

    class Viewer:
        path_params={'identity':'ticket'}
        headers={'x-owner-id':'owner','x-session-id':'login-session'}
        async def accept(self): pass
        async def receive_bytes(self): return await received.get()
        async def send_bytes(self,data): ready.set()
        async def close(self,**kwargs): pass

    endpoint=next(route.endpoint for route in app.routes if route.path=='/view/{identity}')
    running=asyncio.create_task(endpoint(Viewer()))
    await asyncio.wait_for(ready.wait(),1)
    remote=state.test_remotes[-1]
    # A mismatched association is independently checked even if another
    # regression were to forget the epoch change or ticket-map revocation.
    async with state.lock: state.task='task-b'
    key=b'\x04\x01\x00\x00\x00\x00\x00a'
    await received.put(b'RFB 003.008\n\x01\x01'+key)
    await asyncio.wait_for(running,1)
    assert remote.frames==[] and remote.closed
    assert not state.sessions and not state.viewers


@pytest.mark.asyncio
async def test_client_wrapper_preserves_scope_and_maps_actionable_conflict(monkeypatch):
    sent=[]
    real_client=httpx.AsyncClient
    async def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(409,json={'detail':{'code':'browser_task_changed','message':'Changed task'}})
    monkeypatch.setattr(browser_module,'SOCKET','/private/test.sock')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BotError) as caught:
        await browser_module.create_view_session('device','owner',session_id='login',expected_task_id='task-a')
    assert caught.value.code=='browser_task_changed' and caught.value.status==409
    assert sent[0]['expected_task_id']=='task-a' and sent[0]['session_id']=='login'


@pytest.mark.asyncio
async def test_close_clears_task_so_old_conversation_cannot_reopen_fresh_blank_browser(scoped_browser):
    state,app=scoped_browser
    async with state.lock: await state.close()
    assert state.task is None and state.mode=='idle' and state.context is None
    state.ensure.reset_mock()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        response=await client.post('/view-sessions',json=view_body())
        assert response.status_code==409
        state.ensure.assert_not_awaited()
