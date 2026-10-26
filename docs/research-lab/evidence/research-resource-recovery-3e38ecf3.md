# 외부 GPU 자원 복구와 실행 소유권 관측

2026-10-26, 리드 Codex 직접 실행. backend 시작 revision은
`3e38ecf378256ae66fbd41bf76e5b202177f3443`이며 전체 목표는 active다.
PMS7.3의 남은 외부 자원 정리와 실행 상태 표시 일부를 다룬다.

## 배포·기존 증거

누적 controller `081f2c0b-d3a3-4953-ae07-2ad2186d1a16`을 이번 Run에서 한 번
조회했다. requested/skip_tests=false/error=null/generations였다. 이후 대기·폴링이나
같은 A 벤치는 실행하지 않았다. 기존 A~D/575필드·9건 소유권/6건 배포·105/9
증거는 원래 범위로 재사용한다. 변경된 코드의 관련 회귀만 새로 검증했다.

## 관측과 변경

1. 기존 `/runs`는 running 행만으로 host_gpu_lease/tts_lock/cpu_lock을 단정했다.
   현재는 OS flock으로 관측한 연구 실행 소유권만 반환한다. 취소 상태에서도
   정리 중인 살아 있는 소유자를 놓치지 않는다. 물리 GPU/TTS/CPU 잠금과 구분한다.
   최근 후보500개와 truncated/확인 불가 ID/관측 근거를 함께 반환한다.
2. GPU start와 launch identity 읽기 후 실행 중 checkpoint를 별도 transaction으로
   저장한다. 기존 어댑터 함수 인자는 바꾸지 않고 실행별 ContextVar sink를 쓴다.
   기록은 소유 토큰으로 fence하며 어댑터가 반환한 환경값으로 덮어쓰지 못한다.
   성공·예외·취소 뒤 환경과 raw·측정 저장에서도 checkpoint를 보존한다.
3. OS 소유 잠금이 해제된 경우에만 resource checkpoint를 검토한다. 개인 scope,
   llm_inference, 계약 버전·동일 소유 token·UUID launch·PID/start identity가
   확인된 기록만 기존 `_cleanup_owned_launch`/`HostGpu.stop(expected_launch_id=...)`
   경로에 넘긴다. 새 force-stop이나 PID만으로 죽이는 코드는 추가하지 않았다.
4. 다른 launch가 현재라면 기존 guard가 정리를 거절한다. 원래 프로세스가 absent로
   관측될 때만 정리 확인으로 기록하며 present/unknown은 unverified다.
   checkpoint 부재·잘못된 token·미식별 identity·TTS·다른 adapter/회사 scope는
   외부 정리 함수를 호출하지 않는다. 실행을 자동 재호출하지 않는다.
5. 사용자 취소가 먼저 저장된 terminal Run도 pending 자원 기록이 있으면 복구한다.
   원래 상태·사유·완료시각·원본은 그대로 두고 resource_recovery event와 별도 환경
   기록에 정리 결과를 저장한다. 미확인 결과를 같은 조건으로 자동 반복 재시도하지 않는다.
   복구 동안 accepted-work가 세대 퇴역을 보호한다.
6. GPU 임대 probe 실패를 False로 바꾸던 adapter→공통 readiness→API 경로는
   None을 보존한다. 설계실은 held=true/false/미확인을 구분한다. 실행 모니터는
   구서버 응답·조회 상한·소유권 미확인을 표시하고, 설계 미리보기의 자원은 계획값으로 표시한다.

## 반례·검증

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py tests/test_research_v2_api_contract.py -k 'resource_checkpoint or orphan_cleanup or does_not_infer or observes_live_owner' -q --timeout=10 --tb=short
교정 전 8 failed, 66 deselected in 1.03s
교정 후 8 passed, 66 deselected in 0.95s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py -k 'after_user_cancel' -q --timeout=10 --tb=short
교정 전 2 failed, 13 deselected in 0.69s (취소 후 외부 자원이 복구 대상에서 빠짐)
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py tests/test_research_runner_lifecycle.py -k 'after_user_cancel or owner_process_exit' -q --timeout=15 --tb=short
교정 후 3 passed, 55 deselected in 1.03s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py -k 'replacement or observation_error' tests/test_research_v2_api_contract.py -k 'replacement or observation_error or candidate_truncation' -q --timeout=20 --tb=short
5 passed, 77 deselected in 0.98s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_adapters.py -k 'persists_launch_and_cleanup' -q --timeout=15 --tb=short
1 passed, 52 deselected in 0.31s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_adapters.py tests/test_research_v2_api_contract.py -k 'probe_failure_stays_unknown or unknown_gpu_lease' -q --timeout=15 --tb=short
교정 전 2 failed, 122 deselected in 0.96s
교정 후 2 passed, 122 deselected in 0.72s
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_research_external_resources.py -k 'shared_readiness' -q --timeout=15 --tb=short
1 passed, 15 deselected in 0.56s
```

최초8건 중6건은 checkpoint 모듈/기능 부재, 2건은 API의 잘못된 잠금 추정이다.
중간 통합 runner/자원/API/adapter 173PASS8.67초 후 terminal 취소·GPU 미확인·
부정 사례를 보강했다. 따라서 최종 변경파일 인수는 뒤의 필수 push hook 영수증과
현재 source hash를 기준으로 한다. 묶음은 겹치므로 합산하지 않는다.

DB 검사는 주입된 시험 URL의 DB/user와 current_database/current_user를 대조하고
운영DB ttakkari를 거부하는 seed를 재사용했다. 살아 있는 실제 flock은 복구하지 않고,
시험 Python 자식 SIGKILL 후 잠금 해제를 확인하여 복구·event·fresh HTTP에 연결했다.
GPU와 프로세스 상태는 합성 host/probe다. 시험 자식만 중단했으며 실제 GPU/TTS나
운영 프로세스를 죽이지 않았다. `synthetic observation refusal`은 시험용 예외이고
현재 엔진의 실제 권한 거부 관측이 아니다.

## 아티팩트 환경 계약 회귀

추가 artifact/core/CPU 조합 검사에서8FAIL15PASS2.72초를 관측했다. 모두 기존
environment_snapshot == {engine:test} 두 assertion 위치에서 새 소유권 필드를
거부했다. 3e38ecf3 원문도 이미 owner.snapshot을 저장함을 대조했다. 실제 adapter는
engine:test를 반환했고 실패 출력은 그 값 보존과 추가 owner만 보여줬다. 중간의
ForeignKeyViolation은 원래 during_flush 주입 사례이며 이 실패의 공통 원인이 아니다.

테스트는 원본 engine·정확한 허용 key 집합, owner 계약·UUID token·timezone 있는
accepted_at·해제된 OS 잠금을 함께 단언하도록 갱신했다. raw·측정·아티팩트 및
기존 파일 보상 정리 assertion은 유지했다. engine 변조/owner 유실 부정 대조2개도
추가해 새 검사기가 손실을 허용하지 않게 했다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_scenario_b_artifacts.py -q --timeout=20 --tb=short
14 passed in 1.04s
$ .venv/bin/ruff check <변경 Python11개>
All checks passed!
$ git diff --check
exit0 (backend/frontend)
```

API의 기존16 lint 진단은 Query metadata의 Annotated 이동·미사용 import/변수
정리로, service/아티팩트 테스트의 기존 미사용 import 각3개는 제거로 해결했다.
전역 lint 기준·배포 기준·실패 처리 정책은 낮추지 않았다.

## 프론트

기존 main4677580 계정 쿼터 변경을 research-v2에 FF하고 연구 화면만 편집했다.
첫 frontend dc7ac7baab93db660ce2170d785f903041d4feac의 Production7329057358은
GitHub deployments/statuses의 success를 확인했다. 이후 같은 미확인 문제를
설계실·미리보기까지 교정한 누적 frontend는
`1a69dcd95da63036c4a5949b439d6035f2bc62ba`다. 현재 배포 상태는 별도 영수증으로 구분한다.

```text
$ pnpm test run src/pages/research/monitor-ownership.test.tsx
초기2FAIL→2PASS572ms, GPU 미확인 추가1FAIL1.63s→3PASS647ms
$ pnpm test run src/pages/research
6 files / 86 passed, duration3.90s
$ pnpm build
exit0 / tsc+Vite / PWA93 precache entries
$ pnpm lint
exit0 / ui-lint138건은 기준선138건 이하, 증가0
```

JSDOM 상호작용·build/lint이며 실제 운영 인증 브라우저 인수가 아니다.

## 남은 원조건

무 checkpoint의 시작 직후 crash, TTS/STT child identity·정리, native checkpoint
재개·실제 메모리 상한, 실제 운영 GPU cleanup/동일 A launch의 Metal layer,
인증 운영 UI·원문UX10·legacy92·PMS7.x 및 전체 controller gate는 미완료다.
현재 변경을 이 원조건 전체의 완료로 확대하지 않는다.
