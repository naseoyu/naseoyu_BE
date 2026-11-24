from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Path as APIPath, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from . import __version__, repository as repo
from .config import Settings
from . import contracts as output
from .db import Database, ScenarioHead, ScenarioVersion, Run, EventRecord, ReportRecord, Preset
from .domain import ScenarioCreate, VersionCreate, AnalysisCreate, Report
from .agent import load_agent
from .worker import consume

Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]


class BodyLimit:
    def __init__(self, app, limit=2*1024*1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            msg = await receive()
            if msg['type'] == 'http.disconnect':
                return
            size += len(msg.get('body', b''))
            if size > self.limit:
                return await JSONResponse({'detail': '요청 본문은 2MiB 이하로 보내세요'}, status_code=413)(scope, receive, send)
            messages.append(msg)
            if not msg.get('more_body', False):
                break
        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay, send)


def version_payload(row):
    return {'scenario_id': row.scenario_id, 'version': row.version, 'scenario': row.payload,
            'sha256': row.sha256, 'parent_run_id': row.parent_run_id}


def run_payload(run, include_result=False):
    data = {**repo.created(run), 'stage': run.stage, 'attempt': run.attempt, 'error': run.error,
            'created_at': run.created_at, 'completed_at': run.completed_at, 'adapter_revision': run.adapter_revision}
    if include_result:
        data['result'] = run.result
    return data


def completed_result(database, run_id):
    with database.session() as db:
        run = repo.require(db, Run, run_id, '분석')
        if run.state != 'completed' or run.result is None:
            raise HTTPException(409, '분석이 아직 완료되지 않았습니다')
        return run.result


def event_batch(database, run_id, cursor):
    with database.session() as db:
        run = repo.require(db, Run, run_id, '분석')
        # Retried runs do not replay a prior attempt's terminal event or report drafts.
        attempt = run.attempt + (1 if run.state == 'queued' and run.attempt else 0)
        rows = db.scalars(select(EventRecord).where(EventRecord.run_id==run_id, EventRecord.id>cursor,
                         EventRecord.attempt>=attempt).order_by(EventRecord.id).limit(100)).all()
        return [(e.id, e.kind, {'attempt': e.attempt, **e.payload}) for e in rows], run.state


def create_app(settings=None, database=None):
    settings = settings or Settings()
    database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(database.check_schema)
        task = asyncio.create_task(consume(database, settings, load_agent(settings))) if settings.inline_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            database.engine.dispose()

    app = FastAPI(title='DataCenter BE2 API', version=__version__, lifespan=lifespan,
                  description='BE 2: API · DB · 비동기 작업 · SSE. 기본 Agent는 비용을 계산하지 않는 모의 구현입니다.')
    app.state.database, app.state.settings = database, settings
    from .uploads import router as upload_router
    app.include_router(upload_router(database))
    app.add_middleware(BodyLimit)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(',') if x.strip()],
                           allow_methods=['GET','POST'], allow_headers=['Content-Type','Idempotency-Key','Last-Event-ID'])

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, exc):
        return JSONResponse({'detail': [{'location': list(e['loc']), 'message': e['msg'], 'type': e['type']} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(ValueError)
    async def bad_domain(_request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=422)

    @app.get('/health', tags=['system'])
    def health():
        database.check_schema()
        return {'status': 'ok', 'version': __version__, 'inline_worker': settings.inline_worker}

    @app.get('/v1/presets', tags=['presets'], response_model=list[output.PresetOut])
    def presets():
        with database.session() as db:
            rows = db.scalars(select(Preset).order_by(Preset.id)).all()
            return [{'id':r.id,'revision':r.revision,'scenario':r.payload} for r in rows]

    @app.get('/v1/presets/{preset_id}', tags=['presets'], response_model=output.PresetOut)
    def preset(preset_id:str):
        with database.session() as db:
            r = repo.require(db,Preset,preset_id,'Preset')
            return {'id':r.id,'revision':r.revision,'scenario':r.payload}

    @app.post('/v1/scenarios', status_code=201, tags=['scenarios'], response_model=output.Version)
    def create_scenario(body: ScenarioCreate):
        with database.session() as db:
            row = repo.store_version(db, body.scenario)
            db.commit()
            return version_payload(row)

    @app.get('/v1/scenarios', tags=['scenarios'], response_model=list[output.ScenarioSummary])
    def list_scenarios(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=2147483647)):
        with database.session() as db:
            rows = db.scalars(select(ScenarioHead).order_by(ScenarioHead.created_at.desc(), ScenarioHead.id).offset(offset).limit(limit)).all()
            return [{'id': r.id, 'name': r.name, 'latest_version': r.latest_version, 'created_at': r.created_at} for r in rows]

    @app.get('/v1/scenarios/{scenario_id}', tags=['scenarios'], response_model=output.Version)
    def get_scenario(scenario_id: str):
        with database.session() as db:
            head = repo.require(db, ScenarioHead, scenario_id, '시나리오')
            return version_payload(repo.require(db, ScenarioVersion, (scenario_id, head.latest_version), '버전'))

    @app.get('/v1/scenarios/{scenario_id}/versions', tags=['scenarios'], response_model=list[output.VersionSummary])
    def list_versions(scenario_id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=2147483647)):
        with database.session() as db:
            repo.require(db, ScenarioHead, scenario_id, '시나리오')
            rows = db.scalars(select(ScenarioVersion).where(ScenarioVersion.scenario_id==scenario_id)
                              .order_by(ScenarioVersion.version.desc()).offset(offset).limit(limit)).all()
            return [{'version': r.version, 'sha256': r.sha256, 'created_at': r.created_at} for r in rows]

    @app.get('/v1/scenarios/{scenario_id}/versions/{version}', tags=['scenarios'], response_model=output.Version)
    def get_version(scenario_id: str, version: int = APIPath(ge=1, le=2147483647)):
        with database.session() as db:
            return version_payload(repo.require(db, ScenarioVersion, (scenario_id, version), '시나리오 버전'))

    @app.post('/v1/scenarios/{scenario_id}/versions', status_code=201, tags=['scenarios'], response_model=output.Version)
    def create_version(scenario_id: str, body: VersionCreate):
        with database.session() as db:
            row = repo.store_version(db, body.scenario, scenario_id, body.expected_version)
            db.commit()
            return version_payload(row)

    @app.post('/v1/analyses', status_code=202, tags=['analyses'], response_model=output.RunCreated)
    def submit_analysis(body: AnalysisCreate, idempotency_key: Key):
        return repo.submit(database, body.scenario_id, body.version, idempotency_key, settings.agent_revision)

    @app.get('/v1/analyses', tags=['analyses'], response_model=list[output.RunSummary])
    def list_analyses(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=2147483647)):
        with database.session() as db:
            rows = db.scalars(select(Run).order_by(Run.created_at.desc(), Run.id).offset(offset).limit(limit)).all()
            return [run_payload(r) for r in rows]

    @app.get('/v1/analyses/{run_id}', tags=['analyses'], response_model=output.RunDetail)
    def get_analysis(run_id: str):
        with database.session() as db:
            return run_payload(repo.require(db, Run, run_id, '분석'), include_result=True)

    @app.get('/v1/analyses/{run_id}/report', tags=['analyses'], response_model=Report, response_model_exclude_unset=True)
    def get_report(run_id: str):
        completed_result(database, run_id)
        with database.session() as db:
            return repo.require(db, ReportRecord, (run_id,1), '리포트').payload

    @app.get('/v1/analyses/{run_id}/export', tags=['analyses'])
    def export(run_id: str):
        result = completed_result(database, run_id)
        with database.session() as db:
            run = repo.require(db, Run, run_id, '분석')
            report = repo.require(db, ReportRecord, (run_id,1), '리포트')
            # Download and report use the same persisted result, never a recalculation.
            return JSONResponse({'run': run_payload(run), 'scenario_snapshot': run.snapshot, 'result': result, 'report': report.payload},
                                headers={'Content-Disposition': f'attachment; filename="analysis-{run.id}.json"'})

    @app.post('/v1/analyses/{run_id}/cancel', tags=['analyses'])
    def cancel(run_id: str):
        return repo.cancel(database, run_id)

    @app.post('/v1/analyses/{run_id}/retry', status_code=202, tags=['analyses'], response_model=output.RunCreated)
    def retry(run_id: str):
        return repo.retry(database, run_id, settings)

    @app.get('/v1/analyses/{run_id}/events', tags=['stream'], response_class=StreamingResponse,
             responses={200: {'content': {'text/event-stream': {'schema': {'type': 'string'}}},
                              'description': 'Durable events with id, event and JSON data fields'}})
    async def events(run_id: str, request: Request, last_event_id: Annotated[str | None, Header(alias='Last-Event-ID')] = None,
                     after: int = Query(0, ge=0, le=9223372036854775807)):
        cursor = after
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
                if not 0 <= cursor <= 9223372036854775807:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, 'Last-Event-ID는 유효한 이벤트 정수 ID여야 합니다') from None
        def validate_cursor():
            with database.session() as db:
                repo.require(db, Run, run_id, '분석')
                if cursor:
                    event = db.get(EventRecord, cursor)
                    if event is None or event.run_id != run_id:
                        raise HTTPException(400, '이 분석에 속하지 않는 이벤트 커서입니다')
        await asyncio.to_thread(validate_cursor)

        async def stream():
            nonlocal cursor
            heartbeat = asyncio.get_running_loop().time()
            yield 'retry: 1000\n\n'
            while not await request.is_disconnected():
                rows, state = await asyncio.to_thread(event_batch, database, run_id, cursor)
                for eid, kind, payload in rows:
                    cursor = eid
                    yield f'id: {eid}\nevent: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                if state in repo.TERMINAL and len(rows) < 100:
                    break
                now = asyncio.get_running_loop().time()
                if now-heartbeat >= 10:
                    yield ': keep-alive\n\n'
                    heartbeat = now
                await asyncio.sleep(settings.poll_seconds)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get('/', include_in_schema=False)
    def root():
        from fastapi.responses import RedirectResponse
        return RedirectResponse('/docs')
    return app
