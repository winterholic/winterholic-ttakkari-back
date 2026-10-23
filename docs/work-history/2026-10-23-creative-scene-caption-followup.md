# Creative Studio v2 장면 프롬프트·내레이션 자막 후속

날짜: 2026-10-23 (Asia/Seoul)
프로젝트: 개인 Ttakkari / Creative Studio v2
작업 브랜치: `fix/creative-scene-captions-20261023`

## 기존 샘플의 판정

기준은 plan `5809bb7f-4800-44d9-aad6-fcdd9a1d03b8` revision 1, 23.829초 R21 정지 이미지 슬라이드쇼다. 요청은 푸른 새벽 골목과 꺼지는 가로등, 첫 버스와 환경미화원, 한강 해돋이의 금빛 수면 반사, 한국어 내레이션 자막이었다. 독립 컨택트시트에서는 노란 해가 드러난 거리, 빈 도로, 케이블 구조가 보였고 제목 세 개만 표시됐다. 기존 기계 QA 여섯 항목은 트랙·길이·검은 화면·음량 등 기술 검증이며 의미 일치를 확인하지 않는다. 기존 영상의 요청 충족 판정은 **미충족**이다.

### 확인한 원인

기존 deployed revision `81a95a5...`의 managed Comfy builder는 각 장면 프롬프트에 전체 brief를 넣고 제목과 narration만 장면별로 달리했다. 실행 receipt의 기존 prompt hash는 구버전 builder와 plan 입력으로 재구성한 해시와 모두 일치했다.

| 장면 | receipt와 재구성한 prompt SHA-256 |
|---|---|
| s1 | `1ccff7f0058eaee55b5ae7ea51e1dd4f83fb86f7fc3cd0e6477b36db614d86fb` |
| s2 | `f4ad3240f185f87e6b2ca242647aa56697f9df55d719f1b5687a0de0a7f0c5c0` |
| s3 | `af45363c6bb1ed9d288a22767e8ae772603a0622604d52491641931b7923e36f` |

따라서 전체 brief가 장면마다 복제되어 scene-specific visual direction이 약해지는 경로는 소스와 receipt로 확인됐다. 각 샷에서 추출한 시점 `-ss 1`도 확인했지만 이것이 시각 불일치의 원인이라고 입증할 증거는 없어 변경하지 않았다. 장면 이미지 생성이 한국어를 잘 이해하는지는 별도로 검증되지 않았다.

자막은 손실된 TTS 데이터가 아니었다. dispatch가 원래 `scene.title[:160]`을 caption으로 설정하는 기존 구현이었고, narration은 voice asset 생성과 project prompt에 쓰였지만 화면 자막엔 전달되지 않았다. 즉 기존 경로는 사실상 title-only였다.

## 변경

- per-scene prompt는 구조화된 `장면 N:` / `Scene N:` brief에서 해당 장면 조건만 추출하고, 다른 장면 문장을 넣지 않는다. 명시적 장소와 주체·행동·시간·조명을 보존하고, 비구조 brief에는 전체 시놉시스 복제를 피한다.
- prompt가 Comfy 1000자 한도를 넘으면 조용히 자르지 않고 사전 차단한다. 제출한 prompt의 SHA-256을 progress/checkpoint 및 output metadata에 보존하고, runner receipt가 같은 해시를 확인하지 못하면 성공 처리하지 않는다. generator 버전을 갱신해 구 캐시 재사용을 막는다.
- dispatch에는 `caption_mode=narration|title_only`를 추가했다. 기본은 narration이고 title-only는 명시적으로 선택한다. narration이 비어 있는 scene은 title fallback을 기록한다.
- 한국어 narration은 폰트 실측 너비를 기준으로 안전 폭 내 최대 2줄 cue로 나누고 음성 asset 길이에 비례해 추정 구간을 배분한다. 렌더 이미지 overlay와 SRT가 같은 구간을 사용하며 `estimated_voice_duration` 등 timing basis를 저장한다. 단어 단위 발화 정렬이나 정밀 sync를 보장하지 않는다.
- recipe의 R21 정지 이미지 유지 동작은 바꾸지 않았다. 지원되는 기존 animation/transition 처리도 유지했다.

## 검증

- `PYTHONPATH=src:unit_tests .venv/bin/python -m pytest -q unit_tests/test_creative_v2_asset_generation.py unit_tests/test_creative_v2_asset_generation_e2e.py unit_tests/test_creative_v2_dispatch.py unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_lease_handoff.py tests/test_creative_mcp_tools.py tests/test_route_inventory.py` — **90 passed** (109.64s). E2E는 Settings 및 scene prompt builder, receipt hash, 실제 imageio_ffmpeg 설정 기반 최소 렌더, 자막 구간/SRT/명시적 title-only를 포함한다.
- Ruff — 변경된 Python 파일 **All checks passed**.
- `git diff --check` — 통과.
- 프롬프트 단위 문자열 assertion과 장면 출력의 시각 의미 일치는 별개다. 새 Comfy 생성 결과/contact sheet는 아직 검수하지 않았다.

## 아직 필요한 확인

현재 deployed runtime revision `81a95a5...`가 구 코드이고 다른 배포가 `draining` 상태여서 이 코드로 새 sample을 생성하지 않았다. 새 SHA 배포 예약 후 candidate가 활성화되면 기존 plan/asset을 수정하지 말고 새 plan revision/approval/generation으로 3장면을 생성해야 한다. 그 결과에서 s1 푸른 여명/가로등, s2 버스와 청소인력, s3 해와 한강 수면 반사를 각각 contact sheet로 직접 판정하고 미충족은 그대로 기록한다. 기존 lease/TTS loop, ownership, approval, revision/hash 검사, resume key와 QA 보호를 유지한다. `2ac86aa4...needs_reconcile` 작업은 건드리지 않았다.

새 영상은 아직 없음: 실제 신규 시각 QA와 contact sheet 비교는 미실행이다. 음성 청취도 미실행이며 파형/음량에서 발음이나 배경음 명료성을 추론하지 않았다. 이 샘플은 다섯 편의 다양한 영상 E2E 검증으로 계산하지 않는다.


## 보강: 자막 모드 계약 검증 (2026-10-23)

기준 SHA는 `4f341c5`이며 이미 `b2faeb5`(plan style `style_id`+`version` 검증, 422) 위에 있다. 재기반 충돌은 없고 해당 변경은 그대로 보존했다.

### 계약 확인

- `C10.subtitles`(catalog)는 `edit.captions`를 요구하고 "글자 크기 하한과 읽는 시간 확보"만 말한다. R21의 필수 능력도 `edit.captions`다. 둘 다 자막 내용이 대본이어야 한다고 정하지 않는다.
- 따라서 제목만 있는 영상은 `title_only`를 명시했다면 명세 위반이 아니다. 독립 검수의 "제목-only"를 곧바로 명세 위반으로 확정하지 않는다. 다만 브리프(한국어 자막, 차분한 한국어 내레이션)는 대본 자막을 기대할 수 있고, 기존 R21 영상은 구버전 dispatch가 모드 구분 없이 제목을 쓴 결과였다.
- 이 해석은 코드와 문서 근거이며 사용자 의도를 직접 확인한 것은 아니다. 확인 필요.

### 변경

- dispatch 기록과 status에 `caption_contract`를 추가했다. 모드, 계획의 `C10.subtitles` 선택 여부(`plan_selects_subtitles`), 대본이 있는 장면, 제목 자막 장면, 사람이 읽는 notices를 담는다. `title_only`로 대본 있는 장면을 렌더하면 대본이 화면에 나오지 않는다고 알린다. `narration`에서 대본이 빈 장면은 제목 fallback으로 알린다.
- 이미 다른 모드로 dispatch된 revision에 뒤늦게 `caption_mode`를 요청하면 조용히 무시하지 않고 응답에 `caption_mode_request_ignored`를 넣는다.
- 버그 수정: status의 `outdated_segments` 계산이 기록된 `caption_mode`를 쓰지 않고 항상 narration 기본값으로 run key를 만들었다. `title_only` 성공 렌더가 "다시 렌더할 구간"으로 보이는 문제다. 새 테스트는 수정을 되돌리면 `outdated_segments` 단언에서 실패함을 확인했다.

### 검증

- 새 테스트 2개(`test_caption_contract_...`, `test_title_only_dispatch_renders_...`)는 임시 홈에서 실제 ffmpeg 렌더까지 실행한다. 실제 Comfy/ACE-Step 생성은 하지 않았다(GPU 경쟁 방지 지시).

### 적용 후 관찰 기준 (E2E 담당용)

- 새 plan을 만들 때 `style`에는 `{style_id, version}` 형태 DNA ref만 넣는다. catalog `entry_id`는 `style`이 아니라 intent `selections`에 둔다.
- 대본 자막이 목표면 dispatch를 `caption_mode=narration`(기본)으로 한다. dispatch 응답의 `caption_contract.mode == "narration"`, `narration_scene_ids`가 전 장면이고 `notices`가 비어 있는지 본다.
- 렌더 후 project의 scene별 `caption_segments`가 그 장면 narration과 같고, 마지막 cue `end`가 voice asset 길이인지, SRT가 같은 구간인지 본다. 타이밍은 추정이며 청취 sync는 미검증이다.
- 장면 prompt는 generation receipt의 `prompt`/`prompt_sha256`을 보고 s1은 푸른 새벽 골목과 가로등, s2는 첫 버스와 환경미화원, s3는 한강 해돋이와 금빛 윤슬만 담았는지 확인한다. 최종 판정은 contact sheet 시각 검수이며 prompt 문자열 일치로 대체하지 않는다.
- 새 영상의 실출력은 아직 없다. 장면 의미 일치와 음성 청취는 **미실행**이다.


## 보강 2: Style DNA 렌더 제어를 검증 가능하게 지정하는 경로 (2026-10-23)

입력: E2E 담당 원장에 따르면 기존 StyleDNA 시드 5개에는 animation/transition/caption_style 매개변수가 없고 MCP에 DNA 등록 도구가 없어 5편 모두 style을 생략했다. 그 계획 5개와 진행 중 generation은 건드리지 않았다.

### 조사한 계약
- 렌더러(`V1SlideshowAdapter`)가 Style DNA에서 소비하는 값은 세 가지다. `motion_language.params.animation`(still, zoom_in, zoom_out, pan, pan_down, pulse), `motion_language.params.transition`(fade, cut), `visual_identity.params.caption_style`(bottom_card, center_highlight, title_card, code_overlay). 이 밖의 facet과 허용 값 밖의 값은 dispatch 기록 `style.ignored`에 남는다.
- 적용 범위는 계획 전체다. 모든 장면에 같은 값이 들어가므로 변화는 편(plan)별 style 선택으로 만든다. 장면별 값은 지원하지 않는다.
- HTTP API에는 `POST /api/creative/v2/style-dna`(등록, 버전 불변)가 있었지만 MCP 브리지에는 style 조회·등록 연산이 없었다.

### 새로 발견한 실제 버그 (소스 원인 확인)
- 계획에 저장되는 `plan.style`은 합성 결과(ComposeResult)라 facet이 `plan.style.dna` 아래에 있다. `_style_defaults`는 최상위 facet만 읽었다. 격리 재현에서 animation=pan, transition=cut, caption_style=center_highlight를 가진 DNA로 실제 `build_plan`을 만들어도 `applied={}`, `ignored=[]`였다. 즉 DNA를 등록해도 조용히 적용되지 않았다. 기존 단위 테스트는 평평한 가짜 style만 넣어 이 경로를 못 잡았다.
- 수정: `dna`가 있으면 그 아래를 읽고 평평한 형태도 계속 받는다. 실제 `build_plan` 결과를 쓰는 회귀 테스트를 추가했고, 수정을 되돌리면 `applied == {}`로 실패함을 확인했다.

### 추가한 MCP 경로 (기존 서비스·계약 재사용)
- `creative_v2_styles`(읽기): 등록 style 목록과 `render_controls`(facet, 매개변수 이름, 허용 값). `creative_v2_capabilities`에도 `style_dna_render_controls`를 넣었다.
- `creative_v2_style`(읽기): 효과 DNA, 상속 provenance, `render_effects`(R21 렌더에서 적용·무시될 값). 목록이나 저장만으로 지원 완료로 보지 않고 이 미리보기로 확인한다.
- `creative_v2_style_put`(쓰기): 기존 `StyleDNADoc`(Strict, 알 수 없는 필드 거부, 참조는 slug·안전한 상대 이름·해시 고정)만 받는다. `dry_run=true`로 저장 없이 검증과 미리보기를 한다. 같은 (style_id, version)의 다른 내용과 내장 시드 덮어쓰기는 거부하며 같은 내용은 멱등이다. 경로·URL·셸 입력 필드가 없고 개인 세션만 쓸 수 있다.
- 기존 plan_create의 `style`은 그대로 `{style_id, version}` 참조만 받는다. 카탈로그 ID를 style ref로 쓰지 않는다. 계획 revision 승인과 버전 고정(plan.style.sha256)도 그대로다.

### 사용 방법 (E2E 담당용)
1. `creative_v2_styles`로 허용 값을 확인한다.
2. 편마다 `neutral_base@1.0.0`을 부모로 한 새 style을 `creative_v2_style_put`으로 만든다. `motion_language`와 `visual_identity`를 override하고 각각 `overrides`에 사유를 적는다. 먼저 `dry_run=true`로 `render_effects.applied`에 세 값이 모두 있는지 본다.
3. `creative_v2_plan_create`의 `style`에 그 `{style_id, version}`을 넣고 새 plan을 만들어 승인한다. 기존 5개 계획은 style을 바꿀 수 없으므로(plan_edit에 style 없음) 새 plan이 필요하다.
4. dispatch 후 `production_status`의 `style.applied`가 의도한 값인지, 렌더 project 장면의 animation/transition/caption_style이 같은지 확인한다. 시각적 구분은 contact sheet로 직접 본다.
- 배포 전 구버전은 이 도구와 수정이 없다. 새 revision 활성화를 확인한 뒤 사용한다.

### 검증 범위
- 실제 ffmpeg 렌더까지 격리 임시 홈에서 실행했다(등록 style → 계획 → dispatch 기록 → 렌더 project 장면 값). 실제 Comfy/ACE-Step/GPU는 쓰지 않았다.
- 움직임·전환·자막 스타일이 화면에서 편별로 달라 보이는지는 새 영상의 시각 검수가 필요하며 아직 **미실행**이다.
- MCP tool schema는 중첩 `StyleDNADoc` 모델을 직접 사용해 unknown properties를 닫고(`additionalProperties=false`); 브리지에 JSON으로 보낼 때만 model dump 한다. 자유형 object schema를 검출하는 회귀는 먼저 실패했고, typed model 적용 후 통과했다.


## 보강 3: 실행 증거 범위 정정 및 자막 동작 요약 (2026-10-23)

### 실행 기록의 증거 한계

이전 worker 결과에는 실행 원문 대신 요약만 남아 있다. worker session `c935fb6c-f195-40d1-aa8f-9ca423280c2f`의 기록된 결과에는 아래 문자열과 요약이 보인다.

| 작업 | session에 남은 내용 | 원문에서 확인되지 않는 항목 |
|---|---|---|
| 첫 회귀 | `PYTHONPATH=src:unit_tests .venv/bin/python -m pytest -q ...tests...`; `90 passed in 109.64s` | `...tests...`의 전체 argv, 호출 tool, 실행 옵션, 작업 디렉터리, 원시 종료 출력 |
| 첫 push | `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache git push origin HEAD:main`; 결과 메시지에 “Pushed” | 호출 tool/옵션/작업 디렉터리 및 git의 원시 stdout·stderr·종료 코드 |
| 두 번째 회귀·pre-push | `105 passed in 142.07s`; Ruff, 계약 테스트 84개, 변경 테스트 105개 통과; `6428081..9f82c9c HEAD -> main` push라고 worker가 보고 | 각 명령의 전체 argv, 호출 tool/옵션/작업 디렉터리, 각 원시 출력·종료 코드 |

따라서 위 검증과 push는 **worker 보고**이며 이 Run에서 독립 재실행한 결과가 아니다. 특히 관련 코드 검사 92개 및 hook 검사 84/11 보고도 worker가 수행했다고 보고한 범위로만 취급한다. 해당 원문 기록에는 최초 캐시 거부의 정확한 환경, 명령, 에러 출력, 거부 단계가 없다. “허용된 임시 캐시로 같은 gate를 통과” 및 “임시 캐시 삭제가 safety guard에 막힘”도 실행 출력이 보존되지 않아 worker 진술 이상의 증거로 승격하지 않는다.

현재 확인 가능한 어느 원문에도 “저장소를 sandbox에서 읽을 수 없어 git/pytest/push를 sandbox 밖에서 실행했다”는 실행 근거나 외부 실행 흔적이 없다. 또한 tool 호출의 승인/격상 필드가 session 기록에 보존되지 않았다. 그러므로 기본 실행 경로였는지, 별도 escalation이 있었는지, 우회였는지는 **판정 불가**다. 이는 환경 전체의 접근 불가를 뜻하지 않는다. 이 정정은 실행을 재시도하지 않고 남아 있는 session 결과와 저장소 기록만으로 작성했다.

이번 Run에서 독립 확인한 저장소 상태는 별도다: `/Users/winterholic/development/projects/ttakkari-studio-v2-back`에서 `git pull --ff-only origin main`은 `Already up to date`를 반환했고, HEAD와 `origin/main`은 `9f82c9c`였다. 이 사실은 과거 pytest/push 호출 세부사항을 소급 입증하지 않는다.

### 자막 모드 계약과 실제 출력 경로

- 지원 모드는 `narration`과 `title_only`다. `creative_v2_production_dispatch`는 `narration`이 기본이며, `title_only`를 명시하면 장면 제목만 caption source로 쓴다. `C10.subtitles`의 선택 여부는 계약 기록의 `plan_selects_subtitles`로 표시되며, 어느 모드도 대본을 화면에 반드시 표시하라는 spec은 아니다. 따라서 명시한 `title_only`는 그 자체로 명세 위반이 아니다.
- `narration`에서 장면 narration이 비어 있지 않으면 그 본문이 `caption_segments`가 된다. 줄바꿈은 지정된 caption style과 안전 폭을 사용하며 시간 길이는 음성 asset duration(없으면 장면 duration) 기반 추정이다. 대본과 음성의 단어 단위 sync는 보장하지 않는다. 대본이 비어 있는 장면은 제목 fallback이고 `caption_contract.notices`가 이를 알린다.
- `title_only`에서 narration이 있는 장면은 대본 자막을 화면에 내보내지 않는다는 notice가 `caption_contract.notices`에 기록된다. 이미 dispatch된 revision에 반대 모드를 요청하면 기존 모드를 바꾸지 않고 `caption_mode_request_ignored`를 돌려준다. 다른 모드를 적용하려면 새 revision 승인과 dispatch가 필요하다.
- 샘플 plan brief에는 한국어 자막과 한국어 narration이 있었지만 당시 revision의 dispatch mode를 기록하는 근거는 보존되어 있지 않다. 그러므로 옛 영상의 제목-only를 `title_only`로 의도했다고 추정하거나 명세 위반으로 판정하지 않는다. 새 영상에서 대본 자막이 목표일 때는 `narration` 선택, contract의 mode/notices, 각 장면 `caption_segments` 본문, SRT 구간을 함께 확인한다.

이번 정정은 문서 변경만 포함한다. backend 코드·runtime·기존 계획/생성은 바꾸지 않았고, pytest 및 GPU 생성은 실행하지 않았다.
