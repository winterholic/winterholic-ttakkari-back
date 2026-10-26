# GPU 시작 의도 기록과 배포 실패 관측

2026-10-26 리드 Codex 직접. 기준 backend `6a994c2b576b99ec8e982bfbe0d8826b4c2267fd`.
원래 Research Lab R1~16/PMS664a2c29 목표는 active이며 전체 완료가 아니다.

## 변경 범위

기존 LLM 어댑터는 `HostGpu.start()` 반환 후 현재 실행 신원을 읽어 DB에 기록했다.
이제 Run의 기존 owner-token fence/rowlock으로 UUID 시작 의도(`state=starting`)를 먼저
commit하고, 같은 UUID를 HostGpu의 내부 keyword 인자로 전달한다. StartRequest/API는
UUID 입력을 받지 않는다. 기본 호출은 기존처럼 자체 UUID를 생성한다.
시작 의도 commit 거절 시 subprocess 시작은 없다. 시작 후의 ID 불일치는 실패시키고
다른 실행을 채택하거나 벤치마크하지 않는다. timeout·owned commit 실패 시에도
같은 ID의 실행 신원을 관측한 경우에만 기존 expected_launch_id 정리 경로를 사용한다.
spawn 거절 시 supervisor.log 핸들도 context manager로 닫는다.

owner 사망 후 복구는 기존 OS 소유 lock 해제를 전제로 한다. 시작 의도만 남은
Run도 기존 복구 후보에 포함하되, owner token/contract/UUID와 현재 같은 ID의
PID·시작 시각을 검증한 뒤 기존 정리 함수를 사용한다. 다른 ID/불명/잘못된 신원은
미확인으로 남긴다. 관측한 신원은 recovery evidence에 보존한다. 사용자 취소 상태와
사유/raw를 보존하고 terminal 동일 조건의 자동 재시도·어댑터 재실행은 추가하지 않는다.

이는 합성 host 관측/실제 격리 PostgreSQL 인수다. 실제 GPU owner-crash나 native
checkpoint 재개, 장기 실행 및 전체 메모리 예산 인수로 확대하지 않는다.
새 worker·실제 모델/GPU 시작·벤치·stop·브라우저 실행은 하지 않았다.

## 실행 증거

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py -k intent -q --timeout=15 --tb=short
교정 전: 4 failed, 4 passed, 16 deselected in 0.66s
  시작 전에 RESOURCE_KEY 없음/commit 거절에도 start 호출/동일 intent 복구 0/terminal starting 누락
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_host_gpu.py -k 'precommitted or invalid_internal' -q --timeout=10 --tb=short
교정 전: 1 failed, 4 passed, 36 deselected in 1.12s (start keyword 부재)
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py tests/test_host_gpu.py -k 'intent or precommitted or invalid_internal' -q --timeout=15 --tb=short
교정 후: 13 passed, 52 deselected in 0.96s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_adapters.py -k llm -q --timeout=15 --tb=short
33 passed, 21 deselected in 0.59s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py -k poststart -q --timeout=15 --tb=short
2 passed, 24 deselected in 0.56s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py tests/test_research_adapters.py tests/test_host_gpu_launch_identity.py tests/test_game_build_failure_log.py unit_tests/test_game_build_failure_diagnostics.py -q --timeout=30 --tb=short
최종 변경 묶음: 88 passed in 8.52s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_host_gpu_launch_identity.py tests/test_game_build_failure_log.py unit_tests/test_game_build_failure_diagnostics.py -q --timeout=30 --tb=short
import/명시check=False 정리 후 해당 8 passed in 7.23s
$ .venv/bin/ruff check <변경 Python 10파일>
All checks passed!
$ git diff --check
exit0
```

검사 수는 겹치므로 합산하지 않는다. 새 HostGpu UUID 검사는 실제 subprocess 직전에
거절시키는 전용 `tests/test_host_gpu_launch_identity.py`에 있다. 기존 감독기 테스트
파일은 원래 바이트로 유지한다. autospec host fixture는 계획 ID를 고정하고,
별도의 DB/부정 대조가 실제 계획 ID 전달·불일치를 검증한다.

확대 시도 명령:
`PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py tests/test_research_adapters.py tests/test_research_runner_lifecycle.py tests/test_host_gpu.py -q --timeout=30 --tb=short`
Codex 명령 환경에서 `133 passed, 4 failed in 52.07s` 후 Ctrl-C로 중단했다.
실패는 기존 Supervisor 직접 실행의 ready 시간 초과 2건 및 `supervisor_error: OSError:`
2건이다. Supervisor.run은 포트 선정 뒤 swap_reader 등의 호스트 관측을 한다는 코드
대조까지만 했으며, 상세 원인/운영 실패로 확정하지 않았다. 이 환경의 기존 감독기
실행은 다시 시도하지 않는다. 중단된 전체 검사를 PASS로 표현하지 않는다.

## 별도 배포 실패와 진단 반례

`backend_deploy_status(8bf8f04a-6808-4536-b178-12d868218186)`는 failed다.
controller 릴리스 pytest의 G14 첫 API 빌드 설치가 exit127,
`docker: Error response from daemon: error while creating moun...ter/f13c2417-1497-437d-89dc-642200835582: no such file or directory`.
원문 ops range는 `deploy-mount-failure-8bf8f04a.json`에 보존한다.
1322 passed/10 skipped 후 실패했으며, 샌드박스의 감독기 실패와 다른 환경이다.
정확한 mount 경로와 원인은 pytest tuple repr의 중간 생략 때문에 아직 미확인이다.

기존 G13/G14의 `(error, log_tail)` 단언을 합성 긴 문자열로 실제 pytest에 전달하면
가운데 mount marker가 사라지는 반례 2건을 재현했다(2 failed in 8.80s).
별도 실제 BuildService+PG/합성 adapter 검사도 DB log_tail에는 원문이 있으나
로그 출력이 없어 1 failed in 0.75s였다. 최초 검사 import 경로 오기는
games.contract→games.adapter로 교정했고 제품 오류로 해석하지 않았다.
실행 중 모델 capacity 중단으로 이전 shell 결과를 재사용할 수 없어 해당
좁은 반례만 복원했다. 시작했던 shell ID는 새 Run에서 Unknown process id였다.

실패 빌드 서비스에 기존 bounded(20,000자) log_tail의 warning을 추가했다.
상태/설치 명령/마운트/통과 기준/격리 정책·G13/G14 본문은 변경하지 않았다.
새 검사는 실제 pytest가 그 서비스 branch 로그의 중간 경로를 보존함을 확인한다.
이것은 원인 관측 교정이며 mount 실패 해결이나 전체 G14 통과가 아니다.
새 누적 controller가 실패하면 원문 경로를 확보하여 다음 교정으로 연결한다.

## 동일 운영 A의 보존 Metal 증거

이번 host_gpu_status 응답은 `operating-A-metal-6a994c2b.json`에 보존했다.
request(model 4B/ctx4096/key q8_0/value f16), start1792992954.600136,
stop1792992958.460334, stopped/running=false/exit0를 기존 A treatment와 대조했다.
이전 A를 새로 실행하지 않고 보존 로그에서 다음을 읽었다.

```text
load_tensors: offloaded 33/33 layers to GPU
load_tensors: MTL0_Mapped model buffer size = 2571.63 MiB
llama_kv_cache: MTL0 KV buffer size = 98.00 MiB
```

이는 같은 A treatment의 host GPU path/Metal layer placement 증거다. 새 UUID
시작 경로, baseline의 layer, 독립 RuntimeLease·인증 UI·품질 향상 인수는 아니다.
runtime_report f92a8dfd-46eb-42a3-99b9-f551547cc5ee는 여전히 활성
e6b4fd0df9513058f9e30845c08f4b0c9172b095/API PID77451이었다.
새 제품 전체 활성으로 주장하지 않는다. main checkout의 무관한 dirty는 보존했다.

## 남은 원조건

실제 GPU 시작/owner-crash 인수, native checkpoint·장기복구, 전체 메모리/합산·OS상한,
연구별 RuntimeLease, 인증 운영 UI/원UX10, legacy92/PMS7.1·7.2·7.4·7.5와
controller 전체 gate/R4·7·12~16은 미완이다. 기존 A~D/575/실ML/프론트와
유효 검사들은 반복하지 않는다. capacity는 엔진 공급자 중단이며 제품 원인과 구분한다.
