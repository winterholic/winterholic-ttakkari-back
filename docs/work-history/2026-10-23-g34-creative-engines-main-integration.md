# 2026-10-23 G34 creative-engines-director main 통합 후보

## 결과 요약

독립 검수된 `feat/creative-engines-director` `291ecfc53986288e24f607cf749c37941bedf696`을 최신 `origin/main` `f69ee98de22a0cfee7d256f6cd4426a97f2c8483`에 통합했다. 통합 중 새 결함을 하나 발견해 **main에는 push하지 않고** 후보 branch에 보존했다.

- 후보 branch: `integ/g34-creative-engines-main-20261023`
- 병합 커밋: `229780ea9bab7b1f904816d145218412e68ca9dd` (parents f69ee98, 291ecfc)
- 게이트 가드 수정 커밋: `14982a852fb3fe8a79e1db2fbc54777acb623814` (테스트 한 파일, 검수 전)
- `origin/main`은 변경하지 않았고 배포는 예약하지 않았다.

## 병합과 충돌 해결

base 2522d78 이후 main 23 커밋, feature 6 커밋. 두 쪽이 함께 바꾼 파일은 두 개였고 둘 다 합집합으로 해결했다.

- `src/ttakkari/api/creative.py`: import 합집합(records `RecordsService`·`MEDIA_ID_PATTERN` + `EngineError`·`RegistryError`·`submit_feedback`·`production_record`). 기존 records 라우트(`/records`, `/records/{rid}`, `/records/{rid}/media/{media_id}`)와 새 engine/production 라우트가 모두 남았다. 라우트 정의 순서는 두 쪽 원본 순서를 그대로 이었다.
- `tests/test_route_inventory.py`: 인벤토리 union. auth 분류는 두 쪽 값 그대로(`engines`·`productions/{pid}/record` owner-plain, `artifacts/{sha}` public, records 3종 기존 분류).
- `creative/pipeline.py`, `direction.py`, `director.py`는 검수된 291ecfc와 바이트 동일. alembic/계약 파일은 feature가 건드리지 않았다.
- 291ecfc 대비 이 병합이 바꾼 creative/comfy 관련 파일은 main 쪽 변경(`api/creative.py`, `creative/records.py`, `tests/test_creative_records.py`, `tests/test_tts_real_engine.py`)뿐이다.

## 새 결함: 배포 게이트 수집 오류

`unit_tests/test_comfy_i2v_input.py`가 `from ttk_comfy.client import ComfyClient`를 무조건 import한다. `ttk_comfy`는 저장소 밖 lab 설치본이라 `git archive` 체크아웃(pre-push 게이트와 배포 게이트가 쓰는 방식)에는 없다. 수집 오류 하나가 pytest 전체를 중단시킨다.

- 병합 커밋 229780e에서 `scripts/verify_deploy_gate.sh --commit HEAD --base origin/main`: 계약 묶음 exit 2(`4107 deselected, 1 error`), 바뀐 테스트 파일 exit 2. main push 훅이 막았다. 원문 `gate_229780e.log`.
- feature branch 291ecfc 자체의 결함이며 병합 충돌과 무관하다. 배포 컨트롤러도 같은 체크아웃 방식이면 같은 곳에서 멈출 가능성이 크다(컨트롤러 실행은 미확인).
- 이전 인수 보고는 14982a8의 전체 모듈 `pytest.importorskip("ttk_comfy.client")`로 게이트가 `77 passed, 1 skipped`, 바뀐 테스트 13개 `141 passed, 3 skipped`, rc=0이었다고 기록했다. 이 PASS는 핵심 I2V 40개가 모듈 전체 skip으로 실행되지 않은 결과라 계약 검증 완료로 해석할 수 없다. 아래 후속 검증에서 이 해석과 수정 방향을 정정한다.

## 검증 (통합 SHA 229780e/14982a8 기준, 이 세션에서 실행)

- `tests/test_route_inventory.py tests/test_creative_api.py tests/test_creative_records.py`: `35 passed in 3.99s`.
- `unit_tests` creative/comfy 13개 파일(lab adapter를 PYTHONPATH로 제공): `177 passed, 2 skipped in 13.50s`.
- snapshot 안전 subset 11: `11 passed in 0.29s`.
- `ruff check` 변경 py 45개 통과. `git diff --cached --check` 통과.
- `ruff format --check`: 후보 branch가 가져온 3개 파일(`managed_comfy_receipt.py`, `test_comfy_managed.py`, `test_comfy_managed_protocol.py`)이 미정렬이다. 배포 게이트는 `ruff check`만 쓰고, 검수된 바이트를 보존하려고 정렬하지 않았다(포맷 후 AST 동일은 확인함). 스타일 부채로 남긴다.

## 미검증과 비범위

- 검수 harness `direct_retry_fake_e25.py`는 이 세션에서 보호 영역 가드에 막혀 읽지도 실행하지도 못했다. 해당 경로(`pipeline.py`/`direction.py`/`director.py`)가 검수 SHA와 바이트 동일하다는 근거만 있고 통합 SHA에서의 재실행은 미검증이다.
- 실제 FFmpeg를 여는 fixture 테스트(`test_creative_direction`, `test_creative_production_api`, `test_creative_review_real`, snapshot 3개)는 알려진 sandbox 거부(psutil/sysctl) 환경이라 실행하지 않았다. 실제 FFmpeg/TTS 모델/GPU/Comfy/DB-backed 경로와 전체 저장소 회귀는 미검증이고 영상 품질 PASS도 아니다.
- v2 카탈로그/의도추출 branch, frontend, lab 설치본, registry, 모델 다운로드는 건드리지 않았다. I2V는 prepared-unverified/explicit-lab 그대로이고 검증 안 된 profile을 자동 선택으로 켜지 않았다.

## 부수 효과와 보존

- 격리 worktree `/tmp/back-g34-integ` 에서만 작업했다. `/tmp/back-g34`(구현자)와 공유 `back`은 읽기만 했다.
- 공유 `back`: HEAD `607f8bda…`, status 항목 51개 그대로. source ancestry 정렬은 main이 갱신되지 않아 필요 없었고 수행하지 않았다.
- 이전 예약: 422d59a9는 `superseded`(28e7b324로 대체). 28e7b324(75a12d6)는 `draining`이었다(조회 시점). 이번에 새 예약은 만들지 않았다.
- 증거 원문은 이 Run의 outbox `g34-integration/`에 있다.

## 남은 일

1. 새 multipart 계약 테스트와 병합 union을 기존 독립 검수 세션에서 확인한다.
2. 확인이 끝나면 `integ/g34-creative-engines-main-20261023`을 main에 통합하고(그 시점 origin/main 재확인) full SHA로 배포 예약한다.
3. 호스트에서 실제 FFmpeg/TTS/DB 게이트는 컨트롤러 정식 테스트에서 확인한다.


## 2026-10-23 후속 정정: 필수 I2V 계약을 archive 독립으로 복구

사용자는 모듈 전체 skip 안을 채택하지 않고, 핵심 I2V 계약은 lab 없이 반드시 실행하며 실제 설치본 상호운용성만 선택 테스트로 분리하도록 결정했다. production 소스와 `verify_deploy_gate.sh`, skip 정책은 변경하지 않는다.

- RED (`red-i2v-14e58b5.log`): 게이트가 만든 보존 archive의 `.venv/bin/python`에서 `find_spec('ttk_comfy')`가 `None`; `test_comfy_i2v_input.py`는 `SKIPPED [1] ... No module named 'ttk_comfy'`, `pytest_exit=5`로 전부 수집되지 않았다.
- 변경: 핵심 테스트의 최상단 외부 import/전체 importorskip 제거. restore-race 테스트를 path-only client (`base`만 있고 `upload_image_bytes` 없음)로 실행해 controller의 실제 multipart 직렬화와 HTTP transport의 최종 `image` part SHA/filename을 확인한다. 실제 lab ComfyClient 사용 검사는 별도 `test_comfy_i2v_lab_interop.py` 함수로 분리하고 그 테스트 안에서만 `importorskip`한다.
- GREEN (`green-i2v-overlay.log`): 같은 git archive runtime/venv에서 `ttk_comfy None`, 핵심 모듈 `40 passed`, optional interop 단독 `1 skipped` (lab 부재 사유), Ruff 통과. 현재 환경에서 lab 설치본 interop 실행은 미실행이다.
- 음성 대조 (`negative-control-invalid-sha.log`): disposable archive에서 sha256 비교를 의도적으로 비활성화하자 `test_validator_rejects_tampered_empty_nonimage_truncated_and_oversized_inputs`가 `DID NOT RAISE InputImageError`로 실제 실패(exit 1). archive 소스는 실행 후 바이트 복원했다.
- 새 revision 전 기본 게이트 (`red-gate-14e58b5.log`)는 계약 묶음 `77 passed, 1 skipped, 4107 deselected`; 변경 테스트는 `22 failed, 113 passed, 3 skipped, 6 errors`. 실패는 ffmpeg smoke의 `psutil` PID 열거에서 `PermissionError: [Errno 1] Operation not permitted (originated from sysctl() malloc 1/3)`로 끝나는 알려진 Codex 명령 sandbox 거부였다. 같은 시스템 호출을 재시도하거나 우회하지 않는다. 새 SHA에서 전체 impacted gate는 이 이유로 미실행 상태이며, 테스트 자체의 단독 archive GREEN으로 이를 대체 표기하지 않는다.
- 수정 diff는 테스트 2개 파일뿐이며 기존 3개 Ruff format 정렬 부채, G0/G1/공유 lease, API merge union과 291ecfc production bytes는 손대지 않았다. main push/배포 예약은 하지 않는다.
