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


## 보강: 자막 모드 계약 검증 (2026-10-23 18:5x KST)

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
