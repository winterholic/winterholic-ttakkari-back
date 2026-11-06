# Stage3/UI 실제 관측 보충 — Kafka lexical 초안의 지도 요구 (2026-11-07)

이번 보충의 로컬 교정·비교·분류는 완료했다. 운영 적용·동일 Kafka brief의 실추론·원격 executor 소비·인증된 UI 성공 여정은 미완료다. 운영 계획의 gate를 열거나 계획을 수정하지 않았다. 새 API는 만들지 않았다.

## 1. 실제 운영 관측과 환경 구분

UI 정본은 frontend worktree의 [ui-core-compatibility-20261107.md](../../../front-creative-production-20261107/docs/creative-studio/ui-core-compatibility-20261107.md) 및 [ui-integration-evidence-20261107.md](../../../front-creative-production-20261107/docs/creative-studio/ui-integration-evidence-20261107.md)다. 이번에는 이를 읽고 frontend의 기존 result-link 호출과 과거 caption 초안 불러오기 구현도 대조했다. frontend를 변경하거나 기존 fixture 브라우저 검사를 반복하지 않았다.

```text
ttk get /api/version
revision=529022861120e86f80400ffaec53dd3f3f7f81c6
ttk get /api/creative/v2/plans/3bf9f754-0039-4fe3-a771-e137b9235068
revision=1, intent_status=unconfirmed_hint, render_gate=false, primary_recipe=R16
ttk get /api/creative/v2/plans/3bf9f754-0039-4fe3-a771-e137b9235068/production
state=not_dispatched; gate 차단 사유 유지
```

같은 현재 배포에서 `GET /api/creative/v2/storyboard/contract`와 해당 plan의 `GET /production/revisions/1`은 HTTP 404였다. 로컬 Stage3/Stage2 경로가 운영에 반영됐다고 주장하지 않는다. `/health`와 `/openapi.json`도 404였으나 잘못된 health URL·비활성 OpenAPI로부터 서비스 장애나 전체 API 부재를 추론하지 않았다. 올바른 version 경로는 `/api/version`이다. `main.py`는 prod에서 OpenAPI를 끈다. 배포 commit의 API route 선언도 git object에서 별도로 확인해 증거 JSON에 보존했다.

로컬 HEAD는 `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`이며 기존 core/producer/UI 연동 변경이 dirty다. 조사 시작 당시 `matching.py`와 `registry_data.py`의 바이트는 배포 revision의 git blob과 각각 같았다. `storyboard.py`는 해당 배포 commit에 없다. 배포 디렉터리·비밀 설정은 읽지 않았다. 실제 배포 상태는 API 관측, 소스의 연결 여부는 해당 commit/로컬 worktree로 구분했다.

## 2. 유입 경로와 좁은 교정

운영 JSON과 로컬 동일 brief 재계산에서 같은 경로를 재현했다.

1. `hint_intent`는 `기술 설명` → C01.tech_explainer, `내레이션` → C09.narration, `자막` → C10.subtitles의 soft·secondary·confidence 0.3 초안을 만든다. 지도·다큐 힌트는 선택되지 않았다.
2. R16 다큐멘터리의 `fits`에 일반 음성 선택인 C09.narration이 포함돼 있었다. narration 하나로 R16 fit이 0.3333이 된다.
3. matcher는 fit>0인 비기준선 레시피가 있으면 R21 기준선을 후보에서 제외한다. 이 정책 아래 R16 0.2733이 기술 설명 R03 0.2533보다 높아 주 레시피가 된다. R21의 원래 점수 0.4733이 높아도 자동 fallback하지 않는 기존 정책이다.
4. 선택 레시피의 `requires`를 합치면서, 원래 직접 필요 능력 목록에 없던 research.fact_check와 research.map_data가 sources=[]·must·missing으로 추가된다. 운영 trace의 `needs`는 최초 8개지만 최종 needs에는 이 두 개가 더 있다. sources=[]가 사용자 지도 요청의 증거는 아니다.

교정은 `registry_data.py` R16의 `fits`에서 **C09.narration만 제거**했다. C01.documentary·C08.documentary_form은 유지한다. R16의 연구/지도 요구, 카탈로그·능력 실행 증거, rich 레시피 우선 정책, 승인·의도 확인·능력 gate는 바꾸지 않았다. 기술 흐름을 다큐로 오인하는 선택 연결을 고친 것이다.

| 비교 | 주 레시피 | 의도 / gate | 연구 능력 |
|---|---|---|---|
| 실제 운영 Kafka 계획 r1 | R16 | unconfirmed_hint / false | fact_check·map_data must missing |
| 교정 전 로컬 동일 brief | R16 | unconfirmed_hint | 운영과 같은 유입 재현 |
| 교정 후 로컬 동일 brief | R03 | unconfirmed_hint / false, lab_manual | 잘못 유입된 두 요구 없음 |
| 명시적 다큐 선택 반례 | R16 | 미승인 / false | 기존 연구/지도 요구 선언 보존 |

로컬에서 지도 요구가 사라진 사실은 Kafka 설명의 정확성 검증이나 렌더 지원을 뜻하지 않는다. brief의 사실 정확성 요구는 실제 검수 대상으로 남아 있다. R03은 lab_manual이고 의미·시청 평가는 unknown이다. A의 code_motion을 렌더하려면 기존 계약·승인·실행 가능성 검사가 별도로 필요하며 브라우저 차단을 우회하지 않았다.

## 3. 기존 실제 Stage3 감독 결과와의 차이

기존 실추론 Run `90bddc9c-fb91-44fc-8364-fd67c61d21a7`의 저장소 원문 `actual-resume-latest.json.result_text`를 읽었다. status=succeeded와 plan에 기록된 output SHA `2ee5207a0ff51bbafaac7eb8739a8c30ab8c8b371b0c395fd590032e2b8d5b9d`·run_id를 대조하고 기존 materialize로 현재 코드에서 재검증했다.

이 Run은 **기존 B 영상의 9초 재사용** 요청이다. Kafka lexical 초안과 동일 brief의 대조 실험이 아니다. 모델은 C08.three_act, slideshow, 기존 프레임 보존·샷/레이어·대안·출처 인용을 반환했고, 현재 재검증도 confirmed/R21/studio_runtime이다. 재검증으로 만든 로컬 계획은 **미승인**으로 두었으며 approve/render/consumer를 다시 실행하지 않았다. 이전에 실행된 9.03초 제작·승인·로컬 consumer 증거는 Stage3 원기록을 그대로 재사용한다.

차이는 lexical의 저신뢰 단어 선택과 검토된 모델의 명시적 카탈로그·장면 전략·샷·대안·출처 구조다. 모델 결과도 별도 계획 승인 없이는 실행할 수 없다. 과거 모델의 잘못된 catalog/capability 출력 실패도 삭제하지 않았다. 기존 완료 실추론은 개인 Codex 계정의 과거 기술 증거이며 새 ai2 실추론으로 표시하지 않는다.

동일 Kafka brief의 실제 감독 Run은 이번 보충에서 생성하지 않았다. 기존 실제 Stage3·B 보간 여정을 반복하지 말라는 지시와 직렬 소유 순서를 유지했다. Kafka의 실제 감독→계획→운영 executor 소비 성공은 **미검증**으로 남긴다. 이번에는 과거 모델 출력의 재수용 검증과 lexical 유입 원인 교정만 수행했다.

## 4. UI 미충족 항목의 현재 분류와 재사용 순서

| 항목 | 로컬 backend/UI | 운영·실검증 경계 / 다음 사용 |
|---|---|---|
| final/segment MP4 미리보기 | **구현**: 기존 POST result-link `{revision,result_id}` → 서명 URL, plan/revision/result/SHA 검증. frontend creativeV2.ts의 result-link 클라이언트가 재사용 중 | 현재 배포 commit API에는 이 Stage2 경로가 없음. 우선 통합/배포 후 기존 API 사용. 새 scene-preview API로 대체하지 않음 |
| 과거 caption의 새 revision 재적용 | **지원 범위 구현**: revisions scenes/repairs → 동일 target의 현 repair proposal에 이전 문구를 초안으로 불러오기 → repair → applied_needs_approval → 사용자 승인 → 같은 caption_mode dispatch/render | 현재 revision 조회는 운영 404. 전체 rollback은 없음. 고정 producer caption·음성 자산 있는 narration·모션 편집은 기존 blocked 유지 |
| 일반 scene 이미지/shot asset 미리보기 | **미구현**: final/segment MP4 링크와 다른 기능. UI도 미지원 안내 | 기존 segment 결과가 있으면 위 MP4 링크를 우선 재사용. 임의 preview_link를 지어내지 않음 |
| 임의 brief 첨부 | **미구현**: plan 사전 참고 attachments 필드·목록·링크 계약 없음 | scene/music 제작 자산 업로드는 구현돼 있으나 자유 brief 첨부와 구분 |
| 사용자 품질 목표 | **미구현**: 임의 quality_objectives 입력·보존·검사 매핑 없음 | 기존 서버 rubric/quality_report 표시는 구현. 목표를 평가했다고 표시하지 않음 |
| 자연어 수정 | **부분**: Stage3 자유 brief→스토리보드 생성/검토는 로컬 구현. 기존 계획·결함에 대한 자연어 수정 해석은 **미구현** | 구조화 scene/duration/director edit와 caption repair를 재사용. revision-intent API 신설하지 않음 |
| 범용 전체 복원 | **미구현** | 과거 caption 재적용만 지원. 새 restore API를 일괄 추가하지 않음 |
| 특정 장면 재생성 | **부분**: 기존 failed segment 재시도·변경 구간 재실행/성공 구간 보존 구현 | 임의 scene별 새 생성 전용 API는 없음. 기존 render/repair 지원 범위 우선 |
| 의미·시청·청감 검수 | binding 수용 경계 구현, 실제 독립 evaluator **미연결** | unknown 유지. 이번 청취·전체 시청 미실행. 무음 때문에 실패했던 B 보간 최종 QA도 보존 |
| Stage3 원격 executor·실제 UI 여정 | durable consumer/승인 revision 연결은 로컬 구현 | 새 domain API 운영 미배포·공유 저장소/원격 소비 미검증. UI 정본의 브라우저 검증은 fixture이며 실제 로그인/제작 성공 증거 아님 |

분류의 `운영 미배포`는 구현 공백과 다르다. 승인된 브라우저의 기존 인증 격리 기록을 host 전체 장애로 확대하지 않는다. 이번 보충에서 새 browser를 띄우거나 로그인·A/C 렌더 차단을 재시도하지 않았다. 프론트엔드 정본의 API 6개 확장안은 제안으로 남기고 구현 명령으로 해석하지 않았다.

## 5. 명령·회귀·보존

환경은 Codex worker sandbox, 로컬 worktree다. 공통 `PYTHONPATH=src:.:unit_tests UV_CACHE_DIR=/private/tmp/codex-uv-cache`.

```text
uv run pytest unit_tests/test_creative_v2_kafka_recipe_scope.py -q
RED 2 failed, 2 passed in 0.09s → GREEN 4 passed in 0.10s

uv run pytest unit_tests/test_creative_v2_kafka_recipe_scope.py unit_tests/test_creative_v2_matching.py unit_tests/test_creative_v2_registry.py unit_tests/test_creative_v2_stage3.py unit_tests/test_creative_v2_interpolation_bridge.py -q
74 passed in 3.10s
```

전용 반례는 lexical/구조화 기술 설명에 일반 내레이션이 있어도 R16/지도 능력을 유입하지 않는 2건, 명시적 다큐 catalog 2종은 R16·연구 선언·닫힌 gate를 유지하는 2건이다. 처음 다큐 반례에서 matcher의 기존 need 우선순위를 레시피 선언과 혼동한 테스트 assertion을 바로잡은 뒤, 실제 수정 대상만 RED 2건으로 확정했다. matcher 우선순위 결합 자체는 이번에 변경하지 않았다.

registry_data.py와 신규 test의 ruff check, 신규 test format check, git diff --check를 최종 실행했다. 기존 소스 30개 중 registry_data.py만 바뀌었고 29개는 착수 hash와 같다. Stage3 lifecycle/consumer·B 보간 adapter/planner/dispatch·A/C/quality·다른 담당 dirty 변경을 보존했다. 신규 test·이 보충 문서/작은 증거 외 범위 확장 없음.

실행하지 않음: 새 모델 호출, B/Stage2 재렌더, A/C 실행, UI 브라우저·청취, production plan 변경/승인, commit/push/배포. 미연결 기능을 위해 gate를 삭제하지 않았다. 총괄의 통합 배포 후 기존 result-link·caption 재적용, Stage3 운영 lifecycle/remote 소비, 실제 UI·독립 품질 검증 순서가 남는다.

증거는 현재 Run outbox `/Volumes/Backup/ttakkari-data/outbox/51118355-b99e-4530-add5-bb6272982f2d/ui-followup`에 있으며, 운영 응답·version/route 관측·배포 git blob 비교·전후 계획·이전 실추론 재검증 요약·소스 지문·자체 diff·검사 로그를 포함한다. 다른 Run outbox는 읽지 않았다.
