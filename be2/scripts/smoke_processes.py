"""Exercise separate API + worker processes with a disposable local database."""
import json
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--csv', action='store_true', help='Exercise CSV upload instead of a preset')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp)
        env={**os.environ, 'BE2_DATABASE_URL':'sqlite:///'+(root/'smoke.db').as_posix(), 'BE2_INLINE_WORKER':'false',
             'BE2_POLL_SECONDS':'0.05'}
        processes=[]
        with (root/'api.log').open('w',encoding='utf-8') as api_log, (root/'worker.log').open('w',encoding='utf-8') as worker_log:
            try:
                api=subprocess.Popen([sys.executable,'-m','be2_backend.cli','serve','--port','0'],env=env,stdout=api_log,stderr=subprocess.STDOUT)
                processes.append(api)
                url=None;deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    match=re.search(r'DataCenter BE2 API: (http://127.0.0.1:\d+)',(root/'api.log').read_text(encoding='utf-8'))
                    if match:
                        url=match.group(1)
                        try:
                            if httpx.get(url+'/health',timeout=1,trust_env=False).status_code==200:break
                        except httpx.HTTPError:pass
                    if api.poll() is not None:raise RuntimeError((root/'api.log').read_text(encoding='utf-8'))
                    time.sleep(.05)
                else:raise RuntimeError('API startup timed out')
                with httpx.Client(base_url=url,timeout=10,trust_env=False) as c:
                    assert not c.get('/health').json()['inline_worker']
                    if args.csv:
                        uploaded=c.post('/v1/uploads',files={'file':('costs.csv','항목,금액\n전력료,0\n보험료,\n'.encode(),'text/csv')})
                        uploaded.raise_for_status()
                        v=c.post('/v1/uploads/'+uploaded.json()['id']+'/scenarios',json={'name':'CSV 프로세스 테스트'}).json()
                        scenario=v['scenario']
                    else:
                        scenario=c.get('/v1/presets').json()[0]['scenario']
                        v=c.post('/v1/scenarios',json={'scenario':scenario}).json()
                    run=c.post('/v1/analyses',json={'scenario_id':v['scenario_id'],'version':v['version']},headers={'Idempotency-Key':'process-smoke'}).json()
                    rid=run['id'];assert c.get('/v1/analyses/'+rid).json()['state']=='queued'
                    worker=subprocess.Popen([sys.executable,'-m','be2_backend.worker'],env=env,stdout=worker_log,stderr=subprocess.STDOUT)
                    processes.append(worker)
                    deadline=time.monotonic()+15
                    while time.monotonic()<deadline:
                        result=c.get('/v1/analyses/'+rid).json()
                        if result['state'] in ('completed','failed'):break
                        time.sleep(.05)
                    assert result['state']=='completed', result
                    stream=c.get('/v1/analyses/'+rid+'/events').text
                    assert 'event: completed' in stream and 'event: report_block' in stream
                    exported=c.get('/v1/analyses/'+rid+'/export').json()
                    assert exported['result']==result['result']
                    assert exported['scenario_snapshot']==scenario
                    assert c.get('/docs').status_code==200
                    print(json.dumps({'status':'passed','mode':'separate_api_worker','input':'csv' if args.csv else 'preset','is_mock':result['result']['is_mock'],
                                      'metrics':result['result']['metrics'],
                                      'report_title':exported['report']['title'],'sse_completed':True},ensure_ascii=False))
            finally:
                for p in reversed(processes):
                    if p.poll() is None:p.terminate()
                for p in reversed(processes):
                    try:p.wait(timeout=5)
                    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)


if __name__=='__main__':main()
