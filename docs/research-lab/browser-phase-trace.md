# REGNA 브라우저 지연 관측

`browser_phase_trace.cjs`는 공식 MCP CLI의 Node preload와 `--init-page`로 로드한다.
브라우저 샌드박스·노출 도구·타임아웃·스냅샷 모드는 바꾸지 않는다.

새 Run에서 **첫 브라우저 도구 호출 전** 해당 Run outbox의
`browser/phase-spans.jsonl.request`를 빈 파일로 만들면 계측을 켠다.
이 파일이 없으면 trace를 기록하지 않는다. 계측 적용 전 시작한 브라우저를 재사용하지 않는다.

`browser/phase-spans.jsonl`에 실제 로드한 MCP/core 버전·CLI/core SHA256,
RPC 시작/출력 준비, page.goto·waitForLoadState·ariaSnapshot의 시작/끝을 남긴다.
`browser_prepage_trace.cjs`는 CLI가 자연스럽게 로드하는 coreBundle의
Chromium launch·connect·bind·newContext·newPage, backend initialize와 tab initialize도 관측한다.
coreBundle을 미리 require하거나 브라우저를 준비하지 않는다. 이미 로드됐으면 같은 cache의
객체를 관측하며, 아직 없으면 한 번의 자연스러운 module load 뒤 후크를 해제한다.
실제 존재하는 메서드와 빠진 메서드는 `prepage_coverage`에 구분한다.
marker가 없으면 이 후크와 메서드 래핑도 설치하지 않는다.
동시에 여러 RPC가 있으면 phase의 rpc_id는 null이다. URL·도구 인수·페이지 본문·오류 본문은 저장하지 않는다.
기록 실패는 stderr에 한 번 표시하고 계측을 중단하며 MCP 응답은 그대로 전달한다.

`rpc_end`는 stdout.write 진입 시각이다. 클라이언트 수신 완료가 아니며,
launch/connect/context/tab 구간은 중첩될 수 있으므로 단순 합산하지 않는다.
각 phase는 해당 API 호출 경계이며 Chrome의 내부 OS 단계를 분해한 값은 아니다.
빠진 coverage와 RPC 시작부터 첫 관측 phase 사이의 시간은 미할당으로 유지한다.
내부 settle·아티팩트 파일 쓰기는 개별 계측하지 않는다.
snapshot 끝과 출력 준비 사이 간격은 이 잔여 처리를 포함할 수 있다.
`trace_write_ms_before`는 이전 trace 쓰기 비용만이며 전체 계측 오버헤드가 아니다.
계측 결과는 진단용이다. 과거 4.076초 호출의 원인이나 성능 개선을 소급해 입증하지 않는다.
runtime 행에는 preload와 pre-page helper의 SHA256을 각각 남긴다.
관측 wrapper는 반환값·원래 Promise·동일 오류 객체를 유지한다. trace I/O 비용은
새 계측의 비용이며 연구의 reset 예산에서 빼지 않는다.

검증 명령:

```sh
node --test tests/browser_phase_trace.test.cjs tests/browser_prepage_trace.test.cjs
PYTHONPATH=src python -m unittest discover -s tests -p test_browser_mcp.py -v
```

두 명령은 실제 브라우저나 모델을 실행하지 않는다.
