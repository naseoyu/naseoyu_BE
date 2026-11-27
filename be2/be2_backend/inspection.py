"""Local, evidence-only inspection of text PDFs from the frontend upload screen."""
import asyncio
import base64
import binascii
import tempfile
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()
PATH = '/api/inspect-documents'
BODY_BYTES = 50 * 1024**2
PDF_BYTES = 15 * 1024**2
TEXT_BYTES = 4 * 1024**2

# Numbering follows the frontend CoreCostItem taxonomy.
ITEMS = (
    ('전력료', '전기요금', 'electricity cost'),
    ('인터넷 전용 회선 서비스', '인터넷전용회선', 'dedicated internet access'),
    ('발전기 연료비', 'generator fuel'),
    ('공업용수 및 냉각 비용', '냉각용수', 'cooling water'),
    ('GPU 시스템 감가상각비', 'gpu depreciation'),
    ('데이터센터 구축 감가상각비', 'data center depreciation'),
    ('Fit-out 공사 감가상각비', 'fit-out depreciation'),
    ('액체냉각 구축 감가상각비', 'liquid cooling depreciation'),
    ('시설 운영 외주용역비', 'outsourced facility operations'),
    ('NW 운영 외주용역비', 'outsourced network operations'),
    ('수선유지비', 'repair and maintenance'),
)


class Document(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    sizeBytes: int = Field(ge=0)
    sourceType: Literal['local_upload', 'preset_sample']
    base64Data: str | None = Field(default=None, max_length=4 * ((PDF_BYTES + 2) // 3))


class InspectionRequest(BaseModel):
    documents: list[Document] = Field(min_length=1)
    # The FE also sends aidcSpecs. Hardware assumptions do not establish document evidence.


class InspectionItem(BaseModel):
    itemNumber: int
    itemName: str
    existsInDocument: bool
    detectedValue: str
    estimatedValue: str
    estimateBasis: str


class InspectionResult(BaseModel):
    items: list[InspectionItem]
    inspectedAt: datetime
    modelUsed: str


class ErrorResponse(BaseModel):
    error: str
    code: str


class InspectionError(Exception):
    def __init__(self, message, code, status=422):
        super().__init__(message)
        self.code, self.status = code, status


async def extract(document):
    if not document.base64Data:
        raise InspectionError('PDF 원문이 필요합니다. 15 MiB 이하의 로컬 PDF를 선택하세요.', 'PDF_REQUIRED')
    try:
        data = base64.b64decode(document.base64Data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InspectionError('PDF 인코딩을 확인하세요.', 'INVALID_PDF') from exc
    if len(data) != document.sizeBytes or len(data) > PDF_BYTES or not data.startswith(b'%PDF-'):
        raise InspectionError('PDF 형식 또는 파일 크기가 일치하지 않습니다.', 'INVALID_PDF')
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        try:
            proc = await asyncio.create_subprocess_exec(
                'pdftotext', '-enc', 'UTF-8', '-', '-', stdin=source,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError as exc:
            raise InspectionError('서버에 Poppler pdftotext 설치가 필요합니다.', 'EXTRACTOR_UNAVAILABLE', 503) from exc
        try:
            async with asyncio.timeout(15):
                output = bytearray()
                while chunk := await proc.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > TEXT_BYTES:
                        raise InspectionError('추출된 텍스트가 너무 큽니다.', 'TEXT_TOO_LARGE', 413)
                if await proc.wait():
                    raise InspectionError('손상 또는 암호화된 PDF를 확인하세요.', 'PDF_UNREADABLE')
        except TimeoutError as exc:
            raise InspectionError('문서 읽기 제한 시간을 초과했습니다.', 'INSPECTION_TIMEOUT', 504) from exc
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
    text = output.decode('utf-8', errors='replace')
    if not text.strip():
        raise InspectionError('읽을 수 있는 텍스트가 없습니다. 스캔본은 문자 인식이 필요합니다.', 'TEXT_REQUIRED')
    return text


def normalize(text):
    return ''.join(text.casefold().split())


NORMALIZED_ITEMS = tuple(tuple(normalize(alias) for alias in names) for names in ITEMS)


def find_evidence(text, filename, found):
    for line in text.splitlines():
        if len(found) == len(ITEMS):
            break
        normalized = normalize(line)
        if not normalized:
            continue
        for number, aliases in enumerate(NORMALIZED_ITEMS, 1):
            if number not in found and any(alias in normalized for alias in aliases):
                found[number] = (line.strip()[:500], filename)


@router.post(PATH, response_model=InspectionResult, responses={
    status: {'model': ErrorResponse} for status in (413, 422, 503, 504)
})
async def inspect_documents(body: InspectionRequest):
    found = {}
    try:
        for doc in body.documents:
            text = await extract(doc)
            await asyncio.to_thread(find_evidence, text, doc.name, found)
    except InspectionError as exc:
        return JSONResponse({'error': str(exc), 'code': exc.code}, status_code=exc.status)
    items = []
    for number, names in enumerate(ITEMS, 1):
        value, source = found.get(number, ('미기재', '검사한 텍스트에서 등록된 명칭을 찾지 못했습니다.'))
        items.append(InspectionItem(itemNumber=number, itemName=names[0], existsInDocument=number in found,
                                    detectedValue=value, estimatedValue='-', estimateBasis=source))
    return InspectionResult(items=items, inspectedAt=datetime.now(UTC), modelUsed='local-text-match-v1')
