# Creative Studio v2 제작 의도 계약 v2와 동적 노출 규칙

- 상태: 계약 초안 + 백엔드 최소 구현 (2026-11-21, 브랜치 `feat/creative-intent-v2-20261121`)
- 코드: `src/ttakkari/creative/v2/intent_v2.py`(모델·v1 승격·자유 입력 해석), `facets.py`(노출 규칙 데이터와 순수 함수 엔진), `intent_resolve.py`(조립), `src/ttakkari/api/creative_v2_intent.py`(API)
- 시험: `unit_tests/test_creative_v2_intent_v2.py`
- 따르는 사람: 프론트 적응형 워크스페이스 구현자, AI 감독 구현자. 이 문서와 코드가 다르면 코드가 정본이고 문서를 고친다.

## 1. 범위와 출발점

사용자 요구 A(자유형과 구조화 입력의 단일 의도)와 B(조건 조합에 따라 달라지는 질문·설정)를 서버 계약으로 만든다. 출발점은 백엔드 갭 문서 §4의 IntentSpec 제안(schema_version 2, `user_confirmed`, 구조화된 `open_questions`)이다. 아래 네 가지는 제안과 다르게 정했다.

| 제안 | 이 계약 | 근거 |
|---|---|---|
| `Selection.source`에 `user_confirmed` 추가 | 별도 축 `origin`(누가 정했나) 4값으로 교체 | v1 `source`는 "누가"와 "어떻게"가 섞여 있다. `lexical_hint`는 방법이고 `user_explicit`는 사람이다. 한 열거형에 섞으면 "AI가 사용자 문장을 해석한 값"을 어디에 둘지 모호해진다. |
| `open_questions`를 구조화 목록으로 확장해 의도에 저장 | 질문은 저장하지 않고 매 요청마다 조건에서 계산한다(`layout.questions`). 의도에는 답변(`answers`)만 저장한다 | 질문은 조건의 함수다. 저장하면 조건이 바뀐 뒤에도 낡은 질문이 남는다. 답변은 사용자의 결정이라 저장해야 한다. |
| `confirmed_at`·`confirmed_by` 평면 필드 | `confirmation {at, by, revision}` 객체. `origin=user_confirmed`일 때만 존재 | 확정 여부와 기록이 어긋나는 상태를 모델 검증으로 막는다. |
| C01~C12만 사용 | `tone` 확장 차원 추가(공포·액션·감성·코미디·웅장·차분) | 시나리오 A의 "공포", C의 "감성"은 C01~C12 어디에도 없다. 카탈로그에 넣으면 매칭·레시피 판정과 기존 시험 개수가 바뀌므로 노출 규칙에서만 쓰는 확장으로 둔다. |

LLM 호출은 이번 범위에 없다. 자유 입력 해석은 기존 `hint_intent`·`duration_hint`(단어 힌트)를 재사용하고, LLM 추출 결과(`/intent/runs`의 v1 IntentSpec)는 `intent` 필드로 넘기면 v1 승격 규칙으로 받는다.

## 2. IntentSpec v2 필드

```
IntentSpecV2 {
  schema_version: 2
  revision: int ≥1                       // resolve 1회마다 +1
  brief: str                             // 자유 입력 원문
  conditions: Condition[]                // 구조화 조건(카탈로그 항목 또는 tone)
  duration: DurationPolicy | null        // null = 길이 충돌 미해결
  aspect: AspectChoice | null
  info_items: InfoItem[]                 // v1과 같은 전달 항목
  music_seconds: float | null
  references: ReferenceAsset[]
  answers: Answer[]                      // 질문에 대한 사용자 답
  conflicts: IntentConflict[]
  unsupported: UnsupportedItem[]
  rejected: str[]                        // 사용자가 지운 조건. 같은 브리프를 다시 읽어도 되살리지 않는다
  history: HistoryEntry[]
  migrated_from: 1 | null
}
Condition {
  id: "C03.japanese_anime" | "tone.horror"
  dimension: "C01".."C12" | "tone"       // id와 맞지 않으면 거부
  role: primary | secondary
  strength: required | preferred
  origin: user_confirmed | user_free_text | ai_inferred | default
  confidence: 0..1
  evidence: [{quote}]                    // 브리프 인용
  confirmation: {at, by, revision} | null
  note: str
}
```

## 3. 출처(origin)와 강도(strength)

| origin | 뜻 | 만드는 경로 | 확정인가 |
|---|---|---|---|
| `user_confirmed` | 사용자가 구조화 입력·확인 버튼·질문 답·충돌 해결로 직접 정함 | `structured.selections`, `confirm`, `answers`, `resolutions` | 예 |
| `user_free_text` | 사용자가 쓴 문장에 그 낱말이 그대로 있고 서버의 결정적 사전(카탈로그 hints, tone 사전, 길이 정규식)이 매핑함 | `read_free_text` | 아니오 |
| `ai_inferred` | 해석·추정. 인용이 있어도 문장이 그 조건을 직접 말하지 않은 경우(예: "Kafka" → 기술 설명), LLM 추출 결과 | `DOMAIN_CUES`, v1 `llm_inferred` | 아니오 |
| `default` | 시스템 기본값 | 기본 `DurationPolicy()` | 아니오 |

불변 조건(모델 검증으로 강제):
1. `user_confirmed` ⇔ `confirmation`이 있다.
2. `ai_inferred`·`default`는 `required`가 될 수 없다. AI가 필수 조건을 잠그지 못한다.
3. `user_free_text`는 브리프 인용이 있어야 한다.
4. AI 경로는 어떤 경우에도 `user_confirmed`를 만들지 않는다. 출처가 올라가는 유일한 길은 사용자 동작이며, 그때 `history`에 `condition_confirmed (이전 출처 → user_confirmed)`가 남는다.
5. 같은 조건을 더 낮은 출처가 덮어쓰지 못한다(순위 user_confirmed > user_free_text > ai_inferred > default).

강도: `required`는 계획·엔진이 지키지 못하면 차단 사유가 되는 조건, `preferred`는 근접 목표다. v1 `hard/soft`와 1:1 대응한다.

계획 가능 판정(`ready_for_plan`): 열린 차단 충돌이 없고, 콘텐츠 유형(C01) 조건이 하나 이상 `user_confirmed`여야 한다. 자유 입력·AI가 읽은 유형만 있으면 `blocking: ["unconfirmed.content_type"]`이다. v1의 "확인 전에는 결정으로 쓰지 않는다"를 그대로 잇는다.

## 4. 버전과 이력

- `schema_version`은 문서 형식의 버전, `revision`은 이 의도의 변경 번호다.
- resolve는 상태를 저장하지 않는다. 클라이언트가 직전 응답의 `intent`를 그대로 보내면 `revision`이 1 오르고 `history`가 이어진다. `intent` 없이 보내면 revision 1에서 새로 시작한다.
- `HistoryEntry {revision, at, actor(user|ai|system|migration), action, detail}`. action 값: `created`, `condition_read`, `condition_confirmed`, `condition_rejected`, `answered`, `duration_conflict`, `aspect_conflict`, `conflict_resolved`, `reference_added`, `unsupported_condition`, `migrated_from_v1`, `strength_downgraded`, `legacy_open_question`, `stale_text_removed`, `evidence_pruned`, `info_items_set`, `music_seconds_set`, `answer_cleared`. 최근 400건만 유지한다.
- 계획(PlanStore)은 아직 v1을 받는다. 계획 생성 시 v2 스냅샷과 revision을 계획에 묶는 일은 §10 후속이다.

## 5. 충돌 표현과 해결

```
IntentConflict {
  id                       // 결정적: conflict.duration | conflict.aspect | conflict.catalog.<a>~<b> | conflict.primary.<C0x> | conflict.rule.<rule>~<cond>
  kind: duration | aspect | catalog_conflict | single_primary | rule
  dimension
  message                  // 사용자에게 보이는 설명
  candidates: [{value, label, origin, strength, evidence}]   // 모든 후보를 남긴다
  status: open | resolved
  blocking: bool           // open이면서 blocking이면 ready_for_plan=false
  resolution: {choice, by, revision, note} | null
}
```

- 조용히 버리지 않는다. 길이·비율 충돌이 열려 있으면 유효값(`duration`/`aspect`)을 `null`로 두고 후보를 전부 `candidates`에 둔다. 출처 순위로 자동 선택하지 않는다. 사용자가 구조화로 30초를 넣고 브리프에 "3분"이 있으면 `conflict.duration`(후보 `target:30` user_confirmed, `target:180` user_free_text)이 열린다.
- 길이 충돌 판정: 둘 중 하나라도 strict면 값이 다르면 충돌, 둘 다 target이면 큰 값 기준 15%를 넘게 다를 때 충돌, auto는 충돌하지 않는다(`TARGET_TOLERANCE`).
- 카탈로그 충돌: 활성 조건 쌍에 `conflicts_with` 관계가 있으면 연다. hard 관계이거나 한쪽이 user_confirmed면 차단이다.
- 규칙 충돌: 노출 규칙이 선언한 조합 경고(예: 설명 콘텐츠 + 공포 톤). 기본은 비차단이다.
- 해결: 요청의 `resolutions: [{conflict_id, choice, note}]`. `choice`는 후보 value 하나, 또는 `keep_both`(카탈로그·규칙 충돌에만, 혼합 의도). 카탈로그 충돌에서 하나를 고르면 나머지는 `rejected`로 가고 고른 값은 user_confirmed가 된다. 해결 기록은 다음 요청에도 유지되며, 같은 후보 집합이 다시 나오면 해결 상태로 이어진다.

## 6. 미지원 조건

`UnsupportedItem {id, subject, reason, message, alternatives}`. reason:
- `not_in_catalog`: 카탈로그에 없는 항목 id를 구조화로 고름
- `unknown_tone`: tone 사전에 없는 톤
- `reference_not_ingested`: 받았지만 아직 제작 입력으로 못 쓰는 참조 자산(영상·문서)
- `rule_blocked`: 예약. 능력 레지스트리상 실행 불가는 여기서 판정하지 않고 기존 `/match`의 `gaps`·`render_blockers`가 맡는다

미지원 조건은 오류(422)가 아니라 기록이다. 사용자의 의도를 잃지 않고 "왜 못 쓰는지, 대신 무엇을 할 수 있는지"를 보여 주기 위해서다.

## 7. 길이 정책

```
DurationPolicy { mode: auto | target | strict, seconds: 1..900 | null, platform, origin, evidence }
```
- `auto`: 정보량에서 길이를 정한다. 초를 받지 않는다.
- `target`: 희망 길이에 근접. 기존 v1 `preferred`.
- `strict`: 위반 시 명시 처리. 기존 v1 `exact`(플랫폼 상한은 `platform`과 함께 strict로 승격).
- 30/60/120/240 같은 고정 템플릿은 없다. 계산은 기존 `duration.plan_duration`을 그대로 쓴다(`DurationPolicy.to_request()`).
- 정보량 불균형 조정안: resolve 응답의 `length`가 `info_items`로 `ContentProfile`을 만들어 `plan_duration`을 돌린 결과다. `status`는 `target_met | deviated | strict_met | conflict | auto`이고 `alternatives`(relax_to_feasible, drop_nonessential, speed_up_speech, split_parts, extend_content, raise_cap)는 모두 사용자 승인 필요다. 전달 항목이 아직 없으면 `needs_content`, 길이 충돌이 열려 있으면 `blocked_by_conflict`다. 예: 전달 항목 6개를 16초 strict에 넣으면 `conflict`와 대안 목록이 나온다(시험 `test_length_assessment_uses_existing_duration_calculator`).

## 8. 참조 자산

```
ReferenceAsset { id, kind: image|video|audio|document, role: style|character|music|content|layout|motion, sha256, name, note, origin, usable }
```
- 파일 경로·URL은 받지 않는다. 실제 바이트는 기존 업로드 경로로 올리고 여기에는 식별자와 sha256만 둔다.
- `usable`은 서버가 정한다. 지금 업로드 경로가 있는 image·audio만 true다. video·document는 기록하고 `unsupported`에 `reference_not_ingested`와 대안을 남긴다. `usable=true`도 "제작에 반영됨"이 아니라 "받을 수 있음"이다. 반영 여부는 계획·엔진 단계가 기록한다.

## 9. v1 읽기 호환

`load_intent(doc)`: `schema_version`이 2면 그대로, 없거나 1이면 v1으로 검증 후 `promote_v1`. 3 이상은 거부한다. v1 문서는 바꾸지 않는다(읽기 승격만). v2→v1 쓰기는 하지 않는다.

| v1 | v2 |
|---|---|
| `source=user_explicit` + `extraction.method=user_structured` | `user_confirmed` (confirmation.by=migration) |
| `source=user_explicit`(그 외 방식) | `user_free_text` (인용 없으면 `ai_inferred`) |
| `source=lexical_hint` | `user_free_text` |
| `source=llm_inferred` | `ai_inferred` |
| `source=default` | `default` |
| `strength=hard` / `soft` | `required` / `preferred`. 단 AI 추론 hard는 `preferred`로 낮추고 `strength_downgraded` 이력 |
| `duration.mode` auto / preferred / exact / platform_cap | auto / target / strict / strict(+platform) |
| `open_questions: str[]` | `history`의 `legacy_open_question`. 새 질문은 `layout.questions`가 계산 |
| `aspect`, `info_items`, `music_seconds` | 그대로 |

## 10. 동적 노출 규칙의 데이터 계약

정적 데이터는 `GET /api/creative/v2/intent/facets`로 받는다(`rules_version`, `groups`, `fields`, `rules`, `tones`). 계산은 `facets.resolve_layout(conditions, answers, known)` 순수 함수다.

### 10.1 정적 데이터

```
FacetGroup { id, title, description, tier: essential|advanced }
FacetField { id, group, dimension, label, question, kind: single|multi|number|text, options: [{id, label}], tier }
FacetRule {
  id, priority (0..100, 작을수록 강함: 조합 10, 단일 조건 50, 기본 90)
  when: { all_of: [cond], any_of: [cond], none_of: [cond] }   // cond = 조건 id 또는 "C01.*" 같은 차원 와일드카드
  activate: [group]           // 켤 설정 묶음
  ask: [field]                // 물을 설정
  recommend: [{field, value, reason}]
  hide: [field]               // 이 조합에서 불필요한 설정
  conflicts: [{with: cond, message, blocking}]
  reason
}
```
- 선택지 id가 카탈로그·톤 항목이면(예 `C07.battle`) 그 답은 해당 조건을 user_confirmed로 추가한다. 답이 다시 규칙을 켤 수 있어 progressive disclosure가 된다.
- 규칙 데이터는 import 시 참조 무결성을 검사한다(없는 묶음·설정·조건·선택지를 가리키면 실패).

### 10.2 계산 결과(`layout`)

```
Layout {
  rules_version
  groups: [{id, title, tier, triggered_by: [cond], rules: [rule], reasons: [str], fields: [field]}]
  available_groups: [group]   // 이번 조합에서 꺼진 묶음. 전문 설정에서 열 수 있다
  questions: [{id: "q.<field>", field_id, group, dimension, question, kind, options, recommended, reason,
               triggered_by, rules, mode: choose|confirm, alternatives: [{value, rule, reason}], tier}]
  answered: [{field_id, values, origin}]
  hidden: [{field_id, rules, reason}]
  conflicts: IntentConflict[]
  applied_rules: [rule]
}
```

### 10.3 병합과 중복 제거 규칙

1. 묶음 병합: 여러 규칙이 같은 묶음을 켜면 묶음은 하나다. `triggered_by`·`rules`·`reasons`는 합집합, 정렬 우선순위는 가장 강한 규칙을 따른다.
2. 질문 단일화: 질문 키는 설정 id다. 여러 규칙이 같은 설정을 물어도 질문은 하나이고 `rules`·`triggered_by`에 모두 남는다. `Layout` 모델이 중복 질문을 거부한다.
3. 추천 우선순위: 같은 설정에 추천이 여럿이면 `(priority, rule id)` 순으로 첫 값이 `recommended`, 나머지는 `alternatives`다. 조합 규칙(10)이 단일 규칙(50)을 이긴다. 예: 공포는 템포 slow, 액션은 fast를 권하지만 공포+액션 조합 규칙이 `slow_build_burst`를 정하고 slow·fast는 대안으로 남는다.
4. 질문이 숨김을 이긴다: 어떤 규칙이 묻는 설정은 다른 규칙이 숨기지 못한다(설명+뮤직비디오 조합의 `sync.cut_on`).
5. 묻는 설정의 묶음은 자동으로 켠다. 질문만 있고 묶음이 없는 상태를 막는다.
6. 이미 답한 설정은 묻지 않는다: `answers`에 있거나, 선택지 중 user_confirmed 조건이 있거나, `known`(길이·비율)이 user_confirmed면 `answered`로 간다.
7. 확정 안 된 값이 있으면 확인 질문: 선택지 중 user_free_text·ai_inferred 조건이 있거나 `known`이 확정 전이면 `mode=confirm`, `recommended`=그 값, 규칙 추천은 대안이다.
8. 추천 근거가 없으면 `recommended=null`이고 "고르지 않으면 AI가 장면에 맞게 제안"이라고 적는다(요구 A의 미선택 시 AI 제안).
9. 숨김은 삭제가 아니다. `hidden`에 이유와 함께 남고, 꺼진 묶음은 `available_groups`로 남아 전문 설정 접근을 유지한다.

## 11. API

`POST /api/creative/v2/intent/resolve` (소유자 인증, 상태 저장 없음)

요청:
```json
{
  "brief": "감성적인 음악에 어울리는 3분짜리 뮤직비디오를 만들어 줘. ...",
  "intent": null,
  "structured": {"selections": [{"id": "C01.music_video", "role": "primary", "strength": "required"}],
                 "duration": {"mode": "target", "seconds": 30}, "aspect": "16:9"},
  "answers": [{"field_id": "sync.cut_on", "values": ["section"]}],
  "confirm": ["tone.emotional"],
  "reject": [],
  "resolutions": [{"conflict_id": "conflict.duration", "choice": "target:30"}],
  "references": [{"id": "song", "kind": "audio", "role": "music", "sha256": "..."}],
  "read_free_text": true
}
```
- `intent`는 직전 응답의 v2 문서 또는 저장된 v1 문서.
- 처리 순서: reject → 자유 입력 읽기 → 구조화 선택 확정 → confirm → answers → 길이·비율 병합 → 참조 → 충돌 계산 → resolutions → 화면 재계산.

응답: `{intent: IntentSpecV2, layout: Layout, length: LengthAssessment, ready_for_plan: bool, blocking: [str]}`. 잘못된 설정 id·선택지, 없는 충돌 id, 의도에 없는 조건 확정은 422다. 라우터는 `api/creative_v2.py`가 아닌 `api/creative_v2_intent.py`에 둔다(integ 이식 작업과의 충돌 회피).

## 12. 시나리오 결과 (resolve 1회, 브리프만, 2026-11-21 실행)

| | A 일본 애니+SF+공포+액션 약 2분 | B Kafka 16초 | C 감성 뮤직비디오 3분 |
|---|---|---|---|
| 조건(출처) | C01.animation, C02.space_sf, C03.japanese_anime, tone.action, tone.horror (모두 user_free_text) | C01.tech_explainer (ai_inferred, 근거 Kafka·Producer·Broker·Consumer) | C01.music_video, tone.emotional (user_free_text) |
| 활성 묶음 | action, character, consistency, cut_direction, drawing, horror, lighting_vfx, space_camera, world, output | code, diagram, explain, motion_graphics, narration, output | av_sync, cut_direction, lighting_vfx, lyrics, music, output |
| 질문(추천) | action.kind=C07.battle, character.design, character.expression=exaggerated, cut.impact_frames=yes, cut.tempo=slow_build_burst, drawing.shading=cel, horror.gore=mild, horror.style, light.key=C05.neon, camera.moves, world.locations=C05.space, world.tech_design=mecha, output.aspect, output.duration=target(확인) | diagram.motion=continuous_flow, diagram.type=flow, explain.audience=beginner, mg.style=C03.vector_motion_graphics, voice.narration=ko, output.aspect, output.duration=target(확인) | sync.cut_on=section, sync.motion_source=generated_video, sync.transition=C10.dissolve, cut.tempo=slow, light.key=C05.dusk, lyrics.has, music.source, output.aspect, output.duration=target(확인) |
| 숨김 | 없음 | character.expression, character.lipsync, drawing.frame_rate, sync.cut_on | diagram.type, explain.audience, vfx.types, voice.narration |
| 계획 가능 | 아니오(unconfirmed.content_type) | 아니오 | 아니오 |

C에 구조화 길이 30초를 함께 넣으면 `conflict.duration`(후보 `target:30` user_confirmed, `target:180` user_free_text, blocking)이 열리고 `duration=null`, `length.status=blocked_by_conflict`다.

알려진 한계(이 표에서 보이는 것): A의 "단편"은 카탈로그 힌트에 없어 C01.story_film으로 읽히지 않고, "애니메이션 스타일"이 C01.animation으로도 읽힌다. 단어 힌트의 한계이며 사용자 확인 단계나 LLM 추출(`/intent/runs`)이 보완한다.

## 13. 이어서 검증할 범위

- 질문·추천 문구의 품질은 규칙 데이터 수준이다. AI 감독이 장면 단위 질문을 더할 때도 같은 `Question` 모양과 병합 규칙(설정 id 단일화)을 따른다.
- 립싱크·생성 영상 등 능력별 상태와 제약은 [레시피·능력 행렬](creative-studio-v2-capability-matrix.md)에서 확인한다. 렌더 요청은 실제 실행기 의존성과 사양을 다시 검사한다.
- tone 사전 확장, 카탈로그 정식 편입 여부는 매칭 영향 검토 뒤 결정한다.

## 14. 계획과 스토리보드 연결

- `POST /plans`와 `POST /storyboard/runs`는 확정한 `intent_v2`를 받는다. 계획용 투영은 기존 매칭·길이 계산·Style DNA를 사용한다. PlanStore는 원래 의도와 투영 결과를 함께 저장하고, 계획에서 길이를 변경하면 의도 revision과 이력을 갱신한다.
- 스토리보드 결과를 검토하고 생산 계획으로 옮길 때 사용자 의도와 화면 비율을 보존한다. 모델이 제안한 장면은 계획에 기록하고, 참조·답변 중 소비할 수 없는 값은 투영의 미반영 목록에 기록한다. 제작 승인은 별도다.
- Kafka 기술 설명은 검토한 세 장면이 모두 `kafka_flow`를 지정했을 때 R22를 사용한다. 근거와 제약은 [능력 행렬](creative-studio-v2-capability-matrix.md), 과거 실측은 [실행 기록](../../src/ttakkari/creative/v2/data/execution_records.json)에 남긴다.
- Kafka 템플릿의 장면은 발행, 파티션 로그 기록, 소비 순서로 구성한다. 오프셋은 파티션 안에서 레코드의 위치를 나타낸다. [Apache Kafka 공식 입문 문서](https://kafka.apache.org/43/getting-started/introduction/)
- 2026-11-21 검증에서 Kafka 16초 계획은 승인 후 디스패치 사양 검사를 통과했다. Codex 명령 환경에서는 Chromium 권한 검사에서 `mach-register`와 `mach-lookup`이 거부되어 HyperFrames 렌더를 시작하지 않았다. 별도 2D 경로는 실제 8.021초 MP4를 만들었다. 1280×720, 192프레임, 오디오 트랙으로 기계 검사 통과를 확인했고 독립 내용·시청·청취 품질은 미확인으로 남겼다.
- 같은 날 승인된 브라우저에서 Kafka HTML 컴포지션을 2초, 7초, 13초로 이동해 준비한 각 자막과 메시지 위치를 확인했다. 세 장면의 길이는 4초, 5초, 7초이며 자막은 720px 출력에서 y=627–693px에 표시됐다. 이 확인 범위는 HTML 컴포지션이다.
- 정지 이미지 편집의 장편 검증에서 75장면을 15구간으로 분할해 큐의 구간 작업을 실행하고 결과를 합쳤다. FFprobe는 900.032초, 1280×720, 영상 21,600프레임과 오디오 트랙을 확인했다. 기계 검사 통과, 내용·시청·청취 미확인으로 기록했다. 합성된 테스트 파일 해시는 `e2fa0f1f047fd6b5be1328c32206ed9a6ea8bbc8519ab22a7abb4bf35abe96c4`다.

## 15. AI 감독 실제 실행과 독립 의미 평가 (2026-11-21)

재현 스크립트는 `scripts/creative_director_eval.py`다. 격리 PostgreSQL 클러스터와 임시 계획 저장소만 쓰며 운영 DB와 백엔드는 건드리지 않는다.

- 계층: 스토리보드 출력에 `sequences`(시퀀스 ID, 제목, 목적, 장면 ID 목록)를 추가했다. 시퀀스가 있으면 모든 장면이 정확히 한 시퀀스에 속해야 하고, 계획의 `director.sequences`에 저장된다. 장면·샷·레이어·대안·샷 계약(`output_contract`, `quality_criteria`)은 기존 필드이며 프롬프트가 작성을 요구한다.
- 생성 경로: codex 엔진, 모델 gpt-6-luna. 앱의 워커 Run(`spawn_worker`)이 프롬프트를 수행했고, 그 JSON을 격리 DB의 스토리보드 Run 결과로 저장해 `storyboard_runs.read`와 `accept`를 통과시켰다. 격리 DB의 Run 상태는 하네스가 기록했고 앱의 Run 관리자가 실행한 것이 아니다.
- 시나리오 A(애니 SF, 확정 조건 7개)는 3 시퀀스·6 장면, B(Kafka 16초)는 1 시퀀스·3 장면(code_motion kafka_flow)으로 저장됐다. A의 모든 장면이 `generative_video`를 골랐다. 이 표면은 이미 만들어진 생성 영상의 재생만 지원하므로 제작 승인 전에 사람이 확인해야 한다.
- 평가자: `review_cli_evaluator.py`가 `claude -p`(Read 도구)로 전달된 PNG 프레임을 읽는다. 스트림 로그에서 실제로 열린 프레임만 근거로 인정한다. 생성 엔진은 codex 또는 qwen-local이고 평가 엔진은 claude CLI이므로 모델 계열과 프로세스가 다르다. `independent` 값은 평가자 응답이 아니라 서버 바인딩이 정한다. 영상 재생과 소리는 평가하지 못하므로 `viewing_qa`는 unknown으로 남는다.
- 운영 연결: `TTAKKARI_CREATIVE_REVIEW_CLI_MODEL`을 지정하면 `create_app`이 평가자를 묶는다. 비우면 기존처럼 `not_connected`와 unknown이다. 이 설정은 기본값이 꺼져 있고 운영에서 켜지 않았다.
- 2D MP4(SHA `0341dd63…a2`)를 이 평가자로 돌려 `semantic_qa`가 fail(자막 바와 캐릭터 발 겹침 결함 1건), `style_continuity`는 pass로 저장됐다. 불확실도가 기준(0.25)을 넘은 `intent_alignment`와 `text_legibility`는 정책대로 unknown이다. 이 파일은 평가 계획에 묶은 기존 결과물이며 이번에 다시 렌더하지 않았다.
- 평가자 격리(독립 검증 반영): CLI는 `--restricted --tools Read --strict-mcp-config --disable-slash-commands`로 실행한다. 파일 도구는 review 디렉터리로 한정되고 사용자 설정·훅·MCP는 로드되지 않는다. review 밖 파일 읽기는 CLI가 `is_error`로 거부했다(haiku 실제 호출). 근거 프레임은 Read 호출에 성공한 `tool_result`가 있는 것만 인정한다. 평가자 실행이 실패하면 `review_execution`에 `status=evaluator_failed`, `verdict=unknown`, 오류 요약을 남기고 예외는 그대로 올린다. 장면 제목·나레이션이 프롬프트에 들어가고 평가 근거 문장이 영수증에 저장되므로 계획과 같은 민감도로 다룬다.

## 16. 조건 → 제작 결정 연결 (2026-11-22)

코드: `production_decisions.py`(결정 도출), `plans.py`(생성·편집 때 호출), `dispatch.py`(렌더 제어 적용), `matching.py`(톤 점수), `engines.limit_violations`(규격 판정). 시험: `unit_tests/test_creative_v2_intent_wiring.py`.

**자연어 수정 직후 이전 의도.** `/intent/resolve`는 `intent`와 새 `brief`를 받으면 새 브리프에 근거 인용이 없는 자유 입력·AI 해석 조건과 길이·비율을 걷어 낸다(`stale_text_removed`). 확정 조건은 남기고 사라진 인용만 지운다(`evidence_pruned`). `POST /plans`는 현재 브리프에 인용이 없는 조건(확정 포함)을 보면 422 `intent_stale`로 거부한다. 클라이언트가 resolve를 건너뛰어도 이전 문장의 의도로 계획이 만들어지지 않는다.

**입력 추가(하위 호환).** `ResolveRequest.info_items`·`music_seconds`는 선택 필드다. 없으면 직전 의도 값을 이어받고, 빈 목록은 비우기다. 전달 항목이 있으면 계획의 장면(`basis=info_items`)과 길이 산정이 이 항목에서 나온다.

**`plan.production_decisions`.** 답변·톤·추천값·카탈로그 조건·합성 Style DNA를 작화(`visual`)·조명·카메라·음향·전환·편집(`editing`)·액션(`motion`)·정보 구조(`information`)의 결정으로 푼다. 결정마다 `origin`(user_confirmed, user_free_text, ai_inferred, rule_recommended, style_dna)과 실행 상태를 둔다.

| status | 뜻 |
|---|---|
| `executable` | 선택된 엔진 어댑터가 소비하는 렌더 제어(`render_controls`)가 있고 사용자의 말에서 나온 값이다. dispatch가 Style DNA 기본값보다 앞서 적용하고 기록(`style.user_controls`)에 남긴다. |
| `plan_only` | 계획·장면(`scene.production`)·AI 감독 입력에만 반영된다. `status_reason`에 이유가 있다(추천값은 확정 전, 엔진이 값을 받지 않음, 실행기 미연결 등). |
| `unsupported` | 필요한 능력이 없거나 미검증이다. 사유를 `intent_projection.not_applied`에 남기고 장면 지시에서는 뺀다. |

현재 렌더 제어로 이어지는 값은 정지 이미지 합성기가 받는 `transition`, `animation`, `caption_style`뿐이다(전환 cut/dissolve, 카메라 dolly/tracking/crane, 자막 표시). 나머지는 plan_only다. 소비된 답변은 `not_applied`에서 빠지고 `intent_projection.items`에 `kind=answer`와 `execution`으로 나타난다. 현재 조건 조합에서 숨겨진 설정의 답변은 `production_decisions.not_applied`에 이유와 함께 남는다.

**장면 기록.** `scene.production.by_category`에 장면에 적용되는 지시가, `scene.information`에 장면 길이 대비 정보량(`ratio`, `treatment` full·condensed·minimal·extended, 비필수 `drop_candidate`)이 남는다. 컷 템포 `slow_build_burst`는 장면 위치(앞 60%/뒤 40%)로 빌드업·폭발 지시를 나눈다.

**필수 조건 연결.** `production_decisions.required_conditions`는 필수 조건이 레시피 fits, Style DNA, 렌더 제어, 장면 기록 중 어느 경로로 이어지는지 적는다. 유형·세계관·화풍·구조·전환 계열이 어디에도 이어지지 않으면 `unrealized`이고 승인 차단 사유(`render_blockers`)다. 사용자가 말한 길이를 정보량이 크게 못 채우면(`deviation_level=excessive`) `production_decisions.length.treatment`가 `capacity_short` 또는 `condensation_needed`이고 같은 차단 사유가 된다. 대안(내용 추가, 길이 조정)은 사용자 승인이 필요하다.

**레시피 매칭과 톤.** `match_intent(tone=...)`는 톤을 드러내는 카탈로그 항목(`matching.TONE_FITS`)이 레시피 `fits`에 있으면 점수에 0.10을 가산한다. 레시피를 적격으로 만들지는 않는다. 톤이 없으면 지문이 달라지지 않는다. 계획은 매칭에 쓴 톤을 `match_tone`에 저장해 `get()`이 같은 매칭을 다시 계산한다(이 필드가 없는 기존 계획은 톤 없이 계산해 승인이 유지된다).

**엔진 한도와 장면별 엔진.** `production_decisions.engine_fit`은 화면비·장면 수·장면 길이·구간 길이와 어댑터의 `validate_spec`(프리셋 엔진이 소비하지 못하는 카메라·모션 조건 포함)을 렌더 전에 구간별로 판정한다. `status`는 ok, blocked, no_engine(필수 능력을 맡는 제작 엔진이 없어 비교 불가)이다. 장면마다 다른 엔진은 검토된 `director`의 `strategy`로 이미 가능하고, 구간마다 어댑터가 정해지며 `mixed_engines`로 표시된다. 매칭은 조건을 버리고 R21로 낮추지 않는다. 어떤 풍부한 레시피도 맞지 않으면 R21이 후보가 되지만, 필수 조건은 `required_conditions`가 연결 여부를 따로 판정한다.

**품질 검수.** 사전 루브릭(`quality.build_rubric`)이 사용자 조건에서 나온 결정(`intent_decisions`)과 장면별 지시를 검수 입력으로 받는다.

**빈 답(선택 해제).** `answers[].values: []`는 그 설정의 선택을 모두 해제한다(422가 아니다). 선택지가 곧 조건인 값(예 `C06.dolly`)은 지운 조건(`rejected`)이 되어 같은 브리프를 다시 읽어도 되살아나지 않고, 다시 고르면 정상으로 돌아온다. 선택 설정은 `values: []`인 명시적 답으로 남아 질문이 추천값을 다시 채우지 않으며 제작 결정도 만들지 않는다. 필수 설정(`content.type`, `output.duration`, `output.aspect`)은 저장된 답을 지우고 미확정으로 돌린다: 콘텐츠 유형 조건은 제거되어 `missing.content_type`로 계획이 막히고, 길이는 기본(auto, origin default), 비율은 null이 되며 질문으로 다시 나온다. 한 설정만 답하는 single 필드에 값을 둘 이상 보내는 오류와 선택지 검증은 그대로다.

## 17. 데이터 흐름 설명 → R22 자동 연결

코드: `flow_template.py`, `plans.build_plan`(장면 지시 자동 구성), `plans._match_plan`(범위 판정). 시험: `unit_tests/test_creative_v2_flow_template.py`.

R22(HyperFrames 다이어그램 흐름)는 이전에 검토된 `kafka_flow` 장면 지시가 있을 때만 선택됐다. 지금은 의도 v2로 계획을 만들 때 장면 지시가 없으면 `flow_template.plan_direction`이 요청이 템플릿 범위 안인지 판정하고, 안이면 `source=recipe_template` 장면 지시를 만들어 같은 경로를 탄다. 판정 기준은 제품 이름이 아니라 템플릿이 선언한 주체 개념이다.

- 대상: 콘텐츠 조건이 템플릿 `fits`(기술 설명, 벡터 모션그래픽, 설명형 구조)에 있을 때만 평가한다.
- 범위 안: brief가 개념(producer, broker, consumer, partition, offset과 한국어 별칭) 3개 이상을 말하고 발행자·소비자를 포함하거나, 템플릿의 주제어를 직접 말한다. 화면비 16:9, 전달 항목이 있으면 3~5개.
- 장면: 전달 항목이 있으면 그것을 장면으로, 없으면 템플릿 막(발행·기록·소비, 병렬 파티션, 오프셋 커밋)을 쓴다. 개수는 개념으로 하한을 정하고 기존 `plan_duration`이 요청 길이를 담는 가장 작은 수를 고른다(막 안에서만 늘린다).
- 범위 밖: 장면 지시를 만들지 않고 기존 매칭을 그대로 쓴다. `plan.template_scope`에 `status=out_of_scope`, 사유, 누락 개념, 지원 범위를 남기고 `notes`에도 적는다. 범위 안이면 `status=applied`와 근거 개념, 장면 출처(`request_items` 또는 `template_acts`)를 남긴다.
- `ProductionDirection.source=recipe_template`은 서버가 결정적으로 만든 값이라 `reviewed=false`를 허용한다. 모델 출력이나 사용자 검토가 아니며, 렌더 게이트는 계획 승인 이후에만 열린다.
- 템플릿은 현재 메시지 흐름 한 종이다. 다른 데이터 흐름(캐시, 요청 라우팅 등)은 범위 밖으로 사유와 함께 기존 레시피로 간다. 새 템플릿은 `flow_template.TEMPLATES`에 선언을 추가한다.
