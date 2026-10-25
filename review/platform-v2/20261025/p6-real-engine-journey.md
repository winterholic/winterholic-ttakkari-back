# P6 실제 엔진 여정 (opt-in)

- 일시: 2026-10-25, 베이스 `e4e0918`. 제품 src 수정 없음, 제품 결함 없음.
- 테스트: `tests/test_real_engine_journey.py` 1건. `TTAKKARI_REAL_ENGINE_JOURNEY=1`일 때만 실행하고 기본 스위트에서는 skip한다.
- 하네스: 기존 `test_executor_process_journey`의 실제 `ttakkari.cli serve`/`executor --activate` child, Core observer, 주입 테스트 DB를 그대로 쓴다. 엔진만 fake_claude 대신 실제 `claude`(2.1.285)다.

## 결과

```
$ TTAKKARI_REAL_ENGINE_JOURNEY=1 PYTHONPATH=$PWD/src python -m pytest tests/test_real_engine_journey.py -q -s
1 passed in 12.36s
run1 succeeded cost $0.0327 | reply "The memo categories list is empty with PERSIMMON381DDC showing 0 categories."
run2 succeeded cost $0.0366 | reply "PERSIMMON381DDC" | same_engine_session true | core samples 60 (전체 189개 / 10.9초)
$ python -m pytest tests/test_real_engine_journey.py tests/test_executor_process_journey.py -q   (opt-in 없이)
6 passed, 2 skipped      (새 파일 skip + 기존 6b skip)
```

| 요구 | 관측 |
|---|---|
| 1 실제 응답 저장 | Run1 `succeeded`, `result_text`가 비어 있지 않고 무작위 codeword 포함. `agent.session` 이벤트 존재, 실제 engine session id는 `ChatSession.engine_sessions`에 1건 저장(이벤트의 id 값은 제품이 `[REDACTED]`로 마스킹) |
| 2 MCP 도구 호출 | 제품이 Run에 붙이는 orchestrator MCP의 `mcp__ttakkari__list_memo_categories`가 `tool.call` 정확히 1회, `tool.result`는 `is_error=false`. 본문 `{"categories": []}`는 임시 Core가 테스트 DB를 읽은 결과(빈 DB) |
| 3 재개 | 두 번째 Run이 같은 계정(profile)·같은 engine session id로 이어졌음을 단언(`resumed == engine_session`, `done2.profile == done1.profile`)하고 codeword를 정확히 답함. 같은 계정 재개일 때만 `_handoff_context`가 비므로 codeword가 handoff로 새어 들어간 답이 아니다. 두 번째 Run에는 도구 호출 없음 |
| 4 실행부 교체 | Run1과 Run2 사이에 실행부 SIGTERM(exit 0) 후 새 revision(`f`*40) 기동. 새 실행부에서 재개 성공. Core는 observer 동안 같은 PID·ready 200 |

## 주의와 한계

- 비용: 모델 호출(claude 호출) 총 5회. 인증 확인용 직접 호출 1회, 첫 구현의 단언 오류로 Run1만 돈 2회, 최종 통과 실행 2회. 모델은 `haiku`, effort `low`.
- 검증 이력: GPT 독립검증(d1b23cb)이 재개 동일성이 출력 필드일 뿐 단언이 아니라는 점(handoff로도 통과 가능)을 지적해 위 두 단언을 추가했다. MCP 본문 단언과 재개 단언을 모두 포함한 상태로 root가 실제 엔진으로 재실행했다(결과는 커밋 메시지).
- MCP 서버는 HTTP 오류도 문자열로 돌려줘 `is_error=false`만으로는 성공이 아니다. 그래서 본문을 확인한다.
- `TTAKKARI_INTERNAL_API_URL` 기본값이 고정 포트 `127.0.0.1:8787`이다. 이 여정은 실행부 env에서 임시 Core 주소로 덮어썼다. 덮어쓰지 않으면 MCP가 그 고정 포트로 간다는 점은 별도로 알아둘 사항이다(제품 결함 판정은 하지 않았다).
- `TTAKKARI_CLAUDE_SETTING_SOURCES=project,local`로 사용자 전역 hook/CLAUDE.md를 읽지 않게 했다. 기본 설정(전역 설정 로드)의 Run은 이 증거가 아니다.
- 읽기 전용 MCP 도구 1개와 빈 테스트 DB만 확인했다. 쓰기 도구, 다른 MCP 서버, guard hook 차단 경로는 다루지 않았다.
- 실행부 교체는 진행 Run이 없는 idle 교체다. 같은 소스 트리, revision 문자열만 다르다.
- 장기 Run, 한도·fallback 계정 전환, 취소, 다중 계정, 다른 엔진(codex 등), 운영 환경은 다루지 않았다.
- `TTAKKARI_RUN_ID`가 설정된 agent Run 환경에서 실행했으며, apply 가드를 쓰는 경로는 이 테스트에 없다.
