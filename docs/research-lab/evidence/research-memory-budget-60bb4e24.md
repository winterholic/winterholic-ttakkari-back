# 선택형 모델 자식 RSS 중단 기준

2026-10-26 리드 Codex가 연구 worktree에서 직접 구현·검증했다. 시작 backend는
`60bb4e24861cc6722129d5fc44dd0bb73c5435a1`, frontend는
`1a69dcd95da63036c4a5949b439d6035f2bc62ba`다. 원목표와 PMS7.3은 active/doing이다.
기존 A~D/UI/575필드·소유권·child 영수증 검증은 해당 원범위로 재사용했다.
새 worker나 모델 서버를 만들지 않았다. 이전 d1204079 예약은 이번 한 번 조회에서
계속 testing/generations/skip_tests=false/error=null이었으며 대기 폴링하지 않았다.

## 확인한 차이와 계약

기존 planner는 계획용 메모리 추정치(4096/512)를 내놓고, TTS/STT는 종료 뒤
ru_maxrss를 돌려줬다. UI의 max_memory_mb는 이 실행 경로에서 소비하지 않았다.
기존 host GPU swap-growth guard는 호스트 swap 변화에 대한 보호로, Run RSS 상한과
구분한다. 기존 보호를 바꾸거나 타 launch를 중단하지 않았다.

모델 없는 소유 자식에 16MiB를 할당한 직접 probe에서 자기 peak RSS 34,717,696bytes,
부모가 읽은 RSS 34,750,464bytes와 exit0을 관측했다. 이는 Codex 명령 환경의 관측이며
host GPU 경로의 측정이나 ML 추론 인수가 아니다.

공유 run_json_worker에 선택형 ContextVar 감시만 붙였다. 요청 payload 전 첫 RSS를
조회하고 이후 50ms마다 **각 소유 모델 자식의 RSS**를 읽는다. 초과하거나 관측 권한이
거절되면 그 handle만 기존 kill/wait 경로로 정리한다. 실패는 문맥에 고정해 다음 자식
생성을 막는다. 기존 소유권 기록은 입력 전에 저장하며 종료 영수증도 그대로 남긴다.

이는 **OS 강제 상한이 아니다**. 샘플 사이의 초과/피크, 부모·자손·GPU 메모리,
Run의 합산 메모리는 보장하지 않는다. 설정 단위는 MiB(1,048,576bytes)다. 기존
각 자식의 사후 peak RSS 지표와 이번 sampled_peak_rss_bytes도 구분한다.

runner는 max_memory_mb를 파싱하고 TTS synthesize/synthesize_and_stt에서만 연결한다.
그 외 실행기·normalize_only·잘못된 형식은 plan/API와 저장된 Run의 실제 dispatch에서
거절하며 조용히 무시하지 않는다. 생략/null은 감시 꺼짐이다. v1 memory_mb/seconds와
기존 host guard는 변경하지 않았다. 어댑터가 감시 예외를 잡아 성공을 반환해도 runner는
cancelled로 저장하고 후속 queued Run을 중단한다. 사용자 취소와 원문 출력은 보존한다.
관측이 0건인 성공 반환도 거절한다. 환경 영수증은 owner token으로 연결하며 어댑터가
research_memory_guard를 주입할 수 없다. schema/migration 변경은 없다.

UI는 새 실험과 읽기 정규화에서 가짜 기본 4096을 넣지 않는다. RSS 기준은 선택값이고
비우면 요청에서 빠진다. 저장 null도 감시 꺼짐으로 보여준다. 현재 서버가 검증 응답의
resource_estimate.memory_budget_contract=1을 반환하지 않으면 명시한 RSS 기준의
실행을 프론트에서도 거절한다. 따라서 새 프론트와 이전 백엔드의 배포 시차 중에
동작하지 않는 제한을 지원한다고 표시하지 않는다. 해당 표지는 구현 계약이며 실제
프로세스 RSS 관측 가능 여부는 실행 전 관측으로 다시 확인한다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_memory_budget.py -q --timeout=12 --tb=short
최초 4 failed in 0.52s → 최초 구현 뒤 3PASS/1FAIL
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_memory_budget.py -q --timeout=15 --tb=short
최초 10 failed in 21.30s → 연결 후 worker/DB 14 passed in1.99s
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_memory_budget.py tests/test_research_memory_budget.py unit_tests/test_model_process_lifecycle.py tests/test_research_model_resources.py tests/test_research_runner_lifecycle.py tests/test_research_adapters.py tests/test_local_stt_runner.py tests/test_local_stt_worker.py tests/test_tts_queue.py -q --timeout=25 --tb=short
203 passed in22.02s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_memory_budget.py unit_tests/test_model_memory_budget.py -q --timeout=15 --tb=short
최종 19 passed in2.06s
$ .venv/bin/ruff check src/ttakkari/model_process.py src/ttakkari/research/planner.py src/ttakkari/research/runner.py src/ttakkari/research/ownership.py tests/test_research_memory_budget.py unit_tests/test_model_memory_budget.py
All checks passed!
$ pnpm exec vitest run src/pages/research
7 files / 91 passed (duration3.59s)
$ pnpm exec eslint src/pages/research/{types.ts,designerModel.ts,api.ts,components/ValidatePanel.tsx,views/ExperimentDesignerView.tsx,memory-budget.test.tsx}
exit0
$ pnpm build
tsc/Vite/PWA exit0; existing chunk-size warning remains
$ git diff --check
backend/frontend exit0
```

메모리 시험은 실 소유 자식의 96MiB 할당과 ctypes.PyDLL의 native sleep을 사용한다.
낮은 기준 초과·입력 전 합성 초과·AccessDenied 반례·정상 출력·실패 후 새 자식0을
검사했다. 실제 Supertonic/Whisper ML 호출로 확대하지 않는다.

최초 입력 전 1MiB 반례는 exec 초기 RSS가 그보다 작을 수 있어 불충분했다.
이미 초과한 관측을 주입하는 반례로 고치고 별도의 실 96MiB 할당 검사는 유지했다.
확대 회귀의 정상 자식1FAIL/154PASS는 RSS 조회 직전에 자식이 종료된 NoSuchProcess였다.
종료 순서를 고정한 반례1RED 후 정상 종료는 communicate/wait를 끝내고 보존하도록
교정했다. AccessDenied는 여전히 거절한다. 선택 런타임 의존성 반례는 python -S에서
psutil import 실패를 재현한 뒤 부모 감시에서만 지연 import하도록 교정했다.
배포 시차 반례(API 표지 누락/프론트 통과)와 null 화면 오류도 RED 후 교정했다.
중간·관련·최종·필수 hook의 개수는 겹치므로 합산하지 않는다.

actual Run PostgreSQL의 identity를 기존 seed가 주입 URL과 비교하고 운영 ttakkari DB를
거부한다. 어댑터는 합성이지만 자식·kill/wait·Run 저장·인증 fresh HTTP GET은 실제다.
종료 뒤 released checkpoint, 원본 raw/취소 상태, owner-fenced 환경값을 새 세션에서 읽었다.
프론트 검사는 jsdom과 mocked API 요청이며 실제 인증 브라우저 인수는 아니다.

## 남은 원조건

실제 ML 경로에서의 감시, OS 강제 상한/Run 전체 합산·GPU/부모/자손 메모리 예산,
native checkpoint 재개·GPU 시작 직후 기록틈, 운영 동일 A launch의 Metal/별도
RuntimeLease·인증 UI/UX10·legacy92·PMS7.1/2/4/5 및 controller 전체 gate는 미완이다.
이번 sampled RSS 기준을 그 원조건 전부의 완료로 취급하지 않는다.

최종 소스 hash는 같은 이름의 JSON에 보존한다. 필수 push hook과 원격 SHA,
배포 예약 수락은 이후 영속 계약/사용자 전달 영수증으로 연결한다. 수락은 활성 성공이 아니다.
