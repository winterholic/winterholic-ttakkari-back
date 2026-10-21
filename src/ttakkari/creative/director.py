"""CPU-first, file-backed production control plane for the creative studio."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return str(uuid.uuid4())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash(value: object) -> str:
    return _sha(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )


class ConflictError(ValueError):
    """Optimistic revision did not match the latest record."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ProductionManifest(StrictModel):
    schema_version: Literal[1] = 1
    production_id: str
    manifest_revision: int = Field(ge=1)
    content_hash: str
    brief_ref: str
    bible_ref: str
    timeline_ref: str
    active_shot_revisions: dict[str, int]
    policy_ref: str = "research-null-approval"
    created_at: str
    updated_at: str


class BibleRevision(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    content_hash: str
    palette: list[str] = Field(default_factory=list)
    typography: dict = Field(default_factory=dict)
    composition: dict = Field(default_factory=dict)
    camera: dict = Field(default_factory=dict)
    character_reference_hashes: list[str] = Field(default_factory=list)
    negative_rules: list[str] = Field(default_factory=list)
    author: str = "system"
    parent_revision: int | None = None
    approval_event_id: str | None = None
    created_at: str


class TimelineRevision(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(default=1, ge=1)
    content_hash: str = ""
    fps_num: int = Field(gt=0)
    fps_den: int = Field(gt=0)
    duration_frames: int = Field(gt=0)
    sample_rate: int = Field(gt=0)
    shots: list[dict] = Field(default_factory=list)
    sections: list[dict] = Field(default_factory=list)
    beats: list[dict] = Field(default_factory=list)
    cues: list[dict] = Field(default_factory=list)
    created_at: str = ""

    @model_validator(mode="after")
    def validate_shots(self):
        seen = set()
        previous_end = 0
        for shot in self.shots:
            start, end = shot.get("start_f"), shot.get("end_f")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= self.duration_frames
            ):
                raise ValueError(
                    "반열림 프레임 구간은 0≤start<end≤duration이어야 합니다."
                )
            ident = shot.get("shot_id")
            if not ident or ident in seen or start != previous_end:
                raise ValueError("타임라인 샷 ID 또는 순서가 잘못됐습니다.")
            seen.add(ident)
            previous_end = end
        if self.shots and previous_end != self.duration_frames:
            raise ValueError(
                "타임라인 샷은 선언된 duration 프레임을 전부 덮어야 합니다."
            )
        return self

    @staticmethod
    def sample_to_frame(
        sample: int, *, sample_rate: int, fps_num: int, fps_den: int
    ) -> int:
        if sample < 0 or sample_rate <= 0 or fps_num <= 0 or fps_den <= 0:
            raise ValueError("샘플·샘플레이트·FPS는 유효한 양수여야 합니다.")
        exact = Fraction(sample * fps_num, sample_rate * fps_den)
        return (2 * exact.numerator + exact.denominator) // (2 * exact.denominator)


class ShotRevision(StrictModel):
    schema_version: Literal[1] = 1
    shot_id: str
    revision: int = Field(ge=1)
    content_hash: str
    intent: str = ""
    start_f: int = Field(ge=0)
    end_f: int = Field(gt=0)
    engine: str = "cpu-fake"
    profile: str = "copy-existing-media"
    input_sha256: list[str] = Field(default_factory=list)
    bible_hash: str
    timeline_hash: str
    skill_versions: dict[str, str] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    seed: int | None = None
    budget: dict = Field(default_factory=dict)
    created_at: str

    @model_validator(mode="after")
    def half_open(self):
        if self.end_f <= self.start_f:
            raise ValueError("샷 구간은 반열림 프레임 [start_f,end_f)이어야 합니다.")
        return self


class WorkflowRevision(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1)
    content_hash: str
    api_graph: dict
    bind_schema: dict = Field(default_factory=dict)
    model_hashes: dict[str, str] = Field(default_factory=dict)
    custom_nodes_hashes: list[str] = Field(default_factory=list)
    runtime: dict = Field(default_factory=dict)
    license_refs: list[str] = Field(default_factory=list)
    preset: str = "cpu-fake"
    created_at: str


class AttemptReceipt(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    attempt_id: str
    idempotency_key: str
    content_hash: str
    shot_id: str
    shot_revision: int
    bible_revision: int
    timeline_revision: int
    workflow_revision: int
    input_sha256: str
    external_prompt_id: str | None = None
    resolved_graph: dict
    resolved_graph_sha256: str
    seed: int | None = None
    state: Literal[
        "queued", "running", "collecting", "review_pending", "failed", "cancelled"
    ]
    last_event_sequence: int = 0
    expected_output_count: int | None = None
    error_kind: str | None = None
    outputs: list[dict] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ReviewReceipt(StrictModel):
    schema_version: Literal[1] = 1
    review_id: str
    content_hash: str
    output_sha256: str
    shot_revision: int | None
    bible_revision: int
    timeline_revision: int
    criteria: dict[str, Literal["pass", "fail", "unknown"]]
    evidence_uris: list[str] = Field(default_factory=list)
    reviewer: str = "cpu-check"
    independent: bool = False
    approval_event_id: str | None = None
    created_at: str


class DirectorStore:
    """One production per private directory; state revisions and events use flock + atomic replace."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve(strict=True)

    def production_path(self, production_id: str) -> Path:
        try:
            canonical = str(uuid.UUID(production_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("제작 ID가 잘못됐습니다.") from exc
        path = self.root / canonical
        if path.is_symlink():
            raise ValueError("제작 경로 symlink는 허용되지 않습니다.")
        return path

    def _state_path(self, production_id: str) -> Path:
        return self.production_path(production_id) / "state.json"

    @contextmanager
    def _locked_state(self, production_id: str):
        folder = self.production_path(production_id)
        if not folder.is_dir() or folder.is_symlink():
            raise FileNotFoundError("제작물이 없습니다.")
        lock_path = folder / "state.lock"
        if lock_path.is_symlink():
            raise ValueError("잠금 경로 symlink는 허용되지 않습니다.")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state_path = folder / "state.json"
                if state_path.is_symlink():
                    raise ValueError("상태 경로 symlink는 허용되지 않습니다.")
                state = json.loads(state_path.read_text())
                yield state
                self._save_state(folder, state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _save_state(folder: Path, state: dict) -> None:
        temp = folder / (".state-" + _id() + ".tmp")
        data = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2).encode()
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, folder / "state.json")
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def create_production(self, brief: str) -> dict:
        pid = _id()
        folder = self.production_path(pid)
        folder.mkdir(mode=0o700)
        (folder / "artifacts").mkdir(mode=0o700)
        (folder / "state.lock").touch(mode=0o600)
        created = _now()
        bible_body = {
            "palette": [],
            "typography": {},
            "composition": {},
            "camera": {},
            "character_reference_hashes": [],
            "negative_rules": [],
        }
        bible = BibleRevision(
            revision=1, content_hash=_hash(bible_body), **bible_body, created_at=created
        ).model_dump()
        timeline_body = {
            "fps_num": 24,
            "fps_den": 1,
            "duration_frames": 48,
            "sample_rate": 48_000,
            "shots": [
                {"shot_id": "shot-1", "start_f": 0, "end_f": 24},
                {"shot_id": "shot-2", "start_f": 24, "end_f": 48},
            ],
        }
        timeline = TimelineRevision(**timeline_body, created_at=created)
        timeline = timeline.model_copy(
            update={"content_hash": _hash(timeline_body)}
        ).model_dump()
        shots = {}
        for shot_id, start, end in (("shot-1", 0, 24), ("shot-2", 24, 48)):
            body = {
                "shot_id": shot_id,
                "revision": 1,
                "intent": "",
                "start_f": start,
                "end_f": end,
                "engine": "cpu-fake",
                "profile": "copy-existing-media",
                "input_sha256": [],
                "bible_hash": bible["content_hash"],
                "timeline_hash": timeline["content_hash"],
                "skill_versions": {},
                "acceptance_criteria": [],
                "seed": None,
                "budget": {},
            }
            shots[shot_id] = [
                ShotRevision(
                    **{**body, "content_hash": _hash(body), "created_at": created}
                ).model_dump()
            ]
        workflow_body = {
            "api_graph": {"kind": "copy-existing-media"},
            "bind_schema": {},
            "model_hashes": {},
            "custom_nodes_hashes": [],
            "runtime": {"engine": "cpu-fake"},
            "license_refs": [],
            "preset": "cpu-fake",
        }
        workflow = WorkflowRevision(
            revision=1,
            content_hash=_hash(workflow_body),
            **workflow_body,
            created_at=created,
        ).model_dump()
        manifest_body = {
            "schema_version": 1,
            "production_id": pid,
            "manifest_revision": 1,
            "brief_ref": brief,
            "bible_ref": "bible:1",
            "timeline_ref": "timeline:1",
            "active_shot_revisions": {key: 1 for key in shots},
            "policy_ref": "research-null-approval",
            "created_at": created,
            "updated_at": created,
        }
        manifest_body["content_hash"] = _hash(manifest_body)
        state = {
            "schema_version": 1,
            "manifest": ProductionManifest(**manifest_body).model_dump(),
            "bibles": [bible],
            "timelines": [timeline],
            "shots": shots,
            "workflows": [workflow],
            "attempts": [],
            "reviews": [],
            "events": [],
            "artifacts": {},
            "stale": {
                "shots": [],
                "transition_neighbors": [],
                "bible_assets": [],
                "reviews": [],
            },
            "cancelled": False,
        }
        self._save_state(folder, state)
        return state

    def get_production(self, production_id: str) -> dict:
        path = self._state_path(production_id)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("제작물이 없습니다.")
        return json.loads(path.read_text())

    def _revise(
        self,
        production_id: str,
        collection: str,
        expected_revision: int,
        values: dict,
        key: str | None = None,
    ):
        mutable_fields = {
            "bibles": {
                "palette",
                "typography",
                "composition",
                "camera",
                "character_reference_hashes",
                "negative_rules",
                "author",
                "parent_revision",
                "approval_event_id",
            },
            "timelines": {
                "fps_num",
                "fps_den",
                "duration_frames",
                "sample_rate",
                "shots",
                "sections",
                "beats",
                "cues",
            },
            "shots": {
                "intent",
                "start_f",
                "end_f",
                "engine",
                "profile",
                "input_sha256",
                "bible_hash",
                "timeline_hash",
                "skill_versions",
                "acceptance_criteria",
                "seed",
                "budget",
            },
            "workflows": {
                "api_graph",
                "bind_schema",
                "model_hashes",
                "custom_nodes_hashes",
                "runtime",
                "license_refs",
                "preset",
            },
        }[collection]
        unknown = set(values) - mutable_fields
        if unknown:
            raise ValueError("수정할 수 없는 필드: " + ", ".join(sorted(unknown)))
        with self._locked_state(production_id) as state:
            rows = state[collection] if key is None else state[collection][key]
            current = rows[-1]
            if current["revision"] != expected_revision:
                raise ConflictError("expected_revision이 현재 revision과 다릅니다.")
            body = {
                k: v
                for k, v in current.items()
                if k not in {"content_hash", "created_at", "revision"}
            }
            body.update(values)
            if collection == "bibles" and values.get("approval_event_id") is not None:
                raise ValueError(
                    "사용자 승인 event는 사용자 승인 경로에서만 생성할 수 있습니다."
                )
            revision = expected_revision + 1
            content_hash = _hash(body)
            body.update(revision=revision, content_hash=content_hash, created_at=_now())
            model = {
                "bibles": BibleRevision,
                "timelines": TimelineRevision,
                "shots": ShotRevision,
                "workflows": WorkflowRevision,
            }[collection]
            record = model(**body).model_dump()
            rows.append(record)
            if collection == "shots":
                state["manifest"]["active_shot_revisions"][key] = revision
                state["stale"]["shots"] = sorted(set(state["stale"]["shots"] + [key]))
                ordered = list(state["shots"])
                i = ordered.index(key)
                neighbors = ordered[max(0, i - 1) : i] + ordered[i + 1 : i + 2]
                state["stale"]["transition_neighbors"] = sorted(
                    set(state["stale"]["transition_neighbors"] + neighbors)
                )
                state["stale"]["reviews"] = sorted(
                    set(state["stale"]["reviews"] + ["production"])
                )
            elif collection == "bibles":
                state["manifest"]["bible_ref"] = f"bible:{revision}"
                state["stale"]["bible_assets"] = sorted(state["shots"])
                state["stale"]["reviews"] = sorted(
                    set(state["stale"]["reviews"] + ["bible", "production"])
                )
            elif collection == "timelines":
                state["manifest"]["timeline_ref"] = f"timeline:{revision}"
                state["stale"]["shots"] = sorted(state["shots"])
                state["stale"]["reviews"] = sorted(
                    set(state["stale"]["reviews"] + ["timeline", "production"])
                )
            state["manifest"]["manifest_revision"] += 1
            state["manifest"]["updated_at"] = _now()
            state["manifest"]["content_hash"] = _hash(
                {k: v for k, v in state["manifest"].items() if k != "content_hash"}
            )
            return record

    def update_bible(self, pid: str, *, expected_revision: int, values: dict):
        return self._revise(pid, "bibles", expected_revision, values)

    def update_timeline(self, pid: str, *, expected_revision: int, values: dict):
        return self._revise(pid, "timelines", expected_revision, values)

    def update_shot(
        self, pid: str, shot_id: str, *, expected_revision: int, values: dict
    ):
        return self._revise(pid, "shots", expected_revision, values, shot_id)

    def update_workflow(self, pid: str, *, expected_revision: int, values: dict):
        return self._revise(pid, "workflows", expected_revision, values)

    def update_manifest(self, pid: str, *, expected_revision: int, values: dict):
        with self._locked_state(pid) as state:
            manifest = state["manifest"]
            if manifest["manifest_revision"] != expected_revision:
                raise ConflictError("expected_revision이 현재 revision과 다릅니다.")
            mutable_fields = {
                "brief_ref",
                "bible_ref",
                "timeline_ref",
                "active_shot_revisions",
                "policy_ref",
            }
            unknown = set(values) - mutable_fields
            if unknown:
                raise ValueError("수정할 수 없는 필드: " + ", ".join(sorted(unknown)))
            if "approval_event_id" in values:
                raise ValueError(
                    "사용자 승인 event는 사용자 승인 경로에서만 생성할 수 있습니다."
                )
            manifest.update(values)
            manifest["manifest_revision"] += 1
            manifest["updated_at"] = _now()
            manifest["content_hash"] = _hash(
                {k: v for k, v in manifest.items() if k != "content_hash"}
            )
            manifest = ProductionManifest(**manifest).model_dump()
            state["manifest"] = manifest
            return manifest

    def put_artifact(
        self, pid: str, name: str, content: bytes, *, media_type: str
    ) -> dict:
        if (
            not name
            or name in {".", ".."}
            or Path(name).name != name
            or "/" in name
            or "\\" in name
        ):
            raise ValueError("파일명은 경로가 아닌 단일 이름이어야 합니다.")
        if not content:
            raise ValueError("빈 파일은 자산으로 저장할 수 없습니다.")
        sha = _sha(content)
        folder = self.production_path(pid) / "artifacts"
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("자산 저장 경로가 안전하지 않습니다.")
        target = folder / sha
        if target.is_symlink():
            raise ValueError("자산 경로 symlink는 허용되지 않습니다.")
        temp = folder / ("." + _id() + ".part")
        with temp.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if target.exists():
                if target.is_symlink() or _sha(target.read_bytes()) != sha:
                    raise ValueError("기존 CAS 파일 무결성이 맞지 않습니다.")
            else:
                try:
                    os.link(temp, target)
                except FileExistsError:
                    if target.is_symlink() or _sha(target.read_bytes()) != sha:
                        raise ValueError("CAS 경로 충돌입니다.")
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink(missing_ok=True)
        with self._locked_state(pid) as state:
            prior = state["artifacts"].get(name)
            if prior and prior["sha256"] != sha:
                raise ValueError("동명 파일을 다른 내용으로 덮어쓸 수 없습니다.")
            record = {
                "name": name,
                "sha256": sha,
                "bytes": len(content),
                "media_type": media_type,
                "path": f"artifacts/{sha}",
            }
            state["artifacts"][name] = record
            return record

    def read_artifact(self, pid: str, sha256: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256가 잘못됐습니다.")
        folder = self.production_path(pid) / "artifacts"
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("자산 저장 디렉터리 symlink는 허용되지 않습니다.")
        target = folder / sha256
        if target.is_symlink():
            raise ValueError("자산 symlink는 읽을 수 없습니다.")
        try:
            resolved = target.resolve(strict=True)
            if resolved.parent != folder.resolve(strict=True):
                raise ValueError("자산 경로가 작품 루트를 벗어났습니다.")
            data = target.read_bytes()
        except FileNotFoundError as exc:
            raise FileNotFoundError("자산이 없습니다.") from exc
        if _sha(data) != sha256:
            raise ValueError("자산 sha256 검증에 실패했습니다.")
        return data

    def _event(
        self,
        state: dict,
        event_id: str,
        kind: str,
        attempt_id: str | None = None,
        **details,
    ):
        if not any(
            event["event_id"] == event_id and event["attempt_id"] == attempt_id
            for event in state["events"]
        ):
            state["events"].append(
                {
                    "event_id": event_id,
                    "kind": kind,
                    "attempt_id": attempt_id,
                    "at": _now(),
                    **details,
                }
            )

    @staticmethod
    def _seal_attempt(attempt: dict) -> None:
        attempt["content_hash"] = _hash(
            {k: v for k, v in attempt.items() if k != "content_hash"}
        )

    def submit_attempt(
        self, pid: str, shot_id: str, *, input_sha256: str, idempotency_key: str
    ):
        with self._locked_state(pid) as state:
            if state["cancelled"]:
                raise ValueError("취소된 제작에는 새 제출을 할 수 없습니다.")
            if not any(
                item["sha256"] == input_sha256 for item in state["artifacts"].values()
            ):
                raise ValueError("입력 sha256이 제작 자산 manifest에 없습니다.")
            if shot_id not in state["shots"]:
                raise ValueError("샷이 없습니다.")
            for item in state["attempts"]:
                if item["idempotency_key"] == idempotency_key:
                    if (
                        item["shot_id"] != shot_id
                        or item["input_sha256"] != input_sha256
                    ):
                        raise ValueError(
                            "idempotency_key가 다른 입력에 이미 사용됐습니다."
                        )
                    return item
            shot = state["shots"][shot_id][-1]
            workflow = state["workflows"][-1]
            aid, task_id = _id(), _id()
            graph = workflow["api_graph"]
            attempt_body = {
                "schema_version": 1,
                "task_id": task_id,
                "attempt_id": aid,
                "idempotency_key": idempotency_key,
                "shot_id": shot_id,
                "shot_revision": shot["revision"],
                "bible_revision": state["bibles"][-1]["revision"],
                "timeline_revision": state["timelines"][-1]["revision"],
                "workflow_revision": workflow["revision"],
                "input_sha256": input_sha256,
                "external_prompt_id": None,
                "resolved_graph": graph,
                "resolved_graph_sha256": _hash(graph),
                "seed": shot["seed"],
                "state": "queued",
                "last_event_sequence": 0,
                "error_kind": None,
                "outputs": [],
                "created_at": _now(),
                "updated_at": _now(),
            }
            attempt_body["content_hash"] = _hash(attempt_body)
            attempt = AttemptReceipt(**attempt_body).model_dump()
            state["attempts"].append(attempt)
            self._event(state, "attempt:" + aid + ":queued", "attempt.queued", aid)
            return attempt

    def execute_attempt(
        self, pid: str, attempt_id: str, *, renderer: Callable[[bytes], list[bytes]]
    ):
        with self._locked_state(pid) as state:
            attempt = next(
                (a for a in state["attempts"] if a["attempt_id"] == attempt_id), None
            )
            if not attempt:
                raise FileNotFoundError("시도가 없습니다.")
            if attempt["state"] in {"review_pending", "failed", "cancelled"}:
                return attempt
            if state["cancelled"]:
                attempt.update(state="cancelled", updated_at=_now())
                self._seal_attempt(attempt)
                return attempt
            attempt["state"] = "running"
            attempt["updated_at"] = _now()
            self._seal_attempt(attempt)
            self._event(
                state,
                "attempt:" + attempt_id + ":running",
                "attempt.running",
                attempt_id,
            )
            input_sha = attempt["input_sha256"]
        try:
            output_data = renderer(self.read_artifact(pid, input_sha))
            if not output_data:
                raise EmptyOutputError("렌더러가 결과 파일을 반환하지 않았습니다.")
            with self._locked_state(pid) as state:
                current = next(
                    a for a in state["attempts"] if a["attempt_id"] == attempt_id
                )
                if current["state"] not in {"cancelled", "review_pending"}:
                    current.update(
                        state="collecting",
                        expected_output_count=len(output_data),
                        updated_at=_now(),
                    )
                    self._seal_attempt(current)
                    self._event(
                        state,
                        "attempt:" + attempt_id + ":collecting",
                        "attempt.collecting",
                        attempt_id,
                    )
            outputs = []
            for index, data in enumerate(output_data):
                if not data:
                    raise EmptyOutputError(
                        "빈 결과 파일은 성공으로 처리할 수 없습니다."
                    )
                record = self.put_artifact(
                    pid,
                    f"{attempt_id}-{index}.bin",
                    data,
                    media_type="application/octet-stream",
                )
                outputs.append(record)
                with self._locked_state(pid) as state:
                    current = next(
                        a for a in state["attempts"] if a["attempt_id"] == attempt_id
                    )
                    if current["state"] not in {"cancelled", "review_pending"}:
                        current["outputs"] = list(outputs)
                        current["updated_at"] = _now()
                        self._seal_attempt(current)
                        self._event(
                            state,
                            f"attempt:{attempt_id}:output:{index}",
                            "attempt.output_collected",
                            attempt_id,
                            output_sha256=record["sha256"],
                        )
            with self._locked_state(pid) as state:
                current = next(
                    a for a in state["attempts"] if a["attempt_id"] == attempt_id
                )
                if current["state"] == "cancelled" or state["cancelled"]:
                    return current
                current.update(
                    state="review_pending", outputs=outputs, updated_at=_now()
                )
                self._seal_attempt(current)
                self._event(
                    state,
                    "attempt:" + attempt_id + ":review_pending",
                    "attempt.review_pending",
                    attempt_id,
                )
                return current
        except Exception as exc:  # noqa: BLE001 - persist every renderer/collector failure.
            with self._locked_state(pid) as state:
                current = next(
                    a for a in state["attempts"] if a["attempt_id"] == attempt_id
                )
                if current["state"] not in {"cancelled", "review_pending"}:
                    current.update(
                        state="failed",
                        error_kind="empty_output"
                        if isinstance(exc, EmptyOutputError)
                        else "collect_failed",
                        updated_at=_now(),
                    )
                    self._seal_attempt(current)
                    self._event(
                        state,
                        "attempt:" + attempt_id + ":failed",
                        "attempt.failed",
                        attempt_id,
                        error_kind=current["error_kind"],
                    )
                return current

    def resume_pending(
        self, pid: str, *, renderer: Callable[[bytes], list[bytes]]
    ) -> list[dict]:
        state = self.get_production(pid)
        if state["cancelled"]:
            return []
        pending = [
            a["attempt_id"]
            for a in state["attempts"]
            if a["state"] in {"queued", "running", "collecting"}
        ]
        return [
            self.execute_attempt(pid, attempt_id, renderer=renderer)
            for attempt_id in pending
        ]

    def ingest_event(
        self, pid: str, attempt_id: str, *, event_id: str, sequence: int, state: str
    ):
        if state not in {
            "queued",
            "running",
            "collecting",
            "succeeded",
            "failed",
            "cancelled",
        }:
            raise ValueError("알 수 없는 실행 상태입니다.")
        with self._locked_state(pid) as data:
            attempt = next(
                (a for a in data["attempts"] if a["attempt_id"] == attempt_id), None
            )
            if not attempt:
                raise FileNotFoundError("시도가 없습니다.")
            if any(
                event["event_id"] == event_id and event["attempt_id"] == attempt_id
                for event in data["events"]
            ):
                return attempt
            if sequence <= attempt["last_event_sequence"]:
                self._event(
                    data,
                    event_id,
                    "engine.late_event_ignored",
                    attempt_id,
                    sequence=sequence,
                )
                return attempt
            self._event(
                data,
                event_id,
                "engine.event",
                attempt_id,
                sequence=sequence,
                received_state=state,
            )
            attempt["last_event_sequence"] = sequence
            if attempt["state"] in {"review_pending", "failed", "cancelled"}:
                self._seal_attempt(attempt)
                return attempt
            if state == "succeeded":
                if not attempt["outputs"]:
                    attempt.update(
                        state="failed", error_kind="empty_output", updated_at=_now()
                    )
                elif (
                    attempt["expected_output_count"] is not None
                    and len(attempt["outputs"]) != attempt["expected_output_count"]
                ):
                    attempt.update(
                        state="failed",
                        error_kind="incomplete_collection",
                        updated_at=_now(),
                    )
                else:
                    attempt.update(state="review_pending", updated_at=_now())
            elif state in {"failed", "cancelled"} or not data["cancelled"]:
                attempt.update(state=state, updated_at=_now())
            self._seal_attempt(attempt)
            return attempt

    def cancel_attempt(self, pid: str, attempt_id: str):
        with self._locked_state(pid) as state:
            attempt = next(
                (a for a in state["attempts"] if a["attempt_id"] == attempt_id), None
            )
            if not attempt:
                raise FileNotFoundError("시도가 없습니다.")
            state["cancelled"] = True
            for active in state["attempts"]:
                if active["state"] in {"queued", "running", "collecting"}:
                    active.update(state="cancelled", updated_at=_now())
                    self._seal_attempt(active)
                    self._event(
                        state,
                        "attempt:" + active["attempt_id"] + ":cancelled",
                        "attempt.cancelled",
                        active["attempt_id"],
                    )
            self._event(
                state, "production:cancelled", "production.cancelled", attempt_id
            )
            return attempt

    def create_review(
        self,
        pid: str,
        *,
        output_sha256: str,
        approval_event_id: str | None,
        criteria: dict | None = None,
    ):
        if approval_event_id is not None:
            raise ValueError(
                "연구용 검수는 승인 null만 허용하며 사용자 승인을 위조할 수 없습니다."
            )
        with self._locked_state(pid) as state:
            source_attempt = next(
                (
                    attempt
                    for attempt in state["attempts"]
                    if attempt["state"] == "review_pending"
                    and any(
                        output_sha256 == out["sha256"] for out in attempt["outputs"]
                    )
                ),
                None,
            )
            if source_attempt is None:
                raise ValueError(
                    "검수 대상 output hash가 이 제작의 실행 receipt에 없습니다."
                )
            review_body = {
                "schema_version": 1,
                "review_id": _id(),
                "output_sha256": output_sha256,
                "shot_revision": source_attempt["shot_revision"],
                "bible_revision": source_attempt["bible_revision"],
                "timeline_revision": source_attempt["timeline_revision"],
                "criteria": criteria or {},
                "evidence_uris": [],
                "reviewer": "cpu-check",
                "independent": False,
                "approval_event_id": None,
                "created_at": _now(),
            }
            review_body["content_hash"] = _hash(review_body)
            review = ReviewReceipt(**review_body).model_dump()
            state["reviews"].append(review)
            return review


class EmptyOutputError(ValueError):
    pass


def default_director_store() -> DirectorStore:
    from ttakkari.config import get_settings

    return DirectorStore(get_settings().home / "creative-studio" / "productions")
