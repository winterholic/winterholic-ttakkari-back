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
