# 실제 CPU 모델의 선택형 RSS 감시 인수

2026-10-26 리드 Codex 명령 환경에서 직접 실행했다. 제품 소스는
`e1cfa8b492bc855ec90c04a5e44af54b59375ba8`이며 새 작업자는 만들지 않았다.
이전 203/19/84/91검사와 A~D 품질 실험을 반복하지 않았다. 설정 파일·인증
비밀을 읽지 않고 저장소의 런타임 기본 경로와 명시한 테스트 설정만 사용했다.

## 관측과 경계

| 경로 | 실제 관측 | 저장 범위 |
|---|---|---|
| TTS→STT 정상, 각 자식8192MiB 기준 | 2청크 WAV, TTS1+Whisper2 자식 모두 exit0, RSS529회·sampled peak3,599,024,128bytes, released | Run70d0d830-fb91-43af-aa2d-1b770eb62e9f; 인증 fresh ASGI HTTP GET과 별도 RunPG session의 raw/env 일치 |
| TTS128MiB 기준 | RSS6회·214,892,544bytes에서 중단, 소유 자식exit-9/released, cancelled/memory_budget_exceeded | Run826ed8fa-5d97-409b-b943-b4bb325965ee; 동일한 fresh HTTP/PG 대조 |
| STT512MiB 기준 | 기존 정상 WAV로 실제 STT runner 직접 호출, RSS6회·783,843,328bytes에서 중단, 동일 worker UUID/PID running→exited/-9 | 직접 감독기 관측만; 별도 Research Run 저장 인수 아님 |

표의 초과값은 50ms 표본 기준이며 OS 강제 상한이 아님을 보여준다. 부모·자손·GPU·
Run 합산 메모리, 표본 사이 피크는 보장하지 않는다. 품질/발음 향상·MOS·일반 성능
결론으로 확대하지 않는다. 정상 STT 전사는 이 변경의 실행 경계 확인에만 사용했다.

기존 provider 파일 readiness와 모델·Python 존재를 확인했다. 호스트 MCP 관측 당시
메모리 available38,181,486,592bytes/pressure normal·CPU13.6%였다. 연구 dashboard는
TTS/STT ready, GPU lease held=false를 반환했다. 이 값은 호스트의 모든 TTS/STT
프로세스가 없다는 증거가 아니다. 두 CPU thread로 자신의 자식만 실행·중단했고,
기존 타 launch·모델 서버·운영 프로세스에는 신호를 보내지 않았다.

실제 런타임 metadata: TTS Python3.11.16/numpy2.4.6/onnxruntime1.31.0,
STT Python3.13.15/faster-whisper1.2.1/ctranslate2 4.8.3. 설치·모델 다운로드 없음.
원시 응답·소유 자식 UUID/PID/parent/start·guard·런타임 위치·기존7소스hash는
`research-memory-live-e1cfa8b4.json`에 보존한다. 선행 front6hash까지 13/13 일치했고,
후속 selection-only 수정 후에도 모델 경로7hash가 일치했다.

## 실행 명령과 중간 실패

```text
$ TTAKKARI_RESEARCH_LIVE_MEMORY=1 RESEARCH_MEMORY_LIVE_REPORT=<이번Run outbox>/research-memory-live.json PYTHONPATH=src:tests .venv/bin/python -m pytest tests/research_smoke/memory_live_acceptance.py -q -s --timeout=260 --tb=short
1 failed in28.17s: 실제 정상Run 저장·freshHTTP/PG 대조와 자식exit0 검사는 지난 뒤 len(stt_eval)==1 가정 실패(실2청크).
$ ... -m pytest tests/research_smoke/memory_live_acceptance.py -k low_model -q -s --timeout=150 --tb=short
1 passed, 1 deselected in1.32s: 정상 추론 재실행 없이 낮은 TTS/STT만 실행.
$ ... -m pytest tests/research_smoke/memory_live_acceptance.py -k captured -q --timeout=10 --tb=short
1 passed, 2 deselected in0.54s: 보존한 정상 영수증의 청크 수 대조, child exit17/빈STT 결과 조작 반례 거절. 새 모델 실행 아님.
$ .venv/bin/ruff check src/ttakkari/research/rules.py tests/test_research_selection_failures.py tests/research_smoke/memory_live_acceptance.py
All checks passed!
$ git diff --check
exit0
```

검사 스크립트는 `tests/research_smoke/memory_live_acceptance.py`다. 기본 CI 수집 패턴에
들어가지 않고 live opt-in이 필요하다. 낮은 기준 검사는 정상 JSON과 WAV가 필요하다.
수집 파일·명령 환경 및 일반 controller 전체 검사를 혼동하지 않는다.

## 운영과 남은 원조건

`runtime_report` ffd0e5e3-68cc-41f5-bf39-30a0ee152693과
`curl -fsS --max-time 5 http://127.0.0.1:8787/api/version`은
활성 e6b4fd0df9513058f9e30845c08f4b0c9172b095/PID77451를 반환했다.
새 RSS 제품 e1cfa8b4가 활성이라는 증거가 아니다. 2a8ef9e3 예약은 이번 직접조회
1회 testing/error=null. 초기 runtime_api_url8791 접속 거절은 별도 runtime 포트를
잘못 탐색한 결과이며 8787 운영 중단을 뜻하지 않는다. runtime_report 원문은
`runtime-report-memory-live-e1cfa8b4.json`에 있다.

선행838/d120 controller는 selection failure[True]의 unknown!=applicable로 실패했다.
그 교정은 `selection-receipts-fix-e1cfa8b4.md`로 연결한다. 전체gate/새활성,
운영 동일A의 Metal·별도 RuntimeLease·인증 UI/UX10·legacy92·native checkpoint/GPU
기록틈 및 PMS7.1/2/4/5 원조건은 미완이다. R4/7/12~16은 review/doing을 유지한다.
