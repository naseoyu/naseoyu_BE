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
