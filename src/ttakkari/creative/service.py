from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def timeline(data: dict) -> dict:
    visual, captions, cursor = [], [], 0.0
    for scene in data["scenes"]:
        end = cursor + scene["duration"]
        visual.append(
            {
                "scene_id": scene["id"],
                "asset_id": scene["asset_id"],
                "start": cursor,
                "end": end,
                "animation": scene["animation"],
                "transition": scene["transition"],
            }
        )
        if scene["caption"]:
            captions.append(
                {
                    "scene_id": scene["id"],
                    "text": scene["caption"],
                    "start": cursor,
                    "end": end,
                }
            )
        cursor = end
    return {
        "duration": cursor,
        "visual": visual,
        "captions": captions,
        "music": [{"asset_id": data["music_asset_id"], "start": 0, "end": cursor}]
        if data["music_asset_id"]
        else [],
    }


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Scene(Strict):
    id: str = Field(default_factory=uid, max_length=50)
    duration: float = Field(default=10, ge=1, le=60)
    prompt: str = Field(default="", max_length=3000)
    caption: str = Field(default="", max_length=160)
    asset_id: str | None = None
    animation: Literal["still", "zoom_in", "zoom_out", "pan"] = "zoom_in"
    transition: Literal["fade", "cut"] = "fade"


class ProjectInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    brief: str = Field(default="", max_length=6000)
    content_type: str = Field(default="music_video", max_length=80)
    style: str = Field(default="cinematic", max_length=1000)
    duration: float = Field(default=30, ge=3, le=240)
    aspect: Literal["16:9", "9:16", "1:1"] = "16:9"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("제목을 입력해 주세요.")
        return value.strip()


class Studio:
    """Single-user Mac store. Immutable assets + atomic manifests + cross-process locks.

    No database schema reservation is needed for the first slice. Paths are private
    implementation details; every lookup starts from validated UUIDs.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, project_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(project_id))
        except ValueError as e:
            raise ValueError("프로젝트 ID가 잘못됐습니다.") from e
        return self.root / canonical

    def get(self, project_id: str) -> dict:
        p = self.path(project_id) / "project.json"
        if not p.is_file():
            raise FileNotFoundError("프로젝트가 없습니다.")
        return json.loads(p.read_text())

    def list(self) -> list[dict]:
        return sorted(
            [
                self.get(p.name)
                for p in self.root.iterdir()
                if p.is_dir() and (p / "project.json").is_file()
            ],
            key=lambda p: p["updated_at"],
            reverse=True,
        )

    def _save(self, data: dict):
        data["timeline"] = timeline(data)
        folder = self.path(data["id"])
        temp = folder / (uid() + ".tmp")
        with temp.open("w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(folder / "project.json")

    @contextmanager
    def transaction(self, project_id: str):
        folder = self.path(project_id)
        if not folder.is_dir():
            raise FileNotFoundError("프로젝트가 없습니다.")
        with (folder / "manifest.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                data = self.get(project_id)
                yield data
                data["updated_at"] = now()
                self._save(data)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def create(self, body: ProjectInput) -> dict:
        data = body.model_dump()
        data.update(
            id=uid(),
            revision=1,
            created_at=now(),
            updated_at=now(),
            music_asset_id=None,
            assets=[],
            generation_jobs=[],
            render_jobs=[],
        )
        count = max(3, math.ceil(body.duration / 10))
        data["scenes"] = [
            Scene(
                duration=body.duration / count, prompt=f"{body.brief} · 장면 {i + 1}"
            ).model_dump()
            for i in range(count)
        ]
        folder = self.path(data["id"])
        folder.mkdir()
        (folder / "assets").mkdir()
        (folder / "renders").mkdir()
        (folder / "cache").mkdir()
        self._save(data)
        return data

    def edit(self, project_id: str, revision: int, **changes) -> dict:
        with self.transaction(project_id) as data:
            if revision != data["revision"]:
                raise ValueError(
                    "다른 변경이 먼저 저장됐습니다. 새로고침한 뒤 다시 편집해 주세요."
                )
            if "scenes" in changes:
                scenes = [
                    s if isinstance(s, Scene) else Scene(**s) for s in changes["scenes"]
                ]
                if (
                    not 1 <= len(scenes) <= 60
                    or sum(s.duration for s in scenes) > 240.001
                ):
                    raise ValueError("장면은 1~60개, 영상 길이는 최대 240초입니다.")
                if len({s.id for s in scenes}) != len(scenes):
                    raise ValueError("장면 ID가 중복됐습니다.")
                images = {
                    a["id"] for a in data["assets"] if a["kind"] in ("image", "video")
                }
                if any(s.asset_id and s.asset_id not in images for s in scenes):
                    raise ValueError("이 프로젝트의 이미지·영상 자산을 선택해 주세요.")
                changes["scenes"] = [s.model_dump() for s in scenes]
                changes["duration"] = sum(s.duration for s in scenes)
            if (
                "music_asset_id" in changes
                and changes["music_asset_id"] is not None
                and changes["music_asset_id"]
                not in {a["id"] for a in data["assets"] if a["kind"] == "audio"}
            ):
                raise ValueError("이 프로젝트의 음악 자산을 선택해 주세요.")
            data.update(changes)
            data["revision"] += 1
        return data

    def add_asset(
        self,
        project_id: str,
        source: Path,
        *,
        name: str,
        kind: str,
        provenance: str,
        generation_job_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        content = source.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        with self.transaction(project_id) as data:
            existing = next(
                (a for a in data["assets"] if a["sha256"] == sha and a["kind"] == kind),
                None,
            )
            if existing:
                return existing
            asset_id = uid()
            relative = f"assets/{asset_id}{source.suffix.lower()}"
            (self.path(project_id) / relative).write_bytes(content)
            asset = {
                "id": asset_id,
                "name": name[:160],
                "kind": kind,
                "file": relative,
                "sha256": sha,
                "provenance": provenance,
                "generation_job_id": generation_job_id,
                "created_at": now(),
                "metadata": metadata or {},
            }
            data["assets"].append(asset)
            data["revision"] += 1
        return asset

    def demo(self, project_id: str) -> dict:
        """Reproducible local procedural assets. Explicitly not a neural model."""
        from ttakkari.creative.demo import make_demo

        return make_demo(self, project_id)

    def enqueue(self, project_id: str, retry: bool = False) -> dict:
        with self.transaction(project_id) as data:
            if not data["music_asset_id"] or any(
                not s["asset_id"] for s in data["scenes"]
            ):
                raise ValueError("음악과 모든 장면의 자산을 먼저 선택해 주세요.")
            snapshot = {
                k: copy.deepcopy(data[k])
                for k in (
                    "id",
                    "revision",
                    "name",
                    "aspect",
                    "duration",
                    "style",
                    "scenes",
                    "assets",
                    "music_asset_id",
                )
            }
            snapshot["timeline"] = timeline(data)
            # Revision/name/unused assets do not change the rendered media.
            used = {data["music_asset_id"], *(s["asset_id"] for s in data["scenes"])}
            key = digest(
                {
                    k: snapshot[k]
                    for k in ("aspect", "duration", "scenes", "music_asset_id")
                }
                | {
                    "assets": [
                        (a["id"], a["sha256"])
                        for a in snapshot["assets"]
                        if a["id"] in used
                    ],
                    "renderer": 1,
                }
            )
            matches = [j for j in data["render_jobs"] if j["cache_key"] == key]
            for job in reversed(matches):
                if job["status"] in ("queued", "running"):
                    return job
                if (
                    job["status"] == "succeeded"
                    and (self.path(project_id) / job["output"]).is_file()
                ):
                    return job
                if not retry:
                    raise ValueError(
                        "이 설정의 렌더가 실패했습니다. 재시도를 눌러 주세요."
                    )
                break
            if (
                len(
                    [
                        j
                        for j in data["render_jobs"]
                        if j["status"] in ("queued", "running")
                    ]
                )
                >= 3
            ):
                raise ValueError("프로젝트당 대기 렌더는 최대 3개입니다.")
            job = {
                "id": uid(),
                "cache_key": key,
                "status": "queued",
                "progress": 0,
                "created_at": now(),
                "started_at": None,
                "ended_at": None,
                "elapsed_seconds": None,
                "peak_rss_bytes": None,
                "output": None,
                "error": None,
                "snapshot": snapshot,
                "cached_scenes": 0,
            }
            data["render_jobs"].append(job)
        return job


def default_studio() -> Studio:
    from ttakkari.config import get_settings

    return Studio(get_settings().home / "creative-studio")
