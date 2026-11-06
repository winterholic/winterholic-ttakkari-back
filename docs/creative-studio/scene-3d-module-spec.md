# 3D 씬 스튜디오 모듈 연동 명세 (`scene_3d.py`)

- **작성 모듈**: `src/ttakkari/creative/v2/scene_3d.py`
- **전용 템플릿**: `templates/scene_3d/` (`world.js`, `audit.mjs`, `blender_render.py`, `index.html`, `main.js`)
- **단위 테스트**: `unit_tests/test_scene_3d.py` (5건 통과)
- **통합 대상**: 엔진·API 통합 담당 (`86c93b9b-7d99-4814-b83c-0dd77c453b2c`), UI 담당 (`9f82d70e-5836-4e2b-9c94-f35f77f7b2d7`), 품질 담당 (`ece739f8-5b65-44e0-a977-abfbead42d74`)
- **준수 계약**: `docs/creative-studio/engine-contract-20261107.md` 규격 100% 충족

---

## 1. 모듈 연동 방법 (두 가지 방식 모두 지원)

### 방법 A: 모듈 레벨 함수 직접 호출 (추천)
```python
from ttakkari.creative.v2 import scene_3d

# 1. 사전 점검
preflight_result = scene_3d.preflight()
# 반환: Preflight(ok=True, version="7.1 | Node v26.9.0 | Blender 5.0.1", missing=[])

# 2. 렌더링 실행
spec = {
    "plan_id": "...",
    "seconds": 8.0,
    "fps": 24,
    "width": 960,
    "height": 540,
    "scenes": [...]
}
result = scene_3d.render(spec, output_path="/path/to/output.mp4")
# 반환:
# {
#   "output_path": "...",
#   "sha256": "...",
#   "duration": 8.0,
#   "fps": 24,
#   "width": 960,
#   "height": 540,
#   "engine": "scene_3d",
#   "version": "Blender 5.0.1 (Cycles CPU) + Three.js v2 audit",
#   "real_execution": True,
#   "metrics": {"elapsed_seconds": 10.95, "peak_rss_bytes": 0},
#   "receipt": {"commands": [...], "audit": {"ok": True, "shots_count": 3}, "scenes": [...]}
# }
```

### 방법 B: `engines.py`의 `ADAPTERS` 등록
```python
from ttakkari.creative.v2.scene_3d import ADAPTER, Scene3DAdapter
ADAPTERS.append(ADAPTER)
```
- **어댑터 id**: `skill.studio-3d-cinematic` (기존 `skill.v2_three_scene_3d` 호환)
- **제공자 (providers)**:
  `frozenset({"skill.studio-3d-cinematic", "tool.three", "tool.blender", "skill.studio-character-lipsync", "skill.v2_three_scene_3d", "tool.threejs", "tool.blender_3d"})`
- **규격 한도 (limits)**:
  - aspects: `("16:9", "1:1", "9:16")`
  - min_scenes: 1, max_scenes: 10
  - min_segment_seconds: 3.0, max_segment_seconds: 60.0
- **스타일 파라미터**:
  - `lighting`: `("cinematic_fog", "studio_clean", "cyberpunk_neon", "monochrome_dramatic")`
  - `camera_motion`: `("crane_down", "tracking_rackfocus", "orbit_pulse", "handheld_subtle")`
  - `rendering_backend`: `("threejs_headless", "blender_cycles_cpu", "blender_eevee")`
  - `color_grade`: `("teal_orange", "neutral", "filmic_warm", "high_contrast")`

---

## 2. 사전 감사 게이트 (Pre-render Audit Gate)
Three.js 기반 씬은 렌더 전에 Node.js로 0.11초 만에 다음 5가지 기하학적 안전성을 검사합니다:
1. `framing`: 타겟 주인공이 카메라 화각(NDC [-1, 1]) 내 안전 마진에 위치하는지
2. `occlusion`: 장애물/거석에 의한 시선 차단율이 버짓 이하인지
3. `clearance`: 카메라가 장애물 표면을 뚫고 들어가지 않는지
4. `floor`: 카메라가 지형 아래로 파고들지 않는지
5. `continuity`: 프레임 간 비정상적 위치 튐(jump)이나 FOV 급변이 없는지

---

## 3. 검증 산출물 위치
- Outbox: `/Volumes/Backup/ttakkari-data/outbox/34e65bca-bda8-4e82-959e-d74ce49a87b9/3d_scene_studio/`
  - `blender_3d_8s_motion.mp4` (8.0초 3D 애니메이션 MP4)
  - `threejs_cinematic_14s_v2.mp4` (14.0초 Three.js 시네마틱 3D MP4)
  - `mix_a_14s.wav` (14.0초 사운드트랙)
  - `stills_v2/`, `stills_blender/` (주요/연속 프레임 스틸)
  - `benchmark_summary.json` (벤치마크 요약)
- 모두 `share_file` 등록 완료.
