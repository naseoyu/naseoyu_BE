import base64
import shutil

import pytest
from fastapi.testclient import TestClient

from be2_backend import inspection
from be2_backend.api import create_app


def pdf(line):
    stream = f'BT /F1 12 Tf 50 700 Td ({line}) Tj ET'.encode()
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Count 1 /Kids [3 0 R] >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    data = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    start = len(data)
    data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets:
        data.extend(f'{offset:010} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF'.encode())
    return bytes(data)


def doc(raw):
    return dict(name='input.pdf', sourceType='local_upload', sizeBytes=len(raw),
                base64Data=base64.b64encode(raw).decode())


def post(docs):
    return TestClient(create_app()).post(inspection.PATH, json={'documents': docs, 'aidcSpecs': {}})


@pytest.mark.skipif(not shutil.which('pdftotext'), reason='requires Poppler')
def test_actual_pdfs_and_partial_failure():
    docs = [doc(pdf('Electricity cost USD 0')), doc(pdf('Generator fuel USD 55'))]
    response = post(docs)
    assert response.status_code == 200
    items = response.json()['items']
    assert [x['itemNumber'] for x in items] == list(range(1, 12))
    assert [x['itemNumber'] for x in items if x['existsInDocument']] == [1, 3]
    assert items[0]['detectedValue'] == 'Electricity cost USD 0'
    assert items[1]['estimatedValue'] == '-'
    assert post([docs[0], doc(pdf(''))]).json()['code'] == 'TEXT_REQUIRED'


def test_invalid_and_metadata_only():
    assert post([]).json()['code'] == 'INVALID_REQUEST'
    assert post([doc(b'not a pdf')]).json()['code'] == 'INVALID_PDF'
    sample = dict(name='sample', sizeBytes=100, sourceType='preset_sample')
    assert post([sample]).json()['code'] == 'PDF_REQUIRED'


def test_documented_error_shape_matches_response():
    schema = create_app().openapi()
    responses = schema['paths'][inspection.PATH]['post']['responses']
    for status in ('413', '422', '503', '504'):
        assert responses[status]['content']['application/json']['schema']['$ref'] == '#/components/schemas/ErrorResponse'
    fields = schema['components']['schemas']['ErrorResponse']['required']
    assert set(post([]).json()) == set(fields) == {'error', 'code'}
