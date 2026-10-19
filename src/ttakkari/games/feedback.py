"""시각 피드백(스크린샷 Artifact + 정규화 영역)을 worker 가 실제로 열 수 있는 파일로 내놓는다 (TASK-G11 후속, ADR 0051).

Artifact 는 data_dir 의 관리 영역에 있어 게임 workspace 안에서 돌아가는 worker 가 열 수 없다. 그래서 요청마다 검증한 PNG 를
게임 저장소 안 `.ttakkari-feedback/<id>/` 에 복사하고 절대 경로를 prompt 에 싣는다. 이 디렉터리는 `.git/info/exclude` 로 git 추적에서
빼서(저장소 파일을 건드리지 않는다) 작업 트리가 더러워지지 않고, 격리 빌드 복사에서도 제외된다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.service import content_path
from ttakkari.broker.policy import BrokerError
from ttakkari.games.contract import GameContractError
from ttakkari.models import Artifact, GameProject

FEEDBACK_DIR = ".ttakkari-feedback"
KEEP_LAST = 20
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_ANNOTATIONS = 20
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
    if feedback.artifact_id is None and feedback.annotated_artifact_id is None and not feedback.annotations:
        raise GameContractError("시각 피드백에는 스크린샷 또는 영역 주석이 있어야 합니다.", "bad_request")
    original = _read_artifact(await db.get(Artifact, feedback.artifact_id), game, "원본 스크린샷") if feedback.artifact_id else None
    annotated = (_read_artifact(await db.get(Artifact, feedback.annotated_artifact_id), game, "주석 스크린샷")
                 if feedback.annotated_artifact_id else None)
    annotations = validate_annotations(feedback.annotations)

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
    meta = d / "feedback.json"
    meta.write_text(json.dumps({
        "version": 1, "game_id": str(game.id), "build_id": str(feedback.build_id), "coordinate_space": COORDINATE_SPACE,
        "image_size": [feedback.width, feedback.height], "screenshot": "screenshot.png" if original else None,
        "annotated": "screenshot-annotated.png" if annotated else None, "annotations": annotations, "instruction": instruction,
    }, ensure_ascii=False, indent=2) + "\n")
    meta.chmod(0o600)
    _exclude_from_git(root)
    _prune(base)
    return StagedFeedback(d, paths["original"], paths["annotated"], meta, annotations, feedback.width, feedback.height, feedback.build_id,
                          feedback.artifact_id, feedback.annotated_artifact_id)
