"""Durable DB queue consumer. Each write is fenced by the current lease owner."""
from __future__ import annotations

import asyncio
import logging
import time
from sqlalchemy import update

from . import repository as repo
from .config import Settings
from .db import Database, Run
from .domain import Scenario, AgentResult, Progress, AgentMessage
from .utils import digest
from .agent import load_agent
import json

log = logging.getLogger(__name__)


def load_snapshot(database, run_id, owner):
    with database.session() as db:
        run = repo.fence(db, run_id, owner)
        payload, expected = run.snapshot, run.snapshot_hash
        db.commit()
    if digest(payload) != expected:
        raise ValueError('시나리오 스냅샷 무결성 불일치')
    return Scenario.model_validate(payload)


def renew(database, settings, run_id, owner):
    now = time.time()
    with database.session() as db:
        n = db.execute(update(Run).where(Run.id==run_id, Run.state=='running', Run.owner==owner, Run.lease_until>now)
                       .values(lease_until=now+settings.lease_seconds)).rowcount
        db.commit()
        if not n:
            raise repo.LeaseLost()


def release(database, run_id, owner):
    with database.session() as db:
        db.execute(update(Run).where(Run.id==run_id, Run.state=='running', Run.owner==owner)
                   .values(state='queued', owner=None, lease_until=None))
        db.commit()


async def execute(database, settings, run_id, owner, agent):
    event_count = 0
    async def emit(kind, payload):
        nonlocal event_count
        event_count += 1
        if event_count > 1000:
            raise ValueError('Agent event limit exceeded')
        schema = {'progress': Progress, 'agent_message': AgentMessage}.get(kind)
        if schema is None:
            raise ValueError('Agent cannot emit lifecycle or unregistered events')
        data = schema.model_validate(payload).model_dump(mode='json')
        await asyncio.to_thread(repo.emit, database, run_id, owner, kind, data)
    scenario = await asyncio.to_thread(load_snapshot, database, run_id, owner)
    input_schema = scenario.inputs.get('input_schema')
    if input_schema == 'be2.csv.raw.v1' and input_schema not in getattr(agent, 'supported_input_schemas', ()):
        # Raw CSV is not a normalized cost input. Adapters must opt in explicitly.
        raise ValueError('Agent does not support raw CSV; mapping and domain validation are required')
    raw = await agent.analyze(scenario, emit)
    result = AgentResult.model_validate(raw)
    payload = result.model_dump(mode='json')
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 2*1024*1024:
        raise ValueError('Agent result exceeds 2MiB')
    for block in result.report.blocks:
        await asyncio.to_thread(repo.emit, database, run_id, owner, 'report_block', block.model_dump(mode='json'))
    await asyncio.to_thread(repo.finish, database, run_id, owner, payload)


async def process(database, settings, job, agent=None):
    agent = agent or load_agent(settings)
    run_id, owner = job
    async def heartbeat():
        while True:
            await asyncio.sleep(settings.lease_seconds/3)
            await asyncio.to_thread(renew, database, settings, run_id, owner)
    execution = asyncio.create_task(execute(database, settings, run_id, owner, agent))
    lease = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(settings.job_timeout_seconds):
            done, _ = await asyncio.wait([execution, lease], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except repo.LeaseLost:
        log.info('Run stopped after lease loss: %s', run_id)
    except asyncio.CancelledError:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await asyncio.to_thread(release, database, run_id, owner)
        raise
    except Exception as exc:
        log.error('Analysis failed: %s (%s)', run_id, type(exc).__name__)
        message = '분석 제한 시간을 초과했습니다' if isinstance(exc, TimeoutError) else '분석 실행 오류가 발생했습니다'
        try:
            await asyncio.to_thread(repo.fail, database, run_id, owner, message)
        except repo.LeaseLost:
            pass
    finally:
        execution.cancel()
        lease.cancel()
        await asyncio.gather(execution, lease, return_exceptions=True)


async def consume(database, settings, agent=None):
    agent = agent or load_agent(settings)
    while True:
        try:
            job = await asyncio.to_thread(repo.claim, database, settings)
            if job is not None:
                await process(database, settings, job, agent)
                continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('Worker polling failed (%s)', type(exc).__name__)
        await asyncio.sleep(settings.poll_seconds)


def main():
    settings = Settings()
    db = Database(settings.database_url)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s')
    try:
        db.check_schema()
        asyncio.run(consume(db, settings))
    except KeyboardInterrupt:
        pass
    finally:
        db.engine.dispose()


if __name__ == '__main__':
    main()
