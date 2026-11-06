# Creative backend 통합 후보 — 2026-11-07

상태: 후보 패키징 완료, 검증은 부분. 전체 제작 목표는 계속 active이며 이 후보만으로 독립 평가·운영 적용·유음 조립 완료를 주장하지 않는다.

## profile 영수증과 시작 지문

실행 환경은 Mac Studio의 Codex 명령 샌드박스, 로컬 backend worktree다. profile은 제품 작업 전에 지원 CLI GET으로 확인했다.

```text
/Users/winterholic/bin/ttk get /api/sessions/d93c9153-076c-4479-9a65-3b61de244221/runs
→ status=running인 현재 Run: f2905bec-7d02-4b56-b227-40a01d66de60
→ profile=회사 코덱스 ai2 (ai2@hyperez.io)

/Users/winterholic/bin/ttk get /api/runs/f2905bec-7d02-4b56-b227-40a01d66de60
→ status=running, profile=회사 코덱스 ai2 (ai2@hyperez.io), last_seq=10
```

착수 시 branch는 `feat/creative-production-20261107`, HEAD는 `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`였다. `git status --porcelain=v1 -uall`은 1,221행이었고 그 출력의 SHA-256은 `26ed2ae16d4b13ad4dbcc5a1a19f5462d8c7e55566959cdd06ca0ae36f7d0e27`이다. 상태에는 승인된 Creative 소스와 문서, 각 담당자의 렌더/프레임 산출물이 함께 있었다. 후보에는 코드·테스트·템플릿·문서만 경로 지정해 포함했다. main/frontend 또는 타인 산출물은 수정·삭제하지 않았다.

## 후보 범위와 revision

고정한 첫 패키지 커밋은 `da0b83d0176d8bfe1352204014bcc5ddab69482d` (`Freeze approved Creative backend integration candidate`)다. 이 문서를 추가하는 커밋이 최종 후보다. 최종 전체 SHA는 backend worktree에서 다음으로 조회한다.

```sh
git rev-parse HEAD
git show -s --format='%H %s' HEAD
```

후보에는 Core의 plans/dispatch/engines/API, Stage2 repair·review/신뢰 URL, Stage3 storyboard 및 durable consumer, B interpolation 전달, R16 recipe 연결 교정, quality와 승인된 HyperFrames/3D adapter·템플릿·전용 회귀, 이를 뒷받침하는 Creative Studio 기록을 포함했다. 포함한 경로는 `git show --name-status --format= HEAD`로 검토할 수 있다. HyperFrames template WAV는 템플릿 source asset이다.

대량 영상·프레임과 bench 출력, `outbox/benchmark_b_work/pose_interpolation_20261107/`, `bench/scene_3d_blender/`, `benchmark_tests/test_hf_minimal/out.mp4`, `templates/scene_3d/node_modules`, `.hyperframes` 생성 상태와 Python cache는 새 후보 commit에서 제외했다. 기존 B 영상/무음 최종 실패와 Stage2/3·보간 evidence는 기존 저장소 자료를 근거로 사용했으며 다른 Run outbox를 읽지 않았다. `templates/scene_3d`가 `three` 모듈을 import하는 점은 기록한다. node_modules는 묶지 않았으므로 별도 clean 환경에서 3D 렌더 의존성 확인은 미실행이다.

R16의 일반 내레이션은 교정된 R03 경로를 유지한다. `unconfirmed_hint`, render gate=false, 명시적 승인 요구도 그대로다. Stage2/3, interpolation bridge, 승인된 A/B/C/quality 파일만 포함하고 품질 기준·gate는 바꾸지 않았다.

## 통합 회귀와 검사

다음 대표 회귀를 현재 후보 소스에서 실행했다. `tests/test_scene_3d.py`와 `unit_tests/test_scene_3d.py`는 같은 모듈명이라 별도 실행 대상으로 나눴다.

```text
PYTHONPATH=src:. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest \
  unit_tests/test_creative_v2_stage2.py unit_tests/test_creative_v2_plans.py \
  unit_tests/test_creative_v2_dispatch.py unit_tests/test_creative_v2_core_api.py \
  unit_tests/test_creative_v2_core_integration.py unit_tests/test_creative_v2_stage3.py \
  unit_tests/test_creative_v2_interpolation_bridge.py \
  unit_tests/test_creative_v2_kafka_recipe_scope.py unit_tests/test_creative_v2_matching.py \
  unit_tests/test_creative_v2_registry.py unit_tests/test_creative_v2_quality.py \
  unit_tests/test_creative_v2_frame_animation.py \
  unit_tests/test_creative_v2_hyperframes_style_bridge.py unit_tests/test_hyperframes.py \
  unit_tests/test_scene_3d.py \
  -k 'not real_frame_producer_is_called_through_studio_dispatch and not engines_are_discovered_but_unsupported_contracts_fail_closed and not hyperframes_preflight_live and not binary_finders' -q
→ 244 passed, 1 failed, 4 deselected in 135.49s
```

실패 1건은 `test_hyperframes_lint_on_fixture`에서 `run_lint`가 npm 실행을 시작하고 `/Users/winterholic/.npm/_logs`에 쓰려다 이 Codex 명령 샌드박스에서 `EPERM`을 받은 것이다. 다른 캐시 경로·엔진으로 우회하지 않았으며 production/호스트 실패로 일반화하지 않는다. 실제 2D producer 호출과 HyperFrames live preflight/binary 탐지는 앞선 유효 증거 또는 알려진 차단 때문에 제외했다.

첫 일괄 수집은 동일한 `test_scene_3d` 모듈명으로 `import file mismatch`가 나서 수집 중단됐고 테스트는 실행되지 않았다. 별도 실행 `PYTHONPATH=src:. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest --import-mode=importlib tests/test_scene_3d.py -q`는 `tests/conftest.py`가 `pg_sandbox`를 import하지 못해 `ModuleNotFoundError`로 막혔다. unit test 쪽 3D 계약 검사는 위 244 통과에 포함된다. 의존성 설치나 테스트 설정 변경은 하지 않았다.

정적 결과:

- `uv run ruff check`를 후보 Python source/test/script 전체에 실행했을 때 71건 진단이 나왔다. source 파일만 좁혀 실행한 결과는 29건이었다. 이번 단위는 기존 변경 패키징이므로 자동 수정하지 않았고 독립 검증에 넘긴다.
- 후보 추가 diff의 `git show --check`는 exit 2, whitespace 진단 90개/21개 경로였다. 대부분 verbatim evidence log/patch, Markdown 줄바꿈, 템플릿/vendor 파일이다. 증거 로그·patch를 정리해 원문을 바꾸지 않았다. 따라서 whitespace-clean 후보는 아니다.
- 회귀에서 실제 렌더, 신규 benchmark, HyperFrames/browser, bpy, 새 모델 Run을 실행하지 않았다. diff 확인으로 발견한 whitespace를 성공으로 바꾸어 보고하지 않는다.

## 이전 검증 증거 재사용

| 변경 묶음 | 재사용한 유효 증거 | 이번 후보 검사 / 남은 경계 |
|---|---|---|
| Stage2 repair/review/result URL | `core-stage2-20261107.md`의 보존 구간·receipt·API 신뢰 경계 및 당시 14 전용 회귀 | 관련 Stage2/dispatch/API·통합 회귀 포함. 실제 independent evaluator와 UI 여정은 미연결/미실행 |
| Stage3 storyboard/consumer | `core-stage3-20261107.md`의 기존 실모델 결과·승인 revision·로컬 durable consumer 제작 기록, 19 Stage3 회귀 | lifecycle/consumer 및 API 회귀 포함. 운영 API/원격 executor 소비는 미검증 |
| B 보간 | `core-interpolation-20261107.md`의 승인 선택→ProjectSpec 및 192 producer 프레임. 무음 최종은 QA 실패 상태로 보존 | interpolation bridge와 B unit regression 포함. silent-master final은 성공으로 덮지 않았고 유음 조립은 후속 |
| Kafka recipe/R16 | `core-ui-observation-followup-20261107.md`의 동일 brief 로컬 전후 및 기존 74 회귀 | Kafka recipe/matching/registry 회귀 포함. 의도 미확인과 닫힌 gate 유지; 신규 실추론/운영 plan 변경 없음 |
| A/C·quality | A browser preflight·C bpy 차단과 기존 품질 unknown은 각 정본의 당시 환경 기록 재사용 | style/quality/3D 단위 계약 포함. browser/bpy 재시도·실제 A/C 렌더 없음; 시청·청취·독립 의미평가 unknown |

기존 B 9건·전체 benchmark 렌더는 반복하지 않았다. 현재 회귀는 소유 변경 bundle에 대한 통합 확인이며 전체 ENG/B/C 또는 콘텐츠 품질 통과 증거가 아니다.

## Clean 검증 worktree와 미완

첫 패키지 커밋은 `da0b83d0176d8bfe1352204014bcc5ddab69482d`이며, 이 문서를 포함한 최종 후보 커밋의 전체 SHA는 아래 명령으로 확인한다.

```sh
git rev-parse HEAD
```

별도 detached 검증 worktree 경로는 `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107-verify`다. 이 경로는 최종 후보 SHA를 checkout하고 `git status --porcelain`이 비었는지 확인한다. required source/API/test/template 경로는 `git ls-tree -r --name-only HEAD`에서 추적되고 실제 존재하는지 확인한다. 본 branch에 남은 프레임·영상·C 작업 디렉터리 및 제외된 산출물은 후보에 포함하지 않았다.

다음은 이번 후보에서 완료하지 않았다.

- 기존 성공 B 영상과 비무음 음원의 승인 계획 기반 유음 최종 조립 및 machine QA.
- 운영 API 반영, 원격 consumer 실행, 실제 로그인 Studio UI E2E.
- 자유 brief 첨부, 사용자 품질 목표, 자연어 수정 UI/해석, 범용 복원 및 독립 의미·시청·청감 평가.
- A/C 실제 렌더와 알려진 browser/bpy 차단 해소.
- 독립 검증, push, 배포, 서비스 재시작.

독립 검증은 이 후보의 전체 SHA 및 clean worktree에서 수행한다. 이 문서는 후보 준비를 기록하며 관리 중인 원목표의 완료 상태를 변경하지 않는다.
