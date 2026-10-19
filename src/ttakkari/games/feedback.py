"""시각 피드백(스크린샷 Artifact + 정규화 영역)을 worker 가 실제로 열 수 있는 파일로 내놓는다 (TASK-G11 후속, ADR 0052).

Artifact 는 data_dir 의 관리 영역에 있어 게임 workspace 안에서 돌아가는 worker 가 열 수 없다. 그래서 요청마다 검증한 PNG 를
게임 저장소 안 `.ttakkari-feedback/<id>/` 에 복사하고 절대 경로를 prompt 에 싣는다. 이 디렉터리는 `.git/info/exclude` 로 git 추적에서
빼서(저장소 파일을 건드리지 않는다) 작업 트리가 더러워지지 않고, 격리 빌드 복사에서도 제외된다.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.service import content_path
from ttakkari.broker.policy import BrokerError
from ttakkari.games.contract import GameContractError
from ttakkari.models import Artifact, GameProject

FEEDBACK_DIR = ".ttakkari-feedback"
KEEP_LAST = 20
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_ANNOTATIONS = 20
MAX_PICKED = 10
MAX_CLIP_FRAMES = 8
MAX_CLIP_MS = 15_000
MAX_CLIP_FRAME_BYTES = 1024 * 1024
MAX_CLIP_TOTAL_BYTES = 6 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
COORDINATE_SPACE = "normalized-0-1-top-left"
EPS = 1e-6

Unit = Annotated[float, Field(ge=0.0, le=1.0)]


class BoxIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["box"]
    x: Unit
    y: Unit
    w: Unit
    h: Unit


class ArrowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["arrow"]
    x1: Unit
    y1: Unit
    x2: Unit
    y2: Unit


class FreehandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["freehand"]
    points: list[Annotated[list[Unit], Field(min_length=2, max_length=2)]] = Field(min_length=2, max_length=200)


AnnotationIn = Annotated[BoxIn | ArrowIn | FreehandIn, Field(discriminator="type")]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


class PickBoundsIn(BaseModel):
    """선택한 오브젝트의 화면 경계. 캔버스 대비 0~1 비율이지만 일부가 화면 밖으로 나간 오브젝트도 있어 범위를 넉넉히 둔다."""

    model_config = ConfigDict(extra="forbid")
    x: Annotated[float, Field(ge=-2.0, le=3.0)]
    y: Annotated[float, Field(ge=-2.0, le=3.0)]
    width: Annotated[float, Field(gt=0.0, le=5.0)]
    height: Annotated[float, Field(gt=0.0, le=5.0)]


class PickedObjectIn(BaseModel):
    """관제탑의 오브젝트 선택(ADR 0059) 결과 1건. 게임이 bridge 로 알려 준 값이라 믿지 않는다: 길이를 제한하고 제어 문자를 없앤다."""

    model_config = ConfigDict(extra="forbid")
    scene: str = Field(default="", max_length=80)
    name: str = Field(default="", max_length=80)
    type: str = Field(min_length=1, max_length=40)
    texture: str | None = Field(default=None, max_length=80)
    frame: str | None = Field(default=None, max_length=80)
    text: str | None = Field(default=None, max_length=60)
    container: str | None = Field(default=None, max_length=80)
    depth: float = Field(default=0.0, ge=-1e6, le=1e6)
    bounds: PickBoundsIn

    @field_validator("scene", "name", "type", "texture", "frame", "text", "container")
    @classmethod
    def _no_control_chars(cls, v: str | None) -> str | None:
        return None if v is None else _CONTROL_RE.sub(" ", v).strip()


class PickPointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: Unit
    y: Unit


class ClipFrameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: uuid.UUID  # 사용자가 확인한 구간 프레임을 /api/uploads 로 올린 Artifact(JPEG)
    t_ms: int = Field(ge=0, le=3_600_000)


class ClipIn(BaseModel):
    """사용자가 녹화에서 고른 구간(ADR 0060). 영상 파일이 아니라 구간의 대표 프레임이다. 전체 녹화는 받지 않는다."""

    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0, le=3_600_000)
    end_ms: int = Field(ge=0, le=3_600_000)
    frames: list[ClipFrameIn] = Field(min_length=1, max_length=MAX_CLIP_FRAMES)
    note: str = Field(default="", max_length=200)  # 사용자가 이 구간에서 문제라고 한 것

    @field_validator("note")
    @classmethod
    def _note_clean(cls, v: str) -> str:
        return _CONTROL_RE.sub(" ", v).strip()


class FeedbackIn(BaseModel):
    """요청에 붙는 시각 피드백. 스크린샷은 Artifact 참조로만 받고 좌표는 이미지 왼쪽 위 (0,0) 기준 0~1 비율이다."""

    model_config = ConfigDict(extra="forbid")
    build_id: uuid.UUID  # 캡처한 빌드. 요청의 build_id 와 같아야 한다
    artifact_id: uuid.UUID | None = None  # 원본 스크린샷
    annotated_artifact_id: uuid.UUID | None = None  # 주석을 그린 스크린샷
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    coordinate_space: Literal["normalized-0-1-top-left"] = COORDINATE_SPACE
    annotations: list[AnnotationIn] = Field(default_factory=list, max_length=MAX_ANNOTATIONS)
    picked: list[PickedObjectIn] = Field(default_factory=list, max_length=MAX_PICKED)  # 사용자가 화면에서 고른 오브젝트(위에 그려진 것부터)
    pick_point: PickPointIn | None = None  # 고른 위치(게임 화면 0~1 비율)
    clip: ClipIn | None = None  # 사용자가 고른 녹화 구간의 프레임(ADR 0060)


@dataclass(frozen=True)
class StagedFeedback:
    dir: Path
    original: Path | None
    annotated: Path | None
    meta: Path
    annotations: list[dict]
    width: int | None
    height: int | None
    build_id: uuid.UUID
    artifact_id: uuid.UUID | None
    annotated_artifact_id: uuid.UUID | None
    picked: list[dict]
    pick_point: dict | None
    clip: dict | None = None  # {start_ms, end_ms, note, frames: [{file, t_ms, artifact_id}]}
    clip_files: tuple[Path, ...] = ()


def validate_annotations(items: list) -> list[dict]:
    out: list[dict] = []
    for a in items:
        d = a.model_dump()
        if d["type"] == "box" and (d["x"] + d["w"] > 1 + EPS or d["y"] + d["h"] > 1 + EPS):
            raise GameContractError("box 영역이 이미지 밖으로 나갑니다(x+w, y+h 는 1 이하).", "bad_request")
        out.append(d)
    return out


def describe_annotation(i: int, a: dict) -> str:
    if a["type"] == "box":
        return f"{i}. 사각형: 왼쪽 위 ({a['x']:.3f}, {a['y']:.3f}), 너비 {a['w']:.3f}, 높이 {a['h']:.3f}"
    if a["type"] == "arrow":
        return f"{i}. 화살표: ({a['x1']:.3f}, {a['y1']:.3f}) → ({a['x2']:.3f}, {a['y2']:.3f}) 끝이 가리키는 곳이 대상"
    pts = a["points"]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return f"{i}. 자유 곡선 {len(pts)}점: 외곽 x {min(xs):.3f}~{max(xs):.3f}, y {min(ys):.3f}~{max(ys):.3f}"


def describe_picked(i: int, p: dict) -> str:
    """worker prompt 한 줄. 게임이 낸 문자열은 JSON 으로 감싸 prompt 구조를 깨거나 지시처럼 읽히지 않게 한다."""
    fields = {k: p[k] for k in ("name", "type", "texture", "frame", "text", "container", "scene") if p.get(k)}
    b = p["bounds"]
    return f"{i}. {json.dumps(fields, ensure_ascii=False)} 화면 경계 x {b['x']:.3f}, y {b['y']:.3f}, 너비 {b['width']:.3f}, 높이 {b['height']:.3f}"


def _read_artifact(art: Artifact | None, game: GameProject, label: str) -> bytes:
    if art is None or art.workspace_id != game.workspace_id:
        raise GameContractError(f"{label} Artifact 를 이 게임에서 찾을 수 없습니다.", "bad_request")
    if art.mime_type != "image/png":
        raise GameContractError(f"{label} 는 PNG 이미지여야 합니다.", "bad_request")
    if art.size_bytes > MAX_IMAGE_BYTES:
        raise GameContractError(f"{label} 가 너무 큽니다(최대 {MAX_IMAGE_BYTES // (1024 * 1024)}MB).", "bad_request")
    try:
        data = content_path(art).read_bytes()
    except (BrokerError, OSError) as e:
        raise GameContractError(f"{label} 파일을 읽을 수 없습니다.", "bad_request") from e
    if not data.startswith(PNG_MAGIC) or len(data) > MAX_IMAGE_BYTES:
        raise GameContractError(f"{label} 가 PNG 형식이 아닙니다.", "bad_request")
    if hashlib.sha256(data).hexdigest() != art.sha256:
        raise GameContractError(f"{label} 의 내용이 기록된 해시와 다릅니다.", "bad_request")
    return data


JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def _read_clip_frame(art: Artifact | None, game: GameProject, i: int) -> bytes:
    label = f"구간 프레임 {i}"
    if art is None or art.workspace_id != game.workspace_id:
        raise GameContractError(f"{label} Artifact 를 이 게임에서 찾을 수 없습니다.", "bad_request")
    if art.mime_type != "image/jpeg":
        raise GameContractError(f"{label} 는 JPEG 이미지여야 합니다.", "bad_request")
    if art.size_bytes > MAX_CLIP_FRAME_BYTES:
        raise GameContractError(f"{label} 가 너무 큽니다(최대 {MAX_CLIP_FRAME_BYTES // 1024}KB).", "bad_request")
    try:
        data = content_path(art).read_bytes()
    except (BrokerError, OSError) as e:
        raise GameContractError(f"{label} 파일을 읽을 수 없습니다.", "bad_request") from e
    if len(data) > MAX_CLIP_FRAME_BYTES or not (data.startswith(JPEG_SOI) and data.endswith(JPEG_EOI)):
        raise GameContractError(f"{label} 가 JPEG 형식이 아닙니다.", "bad_request")
    if hashlib.sha256(data).hexdigest() != art.sha256:
        raise GameContractError(f"{label} 의 내용이 기록된 해시와 다릅니다.", "bad_request")
    return data


def validate_clip(clip: ClipIn) -> None:
    """구간·프레임 시각의 일관성. 길이·장수 상한은 모델이, 여기서는 순서와 구간 포함 관계를 본다."""
    if clip.end_ms <= clip.start_ms:
        raise GameContractError("구간의 끝은 시작보다 뒤여야 합니다.", "bad_request")
    if clip.end_ms - clip.start_ms > MAX_CLIP_MS:
        raise GameContractError(f"구간이 너무 깁니다(최대 {MAX_CLIP_MS // 1000}초).", "bad_request")
    times = [f.t_ms for f in clip.frames]
    if any(not (clip.start_ms <= t <= clip.end_ms) for t in times):
        raise GameContractError("프레임 시각이 선택한 구간 밖에 있습니다.", "bad_request")
    if any(b <= a for a, b in pairwise(times)):
        raise GameContractError("프레임은 시각 순서대로 중복 없이 보내야 합니다.", "bad_request")


def _exclude_from_git(root: Path) -> None:
    info = root / ".git" / "info"
    if not info.is_dir():
        return
    exclude = info / "exclude"
    current = exclude.read_text() if exclude.is_file() else ""
    if FEEDBACK_DIR not in current.split():
        exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + f"/{FEEDBACK_DIR}/\n")


def _prune(base: Path) -> None:
    dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    for old in dirs[:-KEEP_LAST]:
        shutil.rmtree(old, ignore_errors=True)


async def stage_feedback(db: AsyncSession, game: GameProject, root: Path, feedback: FeedbackIn, *, instruction: str) -> StagedFeedback:
    """검증한 스크린샷을 저장소 안으로 복사하고 feedback.json 을 남긴다. 어느 하나라도 검증에 실패하면 아무것도 쓰지 않는다."""
    if (feedback.artifact_id is None and feedback.annotated_artifact_id is None and not feedback.annotations and not feedback.picked
            and feedback.clip is None):
        raise GameContractError("시각 피드백에는 스크린샷, 영역 주석, 고른 오브젝트, 녹화 구간 중 하나가 있어야 합니다.", "bad_request")
    original = _read_artifact(await db.get(Artifact, feedback.artifact_id), game, "원본 스크린샷") if feedback.artifact_id else None
    annotated = (_read_artifact(await db.get(Artifact, feedback.annotated_artifact_id), game, "주석 스크린샷")
                 if feedback.annotated_artifact_id else None)
    annotations = validate_annotations(feedback.annotations)
    picked = [p.model_dump() for p in feedback.picked]
    pick_point = feedback.pick_point.model_dump() if feedback.pick_point else None
    clip_frames: list[tuple[ClipFrameIn, bytes]] = []
    if feedback.clip is not None:
        validate_clip(feedback.clip)
        total = 0
        for i, f in enumerate(feedback.clip.frames, 1):
            data = _read_clip_frame(await db.get(Artifact, f.artifact_id), game, i)
            total += len(data)
            if total > MAX_CLIP_TOTAL_BYTES:
                raise GameContractError(f"구간 프레임의 합계가 너무 큽니다(최대 {MAX_CLIP_TOTAL_BYTES // (1024 * 1024)}MB).", "bad_request")
            clip_frames.append((f, data))

    base = root / FEEDBACK_DIR
    base.mkdir(exist_ok=True, mode=0o700)
    d = base / uuid.uuid4().hex[:12]
    d.mkdir(mode=0o700)
    paths: dict[str, Path | None] = {"original": None, "annotated": None}
    for key, data, name in (("original", original, "screenshot.png"), ("annotated", annotated, "screenshot-annotated.png")):
        if data is not None:
            (d / name).write_bytes(data)
            (d / name).chmod(0o600)
            paths[key] = d / name
    clip_files: list[Path] = []
    clip_meta: dict | None = None
    if feedback.clip is not None:
        entries = []
        for i, (f, data) in enumerate(clip_frames, 1):
            name = f"clip-{i:02d}.jpg"
            (d / name).write_bytes(data)
            (d / name).chmod(0o600)
            clip_files.append(d / name)
            entries.append({"file": name, "t_ms": f.t_ms, "artifact_id": str(f.artifact_id)})
        clip_meta = {"start_ms": feedback.clip.start_ms, "end_ms": feedback.clip.end_ms, "note": feedback.clip.note, "frames": entries}
    meta = d / "feedback.json"
    meta.write_text(json.dumps({
        "version": 1, "game_id": str(game.id), "build_id": str(feedback.build_id), "coordinate_space": COORDINATE_SPACE,
        "image_size": [feedback.width, feedback.height], "screenshot": "screenshot.png" if original else None,
        "annotated": "screenshot-annotated.png" if annotated else None, "annotations": annotations, "picked": picked, "pick_point": pick_point,
        "clip": clip_meta, "instruction": instruction,
    }, ensure_ascii=False, indent=2) + "\n")
    meta.chmod(0o600)
    _exclude_from_git(root)
    _prune(base)
    return StagedFeedback(d, paths["original"], paths["annotated"], meta, annotations, feedback.width, feedback.height, feedback.build_id,
                          feedback.artifact_id, feedback.annotated_artifact_id, picked, pick_point, clip_meta, tuple(clip_files))
