"""Executor startup costs must fall without changing its IPC contract."""
import asyncio
import json
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from personal_bot import sandbox, sandbox_child


def legacy_broker():
    """The previous route is a reference for body/error compatibility."""
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

    @app.post('/execute')
    async def dispatch(request: dict):
        try:
            return await sandbox.run_job(request)
        except (ValueError,OSError,RuntimeError) as exc:
            raise HTTPException(422,detail={'code':'sandbox_rejected','message':str(exc)[:200]}) from exc

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('content,headers',[
    (b'',{}),
    (b'null',{'content-type':'application/json'}),
    (b'[]',{'content-type':'application/json'}),
    (b'42',{'content-type':'application/json'}),
    (b'true',{'content-type':'application/json'}),
    (b'"text"',{'content-type':'application/json'}),
    (b'{broken',{'content-type':'application/json'}),
    (b'\xff',{'content-type':'application/json'}),
    (b'action=selftest',{'content-type':'application/x-www-form-urlencoded'}),
    (b'{}',{'content-type':'text/plain'}),
    (b'{}',{'content-type':'application/vnd.test+json'}),
    (b'{"action":"parse"}',{}),
])
async def test_broker_body_validation_matches_previous_framework(monkeypatch,content,headers):
    async def run(request):
        return {'status':'succeeded','request':request}
    monkeypatch.setattr(sandbox,'run_job',run)
    responses=[]
    for app in (legacy_broker(),sandbox.create_app()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://executor') as client:
            response=await client.post('/execute',content=content,headers=headers)
            responses.append((response.status_code,response.json()))
    assert responses[0]==responses[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('exception',[ValueError,OSError,RuntimeError])
async def test_broker_rejections_keep_bounded_error_shape(monkeypatch,exception):
    async def run(request):
        raise exception('x'*250)
    monkeypatch.setattr(sandbox,'run_job',run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox.create_app()),base_url='http://executor') as client:
        response=await client.post('/execute',json={'action':'parse'})
        assert response.status_code==422
        assert response.json()=={'detail':{'code':'sandbox_rejected','message':'x'*200}}
        assert (await client.get('/execute')).json()=={'detail':'Method Not Allowed'}
        assert (await client.get('/missing')).json()=={'detail':'Not Found'}


@pytest.mark.asyncio
async def test_broker_still_serializes_jobs(monkeypatch):
    active=0
    maximum=0

    async def run(request):
        nonlocal active,maximum
        active+=1
        maximum=max(active,maximum)
        await asyncio.sleep(0.01)
        active-=1
        return {'status':'succeeded'}

    monkeypatch.setattr(sandbox,'run_job',run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox.create_app()),base_url='http://executor') as client:
        responses=await asyncio.gather(*(client.post('/execute',json={'action':'selftest'}) for _ in range(4)))
    assert all(response.status_code==200 for response in responses)
    assert maximum==1


def test_broker_does_not_load_api_schema_dependencies():
    result=subprocess.run([sys.executable,'-c',
        "import json,sys; from personal_bot.sandbox import create_app; create_app(); "
        "print(json.dumps([name for name in ('fastapi','pydantic','pypdf','docx','openpyxl','reportlab','PIL') if name in sys.modules]))"],
        check=True,capture_output=True,text=True)
    assert json.loads(result.stdout)==[]


@pytest.mark.parametrize('action,names,expected',[
    ('parse',['plain.txt','table.csv','notes.md','data.json'],set()),
    ('parse',['ONE.PDF'],{'pypdf'}),
    ('parse',['one.docx'],{'docx'}),
    ('parse',['one.xlsx'],{'openpyxl'}),
    ('parse',['one.pdf','two.PDF','three.xlsx','four.docx'],{'pypdf','docx','openpyxl'}),
    ('parse',['unknown.py','fake.pypdf','name.pdf.exe'],set()),
    ('selftest',[],set()),
    ('python',[],{'pypdf','docx','openpyxl','reportlab','PIL.Image'}),
    ('execute',[],{'pypdf','docx','openpyxl','reportlab','PIL.Image'}),
])
def test_preloading_uses_fixed_action_and_extension_allowlist(monkeypatch,action,names,expected):
    imported=[]

    def load(name):
        imported.append(name)
        return name

    monkeypatch.setattr(sandbox_child.importlib,'import_module',load)
    dependencies=sandbox_child.preload_dependencies({'action':action,'input_files':[{'name':name} for name in names]})
    assert set(imported)==expected
    assert len(imported)==len(expected)
    assert set(dependencies)==expected


def test_plain_text_child_preload_has_no_document_modules():
    # A fresh process checks real imports, independently of the test runner's
    # already loaded libraries. Linux isolation is verified by the native probe.
    result=subprocess.run([sys.executable,'-c',
        "import json,sys; from personal_bot.sandbox_child import preload_dependencies; "
        "preload_dependencies({'action':'parse','input_files':[{'name':'data.csv'}]}); "
        "print(json.dumps([name for name in ('pypdf','docx','openpyxl','reportlab','PIL') if name in sys.modules]))"],
        check=True,capture_output=True,text=True)
    assert json.loads(result.stdout)==[]
