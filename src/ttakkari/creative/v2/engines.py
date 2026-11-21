"""제작 엔진 실행 계약과 실제 미디어 QA.

엔진 어댑터는 레지스트리의 제공자 항목(`skill.v1_ffmpeg_slideshow` 등)과 1:1로 묶인다.
계획의 필수 제공자가 모두 어떤 어댑터가 맡는 항목이고, 그 어댑터의 사전 점검(preflight)이 통과해야 디스패치한다.
실행 기록에는 어댑터 id·버전·입력/출력 식별자를 남긴다. 독립 제작자는 지연 탐색하며,
등록·사전 검사·지원 프리셋·실제 실행을 구분한다. 생성 모델 호출은 managed 경로에 남긴다.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Protocol

from ttakkari.agents.browser_preflight import inspect_permissions
from ttakkari.creative.render import caption_image, dimensions, ffmpeg

CAPTION_FONT = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")


@dataclass(frozen=True)
class Limits:
    """어댑터가 한 번의 실행(구간)에서 받는 규격."""

    aspects: tuple[str, ...]
    min_scenes: int
    max_scenes: int
    max_scene_seconds: float
    max_segment_seconds: float
    min_segment_seconds: float


@dataclass(frozen=True)
class Preflight:
    ok: bool
    version: str | None
    missing: list[str] = field(default_factory=list)


class EngineAdapter(Protocol):
    id: str
    providers: frozenset[str]
    limits: Limits
    # 스타일 DNA에서 이 엔진이 실제로 반영하는 매개변수 이름과 허용 값.
    style_params: dict[str, tuple[str, ...]]

    def preflight(self) -> Preflight: ...


class V1SlideshowAdapter:
    """스튜디오 v1 FFmpeg 슬라이드쇼 렌더러(`creative/render.py`)."""

    id = "skill.v1_ffmpeg_slideshow"
    providers = frozenset({"skill.v1_ffmpeg_slideshow", "tool.ffmpeg"})
    # creative/catalog.py FORMATS['music_video']와 service.Scene 한도.
    limits = Limits(
        aspects=("16:9", "1:1"),
        min_scenes=3,
        max_scenes=40,
        max_scene_seconds=60.0,
        max_segment_seconds=240.0,
        min_segment_seconds=3.0,
    )
    style_params: ClassVar[dict[str, tuple[str, ...]]] = {
        "animation": ("still", "zoom_in", "zoom_out", "pan", "pan_down", "pulse"),
        "transition": ("fade", "cut"),
        "caption_style": ("bottom_card", "center_highlight", "title_card", "code_overlay"),
    }

    def preflight(self) -> Preflight:
        missing: list[str] = []
        version: str | None = None
        try:
            exe = ffmpeg()
            out = subprocess.run(
                [exe, "-version"], capture_output=True, text=True, timeout=15, check=False
            )
            first = (out.stdout or "").splitlines()[0] if out.stdout else ""
            if out.returncode != 0 or not first:
                missing.append("ffmpeg 실행 파일이 버전을 응답하지 않습니다.")
            else:
                version = first.strip()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            missing.append(f"ffmpeg 실행 파일을 쓸 수 없습니다: {e}")
        if not CAPTION_FONT.is_file():
            missing.append(f"한국어 자막 글꼴이 없습니다: {CAPTION_FONT}")
        return Preflight(ok=not missing, version=version, missing=missing)


ADAPTERS: list[EngineAdapter] = [V1SlideshowAdapter()]
_FAILED_PREFLIGHTS: dict[str, Preflight] = {}


class GenerativeVideoAdapter(V1SlideshowAdapter):
    """Composite existing managed-generated videos using the native video decoder.

    Generation itself stays in ManagedAssetGenerator with its leases/approval.
    """

    id = "skill.comfy_video_renderer"
    providers = frozenset({"skill.comfy_video_renderer"})
    limits = Limits(("16:9", "1:1"), 1, 40, 60.0, 240.0, 1.0)


class ModuleAdapter:
    """Conservative core boundary around independently owned producer modules."""

    def __init__(self, module: str, adapter: EngineAdapter):
        self.module = module
        self.delegate = adapter
        self.id = adapter.id
        aliases = {"skill.hyperframes_motion"} if module == "hyperframes" else set()
        self.providers = adapter.providers | aliases
        self.limits = adapter.limits
        if module == "frame_animation":
            # AnimationProjectSpec.validate() is stricter than the advertised limits.
            self.limits = replace(adapter.limits, min_segment_seconds=8.0, max_segment_seconds=15.0)
        if module == "hyperframes":
            self.limits = replace(adapter.limits, aspects=("16:9",), min_scenes=3, max_scenes=3)
        self.style_params = adapter.style_params

    def preflight(self) -> Preflight:
        if not callable(getattr(self.delegate, "render_segment", None)):
            # Do not run npx doctor/install or attempt a browser to hide a missing contract.
            return Preflight(False, None, [f"{self.module}: common render_segment contract missing; browser render not attempted"])
        root = Path(__file__).resolve().parents[4]
        paths = [Path(__file__).with_name(f"{self.module}.py"), Path(__file__)]
        paths += sorted((root / "templates" / self.module).glob("*.py"))
        paths += sorted((root / "templates" / self.module).rglob("*.js"))
        paths += sorted((root / "templates" / self.module).rglob("*.html"))
        digest = hashlib.sha256(b"".join(p.read_bytes() for p in paths if p.is_file())).hexdigest()
        failure_key = f"{self.module}:{digest}"
        if failure_key in _FAILED_PREFLIGHTS:
            return _FAILED_PREFLIGHTS[failure_key]
        if self.module == "hyperframes":
            ipc = inspect_permissions()
            if ipc["status"] not in {"permissions_allowed", "not_applicable"}:
                pre = Preflight(False, None, [f"HyperFrames render_segment browser IPC {ipc['status']}; no browser launched"])
                _FAILED_PREFLIGHTS[failure_key] = pre
                return pre
        pre = self.delegate.preflight()
        missing = list(pre.missing)
        if self.module == "scene_3d" and "Blender " not in (pre.version or ""):
            missing.append("scene_3d: Blender import/version check failed or bpy runtime missing; no render attempted")
        result = Preflight(pre.ok and not missing, f"{pre.version or 'unknown'}; source={digest}", missing)
        if not result.ok and self.module in {"hyperframes", "scene_3d"}:
            _FAILED_PREFLIGHTS[failure_key] = result
        return result

    def validate_style_dna(self, style_dna: dict[str, Any] | None) -> dict[str, Any] | None:
        """Delegate pure producer validation without preflight or rendering."""
        validate = getattr(self.delegate, "validate_style_dna", None)
        return validate(style_dna) if callable(validate) else None

    def validate_spec(self, scenes: list[dict[str, Any]]) -> list[str]:
        reasons = []
        preset = {"frame_animation": "walk_cycle", "scene_3d": "crystal_orbit"}.get(self.module)
        # Single-shot preset is the only verified input surface in current producers.
        if preset and (len(scenes) != 1 or scenes[0].get("preset") != preset):
            reasons.append(f"{self.id}: preset={preset} and one scene required; custom scene/rig not supported")
        if self.module == "hyperframes" and (len(scenes) != 3 or any(s.get("preset") != "kafka_flow" for s in scenes)):
            reasons.append("HyperFrames: preset=kafka_flow and exactly three scenes required")
        for scene in scenes:
            if any(shot.get("required_capabilities") for shot in scene.get("shots", [])):
                reasons.append(f"{self.id}: arbitrary shot capabilities are not supported by fixed presets")
            if scene.get("camera") or scene.get("motion"):
                reasons.append(f"{self.id}: custom camera/motion is not consumed by the preset")
        return reasons

    def render_segment(self, spec: dict[str, Any], *, output_path: Path, workdir: Path) -> dict[str, Any]:
        reasons = self.validate_spec(spec["scenes"])
        if reasons:
            raise ValueError(" / ".join(reasons))
        self.validate_style_dna(spec.get("style_dna"))
        payload = spec
        if self.module == "scene_3d":
            # The producer now rejects all unconsumed editorial fields. Core
            # retains the full input.json; only the supported preset crosses.
            payload = {key: spec[key] for key in ("plan_id", "revision", "segment_id", "aspect", "width", "height", "fps", "seconds") if key in spec}
            payload["scenes"] = [{"scene_id": scene["scene_id"], "scene_type": "crystal_orbit", "seconds": scene["seconds"]} for scene in spec["scenes"]]
        if self.module == "hyperframes":
            # Current A consumes ComposeResult style_dna itself, but rejects
            # editorial shot/strategy metadata. Preserve the full core input in
            # dispatch input.json and project only consumed scene content here.
            payload = {key: spec[key] for key in ("plan_id", "revision", "segment_id", "aspect", "width", "height", "fps", "seconds", "style_dna", "style") if key in spec}
            payload["scenes"] = [{key: scene[key] for key in ("scene_id", "title", "narration", "seconds") if key in scene} for scene in spec["scenes"]]
        result = self.delegate.render_segment(payload, output_path=output_path, workdir=workdir)
        if result.get("real_execution") is not True or not output_path.is_file():
            raise RuntimeError(f"{self.id}: missing real execution/output")
        if self.module == "scene_3d":
            result.setdefault("receipt", {})["core_input_scope"] = {
                "applied": ["crystal_orbit", "seconds", "fps", "width", "height"],
                "editorial_only": ["title", "narration", "shots", "style_dna"],
            }
        return result


def load_adapters() -> list[EngineAdapter]:
    """Lazy loading avoids producer→engines import cycles; no binary execution here."""
    result = list(ADAPTERS)
    # An empty/custom ADAPTERS remains an explicit override for legacy callers/tests.
    if not any(type(a) is V1SlideshowAdapter for a in result):
        return result
    result.append(GenerativeVideoAdapter())
    for name in ("hyperframes", "frame_animation", "scene_3d"):
        try:
            module = importlib.import_module(f"ttakkari.creative.v2.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"ttakkari.creative.v2.{name}":
                raise
            continue
        result.append(ModuleAdapter(name, module.ADAPTER))
    return result


def adapter_by_id(engine_id: str) -> EngineAdapter | None:
    return next((a for a in load_adapters() if engine_id == a.id or engine_id in a.providers), None)


def adapter_for(providers: set[str]) -> EngineAdapter | None:
    """필수 제공자를 모두 맡는 어댑터. 하나라도 맡지 못하면 None."""
    for adapter in ADAPTERS:
        if providers and providers <= adapter.providers:
            return adapter
    for adapter in load_adapters():
        if providers and providers <= adapter.providers:
            return adapter
    return None


def compose_segment(
    source: Path, music: Path, output: Path, *, aspect: str, seconds: float,
    captions: list[dict[str, Any]], caption_style: str = "bottom_card", transition: str = "cut",
) -> dict[str, Any]:
    """Core single-scene compositor, independent of V1's three-scene format gate.

    Preserve decoded source video motion, normalize all producers to 24fps H.264
    for concat, and use only the approved master audio slice. No model invocation.
    """
    width, height = dimensions(aspect)
    inputs = (["-loop", "1", "-framerate", "24"] if source.suffix.lower() in {".png", ".jpg", ".webp"}
              else ["-stream_loop", "-1"])
    args = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file,pipe", "-format_whitelist", "image2,png_pipe,jpeg_pipe,webp_pipe,mov,matroska",
            *inputs, "-i", str(source), "-protocol_whitelist", "file,pipe", "-i", str(music)]
    filters = f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=24,format=yuv420p[v0]"
    current = "v0"
    for index, cue in enumerate(captions):
        path = output.parent / f"caption-{index}.png"
        caption_image(path, cue["text"], width, height, style=caption_style)
        args.extend(["-loop", "1", "-framerate", "24", "-i", str(path)])
        following = f"v{index+1}"
        filters += f";[{current}][{index+2}:v]overlay=0:0:enable='between(t,{cue['start']:.6f},{cue['end']:.6f})'[{following}]"
        current = following
    if transition == "fade":
        filters += f";[{current}]fade=t=in:d=0.25,fade=t=out:st={max(0, seconds-0.25):.6f}:d=0.25[out]"
    else:
        filters += f";[{current}]null[out]"
    args.extend(["-filter_complex_threads", "1", "-filter_complex", filters, "-map", "[out]", "-map", "1:a:0",
                 "-t", f"{seconds:.6f}", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", str(output)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode:
        raise RuntimeError(f"core compositor exit {result.returncode}: {result.stderr[-1000:]}")
    return {"commands": [args], "real_execution": True, "video_motion": "source video decoded; no still extraction"}


# --- 실제 미디어 QA ---------------------------------------------------------


def _run_ffmpeg(args: list[str], timeout: int = 300) -> str:
    result = subprocess.run(
        [ffmpeg(), "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stderr


def probe(path: Path) -> dict[str, Any]:
    text = _run_ffmpeg(["-i", str(path)], timeout=30)
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video = re.search(r"Video: [^\n]*?, (\d{2,5})x(\d{2,5})", text)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", text)
    return {
        "duration": (
            int(duration.group(1)) * 3600
            + int(duration.group(2)) * 60
            + float(duration.group(3))
            if duration
            else None
        ),
        "width": int(video.group(1)) if video else None,
        "height": int(video.group(2)) if video else None,
        "fps": float(fps.group(1)) if fps else None,
        "has_video": "Video:" in text,
        "has_audio": "Audio:" in text,
    }


def black_ratio(path: Path, duration: float) -> float:
    text = _run_ffmpeg(
        ["-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-"]
    )
    black = sum(float(m) for m in re.findall(r"black_duration:(\d+(?:\.\d+)?)", text))
    return black / duration if duration else 1.0


def volume(path: Path) -> dict[str, float | None]:
    text = _run_ffmpeg(["-i", str(path), "-af", "volumedetect", "-vn", "-f", "null", "-"])
    mean = re.search(r"mean_volume: (-?\d+(?:\.\d+)?) dB", text)
    peak = re.search(r"max_volume: (-?\d+(?:\.\d+)?) dB", text)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "max_db": float(peak.group(1)) if peak else None,
    }


DURATION_TOLERANCE_SECONDS = 0.35
SILENT_PEAK_DB = -60.0
MOSTLY_BLACK = 0.9


def qa_media(
    path: Path,
    *,
    aspect: str,
    planned_seconds: float,
    tolerance: float = DURATION_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """결과 미디어를 실제로 읽어 측정한다. 항목마다 측정값과 통과 여부를 남긴다."""
    info = probe(path)
    expected = dimensions(aspect)
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str, measured: Any) -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail, "measured": measured})

    add("video_track", info["has_video"], "영상 트랙", info["has_video"])
    add("audio_track", info["has_audio"], "오디오 트랙", info["has_audio"])
    resolution = (info["width"], info["height"])
    add(
        "resolution",
        resolution == expected,
        f"기대 {expected[0]}x{expected[1]}",
        f"{resolution[0]}x{resolution[1]}",
    )
    duration = info["duration"]
    add(
        "duration",
        duration is not None
        and abs(duration - planned_seconds) <= tolerance,
        f"계획 {planned_seconds:g}초 ±{tolerance:g}초",
        duration,
    )
    if info["has_video"] and duration:
        ratio = black_ratio(path, duration)
        add("not_black", ratio < MOSTLY_BLACK, f"검은 화면 비율 {MOSTLY_BLACK:.0%} 미만", round(ratio, 3))
    if info["has_audio"]:
        level = volume(path)
        add(
            "audible",
            level["max_db"] is not None and level["max_db"] > SILENT_PEAK_DB,
            f"오디오 최대 음량 {SILENT_PEAK_DB:g} dB 초과",
            level,
        )
    return {"passed": all(c["passed"] for c in checks), "checks": checks, "probe": info}
