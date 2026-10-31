"""Opt-in real Worker/LangGraph memory fixture; model replies are simulated.

No owner database, real model endpoint or billing approval is used. PSS is for
this fixture process, not the deployed Worker. GC at each measurement makes
retained-input comparison conservative for the previous implementation.
"""
import asyncio
import gc
import json
import os
from pathlib import Path
import weakref

from personal_bot import worker


class Done(BaseException):
    pass


class Input(dict):
    pass


def pss():
    return int(next(line.split()[1] for line in Path('/proc/self/smaps_rollup').read_text().splitlines()
                    if line.startswith('Pss:')))


async def main():
    assert os.environ.get('PERSONAL_BOT_ACCEPTANCE_PROBE') == '1'
    baseline = pss()
    samples = []
    inputs = []

    class Client:
        async def aclose(self):
            pass

    class IPC:
        def __init__(self):
            self.client = Client()
            self.count = 0

        async def call(self, path, body):
            if path == 'claim':
                gc.collect()
                if self.count:
                    samples.append({'completed_tasks': self.count, 'pss_kib': pss(),
                                    'retained_inputs': sum(reference() is not None for reference in inputs)})
                if self.count == 5:
                    raise Done
                self.count += 1
                task = Input(id=f'fixture-{self.count}', attempt_id='fixture', runtime_id='perf', revision=1,
                             context_generation=1, files=[], completed_tools=[],
                             messages=[{'role': 'user', 'content': 'x' * 500000}])
                inputs.append(weakref.ref(task))
                return {'task': task}
            if path == 'checkpoints/get':
                return {'checkpoint': None}
            if path in {'checkpoints/put', 'checkpoints/writes', 'finish', 'heartbeat'}:
                return {}
            if path == 'model':
                return {'text': 'done', 'raw_output': {'role': 'assistant', 'content': 'done'},
                        'tool_calls': [], 'model_call_id': 'fixture'}
            raise AssertionError(path)

    worker.IPC = IPC
    os.environ['PERSONAL_BOT_RUNTIME_ID'] = 'perf'
    try:
        await worker.worker_main()
    except Done:
        pass
    print(json.dumps({'native': True, 'model_http': 'simulated', 'provider_calls': 0,
                      'fixture_process_cold_pss_kib': baseline, 'samples': samples}))


if __name__ == '__main__':
    asyncio.run(main())
