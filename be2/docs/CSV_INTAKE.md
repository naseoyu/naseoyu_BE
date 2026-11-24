# CSV 수신 계약 초안 — v1.1.0

2026-11-17 구현. 팀 합의 전의 기술 초안이며 비용 데이터 공통 스키마가 아니다.
CSV 수신·구조 검증·원문 저장을 먼저 구현했다. DOCX/PDF 추출, 비용 의미 검증,
통화·기간·단위 정규화 및 실제 비용 분석은 아직 구현하지 않았다.

## 사용 흐름

1. `POST /v1/uploads`에 multipart `file` 하나를 전송한다.
2. 응답의 미리보기와 `warnings`를 확인한다. 이 단계는 `domain_validation=pending`이다.
3. `POST /v1/uploads/{id}/scenarios`에 `{"name":"11월 비용"}`을 보내 입력 스냅샷을 만든다.
4. 기존 `POST /v1/analyses`에 `scenario_id`, `version` 및 `Idempotency-Key`를 보낸다.
5. 기존 상태 조회·SSE·취소·재시도·리포트 API를 사용한다. 기본 실행은 모의 분석이다.

| 메서드 | 경로 | 응답 |
|---|---|---|
| POST | `/v1/uploads` | 201, 메타데이터와 최대 20개 데이터 행 |
| GET | `/v1/uploads?limit=50&offset=0` | 메타데이터 목록, 본문·원본 바이트 제외 |
| GET | `/v1/uploads/{id}` | 메타데이터와 미리보기 |
| POST | `/v1/uploads/{id}/scenarios` | 201, 새 시나리오 버전 1 |

업로드와 시나리오 생성은 매 호출 새 리소스를 만든다. 이 두 API의 멱등성은 아직 지원하지 않는다.
분석 등록은 기존 `Idempotency-Key` 중복 방지 기능을 그대로 사용한다.
다중 파일 배치 원자성은 미정이다. 현재 한 요청에 여러 파일을 보내면 422로 거절한다.

## 입력 제한과 오류

- 확장자 `.csv` (대소문자 무관). MIME만 신뢰하지 않고 실제 텍스트를 파싱한다.
- CSV: 쉼표 구분, 큰따옴표 인용, 헤더 필수. 탭/세미콜론 구분을 자동 추측하지 않는다.
- `encoding`: `utf-8-sig` 기본(UTF-8 BOM 유무 모두 지원) 또는 명시적인 `cp949`.
- 파일 최대 1MiB, 요청 전체 최대 2MiB(기존 공통 미들웨어), 최대 5,000행·100열·100,000셀.
- 헤더는 앞뒤 공백 없이 1~100자. 유니코드 NFC·대소문자 기준 중복을 거절한다.
- 모든 행은 헤더와 같은 셀 수여야 하며 빈 줄을 자동으로 버리지 않는다.
- 셀 최대 10,000자. 빈 문자열과 `"0"`을 구분하고 셀 공백·개행·숫자 문자열을 보존한다.
- 숫자 변환·환율 변환·누락값 채우기·금액 계산은 수행하지 않는다.
- 파일명은 경로 부분을 제거한 표시 정보일 뿐, 실제 파일 경로로 사용하지 않는다.
- 잘못된 CSV는 저장하지 않는다. 검증 오류는 코드·메시지·행/열을 반환하며 셀 내용을 노출하지 않는다.
- 오류의 `row`는 헤더를 1로 하는 논리적 CSV 레코드 번호다. 인용된 개행은 같은 레코드다.
- 413: 크기 초과, 415: CSV 외 형식, 422: CSV/요청 형식 오류, 404: 없는 업로드.
- CSV 구조 오류: `detail={code,message,row,column}`. FastAPI 필드 오류는 기존 `detail` 목록 형식이다.

원본 바이트와 SHA-256, 파싱 결과를 같은 DB 트랜잭션에 저장한다. 업로드 수정 API는 없다.
분석에는 시나리오 생성 시점의 전체 표 사본과 원본 해시가 저장된다.
파일 다운로드/CSV 재내보내기는 제공하지 않으며, 문자열을 수식이나 코드로 실행하지 않는다.
프론트는 셀을 텍스트로 렌더링하고 HTML로 직접 삽입하지 않아야 한다.

## BE1 연동

`Scenario.inputs` 형식:

```json
{
  "input_schema": "be2.csv.raw.v1",
  "source": {"upload_id":"UUID", "filename":"costs.csv", "sha256":"SHA256", "encoding":"utf-8-sig"},
  "validation_scope": "structure_only",
  "domain_validation": "pending",
  "columns": ["항목", "금액"],
  "rows": [["전력료", "0"], ["보험료", ""]]
}
```

이 형식을 처리할 실제 Agent는 `supported_input_schemas = ('be2.csv.raw.v1',)`를 선언하고,
`analyze`에서 합의된 컬럼 매핑과 업무 검증을 수행해야 한다. 지원 선언이 없는 기존
Agent에는 원시 CSV를 전달하지 않고 작업을 실패 상태로 끝낸다. 선언은 업무 검증을 대신하지 않는다.
MockAgent만 기본으로 허용하며 비용은 계산하지 않는다. OpenAI 키·SDK는 추가하지 않았다.

## Windows 예시

서버 실행 후 콘솔 주소를 기준으로 포트를 지정한다.

```powershell
curl.exe -X POST http://127.0.0.1:8010/v1/uploads -F "file=@examples/costs-draft.csv" -F "encoding=utf-8-sig"
```

`/docs`에서도 업로드 → 시나리오 생성 → 분석 등록을 순서대로 시험할 수 있다.
기존 DB를 별도 API/워커에서 사용하는 경우 업그레이드 전에 백업하고,
API·워커를 종료한 뒤 `python -m be2_backend.cli init`을 한 번 실행한다.
`serve` 명령은 로컬 편의상 마이그레이션을 자동 적용한다.

현재 인증·사용자별 접근 제어·업로드 총량/보존기간·삭제 정책이 없으므로 로컬 개발용이다.
PostgreSQL은 DDL 생성 검사만 수행하며 실제 PostgreSQL 통합 및 Windows 실행 검증은 별도다.
