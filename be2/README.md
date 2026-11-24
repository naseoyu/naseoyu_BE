# 데이터센터 Agent — BE 2 전용 백엔드

## v1.1.0 — 2026-11-17 개발

CSV 업로드·구조 검증·원본 저장·미리보기·분석 입력 연결을 추가했습니다.
사용법과 제한은 `docs/CSV_INTAKE.md`를 확인하세요.
비용 공통 스키마는 미정이므로 숫자·통화·기간 검증 완료를 의미하지 않습니다.
DOCX/PDF 추출과 실제 OpenAI 분석은 후속 작업입니다. 기존 API는 유지됩니다.

**담당 범위: API·DB·Preset 저장·분석 이력·비동기 워커·SSE·취소·재시도.**

BE 1의 비용 분석·추천·프롬프트·LLM 호출은 포함하지 않습니다. 기본 `MockAgent`로 BE 2의 전체 흐름을 실행할 수 있으며, 결과는 `is_mock=true`, 비용은 `null`로 표시됩니다. BE 1은 정해진 Python 인터페이스에 실제 Agent를 연결합니다.

이전 통합 백엔드와 구분되는 독립 프로젝트입니다. `be2.db`라는 새 DB를 사용하고, 이전 DB를 연결하지 않습니다. 배포·인증·EXE 빌드는 이번 범위에 없습니다.

## 실행

저장소 루트에서 `cd be2`를 실행한 뒤 아래 명령을 사용하세요.

Python 3.11 이상이 필요합니다. Windows는 `start_windows.bat`를 더블 클릭하거나 다음을 실행하세요.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m be2_backend.cli serve --open
```

3.13이 없으면 `py -3.11`을 사용할 수 있습니다. BAT는 설치된 3.13, 3.12, 3.11을 순서대로 확인합니다. 최초 패키지 설치에는 인터넷이 필요합니다. PowerShell 가상환경 활성화는 필요하지 않습니다.

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m be2_backend.cli serve --open
```

기본 주소는 `http://127.0.0.1:8010/docs`입니다. 포트를 사용할 수 없으면 다른 포트를 자동 선택하므로 콘솔의 실제 주소를 확인하세요. `/docs`에서 API를 실행하고 `/openapi.json`으로 스키마를 가져올 수 있습니다.

## 확인 순서

1. `GET /v1/presets/small`로 DB에 저장된 예시를 조회합니다.
2. 응답의 `scenario`를 `POST /v1/scenarios`에 넣어 저장합니다.
3. 반환된 `scenario_id`, `version`으로 `POST /v1/analyses`를 호출합니다. `Idempotency-Key` 헤더에 UUID 등 고유한 키를 넣습니다.
4. `GET /v1/analyses/{id}/events`로 진행 상황을 구독합니다.
5. 완료 후 분석 조회·리포트·JSON 다운로드를 확인합니다.

모의 Agent는 짧은 시간에 완료됩니다. 취소를 확인하려면 `BE2_INLINE_WORKER=false`로 API만 실행하여 대기 상태에서 취소하거나 테스트의 지연 Agent를 사용합니다.

## API

| 메서드 | 경로 | 기능 |
|---|---|---|
| GET | `/health` | API·DB 스키마 확인. 분리 워커 생존 확인은 아님 |
| GET | `/v1/presets` | 저장된 Preset 목록 |
| GET | `/v1/presets/{id}` | Preset 상세 |
| POST / GET | `/v1/scenarios` | 시나리오 저장 / 목록 |
| GET | `/v1/scenarios/{id}` | 최신 버전 조회 |
| POST / GET | `/v1/scenarios/{id}/versions` | 새 버전 저장 / 버전 목록 |
| GET | `/v1/scenarios/{id}/versions/{n}` | 특정 버전 조회 |
| POST / GET | `/v1/analyses` | 분석 등록 / 이력 목록 |
| GET | `/v1/analyses/{id}` | 실행 상태 및 완료 결과 |
| GET | `/v1/analyses/{id}/events` | SSE 실시간 이벤트와 재생 |
| GET | `/v1/analyses/{id}/report` | 완료 리포트 |
| GET | `/v1/analyses/{id}/export` | 입력 스냅샷·결과·리포트 JSON 다운로드 |
| POST | `/v1/analyses/{id}/cancel` | 대기/실행 중 작업 취소 |
| POST | `/v1/analyses/{id}/retry` | 실패/취소 작업 재시도 |

목록은 `limit`(최대 200), `offset`으로 조회합니다. 요청과 최종 Agent 결과 크기는 각각 최대 2MiB입니다. 분석 등록에만 `Idempotency-Key`가 필수입니다. 동일한 키·요청은 같은 분석을 반환하며, 다른 요청을 같은 키로 보내면 409입니다. Agent revision이 바뀌면 새 키로 분석을 등록하세요.

시나리오의 바깥 형식은 `{name, inputs}`입니다. `inputs`는 JSON 객체로 보존하며 비용·성능 의미 검사는 BE 1/데이터 담당이 수행합니다. 새 버전은 `{scenario, expected_version}`으로 저장하고 동시 수정 충돌은 409를 반환합니다. 이전 버전과 분석 스냅샷은 수정하지 않습니다.

## DB와 비동기 처리

- SQLAlchemy + Alembic 사용, 기본 SQLite. PostgreSQL 드라이버는 선택 설치.
- `presets`, `scenarios`, `scenario_versions`, `analysis_runs`, `analysis_events`, `reports` 테이블.
- 시작 시 소형·중형·대형 Preset을 없는 경우에만 추가합니다. 기존 Preset을 덮어쓰지 않습니다.
- API는 작업을 DB에 등록하고 워커가 가져가 실행합니다. PostgreSQL에서는 `FOR UPDATE SKIP LOCKED`를 사용합니다.
- 워커가 주기적으로 실행 소유권을 갱신합니다. 종료·중단으로 임대가 만료되면 다른 워커가 회수합니다.
- 취소 시 소유권을 무효화하여 늦게 도착한 결과의 저장을 막습니다.
- 정상 종료 시 실행 중 작업을 다시 대기 상태로 돌립니다. 강제 종료 시 임대 만료 후 회수합니다.
- 예외/시간 초과는 `failed`이며 명시적 재시도가 필요합니다. 중단 작업 회수와 재시도에는 공통 시도 한도를 적용합니다.
- Agent 버전이 다른 워커가 작업을 처리하면 실패로 표시합니다. API와 워커의 revision을 일치시키세요.

## API / 워커 분리 실행

기본 모의 테스트는 API 프로세스 안에서 워커를 실행합니다. BE 1의 실제 Agent를 연결할 때는 프로세스를 분리해 사용하세요. 두 터미널은 같은 프로젝트 폴더와 같은 DB 설정을 사용합니다.

```powershell
# 초기화
.\.venv\Scripts\python.exe -m be2_backend.cli init

# 터미널 A: API
$env:BE2_INLINE_WORKER="false"
.\.venv\Scripts\python.exe -m be2_backend.cli serve

# 터미널 B: 워커
.\.venv\Scripts\python.exe -m be2_backend.worker
```

Linux에서는 `BE2_INLINE_WORKER=false .venv/bin/python -m be2_backend.cli serve`와 `.venv/bin/python -m be2_backend.worker`를 별도로 실행합니다. SQLite 상대 경로는 작업 폴더에 따라 달라지므로 다른 폴더에서 실행하면 절대 경로 DB URL을 지정하세요.

PostgreSQL은 `pip install -e ".[postgres]"` 후 `BE2_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DB`를 설정하고 `init`을 실행합니다. 실제 PostgreSQL 통합 검증은 이 전달본에서 수행하지 않았습니다.

## 설정

`.env.example`을 `.env`로 복사하거나 환경변수로 지정합니다.

| 설정 | 기본값 |
|---|---|
| `BE2_DATABASE_URL` | `sqlite:///./be2.db` |
| `BE2_INLINE_WORKER` | `true` |
| `BE2_POLL_SECONDS` | `0.2` |
| `BE2_LEASE_SECONDS` | `30` |
| `BE2_JOB_TIMEOUT_SECONDS` | `120` |
| `BE2_MAX_ATTEMPTS` | `3` |
| `BE2_AGENT_FACTORY` | `be2_backend.agent:MockAgent` |
| `BE2_AGENT_REVISION` | `mock-v1` |
| `BE2_CORS_ORIGINS` | 비어 있음. 예: `http://localhost:5173` |

BE 1 연결 규약은 `docs/BE1_HANDOFF.md`, 프론트 SSE 규약은 `docs/FRONTEND.md`, 테스트 결과는 `docs/VERIFICATION.md`를 확인하세요.

## 테스트

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\smoke_processes.py
```

두 번째 명령은 임시 DB를 사용하여 API와 워커를 실제 별도 프로세스로 시작하고 등록·처리·SSE·다운로드를 확인합니다. 사용자 DB를 변경하지 않습니다.

## 담당 파일

| 파일 | BE 2에서 수정할 부분 |
|---|---|
| `api.py` | API·조회·SSE·CORS·요청 크기 제한 |
| `db.py`, `migrations/` | 저장 모델과 DB 스키마 변경 |
| `repository.py` | 저장 트랜잭션·중복 요청·상태 전환·실행 소유권 |
| `worker.py` | Agent 실행·타임아웃·임대 갱신·결과 저장 |
| `domain.py`, `contracts.py` | BE 1 및 프론트와 합의할 데이터 계약 |
| `agent.py` | 연결 인터페이스와 테스트 전용 MockAgent |
| `presets.py` | 초기 Preset 데이터 |
| `config.py`, `cli.py` | 실행 설정과 로컬 실행기 |

현재 API는 이전 통합 백엔드의 입력 계약과 다릅니다. 기존 프론트를 그대로 붙이지 말고 제공한 OpenAPI와 예제를 기준으로 연결하세요.
