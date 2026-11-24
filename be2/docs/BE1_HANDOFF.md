# BE 1 연결 규약

v1.1.0 CSV 원시 입력 계약은 `CSV_INTAKE.md`를 참고하세요. 해당 스키마를 지원하는
Agent는 `supported_input_schemas`를 선언하고 컬럼 매핑·비용 의미 검증을 수행해야 합니다.

BE 2는 입력 저장·작업 실행·결과 형식 검증·SSE·상태 관리를 담당합니다. 비용 계산, 추천 판정, 프롬프트, LLM 호출·실패 시 설명 대체는 BE 1/데이터 담당의 구현입니다.

## 인터페이스

```python
from be2_backend.domain import Scenario, AgentResult
from be2_backend.agent import Emit

class TeamAgent:
    async def analyze(self, scenario: Scenario, emit: Emit) -> AgentResult:
        # 이 메서드 안에 실제 Agent 실행을 연결합니다.
        # 반환 형식: AgentResult (domain.py 및 examples/agent-result-contract.json 참고)
        ...
```

위 코드는 규약 설명이며 실제 Agent 구현은 포함하지 않습니다. 프로젝트 루트에서 import 가능한 `team_agent.py`에 구현했다면 다음을 설정합니다.

```text
BE2_AGENT_FACTORY=team_agent:TeamAgent
BE2_AGENT_REVISION=team-agent-v1
```

설정은 API와 워커에서 같아야 합니다. 팩터리는 인자 없이 객체를 반환합니다. 클래스 생성자도 사용할 수 있습니다. 실행 의미가 바뀌면 revision을 올리세요. API 키 등은 BE 1 모듈이 환경변수로 읽고 입력·결과·이벤트에 담지 않습니다.

## 입력

`Scenario.name`은 표시 이름, `Scenario.inputs`는 BE 1/데이터 담당과 합의할 입력 JSON입니다. 원본 스냅샷은 DB에 저장되고 워커에는 검증한 사본을 전달합니다. BE 2는 GPU 가격이나 피크 부하 등의 업무 규칙을 판단하지 않습니다. 필수 업무 항목 누락은 Agent에서 처리하거나 팀 합의 후 입력 모델을 구체화하세요.

## 진행 이벤트

```python
await emit('progress', {'stage':'analyzing', 'message':'구성을 분석하고 있습니다'})
await emit('agent_message', {'text':'현재까지 확인한 내용입니다'})
```

- `progress`: stage는 영문 소문자/밑줄 1~24자, message는 최대 2,000자.
- `agent_message`: text 최대 4,000자. 프론트에서 임시 설명으로 표시합니다.
- 한 실행에서 Agent 이벤트는 최대 1,000개입니다. 토큰마다 보내지 말고 적절한 문장/청크로 묶으세요.
- `completed`, `failed`, `reset`, `report_block`은 BE 2가 생성합니다. Agent가 발행하면 계약 위반으로 실패합니다.
- `emit`은 await해야 합니다. 분석 이후 별도 background task에서 호출하지 마세요.

## 결과

`AgentResult`의 필수 필드는 `provider`, `is_mock`, `summary`, `report`입니다. 기본 schema_version은 `be2.v1`입니다.

- `metrics`: 금액/수치 표시값을 문자열로 전달합니다. 누락은 `null`, 0으로 대체하지 않습니다.
- `recommendations`: id, title, details를 가진 목록입니다.
- `report`: title과 blocks 목록입니다. block의 id는 리포트 안에서 유일해야 합니다.
- `metadata`: 데이터/계산 버전 등 JSON 메타데이터입니다.
- 최종 결과 최대 2MiB, 리포트 블록 최대 100개, 블록당 text 최대 8,000자입니다.
- BE 2는 결과를 스키마 검증한 뒤 블록을 순차 전송하고 결과·리포트·완료 이벤트를 하나의 트랜잭션에 저장합니다.
- 보고된 값의 비용 정확성과 근거 타당성은 BE 1/데이터 담당의 책임이며 BE 2는 재계산하지 않습니다.

`AgentResult` 객체 또는 해당 형식의 dict를 반환할 수 있습니다. 실제 Agent에서는 `is_mock=false`로 표시합니다. 제공된 MockAgent는 항상 `is_mock=true`이고 실제 비용 값을 생성하지 않습니다.

## 실행·취소 조건

Agent는 하나의 워커 안에서 작업마다 순차 호출됩니다. 실행별 상태는 인스턴스 전역에 누적하지 않는 것이 좋습니다. CPU 연산이나 동기 SDK는 `await asyncio.to_thread(...)` 등으로 이벤트 루프를 막지 않도록 연결하세요. 비동기 I/O는 await하고 `CancelledError`를 삼키지 마세요.

취소·시간 초과는 협력적 취소입니다. 이미 시작된 스레드 연산이나 외부 요청을 강제로 되돌리지 못할 수 있지만 늦은 결과의 DB 저장은 실행 소유권 검사로 차단합니다. 외부 요청 자체에 대한 타임아웃도 BE 1에서 지정해야 합니다. 작업 회수로 Agent가 다시 호출될 수 있으므로 외부 부수 효과를 멱등하게 설계하세요.

BE 1에서 예외를 처리하지 않고 올리면 BE 2가 작업을 `failed`로 저장하고 공개 오류 메시지만 반환합니다. LLM 실패 시 기본 리포트를 제공하려면 BE 1이 유효한 AgentResult를 반환해야 합니다.
