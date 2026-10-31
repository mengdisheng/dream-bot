"""Browser broker compatibility and dependency footprint; no real browser/model."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from personal_bot import browser as browser_module
from personal_bot import broker_support


@pytest.fixture
def broker(monkeypatch):
    state=browser_module.Browser()
    state.ensure=AsyncMock()
    state.action=AsyncMock(side_effect=lambda body: body)
    state.takeover=AsyncMock(side_effect=lambda body: body)
    captured={}
    monkeypatch.setattr(browser_module,'Browser',lambda: state)
    monkeypatch.setattr(broker_support,'serve',lambda app,path: captured.update(app=app,path=path))
    browser_module.main()
    assert captured['path']==browser_module.SOCKET
    return state,captured['app']


def test_fresh_browser_role_does_not_load_fastapi_or_pydantic():
    # Other runtime roles and pytest's shared fixtures need those libraries.
    # Check a clean process so their imports cannot conceal broker regressions.
    script="""
import asyncio,json,sys
import httpx
from personal_bot import browser,broker_support
captured={}
broker_support.serve=lambda app,path: captured.update(app=app)
browser.main()
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=captured['app']),base_url='http://browser') as client:
        response=await client.get('/status')
        assert response.status_code==200 and response.json()['running'] is False
asyncio.run(run())
print(json.dumps([name for name in sys.modules if name=='fastapi' or name.startswith('fastapi.') or name=='pydantic' or name.startswith(('pydantic.','pydantic_core'))]))
"""
    completed=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=30,check=True)
    assert json.loads(completed.stdout)==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,content_type',[
    (b'',None),(b'', 'application/json'),(b'null','application/json'),
    (b'false','application/json'),(b'0','application/json'),(b'[]','application/json'),
    (b'"text"','application/json'),(b'{','application/json'),(b'  ','application/json'),
    (b'{}',None),(b'{}','text/plain'),(b'{}','application/json; charset=utf-8'),
    (b'{}','application/vnd.personal-bot+json'),
    ('{"nested":{"value":["中文",3,true]}}'.encode(),'application/json'),
    (b'\xff','application/json'),(b'\xff','text/plain'),
])
async def test_json_body_and_422_details_match_previous_framework(broker,raw,content_type):
    state,app=broker
    previous=FastAPI()

    @previous.post('/perform')
    async def previous_action(body: dict):
        return body

    headers={} if content_type is None else {'content-type':content_type}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=previous,raise_app_exceptions=False),base_url='http://previous') as reference:
        expected=await reference.post('/perform',content=raw,headers=headers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://browser') as client:
        actual=await client.post('/perform',content=raw,headers=headers)
    assert actual.status_code==expected.status_code
    assert actual.content==expected.content
    if actual.status_code==200:
        state.action.assert_awaited_once_with(json.loads(raw))
    else:
        state.action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['/control','/perform','/revoke-session','/view-sessions'])
async def test_all_object_routes_reject_nonobjects_before_effects(broker,path):
    state,app=broker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        response=await client.post(path,json=[])
    assert response.status_code==422
    assert response.json()=={'detail':[{'type':'dict_type','loc':['body'],'msg':'Input should be a valid dictionary','input':[]}]}
    state.ensure.assert_not_awaited()
    state.action.assert_not_awaited()
    state.takeover.assert_not_awaited()
    assert state.sessions=={} and state.epoch==1


@pytest.mark.asyncio
async def test_conflict_envelopes_and_action_trace_redaction_are_preserved(broker):
    state,app=broker
    state.takeover.side_effect=ValueError('Browser control revision changed')
    state.action.side_effect=RuntimeError('x'*250+'\nprivate browser trace')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        control=await client.post('/control',json={'action':'takeover'})
        action=await client.post('/perform',json={'action':{'type':'observe'}})
    assert control.status_code==409 and control.json()=={'detail':'Browser control revision changed'}
    assert action.status_code==409 and action.json()=={'detail':{'code':'browser_action_failed','message':'x'*200}}


@pytest.mark.asyncio
async def test_status_does_not_start_browser_and_quiesce_ignores_body(broker):
    state,app=broker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        status=await client.get('/status')
        assert status.status_code==200 and status.json()['running'] is False
        assert state.epoch==1
        quiesce=await client.post('/quiesce',content=b'{',headers={'content-type':'application/json'})
    assert quiesce.status_code==200 and quiesce.json()['stopped'] is True
    assert quiesce.json()['browser_epoch']==2
    state.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_error_envelopes_and_get_only_status_are_preserved(broker):
    _,app=broker
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://browser') as client:
        missing=await client.get('/missing')
        wrong_method=await client.get('/control')
        head=await client.head('/status')
        options=await client.options('/status')
    assert missing.status_code==404 and missing.json()=={'detail':'Not Found'}
    assert wrong_method.status_code==405 and wrong_method.json()=={'detail':'Method Not Allowed'}
    assert wrong_method.headers['allow']=='POST'
    assert head.status_code==405 and head.headers['allow']=='GET' and head.content==b''
    assert options.status_code==405 and options.json()=={'detail':'Method Not Allowed'}


@pytest.mark.asyncio
async def test_lifespan_reaps_expiry_task_and_closes_browser(broker):
    state,app=broker
    entered=asyncio.Event()
    cancelled=asyncio.Event()

    async def expire():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    state.expire=expire
    state.close=AsyncMock()
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(entered.wait(),1)
        state.close.assert_not_awaited()
    assert cancelled.is_set()
    state.close.assert_awaited_once()


@pytest.mark.parametrize('change',[
    {'used':True},{'expires':0},{'epoch':0},{'owner_id':'other-owner'},{'session_id':'other-session'},
])
def test_viewer_route_rejects_replay_expiry_epoch_and_owner_session_mismatch(broker,change):
    state,app=broker
    state.sessions['ticket']={'used':False,'expires':time.monotonic()+60,'epoch':state.epoch,
                              'owner_id':'owner','session_id':'session','device_id':'device',**change}
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect('/view/ticket',headers={'x-owner-id':'owner','x-session-id':'session'}):
                pytest.fail('A rejected viewer must not be accepted')
        assert denied.value.code==1008
        assert state.viewers=={}
    state.ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_peer_denial_precedes_broker_http_and_websocket_routes(broker):
    state,app=broker
    protected=broker_support.APIPeerOnly(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=protected),base_url='http://browser') as client:
        denied=await client.post('/control',json={'action':'takeover'},headers={'x-api-uid':'31001'})
    assert denied.status_code==403 and denied.json()=={'error':'peer_denied'}
    events=[]

    async def send(message):
        events.append(message)

    await protected({'type':'websocket','personal_bot.peercred':{'uid':31002}},None,send)
    assert events==[{'type':'websocket.close','code':1008}]
    state.takeover.assert_not_awaited()


def test_viewer_stream_forwards_frames_and_blocks_readonly_input(broker,monkeypatch):
    state,app=broker
    state.start_vnc=AsyncMock()
    frames=[]

    class Remote:
        def write(self,data):
            frames.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(path):
        reader=asyncio.StreamReader()
        reader.feed_data(b'real-rfb-frame')
        return reader,Remote()

    monkeypatch.setattr(asyncio,'open_unix_connection',connect,raising=False)
    state.vnc_socket=Path('/private/vnc.sock')
    with TestClient(app) as client:
        ticket=client.post('/view-sessions',json={'device_id':'device','owner_id':'owner','session_id':'session'}).json()
        identity=ticket['view_session_id']
        with client.websocket_connect('/view/'+identity,headers={'x-owner-id':'owner','x-session-id':'session'}) as ws:
            assert ws.receive_bytes()==b'real-rfb-frame'
            ws.send_bytes(b'RFB 003.008\n\x01\x01'+b'\x04\x01\x00\x00\x00\x00\x00A')
            ws.close()

            async def wait_for_cleanup():
                async with asyncio.timeout(1):
                    while identity in state.sessions or identity in state.viewers:
                        await asyncio.sleep(.001)

            # Let the broker consume its disconnect before TestClient cancels
            # the ASGI task, matching a normally completed socket lifecycle.
            ws.portal.call(wait_for_cleanup)
        assert identity not in state.sessions and identity not in state.viewers
    assert b''.join(frames)==b'RFB 003.008\n\x01\x01'
    assert state.last_input==0


@pytest.fixture
async def real_browser_page():
    if os.environ.get('PERSONAL_BOT_BROWSER_FIXTURE')!='1':
        pytest.skip('Opt-in offline real Chromium DOM equivalence fixture')
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        options={'headless':True,'chromium_sandbox':True,'args':['--disable-extensions','--disable-background-networking']}
        executable=os.environ.get('PERSONAL_BOT_TEST_CHROMIUM_EXECUTABLE')
        if executable:
            options['executable_path']=executable
        chromium=await playwright.chromium.launch(**options)
        context=await chromium.new_context(viewport={'width':1280,'height':720},offline=True)
        page=await context.new_page()
        try:
            yield context,page
        finally:
            await context.close()
            await chromium.close()


async def previous_observation_fields(state):
    """Prior broker algorithm as a live Playwright compatibility oracle."""
    elements=[]
    identities={}
    locator=state.page.locator('a,button,input:not([type=password]),textarea,select,[role=button]')
    for index in range(min(await locator.count(),100)):
        node=locator.nth(index)
        if not await node.is_visible():
            continue
        ref=f'e{index}'
        identities[ref]=await state.element_identity(node)
        elements.append({'ref':ref,'tag':await node.evaluate('(e)=>e.tagName.toLowerCase()'),
                         'text':(await node.inner_text())[:160],'label':await node.get_attribute('aria-label'),
                         'type':await node.get_attribute('type')})
    return elements,identities


@pytest.mark.asyncio
async def test_real_dom_observation_equivalence_cap_and_approval_recheck(real_browser_page):
    context,page=real_browser_page
    await page.set_content('''<!doctype html><html><head><base href="https://fixture.example/path/"></head><body>
        <form id="form" action="submit" method="post">
          <input id="secret" type="password" value="fixture-only">
          <input name="query" aria-label="查询" type="text" value="value">
          <input type="file" name="upload"><textarea aria-label="备注">multiline\ntext</textarea>
          <select name="choice"><option>第一项</option><option>第二项</option></select>
          <button id="target" formaction="alternate" name="confirm">确认 <span style="display:none">hidden</span>提交</button>
          <a href="relative" aria-label="链接">一个\n链接</a>
          <button style="display:none">display hidden</button>
          <button style="visibility:hidden">visibility hidden</button>
          <button style="opacity:0">opacity remains visible</button>
          <button style="display:contents"><span>contents child</span></button>
          <button disabled aria-hidden="true">disabled visible</button>
        </form><button form="form" formaction="outside" name="outside">外部表单</button>
        <div role="button" aria-label="多行">line one<br>line two</div><div id="tail"></div>
        </body></html>''')
    await page.evaluate('''()=>{
        const tail=document.querySelector('#tail');
        for(let i=0;i<110;i++) {
            const button=document.createElement('button');
            button.id='tail-'+i;button.textContent=i===0?'长'.repeat(150)+'🙂'.repeat(20):'tail '+i;
            if(i%7===0) button.style.display='none';
            tail.appendChild(button);
        }
        window.fixtureClicked=false;
        document.querySelector('#target').onclick=event=>{event.preventDefault();window.fixtureClicked=true;};
    }''')
    state=browser_module.Browser()
    state.context=context
    state.page=page
    state.identity_salt=b'offline-fixture-salt'
    state.mode='agent'
    state.task='fixture-task'
    started=time.perf_counter()
    expected,identities=await previous_observation_fields(state)
    original_seconds=time.perf_counter()-started
    started=time.perf_counter()
    observed=await state.observe()
    optimized_seconds=time.perf_counter()-started
    assert observed['elements']==expected
    assert {ref:bound[1] for ref,bound in state.refs.items()}==identities
    assert all(int(item['ref'][1:])<100 for item in observed['elements'])
    assert len(observed['elements'])<100
    assert await page.locator('button').count()>100
    assert not any(identity['type']=='password' for identity in identities.values())
    # The retained nth locator and complete identity still detect a changed form
    # before an approved mutation can reach the page.
    target_ref=next(ref for ref,bound in state.refs.items() if bound[1]['name']=='confirm')
    target=state.refs[target_ref][0]
    assert await target.get_attribute('id')=='target'
    await page.eval_on_selector('#target',"e=>e.setAttribute('formaction','changed')")
    action={'type':'click','ref':target_ref,'browser_epoch':state.epoch,
            'observation_id':observed['observation_id'],'origin':observed['origin'],
            'account_fingerprint':observed['account_fingerprint']}
    approval={'status':'approved','task_id':state.task,'action':{'arguments':{'action':action}}}
    with pytest.raises(ValueError,match='Target element or form changed'):
        await state.action({'action':action,'task_id':state.task,'attempt_id':'fixture-attempt','approval':approval})
    assert await page.evaluate('window.fixtureClicked') is False
    print(json.dumps({'fixture':'offline_dom_equivalence','reference_ms':round(original_seconds*1000,3),
                      'optimized_ms':round(optimized_seconds*1000,3),'visible_elements':len(expected)}))


@pytest.mark.asyncio
async def test_real_non_html_target_keeps_playwright_inner_text_refusal(real_browser_page):
    context,page=real_browser_page
    await page.set_content('<svg width="200" height="80"><a role="button"><text x="10" y="20">SVG target</text></a></svg>')
    state=browser_module.Browser()
    state.context=context
    state.page=page
    state.identity_salt=b'offline-fixture-salt'
    assert await page.locator('[role=button]').is_visible()
    with pytest.raises(Exception,match='Node is not an HTMLElement') as previous:
        await previous_observation_fields(state)
    with pytest.raises(Exception,match='Node is not an HTMLElement') as optimized:
        await state.observe()
    assert str(optimized.value).split('\n')[0]==str(previous.value).split('\n')[0]


@pytest.mark.asyncio
async def test_page_defined_dom_getters_cannot_change_isolated_display_fields(real_browser_page):
    context,page=real_browser_page
    await page.set_content('<button id="target" type="button" aria-label="real-label">real-text</button>')
    await page.eval_on_selector('#target','''element=>{
        Object.defineProperty(element,'innerText',{get:()=> 'page-defined-text'});
        const original=element.getAttribute.bind(element);
        element.getAttribute=name=>name==='aria-label'?'page-defined-label':name==='type'?'page-defined-type':original(name);
    }''')
    state=browser_module.Browser()
    state.context=context
    state.page=page
    state.identity_salt=b'offline-fixture-salt'
    expected,identities=await previous_observation_fields(state)
    observed=await state.observe()
    assert expected==[{'ref':'e0','tag':'button','text':'real-text','label':'real-label','type':'button'}]
    assert observed['elements']==expected
    assert {ref:bound[1] for ref,bound in state.refs.items()}==identities
    # Identity deliberately retains its original main-world implementation;
    # the separate owner-approval identity check must use the same semantics.
    assert identities['e0']['text']=='page-defined-text' and identities['e0']['type']=='page-defined-type'
