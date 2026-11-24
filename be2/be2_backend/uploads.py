"""CSV transport validation only. Preserve cell strings; never infer costs."""
from __future__ import annotations

import csv
import hashlib
import io
import unicodedata
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import defer

from .db import UploadRecord
from .domain import Model, Scenario
from . import contracts, repository as repo

MAX_BYTES = 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 100
MAX_CELLS = 100000
MAX_CELL_CHARS = 10000
INPUT_SCHEMA = 'be2.csv.raw.v1'
NOTICE = 'CSV 구조만 검증했습니다. 비용 컬럼·단위·통화·기간·누락값의 의미 검증은 미완료입니다.'
Encoding = Literal['utf-8-sig', 'cp949']


class UploadSummary(Model):
    id: str
    filename: str
    encoding: str
    size_bytes: int
    sha256: str
    row_count: int
    columns: list[str]
    created_at: float
    input_schema: Literal['be2.csv.raw.v1'] = INPUT_SCHEMA
    validation_scope: Literal['structure_only'] = 'structure_only'
    domain_validation: Literal['pending'] = 'pending'
    warnings: list[str] = Field(default_factory=lambda: [NOTICE])


class UploadPreview(UploadSummary):
    model_config = ConfigDict(str_strip_whitespace=False)
    preview_rows: list[list[str]]


class ScenarioFromUpload(Model):
    name: str = Field(min_length=1, max_length=100)


def invalid(code, message, *, row=None, column=None, status=422):
    # Do not echo cell contents: uploaded reports may contain private data.
    raise HTTPException(status, {'code': code, 'message': message, 'row': row, 'column': column})


def parse_csv(raw: bytes, encoding: str):
    if not raw:
        invalid('empty_file', '빈 파일입니다')
    if len(raw) > MAX_BYTES:
        invalid('file_too_large', 'CSV 파일은 1MiB 이하로 보내세요', status=413)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        invalid('invalid_encoding', '선택한 인코딩으로 읽을 수 없습니다. UTF-8 또는 CP949를 지정하세요')
    if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
        invalid('control_character', 'CSV에 허용되지 않는 제어문자가 있습니다')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    rows = []
    columns = []
    try:
        columns = next(reader, [])
        if not columns:
            invalid('empty_header', '첫 행에 컬럼명이 필요합니다', row=1)
        if len(columns) > MAX_COLUMNS:
            invalid('too_many_columns', '컬럼은 최대 100개입니다', row=1)
        seen = set()
        for index, value in enumerate(columns, start=1):
            if not value.strip() or value != value.strip() or len(value) > 100:
                invalid('invalid_header', '컬럼명은 앞뒤 공백 없이 1~100자로 지정하세요', row=1, column=index)
            normalized = unicodedata.normalize('NFC', value).casefold()
            if normalized in seen:
                invalid('duplicate_header', '중복 컬럼명입니다', row=1, column=index)
            seen.add(normalized)
        for row_index, values in enumerate(reader, start=2):
            if len(values) != len(columns):
                invalid('column_count', '헤더와 데이터의 컬럼 수가 다릅니다', row=row_index)
            if len(rows) >= MAX_ROWS or (len(rows) + 1) * len(columns) > MAX_CELLS:
                invalid('too_many_cells', '최대 5,000행·100,000셀까지 허용합니다', row=row_index)
            for col, value in enumerate(values, start=1):
                if len(value) > MAX_CELL_CHARS:
                    invalid('cell_too_long', '셀은 최대 10,000자입니다', row=row_index, column=col)
            rows.append(values)
    except csv.Error:
        invalid('invalid_csv', 'CSV 따옴표 또는 필드 길이를 확인하세요', row=len(rows) + 2 if columns else 1)
    if not rows:
        invalid('missing_rows', '헤더 외에 데이터가 한 행 이상 필요합니다')
    return columns, rows


def summary(record):
    return {key: getattr(record, key) for key in (
        'id', 'filename', 'encoding', 'size_bytes', 'sha256', 'row_count', 'columns', 'created_at')}


async def single_file_form(request: Request):
    form = await request.form()
    if (len(form.getlist('file')) != 1 or len(form.getlist('encoding')) > 1
            or set(form.keys()) - {'file', 'encoding'}):
        invalid('invalid_form', 'file 하나와 선택적인 encoding 하나만 보내세요. 여러 파일은 각각 업로드하세요')


def router(database):
    routes = APIRouter(prefix='/v1/uploads', tags=['uploads'])

    @routes.post('', status_code=201, response_model=UploadPreview, dependencies=[Depends(single_file_form)])
    def upload(file: UploadFile = File(...), encoding: Encoding = Form('utf-8-sig')):
        # Client filenames are display metadata, never filesystem paths.
        filename = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
        try:
            if not filename or len(filename) > 200 or any(ord(c) < 32 for c in filename):
                invalid('invalid_filename', '파일명은 제어문자 없이 1~200자로 지정하세요')
            if not filename.lower().endswith('.csv'):
                invalid('unsupported_type', '현재는 CSV만 지원합니다. DOCX·PDF는 추출 모듈 연동 예정입니다', status=415)
            raw = file.file.read(MAX_BYTES + 1)
            columns, rows = parse_csv(raw, encoding)
            record = UploadRecord(id=str(uuid.uuid4()), filename=filename, encoding=encoding,
                size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), raw=raw,
                columns=columns, rows=rows, row_count=len(rows))
            with database.session() as db:
                db.add(record)
                db.commit()
                return {**summary(record), 'preview_rows': rows[:20]}
        finally:
            file.file.close()

    @routes.get('', response_model=list[UploadSummary])
    def list_uploads(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=2147483647)):
        with database.session() as db:
            records = db.scalars(select(UploadRecord).options(defer(UploadRecord.raw), defer(UploadRecord.rows))
                .order_by(UploadRecord.created_at.desc(), UploadRecord.id).offset(offset).limit(limit)).all()
            return [summary(record) for record in records]

    @routes.get('/{upload_id}', response_model=UploadPreview)
    def get_upload(upload_id: str):
        with database.session() as db:
            record = repo.require(db, UploadRecord, upload_id, '업로드')
            return {**summary(record), 'preview_rows': record.rows[:20]}

    @routes.post('/{upload_id}/scenarios', status_code=201, response_model=contracts.Version)
    def create_scenario(upload_id: str, body: ScenarioFromUpload):
        with database.session() as db:
            record = repo.require(db, UploadRecord, upload_id, '업로드')
            scenario = Scenario(name=body.name, inputs={
                'input_schema': INPUT_SCHEMA,
                'source': {'upload_id': record.id, 'filename': record.filename, 'sha256': record.sha256,
                           'encoding': record.encoding},
                'validation_scope': 'structure_only', 'domain_validation': 'pending',
                'columns': record.columns, 'rows': record.rows,
            })
            version = repo.store_version(db, scenario)
            db.commit()
            return {'scenario_id': version.scenario_id, 'version': version.version,
                    'scenario': version.payload, 'sha256': version.sha256, 'parent_run_id': None}

    return routes
