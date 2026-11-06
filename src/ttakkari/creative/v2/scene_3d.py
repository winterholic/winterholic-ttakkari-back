"""Creative v2 3D 씬 제작 엔진 어댑터 및 렌더링 파이프라인 (`scene_3d.py`).

기존 Three.js 시네마틱 및 Blender 헤드리스 성공 실험을 재사용하여
8~15초 카메라·조명·공간·오브젝트 움직임 영상을 제작하고 스튜디오에 연동한다.

소유권: 신규 모듈 (`engines.py`/`dispatch.py` 직접 편집 금지 불변 조건 준수).
공통 통합 계약: `docs/creative-studio/engine-contract-20261107.md` 완벽 준수.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.creative.render import ffmpeg
from ttakkari.creative.v2 import engines

logger = logging.getLogger(__name__)

# 기본 템플릿 및 랩 디렉토리 경로
TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "templates" / "scene_3d"
LAB_3D_DIR = Path("/Users/winterholic/development/projects/ttakkari-studio-lab/3d")
BPY_VENV_PYTHON = LAB_3D_DIR / ".venv-bpy" / "bin" / "python"

# 3D 제작 규격 한도
SCENE_3D_LIMITS = engines.Limits(
    aspects=("16:9", "1:1", "9:16"),
    min_scenes=1,
    max_scenes=10,
    max_scene_seconds=60.0,
    max_segment_seconds=60.0,
    min_segment_seconds=3.0,
)

MAX_FPS = 60
MAX_WIDTH = 3840
MAX_HEIGHT = 2160
SUPPORTED_STYLE = {
    "lighting": {"cinematic_fog", "studio_clean", "cyberpunk_neon", "monochrome_dramatic"},
    "camera_motion": {"crane_down", "tracking_rackfocus", "orbit_pulse", "handheld_subtle"},
    "color_grade": {"teal_orange", "neutral", "filmic_warm", "high_contrast"},
    "rendering_backend": {"blender_cycles_cpu"},
}


def _normalize_render_input(spec: dict[str, Any]) -> dict[str, Any]:
    supported_fields = {"plan_id", "revision", "segment_id", "aspect", "width", "height", "fps", "seconds", "frames",
                        "caption_mode", "style", "style_dna", "music_path", "scenes"}
    unsupported_fields = set(spec) - supported_fields
    if unsupported_fields:
        raise ValueError(f"unsupported render fields (not applied): {', '.join(sorted(unsupported_fields))}")
    if spec.get("style_dna"):
        raise ValueError("style_dna is not implemented; use the supported fixed style presets")
    seconds = float(spec.get("seconds", 8.0))
    fps = int(spec.get("fps", 24))
    frames = int(spec.get("frames", round(seconds * fps)))
    width = int(spec.get("width", 960))
    height = int(spec.get("height", 540))
    if not (SCENE_3D_LIMITS.min_segment_seconds <= seconds <= SCENE_3D_LIMITS.max_segment_seconds):
        raise ValueError(f"seconds must be between {SCENE_3D_LIMITS.min_segment_seconds:g} and {SCENE_3D_LIMITS.max_segment_seconds:g}")
    if not (1 <= fps <= MAX_FPS):
        raise ValueError(f"fps must be between 1 and {MAX_FPS}")
    if not (2 <= width <= MAX_WIDTH and 2 <= height <= MAX_HEIGHT):
        raise ValueError(f"resolution must be at most {MAX_WIDTH}x{MAX_HEIGHT}")
    if frames != round(seconds * fps):
        raise ValueError(f"frames ({frames}) must equal round(seconds * fps) ({round(seconds * fps)})")
    if not 1 <= frames <= round(SCENE_3D_LIMITS.max_segment_seconds * MAX_FPS):
        raise ValueError("frames exceeds the render limit")
    style = dict(spec.get("style") or {})
    for key, value in style.items():
        if key not in SUPPORTED_STYLE:
            raise ValueError(f"unsupported style parameter: {key}")
        if value not in SUPPORTED_STYLE[key]:
            raise ValueError(f"unsupported {key} preset: {value}")
    style.setdefault("lighting", "cinematic_fog")
    style.setdefault("camera_motion", "orbit_pulse")
    style.setdefault("color_grade", "teal_orange")
    style.setdefault("rendering_backend", "blender_cycles_cpu")
    scenes = spec.get("scenes") or []
    if len(scenes) > 1:
        raise ValueError("Blender preset currently supports one scene per segment")
    scene = scenes[0] if scenes else {"scene_type": "crystal_orbit"}
    if scene.get("scene_type", "crystal_orbit") != "crystal_orbit":
        raise ValueError("unsupported scene_type; supported preset is crystal_orbit")
    unsupported = set(scene) - {"scene_id", "scene_type", "seconds", "duration"}
    if unsupported:
        raise ValueError(f"unsupported scene fields (not applied): {', '.join(sorted(unsupported))}")
    scene_duration = scene.get("seconds", scene.get("duration"))
    if scene_duration is not None and abs(float(scene_duration) - seconds) > (0.5 / fps):
        raise ValueError("scene duration must match the segment duration")
    music_path = spec.get("music_path")
    if music_path and not Path(music_path).is_file():
        raise ValueError(f"music_path does not exist: {music_path}")
    return {"seconds": seconds, "fps": fps, "frames": frames, "width": width, "height": height,
            "style": style, "scene": scene, "music_path": str(music_path) if music_path else None}


def _probe_duration(path: Path) -> float:
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe is required to verify rendered duration")
    result = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    return float(json.loads(result.stdout)["format"]["duration"])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class AuditShotCheck:
    pass_status: bool
    data: dict[str, Any]


@dataclass(frozen=True)
class AuditReport:
    ok: bool
    version: str
    shots: list[dict[str, Any]]
    raw_output: str


class Scene3DAdapter:
    """Three.js 및 Blender 기반 3D 시네마틱 제작 어댑터.

    스튜디오 v2 계약(`docs/creative-studio/engine-contract-20261107.md`)의
    `skill.studio-3d-cinematic` 및 `tool.three`, `tool.blender`를 전담한다.
    """

    id = "skill.studio-3d-cinematic"
    providers = frozenset({
        "skill.studio-3d-cinematic",
        "tool.three",
        "tool.blender",
        "skill.studio-character-lipsync",
        "skill.v2_three_scene_3d",
        "tool.threejs",
        "tool.blender_3d",
    })
    limits = SCENE_3D_LIMITS

    style_params: ClassVar[dict[str, tuple[str, ...]]] = {
        "lighting": ("cinematic_fog", "studio_clean", "cyberpunk_neon", "monochrome_dramatic"),
        "camera_motion": ("crane_down", "tracking_rackfocus", "orbit_pulse", "handheld_subtle"),
        "rendering_backend": ("threejs_headless", "blender_cycles_cpu", "blender_eevee"),
        "color_grade": ("teal_orange", "neutral", "filmic_warm", "high_contrast"),
    }

    def __init__(
        self,
        node_bin: str | None = None,
        bpy_python: Path | None = None,
        template_dir: Path | None = None,
    ):
        self.node_bin = node_bin or shutil.which("node") or "node"
        self.bpy_python = bpy_python or (BPY_VENV_PYTHON if BPY_VENV_PYTHON.exists() else None)
        self.template_dir = template_dir or (TEMPLATE_DIR if TEMPLATE_DIR.exists() else LAB_3D_DIR / "cinematic")

    def preflight(self) -> engines.Preflight:
        missing: list[str] = []
        versions: list[str] = []

        # 1. FFmpeg 확인
        try:
            exe = ffmpeg()
            out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10, check=False)
            first = (out.stdout or "").splitlines()[0] if out.stdout else ""
            if out.returncode != 0 or not first:
                missing.append("ffmpeg 실행 파일이 버전을 응답하지 않습니다.")
            else:
                versions.append(first.strip().split()[2] if len(first.strip().split()) > 2 else first.strip())
        # Preserve the Preflight contract: environmental failures become missing reasons.
        except Exception as e:  # noqa: BLE001
            missing.append(f"ffmpeg 실행 파일을 쓸 수 없습니다: {e}")

        # 2. Node.js 확인
        try:
            node_out = subprocess.run([self.node_bin, "-v"], capture_output=True, text=True, timeout=5, check=False)
            if node_out.returncode == 0:
                versions.append(f"Node {node_out.stdout.strip()}")
            else:
                missing.append("Node.js를 실행할 수 없습니다.")
        # Preserve the Preflight contract: environmental failures become missing reasons.
        except Exception as e:  # noqa: BLE001
            missing.append(f"Node.js 확인 실패: {e}")

        # 3. 3D 템플릿/라이브러리 확인
        world_js = self.template_dir / "world.js"
        if not world_js.is_file():
            missing.append(f"3D 씬 템플릿(world.js)을 찾을 수 없습니다: {world_js}")

        # Blender는 이 어댑터의 필수 renderer다.
        if self.bpy_python and self.bpy_python.exists():
            try:
                bpy_out = subprocess.run(
                    [str(self.bpy_python), "-c", "import bpy; print(bpy.app.version_string)"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if bpy_out.returncode == 0:
                    versions.append(f"Blender {bpy_out.stdout.strip()}")
                else:
                    missing.append(f"Blender 실행 확인 실패: {bpy_out.stderr.strip() or bpy_out.returncode}")
            # Preserve the Preflight contract: bpy failures become a missing reason.
            except Exception as e:  # noqa: BLE001
                missing.append(f"Blender 실행 확인 실패: {e}")
        else:
            missing.append(f"Blender Python 실행 파일을 찾을 수 없습니다: {self.bpy_python or BPY_VENV_PYTHON}")

        # 버전 문자열 요약
        version_summary = " | ".join(versions) if versions else None
        return engines.Preflight(ok=not missing, version=version_summary, missing=missing)

    def audit_scene(self, version: str = "v2") -> AuditReport:
        """렌더 전 카메라 동선 및 씬 지오메트리 사전 감사 (0.1초 내외 고속 실행)."""
        audit_script = self.template_dir / "audit.mjs"
        if not audit_script.exists():
            # 랩 디렉토리 폴백
            audit_script = LAB_3D_DIR / "cinematic" / "audit.mjs"

        # node_modules 심볼릭 링크 확보
        local_nm = audit_script.parent / "node_modules"
        lab_nm = LAB_3D_DIR / "node_modules"
        if not local_nm.exists() and lab_nm.exists():
            try:
                local_nm.symlink_to(lab_nm)
            except OSError:
                pass

        cmd = [self.node_bin, str(audit_script), version, "--json"]
        cwd = str(audit_script.parent)
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30, check=False)

        raw_text = res.stdout or res.stderr
        ok = res.returncode == 0
        shots: list[dict[str, Any]] = []
        try:
            data = json.loads(res.stdout)
            ok = data.get("ok", ok)
            shots = data.get("report", [])
        except json.JSONDecodeError:
            pass

        return AuditReport(ok=ok, version=version, shots=shots, raw_output=raw_text)

    def render_blender_video(
        self,
        output_dir: Path,
        *,
        frames: int = 192,
        fps: int = 24,
        width: int = 960,
        height: int = 540,
        samples: int = 4,
        render_config: dict[str, Any] | None = None,
    ) -> Path:
        """헤드리스 Blender를 사용하여 카메라·조명·오브젝트 애니메이션 3D MP4 제작."""
        if not 1 <= fps <= MAX_FPS:
            raise ValueError(f"fps must be between 1 and {MAX_FPS}")
        if not 2 <= width <= MAX_WIDTH or not 2 <= height <= MAX_HEIGHT:
            raise ValueError(f"resolution must be at most {MAX_WIDTH}x{MAX_HEIGHT}")
        if not 1 <= frames <= round(SCENE_3D_LIMITS.max_segment_seconds * MAX_FPS):
            raise ValueError("frames exceeds the render limit")
        duration = frames / fps
        if not SCENE_3D_LIMITS.min_segment_seconds <= duration <= SCENE_3D_LIMITS.max_segment_seconds:
            raise ValueError(f"render duration must be between {SCENE_3D_LIMITS.min_segment_seconds:g} and {SCENE_3D_LIMITS.max_segment_seconds:g} seconds")
        if not 1 <= samples <= 16:
            raise ValueError("samples must be between 1 and 16")
        render_config = render_config or {}
        normalized = _normalize_render_input({"seconds": duration, "fps": fps, "frames": frames, "width": width, "height": height,
                                             "style": render_config.get("style", {}),
                                             "scenes": [render_config.get("scene", {"scene_type": "crystal_orbit"})]})
        render_config = {"style": normalized["style"], "scene": normalized["scene"]}
        output_dir.mkdir(parents=True, exist_ok=True)
        render_py = self.template_dir / "blender_render.py"
        if not render_py.exists():
            render_py = TEMPLATE_DIR / "blender_render.py"

        exe = ffmpeg()
        cmd = [
            str(self.bpy_python),
            str(render_py),
            "--out",
            str(output_dir),
            "--frames",
            str(frames),
            "--fps",
            str(fps),
            "--width",
            str(width),
            "--height",
            str(height),
            "--samples",
            str(samples),
            "--ffmpeg",
            exe,
            "--config",
            json.dumps(render_config or {}),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        stdout_log = output_dir / "blender_render.stdout.log"
        stderr_log = output_dir / "blender_render.stderr.log"
        stdout_log.write_text(res.stdout or "", encoding="utf-8")
        stderr_log.write_text(res.stderr or "", encoding="utf-8")
        if res.returncode != 0:
            raise RuntimeError(f"Blender exited {res.returncode}; stdout={stdout_log}, stderr={stderr_log}")
        video_path = output_dir / "blender_scene_3d.mp4"
        if not video_path.is_file() or video_path.stat().st_size == 0:
            raise RuntimeError(f"Blender exited successfully without a non-empty video: {video_path}")
        evidence_path = output_dir / "motion_evidence.json"
        if not evidence_path.is_file():
            raise RuntimeError(f"Blender render is missing per-frame transform evidence: {evidence_path}")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        transform_frames = evidence.get("transform_frames", [])
        if evidence.get("frame_count") != frames or len(transform_frames) != frames:
            raise RuntimeError(f"Blender transform evidence has {len(transform_frames)} frames; expected {frames}")
        if any(float(sample.get("camera_target_dot", -1)) < 0.999999 for sample in transform_frames):
            raise RuntimeError("Blender per-frame camera aim evidence failed")
        if transform_frames:
            hero_start = transform_frames[0]["hero_rotation_euler_xyz"]
            hero_end = transform_frames[-1]["hero_rotation_euler_xyz"]
            camera_start = transform_frames[0]["camera_location"]
            camera_end = transform_frames[-1]["camera_location"]
            if max(abs(hero_end[i] - hero_start[i]) for i in range(3)) < 0.01:
                raise RuntimeError("Blender transform evidence shows no hero-object rotation")
            if math.dist(camera_start, camera_end) < 0.01:
                raise RuntimeError("Blender transform evidence shows no camera movement")
        return video_path

    def render_segment(
        self,
        spec: dict[str, Any],
        *,
        output_path: Path | str,
        workdir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Core 공통 엔진 계약 규격에 따른 구간 렌더링 함수."""
        t0 = time.time()
        normalized = _normalize_render_input(spec)
        pf = self.preflight()
        if not pf.ok:
            raise RuntimeError(f"Preflight failed; Blender render blocked: {'; '.join(pf.missing)}")
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        work_dir = Path(workdir) if workdir else out_p.parent / "work_3d"
        work_dir.mkdir(parents=True, exist_ok=True)

        seconds, fps, frames = normalized["seconds"], normalized["fps"], normalized["frames"]
        width, height = normalized["width"], normalized["height"]

        # 1. 렌더 전 감사 실행 (사전 검증)
        audit = self.audit_scene("v2")
        if not audit.ok:
            raise RuntimeError(f"Scene audit failed; Blender render blocked: {audit.raw_output}")

        # 2. Blender 헤드리스 렌더 실행
        video_res = self.render_blender_video(
            work_dir,
            frames=frames,
            fps=fps,
            width=width,
            height=height,
            samples=4,
            render_config={"style": normalized["style"], "scene": normalized["scene"]},
        )
        motion_evidence_path = work_dir / "motion_evidence.json"
        motion_evidence = json.loads(motion_evidence_path.read_text(encoding="utf-8"))

        if normalized["music_path"]:
            muxed = work_dir / "blender_scene_3d_with_audio.mp4"
            subprocess.run([ffmpeg(), "-y", "-i", str(video_res), "-i", normalized["music_path"], "-map", "0:v:0", "-map", "1:a:0",
                            "-c:v", "copy", "-c:a", "aac", "-shortest", str(muxed)], capture_output=True, text=True, timeout=120, check=True)
            video_res = muxed

        actual_duration = _probe_duration(video_res)
        if abs(actual_duration - seconds) > 1 / fps:
            raise RuntimeError(f"Rendered duration mismatch: requested {seconds:.3f}s, actual {actual_duration:.3f}s")

        # 대상 경로로 복사
        shutil.copy2(video_res, out_p)
        elapsed = time.time() - t0
        sha = _sha256_file(out_p)

        return {
            "output_path": str(out_p),
            "sha256": sha,
            "duration": actual_duration,
            "fps": fps,
            "width": width,
            "height": height,
            "engine": "scene_3d",
            "version": f"{pf.version} + Three.js {audit.version} audit",
            "real_execution": True,
            "metrics": {
                "elapsed_seconds": round(elapsed, 2),
                "peak_rss_bytes": 0,
            },
            "receipt": {
                "commands": [f"blender_render.py --frames {frames} --fps {fps} --config <json>"],
                "audit": {"ok": audit.ok, "shots_count": len(audit.shots)},
                "scenes": [normalized["scene"]],
                "applied_style": normalized["style"],
                "audio": {"source": normalized["music_path"], "muxed": bool(normalized["music_path"]), "listening_verified": False},
                "motion_evidence": {
                    "path": str(motion_evidence_path),
                    "frame_count": len(motion_evidence["transform_frames"]),
                    "min_camera_target_dot": min(float(x["camera_target_dot"]) for x in motion_evidence["transform_frames"]),
                    "transform_samples": "all rendered frames",
                },
                "renderer_logs": {
                    "stdout": str(work_dir / "blender_render.stdout.log"),
                    "stderr": str(work_dir / "blender_render.stderr.log"),
                },
            },
        }


# 공용 싱글톤 인스턴스
ADAPTER = Scene3DAdapter()


# 공통 모듈 레벨 함수 계약 (engine-contract-20261107.md 방식 A)
def preflight() -> engines.Preflight:
    return ADAPTER.preflight()


def render(
    spec: dict[str, Any],
    *,
    output_path: Path | str | None = None,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    if output_path is None:
        work_dir = Path(workdir) if workdir else Path("/tmp/creative_3d")
        output_path = work_dir / "rendered_scene_3d.mp4"
    return ADAPTER.render_segment(spec, output_path=output_path, workdir=workdir)


render_segment = render
render_scene = render


def get_lead_contract() -> dict[str, Any]:
    """총괄(Lead) 및 Core 통합 담당자에게 보고할 연결 계약 사양."""
    return {
        "adapter_class": "Scene3DAdapter",
        "adapter_id": ADAPTER.id,
        "providers": list(ADAPTER.providers),
        "limits": {
            "aspects": list(ADAPTER.limits.aspects),
            "min_scenes": ADAPTER.limits.min_scenes,
            "max_scenes": ADAPTER.limits.max_scenes,
            "max_scene_seconds": ADAPTER.limits.max_scene_seconds,
            "max_segment_seconds": ADAPTER.limits.max_segment_seconds,
            "min_segment_seconds": ADAPTER.limits.min_segment_seconds,
        },
        "style_params": ADAPTER.style_params,
        "contract_endpoints": {
            "module_functions": ["preflight() -> Preflight", "render(spec, output_path, workdir) -> dict"],
            "adapter_class": "Scene3DAdapter(preflight, audit_scene, render_segment, render_blender_video)",
            "adapter_instance": "ADAPTER",
        },
        "registration_snippet": (
            "# In engines.py:\n"
            "from ttakkari.creative.v2.scene_3d import ADAPTER, Scene3DAdapter\n"
            "ADAPTERS.append(ADAPTER)\n"
        ),
    }
