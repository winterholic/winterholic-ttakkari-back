# Creative Studio C 증거 색인 — 3D 씬 입력 안전 계약

기록일: 2026-11-07 KST <br>
범위: 기존 Blender 8초 결과와 Three.js 14초 결과의 보존, `scene_3d.py` 렌더 전 차단 및 입력 계약 보완. 기존 영상의 최초 제작은 반복하지 않았다.

## 품질 실패 후속 교정 — 2026-11-07

앞선 독립 보고의 Blender 영상 품질 판정은 실패로 유지한다. 기존 `bench/scene_3d_blender/blender_scene_3d.mp4`는 거의 검은색이며 정상 결과가 아니다. 앞선 계약 테스트·fixture 통과나 파일 디코드 성공을 시각 품질 통과로 해석하지 않는다.

### 관측된 원인

`blender_render.py` 카메라 계산은 목표 벡터에서 `pitch=atan2(dz, hypot(dx,dy))`, `yaw=atan2(dy,dx)`를 구한 뒤 기존에 `rotation=(π/2−pitch, 0, yaw+π/2)`를 설정했다. Blender 카메라 local `-Z`축을 XYZ Euler로 월드 변환해 첫 프레임 위치에서 계측하면:

```text
camera=(0, -7.5, 4.4), target=(0, 0, 1.8)
legacy_forward=(0, -0.944836, 0.327543)
target_direction=(0, 0.944836, -0.327543), dot=-1.000000
```

따라서 첫 프레임은 목표점을 정확히 등지고 있었다. 이 기하 오류는 중심 객체가 카메라 프레임에서 사라지고 어두운 월드만 렌더된 관측과 일치한다. 픽셀 원인을 Blender에서 새로 재현해 확인한 것은 아니므로, 실제 검은 출력에 대한 원인 관계는 기하 관측에 근거한 설명으로 한정한다.

### 수정과 검증 범위

- 카메라 회전을 `π/2 + pitch`, `yaw − π/2`로 교정했고, 프레임별 camera/target 내적이 0.999999 미만이면 Blender 씬 구성을 중단하도록 했다.
- 직접 `render_blender_video()` 호출도 fps 1–60, 3–60초, 최대 3840×2160, 프레임 총량과 sample 범위를 검사한다. audit 실패, Blender 필수 preflight, spec/실제 MP4 duration 확인은 기존 안전 계약에 유지한다.
- 수정 후 첫 프레임과 두 추가 카메라/목표 위치의 정렬 내적은 모두 `1.000000`이다. 회귀 테스트는 소스의 Euler 기하 함수를 검사한다. 이것은 Blender raster 이미지 검수가 아니다.
- 실제 실행이 가능한 다음 환경에서는 Blender가 각 렌더 프레임에서 camera 위치·Euler 회전·대상 위치·HeroCrystal 회전을 `motion_evidence.json`에 기록하고 stdout/stderr도 보존하도록 했다. `render_blender_video()`는 프레임 수, 목표 정렬, 카메라 이동, 주 객체 회전 증거가 없거나 실패하면 결과를 거부한다. 이번 실행에서 Blender에 들어가지 못했으므로 새 motion evidence 파일은 생성되지 않았다.

### 현재 실행 환경과 대체 경로 조사

- 기존 실패가 기록된 LAB Python은 `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/.venv-bpy/bin/python`이다. 이전 preflight 출력은 `bpy` import 단계의 USD `ArchWarn: ARCH_CACHE_LINE_SIZE != Arch_ObtainCacheLineSize(), assumptions.cpp:140`를 보고했다. 이 known failure를 다시 import하거나 렌더해 반복하지 않았다.
- 이번 Codex shell에서 `command -v blender`와 통상 설치 위치(`/Applications/Blender.app`, `/opt/homebrew/bin/blender`, `/usr/local/bin/blender`)를 읽기 전용으로 확인했으나 실행 파일이 없었다. capability snapshot(생성 2026-10-22)은 Blender와 Three.js 실행을 `lab_only`로 표시하며 현재 호스트 renderer 명령은 제공하지 않는다. Ttakkari host operation 도구도 고정된 작업 종류만 허용하고 임의 Blender 명령은 받지 않는다.
- Three.js 대체 재렌더는 로컬 Playwright/Chrome 실행을 요구한다. 지시된 browser preflight를 실행했지만 browser를 띄우기 전에 `status=blocked`, `browser_launched=false`; `mach-register`와 `com.apple.coreservices.launchservicesd`/`coreservicesd` 조회가 denied였다. 이에 Three.js 제작 실행은 중단했고 다른 binary/headless 옵션으로 우회하지 않았다. 이 결과는 Codex shell 환경만 가리킨다.
- 이 실행에서 새 Blender/Three.js MP4나 새 연속 프레임 파일은 만들지 않았다. 따라서 **수정영상 공유 ID: 없음, 수정영상 연속 프레임 ID: 없음**이다. 현재 소스에서 확인한 목표 정렬은 raster 품질을 대체하지 않으며 실제 8–15초 화면 검수는 미완료다.

### 검증 명령과 관측

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run pytest unit_tests/test_scene_3d.py -q
→ 21 passed in 0.26s
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python -m py_compile src/ttakkari/creative/v2/scene_3d.py templates/scene_3d/blender_render.py
→ exit 0
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python - <<'PY'
import runpy
r=runpy.run_path('templates/scene_3d/blender_render.py')
look=r['_look_at_euler']; dot=r['_target_alignment']
points=[((0,-7.5,4.4),(0,0,1.8)),((4,-5,3),(0,.5,1.7)),((-3,6,2),(.5,-.3,2.1))]
for camera,target in points: print(f"aim_dot={dot(camera,target,look(camera,target)):.6f}")
PY
→ aim_dot=1.000000 (3 positions)
```

테스트에는 앞선 fixture 반례에 대한 preflight/audit 차단, 실제 duration 불일치, 상한 입력 거부, 지원 장면·스타일 반영, 직접 renderer 과도한 입력, 카메라 프레이밍 수학 회귀를 포함한다. 테스트 fixture는 Blender raster 엔진을 대체하지 않는다.

### 기존 Three.js 재사용 자산

Three.js 14초 MP4는 기존 `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/out/cinematic_3d_v2.mp4`의 **재사용 사본**으로 이 Run에서 다시 공유했다(ID `d28262ac-3d79-4ac0-8f89-08df7722a8a7`, SHA-256 `230a7e8598e8b24caa5537b5c8b13e60900f20d5d23e9afc5a9ee7ad5eabb151`). 새 제작물이 아니며 Blender 카메라 수정 검증도 아니다. 기존 14초 AAC 오디오를 실제로 듣지 않아 청감은 미검증이다.

## 복원한 지시

`account-reassignment-20261107.json`에서 worker `b213f49b-8db7-4327-af8a-0629d8c208f5`의 완료 전 대기 지시 두 건을 확인했다.

- `18a9e92f-ba23-48ad-b11d-43ee10523fc0`: 구현·증거를 소유 범위에 보존하고 감사자 역할을 대신하지 말라는 인계 조건.
- `6ad545e0-c724-4eed-b57f-ec7b7ef4e87f`: Blender 누락·audit 실패 사전 차단, 입력 반영·제한, Blender 신규 제작과 Three.js 재사용을 나누어 기록하라는 교정 지시.

이미 있는 Blender MP4는 저장소의 `bench/scene_3d_blender/blender_scene_3d.mp4`와 프레임 시퀀스를 재사용했다. 새 Blender 렌더는 실행하지 않았다. 독립 검증 기록 `verification-c-20261107.md`가 이전 신규 렌더 시도에서 이 Codex 샌드박스의 Blender 자식 프로세스 SIGSEGV(11)를 기록했고, 기존 Blender 그림의 품질도 불합격으로 판정했다.

## 입력·소스·설정 색인

### Blender 기존 결과

- 입력/장면 소스: `templates/scene_3d/blender_render.py`의 procedural `crystal_orbit` (추가 모델/이미지 입력 없음).
- 렌더 어댑터: `src/ttakkari/creative/v2/scene_3d.py`, `Scene3DAdapter.render_blender_video()`.
- 보존된 결과: `bench/scene_3d_blender/blender_scene_3d.mp4`, SHA-256 `d24978f5aa125375a8268cc7ed802c6b52ce73e8c8de7ba2b501e7ae393eb87c`.
- 설정으로 기록된 값: Blender Cycles CPU, 192 frames, 24 fps, 960×540, 4 samples, 8.0 s.
- 재현 가능한 제작 호출은 `scripts/render_3d_benchmark.py`의 `Scene3DAdapter.render_blender_video(... frames=192, fps=24, width=960, height=540, samples=4)`에 있다. 이 스크립트는 이전 Run outbox 두 곳에도 복사하는 부수효과가 있어 다시 실행하지 않았다. 기존 결과에 대해 현재 실행기 stdout/stderr는 보존되어 있지 않으며, 독립 검증의 신규 렌더 실패 로그는 `verification-c-20261107.md` §2를 참조한다.
- 기존 출력 검사: `ffprobe -v error -show_entries format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate,nb_frames -of json bench/scene_3d_blender/blender_scene_3d.mp4` → H.264, 960×540, 8.000 s, 오디오 스트림 없음. 프레임 디렉터리 `bench/scene_3d_blender/frames/`에는 PNG 192장이 있다. 핵심 인덱스는 `frame_0001.png`, `frame_0048.png`, `frame_0096.png`, `frame_0144.png`, `frame_0192.png`; 연속 검사 인덱스는 `0001–0003`, `0095–0098`, `0188–0192`다. 독립 기록 §2의 인접 프레임 통계에서 `0001–0003`, `0095–0098`은 변화 0, `0188–0192`는 프레임당 약 4.0–4.8% 픽셀 변경·평균 RGB 차이 0.21–0.24였다. 주요 프레임의 고휘도 픽셀 비율은 거의 0이고 육안상 암부 단색에 가까워 장면 물체를 판별하기 어렵다.

### Three.js 기존 재사용 결과

- 입력/소스: `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/cinematic/world.js`, `audit.mjs`, 기록 `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/records/three-cinematic-v1-v2/record.json`.
- 장면 설정: 420 frames, 30 fps, 1920×1080, 14 s; 3 shots (크레인 0–5 s, low tracking/rack focus 5–9 s, orbit/pulse 9–14 s); 4,200 particles, fog/monolith/terrain. 기존 기록은 Three.js 0.185 및 FFmpeg libx264 CRF 15를 적는다.
- 비디오: `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/out/cinematic_3d_v2.mp4`, SHA-256 `230a7e8598e8b24caa5537b5c8b13e60900f20d5d23e9afc5a9ee7ad5eabb151`.
- 오디오 소스: `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/out/mix_a.wav`, SHA-256 `e69aea2ff7f58d67e06a2371ed03e6cd978b3c1f15edc9e43d114c4c01ac8d6f`; 기존 기록은 ACE-Step ambient cue와 impact의 믹스로 설명한다.
- `ffprobe` 검사: MP4 14.000 s, H.264 1920×1080 video + AAC audio; WAV 14.000 s PCM s16le. `ffmpeg -i mix_a.wav -af astats=metadata=1:reset=0 -f null -` 측정값: peak −2.49 dB, RMS −18.13 dB, 672,000 samples. 측정은 청취 검증이 아니다.
- 기존 Three.js 핵심 정지 프레임: `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/cinematic/out/stills_v2/f_00000.png`, `f_00140.png`, `f_00200.png`, `f_00300.png`, `f_00400.png`; 12 stills 전체 목록과 샷 타이밍은 같은 디렉터리 및 `record.json`에서 확인할 수 있다. 독립 검증 기록은 1·7·13초 화면을 확인했으나 청취하지 않았다.
- Three.js 비디오의 최초 제작 명령/stdout은 이 실행의 접근 가능한 증거에 포함되어 있지 않다. `record.json`의 렌더 기록은 v2 렌더 24.9 s로 기재하지만, 이는 당시 저장된 제작 기록이며 이 작업에서 재실행하지 않았다. 파일 decode는 이전 독립 기록에서 정상 종료(exit 0)로 보존되어 있다.

## 수정한 렌더 계약

- Blender Python을 필수 preflight 대상으로 바꿨다. 미설치·import/실행 실패면 `Preflight.ok=False`; `render_segment`가 그 이유를 포함해 Blender 호출 전 중단한다.
- Blender 호출 전에 scene audit `ok`를 검사한다. 실패 시 결과 영상 렌더 함수에 들어가지 않는다.
- 상한/일관성: duration 3–60 s, fps 1–60, 해상도 최대 3840×2160, `frames == round(seconds * fps)`, 장면 duration과 구간 duration 일치.
- 실제 적용 프리셋: `scene_type=crystal_orbit`; 조명(`cinematic_fog`, `studio_clean`, `cyberpunk_neon`, `monochrome_dramatic`), 카메라(`crane_down`, `tracking_rackfocus`, `orbit_pulse`, `handheld_subtle`), 색상(`teal_orange`, `neutral`, `filmic_warm`, `high_contrast`). 지원 값은 Blender 장면 조명/월드·카메라 궤적·크리스탈 재질에 전달한다.
- 한 segment당 crystal_orbit 장면 한 개와 고정 프리셋만 지원한다. 복수 장면, 임의 `prompt`/`title`/`narration`, 알 수 없는 top-level 필드, 미지원 스타일과 `style_dna`는 구체적인 오류로 거부한다. 입력을 조용히 생략하지 않는다.
- `music_path`가 있으면 FFmpeg에서 영상 stream copy + AAC mux를 수행한다. 결과 receipt는 `muxed` 여부와 `listening_verified: false`를 보존한다. 청취 품질 판정은 별도다.
- 새 출력은 `ffprobe` duration과 요청 duration을 비교하며 차이가 1 frame 초과면 실패한다. 응답의 `duration`은 측정된 MP4 길이다.

## 실행 증거 및 제한

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run pytest unit_tests/test_scene_3d.py -q
→ 15 passed in 0.81s
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python -m py_compile src/ttakkari/creative/v2/scene_3d.py templates/scene_3d/blender_render.py
→ exit 0
```

새 반례 테스트는 preflight/audit 실패 시 렌더 진입 금지, duration·frames·fps·resolution 경계, scene/style/style_dna 거부·보존, 렌더 후 실제 길이 불일치를 다룬다. Blender의 렌더 효과 자체는 이 환경에서 새로 검증하지 않았다. 독립 검증 기록의 실패 범위는 `verification-c-20261107.md` §2에 한정된다.

현재 Codex nested worker sandbox에서 별도 preflight 관측:

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python -c 'from ttakkari.creative.v2.scene_3d import preflight; p=preflight(); print({"ok":p.ok,"version":p.version,"missing":p.missing})'
→ ok=False, version='7.1 | Node v26.9.0'
→ Blender bpy import stage: ArchWarn: ARCH_CACHE_LINE_SIZE != Arch_ObtainCacheLineSize(), assumptions.cpp:140
```

이는 이 명령 환경에서 관측한 Blender 사전 점검 결과이며, 호스트 전체의 실행 가능성을 뜻하지 않는다. 이 결과를 렌더 허용 상태로 취급하지 않아 새 제작은 차단된다.

연결 계약을 core worker `86c93b9b-7d99-4814-b83c-0dd77c453b2c`에 `message_worker`로 전달하려 했으나 응답은 HTTP 404 `이 작업 세션의 하위 세션이 아닙니다.`였다. sibling worker 대상 메시지가 이 세션에 연결되지 않는다는 실행 증거다. 통합 담당자가 계약을 전달받았다고 간주하지 않는다. `engines.py`/`dispatch.py`는 이 작업에서 수정하지 않았다.

## 현재 Run 공유 파일

| 파일 | 의미 | 공유 파일 ID | SHA-256 |
|---|---|---|---|
| `blender_scene_3d_existing_8s.mp4` | 저장소에 있던 Blender 결과 사본, 새 렌더 아님 | `1476a691-7b53-436f-a8a4-649d8cf30b75` | `d24978f5aa125375a8268cc7ed802c6b52ce73e8c8de7ba2b501e7ae393eb87c` |
| `threejs_existing_14s_v2.mp4` | 14초 기존 Three.js 결과 사본, 재사용 | `240efe44-35d0-4431-8a93-f3ab9fb1a319` | `230a7e8598e8b24caa5537b5c8b13e60900f20d5d23e9afc5a9ee7ad5eabb151` |

새 8–15초 Blender 제작, 새 오디오 청취, 그리고 실제 Studio dispatch/queue는 이 범위에서 실행하지 않았다. 이 문서는 독립 검증 결과를 대신하지 않으며, 독립 검증자가 결과를 추가하기 전 판정을 통과로 추정하지 않는다.
