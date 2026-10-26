# 研究 실행의 모델 자식 checkpoint와 종료 관측

2026-10-26, 리드 Codex 명령 환경에서 직접 수행. 시작 revision
`a2c594f6241d7529d4d61a19577f21e19a1d7d4f`. 원목표와 PMS7.3은 active/doing이다.

## 이전 증거와 이번 범위

누적 배포 `838ae3e6-f2e9-461d-9613-8f28db604738`를 이번 Run에서 한 번
조회했다. testing/generations/error=null이었다. 이후 예약 대기나 폴링 없이 작업했다.
기존 A~D/UI/575필드·GPU checkpoint/소유권/프론트 증거는 원래 범위로 재사용했다.
모델 서버나 새 작업 세션을 만들지 않았다.

기존 run_json_worker의 수명주기 정리는 a2c의 유효 증거다. 하지만 연구 자원
checkpoint와 복구는 host GPU만 다뤄 TTS/STT 자식 종료와 연구 DB 연결이 없었다.
이번에는 공통 감독기에 선택적인 ContextVar 관측 콜백을 추가했다. 공통 모듈은
DB/설정/연구 코드를 가져오지 않으며 선택 런타임의 Python3.11 구문과 호환된다.

## 계약과 변경

- 연구 runner의 기존 resource_checkpoints 문맥에서만 두 알려진 TTS/STT 모듈의
  관측을 받는다. request payload나 원문·음성·인증정보는 checkpoint에 넣지 않는다.
- 자식 UUID·PID·부모 PID·가능한 경우 기존 proc_start 관측을 실행 중 별도
  transaction에 저장한다. 소유 token과 기존 Run row lock으로 fence한 뒤에만
  자식 stdin으로 요청을 전송하므로 checkpoint 거부 뒤 추론은 시작되지 않는다.
- research_model_workers JSONB 필드의 workers 맵은 row lock 아래서 병합한다.
  마지막으로 종료한 자식 하나가 다른 살아 있는 자식을 지우거나 released로
  표시하지 않는다. 기존 GPU 자원 필드는 별도로 보존한다.
- process handle의 kill/wait가 끝나면 returncode와 종료 관측을 기록한다. 이전
  생성/반복 취소 정리를 재사용하며 종료 기록도 같은 shield 보호 안에서 마친다.
  adapter 반환 환경이 보호된 기록을 주입하거나 덮어쓰지 못하게 했다.
- owner OS lock이 해제된 orphan과 사용자 취소 후 pending 자식도 복구 대상이다.
  token·contract·허용 모듈·UUID·PID를 검증하고 기존 process_state를 관측만 한다.
  종료 wait 또는 absent만 verified이고 present/unknown은 unverified다. PID에
  신호를 보내지 않으며 다른 프로세스/launch를 찾거나 중단하지 않는다.
- 기존 owner_lost 상태/원본 보존과 resource_recovery 이벤트를 재사용한다.
  사용자 취소의 상태·사유·완료시각은 바꾸지 않는다. 미확인 결과를 동일 조건에서
  반복 재시도하거나 adapter를 재실행하지 않는다.
- 종료 wait 후 관측 콜백만 실패한 경우에는 실제 완료 결과를 버리지 않는다.
  supervisor가 예외를 로그에 남기고 metrics.lifecycle_receipt_error에 종류를
  표시한다. 저장된 시작 checkpoint는 미정리 상태로 남아 부재 검증 전에는 해제를
  단정하지 않는다. 시작 checkpoint 거부는 여전히 fail-closed다.

schema/protocol/migration이나 프론트 배포 변경은 없다. 회사 scope의 TTS도 기존
실행 정책 아래 메타데이터만 관측하며 GPU의 개인 scope 제한은 유지한다.

## 실행 근거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_model_resources.py -q --timeout=20 --tb=short
교정 전 7 failed,9 passed in1.93s
교정 후 최초16개 16 passed in2.17s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_model_resources.py unit_tests/test_model_process_lifecycle.py -q --timeout=25 --tb=short
40 passed in12.32s (이후 병행 자식1개·종료기록실패1개 보강 전)
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_process_lifecycle.py -k exit_receipt_failure -q --timeout=15 --tb=short
교정 전1 failed,20 deselected in0.35s (완료 출력 대신 종료기록 OSError가 전파됨)
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_model_resources.py tests/test_research_external_resources.py tests/test_research_runner_lifecycle.py tests/test_research_adapters.py tests/test_local_stt_runner.py tests/test_local_stt_worker.py tests/test_tts_queue.py unit_tests/test_model_process_lifecycle.py -q --timeout=25 --tb=short
201 passed in20.96s
$ .venv/bin/ruff check src/ttakkari/model_process.py src/ttakkari/research/resources.py src/ttakkari/research/runner.py src/ttakkari/research/ownership.py tests/test_research_model_resources.py unit_tests/test_model_process_lifecycle.py
All checks passed!
$ git diff --check
exit0
```

새 DB파일21개와 공유 수명주기파일21개(기존19개 포함)는 최종201개 안에 들어 있다.
중간 결과와 합산하지 않는다. SQL current_database/current_user를 주입 시험 URL과
대조하고 운영 ttakkari DB를 거부하는 기존 seed를 재사용했다.

이번 actual owner-death4건은 별도 Python 부모에서 실제 Research runner와 공유
run_json_worker/guard를 실행하고 모델 없는 executable wrapper를 사용했다.
module 메타데이터는 TTS/STT지만 이 wrapper를 실제 Supertonic/Whisper 추론으로
인정하지 않는다. a2c의 실제 두 production main 검사와도 구분한다. 시험 부모만
SIGKILL한 뒤 해당 자식 PID 소멸·fresh HTTP Run 조회/Study resume 이벤트에서
부재 verified와 원본·사용자 취소 사유 보존을 확인했다. 살아 있는 owner는 복구하지
않고, 복구 재호출은0건이었다. 다른 PID/운영 프로세스에는 신호를 보내지 않았다.

present/unknown 및 잘못된 token/모듈/누락 PID는 합성 관측으로 검증했다. 모든
경우 복구가 os.kill을 호출하면 실패하게 했다. 병행 TTS/STT의 첫 자식만 끝난
상태는 owned/exited+running, 두 자식 종료 뒤 released이며 GPU 별도 필드가
보존됨을 fresh HTTP로 확인했다.

중간 owner-death 묶음1FAIL19PASS8.32초는 직접 복구 호출이0을 반환했는데
기존 periodic 경로의 복구1건 로그가 함께 나왔다. generation_service의 periodic
operation 경쟁으로 추정했다. 시험에서 해당 foreground task만 기존 복구 본문을
실행하게 해 순서를 고정한 뒤40PASS/최종201PASS였다. 제품의 periodic 복구나
실행 정책을 바꾸지 않았다. 초기 import 정리와 unused noqa 제거는 lint만 교정했다.

## 남은 원조건

실제 ML/native GIL 점유 중 감시 시점·실메모리 상한·native checkpoint 재개,
GPU의 checkpoint 없는 시작 직후 crash, 운영 동일 A launch의 Metal/별도
RuntimeLease·인증 UI·원문UX10·legacy92·PMS7.1/2/4/5와 전체 controller gate는
미완료다. child 기록 연결을 이러한 전체 조건의 인수로 확대하지 않는다.

커밋/원격 SHA·필수 push hook 및 새 누적 배포 수락은 별도 영수증에 남긴다.
수락은 운영 활성 성공이 아니다.

