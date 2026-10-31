"""Opt-in, no-model native executor benchmark. Run as API UID 31001.

Creates only synthetic immutable fixtures, invokes the real peer-authenticated
broker and removes only its own UUID fixtures. Peak RSS is sampled from the
fixed Executor UID's public /proc status fields; it is a sampled lower bound,
not an exact process accounting peak. Run only in a quiet runtime.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import statistics
import time
import uuid

from personal_bot.sandbox import execute


def child_peak_rss_kib() -> int | None:
    peak=None
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            values={line.split(':',1)[0]:line.split(':',1)[1].split()
                    for line in (entry/'status').read_text().splitlines() if ':' in line}
            if values.get('Uid',[''])[0]!='31004':
                continue
            if 'VmHWM' in values:
                peak=max(peak or 0,int(values['VmHWM'][0]))
        except (OSError,ValueError):
            continue
    return peak


async def main() -> None:
    assert os.uname().machine=='x86_64' and os.getuid()==31001
    fixtures=[]
    metrics={}
    try:
        for suffix,data in [('txt',('Synthetic benchmark row 123 中文\n'*4096).encode()),
                            ('csv',b'name,value\n'+b'alpha,17\n'*8192)]:
            identity=str(uuid.uuid4())
            path=Path('/data/files')/identity
            with path.open('xb') as output:
                output.write(data)
            path.chmod(0o600)
            fixtures.append(path)
            item={'id':identity,'path':str(path),'name':f'benchmark.{suffix}',
                  'media_type':'text/plain' if suffix=='txt' else 'text/csv'}
            runs=[]
            for _ in range(5):
                started=time.perf_counter()
                pending=asyncio.create_task(execute(action='parse',input_files=[item],
                    output_dir='/scratch/outputs',limits={'wall_seconds':20,'cpu_seconds':10,
                    'address_space_bytes':512*1024**2},task_id=str(uuid.uuid4()),run_id=str(uuid.uuid4())))
                peak=None
                while not pending.done():
                    observed=child_peak_rss_kib()
                    if observed is not None:
                        peak=max(peak or 0,observed)
                    await asyncio.sleep(0.005)
                result=await pending
                elapsed=time.perf_counter()-started
                assert result['status']=='succeeded'
                assert result['security']['uid']==31004 and result['security']['seccomp'] is True
                assert result['security']['landlock_abi']>=3
                assert len(result['documents'])==1 and result['documents'][0]['text']==data.decode()
                assert result.get('artifacts',[])==[]
                runs.append({'seconds':elapsed,'sampled_child_peak_rss_kib':peak})
            metrics[suffix]={'input_bytes':len(data),'runs':runs,
                'median_seconds':statistics.median(run['seconds'] for run in runs),
                'maximum_seconds':max(run['seconds'] for run in runs)}
        print(json.dumps({'native':True,'model_calls':0,'runs_per_type':5,
            'peak_measurement':'sampled_VmHWM_lower_bound','metrics':metrics}))
    finally:
        for path in fixtures:
            path.unlink(missing_ok=True)


if __name__=='__main__':
    asyncio.run(main())
