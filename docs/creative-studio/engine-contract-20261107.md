# Creative Studio v2 공통 엔진·QA·UI 통합 계약 (2026-11-07)

작성: Core 통합 담당 (`86c93b9b-7d99-4814-b83c-0dd77c453b2c`)
작업 경로: `/Users/winterholic/development/projects/winterholic-ttakkari/back-creative-production-20261107`

## 1. 엔진 어댑터 계약 (`src/ttakkari/creative/v2/engines.py`)

신규 엔진 모듈(`hyperframes.py`, `frame_animation.py`, `scene_3d.py`)은 아래 두 방식 중 하나(또는 둘 다)를 제공하면 `engines.py`가 자동 탐색·연결합니다.

### A. 모듈 함수 계약 (권장·최소 요구)
1. `preflight() -> Preflight | dict[str, Any]`
   - 반환: `Preflight(ok: bool, version: str | None, missing: list[str])` 또는 동등한 `dict` (`{"ok": True, "version": "...", "missing": []}`).
2. `render(spec: dict[str, Any], *, output_path: Path | str | None = None, workdir: Path | str | None = None) -> dict[str, Any]` (또는 `render_segment` / `render_scene`)
   - `spec` 입력 구조:
     ```json
     {
       "plan_id": "uuid",
       "revision": 1,
       "segment_id": "seg-01",
       "aspect": "16:9",
       "width": 1280,
       "height": 720,
       "fps": 30,
       "seconds": 15.0,
       "caption_mode": "narration",
       "style": {"animation": "pan", "transition": "cut", "caption_style": "bottom_card"},
       "style_dna": {...},
       "music_path": "/abs/path/to/music_slice.wav",
       "scenes": [
         {
           "scene_id": "s1",
           "title": "장면 제목",
           "narration": "내레이션 대본",
           "prompt": "시각 지시 프롬프트",
           "seconds": 5.0,
           "duration": 5.0,
           "start_time": 0.0,
           "end_time": 5.0,
           "start_frame": 0,
           "end_frame": 150,
           "caption": "자막 첫 줄",
           "caption_segments": [{"text": "...", "start": 0.0, "end": 4.2, "timing_basis": "estimated_voice_duration"}],
           "asset_path": "/abs/path/to/scene_asset.mp4_or_png",
           "asset_kind": "video",
           "voice_path": "/abs/path/to/voice.wav",
           "strategy": "code_motion | frame_animation_2d | scene_3d | generative_video | slideshow",
           "camera": ["C06.pan"],
           "motion": ["C07.ui_anim"],
           "shots": [...]
         }
       ]
     }
     ```
   - 반환 `dict` 구조:
     ```json
     {
       "output_path": "/abs/path/to/rendered.mp4",
       "sha256": "64-hex",
       "duration": 15.0,
       "fps": 30,
       "width": 1280,
       "height": 720,
       "engine": "hyperframes | frame_animation | scene_3d",
       "version": "엔진 버전 문자열",
       "real_execution": true,
       "metrics": {"elapsed_seconds": 3.2, "peak_rss_bytes": 123456},
       "receipt": {"commands": [...], "scenes": [...]}
     }
     ```

### B. `EngineAdapter` 클래스/인스턴스 노출 (선택)
- 모듈에 `ADAPTER` 인스턴스 또는 `HyperFramesAdapter` / `FrameAnimationAdapter` / `Scene3DAdapter` 클래스가 있으면 `engines.load_adapters()`가 직접 등록합니다.
- 어댑터 속성:
  - `id: str`
    - HyperFrames: `"skill.hyperframes_motion"` (호환: `"tool.remotion"`, `"skill.ttakkari-remotion-setup"`, `"skill.ttakkari-motion-rules"`, `"tool.svg_canvas"`)
    - 2D Frame Animation: `"skill.frame_animation_2d"` (호환: `"tool.svg_canvas"`, `"tool.artcraft"`, `"skill.t2i-flux2-klein"`, `"skill.edit1-flux2-klein"`, `"skill.edit2-flux2-klein"`)
    - 3D Scene: `"skill.studio-3d-cinematic"` (호환: `"tool.three"`, `"tool.blender"`, `"skill.studio-character-lipsync"`)
    - Generative Video / Hybrid: `"skill.comfy_video_renderer"` / `"skill.v1_ffmpeg_slideshow"`
  - `providers: frozenset[str]`
  - `limits: Limits`
  - `style_params: dict[str, tuple[str, ...]]`
  - `preflight() -> Preflight`
  - `render_segment(spec: dict[str, Any], *, output_path: Path, workdir: Path) -> dict[str, Any]`

## 2. QA 모듈 연동 계약 (`src/ttakkari/creative/v2/quality.py`)

`dispatch.py`와 `api/creative_v2.py`는 `quality.py`가 존재하면 아래 함수를 호출해 기계 QA(`engines.qa_media`)와 분리된 3단계 품질 평가·결함·부분 수정 계획을 `production.json`에 기록합니다.
- `build_rubric(plan: dict[str, Any]) -> dict[str, Any]` (장르별 사전 루브릭)
- `evaluate_production(plan: dict[str, Any], record: dict[str, Any], *, media_path: Path | None = None) -> dict[str, Any]`
  - `machine_qa`, `semantic_qa`, `viewing_qa`, `defects`, `repair_plan`, `overall_verdict` 포함.
- `plan_partial_repair(plan: dict[str, Any], record: dict[str, Any], defects: list[dict[str, Any]]) -> dict[str, Any]`
  - 결함 있는 `scene_id`/`shot_id`/`layer`만 무효화하고 나머지 성공 자산/구간 `run_key`는 보존.

## 3. UI 연동 추가 필드 (`plans.py`, `dispatch.py`, `api/creative_v2.py`)

- `plan["scenes"][i]` 확장 필드 (기존 필드 100% 유지):
  - `strategy`: 장면별 제작 전략 (`"code_motion"`, `"frame_animation_2d"`, `"scene_3d"`, `"generative_video"`, `"slideshow"`)
  - `strategy_rationale`: 선택 이유, 필수 능력, 실행 가능성, 비용/지연/품질 근거 및 대안(`alternatives`)
  - `selected_engine`: 장면 실행 엔진 어댑터 ID
  - `shots`: 장면 하위 Shot 목록 (`shot_id`, `scene_id`, `index`, `title`, `action`, `camera`, `motion`, `seconds`, `start_time`, `end_time`, `strategy`, `required_capabilities`, `selected_providers`, `fallback_alternatives`, `cost_latency_quality`)
- `production` 상태 응답 확장:
  - `assets["scenes"][scene_id]`에 `"kind": "video" | "image"`, `"source_video_file"`, `"preview_image_file"` 지원
  - `quality_report` / `rubric` / `repair_plan` 노출 (존재 시)
  - 엔진 실패 시 묵시적 슬라이드쇼 대체 없이 정확한 실패 원인과 구간 상태(`failed`) 유지
