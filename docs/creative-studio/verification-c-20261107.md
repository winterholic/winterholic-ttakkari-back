# 최신 교정 후 독립 재검증 — Run 54895a81

> 이 최신 절은 C 카메라 기하와 안전 계약의 후속 독립 확인이다. 아래의 기존 검증 기록은 당시 관측을 보존하기 위해 본문 그대로 유지한다. 과거 반례·registry 관측은 당시 revision에 한정하고, 이번 결과는 실제 렌더 또는 전체 C 완료를 뜻하지 않는다.

검증 Run: `54895a81-5d2c-41d2-a78c-7588e08d1edf` <br>
기준 저장소 HEAD (시작·종료 동일): `439472b3bc4665edc04ad1d7fa8ad1a9684a47b4`

| 대상 | Run 시작·종료 SHA-256 |
|---|---|
| `src/ttakkari/creative/v2/scene_3d.py` | `b3787d2509d7dcf26e7b4745cb74802ba5ff31c93d96bd4a95ec3905644a9a85` |
| `templates/scene_3d/blender_render.py` | `09c0db3b8e1722c80b4b0bac073e90e4fd02246141131c26d08109c2ff4a139d` |
| `unit_tests/test_scene_3d.py` | `e466055668ab682c5a95da3cc1f0ea60ea2795e595ca172a3a31ce42f6e5e690` |

각 대상의 시작·종료 SHA는 같았다. 소스 구현·테스트 및 다른 담당자의 core 변경은 수정하지 않았다.

## 판정과 교정 범위

**부분** — 독립 local `-Z` 기하와 입력/실패 차단 fixture는 통과했다. 실제 raster 영상·청취·Creative Studio 실제 여정은 미검증으로 남고, 원래 C 요구 전체를 통과로 보지 않는다.

- 교정 전 문서화된 Euler를 독립 XYZ 행렬로 계산한 첫 위치 dot은 `-1.000000`; 교정 후 4개 camera motion × 시작/중간/끝 12개 표본은 기록 출력상 `min_dot=1.000000000000`이었다. 원 명령은 개별 12개 숫자를 출력하지 않고 개수와 최솟값만 출력했으므로 그 집계를 그대로 보존한다.
- 과거 반례 중 audit 실패는 renderer 진입 전 거부, bpy 경로 부재는 preflight 실패, 요청 8초 대 probe fixture 5초는 duration mismatch 거부로 확인했다. 직접 renderer의 fps·해상도·duration·frame 상한도 Blender subprocess 전에 거부했다.
- 미지원 scene/style은 구체적 오류로 거부했고, 지원 scene/style은 fixture에서 renderer `--config` JSON 인자로 전달됐다. motion evidence 누락과 metadata frame-count 불일치도 거부했다. 이 결과들은 모두 fixture 계약 확인이며 Blender raster 실행이 아니다.
- 계측 코드 첫 시도(seq21)는 local `-Z` 월드 벡터의 z 성분 부호를 반대로 둬 첫 dot 단언에서 `AssertionError`가 났다. 검사 수식의 오류였으며 제품 소스 결함 증거가 아니다. seq24/25 계산 후 seq26에서 해당 성분을 `-cos(y)cos(x)`로 바로잡았고 seq27은 `COUNTEREXAMPLES=PASS`를 기록했다. 수정된 스크립트 지문은 아래 artifact 지문과 일치한다.
- 이전 verification 원문의 Studio registry 미등록은 앞선 revision의 관측이다. 이번 검증은 현재 dispatch/registry를 확인하지 않았다. Core Stage3Run `7951dc2c` 및 UI/B 진행이 별도로 남아 있으므로 현재 core의 미등록/등록을 새로 단정하지 않는다.
- 실제 화면의 밝기·프레이밍·가시성·자연스러움, 새 8–15초 렌더, 오디오 청취, 실제 Studio dispatch/queue 통합은 미검증이다. 기존 Blender 결과는 이전 품질 실패 판정을 유지하고, 기존 Three.js 영상은 재사용 결과로 구분한다.

## Run 54895a81 재현 증거

주요 명령·관측은 Run 기록에서 가져왔으며 이 보존 작업에서는 재실행하지 않았다.

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python .../scene3d_c_independent_counterexamples.py
→ COUNTEREXAMPLES=PASS (fixtures only; no Blender process/render)
→ camera legacy first-frame dot=-1.000000 (back-facing)
→ camera corrected Euler/local -Z: 12 trajectory samples, min_dot=1.000000000000

UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run pytest unit_tests/test_scene_3d.py -q
→ 21 passed in 0.26s
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python -m py_compile src/ttakkari/creative/v2/scene_3d.py templates/scene_3d/blender_render.py
→ exit 0
git diff --check
→ 출력 없음, exit 0
```

Fixture 요약: audit 실패 거부; bpy 실행 파일 부재 시 import 없이 preflight 거부; 미지원 `city_flythrough`/`dolly_zoom` 거부; 지원 preset 전달; direct renderer fps 61, width 3841, 1 frame/24fps, 3601 frames/60fps 거부; motion evidence 누락 및 metadata 71 대 요청 72 거부; 요청 8초 대 가짜 probe 5초 거부. probe 값은 fixture 주입으로 확인했으며 새 MP4나 실제 ffprobe 측정이 아니다.

`test_adapter_integration_with_engines_dispatch`는 테스트 내부 `mock_adapter_for`를 사용하므로 실제 Studio 등록 증거로 인정하지 않는다. 기존 브라우저 IPC 차단 및 Blender `bpy` import/render 실패를 반복하거나 우회하지 않았다.

## 재현 스크립트 원본과 지문

이 새 Run에서 이전 Run outbox 경로를 열지 않았다. `read_session`으로 이전 conversation 결과와 artifact metadata를 확인했고, 스크립트 본문은 현재 대화의 이전 assistant 생성 기록에 남은 원문으로 복원했다. 복원 후 바이트 지문이 당시 공유 artifact와 일치한다.

- 보존 경로: `docs/creative-studio/evidence-c-independent-20261107/counterexamples.py`
- 원 공유 스크립트 ID: `605c37ca-6a0f-45c8-a36a-5895fc7aabbc`
- 원 공유 스크립트 SHA-256: `37c313adb78627e4e278e8dc3eb3f4421a3b1bc7ec3f94df749fcc713f0df7e1`
- 원 공유 독립 보고 ID: `bd6d63d4-c976-4f7a-b8f2-0fa3d04d27ce`
- 기존 검증 문서·스크립트는 fixture를 실행하지 않고 파일과 artifact metadata만 확인했다.

---

# Creative Studio C 독립 검증 — 3D 씬 어댑터

검증 시각: 2026-11-07 00:14~00:24 KST (Codex 실행 샌드박스) <br>
판정: **부분**. 단위 테스트와 계약 반례는 재현했지만, 이 실행 환경에서 Blender 프로세스가 SIGSEGV로 끝나 새 8~15초 렌더를 만들지 못했다. 저장소의 기존 Blender MP4는 8초로 디코드되지만 화면 품질이 불합격이다.

## 기준 상태와 입력 식별

- 저장소: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`
- 브랜치 / HEAD: `feat/creative-production-20261107` / `5f5cbd997addcb4ea084ab908790f8f6eec54122` (`2026-11-06T23:48:19+09:00`)
- 검증 시작 때 dirty 상태: 변경 추적 파일 0개, 미추적 경로 15개. `bench/scene_3d_blender/`, `benchmark_tests/test_hf_minimal/`, `docs/creative-studio/{engine-contract-20261107.md,scene-3d-module-spec.md}`, `outbox/benchmark_b_work/`, `scripts/{render_3d_benchmark.py,run_frame_animation_benchmark.py}`, `src/ttakkari/creative/v2/{frame_animation.py,hyperframes.py,scene_3d.py}`, `templates/`, `tests/test_scene_3d.py`, `unit_tests/{test_creative_v2_frame_animation.py,test_hyperframes.py,test_scene_3d.py}`.
- 검증 도중 마지막 `git status --short`에는 다른 작업의 변경 추적 파일 `src/ttakkari/creative/v2/{asset_generation.py,asset_generation_managed.py}`와 추가 미추적 파일(`quality.py`, `verification-b-20261107.md`, quality/frame-animation 자료 등)이 보였다. 실행 시각과 해시를 대조했고 아래 5개 입력은 시작/마지막 해시가 동일했다. 병행 작업이 만든 파일은 수정하지 않았다.

| 입력 | 검증 때 수정 시각 (KST) | SHA-256 |
|---|---|---|
| `src/ttakkari/creative/v2/scene_3d.py` | 00:14:21 | `4b387182307172f422213b19b63333ae01d61ac5c2c101fcf988bd2c32bf9bd3` |
| `templates/scene_3d/blender_render.py` | 00:07:21 | `492434d72ead94da4e598b2b541ee1ae761cf39dafb44129fdfb395dfb760abc` |
| `unit_tests/test_scene_3d.py` | 00:14:32 | `e77be7389898fdcf3ee21edada392975ba1705b4ca0b90c99434d6a9611cca90` |
| `scripts/render_3d_benchmark.py` | 00:09:20 | `980b5e050bdad7678bb9ad017f5a4414e84cf020a2156bfe2e602b04e9b51134` |
| `docs/creative-studio/engine-contract-20261107.md` | 00:07:24 | `a482d9faea1bf859b1d528f39a1cef15aeb56ca6bac66e628d7193372d0fd5c4` |

## 완료 기준별 판정

### 1. Revision, dirty 상태, 입력 해시, 기존 5개 테스트와 반례 — **통과**

실행:

```text
uv run pytest unit_tests/test_scene_3d.py -q
→ uv 캐시 초기화 실패: ~/.cache/uv/... Operation not permitted (os error 1)

UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run pytest unit_tests/test_scene_3d.py -q
→ .....                                                                    [100%]
→ 5 passed in 0.84s
```

두 번째 명령은 첫 시도와 다른, 쓰기 가능한 임시 캐시를 사용했다. 다섯 테스트 중 `test_adapter_integration_with_engines_dispatch`는 `engines.adapter_for`가 아니라 테스트 내부의 `mock_adapter_for`를 검사한다. 따라서 이 테스트 통과는 실제 등록·디스패치 증거가 아니다. 실패 주입, 장면/스타일, 길이 불일치 반례는 §3과 첨부 계측 스크립트에서 확인했다.

### 2. 저장소 MP4/프레임 검사와 독립 8~15초 렌더 — **부분**

기존 저장소 자산 검사:

```text
/Users/winterholic/bin/ffprobe -v error -show_entries format=duration,size:stream=codec_name,codec_type,width,height,r_frame_rate,nb_frames -of json bench/scene_3d_blender/blender_scene_3d.mp4
→ codec=h264, 960x540, duration=8.000000, video stream 1개
/Users/winterholic/bin/ffmpeg -v error -i bench/scene_3d_blender/blender_scene_3d.mp4 -f null -
→ bench_mp4_decode_exit=0; PNG 프레임 192개
```

샘플 프레임 0001→0002, 0002→0003, 0095→0096, 0096→0097, 0097→0098의 평균 절대 픽셀 차이는 모두 0이었다. 0188→0192도 프레임당 바뀐 픽셀은 약 4.0~4.8%, 평균 RGB 차이는 0.21~0.24였다. 프레임 0001/0048/0096/0144의 휘도 40 초과 픽셀은 0%; 0192도 0.0012%였다. 프레임을 직접 열어 확인한 결과 거의 단색의 어두운 바닥만 보이고 HeroCrystal/위성/조명 구성을 판별할 수 없었다. 따라서 8초 파일 존재와 디코드는 확인했으나 카메라·조명·오브젝트의 의미 있는 움직임과 시청 품질은 **실패**다.

새 렌더 재현 명령(출력은 이 Run outbox의 `scene3d-verification/direct-render-work/`로 지정):

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python - <<'PY'
from pathlib import Path
from ttakkari.creative.v2.scene_3d import render
out=Path('/Volumes/Backup/ttakkari-data/outbox/3ea30d78-b2f9-48e2-b2f8-5e895f8dae86/scene3d-verification/direct-render-attempt.mp4')
render({'seconds':8.0,'fps':24,'frames':192,'width':960,'height':540,'scenes':[{'scene_id':'audit-attempt'}]}, output_path=out, workdir=out.parent/'direct-render-work')
PY
→ CalledProcessError: blender_render.py --frames 192 --fps 24 --width 960 --height 540 ... died with <Signals.SIGSEGV: 11>
```

실패 범위는 이 Codex 명령 실행 환경에서 `/Users/winterholic/development/projects/ttakkari-studio-lab/3d/.venv-bpy/bin/python`이 Blender 스크립트를 실행하는 단계다. 별도 `import bpy`도 종료 코드 139와 USD `ArchWarn` 뒤에 프로세스가 종료됐다. 이는 이 실행 환경의 결과이며 호스트 전체 또는 다른 엔진의 실행 가능성을 뜻하지 않는다. 새 8~15초 MP4/연속 프레임은 만들지 못했다. 저장소의 기존 MP4 사본은 검토용으로만 공유했으며 새 렌더라고 부르지 않는다.

### 3. preflight/audit 차단, 반환 길이, 상한 및 장면/스타일 효과 — **부분**

재현:

```text
UV_CACHE_DIR=/private/tmp/uv-cache-creative-20261107 uv run python /Volumes/Backup/ttakkari-data/outbox/3ea30d78-b2f9-48e2-b2f8-5e895f8dae86/scene3d-verification/counterexamples.py
→ audit_fail_did_not_block=True
→ style_scene_renderer_calls_equal=True receipt_scenes_differ=True
→ duration_return_vs_mp4=12.0 2.0
→ accepted_frames_fps_resolution=1440 120 4096x2160
→ missing_bpy_preflight=True []
```

이 스크립트는 실제 Blender 함수를 2초짜리 ffmpeg fixture로 대체하고 adapter의 호출 인자를 기록하는 좁은 계측 반례다. `audit_scene`을 `ok=False`로 주입해도 Blender 렌더 함수가 호출되고 반환이 생성됐다. 서로 다른 `scenes`·`style` 입력 두 건의 Blender 인자가 같았고 `scenes`는 receipt만 달라졌다. 입력 12초에 대해 어댑터는 `duration=12`라 반환했지만 생성된 fixture MP4는 ffprobe상 2초였다. 1440 frames / 120 fps / 4096×2160도 어댑터가 renderer로 전달했다. 이는 프레임·fps·해상도 상한 검사가 이 진입점에서 수행되지 않음을 보인다. `bpy` 경로가 존재하지 않는 경우에도 `preflight()`가 `ok=True, missing=[]`로 반환했다.

따라서 이 좁은 반례에서는 audit 실패, 길이 일치, 상한, 장면/스타일 효과 기대가 실패했다. fixture는 실제 Blender 그림 효과를 검사하지 않았고, 이 Codex 환경에서 실제 raster화 효과는 Blender SIGSEGV로 **미검증**이다. `counterexamples.py`와 그 fixture 출력 파일은 명시적으로 계측용이며 실제 렌더 산출물이 아니다.

### 4. 새 Blender 무음과 Three.js 오디오 재사용 — **부분**

```text
ffprobe bench/scene_3d_blender/blender_scene_3d.mp4 → h264 video stream만 있음, 오디오 stream 없음
ffprobe .../cinematic_3d_v2.mp4 → h264 1920x1080 + aac audio, format duration 14.000000초
ffprobe .../mix_a.wav → pcm_s16le, duration 14.000000초
ffmpeg -i .../mix_a.wav -af volumedetect -f null - → mean_volume=-18.1 dB, max_volume=-2.5 dB
ffmpeg -v error -i .../cinematic_3d_v2.mp4 -f null - → reused_threejs_decode_exit=0
```

Blender 출력은 검사한 MP4 기준 무음이다. 별도 Three.js 14초 영상·믹스 파일은 기존 자산을 재사용한 것이며 신규 Blender 제작물이 아니다. 세 시점(1초/7초/13초) 화면을 확인했다. 오디오 파일에는 측정 가능한 레벨과 MP4 내 AAC stream이 있으나 실제 청취는 수행하지 않았다. 따라서 사람 귀로 들리는지, 음질이 적절한지는 **미검증**이다.

### 5. 등록, 모듈 직접 호출, 실제 Studio dispatch — **부분**

실제 import된 현재 작업트리에서:

```text
engines.ADAPTERS → ['skill.v1_ffmpeg_slideshow']
engines.adapter_for({'skill.studio-3d-cinematic'}) → None
dispatch.dispatch_blockers(3D required provider plan)
→ ['이 계획의 필수 제공자를 실행하는 엔진 어댑터가 없습니다: skill.studio-3d-cinematic']
```

`scene_3d`의 `ADAPTER`와 모듈 함수는 직접 import 가능하며, 앞서 `render()`를 호출했으나 실제 실행은 Blender SIGSEGV에서 끝났다. 등록은 현재 코드에서 확인되지 않았고 실제 dispatch/render queue를 호출하지 않았다(등록이 없어 dispatch blocker에서 중단됨). `engine-contract-20261107.md`의 통합 담당자는 별도이며, 이 누락은 해당 core `engines.py`/`dispatch.py` 통합 작업 대기로 분류한다. 모듈 직접 호출 가능성과 Studio 통합을 혼동하지 않는다.

### 6. 검증 문서 및 공유 파일 ID — **통과**

이 문서가 요청된 검증 문서다. 공유한 기존 자산과 증거는 다음과 같다.

| 파일 | 구분 | 공유 파일 ID |
|---|---|---|
| `repo-bench-blender-8s.mp4` | 저장소 기존 Blender 벤치 사본, 신규 렌더 아님 | `1a95f1dd-2c23-4615-9604-b4a9452926a3` |
| `blender-bench-frame-0096.png` | 기존 어두운 Blender 프레임 | `7fbe9373-7a14-495d-9448-36761b35ab50` |
| `reused-threejs-14s.mp4` | 기존 Three.js 재사용 영상 | `64ed994a-37ea-4437-a4f8-cd77912d6b72` |
| `threejs-07s.png` | 기존 Three.js 영상의 7초 시점 | `fffb58ef-fa27-49ad-a458-2ade013e1dea` |
| `reused-threejs-audio-14s.wav` | 기존 별도 오디오 파일 | `81aaa8eb-df86-4bf0-b0dd-711d91aa20a4` |
| `counterexamples.py` | Blender 본체를 대체하는 계약 반례 계측 스크립트 | `dd3c636b-13d3-4b11-bbba-3e50215a5c1b` |

## 미실행 / 미검증

- 새 Blender 8~15초 실제 CPU 렌더 및 새 연속 프레임: `bpy` 자식 프로세스가 Codex 실행 환경에서 SIGSEGV 11로 종료되어 미실행.
- 실제 Blender 씬 raster 기준으로 scenes/style 입력 효과와 프레임/fps/해상도 한도: 계측기에서 호출 전달 여부는 확인했으나 실제 엔진 결과는 미검증.
- Blender 직접 실행에서 실제 MP4 길이와 반환 duration 대조: 새 파일을 만들지 못함. fixture 반례의 12초/2초 불일치는 재현됨.
- 실제 Studio dispatch/큐 실행: 현재 registry에서 어댑터를 찾지 못해 미실행; blocker를 통해 미등록을 확인.
- 재사용 오디오의 실제 청취: 미실행. `volumedetect`는 수치 측정이며 가청성 판정이 아님.

소스 구현 파일은 수정하지 않았다. 이 Run outbox 외의 다른 Run outbox 원본은 열지 않았다.
