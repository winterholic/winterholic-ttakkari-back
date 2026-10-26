# TTS/STT 자식 프로세스 생성·정리 경계

2026-10-26, 리드 Codex 명령 환경에서 직접 실행. 시작 backend revision은
`f0b9ee49790ff821de6b7403b0b1e197b0c05cf9`이다. 원목표 R4/7/12~16과 PMS7.3은
여전히 미완료다. 새 작업 세션이나 모델 서버를 만들지 않았다.

## 범위와 관측

기존 누적 배포 `975ce851-758b-400e-91f3-50813e6beedb`를 상태 조회 후
ops_deploy_log summary로 확인했다. testing/generations/error=null, 로그24줄,
27%까지의 출력에 오류 section0이었다. 전체 gate 통과나 운영 활성으로 해석하지
않으며 이후 같은 예약을 대기·폴링하지 않았다. 기존 A~D/UI/575필드·소유권9/
배포6·84/197·프론트86의 유효 범위는 재사용했다.

공통 run_json_worker는 자식 생성 await가 try/finally 밖에 있었다. 자식이 OS에
생성됐지만 핸들 반환이 지연되는 시험 경계에서 감독 태스크를 취소하면 자식이
살아 있었다. finally의 wait도 두 번째 취소로 중단될 수 있었다. 실제 시험
자식과 Event로 순서를 고정했으며 모델·GPU를 로드하지 않았다.

기존 TTS/STT main은 각자 진입 당시 getppid()를 소유자로 삼았다. 감독기가
spawn 전에 정한 부모 PID를 전달하고, 공통 start_parent_guard가 inference 전에
유효성/동일성을 검사하게 했다. 1·잘못된 값·다른 PID는 자기 자신만 exit74한다.
환경키가 없는 직접 실행은 기존 getppid fallback을 유지한다. STT의 중복 감시
구현은 공통 watch_parent를 재사용하며 기존 import surface를 보존한다.

spawn은 별도 Task로 소유하고 shield한다. 취소 중이어도 핸들을 받은 뒤 기존
process.kill()/wait()를 끝내고 취소를 전달한다. reap도 같은 방식으로 반복 취소에
보호한다. 이미 종료된 자식에 대한 ProcessLookupError만 허용하며 다른 오류는
실패로 남긴다. 별도 PID 검색·프로세스 열거·외부 child 강제 정리·새 큐는 없다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_process_lifecycle.py -q --timeout=15 --tb=short
교정 전 10 failed in 0.41s
교정 후 초기10개 10 passed in 0.10s
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_process_lifecycle.py -q --timeout=20 --tb=short
실제 부모 사망4건 추가 뒤 14 passed in 3.67s
$ PYTHONPATH=src .venv/bin/python -m pytest unit_tests/test_model_process_lifecycle.py tests/test_local_stt_runner.py tests/test_local_stt_worker.py tests/test_tts_queue.py tests/test_research_adapters.py -q --timeout=25 --tb=short
최종 119 passed in 9.65s
$ .venv/bin/ruff check src/ttakkari/model_process.py src/ttakkari/tts/worker_supertonic.py src/ttakkari/meetings/local_stt/worker.py unit_tests/test_model_process_lifecycle.py
All checks passed!
$ git diff --check
exit0
```

최종 묶음의 새19건은 spawn 중 취소1/2회·reap 중 재취소·부모 identity 부정6건·
감독 PID override 차단·정상/직접 실행4건·취소 뒤 spawn 실패·실제 부모 사망4건이다.
실제 사망4건은 두 production main에서 모델 함수만 stub한 별도 프로세스다.
부모를 SIGKILL하고 guard 시작 전/추론 중 자식 exit74와 그 PID 소멸을 관측했다.
동시에 띄운 무관한 시험 sibling이 살아 있음도 단언했다. 전체 호스트 프로세스를
열거하지 않았고 종료 신호는 이 시험이 직접 만든 부모/sibling handle에만 보냈다.
타임아웃/취소·정상 종료·비정상 exit의 기존 감독기 검사와 TTS 큐/연구 어댑터
회귀를 함께 확인했다. 중간 묶음은 겹치므로 합산하지 않는다.

## 시험 환경 오염 교정

최초 통합 묶음은 1 failed,118 passed in10.44s였다. 실패는 기존
readiness_axis_f 검사가 tool_integrated 대신 environment_unavailable을 받은 것이다.
단독 같은 검사1 passed in0.06s였다. 새 positive-control에서 STT main을 pytest
프로세스 안에서 직접 호출했으며 main의 disposable HOME/cache 환경 변경이 남았다.
호출부와 main의 os.environ.update 경로를 대조하고 positive-control의 os.environ을
copy로 격리한 뒤 같은 통합 묶음119PASS를 확인했다. 제품의 HOME/cache 정책이나
readiness 기준을 완화하지 않았다. lint import 정리도 필수 gate 전에 반영했다.

## 한계와 남은 조건

실제 ML/native 호출이 GIL을 붙든 경우의 감시 실행 시점, 실메모리 상한, native
checkpoint 재개, OS 부모 사망 뒤 연구 DB의 TTS/STT child 정리 영수증과 상태 연결,
GPU의 checkpoint 없는 시작 직후 crash 구간은 이 증거가 아니다. 공유 supervisor
경로의 수명주기 검증이며 실제 Supertonic/Whisper 품질·속도·운영 복구를 재측정한
것이 아니다. 운영 Metal/동일A launch·별도 RuntimeLease·인증UI·원문UX10·legacy92·
PMS7.1/2/4/5·최종 controller gate도 미완료다. schema/protocol/migration 변경은 없다.

커밋·원격 SHA·필수 push hook·후속 누적 배포 수락은 이 문서의 결과물과 별도
영수증으로 기록한다. 수락은 배포 성공이 아니다.

