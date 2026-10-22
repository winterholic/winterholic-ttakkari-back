# Creative Studio v2 설계와 첫 수직 슬라이스 (2026-10-22)

요구사항 정본은 [creative-studio-v2-requirements.md](creative-studio-v2-requirements.md)다. 이 문서는 첫 구현 단위(WBS 8.2~8.6)가 무엇을 만들었고 무엇을 만들지 않았는지 적는다. 전체 요구사항 완료 문서가 아니다.

## 설계 원칙 (agentic-architecture 적용)

- 분기가 고정이고 입력이 정형이면 코드로 처리한다. LLM은 의도 추출에만 쓰이고, 그 결과는 타입·카탈로그·근거 인용으로 코드가 검증한다. "테스트에서 통과했다"는 조건 없이는 정보가 없으므로 변이 시험으로 테스트가 결함을 잡는지 확인했다.
- 더하는 쪽이 입증 책임을 진다. 능력은 선언만으로 올라가지 않고 호스트 스냅샷과 lab 실행 기록으로만 올라간다.
- 정책은 프롬프트가 아니라 강제 계층에 둔다. 승인·strict 길이·충돌 해결은 서버가 막는다.
- 비가역 행동(승인된 길이로 렌더)은 revision에 묶고, 편집하면 승인이 무효가 된다.

## 모듈 지도 (`src/ttakkari/creative/v2/`)

| 파일 | 역할 |
|---|---|
| `catalog_data.py` | C01~C12, 총 166개 항목. id·버전·제작 지식·제약·관계 7종·근거·사례 |
| `capabilities.py` | 능력 분류 55개, 수준 6단계, Tool/Model/Skill/Recipe 모델 |
| `registry_data.py` | 도구 10, 모델 12, 스킬 22(lab 초안 21 + v1 FFmpeg 렌더러 1), 레시피 R01~R20 시드 |
| `probe.py` | 호스트 실측 탐침. 설치·버전·lab 기록·스킬 초안 상태를 스냅샷 JSON으로 저장 |
| `registry.py` | 스냅샷에서 항목·능력 수준을 계산하고 레시피 실행 가능성을 판정 |
| `intent.py` | IntentSpec 계약과 검증, 단어 힌트 보조(결정 불가), LLM 추출기용 계약 |
| `extraction.py` | 관리형 로컬 Qwen으로 구조화 의도 초안 추출, 근거 검증, 추론을 soft로 제한 |
| `matching.py` | 선택 → 충돌 해석 → 필요 능력 → 레시피 점수 → 결정과 근거 추적 |
| `style_dna.py` | Style DNA 16필드, 참조 자산, 버전 불변 저장소, 상속, 합성, 충돌 해결, 작품 고정 |
| `duration.py` | 통합 길이 정책, 발화 추정, 프레임 배분, 재계산 |
| `plans.py` | 제작 계획 생성·저장(revision CAS)·편집·승인 |
| `report.py` | 능력 행렬 문서 생성 |

API는 `src/ttakkari/api/creative_v2.py`(`/api/creative/v2/*`, 소유자 인증). 프런트는 `/creative/plan`, `/creative/plan/:id`.

## 의도 → 실행 계획 (요구사항 처리 순서 대응)

1. **의도·제약 추출**: `IntentSpec`(카탈로그 선택 + role primary/secondary + strength hard/soft + source + 인용 근거, 제약, 길이 요청, 정보 항목). `validate_intent`가 알 수 없는 항목, 브리프에 없는 인용(환각), 근거 없는 hard, 한 축의 주 선택 중복을 거부한다.
2. **충돌 해석** (`_find_conflicts`): 관계가 hard이고 둘 다 필수면 `blocked_both_hard`(사용자 결정), 한쪽만 필수면 약한 쪽을 제외하며 근거를 남긴다, 둘 다 선호면 주 선택이 이긴다, 주 선택도 같으면 `needs_user`. soft 관계는 혼합을 허용하고 Style DNA 충돌 해결로 넘긴다. 쇼츠 상한(180초, 근거 URL·확인일)과 요청 길이의 충돌도 같은 구조다.
3. **필요 능력 도출** (`_derive_needs`): 선택 항목의 requires/prefers가 다대다로 모여 must/should/prefer가 된다. 같은 능력을 여러 항목이 요구하면 출처가 합쳐진다. 모든 계획에 `qa.machine_video`, `edit.timeline`이 must다.
4. **공급자 탐색**: 능력마다 Tool/Model/Skill 공급자를 수준·연동 범위 순으로 고른다. `a|b` 대안 묶음은 하나만 충족하면 된다.
5. **레시피 점수**: `0.40 적합 + 0.30 능력 커버 + 0.30 실행 가능성`. 필수·주 선택과 충돌하는 레시피는 탈락한다. 막힌 레시피는 보조로 끌어오지 않는다.
6. **결정**: `ready`(필수 모두 실행 기록) / `ready_unverified`(설치만) / `blocked`(필수 누락, 대안 목록) / `needs_user_decision`. `fingerprint`는 의도 + 레지스트리 상태의 해시라 같은 입력이면 같은 결정, 설치 상태가 바뀌면 달라진다.

## 능력 수준과 레시피 판정

수준: `missing < declared < installed < executed_partial < executed < verified`. 설치와 실행 실측을 섞지 않고, 독립 검수 pass만 verified다. 실행 코드 없이 지침 문서뿐인 스킬은 declared에서 멈춘다. 의존 도구가 없으면 스킬도 missing이다.

레시피 판정은 능력 증거와 레시피 사례 증거를 분리한다: `studio_executable`, `recipe_demonstrated`(레시피 사례 기록이 있음), `capabilities_executed`(능력은 각각 실행됐으나 이 조합은 미실행), `partially_demonstrated`, `unverified`, `blocked`. 전체 행렬은 [creative-studio-v2-capability-matrix.md](creative-studio-v2-capability-matrix.md)이고 코드에서 생성한다(테스트가 최신 여부를 확인).

2026-10-22 스냅샷 기준: 스튜디오 실행 가능 0, 레시피 사례 실행됨 1(R19), 능력별 실행됨·조합 미실행 1(R06), 설치만 8, 차단 10. 가짜 공통 렌더러로 전부 실행 가능으로 표시하지 않았다.

## Style DNA

구현의 필수 16필드는 사용자 원문의 15개 필드에 설계상 `version`을 더한 것이다. 사용자는 Style DNA에 작품 단위 상속·수정·버전 고정이 가능해야 한다고 요구했으며, `version` 필드의 추가 자체는 버전 고정을 명시적으로 저장하기 위한 구현 설계 선택이다. 8개 텍스트 면(Facet은 텍스트 + 수치 params), palette, reference_assets(image·video·code는 sha256 필수, render_settings는 params, outcome은 success/failure), positive/negative 예시, quality_constraints, compatible_skills.

- 저장소는 버전이 불변이다. 같은 (id, version)에 다른 내용은 거부한다. 내장 시드는 덮어쓸 수 없다.
- 부모가 있으면 바꾸는 필드만 적고, 부모와 다른 값은 `overrides`에 사유가 있어야 한다. 사유 없는 덮어쓰기는 저장 단계에서 거부된다.
- 합성에서 서로 다른 출처가 같은 면을 주장하면 충돌이다. `Resolution`(필드·키·take/custom·근거·결정자)이 있어야 닫힌다. 충돌 없는 곳의 해결 기록, 후보에 없는 winner는 거부한다. assistant가 정한 해결은 `resolved_pending_confirmation`으로 남는다.
- `pin_work`는 작품 단위로 해결된 DNA와 해시를 revision CAS로 고정한다. 이후 새 버전이 생겨도 고정본은 변하지 않는다.
- 예: 귀여운 3D + 공포 조명 + SF 세계관은 lighting·environment_rules·sound_identity 세 충돌을 명시적으로 올린다(`unit_tests/test_creative_v2_style_dna.py`).
- 시드 5종(neutral_base, cute_3d, horror_lighting, sf_world, anime_cel)은 참조 자산·예시가 없는 `draft`다.

## 길이 정책

기본 자동, 희망 시간은 soft다. strict는 명시적 정확한 길이(`exact`)와 플랫폼 상한(`platform_cap`, 근거·확인일 필수)뿐이다. UI 선택지는 자동 / 15초 / 30초 / 1분 / 2분 / 3분 / 5분 / 10분 / 15분 / 직접 입력(1~900초).

1. 필요 길이 먼저: 장면마다 floor(발화 + 앞뒤 여유 0.4초, 최소 장면 길이, 사용자 하한 중 최대) / ideal / ceiling(반복 없이 늘릴 수 있는 한계). 합계가 `NeededRuntime`(최소, 이상, 최대)이다. 음악 길이가 있으면 전체 길이를 정한다.
2. 발화 추정: 한글 음절 4.4/초(lab 기록 three-character-talk-v1-v2의 Supertonic 58음절 13.299초, 단일 표본), 영문 2.4단어/초, 숫자 3자리/초, 문장 끝 0.35초·쉼표 0.15초 여백. 실측 오디오 길이가 있으면 `measured_speech_seconds`가 대체한다.
3. 목표 적용: soft는 [floor 합, ceiling 합]으로 clamp하고 벗어나면 `deviated`와 이탈률, 25% 초과 시 `excessive`, 대안을 돌려준다. exact가 범위 밖이면 계획을 만들지 않고 `conflict`와 대안(최소 길이로 조정, 선택 장면 제외, 발화 배속(1.15배 초과는 비현실 표시), 분할, 내용 추가)을 준다. cap은 이상 길이를 상한까지 압축하되 floor 아래로 내리지 않는다. 모든 대안은 `requires_user_approval`이며 자동 적용하지 않는다.
4. 프레임 단위: 30fps 기본(12~120). 배분은 floor→ideal→ceiling 구간 보간 + 소수부가 큰 장면부터 프레임을 나눠 합이 정확히 목표 프레임이다.
5. 편집 후 재계산(`recheck_duration`): 사용자가 정한 장면 길이는 바꾸지 않고 발화 잘림, 반복 위험, exact/cap 위반, 음악 불일치를 알린다. 위반이면 같은 요청의 맞춤안도 준다. 대본을 바꾸면 발화 바닥이 다시 계산된다.
6. 입력 경계: 0, 0.999, 음수, 900.001, NaN, ±Infinity, JSON `true`는 모두 422. 1과 900은 허용.

장면은 정보 항목(`info_items`)에서만 생긴다. 항목이 없으면 서사 구조 템플릿으로 추정하고 그렇다고 표시하며, 긴 목표로 장면을 채우지 않는다.

## 계획 → 승인

`POST /plans`(브리프 + 의도 + 길이 + 스타일) → 계획(예상 러닝타임, 판단 이유, 필요 길이 범위, 장면, 판정) → `PATCH /plans/{id}`(장면 편집은 재계산, 길이 변경은 재계획, revision 충돌은 409) → `POST /plans/{id}/approve`. 단어 힌트 초안, 미해결 스타일 충돌, 길이 충돌, 차단된 판정, 위반이 있으면 승인이 거부된다. 승인은 revision·프레임 수·장면 해시에 묶이고 편집하면 사라진다. 렌더 게이트(`render_gate`)는 계산되지만 렌더 실행은 아직 연결되지 않았다(8.7).

## 알려진 한계와 반례

- 발화 추정은 표본 하나와 한 엔진 기준이다. 빠른 화자나 영문·숫자 위주 대본에서 어긋날 수 있다. 실측 오디오 전까지 `speech_source=estimate`로 표시된다.
- `POST /intent/extract`는 기존 관리형 Qwen 서비스에 연결돼 있다. 모델 출력은 IntentSpec으로 파싱하고, 브리프 안에 없는 인용은 거부하며, 선택·제약은 전부 `llm_inferred` soft로 낮춰 사용자 결정을 대신하지 못하게 한다. 추출 결과에는 확인 질문이 붙고, 계획 작성 UI도 검토 체크 후에만 이 의도를 보낸다. 이 세션에서는 API/UI 경로를 mock으로 검증했으며 실제 Qwen 추론은 실행하지 않았다.
- 화면에서 추출안을 직접 편집하는 기능은 아직 없다. 틀린 결과는 브리프 수정 후 재분석해야 한다.
- 레시피 사례 연결(`Recipe.cases`)은 사람이 지정한다. R19 사례 기록은 박자 컷 장면이며 립싱크·댄스를 포함하지 않는다.
- 스냅샷은 수동 생성이고 이 호스트 값이다. 배포 환경의 실제 상태가 아니다. 갱신: `python -m ttakkari.creative.v2.probe --out src/ttakkari/creative/v2/data/capability_snapshot.json` 뒤 `python -m ttakkari.creative.v2.report > docs/design/creative-studio-v2-capability-matrix.md`.
- 플랫폼 상한은 YouTube Shorts 180초만 근거(YouTube 도움말 15424877, 2026-10-22 확인)가 있다. 다른 플랫폼은 사용자가 근거와 함께 지정한다. Instagram Reels는 20분까지 만들 수 있고 3분 초과는 신규 노출 추천에서 제외된다는 공식 안내가 있어 hard 상한으로 등록하지 않았다.
- v1 `ProjectInput`은 여전히 3~240초이고 v1 화면은 4분까지만 선택된다(더 긴 영상은 제작 계획에서). 15분 실제 렌더는 8.7 이후다.
- UI는 jsdom 테스트와 빌드·lint까지만 확인했다. 브라우저 렌더 스크린샷은 찍지 않았다.
- QA 평가 모델(`qa.visual_semantic`)은 등록·실행된 적이 없다.

## 다음 단계 (WBS)

8.7 장편 계층(Project→Sequence→Scene→Shot→Asset)·장면별 렌더·캐시·복구, 8.8 QA·피드백·지식·ML 연결, 8.9 실제 영상 7종, 8.10 독립 검수·배포. 전문 능력 구현(레시피 차단 10개의 누락 능력: anim.expression, gen.layer_separation, research.fact_check 등)은 8.5 후속으로 남는다.
