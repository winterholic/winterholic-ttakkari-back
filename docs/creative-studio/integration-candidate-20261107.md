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

## 후속 정적 교정 — Run 52954c90

### 영수증과 시작 상태

제품 파일을 열기 전에 지원 CLI로 현재 Run을 조회했다. 상세 영수증은 다음을 반환했다.

```text
/Users/winterholic/bin/ttk get /api/sessions/d93c9153-076c-4479-9a65-3b61de244221/runs
→ status=running, id=52954c90-333e-4b33-971c-a500facc755d
/Users/winterholic/bin/ttk get /api/runs/52954c90-333e-4b33-971c-a500facc755d
→ status=running, profile=회사 코덱스 ai2 (ai2@hyperez.io), last_seq=15
```

착수 HEAD는 기준 `a3980e6b043df9c74a279f6717fc2e2ad9670e72`와 일치했다. branch는 `feat/creative-production-20261107` (`origin/main` 대비 ahead 3, behind 44)였고 tracked 변경은 없었다. 기존 untracked render/benchmark 자료 1,017경로는 그대로 보존했으며 착수 뒤 porcelain의 untracked 행 SHA-256은 `6026800ec3e3295d16cc69748d3eb841b0a76e8be13563df9fc43b0bcdad66fc`다. 후속 커밋에는 경로 지정으로 승인한 소스·테스트·스크립트·문서만 넣는다.

### Ruff와 소스 교정

기준 후보 Python 중 `src/`, `unit_tests/`, `tests/`, `scripts/`의 변경 경로만 Ruff에 넣었다. 기존 71건은 unused import, import 정렬, 미사용 값, EXE001 실행 비트, 불필요한 int/list 변환, `PLR0402` import 형식, BLE001 broad catch, TRY004 validation 예외 타입 경고였다. 안전한 import/정렬/불필요 변환과 미사용 이름을 제거하고 shebang 스크립트 두 개에 실행 비트를 설정했다. 광역 자동 수정이나 전역 규칙 변경은 하지 않았다.

예외 경계는 호출 계약에 맞춰 유지했다. `FrameAnimationAdapter`, `HyperFramesAdapter`, `Scene3DAdapter`의 preflight는 도구·권한·프로세스 실패를 `Preflight.missing` 또는 IPC `unknown`으로 반환하므로 해당 경계 catch에는 줄 단위 `BLE001` 예외와 설명을 붙였다. `scene_3d` audit의 JSON 파싱은 `JSONDecodeError`, 심볼릭 링크 생성은 `OSError`, 자막 폰트 fallback은 `OSError`만 잡도록 좁혔다. Kafka `_style_dna_contract`와 `_prepare_spec_project`의 다섯 입력 검사는 기존 `ValueError` 외부 계약(예: `unit_tests/test_hyperframes.py`의 ValueError 반례)을 보존하고 해당 다섯 줄에만 `TRY004` 예외를 명시했다. 예외 종류나 catch 폭을 바꾸는 회귀는 `47 passed`에 포함된다.

검사 명령과 결과:

```text
git diff --name-only 439472b3bc4665edc04ad1d7fa8ad1a9684a47b4 da0b83d0176d8bfe1352204014bcc5ddab69482d -- src/ unit_tests/ tests/ scripts/ | xargs env UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run ruff check --output-format concise
→ All checks passed!
git diff --name-only a3980e6b043df9c74a279f6717fc2e2ad9670e72 -- '*.py' | xargs -n 1 python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), filename=sys.argv[1])'
→ AST parse passed
```

첫 기본 `uv run`은 Codex 명령 샌드박스가 `/Users/winterholic/.cache/uv` 접근을 거부해 초기화 단계에서 끝났다 (`Operation not permitted`). 앞선 후보 검증과 같은 허용된 `/private/tmp/codex-uv-cache`로만 후속 실행했다. npm 로그 EPERM 검사는 재실행하지 않았고 기존 Codex 샌드박스 결과를 그대로 남긴다.

### 테스트 경로와 재사용

`pyproject.toml`은 pytest `testpaths = ["tests", "benchmark_tests", "unit_tests"]`를 사용한다. `tests/conftest.py`는 `import pg_sandbox` 절대 import를 하며 `tests/pg_sandbox.py`는 기준 commit에 tracked로 존재한다. 이 세션의 좁은 실행은 표준 경로를 명시해 통과했다.

```text
PYTHONPATH=src:tests:. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest --import-mode=prepend unit_tests/test_scene_3d.py unit_tests/test_creative_v2_frame_animation.py unit_tests/test_hyperframes.py -k 'not hyperframes_preflight_live and not hyperframes_lint_on_fixture' -q
→ 47 passed, 2 deselected in 2.86s
```

앞선 전체 통합 회귀 `244 passed, 1 failed, 4 deselected`는 이 정적 변경 전 `a3980e6...`에서 실행됐으므로 반복하지 않았다. 이번에 지문이 바뀐 frame animation/HyperFrames/3D 소스와 직접 회귀 테스트는 위 47개에서 재실행했다. 변경하지 않은 Stage2·Stage3·dispatch·interpolation 및 나머지 기존 bundle은 해당 후보의 기존 통과 결과를 재사용한다. 244 전체를 새 소스 해시 기준 통과했다고 주장하지 않는다.

`tests/test_scene_3d.py`는 `PYTHONPATH=src:tests:.`와 `--import-mode=prepend`로 별도 실행했으며 4개 통과, 1개 실패했다. 유일 실패는 실제 preflight에서 기존 Blender `ArchWarn: ARCH_CACHE_LINE_SIZE != Arch_...` 진단으로 필수 bpy 확인이 실패한 것이다. 이는 C 환경 차단의 재관측이며 이 명령 이후 같은 preflight/bpy를 다시 실행하지 않았다. unit 3D 계약 21개는 위 47개에 포함되며, 실제 C 렌더 성공을 뜻하지 않는다. `HyperFrames live preflight`와 npm lint 테스트는 재시도하지 않았다.

### 공백 결과와 원문 예외

이번에 편집한 Python·HTML·Markdown·JSON diff는 `git diff --check`로 통과했다. 전체 후보를 기준 `439472b3...`와 비교하면 공백 검사에는 여전히 원문 로그·patch와 vendor만 남는다. 따라서 전역 whitespace 통과로 보고하지 않는다. 아래 SHA-256은 변경 없이 보존한 기준 후보 commit `da0b83d...`의 원본 bytes 지문이며, 로그/patch는 실행 증거 원문, vendor는 외부 GSAP 복사본이므로 제외했다.

| 보존 경로 | 기준 원본 SHA-256 | 제외 이유 |
|---|---|---|
| `benchmark_tests/test_hf_minimal/vendor/gsap.min.js` | `92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb` | vendor 원본 |
| `templates/hyperframes/kafka_message_flow/vendor/gsap.min.js` | `92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb` | vendor 원본 |
| `docs/creative-studio/core-integration-evidence-20261107/inherited-asset-changes.patch` | `6f920fddd0cb604728fa4b6cac9beb1e566b136cf1785f3982be65b6bf77b56d` | 기록용 patch 원문 |
| `docs/creative-studio/core-integration-evidence-20261107/regression-final.log` | `b43111760ca9d00d7d153d2b691ce44cd898a8ab87bf51ce1cd0526708bbbc7b` | 테스트 원문 로그 |
| `docs/creative-studio/core-interpolation-evidence-20261107/frame_animation.py.patch` | `8336b2f075ef9c9b0949c6c584d5aca5be7220411405e1a736b95e002147bdc5` | 기록용 patch 원문 |
| `docs/creative-studio/core-interpolation-evidence-20261107/planner.py.patch` | `334649496bc0c1aeeb728647e6ad14504c8664b5ed49c88eccdd0b43dbcd9f9e` | 기록용 patch 원문 |
| `docs/creative-studio/core-interpolation-evidence-20261107/red.log` | `1bcbb2acd2872dfe5fd46b519daf069ee91b2743261a429e728568f2cb421e46` | 테스트 원문 로그 |
| `docs/creative-studio/core-stage2-evidence-20261107/a-style-bridge/core-style-bridge.patch` | `92a6945f7c59ff5157cf8a3838a8e6cb50672b7227069cd7db319ddb466dda66` | 기록용 patch 원문 |
| `docs/creative-studio/core-stage2-evidence-20261107/a-style-bridge/red.log` | `202c2bd153dc2ac65ae264b9b3dd980b3873831191e77517d7ef5d6303aa7bc1` | 테스트 원문 로그 |
| `docs/creative-studio/core-stage2-evidence-20261107/red-fixed-producer.log` | `c78df2d40dcd3cebe2839cee0f923ec252c468445ecee63dce18a1c125729cda` | 제작/검사 원문 로그 |
| `docs/creative-studio/core-stage2-evidence-20261107/red-hyperframes.log` | `e0887ae2a5481db2bc610709fe78f60abbb834bc4fd51a85d6264e04ecf0ea76` | 테스트 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/green-initial.log` | `9b50850407aca839a8fd350fe74ba9b8496acc6174b4f93249a079ee568cef2d` | 테스트 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/red-asset-file.log` | `108def67c8bdd6f590dcc431eac49328436d13a05c7fc66ced568b4bb4f8b336` | 실패 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/red-remote-api.log` | `0321a5b6ea1c29739ef20e06b80245f2257c3fddb1700c0463a2ec1781175aad` | 실패 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/red-timeout.log` | `079e870f483f18e8cbe1389b8d8c179393efb5f84d8d7a98586a41e172154a35` | 실패 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/regression-bundle.log` | `24f764c86cd3bbccb2eef0084fa9c59ad1eee2cc7b434e42476b957bcee4531f` | 테스트 원문 로그 |
| `docs/creative-studio/core-stage3-evidence-20261107/stage3-core.patch` | `c0c0916a0c6b22261e17e707ebad9cdc810bf7db9a35cbf641de7fd778569214` | 기록용 patch 원문 |

남은 공백 진단 경로는 위 17개뿐이다. evidence 경로 안에 Python 테스트 파일명을 인용한 로그 행이 있어도 원문은 수정하지 않았다.

### 후속 후보와 미완

정적 코드·테스트·스크립트·보조 문서의 경로 지정 commit은 `181d12e1d9ce03a3cfff79da3cef492d0d71c18a`다. 기준 `a3980e6...`부터 이 코드 commit까지의 binary diff SHA-256은 `eea39dc3a5d749b006c7cd54f7b17e2863de8b04a5b9eb28ca404788506199d4`다. 파일별 재사용 구분은 다음과 같다. `base`는 기준 후보 bytes, `new`는 정적 교정 commit bytes의 SHA-256이다.

| 경로 | base SHA-256 | new SHA-256 | 이번 회귀/재사용 |
|---|---|---|---|
| `scripts/render_3d_benchmark.py` | `980b5e050bdad7678bb9ad017f5a4414e84cf020a2156bfe2e602b04e9b51134` | `e022c445ba4c2dea2c8cda321a7070de6f09e8b5f90e52eb3491aa42e89bb54a` | 실행 안 함; lint 및 AST만 |
| `scripts/render_hyperframes_benchmark.py` | `691ff693d6cf5622c8249cea75c560ec9cc0404007159ab0a211ac6f0a7d633a` | `691ff693d6cf5622c8249cea75c560ec9cc0404007159ab0a211ac6f0a7d633a` | 내용 동일, shebang 실행 비트만; lint 및 AST |
| `scripts/run_frame_animation_benchmark.py` | `223b4038a3153bd9ff369242e45a111f07f4a075ec128d9261e0592be856f0d2` | `324657a2fa83df8d8f1e0833106508855f1bdff608f57b31d49258a2ab3c2e5c` | 실행 안 함; lint 및 AST |
| `src/ttakkari/creative/v2/frame_animation.py` | `dca9dbb1ecdbfba45ac23b3580b2ae087c86ed492f7d2c7892910ecbe027d5a7` | `e53f59ae99e537bbfbbfd7e99880f20c17a2273372a7a8c64ce42a2533012200` | frame animation 회귀 포함 |
| `src/ttakkari/creative/v2/hyperframes.py` | `4bd5579ef68379e75c98d84fe1f2827b593c30745c2e5872ce2506dde6d1c520` | `8dcdeb3f06abb972121faafa4e566a950c1619a0986308b09ad89f507302faa0` | HyperFrames 회귀 포함; live/npm 제외 |
| `src/ttakkari/creative/v2/scene_3d.py` | `b3787d2509d7dcf26e7b4745cb74802ba5ff31c93d96bd4a95ec3905644a9a85` | `715b36dde7673c1bbce832650081a62a2a590a75fb3a38a7cbe336aed5994851` | unit 3D 회귀 포함 |
| `tests/test_scene_3d.py` | `e77be7389898fdcf3ee21edada392975ba1705b4ca0b90c99434d6a9611cca90` | `54bd197555e9875c644352cde9830b7f5834730da08585e42ce3f600dac85219` | 4 pass, 1 known bpy preflight failure |
| `unit_tests/test_creative_v2_frame_animation.py` | `449c7cc5b7f338b1dbfe61212ef6b0c6948ed405e1cd2e4d285a658ced416f86` | `7eafcffdc66c909d0ee7e1293a85b102e578215a04ffb00478b7f0f2902a3140` | 47-test bundle에 포함 |
| `unit_tests/test_hyperframes.py` | `d05abe6384d0b59c8c06a776309b2f9f8d64bd059ea5aeac04cf323b3e89f366` | `b38815abd681a0e1b6ea2de8fffea54c8a977d1513158930f34e0055cf2a6447` | 47-test bundle; live/npm 2개 제외 |
| `unit_tests/test_scene_3d.py` | `e466055668ab682c5a95da3cc1f0ea60ea2795e595ca172a3a31ce42f6e5e690` | `d45fb66833413b2cd8fc7388a79da60bf327f130792294db231f3d974998f7b8` | 47-test bundle에 포함 |

최종 통합 문서를 포함한 full SHA는 다음 경로 지정 문서 commit에서 확정한다. 별도 신규 clean 검증 worktree는 원 `/back-creative-production-20261107-verify`를 보존하기 위해 `/back-creative-production-20261107-static-verify`에 제공한다.

미완: npm lint의 기존 샌드박스 `.npm/_logs` EPERM은 재시도/우회 없이 미검증으로 유지한다. A browser preflight, C bpy 렌더, 유음 조립, 의미·시청·청취 평가, 실제 UI, 독립 품질 검증, push와 배포는 이 정적 교정 작업에서 수행하지 않았다. 기존 무음 QA 실패 및 의미·시청·청취 `unknown` 상태는 그대로다.
