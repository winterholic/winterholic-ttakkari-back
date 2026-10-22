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
- 가드 수정 14982a8: 모듈 로드 시 `pytest.importorskip("ttk_comfy.client")`. lab adapter를 PYTHONPATH에 둔 실행에서는 `test_comfy_i2v_input.py` 40 passed로 동일하게 실행된다. 게이트 환경에서는 그 모듈이 skip된다. 수정 후 같은 게이트: ruff 통과, 계약 묶음 `77 passed, 1 skipped`, 바뀐 테스트 13개 `141 passed, 3 skipped`, rc=0. 원문 `gate_fix.log`.
- 이 수정은 독립 검수를 받지 않았다. 게이트 환경에서 I2V 입력 계약 40개 테스트가 실행되지 않는다는 점이 trade-off이므로, 검수자가 (a) 이 skip 방식, (b) 어댑터를 PYTHONPATH로 제공하는 게이트 설정, (c) 모듈 vendor 중 하나를 고르게 한다.

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

1. 가드 수정 14982a8의 독립 검수(위 trade-off 선택).
2. 승인되면 `integ/g34-creative-engines-main-20261023`을 main에 fast-forward 또는 병합하고(그 시점 origin/main 재확인) full SHA로 배포 예약.
3. 호스트에서 실제 FFmpeg/TTS/DB 게이트는 컨트롤러 정식 테스트에서 확인.
