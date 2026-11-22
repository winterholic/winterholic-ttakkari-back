# 감성 시네마틱 영상용 관리형 Comfy 프로필 (2026-11-22)

작성 시점에는 코드·정적 검증까지 끝났고 호스트 실측은 하지 않았다. 이어받기 때 실행한 호스트 측정은 아래 `2026-11-22 이어받기 결과`에 덧붙였다.
이 작업은 운영 재배포, 새 모델 다운로드, GPU lease 우회를 하지 않았다.

## 추가한 프로필 (`managed_comfy.PROFILES`)

| 프로필 | 엔진 / workflow (고정 revision) | 입력 | 허용 범위 | 기본값 |
|---|---|---|---|---|
| `flux2-klein-t2i-16x9` | Comfy / `t2i-flux2-klein` `4a0d0ecfab25` | prompt, seed, width·height | 1280x720, 1024x576, 768x432 | 1280x720, 4스텝 |
| `wan22-5b-i2v-16x9` | Comfy / `i2v-wan22-5b` `6c9413e014d5`, preset standard | prompt, seed, 시작 이미지, width·height, duration_seconds | 832x480, 1024x576, 640x352 / 1.04~3.375초(25~81프레임, 24fps) | 832x480, 3.04초(73프레임) |
| `wan22-5b-t2v-long-16x9` | Comfy / `t2v-wan22-5b` `a603a63d6f3a`, preset standard | prompt, seed, width·height, duration_seconds | 같은 해상도 / 2.04~5.04초(49~121프레임) | 832x480, 5.04초 |
| `acestep15-music` | ACE-Step 프로세스(`run_acestep_job.py`, turbo 8스텝, LM 끔) | prompt(캡션), seed, duration_seconds | 10~120초 | 30초 |

- 해상도는 Wan 5B VAE 패치 단위(32)의 배수만 허용한다. 정확한 16:9는 1024x576뿐이고 832x480은 1.733이다.
- 모든 프로필은 기존 `ResourceLeaseManager` lease를 그대로 쓴다. Comfy 프로필은 family `comfy`, ACE-Step은 family `gpu`(기존 ACE-Step 경로와 같음)라서 `force_release`는 ACE-Step lease를 거부하고 소비자 reconcile만 허용한다.
- 길이·해상도는 `ComfyProfile.resolve`가 prepare에서 검증한다. 목록·범위 밖이면 lease를 잡기 전에 ValueError다. 프레임 수는 4n+1로 맞춘다.
- 시작 이미지는 `image_task_id`(같은 소유자의 성공한 이미지 task, receipt sha256 일치)나 내부용 `image_path`(절대 경로 일반 파일, PNG/JPEG/WebP, 25MiB 이하, 심볼릭 링크 금지)만 받는다. HTTP·MCP는 `image_task_id`만 노출한다. 실행 직전에 sha256을 다시 확인하고, 다르면 런타임을 띄우기 전에 실패시킨다.
- `wan22-5b-draft`는 그대로다(고정 640x352, 1.375초). 입력 옵션을 주면 거부한다.
- 같은 idempotency_key로 size·길이·이미지가 달라지면 거부한다.

## 정적 검증 결과 (이 세션에서 실행)

- 세 Comfy 프로필 모두 lab 레지스트리 graph hash가 프로필 revision과 같고, 그래프의 `.safetensors` 이름 집합이 `profile.models`와 같으며 모든 파일이 `comfyui/ComfyUI/models/*/` 아래 디스크에 있다.
- `notes/object_info.json`(2026-10-21 덤프) 기준 missing_nodes·broken_links 없음. 덤프가 모델 다운로드 전이라 모델 목록 검사는 디스크 직접 확인으로 대신했다.
- width·height·length·fps가 `meta.params` 바인딩 노드에 들어가는 것을 허용 범위 전체에서 확인했다(`unit_tests/test_comfy_profile_workflows_on_host.py`).
- ACE-Step 모델 파일은 ComfyUI 형식이 아니고 ComfyUI 모델 폴더에도 없다. 그래서 ACE-Step 프로필은 Comfy 서버가 아니라 기존 `run_acestep_job.py`를 같은 lease 아래에서 실행한다.

## Creative Studio 연결

`creative/v2/asset_routing.py`가 장면 요구에서 단계 사다리를 고르고, `ManagedAssetGenerator`가 위에서부터 시도한다.

- 정지 키프레임 장면(모든 shot의 `output_contract.kind=image`, 또는 `gen.image`만 요구): `keyframe` 하나. 이미지 자산으로 등록하고 카메라 움직임은 렌더러가 입힌다.
- 움직이는 클립(`strategy=generative_video`, 영상 능력 요구, 또는 명시가 없을 때): 장면이 3.375초 이하면 `keyframe_i2v → t2v → t2v_draft → camera_motion`, 더 길면 `t2v → keyframe_i2v → t2v_draft → camera_motion`.
- 생성형 단계가 모두 막혔거나(`blocked`, 모델 파일 없음) 확정 실패(`failed`)·거부(`rejected`)일 때만 `camera_motion`(키프레임 + ffmpeg zoompan)으로 내려간다. 각 장면 자산 `meta.generation_route`에 시도한 단계·상태·사유가 남고, 내려간 경우 `meta.fallback_reason`과 `meta.motion_source=image_camera_motion`이 붙는다.
- `needs_reconcile`, `cancelled`, lease 대기 초과는 자동 폴백하지 않고 생성을 중단한다(런타임 상태가 불확실한 채 다음 작업을 제출하지 않기 위해).
- 음악은 `acestep15-music`으로 계획 전체 길이를 요청한다. 계획이 120초를 넘으면 blockers에 사유를 남긴다.
- `registry_data.py`: 새 프로필을 claim의 `managed`에 달고 "관리형 실측 기록 없음: 배포 후 검증 대상"을 표시한다. 실행 기록이 없으므로 수준은 `installed`에 머문다(기록이 올려 줄 때까지). 능력 행렬 문서를 재생성했다.
- 이미 배포된 컨트롤러에 새 프로필이 없는 동안에는 `prepare`가 ValueError("unknown registered profile")를 내고 생성기는 이를 `rejected`로 기록한 뒤 `t2v_draft`(검증된 프로필)로 내려간다. 배포 순서 때문에 생성이 깨지지는 않는다.

## 배포 후 검증 절차 (미실행)

`comfy_submit`에 새 선택 인자가 생긴다(width, height, duration_seconds, image_task_id). 순서대로 한 건씩 제출하고 `comfy_job`의 receipt와 `ffprobe`를 남긴다.

1. `flux2-klein-t2i-16x9`, prompt "rainy Japanese night alley, neon reflections on wet asphalt, cinematic", seed 2211 → 1280x720 PNG.
2. `wan22-5b-i2v-16x9`, `image_task_id`=1번 task, duration_seconds 3.0, prompt "slow camera push forward, rain falling, neon reflections shimmer" → 832x480, 73프레임, 약 3.04초.
3. `wan22-5b-t2v-long-16x9`, duration_seconds 5.0 → 832x480, 약 5.04초(121프레임은 호스트 미측정이라 OOM 가능성을 열어 둔다).
4. `acestep15-music`, duration_seconds 30, prompt "calm ambient cinematic piano, soft rain texture, no vocals" → 48kHz wav 30초 이상.
5. 4건의 시간·해상도·길이를 `execution_records.json`에 `managed-comfy-<profile>-<date>` 기록으로 추가해 능력 수준을 `executed`로 올린다.

기대 시간은 lab 벤치 기준 klein 5초 안팎, Wan 5B 832x480 81프레임 약 300초(i2v는 기록 없음)다. 1024x576은 같은 5B의 토큰 수가 1280x704·49프레임 실측(374초)과 비슷한 정도라는 외삽일 뿐 측정이 아니다.

## 알려진 한계와 후속

- 모든 Comfy 서버 기동은 작업마다 시작·정지한다. 장면당 키프레임+영상이면 서버 기동이 두 번이다.
- lab 워크플로의 `VAEDecodeTiled` 기본(tile 512, temporal 64)은 lab 벤치에서 tile 384/temporal 32가 훨씬 빨랐다(44초 대 186초, 5B 832x480 49프레임). 이번에는 레지스트리 revision을 바꾸지 않았다.
- `inspect_media`는 길이만 돌려준다. 기존 코드가 해상도·fps를 1280x720 기본값으로 채우던 것을 receipt의 컨테이너 실측값 우선으로 고쳤다.

## 2026-11-22 이어받기 결과

- `origin/main` 최신 `dd5939320b54b94353d9929502d8d532e8682dd8`을 작업 브랜치에 fast-forward한 뒤 이 작업 변경을 재적용했다. `registry_data.py` 충돌은 main의 Kafka 3~5장면 변경과 이 작업의 Wan 프로필 상태를 함께 보존했다. 기준 revision은 `dd593932`다.
- 운영 배포는 revision `874c0d97a95c2f70c33e5aa469417470a737bea5`다. 운영 `comfy_profiles`는 `wan22-5b-draft` 하나만 반환한다. 이번 코드의 FLUX, I2V, long T2V, ACE-Step 프로필은 운영 등록되지 않았다. `comfy_readiness`: backend_host, ready, mps, torch 2.14.1.
- 기존 하위 호환 프로필로 새 영상 1개 생성: task `7520660589ef5832806d96cf76c722ab`, adapter job `20261122-131630-c0a302`, profile `wan22-5b-draft`, workflow `t2v-wan22-5b` revision `a603a63d6f3a`, seed 2211. 결과 640x352, 24fps, 1.375초, 100,502 bytes, 관측 39.44초. receipt `graph_hash_consistent=true`, seed 일치, lease id `be1a11a0d779420e8d17225c6a65ae30` 획득이 기록됐다. Comfy MCP 응답에는 lease 해제 필드가 없어 해제는 직접 관측으로 단정하지 않는다.
- 음악 새 생성은 하지 않았다. Creative v2 자동 자산 서비스는 현재 R21만 지원하고 ACE-Step 경로를 광고하지만, 맞는 감성 플랜 `6f8ba665-5a87-4763-9b3e-ba7e9e3f276a`는 승인 revision 1과 다르게 현재 `render_gate=false`이며 사유는 승인 후 능력 판정 변경이다. 콘텐츠 B의 범위 밖이며, 검수용 Kafka 계획을 빌리거나 승인을 우회하지 않았다. `comfy_profiles`에도 운영 ACE-Step 프로필이 없다.
- 최종 관련 pytest 149개 통과. 명령은 `PYTHONPATH=src UV_CACHE_DIR=/private/tmp/comfy-profile-uv-cache uv run --locked --offline python -m pytest -q`와 아래 8개 관련 테스트 파일이다. 결과 `149 passed in 124.48s`. 첫 실행의 실패 두 건은 문서 redirect 누락과 console-script가 프로젝트 root를 import path에 넣지 않은 실행 방식 때문이었다. `test_committed_capability_matrix_doc_is_generated_from_current_code`는 리포터 출력을 문서 파일로 redirect한 뒤 통과했고, `test_real_frame_producer_is_called_through_studio_dispatch`는 `python -m pytest`로 재실행해 통과했다. 이때 `uv` 기본 캐시 경로가 권한 거부되어 허용된 `/private/tmp` 캐시로 실행했다.
- 보고 후 콘텐츠 B는 별도 실행으로 진행한다. 배포 승인 뒤 장면별 생성 경로를 운영에서 확인하고, 20~40초에 맞춰 4~8개 장면을 4~5초씩 설계한다(16:9는 Wan 허용 크기 중 1024x576). 매 장면 영상 생성 결과와 길이를 확인하고, R21 승인 게이트가 현재 레지스트리 상태와 맞는 신규/수정 플랜 및 음악 승인을 거친다. 반복 슬라이드쇼 대체는 하지 않는다.
