import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select,update,func
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata

from be2_backend.api import create_app
from be2_backend.config import Settings
from be2_backend.db import Database,Base,Run,EventRecord,ReportRecord,Preset
from be2_backend.domain import Scenario,AgentResult
from be2_backend.agent import MockAgent,load_agent
from be2_backend import repository as repo
from be2_backend.worker import process,renew
from be2_backend.cli import bind_local

@pytest.fixture
def db(tmp_path):
    d=Database('sqlite:///'+str(tmp_path/'be2.db'));d.migrate()
    yield d
    d.engine.dispose()
@pytest.fixture
def settings(db):
    return Settings(database_url=db.url,inline_worker=False,poll_seconds=.02,lease_seconds=2,_env_file=None)
@pytest.fixture
def client(db,settings):
    with TestClient(create_app(settings,db)) as c:yield c
@pytest.fixture
def scenario():
    return Scenario(name='테스트 입력',inputs={'gpu_count':4,'tags':['dev'],'custom':{'x':1}})

def saved(db,scenario):
    with db.session() as session:
        v=repo.store_version(session,scenario);session.commit();return v.scenario_id

def queue(db,settings,scenario,key='request'):
    return repo.submit(db,saved(db,scenario),1,key,settings.agent_revision)['id']

def complete(db,settings,agent=None):
    job=repo.claim(db,settings);assert job
    asyncio.run(process(db,settings,job,agent))
    return job[0]

def test_presets_are_persisted_and_seed_does_not_overwrite(db,client):
    assert len(client.get('/v1/presets').json())==3
    with db.session() as session:
        p=session.get(Preset,'small');p.payload={'name':'edited','inputs':{}};session.commit()
    db.migrate()
    assert client.get('/v1/presets/small').json()['scenario']['name']=='edited'
    assert client.get('/v1/presets/missing').status_code==404

def test_scenario_versions_conflict_and_preserve_snapshot(db,client,scenario):
    r=client.post('/v1/scenarios',json={'scenario':scenario.model_dump()});assert r.status_code==201
    sid=r.json()['scenario_id']
    changed={'scenario':{'name':'v2','inputs':{'gpu_count':8}},'expected_version':1}
    assert client.post(f'/v1/scenarios/{sid}/versions',json=changed).status_code==201
    assert client.post(f'/v1/scenarios/{sid}/versions',json=changed).status_code==409
    assert client.get(f'/v1/scenarios/{sid}/versions/1').json()['scenario']==scenario.model_dump()
    assert client.get(f'/v1/scenarios/{sid}').json()['version']==2
    assert len(client.get(f'/v1/scenarios/{sid}/versions').json())==2

def test_idempotency_conflict_and_concurrent_single_creation(db,settings,scenario):
    sid=saved(db,scenario)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(lambda _:repo.submit(db,sid,1,'same',settings.agent_revision)['id'],range(8)))
    assert len(set(ids))==1
    with pytest.raises(HTTPException) as exc:repo.submit(db,sid,2,'same',settings.agent_revision)
    assert exc.value.status_code==409
    with db.session() as session:assert session.scalar(select(func.count()).select_from(Run))==1

def test_end_to_end_mock_report_and_export(client,db,settings,scenario):
    rid=queue(db,settings,scenario)
    assert client.get(f'/v1/analyses/{rid}/report').status_code==409
    complete(db,settings)
    result=client.get(f'/v1/analyses/{rid}').json()
    assert result['state']=='completed'
    assert result['result']['is_mock'] is True
    assert result['result']['metrics']['monthly_cost_krw'] is None
    report=client.get(f'/v1/analyses/{rid}/report').json()
    export=client.get(f'/v1/analyses/{rid}/export').json()
    assert report==export['report']==result['result']['report']
    assert export['result']==result['result']
    assert export['scenario_snapshot']==scenario.model_dump()

def test_sse_replay_resume_and_final_close(client,db,settings,scenario):
    rid=queue(db,settings,scenario);complete(db,settings)
    r=client.get(f'/v1/analyses/{rid}/events');assert r.status_code==200
    assert r.headers['content-type'].startswith('text/event-stream')
    assert 'event: report_block' in r.text and 'event: completed' in r.text
    ids=[int(l[4:]) for l in r.text.splitlines() if l.startswith('id: ')]
    assert ids==sorted(set(ids))
    tail=client.get(f'/v1/analyses/{rid}/events',headers={'Last-Event-ID':str(ids[-2])}).text
    assert tail.count('\nid: ')==1 and 'event: completed' in tail
    assert '\nid: ' not in client.get(f'/v1/analyses/{rid}/events?after={ids[-1]}').text

def test_sse_rejects_cross_run_and_invalid_cursor(client,db,settings,scenario):
    a=queue(db,settings,scenario,'a');complete(db,settings)
    b=queue(db,settings,scenario,'b');complete(db,settings)
    with db.session() as session:e=session.scalar(select(EventRecord).where(EventRecord.run_id==a))
    assert client.get(f'/v1/analyses/{b}/events?after={e.id}').status_code==400
    assert client.get(f'/v1/analyses/{b}/events',headers={'Last-Event-ID':'bad'}).status_code==400
    assert client.get('/v1/analyses/missing/events').status_code==404

def test_cancel_retry_terminal_rules(client,db,settings,scenario):
    rid=queue(db,settings,scenario)
    assert client.post(f'/v1/analyses/{rid}/cancel').json()['state']=='cancelled'
    assert client.post(f'/v1/analyses/{rid}/cancel').status_code==200
    assert client.post(f'/v1/analyses/{rid}/retry').status_code==202
    complete(db,settings)
    assert 'event: failed' not in client.get(f'/v1/analyses/{rid}/events').text
    assert client.post(f'/v1/analyses/{rid}/cancel').status_code==409
    assert client.post(f'/v1/analyses/{rid}/retry').status_code==409

def test_concurrent_claim_only_one_owner(db,settings,scenario):
    queue(db,settings,scenario)
    with ThreadPoolExecutor(max_workers=4) as pool:jobs=list(pool.map(lambda _:repo.claim(db,settings),range(4)))
    assert len([j for j in jobs if j])==1

def test_expired_lease_recovery_and_stale_writes(client,db,settings,scenario):
    rid=queue(db,settings,scenario);first=repo.claim(db,settings)
    repo.emit(db,*first,'agent_message',{'text':'old draft'})
    with db.session() as session:
        session.execute(update(Run).where(Run.id==rid).values(lease_until=time.time()-1));session.commit()
    second=repo.claim(db,settings);assert second[1]!=first[1]
    with pytest.raises(repo.LeaseLost):renew(db,settings,*first)
    with pytest.raises(repo.LeaseLost):repo.emit(db,*first,'agent_message',{'text':'late'})
    asyncio.run(process(db,settings,second))
    stream=client.get(f'/v1/analyses/{rid}/events').text
    assert 'event: reset' in stream and 'old draft' not in stream
    with db.session() as session:
        result=session.get(Run,rid).result
    with pytest.raises(repo.LeaseLost):repo.finish(db,*first,result)

def test_running_cancel_cannot_save_late_result(db,settings,scenario):
    async def run():
        entered=asyncio.Event();resume=asyncio.Event()
        class Slow(MockAgent):
            async def analyze(self,s,emit):
                entered.set();await resume.wait();return await super().analyze(s,emit)
        rid=queue(db,settings,scenario)
        job=repo.claim(db,settings);task=asyncio.create_task(process(db,settings,job,Slow()))
        await entered.wait();await asyncio.to_thread(repo.cancel,db,rid);resume.set();await task
        with db.session() as session:
            r=session.get(Run,rid);assert r.state=='cancelled' and r.result is None
            assert session.get(ReportRecord,(rid,1)) is None
    asyncio.run(run())

@pytest.mark.parametrize('mode',['raise','invalid_result','spoof_event','too_many_events','timeout'])
def test_agent_failures_are_persisted(db,settings,scenario,mode):
    class Broken:
        async def analyze(self,s,emit):
            if mode=='raise':raise RuntimeError('SECRET provider message')
            if mode=='invalid_result':return {'made_up':True}
            if mode=='spoof_event':await emit('completed',{'id':'fake'})
            if mode=='too_many_events':
                for _ in range(1001):await emit('agent_message',{'text':'draft'})
            if mode=='timeout':await asyncio.sleep(10)
    settings.job_timeout_seconds=1 if mode=='timeout' else 20
    rid=queue(db,settings,scenario);complete(db,settings,Broken())
    with db.session() as session:
        r=session.get(Run,rid);assert r.state=='failed' and r.result is None
        assert 'SECRET' not in r.error
        assert session.get(ReportRecord,(rid,1)) is None

def test_shutdown_requeues_owned_job(db,settings,scenario):
    async def run():
        entered=asyncio.Event()
        class Blocked:
            async def analyze(self,s,emit):entered.set();await asyncio.sleep(100)
        rid=queue(db,settings,scenario);job=repo.claim(db,settings)
        task=asyncio.create_task(process(db,settings,job,Blocked()))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        with db.session() as session:assert session.get(Run,rid).state=='queued'
    asyncio.run(run())

def test_attempt_limit_and_adapter_revision(db,settings,scenario):
    rid=queue(db,settings,scenario)
    with db.session() as session:
        session.execute(update(Run).where(Run.id==rid).values(adapter_revision='other-v1'));session.commit()
    assert repo.claim(db,settings) is None
    with pytest.raises(HTTPException):repo.retry(db,rid,settings)
    rid2=queue(db,settings,scenario,'second')
    with db.session() as session:
        session.execute(update(Run).where(Run.id==rid2).values(attempt=settings.max_attempts));session.commit()
    assert repo.claim(db,settings) is None
    with pytest.raises(HTTPException):repo.retry(db,rid2,settings)

def test_snapshot_integrity(db,settings,scenario):
    rid=queue(db,settings,scenario)
    with db.session() as session:
        session.execute(update(Run).where(Run.id==rid).values(snapshot={'name':'changed','inputs':{}}));session.commit()
    complete(db,settings)
    with db.session() as session:assert session.get(Run,rid).state=='failed'

def test_input_validation_and_body_limit(client):
    assert client.post('/v1/scenarios',json={'scenario':{'name':'','inputs':{}}}).status_code==422
    assert client.post('/v1/scenarios',content='x'*(2*1024*1024+1)).status_code==413
    assert client.post('/v1/analyses',json={'scenario_id':'missing','version':1}).status_code==422
    assert client.get('/v1/scenarios?limit=999').status_code==422

def test_migration_matches_and_pg_ddl_compiles(db):
    db.migrate()
    with db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    for table in Base.metadata.sorted_tables:
        assert 'CREATE TABLE' in str(CreateTable(table).compile(dialect=postgresql.dialect()))

def test_bad_plugin_fails_early(settings):
    settings.agent_factory='invalid'
    with pytest.raises(ValueError):load_agent(settings)

def test_inline_worker_and_docs(db,settings,scenario):
    settings.inline_worker=True
    with TestClient(create_app(settings,db)) as c:
        assert c.get('/').status_code==200
        sid=c.post('/v1/scenarios',json={'scenario':scenario.model_dump()}).json()['scenario_id']
        r=c.post('/v1/analyses',headers={'Idempotency-Key':'inline'},json={'scenario_id':sid,'version':1})
        assert r.status_code==202
        rid=r.json()['id'];deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=c.get(f'/v1/analyses/{rid}').json()['state']
            if state in ['completed','failed']:break
            time.sleep(.02)
        assert state=='completed'
        assert 'AgentResult' in c.get('/openapi.json').json()['components']['schemas']

def test_port_fallback():
    import socket
    with socket.socket() as busy:
        busy.bind(('127.0.0.1',0));busy.listen()
        actual=bind_local(busy.getsockname()[1])
        try:assert actual.getsockname()[1]!=busy.getsockname()[1]
        finally:actual.close()
