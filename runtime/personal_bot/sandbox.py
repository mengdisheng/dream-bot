"""API client and fixed-identity executor broker; never an unrestricted fallback."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import uuid
import zipfile

SOCKET = os.environ.get('PERSONAL_BOT_EXECUTOR_SOCKET', '/run/personal-bot/executor.sock')
ROOT = Path('/scratch/jobs')
OUTPUTS = Path('/scratch/outputs')
FILES = Path(os.environ.get('PERSONAL_BOT_FILES_ROOT', '/data/files'))


async def execute(*, action: str, code: str | None = None, input_files: list[dict],
                  output_dir: str, limits: dict, task_id: str, run_id: str) -> dict:
    import httpx
    transport = httpx.AsyncHTTPTransport(uds=SOCKET)
    async with httpx.AsyncClient(transport=transport, timeout=155) as client:
        response = await client.post('http://executor/execute', json=dict(action=action,code=code,input_files=input_files,
                                         output_dir=output_dir,limits=limits,task_id=task_id,run_id=run_id))
        response.raise_for_status()
        return response.json()


def safe_read_file(path: str) -> tuple[int, os.stat_result]:
    target = Path(path)
    relative = target.relative_to(FILES)
    if len(relative.parts) != 1 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', relative.name):
        raise ValueError('Invalid immutable input path')
    rootfd = os.open(FILES, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rootfd)
    finally:
        os.close(rootfd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > 25*1024**2 or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('Input must be one bounded regular immutable file')
    return fd, info


def check_archive(path: Path) -> None:
    if not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        if len(items) > 10000 or sum(i.file_size for i in items) > 100*1024**2:
            raise ValueError('Archive expansion limit exceeded')
        if any(i.file_size > 1024*1024 and i.file_size > max(i.compress_size,1)*1000 for i in items):
            raise ValueError('Archive compression ratio rejected')


async def run_job(request: dict) -> dict:
    if request.get('action') not in {'parse','python','execute','selftest'}:
        raise ValueError('Unsupported action')
    if len(request.get('code') or '') > 65536 or len(request.get('input_files',[])) > 20:
        raise ValueError('Request limit exceeded')
    # Caller names never become directory paths; every execution gets a fresh ID.
    run = uuid.uuid4().hex
    job = ROOT / run
    destination = OUTPUTS / run
    ROOT.mkdir(parents=True,exist_ok=True)
    OUTPUTS.mkdir(parents=True,exist_ok=True)
    job.mkdir(mode=0o700)
    os.chown(job,31004,31004)
    for name in ['inputs','outputs','tmp']:
        p=job/name
        p.mkdir(mode=0o700)
        os.chown(p,31004,31004)
    process = None
    exported = False
    try:
        staged=[]
        for item in request.get('input_files',[]):
            file_id=str(item['id'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',file_id):
                raise ValueError('Invalid input ID')
            fd,info=safe_read_file(item['path'])
            target=job/'inputs'/file_id
            with os.fdopen(fd,'rb') as source, target.open('xb') as sink:
                shutil.copyfileobj(source,sink,65536)
            check_archive(target)
            target.chmod(0o400)
            os.chown(target,31004,31004)
            staged.append({'id':file_id,'name':str(item['name'])[:255],'media_type':item.get('media_type','application/octet-stream'),'path':str(target)})
        request=dict(request,input_files=staged)
        manifest=job/'request.json'
        manifest.write_text(json.dumps(request),encoding='utf-8')
        manifest.chmod(0o400)
        os.chown(manifest,31004,31004)
        environment={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(job/'tmp'),'TMPDIR':str(job/'tmp'),
                     'LANG':'C.UTF-8','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1',
                     'NUMEXPR_NUM_THREADS':'1','MPLBACKEND':'Agg','MPLCONFIGDIR':str(job/'tmp')}
        process=await asyncio.create_subprocess_exec(sys.executable,'-I',str(Path(__file__).with_name('sandbox_child.py')),str(job),
                   user=31004,group=31004,extra_groups=[],env=environment,close_fds=True,start_new_session=True,
                   stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        wall=min(int(request.get('limits',{}).get('wall_seconds',120)),120)
        try:
            _,stderr=await asyncio.wait_for(process.communicate(),wall)
        except (TimeoutError,asyncio.CancelledError):
            if process.returncode is None:
                os.killpg(process.pid,signal.SIGKILL)
                await process.wait()
            if asyncio.current_task().cancelling():
                raise
            return {'status':'failed','stdout':'','stderr':'','artifacts':[], 'error':{'code':'execution_timeout','message':'受限执行超时，进程已停止'}}
        result_path=job/'result.json'
        if process.returncode != 0 or not result_path.is_file() or result_path.is_symlink():
            return {'status':'failed','stdout':'','stderr':'','artifacts':[], 'error':{'code':'sandbox_unavailable','message':'受限执行器未完成安全启动或被资源限额终止'}}
        if result_path.stat().st_size > 24*1024**2:
            raise ValueError('Result limit exceeded')
        result=json.loads(result_path.read_text())
        if not result.get('security',{}).get('seccomp'):
            raise RuntimeError('Isolation proof missing')
        # The process is reaped before walking outputs. Never resolve-then-copy.
        destination.mkdir(mode=0o700)
        artifacts=[]
        total=0
        sourcefd=os.open(job/'outputs',os.O_DIRECTORY|os.O_NOFOLLOW)
        destinationfd=os.open(destination,os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            for name in os.listdir(sourcefd):
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=sourcefd)
                with os.fdopen(fd,'rb') as source:
                    info=os.fstat(source.fileno())
                    total+=info.st_size
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or total > 200*1024**2 or len(artifacts)>=100:
                        raise ValueError('Unsafe or oversized artifact')
                    out=destination/uuid.uuid4().hex
                    # Same tmpfs, reaped single executor, private job directory:
                    # rename keeps a 200-MiB artifact from needing two copies.
                    os.rename(name,out.name,src_dir_fd=sourcefd,dst_dir_fd=destinationfd)
                    moved=os.stat(out.name,dir_fd=destinationfd,follow_symlinks=False)
                    if (moved.st_dev,moved.st_ino)!=(info.st_dev,info.st_ino):
                        raise ValueError('Artifact changed during export')
                    os.fchown(source.fileno(),0,31000)
                    os.fchmod(source.fileno(),0o600)
                    os.fchown(source.fileno(),31001,31000)
                    import mimetypes
                    artifacts.append({'path':str(out),'name':name,'media_type':mimetypes.guess_type(name)[0] or 'application/octet-stream'})
        finally:
            os.close(sourcefd)
            os.close(destinationfd)
        if artifacts:
            os.chown(destination,31001,31000)
        else:
            destination.rmdir()
        result['artifacts']=artifacts
        exported=True
        return result
    finally:
        if process is not None and process.returncode is None:
            os.killpg(process.pid,signal.SIGKILL)
            await process.wait()
        if job.parent == ROOT and re.fullmatch(r'[0-9a-f]{32}',job.name):
            shutil.rmtree(job)
        if not exported and destination.parent == OUTPUTS and re.fullmatch(r'[0-9a-f]{32}',destination.name):
            shutil.rmtree(destination,ignore_errors=True)


def create_app():
    # This fixed local broker needs neither model/schema generation nor
    # Pydantic. Keep those dependencies in the API process that owns them.
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    lock=asyncio.Lock()

    async def http_error(request, exc):
        return JSONResponse({'detail':exc.detail},status_code=exc.status_code,headers=exc.headers)

    async def dispatch(request):
        # Preserve the existing dictionary-body 422 contract without importing
        # the API framework into this otherwise small resident process.
        try:
            raw=await request.body()
            content_type=request.headers.get('content-type','').split(';',1)[0].strip().lower()
            if not raw:
                body=None
            elif content_type=='application/json' or (
                    content_type.startswith('application/') and content_type.endswith('+json')):
                body=json.loads(raw)
            else:
                body=raw.decode('utf-8')
        except json.JSONDecodeError as exc:
            return JSONResponse({'detail':[{'type':'json_invalid','loc':['body',exc.pos],
                'msg':'JSON decode error','input':{},'ctx':{'error':exc.msg}}]},status_code=422)
        except UnicodeDecodeError:
            return JSONResponse({'detail':'There was an error parsing the body'},status_code=400)
        if not raw:
            return JSONResponse({'detail':[{'type':'missing','loc':['body'],
                'msg':'Field required','input':None}]},status_code=422)
        if not isinstance(body,dict):
            return JSONResponse({'detail':[{'type':'dict_type','loc':['body'],
                'msg':'Input should be a valid dictionary','input':body}]},status_code=422)
        async with lock:
            try:
                return JSONResponse(await run_job(body))
            except (ValueError,OSError,RuntimeError) as exc:
                return JSONResponse({'detail':{'code':'sandbox_rejected','message':str(exc)[:200]}},status_code=422)

    return Starlette(routes=[Route('/execute',dispatch,methods=['POST'])],
                     exception_handlers={HTTPException:http_error})


def main() -> None:
    from .broker_support import serve

    serve(create_app(),SOCKET)


if __name__=='__main__':
    main()
