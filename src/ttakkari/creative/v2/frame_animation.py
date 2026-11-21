"""2D 프레임 애니메이션 및 레이어 리깅 스튜디오 렌더 모듈.

동일 캐릭터 자산의 분리 레이어(머리, 몸통, 팔, 다리, 표정 등)를 유지하며,
구조화된 키포즈, 타이밍, 스페이싱, 카메라 모션, 자막 레이어를 결합하여
실제 2D 동작이 있는 8~15초 애니메이션을 렌더링한다.

제작 FPS(예: 12fps)와 재생 FPS(예: 24fps)를 명확히 분리하고,
무리한 기계적 프레임 보간 없이 hold 방식의 스냅 있는 타이밍을 지원한다.

Creative Studio v2 공통 엔진 통합 계약(`docs/creative-studio/engine-contract-20261107.md`)을 준수한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from PIL import Image, ImageDraw, ImageFont

from ttakkari.creative.render import dimensions, ffmpeg
from ttakkari.creative.v2 import engines
from ttakkari.creative.v2.engines import Limits, Preflight

CAPTION_FONT_PATH = engines.CAPTION_FONT
MIN_ANIMATION_SECONDS = 8.0
MAX_ANIMATION_SECONDS = 15.0
DEFAULT_PRODUCTION_FPS = 12
DEFAULT_PLAYBACK_FPS = 24
MOTION_TELEPORT_THRESHOLD_PX = 120.0


def _lerp(start: float, end: float, progress: float) -> float:
    return start + (end - start) * progress


def _build_motion_eval(
    *, average_displacement: float, max_displacement: float, production_frames: int
) -> dict[str, Any]:
    """기계적 변위 휴리스틱을 시청 품질 판정과 분리해 기록한다."""
    return {
        "same_source_layer_assets_reused": True,
        "identity_comparison": "not_performed",
        "motion_continuity_score": None,
        "motion_continuity_rating": "not_evaluated",
        "average_displacement_per_prod_frame": round(average_displacement, 2),
        "max_displacement_per_prod_frame": round(max_displacement, 2),
        "total_production_frames_calculated": production_frames,
        "displacement_basis": "root_transform_heuristic",
        "teleport_threshold_px": MOTION_TELEPORT_THRESHOLD_PX,
        "teleport_glitch_detected": max_displacement >= MOTION_TELEPORT_THRESHOLD_PX,
        "evaluation_verdict": "NOT_RATED: 변위 통계와 원본 레이어 재사용 사실만 기록했으며 지각 품질 평가는 수행하지 않음",
    }

FRAME_ANIMATION_LIMITS = Limits(
    aspects=("16:9", "9:16", "1:1"),
    min_scenes=1,
    max_scenes=10,
    max_scene_seconds=30.0,
    max_segment_seconds=60.0,
    min_segment_seconds=3.0,
)


@dataclass
class LayerTransform:
    """레이어의 로컬 변환 상태 (위치, 회전, 크기, 가시성)."""

    pivot_x: float = 0.5  # 정규화된 피벗 (0.0 ~ 1.0)
    pivot_y: float = 0.5
    offset_x: float = 0.0  # 부모 기준 픽셀 오프셋
    offset_y: float = 0.0
    rotation_deg: float = 0.0  # 시계방향 각도
    scale_x: float = 1.0
    scale_y: float = 1.0
    alpha: float = 1.0
    z_order: int = 0
    variant: str | None = None  # 대체 레이어 변형 (예: 표정 교체)


@dataclass
class RiggedPose:
    """한 키포즈 시점에서의 모든 레이어 변환 상태."""

    pose_id: str
    parts: dict[str, LayerTransform] = field(default_factory=dict)
    expression: str = "normal"
    root_x: float = 0.0  # 전체 캐릭터 월드 X 좌표
    root_y: float = 0.0  # 전체 캐릭터 월드 Y 좌표
    root_scale: float = 1.0


@dataclass
class CameraKeyframe:
    """카메라 시점 (팬, 줌)."""

    time_sec: float
    center_x: float = 0.5  # 0.0~1.0 (0.5는 중앙)
    center_y: float = 0.5
    zoom: float = 1.0  # 1.0 = 표준, >1.0 = 줌인


@dataclass
class SubtitleCue:
    """자막 큐."""

    start_sec: float
    end_sec: float
    text: str
    style: str = "bottom_card"


@dataclass
class AnimationScene:
    """단일 장면 정의."""

    scene_id: str
    duration_seconds: float
    background_type: str = "color"
    background_value: str = "#2B2D42"
    keyframes: list[tuple[float, RiggedPose]] = field(default_factory=list)
    camera_keyframes: list[CameraKeyframe] = field(default_factory=list)
    subtitles: list[SubtitleCue] = field(default_factory=list)
    transition_in: str = "none"
    transition_out: str = "none"


@dataclass
class AnimationProjectSpec:
    """전체 애니메이션 프로젝트 안전 입력 계약."""

    project_id: str
    title: str
    aspect_ratio: str = "16:9"
    target_duration: float = 10.0
    production_fps: int = DEFAULT_PRODUCTION_FPS
    playback_fps: int = DEFAULT_PLAYBACK_FPS
    interpolation_mode: Literal["hold", "linear"] = "hold"
    interpolation_reason: str = (
        "2D 셀 애니메이션 고유의 스냅과 리듬감을 살리고, "
        "기계적 보간(Optical Flow 등) 시 발생하는 경계선 흐림 및 젤리 왜곡을 방지하기 위해 hold 선택"
    )
    scenes: list[AnimationScene] = field(default_factory=list)
    audio_bgm: str | None = None
    character_id: str = "ttakkari_mascot"
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """안전 입력 계약 유효성 검사."""
        errors: list[str] = []
        if not (MIN_ANIMATION_SECONDS - 0.1 <= self.target_duration <= MAX_ANIMATION_SECONDS + 0.1):
            errors.append(
                f"총 재생 시간은 {MIN_ANIMATION_SECONDS}~{MAX_ANIMATION_SECONDS}초 사이여야 합니다 (현재: {self.target_duration:.2f}초)."
            )
        if self.aspect_ratio not in FRAME_ANIMATION_LIMITS.aspects:
            errors.append(
                f"지원하지 않는 화면비입니다: {self.aspect_ratio}. 지원: {FRAME_ANIMATION_LIMITS.aspects}"
            )
        if not self.scenes:
            errors.append("최소 1개 이상의 장면(scene)이 필요합니다.")
        sum_scene_duration = sum(s.duration_seconds for s in self.scenes)
        if abs(sum_scene_duration - self.target_duration) > 0.3:
            errors.append(
                f"장면 지속 시간 합({sum_scene_duration:.2f}초)이 목표 재생 시간({self.target_duration:.2f}초)과 일치하지 않습니다."
            )
        if self.production_fps <= 0 or self.playback_fps <= 0:
            errors.append("FPS는 양수여야 합니다.")
        if self.playback_fps % self.production_fps != 0 and self.production_fps > self.playback_fps:
            errors.append("제작 FPS는 재생 FPS보다 클 수 없습니다.")
        return errors


class FrameAnimationAdapter:
    """스튜디오 v2 프레임 애니메이션 엔진 어댑터 (EngineAdapter 프로토콜 및 Core 통합 계약 구현)."""

    id = "skill.frame_animation_2d"
    providers = frozenset({
        "skill.frame_animation_2d",
        "tool.ffmpeg",
        "tool.pil_layer_animator",
        "tool.svg_canvas",
        "tool.artcraft",
        "skill.t2i-flux2-klein",
        "skill.edit1-flux2-klein",
        "skill.edit2-flux2-klein",
    })
    limits = FRAME_ANIMATION_LIMITS
    style_params: ClassVar[dict[str, tuple[str, ...]]] = {
        "animation": ("walk_and_wave", "jump_and_cheer", "talking_presentation", "idle_motion"),
        "transition": ("cut", "fade", "dissolve"),
        "caption_style": ("bottom_card", "subtitles", "none"),
        "camera_motion": ("pan", "zoom_in", "zoom_out", "static"),
    }

    def preflight(self) -> Preflight:
        """어댑터 환경 사전 점검."""
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
                version = first
        # Preserve the Preflight contract: environmental failures become a missing reason.
        except Exception as e:  # noqa: BLE001
            missing.append(f"ffmpeg 실행 파일을 쓸 수 없습니다: {e}")

        if not CAPTION_FONT_PATH.is_file():
            missing.append(f"자막 글꼴을 찾을 수 없습니다: {CAPTION_FONT_PATH}")

        try:
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        except ImportError:
            missing.append("Pillow 라이브러리가 설치되어 있지 않습니다.")

        return Preflight(ok=not missing, version=version, missing=missing)

    def render_segment(
        self,
        spec: dict[str, Any],
        *,
        output_path: Path | str,
        workdir: Path | str | None = None,
    ) -> dict[str, Any]:
        """Core 통합 계약에 따른 세그먼트/프로젝트 렌더링 실행."""
        interpolation_mode = spec.get("interpolation_mode", "hold")
        if interpolation_mode not in ("hold", "linear"):
            raise ValueError(
                f"interpolation_mode={interpolation_mode!r}: allowed values are hold, linear"
            )
        start_t = time.perf_counter()
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        work_dir = Path(workdir) if workdir else out_p.parent / f"work_{out_p.stem}"
        work_dir.mkdir(parents=True, exist_ok=True)

        # spec 파싱 및 프로젝트 규격 변환
        duration = float(spec.get("seconds") or spec.get("target_duration") or 10.0)
        aspect = str(spec.get("aspect") or spec.get("aspect_ratio") or "16:9")
        title = str(spec.get("title") or spec.get("segment_id") or "2D Frame Animation")

        # 장면 목록 구성
        raw_scenes = spec.get("scenes") or []
        parsed_scenes: list[AnimationScene] = []

        if raw_scenes:
            for idx, sc in enumerate(raw_scenes):
                sc_id = str(sc.get("scene_id") or f"scene_{idx + 1}")
                sc_dur = float(sc.get("seconds") or sc.get("duration") or (duration / len(raw_scenes)))
                captions: list[SubtitleCue] = []
                c_text = sc.get("caption") or sc.get("narration") or sc.get("title") or ""
                if c_text:
                    captions.append(SubtitleCue(start_sec=0.2, end_sec=max(0.5, sc_dur - 0.3), text=str(c_text)))

                # 모션 템플릿 배정
                from templates.frame_animation.presets import (
                    make_jump_and_cheer_poses,
                    make_walk_cycle_poses,
                    make_wave_poses,
                )
                if idx == 0:
                    kfs = make_walk_cycle_poses(0.0, sc_dur, steps=2, distance_x=200.0, base_x=-150.0)
                    cam = [CameraKeyframe(0.0, center_x=0.46, zoom=1.0), CameraKeyframe(sc_dur, center_x=0.50, zoom=1.05)]
                elif idx == 1:
                    kfs = make_wave_poses(0.0, sc_dur, waves=2, char_x=50.0)
                    cam = [CameraKeyframe(0.0, center_x=0.50, zoom=1.05), CameraKeyframe(sc_dur, center_x=0.52, zoom=1.15)]
                else:
                    kfs = make_jump_and_cheer_poses(0.0, sc_dur, char_x=50.0)
                    cam = [CameraKeyframe(0.0, center_x=0.52, zoom=1.15), CameraKeyframe(sc_dur, center_x=0.50, zoom=1.0)]

                parsed_scenes.append(AnimationScene(
                    scene_id=sc_id,
                    duration_seconds=sc_dur,
                    keyframes=kfs,
                    camera_keyframes=cam,
                    subtitles=captions,
                ))
        else:
            # 기본 3장면 구성 (걷기, 인사, 점프)
            from templates.frame_animation.presets import (
                make_jump_and_cheer_poses,
                make_walk_cycle_poses,
                make_wave_poses,
            )
            s1_d = duration * 0.4
            s2_d = duration * 0.3
            s3_d = duration - s1_d - s2_d
            parsed_scenes = [
                AnimationScene("scene_01_walk", s1_d, keyframes=make_walk_cycle_poses(0.0, s1_d)),
                AnimationScene("scene_02_wave", s2_d, keyframes=make_wave_poses(0.0, s2_d)),
                AnimationScene("scene_03_jump", s3_d, keyframes=make_jump_and_cheer_poses(0.0, s3_d)),
            ]

        # 프로젝트 스펙 빌드
        proj_spec = AnimationProjectSpec(
            project_id=str(spec.get("plan_id") or "frame_anim_project"),
            title=title,
            aspect_ratio=aspect,
            target_duration=duration,
            production_fps=int(spec.get("production_fps") or DEFAULT_PRODUCTION_FPS),
            playback_fps=int(spec.get("playback_fps") or DEFAULT_PLAYBACK_FPS),
            interpolation_mode=interpolation_mode,
            scenes=parsed_scenes,
        )
        if interpolation_mode == "linear":
            proj_spec.interpolation_reason = (
                "승인된 감독 계획의 명시적 linear 선택: 키포즈 수치 필드 선형 보간"
            )

        renderer = FrameAnimationRenderer()
        manifest = renderer.render_project(
            spec=proj_spec,
            output_dir=work_dir,
            output_mp4=out_p,
            save_keyframes=True,
            save_continuous_samples=True,
        )

        elapsed = time.perf_counter() - start_t

        # 파일 sha256 계산
        sha = hashlib.sha256()
        with out_p.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)

        w, h = dimensions(aspect)
        return {
            "output_path": str(out_p),
            "sha256": sha.hexdigest(),
            "duration": manifest["qa"]["probe"]["duration"],
            "fps": proj_spec.playback_fps,
            "width": w,
            "height": h,
            "engine": "frame_animation",
            "version": "2.0.0",
            "real_execution": True,
            "metrics": {"elapsed_seconds": round(elapsed, 3), "total_frames": manifest["total_frames_rendered"]},
            "receipt": {
                "project_id": proj_spec.project_id,
                "scenes": [s.scene_id for s in proj_spec.scenes],
                "keyframes_count": len(manifest["sampled_keyframes"]),
            },
            "qa": manifest["qa"],
            "motion_eval": manifest.get("motion_eval", {}),
        }


class CharacterLayerBuilder:
    """일관된 캐릭터 레이어(머리, 몸통, 사지, 표정)를 절차적으로 생성·보관하는 빌더.

    프레임마다 동일한 소스 레이어 이미지를 재사용한다. 실제 렌더의 시각적
    일관성이나 조인트 결합 상태는 별도 비교·검수 없이 보장하지 않는다.
    """

    COLOR_SKIN = (255, 224, 189)
    COLOR_BODY = (41, 128, 185)  # 따까리 시그니처 블루
    COLOR_LIMBS = (52, 152, 219)
    COLOR_OUTLINE = (30, 40, 55)
    COLOR_EYE = (20, 25, 35)
    COLOR_CHEEK = (255, 160, 160, 180)
    COLOR_ACCENT = (241, 196, 15)  # 노란색 별/뱃지

    def __init__(self, base_scale: float = 1.0):
        self.scale = base_scale

    def create_character_layers(self) -> dict[str, Image.Image]:
        """캐릭터를 구성하는 각 부품 레이어(RGBA) 생성."""
        layers: dict[str, Image.Image] = {}

        # 1. 몸통 (Torso) - 200x240
        w, h = int(200 * self.scale), int(240 * self.scale)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        bbox = [int(30 * self.scale), int(20 * self.scale), int(170 * self.scale), int(220 * self.scale)]
        d.rounded_rectangle(bbox, radius=int(40 * self.scale), fill=self.COLOR_BODY, outline=self.COLOR_OUTLINE, width=int(5 * self.scale))
        cx, cy = int(100 * self.scale), int(90 * self.scale)
        r = int(22 * self.scale)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=self.COLOR_ACCENT, outline=self.COLOR_OUTLINE, width=int(3 * self.scale))
        layers["torso"] = img

        # 2. 머리 (Head) - 220x200
        w, h = int(220 * self.scale), int(200 * self.scale)
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([int(15 * self.scale), int(70 * self.scale), int(55 * self.scale), int(110 * self.scale)], fill=self.COLOR_SKIN, outline=self.COLOR_OUTLINE, width=int(4 * self.scale))
        d.ellipse([int(165 * self.scale), int(70 * self.scale), int(205 * self.scale), int(110 * self.scale)], fill=self.COLOR_SKIN, outline=self.COLOR_OUTLINE, width=int(4 * self.scale))
        d.rounded_rectangle([int(30 * self.scale), int(20 * self.scale), int(190 * self.scale), int(180 * self.scale)], radius=int(60 * self.scale), fill=self.COLOR_SKIN, outline=self.COLOR_OUTLINE, width=int(5 * self.scale))
        d.chord([int(30 * self.scale), int(15 * self.scale), int(190 * self.scale), int(110 * self.scale)], start=180, end=360, fill=self.COLOR_BODY, outline=self.COLOR_OUTLINE, width=int(5 * self.scale))
        layers["head_base"] = img

        # 3. 표정 변형 레이어들 (Face Expressions) - 220x200
        img_normal = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img_normal)
        d.ellipse([int(75 * self.scale), int(85 * self.scale), int(90 * self.scale), int(105 * self.scale)], fill=self.COLOR_EYE)
        d.ellipse([int(130 * self.scale), int(85 * self.scale), int(145 * self.scale), int(105 * self.scale)], fill=self.COLOR_EYE)
        d.ellipse([int(60 * self.scale), int(105 * self.scale), int(80 * self.scale), int(120 * self.scale)], fill=self.COLOR_CHEEK)
        d.ellipse([int(140 * self.scale), int(105 * self.scale), int(160 * self.scale), int(120 * self.scale)], fill=self.COLOR_CHEEK)
        d.arc([int(95 * self.scale), int(100 * self.scale), int(125 * self.scale), int(125 * self.scale)], start=20, end=160, fill=self.COLOR_OUTLINE, width=int(4 * self.scale))
        layers["face_normal"] = img_normal

        img_smile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img_smile)
        d.arc([int(72 * self.scale), int(82 * self.scale), int(95 * self.scale), int(102 * self.scale)], start=190, end=350, fill=self.COLOR_OUTLINE, width=int(5 * self.scale))
        d.arc([int(125 * self.scale), int(82 * self.scale), int(148 * self.scale), int(102 * self.scale)], start=190, end=350, fill=self.COLOR_OUTLINE, width=int(5 * self.scale))
        d.ellipse([int(60 * self.scale), int(105 * self.scale), int(85 * self.scale), int(125 * self.scale)], fill=self.COLOR_CHEEK)
        d.ellipse([int(135 * self.scale), int(105 * self.scale), int(160 * self.scale), int(125 * self.scale)], fill=self.COLOR_CHEEK)
        d.chord([int(93 * self.scale), int(105 * self.scale), int(127 * self.scale), int(135 * self.scale)], start=0, end=180, fill=(231, 76, 60), outline=self.COLOR_OUTLINE, width=int(3 * self.scale))
        layers["face_smile"] = img_smile

        img_wink = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img_wink)
        d.arc([int(72 * self.scale), int(88 * self.scale), int(95 * self.scale), int(104 * self.scale)], start=190, end=350, fill=self.COLOR_OUTLINE, width=int(5 * self.scale))
        d.ellipse([int(130 * self.scale), int(85 * self.scale), int(145 * self.scale), int(105 * self.scale)], fill=self.COLOR_EYE)
        d.ellipse([int(60 * self.scale), int(105 * self.scale), int(80 * self.scale), int(120 * self.scale)], fill=self.COLOR_CHEEK)
        d.ellipse([int(140 * self.scale), int(105 * self.scale), int(160 * self.scale), int(120 * self.scale)], fill=self.COLOR_CHEEK)
        d.arc([int(95 * self.scale), int(105 * self.scale), int(125 * self.scale), int(128 * self.scale)], start=20, end=160, fill=self.COLOR_OUTLINE, width=int(4 * self.scale))
        layers["face_wink"] = img_wink

        # 4. 팔 (Arm) - 80x160 (상단 어깨 피벗)
        aw, ah = int(80 * self.scale), int(160 * self.scale)
        img_arm = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
        d = ImageDraw.Draw(img_arm)
        d.rounded_rectangle([int(20 * self.scale), int(15 * self.scale), int(60 * self.scale), int(130 * self.scale)], radius=int(18 * self.scale), fill=self.COLOR_LIMBS, outline=self.COLOR_OUTLINE, width=int(4 * self.scale))
        d.ellipse([int(16 * self.scale), int(115 * self.scale), int(64 * self.scale), int(155 * self.scale)], fill=self.COLOR_SKIN, outline=self.COLOR_OUTLINE, width=int(4 * self.scale))
        layers["arm_left"] = img_arm
        layers["arm_right"] = img_arm.transpose(Image.FLIP_LEFT_RIGHT)

        # 5. 다리 (Leg) - 70x150 (상단 골반 피벗)
        lw, lh = int(70 * self.scale), int(150 * self.scale)
        img_leg = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
        d = ImageDraw.Draw(img_leg)
        d.rounded_rectangle([int(15 * self.scale), int(10 * self.scale), int(55 * self.scale), int(125 * self.scale)], radius=int(16 * self.scale), fill=self.COLOR_BODY, outline=self.COLOR_OUTLINE, width=int(4 * self.scale))
        d.rounded_rectangle([int(10 * self.scale), int(110 * self.scale), int(65 * self.scale), int(145 * self.scale)], radius=int(12 * self.scale), fill=self.COLOR_OUTLINE)
        layers["leg_left"] = img_leg
        layers["leg_right"] = img_leg.transpose(Image.FLIP_LEFT_RIGHT)

        return layers

    def export_layers_to_disk(self, target_dir: Path) -> dict[str, str]:
        """분리된 캐릭터 레이어를 PNG 파일로 보존하고 파일 매핑 반환."""
        target_dir.mkdir(parents=True, exist_ok=True)
        layers = self.create_character_layers()
        mapping: dict[str, str] = {}
        for name, img in layers.items():
            fpath = target_dir / f"layer_{name}.png"
            img.save(fpath)
            mapping[name] = str(fpath)
        return mapping


def transform_and_paste(
    canvas: Image.Image,
    layer_img: Image.Image,
    center_pos: tuple[float, float],
    pivot_norm: tuple[float, float],
    rotation_deg: float,
    scale: tuple[float, float] = (1.0, 1.0),
    alpha: float = 1.0,
) -> None:
    """피벗 포인트를 기준으로 레이어를 회전/스케일하여 캔버스에 알파 블렌딩 합성."""
    w, h = layer_img.size
    sx, sy = scale
    if sx <= 0 or sy <= 0:
        return

    pv_x = w * pivot_norm[0]
    pv_y = h * pivot_norm[1]

    target_img = layer_img
    if alpha < 1.0:
        target_img = target_img.copy()
        r, g, b, a = target_img.split()
        a = a.point(lambda p: int(p * alpha))
        target_img = Image.merge("RGBA", (r, g, b, a))

    if sx != 1.0 or sy != 1.0:
        new_w = max(1, int(w * sx))
        new_h = max(1, int(h * sy))
        target_img = target_img.resize((new_w, new_h), resample=Image.Resampling.BILINEAR)
        pv_x *= sx
        pv_y *= sy
        w, h = new_w, new_h

    if abs(rotation_deg) > 0.01:
        rotated = target_img.rotate(-rotation_deg, resample=Image.Resampling.BICUBIC, expand=True)
        rot_rad = math.radians(rotation_deg)
        orig_cx = w / 2.0
        orig_cy = h / 2.0
        rel_pv_x = pv_x - orig_cx
        rel_pv_y = pv_y - orig_cy
        rot_pv_x = rel_pv_x * math.cos(rot_rad) - rel_pv_y * math.sin(rot_rad)
        rot_pv_y = rel_pv_x * math.sin(rot_rad) + rel_pv_y * math.cos(rot_rad)

        new_rw, new_rh = rotated.size
        new_cx = new_rw / 2.0
        new_cy = new_rh / 2.0
        actual_pv_x = new_cx + rot_pv_x
        actual_pv_y = new_cy + rot_pv_y

        paste_x = int(center_pos[0] - actual_pv_x)
        paste_y = int(center_pos[1] - actual_pv_y)
        canvas.alpha_composite(rotated, (paste_x, paste_y))
    else:
        paste_x = int(center_pos[0] - pv_x)
        paste_y = int(center_pos[1] - pv_y)
        canvas.alpha_composite(target_img, (paste_x, paste_y))


class FrameAnimationRenderer:
    """프레임 애니메이션 렌더러."""

    def __init__(self, character_builder: CharacterLayerBuilder | None = None, arm_pivot_offset_y: float = -24.0):
        self.builder = character_builder or CharacterLayerBuilder(base_scale=1.0)
        self.layers = self.builder.create_character_layers()
        self.arm_pivot_offset_y = arm_pivot_offset_y

    def render_character_pose(
        self,
        canvas: Image.Image,
        pose: RiggedPose,
        world_center: tuple[float, float],
        scale: float = 1.0,
    ) -> None:
        """지정된 키포즈에 따라 캐릭터 레이어들을 순서대로 조립/합성."""
        cx = world_center[0] + pose.root_x
        cy = world_center[1] + pose.root_y
        s = scale * pose.root_scale

        parts = pose.parts

        order = [
            ("arm_right", (0.5, 0.15), (-45 * s, self.arm_pivot_offset_y * s)),
            ("leg_right", (0.5, 0.15), (-25 * s, 60 * s)),
            ("leg_left", (0.5, 0.15), (25 * s, 60 * s)),
            ("torso", (0.5, 0.5), (0, 0)),
            ("head_base", (0.5, 0.75), (0, -85 * s)),
            ("face", (0.5, 0.75), (0, -85 * s)),
            ("arm_left", (0.5, 0.15), (45 * s, self.arm_pivot_offset_y * s)),
        ]

        for part_name, default_pivot, default_offset in order:
            transform = parts.get(part_name, LayerTransform())
            p_scale = (transform.scale_x * s, transform.scale_y * s)
            pivot = (transform.pivot_x if transform.pivot_x != 0.5 else default_pivot[0],
                     transform.pivot_y if transform.pivot_y != 0.5 else default_pivot[1])
            pos = (cx + default_offset[0] + transform.offset_x * s,
                   cy + default_offset[1] + transform.offset_y * s)

            if part_name == "face":
                expr = transform.variant or pose.expression
                face_layer_name = f"face_{expr}" if f"face_{expr}" in self.layers else "face_normal"
                layer_img = self.layers[face_layer_name]
            else:
                layer_img = self.layers.get(part_name)

            if layer_img:
                transform_and_paste(
                    canvas=canvas,
                    layer_img=layer_img,
                    center_pos=pos,
                    pivot_norm=pivot,
                    rotation_deg=transform.rotation_deg,
                    scale=p_scale,
                    alpha=transform.alpha,
                )

    def draw_background(self, w: int, h: int, scene: AnimationScene, cam: CameraKeyframe) -> Image.Image:
        """배경 생성 (그라데이션 및 원경 디테일, 카메라 패럴랙스 반영)."""
        bg = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        draw = ImageDraw.Draw(bg)

        c_top = (25, 35, 55)
        c_bottom = (45, 75, 105)
        for y in range(h):
            ratio = y / max(1, h - 1)
            r = int(c_top[0] * (1 - ratio) + c_bottom[0] * ratio)
            g = int(c_top[1] * (1 - ratio) + c_bottom[1] * ratio)
            b = int(c_top[2] * (1 - ratio) + c_bottom[2] * ratio)
            draw.line([(0, y), (w, y)], fill=(r, g, b, 255))

        horizon_y = int(h * 0.72)
        stage_c = (35, 50, 75, 255)
        draw.rectangle([(0, horizon_y), (w, h)], fill=stage_c)
        draw.line([(0, horizon_y), (w, horizon_y)], fill=(80, 130, 180, 255), width=3)

        cam_shift_x = int((cam.center_x - 0.5) * w * 0.2)
        for i in range(5):
            lx = int((w * (0.15 + i * 0.18)) - cam_shift_x)
            ly = int(horizon_y * 0.6)
            radius = int(50 * cam.zoom)
            draw.ellipse([lx - radius, ly - radius, lx + radius, ly + radius], fill=(255, 255, 255, 12))

        return bg

    def draw_subtitles(self, canvas: Image.Image, text: str, style: str) -> None:
        """자막 렌더링 (가독성 박스 및 한글 폰트 적용)."""
        if not text:
            return
        w, h = canvas.size
        try:
            font = ImageFont.truetype(str(CAPTION_FONT_PATH), size=max(20, int(h * 0.045)))
        except OSError:
            font = ImageFont.load_default()

        draw = ImageDraw.Draw(canvas)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        pad_x = 24
        pad_y = 12
        box_w = tw + pad_x * 2
        box_h = th + pad_y * 2

        bx = int((w - box_w) / 2)
        by = int(h * 0.84)

        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=10, fill=(15, 20, 30, 210))
        canvas.alpha_composite(overlay)

        tx = bx + pad_x
        ty = by + pad_y - bbox[1]
        draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))

    def evaluate_camera(self, keyframes: list[CameraKeyframe], time_sec: float) -> CameraKeyframe:
        """시간에 따른 카메라 상태 보간."""
        if not keyframes:
            return CameraKeyframe(time_sec=time_sec)
        if len(keyframes) == 1:
            return keyframes[0]

        if time_sec <= keyframes[0].time_sec:
            return keyframes[0]
        if time_sec >= keyframes[-1].time_sec:
            return keyframes[-1]

        for i in range(len(keyframes) - 1):
            k1 = keyframes[i]
            k2 = keyframes[i + 1]
            if k1.time_sec <= time_sec <= k2.time_sec:
                dt = k2.time_sec - k1.time_sec
                if dt <= 0:
                    return k1
                prog = (time_sec - k1.time_sec) / dt
                cx = k1.center_x + (k2.center_x - k1.center_x) * prog
                cy = k1.center_y + (k2.center_y - k1.center_y) * prog
                zoom = k1.zoom + (k2.zoom - k1.zoom) * prog
                return CameraKeyframe(time_sec=time_sec, center_x=cx, center_y=cy, zoom=zoom)

        return keyframes[-1]

    def evaluate_pose(
        self,
        keyframes: list[tuple[float, RiggedPose]],
        time_sec: float,
        mode: str = "hold",
    ) -> RiggedPose:
        """시간에 따른 키포즈 계산. linear는 수치 변환만 보간하고 표정 등은 직전 키포즈를 유지한다."""
        if not keyframes:
            return RiggedPose(pose_id="default")
        if len(keyframes) == 1:
            return keyframes[0][1]

        left_time, left_pose = keyframes[0]
        for right_time, right_pose in keyframes[1:]:
            if time_sec < right_time:
                if mode != "linear" or right_time <= left_time or time_sec <= left_time:
                    return left_pose
                progress = (time_sec - left_time) / (right_time - left_time)
                parts: dict[str, LayerTransform] = {}
                for name in left_pose.parts.keys() | right_pose.parts.keys():
                    left = left_pose.parts.get(name, LayerTransform())
                    right = right_pose.parts.get(name, left)
                    parts[name] = LayerTransform(
                        pivot_x=_lerp(left.pivot_x, right.pivot_x, progress),
                        pivot_y=_lerp(left.pivot_y, right.pivot_y, progress),
                        offset_x=_lerp(left.offset_x, right.offset_x, progress),
                        offset_y=_lerp(left.offset_y, right.offset_y, progress),
                        rotation_deg=_lerp(left.rotation_deg, right.rotation_deg, progress),
                        scale_x=_lerp(left.scale_x, right.scale_x, progress),
                        scale_y=_lerp(left.scale_y, right.scale_y, progress),
                        alpha=_lerp(left.alpha, right.alpha, progress),
                        z_order=left.z_order,
                        variant=left.variant,
                    )
                return RiggedPose(
                    pose_id=f"{left_pose.pose_id}->{right_pose.pose_id}",
                    parts=parts,
                    expression=left_pose.expression,
                    root_x=_lerp(left_pose.root_x, right_pose.root_x, progress),
                    root_y=_lerp(left_pose.root_y, right_pose.root_y, progress),
                    root_scale=_lerp(left_pose.root_scale, right_pose.root_scale, progress),
                )
            left_time, left_pose = right_time, right_pose
        return left_pose

    def render_project(
        self,
        spec: AnimationProjectSpec,
        output_dir: Path,
        output_mp4: Path,
        save_keyframes: bool = True,
        save_continuous_samples: bool = True,
    ) -> dict[str, Any]:
        """전체 애니메이션 프로젝트 렌더링 파이프라인."""
        errors = spec.validate()
        if errors:
            raise ValueError(f"애니메이션 규격 검증 실패: {'; '.join(errors)}")

        output_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = output_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        keyframe_dir = output_dir / "keyframes_saved"
        if save_keyframes:
            keyframe_dir.mkdir(parents=True, exist_ok=True)
        continuous_dir = output_dir / "continuous_samples"
        if save_continuous_samples:
            continuous_dir.mkdir(parents=True, exist_ok=True)

        w, h = dimensions(spec.aspect_ratio)
        prod_fps = spec.production_fps
        play_fps = spec.playback_fps
        total_duration = spec.target_duration
        total_play_frames = round(total_duration * play_fps)

        scene_ranges: list[tuple[float, float, AnimationScene]] = []
        cur_time = 0.0
        for sc in spec.scenes:
            end_t = cur_time + sc.duration_seconds
            scene_ranges.append((cur_time, end_t, sc))
            cur_time = end_t

        frame_paths: list[Path] = []
        sampled_keyframe_records: list[dict[str, Any]] = []
        continuous_sample_records: list[dict[str, Any]] = []

        last_rendered_canvas: Image.Image | None = None
        last_prod_frame_idx = -1
        prev_char_pos: tuple[float, float] | None = None
        motion_displacements: list[float] = []

        for f_idx in range(total_play_frames):
            t = f_idx / play_fps
            prod_frame_idx = math.floor(t * prod_fps)

            active_scene = scene_ranges[-1][2]
            for st, et, sc in scene_ranges:
                if st <= t < et:
                    active_scene = sc
                    break

            scene_local_t = t - next(st for st, et, sc in scene_ranges if sc == active_scene)

            if spec.interpolation_mode == "linear" or prod_frame_idx != last_prod_frame_idx or last_rendered_canvas is None:
                cam = self.evaluate_camera(active_scene.camera_keyframes, scene_local_t)
                pose = self.evaluate_pose(active_scene.keyframes, scene_local_t, mode=spec.interpolation_mode)

                canvas = self.draw_background(w, h, active_scene, cam)

                base_char_pos = (w * 0.5, h * 0.65)
                char_x = base_char_pos[0] - (cam.center_x - 0.5) * w * cam.zoom
                char_y = base_char_pos[1] - (cam.center_y - 0.5) * h * cam.zoom
                self.render_character_pose(
                    canvas=canvas,
                    pose=pose,
                    world_center=(char_x, char_y),
                    scale=1.0 * cam.zoom,
                )

                current_subtitle = ""
                for cue in active_scene.subtitles:
                    if cue.start_sec <= scene_local_t <= cue.end_sec:
                        current_subtitle = cue.text
                        break
                if current_subtitle:
                    self.draw_subtitles(canvas, current_subtitle, "bottom_card")

                last_rendered_canvas = canvas
                last_prod_frame_idx = prod_frame_idx

                cur_pos = (char_x + pose.root_x, char_y + pose.root_y)
                if prev_char_pos is not None:
                    disp = math.hypot(cur_pos[0] - prev_char_pos[0], cur_pos[1] - prev_char_pos[1])
                    motion_displacements.append(disp)
                prev_char_pos = cur_pos

                if save_keyframes and (f_idx % int(play_fps / 2) == 0 or f_idx == total_play_frames - 1):
                    kf_filename = f"keyframe_{f_idx:05d}_t{t:.2f}s.png"
                    kf_path = keyframe_dir / kf_filename
                    canvas.save(kf_path)
                    sampled_keyframe_records.append({
                        "frame_index": f_idx,
                        "time_sec": round(t, 3),
                        "scene_id": active_scene.scene_id,
                        "pose_id": pose.pose_id,
                        "expression": pose.expression,
                        "file": kf_filename,
                    })

                if save_continuous_samples and (1.0 <= t <= 1.6):
                    cf_filename = f"cont_frame_{f_idx:05d}_t{t:.3f}s.png"
                    cf_path = continuous_dir / cf_filename
                    canvas.save(cf_path)
                    continuous_sample_records.append({
                        "frame_index": f_idx,
                        "time_sec": round(t, 3),
                        "file": cf_filename,
                    })

            frame_filename = f"frame_{f_idx:05d}.png"
            frame_path = frames_dir / frame_filename
            last_rendered_canvas.save(frame_path)
            frame_paths.append(frame_path)

        audio_path = output_dir / "soundtrack.wav"
        self._generate_soundtrack(audio_path, duration_seconds=total_duration)

        exe = ffmpeg()
        cmd = [
            exe,
            "-y",
            "-hide_banner",
            "-loglevel", "error",
            "-framerate", str(play_fps),
            "-i", str(frames_dir / "frame_%05d.png"),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output_mp4),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg 렌더 실패: {res.stderr}")

        qa_result = engines.qa_media(
            output_mp4,
            aspect=spec.aspect_ratio,
            planned_seconds=total_duration,
            tolerance=0.4,
        )

        avg_disp = sum(motion_displacements) / max(1, len(motion_displacements))
        max_disp = max(motion_displacements) if motion_displacements else 0.0
        motion_eval = _build_motion_eval(
            average_displacement=avg_disp,
            max_displacement=max_disp,
            production_frames=last_prod_frame_idx + 1,
        )

        spec_dict = asdict(spec)
        with open(output_dir / "animation_spec.json", "w", encoding="utf-8") as f:
            json.dump(spec_dict, f, indent=2, ensure_ascii=False)

        manifest = {
            "project_id": spec.project_id,
            "title": spec.title,
            "target_duration": spec.target_duration,
            "production_fps": spec.production_fps,
            "playback_fps": spec.playback_fps,
            "interpolation_mode": spec.interpolation_mode,
            "interpolation_reason": spec.interpolation_reason,
            "total_frames_rendered": len(frame_paths),
            "output_mp4": str(output_mp4),
            "qa": qa_result,
            "motion_eval": motion_eval,
            "sampled_keyframes": sampled_keyframe_records,
            "continuous_samples_count": len(continuous_sample_records),
        }
        with open(output_dir / "render_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return manifest

    def _generate_soundtrack(self, out_path: Path, duration_seconds: float) -> Path:
        """경쾌하고 애니메이션에 어울리는 신디사이저 BGM 및 비트 오디오 생성."""
        sample_rate = 44100
        total_samples = int(sample_rate * duration_seconds)

        bpm = 120
        quarter_samples = int(sample_rate * (60.0 / bpm))
        melody_notes = [261.63, 329.63, 392.00, 523.25, 440.00, 392.00, 329.63, 293.66]

        samples: list[int] = []
        for i in range(total_samples):
            t = i / sample_rate
            beat_idx = int(i // (quarter_samples / 2)) % len(melody_notes)
            freq = melody_notes[beat_idx]

            note_phase = (i % int(quarter_samples / 2)) / (quarter_samples / 2)
            envelope = math.exp(-3.0 * note_phase)
            melody = math.sin(2 * math.pi * freq * t) * envelope * 0.4

            bass_freq = 130.81 if (i // quarter_samples) % 2 == 0 else 98.00
            bass_env = math.exp(-2.5 * ((i % quarter_samples) / quarter_samples))
            bass = math.sin(2 * math.pi * bass_freq * t) * bass_env * 0.35

            pad = (math.sin(2 * math.pi * 261.63 * t) + math.sin(2 * math.pi * 329.63 * t) + math.sin(2 * math.pi * 392.00 * t)) * 0.1

            mixed = (melody + bass + pad) * 28000
            clamped = max(-32767, min(32767, int(mixed)))
            samples.append(clamped)

        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))

        return out_path


# --- Core 통합 계약 모듈 레벨 함수 및 전역 어댑터 ---

ADAPTER = FrameAnimationAdapter()


def preflight() -> Preflight:
    """Core 통합 계약 모듈 함수: 엔진 사전 점검."""
    return ADAPTER.preflight()


def render(
    spec: dict[str, Any],
    *,
    output_path: Path | str | None = None,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    """Core 통합 계약 모듈 함수: 엔진 렌더링."""
    if output_path is None:
        raise ValueError("output_path는 필수입니다.")
    return ADAPTER.render_segment(spec, output_path=output_path, workdir=workdir)


def render_segment(
    spec: dict[str, Any],
    *,
    output_path: Path | str,
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    """Core 통합 계약 세그먼트 렌더링."""
    return ADAPTER.render_segment(spec, output_path=output_path, workdir=workdir)
