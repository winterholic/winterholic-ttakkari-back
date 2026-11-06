# Creative Studio core 통합 — 2026-11-07

후속: 실제 부분 수정 실행·신뢰된 검수 수용·전후 증거는 [core stage 2](core-stage2-20261107.md)에 추가했다. 아래는 이전 단계 실행 시점의 기록이다.

상태: **부분**. 로컬/host executor에서 호출할 제작 경로, 실제 2D 제작·혼합 합성, 원본 영상 모션 보존, API 및 부분 수정 회귀를 연결했다. HyperFrames·3D 실제 렌더, 원격 executor 소비자 연결, 실제 LLM 감독 추론·독립 시청 품질 검수, 통합 배포는 완료하지 않았다.

이 문서는 구현 인계이며 독립 감사 판정이 아니다. 기준 전체 HEAD는 `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`. core 변경은 **미커밋**이다. 작업 중 다른 담당자의 frame-animation 커밋이 나타났으며 보존했다. 이전 증거 문서들의 `5f5cbd997addcb4ea084ab908790f8f6eec54122`는 과거 기준이다. 실행별 입력 해시와 현재 dirty 소스 해시는 [증거 색인](core-integration-evidence-20261107/source-snapshot.json)을 함께 봐야 한다. HEAD 하나가 이번 변경 전체를 식별하지 않는다.

## 1. 중지된 core 지시 복원과 소유 범위

정본: [account-reassignment-20261107.json](account-reassignment-20261107.json). `86c93b9b-7d99-4814-b83c-0dd77c453b2c`의 원래 실행 1건과 대기 4건을 [원문 복원 JSON](core-integration-evidence-20261107/restored-core-instructions.json)에 보존했다.

| Run | 보존된 요구와 처리 |
|---|---|
| `c12a7f78-1650-421f-8568-3adc871430f7` | 다중 엔진·원본 모션·전략/대안·승인→실행→QA 경로. 아래 구현/미완료로 분리 |
| `004150f5-cb19-4527-b673-de8bd49d347c` | 통합 소유권, 구현자 증식 금지. 새 worker를 만들지 않음 |
| `a5b7f52b-eb01-4ea4-8029-3460010681e7` | 전체 SHA·명령·실제 결과·미완료·접근 가능한 증거. 이 문서와 저장소 증거에 기록 |
| `f9205110-44b3-4495-b467-243afecfd094` | 2D adapter의 실제 render 호출. 새 스튜디오 dispatch 통합에서 호출함 |
| `5875c2c0-8963-40ce-8812-28773673ece9` | 3D 등록과 의미 수행 구별. 명시적 crystal/orbit 프리셋만 허용하고 실제 환경 실패는 차단 |

시작 때 `git diff --stat`은 자산 생성 파일 두 개의 **175 insertions, 13 deletions**였다. [인계된 원본 diff](core-integration-evidence-20261107/inherited-asset-changes.patch)의 SHA-256은 `6f920fddd0cb604728fa4b6cac9beb1e566b136cf1785f3982be65b6bf77b56d`. 원본 영상 보존·카탈로그별 prompt 변경을 유지하고, 등록 종류·preview·실제 compositor·증분 생성 연결을 덧붙였다.

수정 소유 파일은 `engines.py`, `dispatch.py`, `plans.py`, `asset_generation.py`, `asset_generation_managed.py`, `api/creative_v2.py`, 회귀 테스트와 이 통합 문서/증거다. 기존 creative 전용 `planner.py`가 없어 `creative/v2/planner.py`를 추가했다. 일정 관리의 `api/planner.py`는 관련 파일이 아니므로 수정하지 않았다. 제작자 3종·quality·프론트는 읽기만 했다. 다른 담당자의 동시 변경도 되돌리지 않았다. commit/push/배포·감사 세션 통지는 하지 않았다.

## 2. V1·기존 v2와 요구사항의 차이

판정 기준은 [요구사항 정본](../design/creative-studio-v2-requirements.md), [첫 수직 슬라이스 설계](../design/creative-studio-v2-design.md), [ADR 0065](../adr/0065-creative-studio-catalog-and-longform.md)다. 아래 수치는 `core-engine-blocker-probe-20261107.py`가 현재 저장소를 읽은 값이며 호스트 전체/배포 환경의 능력 판정이 아니다.

| 항목 | 실제 구현 근거 | 분류·남은 차이 |
|---|---|---|
| V1 포맷·장면 조립 | `creative/catalog.py:validate_project_spec`, `service.py:Studio.edit/enqueue`, `render.py:scan` | 구현 유지. V1 3~240초와 포맷 규격은 그대로. 새 장면 합성기가 V1 포맷의 최소 3장면 규칙을 전역 완화하지 않음 |
| 기획·길이·승인 | `v2/intent.py`, `matching.py`, `duration.py`, `plans.py:_render_authorized` | 구현 유지. 키워드 힌트는 확인 전 실행 불가. 승인 hash·revision·현재 capability snapshot을 계속 대조 |
| C01~C12 | `catalog_data.py:CATALOG_NAMES/ENTRIES` | 12개 카탈로그·166개 항목. 카탈로그 존재는 해당 장르 제작 성공이 아님 |
| Tool/Model/Skill/Recipe | `registry.py:Registry.recipe_matrix`, `registry_data.py` | R01~R21 존재. 현재 snapshot 판정은 studio 1(R21), 조합 미실행/능력 실행 1, 사례 1, unverified 8, blocked 10. 새 adapter 등록으로 기존 registry를 승격하지 않음 |
| Style DNA | `style_dna.py`, `dispatch.py:_style_defaults`, managed `_resolve_style_directives` | 콘텐츠 15필드+version 관리·불변 저장·상속/충돌/고정은 구현. prose·palette 전체가 모든 제작자에 적용되는 것은 아님. native 합성은 자막/전환을 적용하고 추가 이미지 카메라 모션은 ignored에 기록 |
| 장면/샷/레이어 | `v2/planner.py:apply_direction`, `plans.py:render_plan` | 명시적 전략·근거·대안·레이어 도구·1장면 1샷을 연결. 여러 독립 샷/레이어의 일반 렌더·Sequence 편집기는 미구현 |
| LLM 의도·전문 지식 | `run_extraction.py`, `extraction.py`; 신규 reviewed director 입력 | 기존 의도 Run 경로 유지. 검토된 감독의 model/source·의도·전문 지식·스토리보드는 승인 snapshot에 포함. 이번 작업은 LLM 추론 실행이나 전문 지식의 사실 검증을 하지 않음 |
| 제작 이력 | `creative/records.py:RecordsService`, `v2/dispatch.py:ProductionStore` | RecordsService는 g0/v1/lab, v2는 plan 폴더의 production.json. 새 v2를 전역 RecordsService·library·ML 이력에 자동 편입하는 작업은 미완료이며 소유 범위 밖 |

우선 조치: executor 소비자 연결과 실제 제작 가능한 엔진 환경, registry 근거 승격, 실제 감독/시청 검수. 확인만 할 것: 기존 첫 설계의 오래된 “렌더 미연결”·“20개 레시피” 수치는 현재 구현 상태와 다르다. 이 역사 문서 전체는 이번 범위에서 다시 쓰지 않았다.

## 3. 연결 계약과 동작

### 감독 입력 → 결정적 계획

`POST /api/creative/v2/plans`와 `PATCH /plans/{pid}`에 선택적 `director: ProductionDirection`을 추가했다. 기존 body는 그대로 유효하다. `reviewed=true`, LLM 출처의 model, 중복 없는 scene/shot ID와 계획 장면 집합 일치를 검사한다. 아직 검토하지 않은 LLM 결과·알 수 없는 필드·capability ID는 거부한다. 감독 입력이 검토됐다는 사실과 실제 plan 승인/실행 권한은 별도다.

각 장면에 `strategy`, `selected_engine`, `preset`, `strategy_rationale`, `shots`를 기록한다. 샷에는 action·camera/motion·필수 능력·layer_tools·fallback 대안·비용/지연/품질 판단을 둔다. 비용/지연은 측정하지 않았으면 `measured=false`다. 임의 립싱크 등의 필수 능력을 프리셋 영상 재생으로 충족했다고 하지 않는다. 전략 변경도 revision을 올리고 승인을 닫는다. 자동 대안 전환은 없다.

### 엔진별 실제 허용 표면

| 전략 / adapter | core 호출과 실제 한계 | 이번 실행 |
|---|---|---|
| slideshow / `skill.v1_ffmpeg_slideshow` | 기존 계획은 V1 큐. director 계획은 독립 단일 장면 합성 | 기존 회귀 및 혼합 합성 |
| generative_video / `skill.comfy_video_renderer` | managed가 만든 원본 MP4를 video로 등록·디코드. 이 adapter가 모델을 새로 생성하지 않음 | 실제 MP4 합성·HTTP 승인/dispatch/render. 생성 모델 경계는 fixture로 대체한 회귀와 구별 |
| frame_animation_2d / `skill.frame_animation_2d` | 현재 고정 캐릭터 walk 프리셋 1장면, 실제 validator의 8~15초 범위 | 실제 제작자 호출, 8초 source와 24초 최종 혼합 영상 |
| scene_3d / `skill.studio-3d-cinematic` | crystal_orbit 1장면만. editorial spec을 strict producer payload로 투영. Style DNA prose·shots는 적용했다고 하지 않음 | 승인된 PlanStore→dispatch에서 Blender 사전검사 차단. 실제 Blender 제작 미실행 |
| code_motion / `skill.hyperframes` (별칭 `skill.hyperframes_motion`) | 16:9, Kafka producer/broker/consumer 3장면만. 연속된 코드 모션 3장면을 한 구간으로 계획 | 승인된 PlanStore→dispatch에서 browser IPC blocked. 브라우저 실행 없음 |

`engines.load_adapters()`는 제작자 모듈을 지연 탐색한다. V1 제공자를 고를 때 선택 모듈을 먼저 import하지 않는다. 2D의 import 시점 글꼴 경로가 V1 글꼴 실패 검사에 오염되던 반례를 재현하고 이 경계에서 교정했다. 브라우저 IPC/3D 사전검사의 실패는 같은 source 지문에 대해 프로세스 안에서 보존해 반복 실행을 피한다. 환경/의존을 바꾼 후에는 새 실행 프로세스에서 다시 확인해야 한다.

원본 영상은 `kind=video`, `source_video_file`, preview는 별도 `preview_image_file`로 남는다. V1 경로에서도 Studio에 `kind=video`로 전달한다. native compositor는 원본 동영상 프레임을 디코드하고 자막과 master audio를 합친다. 출력은 24fps H.264로 정규화한다. 짧은 원본은 명시된 장면 길이에 맞춰 반복 재생되므로 길이가 늘었다고 새로운 액션을 생성한 것으로 보지 않는다. FFmpeg 입력에는 V1과 같은 로컬 protocol/format 제한을 유지한다.

## 4. QA와 부분 수정

- dispatch 시 `rubric`을 만들고 dispatched event에 rubric SHA와 `before_render` 시점을 기록한다. 최종 합성 뒤 `quality.evaluate_production`을 호출해 `quality_report`·`repair_plan`을 production.json과 상태 API에 노출한다.
- 기계 QA 통과는 기존 호환 상태인 `state=succeeded`/`final.status=qa_passed`다. 별도 semantic/viewing evidence가 없으면 `quality_report`의 해당 판정과 overall은 `unknown`이다. 이 의미를 최종 시청 품질 pass로 확장하지 않는다.
- `POST /plans/{pid}/production/repair-plan`은 scene/shot/layer 대상과 보존·무효화 목록을 기록하는 제안이다. 자산을 지우거나 자동 재제작하지 않는다. 실제 변경은 기존 edit→approve→dispatch/render를 거친다.
- 장면/음성/음악 SHA·구간 입력·engine version·compositor source hash가 실행 키에 들어간다. 새 revision에서 동일한 성공 구간은 출력 hash까지 확인해 재사용한다. native 출력의 소실/변조는 stale로 전환한다.
- managed 영상 생성은 장면 prompt·negative prompt·aspect·version·소스 지문에 묶인 원본/preview checkpoint를 둔다. 한 장면 변경 후 성공한 두 장면의 Comfy 호출과 원본 hash를 보존했다. TTS의 기존 캐시와 GPU/resource lease 경로는 유지한다. 기존 생성 레코드와 영상 등록 계약을 구별하도록 managed adapter version을 올렸다. 완료된 사용자 제작을 이번 세션에서 다시 생성하지 않았다.

## 5. 실제 명령과 관측

모든 `uv` 명령은 `UV_CACHE_DIR=/private/tmp/codex-uv-cache`, 필요한 명령은 `PYTHONPATH=.`를 사용했다. 아래 경로의 `<OUT>`은 이번 Run의 `/Volumes/Backup/ttakkari-data/outbox/62b89b5b-23cb-4b71-8858-06ed027e1ba7/core-integration`이다.

1. 기존 좁은 회귀:

```sh
PYTHONPATH=. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_dispatch.py unit_tests/test_creative_v2_asset_generation.py unit_tests/test_creative_v2_asset_generation_e2e.py unit_tests/test_creative_v2_run_scope.py -q
```

관측: `69 passed in 138.86s`. [원문](core-integration-evidence-20261107/regression-initial.log).

2. 실제 2D 제작자→스튜디오 최종 출력:

```sh
PYTHONPATH=. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest unit_tests/test_creative_v2_core_integration.py::test_real_frame_producer_is_called_through_studio_dispatch --basetemp=<OUT>/pytest-frame-integration -q
```

관측: `1 passed in 6.20s`. 최종 `24.02s`, `1280x720`, `24fps`, 영상/음성 트랙 있음. SHA-256 `f32992e1882bfd59fb0b5efb8dc550fa9de550510ffbed44d9369039898717d3`. 공유 파일 ID는 `205fa209-ca16-4575-86b8-63ee4b78f30e`다. 이 영상은 8초 2D 프리셋+합성 이미지 2장+시험용 tone으로 구성한 통합 증거이며 완성 콘텐츠나 청감 검수 결과가 아니다. [계획](core-integration-evidence-20261107/frame-plan.json), [제작 기록](core-integration-evidence-20261107/frame-production.json), [제작자 입력](core-integration-evidence-20261107/frame-engine-input.json), [명령 출력](core-integration-evidence-20261107/frame-dispatch.log). 이미 생성된 이 제작자를 뒤 회귀에서 제외해 반복 제작하지 않았다.

3. 확대 회귀와 발견한 실패:

```sh
PYTHONPATH=. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_dispatch.py unit_tests/test_creative_v2_asset_generation.py unit_tests/test_creative_v2_asset_generation_e2e.py unit_tests/test_creative_v2_core_integration.py unit_tests/test_creative_v2_core_api.py -k 'not real_frame_producer_is_called_through_studio_dispatch' --basetemp=<OUT>/pytest-final -q
```

관측: `1 failed, 80 passed, 1 deselected in 154.19s`. 실패는 임시 글꼴 경로가 선택 모듈 import에 고정된 문제였다. 이를 숨기거나 전체 통과로 합산하지 않는다. [원문](core-integration-evidence-20261107/regression-final.log).

원인 교정 후 V1 글꼴 실패→제작자 실패 격리, 3D strict payload, V1 지연 import의 해당 4검사만 재실행: `4 passed in 2.35s`. [원문](core-integration-evidence-20261107/regression-contract-fixes.log).

4. 새 경로 추가 검증:

```sh
PYTHONPATH=. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run pytest unit_tests/test_creative_v2_core_integration.py unit_tests/test_creative_v2_core_api.py -k 'not real_frame_producer_is_called_through_studio_dispatch' --basetemp=<OUT>/pytest-addendum -q
```

관측: `16 passed, 1 deselected in 23.90s`. 모션 반례는 같은 출력의 0.3초/0.7초 상단 영역 frame MD5가 다름을 확인한다. 자막/켄번즈로 생긴 차이로 통과시키지 않는다. 장면 수정 후 다른 성공 구간의 run_key·출력 SHA·자산 집합 보존, managed Comfy 호출 `3+1`(전체 재생성이면 `3+3`) 반례, unsupported lipsync, 제작자 실패 시 무대체, native 출력 stale, 새 승인 필요성을 포함한다. [원문](core-integration-evidence-20261107/core-addendum.log).

HTTP 검증은 auth 경계와 테스트용 owner override 아래에서 새 plan→원본 영상/음악 upload→approve→production→render(202, succeeded)→부분 repair 제안까지 실행했다. 모델·브라우저·DB 추론 경계는 실행한 것으로 보지 않는다. 마지막 protocol 제한/실제 HTTP 검사는 `2 passed in 4.11s`; [원문](core-integration-evidence-20261107/protocol-http-final.log). Kafka 실제 표면의 metadata 검사는 `1 passed`; [원문](core-integration-evidence-20261107/hyperframes-contract.log).

5. 엔진 실패 차단:

```sh
PYTHONPATH=. UV_CACHE_DIR=/private/tmp/codex-uv-cache uv run python docs/creative-studio/core-engine-blocker-probe-20261107.py <OUT>/engine-blockers
```

관측: 두 계획 모두 승인 후 `render_gate=true`였으나 dispatch에서 명시적으로 차단됐고, dispatch 레코드와 V1 fallback 프로젝트는 각각 0개다. HyperFrames는 `browser IPC blocked; no browser launched`. 3D는 Blender 확인의 `ArchWarn: ARCH_CACHE_LINE_SIZE != Arch_ObtainCacheLineSize()` 및 import/version 확인 실패였다. 이 Codex 명령 샌드박스의 관측이며 호스트 전체나 배포 backend 장애로 일반화하지 않는다. [입력 해시/원문 결과](core-integration-evidence-20261107/engine-blockers.json).

지정된 read-only browser preflight도 실행했으며 `status=blocked`, `browser_launched=false`, mach-register/mach-lookup denied를 반환했다. 브라우저 바이너리나 sandbox 설정을 바꿔 재실행하지 않았다. 통합 중 HyperFrames 담당자 변경으로 공통 render 함수가 추가되기 전 상태와 달라졌고, 최초 delegate preflight가 `npx hyperframes doctor`의 npm cache EPERM을 반환한 관측도 있었다. 이후 core에서 IPC를 먼저 확인해 doctor/렌더 진입을 차단했다. npm 캐시 권한을 고치거나 다른 경로로 우회하지 않았다.

## 6. 총괄에 넘길 계약 충돌·미완료

| 항목 | 현재 상태와 다음 연결 |
|---|---|
| 원격 API / executor | director native 렌더는 `queued_work.delegated()`인 API 프로세스에서 명시적 거부. executor의 durable 소비자가 승인된 plan ID/revision으로 `dispatch.request_render`를 호출하는 연결은 이번 소유 파일만으로 추가하지 않았다. 실행부/consumer 소유자가 이어야 함 |
| registry rich recipe | 레지스트리 근거를 바꾸지 않았다. R01~R20의 미충족 능력을 R21 compositor로 대체하지 않음. 총괄이 실제 각 능력의 제작 증거를 확인한 뒤 snapshot/registry 승격 필요 |
| HyperFrames | 초기 공통 render 부재는 담당자 변경으로 보완됨. 실제 표면은 Kafka 3장면, 16:9뿐. core가 이를 제한하며 browser 가능한 승인된 실행 환경의 실제 렌더는 미검증 |
| 2D | 초기 독립 검증의 walk/wave 정지 계산 결함은 담당자가 수정한 증거를 확인. core는 실제 walk 프리셋 통합을 실행. 임의 캐릭터/새 rig·여러 독립 shot 제작은 지원하지 않음 |
| 3D | 담당자가 preflight/audit/입력·style preset 검사를 추가한 최신 모듈에 맞춰 core payload를 축소. 이 환경의 Blender 확인 실패로 실제 최신 MP4·공간/조명/카메라 품질은 미검증 |
| 실제 LLM 감독 | 검토된 모델 출력의 입력 계약과 결정적 실행 판단을 연결. 실제 LLM이 전문 지식을 바탕으로 storyboard를 생성하는 새 Run lifecycle 연결/실추론은 하지 않음. 기존 intent Run을 거짓으로 확장하지 않음 |
| 품질 | 기계 QA와 의미/전체 시청·청취 판정을 분리. 후자는 unknown. 생성/렌더의 API 성공과 품질 성공을 합치지 않음. metadata의 원본 영상 보존은 자연스러운 액션이나 청감 품질을 입증하지 않음 |
| 캐시 복구 | 성공 자산/구간 및 source/입력 지문 검증은 회귀로 확인. renderer 프로세스가 checkpoint 기록 직전에 죽는 모든 경우와 다중 shot/layer 독립 복구는 미검증 |
| 전역 이력·library/ML | v2 production.json을 통합 RecordsService/library/ML로 연결하는 것은 미완료, 관련 소유자 범위 |
| 통합 배포·감사 | core는 commit/push/배포·기존/형제 worker 메시지·감사 세션 통지를 하지 않음. 총괄이 전체 변경을 검토·커밋·배포·자체 검증한 뒤 독립 감사에 전달 |

실제 Comfy/LLM/GPU 생성, 새 HyperFrames/Blender 렌더, 청감/전체 시청 품질 검수, 배포 확인은 **미실행**이다. 전자는 완료된 생성 반복 금지와 실행 경계 보존, 엔진 둘은 위 환경 차단, 배포/다른 소유자 파일 연결은 작업 카드의 역할 분담 때문이다. 별도 mypy/pyright 검사는 프로젝트에 해당 환경이 정의되어 있지 않아 미실행이다.

최종 정적 검증: 소유 source/API/회귀/통합 probe를 지정한 `uv run ruff check ...` 출력은 `All checks passed!`; 같은 7개 core source의 AST 구문 검사는 `syntax: 7 core source files parsed`; `git diff --check`는 exit 0이었다. 사용자 산출물은 현재 outbox의 `core-integration-20261107.md`, `studio-frame-hybrid-24s.mp4`, `core-integration-evidence.zip`으로 공유한다. ZIP에는 계획·receipt·명령 출력·소스 스냅샷·소유 core 소스와 재현용 테스트/입력을 포함한다.

이전 Run outbox 원본은 읽거나 쓰지 않았다. 같은 저장소의 기존 증거는 문서로 대조하고, 이번에 만든 미디어와 공유용 파일은 현재 Run outbox에 둔다. 실제 공유 ID는 총괄/사용자에게 반환되는 파일 첨부와 함께 확인할 수 있다. 증거 JSON은 해당 실행 당시 절대경로를 포함하므로 다음 Run에서 그 경로를 읽을 권한으로 해석하지 않는다.
