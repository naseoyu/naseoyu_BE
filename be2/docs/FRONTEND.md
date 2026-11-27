# 프론트 연결 안내

v1.1.0 CSV 업로드는 `CSV_INTAKE.md`를 참고하세요. `FormData`의 `file` 하나를 보내고,
`Content-Type`은 브라우저가 boundary와 함께 설정하도록 직접 지정하지 마세요.
`domain_validation=pending`을 비용 검증 완료로 표시하지 마세요. 미리보기는 최대 20행입니다.

실행 주소의 `/docs`와 `/openapi.json`을 사용합니다. 별도 origin이라면 `BE2_CORS_ORIGINS`에 정확한 origin을 지정합니다.

```javascript
const create = async (path, body, key) => {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type':'application/json', ...(key ? {'Idempotency-Key':key} : {})},
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
};
const preset = await (await fetch('/v1/presets/small')).json();
const version = await create('/v1/scenarios', {scenario:preset.scenario});
const key = crypto.randomUUID(); // 통신 실패로 같은 요청을 재전송할 때 유지
const run = await create('/v1/analyses', {
  scenario_id:version.scenario_id, version:version.version
}, key);
const source = new EventSource(run.events_url);
source.addEventListener('progress', e => console.log(JSON.parse(e.data)));
source.addEventListener('agent_message', e => console.log(JSON.parse(e.data).text));
source.addEventListener('report_block', e => console.log(JSON.parse(e.data)));
source.addEventListener('reset', () => { /* 이전 시도의 임시 설명·리포트를 지웁니다. */ });
source.addEventListener('completed', async () => {
  source.close();
  const saved = await (await fetch(`/v1/analyses/${run.id}`)).json();
  console.log(saved.result); // 최종 화면은 저장된 결과로 확정
});
source.addEventListener('failed', () => source.close());
```

상태: `queued`, `running`, `completed`, `failed`, `cancelled`. 취소는 `POST /v1/analyses/{id}/cancel`, 실패/취소 후 재시도는 `/retry`입니다. 재시도 응답을 받으면 이전 임시 화면을 지우고 스트림을 새로 여세요.

SSE는 `id`, `event`, JSON `data` 형식입니다. `data.attempt`는 실행 시도 번호입니다. report_block에는 `id`, `text`, `evidence_refs`가 있습니다. `(attempt, block.id)` 또는 SSE 이벤트 ID로 중복 렌더링을 막으세요.

- EventSource 자동 재연결은 `Last-Event-ID`를 사용합니다.
- 새 연결에서 직접 이어 보려면 `?after=<마지막 이벤트 ID>`를 사용합니다. 헤더가 있으면 우선합니다.
- 다른 분석의 커서나 존재하지 않는 커서는 400입니다.
- 재시도/작업 회수 시 이전 시도의 이벤트는 제외하고 `reset`으로 새 초안을 시작합니다.
- 완료/실패 이벤트 후 `.close()`해야 반복 재접속하지 않습니다.
- 연결 장애 시 분석 조회 API로 이미 완료됐는지 확인할 수 있습니다.
- `failed` 이벤트의 `code=cancelled`는 사용자 취소입니다.
- `agent_message`와 완료 전 리포트는 초안입니다. 취소되면 확정 결과로 표시하지 않습니다.
- 재연결은 새 분석을 만들지 않습니다.
- `result.is_mock=true`이면 모의 결과임을 표시하고 `null` 금액을 0원으로 바꾸지 마세요.

API 오류: 400(커서), 404(없는 리소스), 409(버전/요청 충돌·상태 오류·미완료 결과), 413(크기 초과), 422(형식 오류). 등록 응답은 202, 시나리오 저장은 201입니다.

### PDF 비용 항목 사전 검사

FE 커밋 `95424cc`의 업로드 화면과 동일한 `POST /api/inspect-documents`를 제공합니다.
실제 화면에서 사용하려면 개발 프록시나 웹 서버에서 **이 경로만** FastAPI로 전달하세요.
현재 FE Express가 같은 경로를 처리하므로 BE2만 실행해도 호출 대상이 바뀌지는 않습니다.
서버의 PATH에 Poppler의 `pdftotext` 실행 파일이 필요합니다.

요청의 `documents`에는 파일별 `name`, `sourceType`, `sizeBytes`, `base64Data`를 넣습니다.
FE가 함께 보내는 ID·페이지 수·사양은 문서 근거로 사용하지 않습니다. PDF당 15 MiB,
전체 JSON 50 MiB 한도입니다. 원문 없는 샘플·메타데이터, 텍스트 없는 스캔, 손상된 PDF는
`error`와 `code`로 응답합니다. 문서 하나라도 읽지 못하면 전체 검사를 실패 처리합니다.

성공 응답에는 번호순 11개 `items`, `inspectedAt`, `modelUsed`가 있습니다.
각 항목의 `existsInDocument`는 읽은 텍스트 한 줄에서 등록된 항목명 또는 별칭을 찾았다는 뜻입니다.
첫 일치 행과 파일명을 반환합니다. 이미지 속 표, 미등록 표현, 줄바꿈으로 나뉜 항목은 놓칠 수 있습니다.
금액 검증이나 시장 추정은 수행하지 않으며 `estimatedValue`는 "-"입니다.
이 검사 결과를 금액으로 합산하지 마세요. PDF와 검사 결과는 이 경로에서 DB에 저장하지 않습니다.
