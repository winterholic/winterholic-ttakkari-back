# Native GIL 보유 중 owner 사망 처리

2026-10-26 리드 Codex 직접. 기준 `116dad45203fad2c81a862309016d430eb3eae49`.
원목표 R1~16/PMS664a2c29는 active이며 전체 인수가 아니다. 새 worker는 만들지 않았다.

## 원인과 변경

기존 모델 자식은 Python thread로 부모 PID를 감시했다. 모델 없는 실제 자식의
TTS/STT main에 `ctypes.PyDLL(None).sleep(8)`을 주입하면 native 호출이 GIL을
유지해 감시 thread가 실행되지 않는다. 시험 부모 SIGKILL 후 3초 부재 검사가
두 진입점 모두 실패했다. 시험이 만든 부모만 종료했으며 자식은 유한 sleep으로
실패 경로까지 정리했다. 실제 ML 라이브러리가 이 현상을 일으켰다는 관측은 아니다.

공통 `run_json_worker`가 spawn 전 소유자 전용 pipe를 만들고 read FD만 모델 자식에
전달한다. writer는 원감독기에만 남고 정리/wait 이후 닫힌다. 자식은 모델을 읽기 전에
stdlib 감시 subprocess의 ready handshake를 요구한다. 감시기는 owner EOF 뒤 1.5초
기존 thread의 self-exit(74)을 기다린다. 아직 자기 kernel parent인 모델 자식만
SIGKILL하며 PID 검색·저장된 PID 복구·다른 실행의 종료는 하지 않는다. 정상 경로는
별도 stop pipe를 닫고 감시기를 wait한다. 시작 거부/잘못된 descriptor는 추론 전 거절한다.
감독기 취소는 여전히 소유 spawn/reap task를 settle한 뒤 owner pipe를 닫는다.

이는 a2c의 Python 자식 self-exit을 보존하면서 native fallback을 추가한 것이다.
DB 복구의 신호 금지·owner-token fence·원본/취소 사유·terminal 자동 재실행 금지는
그대로다. FD가 없는 직접 worker 호출은 기존 thread guard만 사용한다.
감시기는 모델 자식마다 잠깐 존재하는 경량 프로세스이며 신규 상시 데몬이 아니다.
감시기 메모리는 기존 각 모델 자식 RSS 기준의 범위 밖인 자손 메모리다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_native_owner_death.py -q --timeout=25 --tb=short
교정 전: 2 failed in 16.97s (두 native 자식의 부모 사망 후 3초 종료 TimeoutError)
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_native_owner_death.py unit_tests/test_model_process_lifecycle.py unit_tests/test_model_memory_budget.py -q --timeout=25 --tb=short
첫 교정: 30 passed in 9.02s
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_native_owner_death.py tests/test_research_model_resources.py -q --timeout=30 --tb=short
정상 출력/시작 거부·실제PG owner 복구 추가: 29 passed in 23.68s
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_native_owner_death.py unit_tests/test_model_process_lifecycle.py unit_tests/test_model_memory_budget.py tests/test_research_model_resources.py tests/test_research_memory_budget.py tests/test_local_stt_runner.py tests/test_local_stt_worker.py tests/test_tts_queue.py -q --timeout=30 --tb=short
최종 변경 묶음: 116 passed in 33.11s
$ .venv/bin/ruff check src/ttakkari/model_process.py src/ttakkari/model_process_guard.py unit_tests/test_model_native_owner_death.py tests/test_research_model_resources.py
All checks passed!
$ git diff --check
exit0
```

검사 수는 겹치므로 합산하지 않는다. PG owner-death의 추가 native 4건은 TTS/STT
각각 사용자 취소/owner_lost 경로다. fresh HTTP/별도 DB/Study resume에서 원본,
취소 사유, immutable recovery event, cleanup verified, adapter 재실행 없음과
중복 복구 없음을 검사한다. native 단위 반례는 별도 시험 sibling 생존도 확인한다.
정상 native 출력은 2초 GIL hold 후 그대로 반환하고 감시기 exit0/wait를 검증한다.
감시 시작 거부·non-pipe descriptor 반례는 thread-only로 조용히 내려가지 않는다.

추가로 기존 TTS Python3.11.16와 STT Python3.13.15에서 같은 공통 감독기를 호출한
모델 없는 native wrapper(2초 hold)를 실행했다. 두 자식 모두 process_exited=true,
monitor_exit=0이었다. 첫 보조 스크립트의 f-string JSON braces 오기로 ValueError가
나 추론/자식 spawn 전 중단됐고 문자열 구성을 교정했다. 제품 오류로 확대하지 않는다.
실제 모델/새 GPU 실행·벤치·운영 프로세스 신호는 없었다.

## 배포 마운트 경로 추적과 남은 범위

`execute_build` → `PhaserAdapter.work_dir` → `build_project`에서
`work_dir/build_id/src` 복사 후 install한다. `_egress`는 proxy script만 RO mount,
실제 install의 `_run_argv`는 stage dir을 `/work:rw`로 mount한다.
`stage_dir` 삭제는 build_project finally, `work_dir/build_id` 삭제는 adapter.build
await 뒤 execute_build finally다. 코드 순서만으로 설치 전 삭제를 확정하지 않는다.
기존 8bf 원문의 경로는 pytest tuple 생략으로 불완전하며 원인은 여전히 미확정이다.
기존 recorded-build 단독 성공은 전체 controller 순서의 실패를 지우지 않는다.

이번 1회 f519 status는 testing/error=null, ops summary 23행/25%/오류 section0이다.
활성 성공·G14 통과가 아니며 대기 poll/같은 실패 재시도는 하지 않았다.
이후 fetched origin/main70b70941에는 별도 collector 통합이 들어왔고 Research/
host_gpu_guard/model_process 소스 차이는 없었다. conftest 추가 35행은 paper live
provider 차단과 fake Muse fixture다. commit 후 병합하고 필수 push gate로 합성한다.

실제 ML owner-crash, 실GPU owner-crash, native 학습 checkpoint 재개, 전체/합산·OS
memory 상한, RuntimeLease·인증 운영UI/원UX10·legacy92/PMS7.1/2/4/5 및 전체
controller/배포 인수는 미완이다. 이 증거를 그 완료로 확대하지 않는다.
