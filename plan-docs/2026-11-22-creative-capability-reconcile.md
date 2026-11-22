# Creative Studio 능력 대조 (선언 / 설치 / 검증 / 차단)

측정일 2026-11-22, Mac Studio(arm64, 64GB, MPS). 신규 다운로드 없음. 운영 재배포 없음.
선언은 registry_data.py, 설치는 호스트 탐침과 디스크, 검증은 이번 세션에서 직접 실행한 1회 성공을 뜻한다.
시청·청취 품질 판정은 하지 않았다.

## 결과표

| 대상 | declared | installed | verified (이번 실행) | blocked / 사유 |
|---|---|---|---|---|
| 이미지 생성 (FLUX.2 klein 4B) | 예 | 예, 모델 3종 디스크 확인 | 아니오. 과거 lab 기록만(2026-10-21, 참조 편집 11.6초) | 관리형 프로필 없음. comfy_submit이 `wan22-5b-draft`만 받는다 |
| 이미지→영상 (Wan2.2 5B, 14B+LightX2V) | 예 | 예, lab 워크플로 `i2v-wan22-5b`·`i2v-wan22-14b-lightx2v` 등록 | 아니오. 실행 기록 0편 | 관리형 프로필 없음 |
| 텍스트→영상 (Wan2.2 TI2V 5B) | 예 | 예 | 예. comfy_submit `wan22-5b-draft` task 36ff9a9c…, 640x352 1.375초 24fps, 40.2초 | 프로필 한계 1.375초. 2초 이상은 관리형 경로로 불가 |
| 텍스트→영상 (LTX-2.3 22B, HunyuanVideo 1.5) | 예 | 모델 파일만 있음 | 아니오 | lab ComfyUI 워크플로 레지스트리에 워크플로 없음. 파일이 있어도 실행 경로가 없다 |
| 음악·효과음 (ACE-Step 1.5) | 예 | 예, turbo·base·LM 체크포인트 확인 | 과거 실행 파일을 오늘 재측정만 함 (20.000초 48kHz 스테레오 wav). 새 생성은 못 함 | 새 생성은 자원 lease 저장소가 보호 영역(`~/.ttakkari/data`)이라 이 세션에서 lease를 못 잡는다. 효과음 전용 경로는 없음 |
| TTS (Supertonic 3) | 예 | 예 | 아니오. 과거 기록만(three-character-talk) | 설정 로더가 `.env`를 읽을 수 있어 직접 호출을 하지 않음 |
| HyperFrames 0.9.77~0.9.80 | 예 | 예 | 과거 기록(Kafka 16초 1280x720). 이번에는 재렌더 안 함 | 지원 장면이 `kafka_flow`뿐이라 감성 영상에는 쓸 수 없음 |
| ffmpeg 편집·색보정·믹스 | 예 | 예 (imageio 정적 빌드) | 예. Wan 클립 크롭·curves·grain·페이드 + ACE-Step loudnorm mux → 1280x720 3.375초, h264+aac | 시스템 `~/bin/ffprobe`는 옵션을 무시하는 래퍼. 측정은 `ttakkari-studio-lab/remotion/bin/ffprobe` |

## 디스크에는 있으나 ComfyUI 에 등록되지 않은 모델

- 모델 파일은 모두 `comfyui/ComfyUI/models/` 아래에 이미 있어 `extra_model_paths`나 심볼릭 링크가 필요 없다.
- 부족한 것은 파일이 아니라 실행 경로다. 두 층으로 나뉜다.
  1. lab 워크플로가 있으나 관리형 프로필이 없음: FLUX klein t2i·edit, Qwen edit, Wan 5B/14B I2V, SeedVR2, FILM.
  2. lab 워크플로도 없음: LTX-2.3, HunyuanVideo 1.5. 템플릿 변환과 검증이 별도 작업이다.
- HF 캐시의 FLUX.2-klein-9B, ACE-Step 변형은 ComfyUI 형식이 아니라 등록 대상으로 보지 않았다.
- 관리형 프로필은 `src/ttakkari/managed_comfy.py`의 `PROFILES`에 코드로 들어 있고 comfy_submit은 그 목록만 받는다.
  프로필 추가는 배포가 있어야 반영된다. 이번 지시는 재배포 금지라 코드에 넣지 않았다. ComfyUI 설정은 바꾸지 않았다(되돌릴 변경 없음).

## 2초 이상 16:9 클립과 20초 이상 음악

| 항목 | 결과 | 근거 |
|---|---|---|
| 생성형 클립 2초 이상 | 오늘 새로 생성하지 못함. 같은 모델이 과거에 만든 파일은 있음 | 관리형 경로는 1.375초 고정. lab 출력 `comfyui/out/comfy-output/ttk/wan5b_00023_.mp4`는 832x480 24fps 81프레임 3.375초(진짜 ffprobe). 정확한 16:9는 아님(1.733) |
| 분위기 음악 20초 이상 | 오늘 새로 생성하지 못함. 과거 파일 재측정만 | `audio/out/acestep_ambient/c2b4d6c3-….wav` 20.000초. 프롬프트는 시네마틱 앰비언트, 보컬 없음 |
| 조립 | 성공 | 산출물 `ffmpeg_edit_check.mp4` 1280x720 24fps 81프레임 3.375초, 오디오 aac. sha256 0474044643ef…a3b0 |

## 콘텐츠 B 제작자를 위한 실행 경로 결론

1. 지금 배포된 관리형 경로만으로는 1.375초 640x352 클립 이상을 못 만든다. 감성 영상을 클립 이어붙이기로 만들 수는 있으나 해상도·길이가 부족하다.
2. 다음 작업이 해야 할 일, 우선순위 순서.
   - `PROFILES`에 `wan22-5b-standard`(workflow `t2v-wan22-5b` revision `a603a63d6f3a`, preset `standard`: 832x480, 81프레임)를 추가하고 배포한 뒤 comfy_submit으로 1회 실행. 프로필 필드(`preset`, `required_gb`)만 늘리면 되고 워크플로는 이미 등록돼 있다. (미실행: 배포 금지)
   - 이미지→영상은 `i2v-wan22-5b`용 프로필과 입력 이미지 경로(현재 프로필은 prompt·seed만 받음)가 필요하다.
   - 음악은 R21 승인 계획(`creative_v2_auto_assets_start`)으로 ACE-Step이 돈다. 계획 6f8ba665(비 오는 밤 60초)는 `render_gate=false`, 사유 "승인 이후 능력 판정이 바뀜"이라 사용자의 재승인이 필요하다. 이 세션은 승인을 대신하지 않았다.
3. LTX-2.3·HunyuanVideo는 워크플로 제작이 선행돼야 한다. 지금은 쓸 수 없는 엔진이다.

## 레지스트리 수정 (커밋 7d41dc33)

| 수정 | 이전 | 이후 |
|---|---|---|
| Blender 탐침 | 샌드박스 import bpy exit 139 때문에 `missing` | 비샌드박스 재탐침 5.0.1 `installed`. 기록이 있던 `render.blender_offline`·`render.3d_preset`이 `executed`로 표시됨 |
| `skill.t2v-wan22-5b` | "관리형 프로필이 없다"(실제로는 있음) | draft 프로필 한계 표시 |
| 관리형 실행 기록 | 없음 | `managed-comfy-wan22-5b-draft-20261122` |
| ffmpeg `audio.mix`·`edit.color_grade` | `installed`(실행 기록 없음) | `executed` (`ffmpeg-edit-grade-mix-20261122`) |
| LTX-2.3·HunyuanVideo | "프로필 없음"이라 워크플로가 있는 것처럼 읽힘 | "lab 워크플로 없음" 제약 |
| I2V 3종 | 프로필 없음만 표시 | 실행 기록 없음 제약 추가 |

주의: 스냅샷을 샌드박스 안에서 다시 만들면 bpy가 다시 `missing`으로 떨어진다. 테스트 `test_ffmpeg_edit_run_is_recorded_and_sandbox_bpy_crash_is_not_in_committed_snapshot`가 막는다.
