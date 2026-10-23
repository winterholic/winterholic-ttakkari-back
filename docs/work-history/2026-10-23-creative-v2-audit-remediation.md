# Creative Studio v2 — 감사 결과 기반 수정 (2026-10-23)

범위: 구현 실태 전수 감사(A: 코드·카탈로그·파이프라인, B: 도구 연결·실행 테스트) 이후 별도 구현 세션이 수행한 수정. 기준 backend `4f14f380c1aa3af435384bed53f762a11e6e2831`, frontend `e4de26f519cf329ad04f9e86fa7aec68b994eff8`.

## 입력과 한계

- 감사 A/B 원문(`studio-audit-part-a.md`, `studio-audit-part-b.md`)은 이 구현 Run에서 ttakkari 보호 영역 가드(PreToolUse)에 막혀 **직접 읽지 못했다**. 이는 이 Run 환경의 제한이며 운영 불능을 뜻하지 않는다. 대신 감사 세션 A/B의 `read_session` 요약 결과(핵심 판정)와 현재 코드·실제 실행으로 지적을 재현해 판정했다. 감사 최종 통합표는 받지 못했고, 도착하면 추가 항목을 반영한다.
- 감사 요약에서 확인한 지적: v2 계획에서 실제 렌더 실행으로 이어지는 경로 미구현, 15분 영상은 정책상 허용만 되고 분할 렌더·체크포인트가 계획에 없음(D 시나리오 `deviated`, 분할 없음), 시나리오 A~C 의도 추출은 `lexical_hint`뿐.

## 지적별 판정

| 지적 | 판정 | 조치 |
|---|---|---|
| 긴 영상에 분할 렌더·재개 체크포인트가 계획에 없음 | 유효(코드 재현: 계획에 구간 필드 없음, v1 렌더러는 프로젝트 240초·장면 60초 한도) | 수정: `plans.render_plan` |
| 어휘 힌트가 영문 부분 문자열을 장르로 읽음 | 새로 재현(`bank transfers`→`C02.space_sf`, `build`·`quick`→`C03.ui_animation`) | 수정: 영문 끝 단어 경계 |
| 힌트가 "설명 영상"·"개념 설명"을 못 잡음 | 유효(`Kafka 개념 설명 영상` → 선택 0개) | 수정: `C01.tech_explainer` 힌트 추가 |
| 시나리오 A~C가 LLM 없이 `lexical_hint` | 사실이나 설계상 폴백. Run 기반 LLM 경로는 코드에 있고 배포 revision에서 미검증 | 변경 없음, 미검증 유지 |
| v2 계획 → 실제 렌더 실행 미연결 | 유효. `assert_renderable`을 호출하는 렌더러가 없고 v1은 사용자 업로드 자산(장면별 이미지·음악)을 요구해 자산 제작 엔진(G34 `director.py`/`managed_comfy.py` 작업)과 맞물린다 | **미수정**(범위·동시 작업 충돌). 새 기능으로 확대하지 않음 |
| 레시피 20개 실행 검증 | 현재 코드 재현: blocked 10, unverified 8, capabilities_executed 1, recipe_demonstrated 1. 숫자 그대로 정직하게 표시 중 | 오탐 아님·수정 대상 아님(데이터가 현실을 반영) |
| 15분 지원 | 계획·길이 정책은 900초까지 가능(`target_met` 재현), 렌더는 미연결 | 계획 단계 분할만 수정, 렌더는 미완 |

## 변경

- `src/ttakkari/creative/v2/plans.py`: `render_plan(plan)` 추가. 장면 경계에서 60초 목표로 구간을 나누고, 구간마다 위치 비의존 `input_sha256`(장면 내용·구간 내 프레임·화면비·fps·Style DNA)을 둔다. 조회 때 장면에서 다시 계산하며 저장하지 않고 승인 해시에서도 제외한다(기존 승인 계획의 호환 유지). `executed: false`. 장면이 렌더러 장면 한도 60초를 넘으면 경고.
- `src/ttakkari/creative/v2/intent.py`: `_hint_pattern`으로 영문·숫자로 끝나는 힌트는 단어 경계에서만 맞춤. 한글 조사(`UI를`)는 그대로 허용.
- `src/ttakkari/creative/v2/catalog_data.py`: `C01.tech_explainer` 힌트에 `개념 설명`, `설명 영상` 추가.
- 테스트: `unit_tests/test_creative_v2_plans.py`(분할 4건), `unit_tests/test_creative_v2_matching.py`(힌트 경계·조사·신규 구문), `tests/test_creative_v2_api.py`(API 응답에 `render_plan`).
- frontend: `src/api/creativeV2.ts` 타입, `CreativePlanPage.tsx` 승인 패널에 분할 렌더 계획(미실행 명시, 구간 키, 렌더러 한도 경고), 테스트 2건. 구서버 응답(필드 없음)도 허용.

## 검증 (작업 checkout `ttakkari-studio-v2-back`/`-front`, 테스트 환경 `uv run --frozen`)

- `pytest tests/test_creative_v2_*.py unit_tests/test_creative_v2_*.py tests/test_creative_*.py` → `163 passed`.
- `pytest unit_tests` 전체 → `1 failed, 1585 passed`. 실패 `test_generation_hardening.py::test_db_create_schema_record_failures_carry_their_stage`는 수정 전 HEAD(`4f14f38`) 새 worktree에서도 같은 단언(`'test-db' == 'schema_contract'`)으로 실패하는 기존 문제이며 이번 변경과 무관. 이번 Run에서 원인은 조사하지 않았다.
- `ruff check src/ttakkari/creative unit_tests/test_creative_v2_*.py` 통과. `ruff format --check`는 수정 전에도 5개 파일이 기준 미달이라 일괄 포맷하지 않았다.
- frontend `pnpm exec tsc --noEmit` 0, `pnpm lint`(ui-lint 138건, 증가 없음), `pnpm exec vitest run` → `1312 passed | 10 skipped`, `pnpm build` 성공.
- 실제 계획 생성 실행: 60개 정보 항목 + 희망 900초 → `target_met`, 900초, 60장면, 15구간, `requires_split: true`, `executed: false`. 오탐 재현 입력 `I build a quiz about bank transfers` → 선택 0개. 로그: Run outbox `work/evidence_run.out`.

## 미완·미검증

- 계획 → 실제 렌더 실행 연결(자산 제작 포함)은 하지 않았다. `render_plan`은 계획 데이터이며 렌더 증거가 아니다.
- 브라우저 실측은 하지 않았다(이전 기록과 같이 인증 세션이 필요). UI는 jsdom 테스트까지만 검증.
- 실제 Qwen intent 응답, 배포 revision의 Run 경로, MCP 실행, resume은 계속 미검증.
- 감사 최종 통합표 및 원문 직접 대조는 미완.

## 2차: 통합 감사 후속 (계획 → 엔진 dispatch → 렌더 → 제작 기록)

통합 감사 최종 원문(`8f00fcb8…/2026-10-23-audit-creative-studio.html`, `evidence-part-a/b.md`)도 이 Run의 ttakkari 가드에 막혀 직접 읽지 못했다. 전달받은 요약(계획→dispatch→렌더→기록 연결 미확인, 20개 컨셉·15분 제작 미입증, `/api/version`=710f184c 200 + capabilities 404 + 예약 draining)을 코드와 HTTP로 대조했다.

### 라이브 라우트 불일치 판정 (HTTP 실측, 읽기 전용, 인증 없이)

`https://ttakkari-api.winterholic.net` 2026-10-23 UTC 01:4x:
- `/api/version` → 200 `710f184c9a99616fad8cfaf28b77c4771f32e81e`.
- `/api/creative/v2/catalog`, `/registries`, `/matrix`, `/duration/policy`, `/intent/contract`, `/plans` → **401** (라우트 존재, 인증 필요). `/api/creative/catalog`(v1)도 401.
- `/api/creative/v2/capabilities` → **404**. 그러나 이 경로는 `4f14f38`/`710f184` 코드에 **라우트로 존재한 적이 없다**(`api/creative_v2.py`에 정의 없음, grep 결과 import와 `/catalog` 응답 필드뿐). 감사의 404는 존재하지 않는 경로를 조회한 결과로, 배포 지연이나 라우트 누락이 아니다. v2 라우트는 710f184c 런타임에 모두 노출돼 있다. `/api/openapi.json`, `/openapi.json`은 404(문서 비활성).
- 배포 예약 `6c75778a`가 draining인 것과 `/api/version`=710f184c는 일치한다(draining은 새 요청이 이미 새 세대로 전환된 상태).
- 조치: 감사와 기획 문서가 가정한 `/capabilities`를 실제 읽기 라우트로 추가(아래).

### 구조적 사실 (코드 실행으로 재현)

- 실제 레지스트리 스냅샷으로 C01 15개 단일 선택과 빈 의도를 모두 매칭했을 때 `execution_scope == studio_runtime`이 나온 경우는 **0건**이었다. 필수 능력을 모두 `creative_runtime`이 채우는 레시피가 없어 렌더 게이트가 어떤 계획에서도 열릴 수 없었다. 즉 "연결 미확인"이 아니라 현재 코드에서는 구조적으로 도달 불가였다.
- 스튜디오 제작 런타임(`creative_runtime`)에 연결된 엔진은 `skill.v1_ffmpeg_slideshow`와 `tool.ffmpeg` 둘뿐이다. G34 엔진 디렉터(`director.py`/`pipeline.py`/`engines/`)는 main에 없고 별도 브랜치 `integ/g34-creative-engines-main-20261023`에 있다. 이 작업은 그 코드를 복사하거나 건드리지 않았다.

### 변경

- `registry_data.py`/`capabilities.py`/`matching.py`: 기준선 레시피 **R21 정지 이미지 슬라이드쇼**(필수 `edit.captions`, `baseline=True`)를 추가. R01~R20은 원 요구의 컨셉 레시피로 그대로 두고 R21은 컨셉이 아니다. 기준선은 더 풍부한 레시피가 선택에 하나도 맞지 않을 때만 주 레시피가 되고 보조 레시피로는 쓰이지 않는다(요청을 슬라이드쇼로 조용히 낮추지 않기 위해서). 예: `C08.three_act + C10.dissolve` → R21 `ready/studio_runtime`, `C01.tech_explainer + C03.vector_motion_graphics`는 기존 레시피 유지, 게이트 닫힘. 기존 테스트의 "studio_executable 0개" 단언은 "컨셉 레시피에는 0개, R21만"으로 바꾸고 레시피 수 단언을 21로 갱신. 기능 매트릭스 문서 재생성.
- `creative/v2/dispatch.py` 신규: `dispatch`(렌더 게이트 통과한 승인 revision만 v1 프로젝트로 생성, revision당 1회, 장면 id·길이·자막을 계획에서 가져옴), `request_render`(렌더 직전 게이트 재확인, 프로젝트가 승인 계획과 같은지 drift 검사, 자산 없음이면 거부, 큐 등록), `view`(렌더러 프로젝트의 실제 상태로 계산: not_dispatched, awaiting_assets, ready_to_render, queued, running, succeeded, failed, drifted, stale_revision), 제작 기록은 계획 폴더 `production.json`(revision, 승인 plan_sha256, 프로젝트 id, 렌더 작업 id). 렌더러 한도 초과(화면비 9:16, 3~40장면 밖, 장면 60초 초과, 총 240초 초과)는 이유를 들어 거부한다. 240초 초과의 구간 디스패치와 이어 붙이기는 구현하지 않았고 응답에 그렇게 적는다.
- `api/creative_v2.py`: `GET /capabilities`(능력별 최고 수준·공급자·연동 범위), `GET /plans/{pid}/production`, `POST /plans/{pid}/production`, `POST /plans/{pid}/production/render`. 모두 소유자 인증. 라우트 인벤토리 갱신.
- frontend: 승인 + 게이트 열림일 때만 보이는 "제작 연결" 패널(렌더러 프로젝트 만들기, 자산 대기 사유와 프로젝트 링크 `/creative/<project_id>`, 렌더 시작, 작업 상태, 렌더러 한도 사유), 구서버·게이트 닫힘이면 숨김.

### 검증 (작업 checkout, 테스트 환경 `uv run --frozen`)

- `pytest unit_tests tests/test_creative_v2_*.py tests/test_creative_*.py tests/test_route_inventory.py` → `1657 passed, 1 skipped`. (1차 때 단독 실행에서 실패하던 `test_generation_hardening` 1건은 이번 묶음 실행에서는 통과했다. 실행 순서 의존으로 보이나 원인은 조사하지 않았다.)
- 신규 `unit_tests/test_creative_v2_dispatch.py` 10개: 기준선 게이트, 풍부한 선택은 닫힘·프로젝트 0개, 미승인 거부, 멱등 디스패치와 장면 일치, 자산 없음 거부, **실제 FFmpeg 렌더로 영상 생성(영상·오디오 트랙, 길이 계획과 0.6초 이내)**, 프로젝트 수정 후 drift 거부, 계획 revision 변경 시 stale·게이트 닫힘, 240초 초과·9:16 거부.
- `tests/test_creative_v2_api.py`: `/capabilities` 계약, 인증 없는 접근 401, 승인 전 디스패치 422, 승인 후 201과 프로젝트 장면 일치, 자산 전 렌더 422.
- frontend: `tsc` 0, `pnpm lint`(ui-lint 138, 증가 없음), `vitest` `1316 passed | 10 skipped`, `build` 성공. 신규 UI 테스트 4개.
- 실제 실행: Run outbox `v2-plan-bridge-render.mp4`(25.8초, 영상+오디오, 512,062바이트), `work/bridge_run.out`. 승인 계획 → dispatch → 합성 이미지·음악 업로드 → 렌더 → `succeeded`(elapsed 3.08초, peak_rss 337182720바이트). 프레임 4초 지점에서 계획 자막 `장면 1` 확인.

### 증거 범위 (중요)

- 위 영상의 입력은 **합성 단색 이미지와 사인파**다. 계획→dispatch→v1 렌더러→제작 기록의 **배선**만 입증한다. v2 컨셉 20개나 15분 영상의 제작 증거가 아니고, 시청 가치가 있는 영상도 아니다. v1 합성 3초 데모와 같은 취급을 하지 않기 위해 별도 기록한다.
- 이 경로로 만들 수 있는 것은 사용자가 이미지·음악을 직접 올리는 정지 이미지 슬라이드쇼뿐이다. ComfyUI/Remotion/Blender/Three.js/ArtCraft 엔진은 dispatch 대상이 아니다. 해당 엔진의 `creative_runtime` 연동은 G34 통합 이후 과제다.

### 남은 공백

- 20개 컨셉 레시피의 실제 제작(blocked 10 / unverified 8 / capabilities_executed 1 / recipe_demonstrated 1, 컨셉 레시피 중 studio_executable 0)과 15분 실제 제작, 240초 초과 구간 디스패치·이어 붙이기.
- 실제 모델 응답(intent Run), MCP 실행, resume, 브라우저 UI 실측(`ttakkari_browser`는 이 Run 도구에 없었고 이전 기록처럼 인증 세션이 필요)은 각각 미검증.
- 감사 통합 원문의 직접 대조(가드 차단).
