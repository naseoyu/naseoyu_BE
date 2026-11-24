import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from be2_backend.api import create_app
from be2_backend.config import Settings
from be2_backend.db import Database, UploadRecord, Run, ScenarioVersion
from be2_backend import repository as repo
from be2_backend.domain import Scenario
from be2_backend.worker import process
from be2_backend.uploads import MAX_BYTES


@pytest.fixture
def setup(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'test.db'))
    db.migrate()
    settings = Settings(database_url=db.url, inline_worker=False, _env_file=None)
    with TestClient(create_app(settings, db)) as client:
        yield client, db, settings


def upload(client, content, name='costs.csv', **kwargs):
    return client.post('/v1/uploads', files={'file': (name, content, 'text/csv')}, **kwargs)


def test_raw_values_preview_and_storage_are_preserved(setup):
    client, db, _ = setup
    raw = '\ufeff항목,금액,메모\r\n전력료,0,"  원문 공백  "\r\n보험료,,"첫 줄\n둘째 줄"\r\n'.encode()
    response = upload(client, raw, '../../costs.csv')
    assert response.status_code == 201
    data = response.json()
    assert data['filename'] == 'costs.csv'
    assert data['preview_rows'] == [['전력료', '0', '  원문 공백  '], ['보험료', '', '첫 줄\n둘째 줄']]
    assert data['domain_validation'] == 'pending'
    assert data['sha256'] == hashlib.sha256(raw).hexdigest()
    assert client.get('/v1/uploads/' + data['id']).json() == data
    listing = client.get('/v1/uploads').json()
    assert len(listing) == 1 and 'preview_rows' not in listing[0] and 'raw' not in listing[0]
    with db.session() as session:
        saved = session.get(UploadRecord, data['id'])
        assert saved.raw == raw
        assert saved.rows == data['preview_rows']


def test_cp949_is_explicit_and_unsupported_encoding_rejected(setup):
    client, _, _ = setup
    raw = '항목,금액\n전력료,123\n'.encode('cp949')
    assert upload(client, raw).json()['detail']['code'] == 'invalid_encoding'
    assert upload(client, raw, data={'encoding': 'cp949'}).json()['preview_rows'] == [['전력료', '123']]
    assert upload(client, raw, data={'encoding': 'latin-1'}).status_code == 422


@pytest.mark.parametrize('content,code', [
    (b'', 'empty_file'),
    (b'\n', 'empty_header'),
    (b'a,b\n', 'missing_rows'),
    (b'a,A\n1,2\n', 'duplicate_header'),
    (b'a, b\n1,2\n', 'invalid_header'),
    (b'a,\n1,2\n', 'invalid_header'),
    (b'a,b\n1\n', 'column_count'),
    (b'a,b\n1,2,3\n', 'column_count'),
    (b'a,b\n\n', 'column_count'),
    (b'a,b\n1,"unterminated', 'invalid_csv'),
    (b'a,b\n1,\0\n', 'control_character'),
    (b'a\n' + b'x' * 10001, 'cell_too_long'),
    (b'a\n' + b'x\n' * 5001, 'too_many_cells'),
    ((','.join('c' + str(i) for i in range(101)) + '\n').encode(), 'too_many_columns'),
])
def test_bad_csv_is_rejected_without_partial_save(setup, content, code):
    client, db, _ = setup
    response = upload(client, content)
    assert response.status_code == 422
    assert response.json()['detail']['code'] == code
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(UploadRecord)) == 0


def test_limits_unsupported_formats_and_error_location(setup):
    client, _, _ = setup
    assert upload(client, b'x' * (MAX_BYTES + 1)).status_code == 413
    assert upload(client, b'x' * (2 * MAX_BYTES + 1)).status_code == 413
    for name in ['report.pdf', 'report.docx', 'costs.csv.exe']:
        assert upload(client, b'a\nx\n', name).status_code == 415
    error = upload(client, b'a,b\nx,y\nsecret\n').json()['detail']
    assert error['row'] == 3 and 'secret' not in json.dumps(error)
    assert client.get('/v1/uploads/missing').status_code == 404
    assert client.get('/v1/uploads?limit=201').status_code == 422


def test_multiple_files_and_unknown_fields_are_not_silently_dropped(setup):
    client, db, _ = setup
    for files in [
        [('file', ('one.csv', b'a\nfirst\n')), ('file', ('two.csv', b'a\nsecond\n'))],
        [('file', ('one.csv', b'a\nfirst\n')), ('other', ('two.csv', b'a\nsecond\n'))],
    ]:
        response = client.post('/v1/uploads', files=files)
        assert response.status_code == 422 and response.json()['detail']['code'] == 'invalid_form'
    assert upload(client, b'a\nx\n', data={'ignored': 'bad'}).status_code == 422
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(UploadRecord)) == 0


def test_csv_limits_and_unicode_headers(setup):
    client, _, _ = setup
    assert upload(client, '가,가\n1,2\n'.encode()).json()['detail']['code'] == 'duplicate_header'
    raw = (','.join(f'c{i}' for i in range(100)) + '\n' + (','.join('x' for _ in range(100)) + '\n') * 1001).encode()
    assert upload(client, raw).json()['detail']['code'] == 'too_many_cells'
    assert upload(client, b'a\n' + b'x\n' * 5000).status_code == 201
    # Logical record number is stable when a previous record spans physical lines.
    error = upload(client, b'a,b\n"line1\nline2",2\n"bad').json()['detail']
    assert error['row'] == 3


def test_scenario_name_trim_does_not_modify_input_values(setup):
    client, _, _ = setup
    result = client.post('/v1/scenarios', json={'scenario': {'name': ' name ', 'inputs': {'x': '  value  '}}})
    assert result.status_code == 201
    assert result.json()['scenario'] == {'name': 'name', 'inputs': {'x': '  value  '}}
    assert client.post('/v1/scenarios', json={'scenario': {'name': '  ', 'inputs': {}}}).status_code == 422


def test_preview_is_bounded_and_full_snapshot_reaches_worker(setup):
    client, db, settings = setup
    raw = ('item,amount\n' + '\n'.join(f' item {i} ,{i}' for i in range(25))).encode()
    data = upload(client, raw).json()
    assert data['row_count'] == 25 and len(data['preview_rows']) == 20
    version = client.post('/v1/uploads/' + data['id'] + '/scenarios', json={'name': 'CSV 시험'}).json()
    assert len(version['scenario']['inputs']['rows']) == 25
    assert version['scenario']['inputs']['rows'][0][0] == ' item 0 '
    request = {'scenario_id': version['scenario_id'], 'version': version['version']}
    run = client.post('/v1/analyses', json=request, headers={'Idempotency-Key': 'csv-test'}).json()
    assert client.post('/v1/analyses', json=request, headers={'Idempotency-Key': 'csv-test'}).json()['id'] == run['id']
    job = repo.claim(db, settings)
    asyncio.run(process(db, settings, job))
    result = client.get('/v1/analyses/' + run['id']).json()
    assert result['state'] == 'completed' and result['result']['is_mock']
    exported = client.get('/v1/analyses/' + run['id'] + '/export').json()
    assert exported['scenario_snapshot'] == version['scenario']
    assert 'event: completed' in client.get('/v1/analyses/' + run['id'] + '/events').text


def test_raw_csv_cannot_silently_enter_unsupported_agent(setup):
    client, db, settings = setup
    data = upload(client, b'a\nx\n').json()
    version = client.post('/v1/uploads/' + data['id'] + '/scenarios', json={'name': 'raw'}).json()
    run = repo.submit(db, version['scenario_id'], 1, 'unsupported', settings.agent_revision)
    class LegacyAgent:
        called = False
        async def analyze(self, scenario, emit):
            self.called = True
            raise AssertionError('must not accept raw CSV')
    agent = LegacyAgent()
    asyncio.run(process(db, settings, repo.claim(db, settings), agent))
    assert not agent.called
    with db.session() as session:
        assert session.get(Run, run['id']).state == 'failed'


def test_upgrade_preserves_v1_scenarios(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'legacy.db'))
    cfg = Config()
    import be2_backend
    cfg.set_main_option('script_location', str(Path(be2_backend.__file__).parent / 'migrations'))
    with db.engine.begin() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'be2_0001')
    with db.session() as session:
        version = repo.store_version(session, Scenario(name='기존 데이터', inputs={'x': 1}))
        session.commit()
        key = (version.scenario_id, version.version)
    with pytest.raises(RuntimeError):
        db.check_schema()
    db.migrate()
    db.check_schema()
    with db.session() as session:
        assert session.get(ScenarioVersion, key).payload['inputs'] == {'x': 1}
    db.engine.dispose()
