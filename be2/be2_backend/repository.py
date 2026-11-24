from __future__ import annotations

import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select, update, delete, or_, and_
from sqlalchemy.exc import IntegrityError

from .db import ScenarioHead, ScenarioVersion, Run, EventRecord, ReportRecord
from .domain import Scenario, AgentResult
from .utils import digest

TERMINAL = {'completed', 'failed', 'cancelled'}


class LeaseLost(Exception):
    pass


def require(db, model, key, label):
    row = db.get(model, key)
    if row is None:
        raise HTTPException(404, label+'이 없습니다')
    return row


def store_version(db, s: Scenario, scenario_id=None, expected_version=None, parent_run_id=None):
    if scenario_id is None:
        scenario_id, version = str(uuid.uuid4()), 1
        db.add(ScenarioHead(id=scenario_id, latest_version=1, name=s.name))
        db.flush()
    else:
        require(db, ScenarioHead, scenario_id, '시나리오')
        n = db.execute(update(ScenarioHead).where(ScenarioHead.id==scenario_id, ScenarioHead.latest_version==expected_version)
                       .values(latest_version=expected_version+1, name=s.name)).rowcount
        if not n:
            raise HTTPException(409, '다른 버전이 먼저 저장됐습니다. 최신 버전을 조회하세요')
        version = expected_version+1
    payload = s.model_dump(mode='json')
    row = ScenarioVersion(scenario_id=scenario_id, version=version, payload=payload, sha256=digest(payload), parent_run_id=parent_run_id)
    db.add(row)
    db.flush()
    return row


def event(db, run, kind, payload):
    db.add(EventRecord(run_id=run.id, attempt=run.attempt, kind=kind, payload=payload))


def idempotent(db, key, request_hash):
    old = db.scalar(select(Run).where(Run.request_key==key))
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, '같은 Idempotency-Key에 다른 요청을 보낼 수 없습니다')
        return old
    return None


def enqueue(db, version, key, request_hash, adapter_revision):
    run = Run(id=str(uuid.uuid4()), scenario_id=version.scenario_id, version=version.version,
              snapshot=version.payload, snapshot_hash=version.sha256, adapter_revision=adapter_revision,
              request_key=key, request_hash=request_hash, state='queued', attempt=0)
    db.add(run)
    db.flush()
    event(db, run, 'progress', {'stage': 'queued', 'message': '분석을 등록했습니다'})
    return run


def created(run):
    return {'id': run.id, 'state': run.state, 'scenario_id': run.scenario_id, 'version': run.version,
            'events_url': f'/v1/analyses/{run.id}/events'}


def submit(database, scenario_id, version_number, key, adapter_revision):
    request_hash = digest({'operation': 'analyze', 'scenario_id': scenario_id, 'version': version_number, 'adapter_revision': adapter_revision})
    with database.session() as db:
        old = idempotent(db, key, request_hash)
        if old:
            return created(old)
        version = require(db, ScenarioVersion, (scenario_id, version_number), '시나리오 버전')
        try:
            run = enqueue(db, version, key, request_hash, adapter_revision)
            db.commit()
            return created(run)
        except IntegrityError:
            db.rollback()
            old = idempotent(db, key, request_hash)
            if old:
                return created(old)
            raise


def claim(database, settings):
    now = time.time()
    with database.session() as db:
        eligible = or_(Run.state=='queued', and_(Run.state=='running', Run.lease_until < now))
        query = select(Run).where(eligible).order_by(Run.created_at, Run.id).limit(1)
        if db.bind.dialect.name == 'postgresql':
            query = query.with_for_update(skip_locked=True)
        run = db.scalar(query)
        if run is None:
            return None
        previous_attempt = run.attempt
        owner = str(uuid.uuid4())
        n = db.execute(update(Run).where(Run.id==run.id, eligible, Run.attempt==previous_attempt).values(
            state='running', owner=owner, attempt=previous_attempt+1, lease_until=now+settings.lease_seconds, error=None)).rowcount
        if not n:
            return None
        db.refresh(run)
        if run.attempt > settings.max_attempts or run.adapter_revision != settings.agent_revision:
            run.state, run.error, run.completed_at = 'failed', '실행 한도 또는 Agent 버전 확인이 필요합니다', now
            run.owner, run.lease_until = None, None
            event(db, run, 'failed', {'code': 'execution_policy', 'message': run.error})
            db.commit()
            return None
        if previous_attempt:
            event(db, run, 'reset', {'attempt': run.attempt, 'message': '이전 초안을 초기화하고 새 시도를 시작합니다'})
        event(db, run, 'progress', {'stage': 'validate', 'message': '시나리오를 검증합니다'})
        db.commit()
        return run.id, owner


def fence(db, run_id, owner):
    n = db.execute(update(Run).where(Run.id==run_id, Run.owner==owner, Run.state=='running', Run.lease_until > time.time())
                   .values(owner=owner)).rowcount
    if not n:
        raise LeaseLost()
    return db.get(Run, run_id)


def emit(database, run_id, owner, kind, payload):
    with database.session() as db:
        run = fence(db, run_id, owner)
        if kind == 'progress':
            run.stage = payload['stage']
        event(db, run, kind, payload)
        db.commit()


def finish(database, run_id, owner, result):
    payload = AgentResult.model_validate(result).model_dump(mode='json')
    with database.session() as db:
        run = fence(db, run_id, owner)
        db.execute(delete(ReportRecord).where(ReportRecord.run_id==run_id))
        db.add(ReportRecord(run_id=run_id, revision=1, payload=payload['report']))
        run.result = payload
        run.state, run.stage, run.completed_at = 'completed', 'completed', time.time()
        run.owner, run.lease_until = None, None
        event(db, run, 'completed', {'id': run_id, 'state': 'completed', 'is_mock': payload['is_mock']})
        db.commit()


def fail(database, run_id, owner, message='분석 실행에 실패했습니다. 오류 로그를 확인하고 재시도하세요'):
    with database.session() as db:
        run = fence(db, run_id, owner)
        run.state, run.error, run.completed_at = 'failed', message, time.time()
        run.owner, run.lease_until = None, None
        event(db, run, 'failed', {'code': 'analysis_failed', 'message': message})
        db.commit()


def cancel(database, run_id):
    with database.session() as db:
        run = require(db, Run, run_id, '분석')
        n = db.execute(update(Run).where(Run.id==run_id, Run.state.in_(['queued', 'running']))
                       .values(state='cancelled', owner=None, lease_until=None, completed_at=time.time())).rowcount
        db.refresh(run)
        if not n and run.state != 'cancelled':
            raise HTTPException(409, '완료된 작업은 취소할 수 없습니다')
        if n:
            event(db, run, 'failed', {'code': 'cancelled', 'message': '분석이 취소됐습니다'})
        db.commit()
        return {'state': run.state}


def retry(database, run_id, settings):
    with database.session() as db:
        run = require(db, Run, run_id, '분석')
        n = db.execute(update(Run).where(Run.id==run_id, Run.state.in_(['failed','cancelled']),
            Run.attempt < settings.max_attempts, Run.adapter_revision==settings.agent_revision)
            .values(state='queued', stage='validate', error=None, owner=None, lease_until=None, completed_at=None)).rowcount
        if not n:
            raise HTTPException(409, '재시도 가능한 상태·시도 한도·Agent 버전을 확인하세요')
        db.refresh(run)
        db.commit()
        return created(run)
