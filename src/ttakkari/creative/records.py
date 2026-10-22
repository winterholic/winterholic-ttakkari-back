"""제작 기록(Production Records) 읽기 모델.

세 원천을 하나의 스키마로 정규화한다. 쓰기 경로는 없다.
- g0: G0 제작 제어부(`director.py`)의 production 상태 파일
- v1: v1 creative 프로젝트·렌더(`service.py`)
- lab: `<lab_root>/<영역>/records/<slug>/record.json` 매니페스트(실험 backfill)

미디어는 항상 원천 루트 아래에서만 읽는다. lab 매니페스트의 경로는 루트 기준 상대경로이며
심볼릭 링크를 해석한 뒤 루트 안인지 다시 검사한다. 값이 없으면 None 으로 두고 UI 가 '미측정'으로 표시한다.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
MEDIA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"

Source = Literal["g0", "v1", "lab"]
SOURCE_LABELS = {"g0": "G0 제작", "v1": "v1 프로젝트", "lab": "lab 실험"}
Kind = Literal[
    "production",
    "project",
    "motion_graphics",
    "music_video",
    "cinematic_3d",
    "character_video",
    "audio",
    "image",
    "video_gen",
    "benchmark",
    "pipeline",
    "design",
]
Status = Literal["completed", "partial", "failed", "in_progress", "draft"]
Verdict = Literal["pass", "conditional", "fail", "unreviewed"]
Scalar = str | int | float | bool | None

# 서빙 허용 확장자. 종류와 맞지 않는 확장자는 거부한다(HTML·SVG·스크립트는 서빙 대상이 아니다).
_EXT_BY_KIND = {
    "video": {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"},
    "audio": {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    },
    "image": {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    },
    "document": {
        ".md": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".json": "text/plain; charset=utf-8",
        ".jsonl": "text/plain; charset=utf-8",
        ".lrc": "text/plain; charset=utf-8",
        ".csv": "text/plain; charset=utf-8",
        ".tsv": "text/plain; charset=utf-8",
        ".log": "text/plain; charset=utf-8",
    },
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Media(_Model):
    id: str = Field(pattern=MEDIA_ID_PATTERN)
    kind: Literal["video", "audio", "image", "document"]
    role: Literal[
        "result",
        "before",
        "after",
        "compare",
        "keyframe",
        "contact_sheet",
        "audio",
        "lyrics",
        "evidence",
        "reference",
        "document",
    ]
    label: str = Field(max_length=120)
    # lab 매니페스트에서는 lab 루트 기준 상대경로. g0/v1 은 서버가 원천에서 직접 해석한다.
    path: str | None = Field(default=None, max_length=300)
    duration_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    caption: str = Field(default="", max_length=400)
    # 아래 두 필드는 서버가 항상 덮어쓴다(매니페스트 값은 무시).
    available: bool = True
    url: str | None = None


class Stage(_Model):
    key: str = Field(max_length=40)
    label: str = Field(max_length=60)
    status: Literal["done", "partial", "skipped", "failed", "not_run"]
    summary: str = Field(default="", max_length=800)
    seconds: float | None = Field(default=None, ge=0)
    details: list[str] = Field(default_factory=list, max_length=20)


class Shot(_Model):
    id: str = Field(max_length=80)
    label: str = Field(max_length=120)
    engine: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    prompt: str | None = Field(default=None, max_length=4000)
    seed: int | None = None
    seconds: float | None = Field(default=None, ge=0)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    status: Literal["ok", "failed", "flagged", "not_run"] = "ok"
    keyframe_media_id: str | None = None
    output_media_id: str | None = None
    params: dict[str, Scalar] = Field(default_factory=dict)
    note: str = Field(default="", max_length=600)


class Tool(_Model):
    name: str = Field(max_length=80)
    kind: Literal[
        "renderer",
        "engine",
        "model",
        "tts",
        "stt",
        "encoder",
        "library",
        "analysis",
        "audio",
    ]
    version: str | None = Field(default=None, max_length=60)
    role: str = Field(default="", max_length=200)
    license: str | None = Field(default=None, max_length=80)


class Metric(_Model):
    key: str = Field(max_length=60)
    label: str = Field(max_length=80)
    value: float | int | str | None = None
    unit: str | None = Field(default=None, max_length=20)
    source: str | None = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=300)


class Table(_Model):
    title: str = Field(max_length=100)
    columns: list[str] = Field(max_length=16)
    rows: list[list[Scalar]] = Field(max_length=200)
    source: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("표의 모든 행은 열 수와 같아야 합니다.")
        return self


class Cost(_Model):
    usd: float | None = Field(default=None, ge=0)
    paid_services: list[str] = Field(default_factory=list, max_length=10)
    note: str = Field(default="", max_length=400)


class Defect(_Model):
    id: str = Field(max_length=40)
    location: str = Field(default="", max_length=120)
    symptom: str = Field(max_length=400)
    fix: str | None = Field(default=None, max_length=400)
    status: Literal["fixed", "open", "wontfix"]


class Revision(_Model):
    label: str = Field(max_length=80)
    summary: str = Field(default="", max_length=600)
    defects: list[Defect] = Field(default_factory=list, max_length=40)
    media_ids: list[str] = Field(default_factory=list, max_length=20)


class Criterion(_Model):
    name: str = Field(max_length=120)
    result: Literal["pass", "fail", "unknown"]
    evidence: str = Field(default="", max_length=400)


class Review(_Model):
    verdict: Verdict = "unreviewed"
    reviewer: str = Field(default="", max_length=120)
    independent: bool = False
    summary: str = Field(default="", max_length=800)
    criteria: list[Criterion] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)


class RecordBody(_Model):
    """매니페스트와 API 상세가 공유하는 본문. 세 원천이 같은 모양으로 정규화된다."""

    schema_version: Literal[1] = 1
    slug: str | None = Field(default=None, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=160)
    kind: Kind
    summary: str = Field(default="", max_length=800)
    status: Status
    created_at: str
    updated_at: str | None = None
    brief: str = Field(default="", max_length=6000)
    style: str = Field(default="", max_length=1000)
    engines: list[str] = Field(default_factory=list, max_length=12)
    tags: list[str] = Field(default_factory=list, max_length=12)
    duration_seconds: float | None = Field(default=None, ge=0)
    thumbnail_media_id: str | None = None
    stages: list[Stage] = Field(default_factory=list, max_length=12)
    shots: list[Shot] = Field(default_factory=list, max_length=80)
    tools: list[Tool] = Field(default_factory=list, max_length=40)
    metrics: list[Metric] = Field(default_factory=list, max_length=60)
    tables: list[Table] = Field(default_factory=list, max_length=8)
    cost: Cost | None = None
    media: list[Media] = Field(default_factory=list, max_length=60)
    revisions: list[Revision] = Field(default_factory=list, max_length=20)
    review: Review = Field(default_factory=Review)

    @model_validator(mode="after")
    def references_and_dates(self):
        _parse_time(self.created_at)
        if self.updated_at:
            _parse_time(self.updated_at)
        ids = [m.id for m in self.media]
        if len(ids) != len(set(ids)):
            raise ValueError("미디어 id가 중복됩니다.")
        known = set(ids)
        refs = [self.thumbnail_media_id]
        for shot in self.shots:
            refs += [shot.keyframe_media_id, shot.output_media_id]
        for revision in self.revisions:
            refs += revision.media_ids
        missing = {r for r in refs if r and r not in known}
        if missing:
            raise ValueError(f"존재하지 않는 미디어 참조: {sorted(missing)}")
        return self


class ProductionRecord(RecordBody):
    id: str
    source: Source
    source_label: str
    warnings: list[str] = Field(default_factory=list)


class RecordSummary(_Model):
    id: str
    source: Source
    source_label: str
    title: str
    kind: Kind
    summary: str
    status: Status
    verdict: Verdict
    engines: list[str]
    tags: list[str]
    duration_seconds: float | None
    created_at: str
    shot_count: int
    media_count: int
    thumbnail_media_id: str | None
    thumbnail_url: str | None = None
    complete_media: bool = True


@dataclass(frozen=True)
class MediaFile:
    path: Path
    filename: str
    mime: str


class RecordNotFound(FileNotFoundError):
    pass


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"시각 형식이 잘못됐습니다: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round(max((_parse_time(end) - _parse_time(start)).total_seconds(), 0), 3)
    except ValueError:
        return None


def media_kind_for_mime(mime: str | None, name: str = "") -> str | None:
    mime = (mime or "").lower()
    for kind in ("video", "audio", "image"):
        if mime.startswith(kind + "/"):
            return kind
    suffix = Path(name).suffix.lower()
    for kind, exts in _EXT_BY_KIND.items():
        if suffix in exts and kind != "document":
            return kind
    return None


def safe_relative(root: Path, relative: str, kind: str) -> Path | None:
    """루트 기준 상대경로를 해석한다. 루트 밖·허용 외 확장자·존재하지 않는 파일이면 None."""
    if not relative or "\x00" in relative or "\\" in relative:
        return None
    parts = PurePosixPath(relative).parts
    if PurePosixPath(relative).is_absolute() or ".." in parts or not parts:
        return None
    if Path(relative).suffix.lower() not in _EXT_BY_KIND.get(kind, {}):
        return None
    try:
        base = root.resolve(strict=True)
        # 링크를 끝까지 따라간 뒤에 루트 안인지 다시 본다.
        resolved = (base / relative).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(base) or not resolved.is_file():
        return None
    return resolved


def mime_for(path: Path, kind: str) -> str:
    return _EXT_BY_KIND[kind].get(path.suffix.lower(), "application/octet-stream")


# --- lab ---------------------------------------------------------------------------------


class LabSource:
    def __init__(self, root: Path | None):
        self.root = Path(root).expanduser() if root else None

    @property
    def available(self) -> bool:
        return bool(self.root and self.root.is_dir())

    def _manifests(self) -> list[Path]:
        if not self.available:
            return []
        assert self.root is not None
        found = list(self.root.glob("records/*/record.json"))
        found += self.root.glob("*/records/*/record.json")
        return sorted(set(found))

    def load_all(self) -> tuple[list[ProductionRecord], list[dict]]:
        records, skipped = [], []
        seen: set[str] = set()
        for path in self._manifests():
            try:
                record = self._load(path)
            except (OSError, ValueError, ValidationError) as exc:
                skipped.append({"path": self._display(path), "reason": _short(exc)})
                continue
            if record.id in seen:
                skipped.append(
                    {"path": self._display(path), "reason": "slug가 중복됩니다."}
                )
                continue
            seen.add(record.id)
            records.append(record)
        return records, skipped

    def get(self, slug: str) -> ProductionRecord:
        if not re.fullmatch(SLUG_PATTERN, slug) or not self.available:
            raise RecordNotFound("제작 기록이 없습니다.")
        assert self.root is not None
        for path in self._manifests():
            if path.parent.name == slug:
                try:
                    return self._load(path)
                except (OSError, ValueError, ValidationError) as exc:
                    raise RecordNotFound("제작 기록을 읽을 수 없습니다.") from exc
        raise RecordNotFound("제작 기록이 없습니다.")

    def _display(self, path: Path) -> str:
        assert self.root is not None
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return path.name

    def _load(self, path: Path) -> ProductionRecord:
        assert self.root is not None
        base = self.root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        # 매니페스트 자체가 링크로 루트 밖을 가리키면 읽지 않는다.
        if not resolved.is_relative_to(base) or not resolved.is_file():
            raise ValueError("매니페스트가 lab 루트 밖에 있습니다.")
        if resolved.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError("매니페스트가 1MB를 넘습니다.")
        body = RecordBody.model_validate(json.loads(resolved.read_text("utf-8")))
        if body.slug != path.parent.name:
            raise ValueError("slug가 폴더 이름과 다릅니다.")
        warnings: list[str] = []
        media = []
        for item in body.media:
            file = safe_relative(base, item.path or "", item.kind)
            if file is None:
                warnings.append(f"파일 없음 또는 접근 불가: {item.id}")
            media.append(
                item.model_copy(update={"available": file is not None, "url": None})
            )
        return ProductionRecord(
            **{**body.model_dump(), "media": [m.model_dump() for m in media]},
            id=f"lab-{body.slug}",
            source="lab",
            source_label=SOURCE_LABELS["lab"],
            warnings=warnings,
        )

    def media_file(self, slug: str, media_id: str) -> MediaFile:
        record = self.get(slug)
        item = next((m for m in record.media if m.id == media_id), None)
        if item is None or self.root is None:
            raise RecordNotFound("미디어가 없습니다.")
        path = safe_relative(self.root, item.path or "", item.kind)
        if path is None:
            raise RecordNotFound("미디어가 없습니다.")
        return MediaFile(path, path.name, mime_for(path, item.kind))


def _short(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:300]


# --- g0 ----------------------------------------------------------------------------------

_ATTEMPT_ACTIVE = {"queued", "running", "collecting"}
_SHOT_STATUS = {
    "review_pending": "ok",
    "failed": "failed",
    "cancelled": "failed",
    "queued": "not_run",
    "running": "not_run",
    "collecting": "not_run",
}


def _first_line(text: str, limit: int = 70) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _g0_media_id(sha: str) -> str:
    return f"a-{sha[:24]}"


def _g0_verdict(reviews: list[dict]) -> tuple[Verdict, Review]:
    if not reviews:
        return "unreviewed", Review(
            summary="검수 영수증이 없습니다.",
            limitations=["승인 기록 없음(research-null-approval)."],
        )
    criteria: list[Criterion] = []
    for review in reviews:
        for name, result in (review.get("criteria") or {}).items():
            criteria.append(Criterion(name=name, result=result, evidence=""))
    results = {c.result for c in criteria}
    verdict: Verdict = "conditional"
    if "fail" in results:
        verdict = "fail"
    elif criteria and results == {"pass"}:
        verdict = "pass"
    latest = reviews[-1]
    limitations = []
    if not latest.get("independent"):
        limitations.append("독립 검수가 아닙니다(같은 제작 컨텍스트의 CPU 점검).")
    if latest.get("approval_event_id") is None:
        limitations.append("사용자 승인 기록 없음(approval=null, 연구용).")
    return verdict, Review(
        verdict=verdict,
        reviewer=str(latest.get("reviewer") or ""),
        independent=any(r.get("independent") for r in reviews),
        summary=f"검수 영수증 {len(reviews)}건",
        criteria=criteria[:30],
        limitations=limitations,
    )


def record_from_g0(state: dict) -> ProductionRecord:
    manifest = state["manifest"]
    pid = manifest["production_id"]
    timeline = state["timelines"][-1]
    bible = state["bibles"][-1]
    attempts = state.get("attempts", [])
    reviews = state.get("reviews", [])
    fps = timeline["fps_num"] / timeline["fps_den"]
    duration = round(timeline["duration_frames"] / fps, 3)

    # CAS 산출물은 같은 해시의 이름 있는 업로드(원본 미디어 종류)를 우선한다.
    by_sha: dict[str, dict] = {}
    for name, art in sorted(state.get("artifacts", {}).items()):
        current = by_sha.get(art["sha256"])
        if current is None or current["media_type"] == "application/octet-stream":
            by_sha[art["sha256"]] = {**art, "name": name}
    output_shas = {o["sha256"] for a in attempts for o in a.get("outputs", [])}
    media: list[Media] = []
    for sha, art in by_sha.items():
        kind = media_kind_for_mime(art["media_type"], art["name"])
        if kind is None:
            kind = "document"
        media.append(
            Media(
                id=_g0_media_id(sha),
                kind=kind,
                role="result" if sha in output_shas else "reference",
                label=art["name"][:120],
                caption=f"{art['media_type']} · {art['bytes']:,} bytes",
            )
        )

    shots: list[Shot] = []
    workflows = state.get("workflows", [])
    workflow = workflows[-1] if workflows else {}
    for shot_id, active in manifest["active_shot_revisions"].items():
        revisions = state["shots"].get(shot_id, [])
        shot = next((s for s in revisions if s["revision"] == active), None)
        if shot is None:
            continue
        shot_attempts = [a for a in attempts if a["shot_id"] == shot_id]
        latest = shot_attempts[-1] if shot_attempts else None
        out_sha = (
            latest["outputs"][0]["sha256"] if latest and latest["outputs"] else None
        )
        shots.append(
            Shot(
                id=shot_id,
                label=f"{shot_id} (r{shot['revision']})",
                engine=shot["engine"],
                model=workflow.get("preset"),
                prompt=shot["intent"] or None,
                seed=(latest or {}).get("seed", shot.get("seed")),
                seconds=_seconds_between(
                    (latest or {}).get("created_at"), (latest or {}).get("updated_at")
                ),
                start_seconds=round(shot["start_f"] / fps, 3),
                end_seconds=round(shot["end_f"] / fps, 3),
                status=_SHOT_STATUS.get(latest["state"], "not_run")
                if latest
                else "not_run",
                output_media_id=_g0_media_id(out_sha) if out_sha else None,
                params={
                    "profile": shot["profile"],
                    "frames": f"{shot['start_f']}-{shot['end_f']}",
                    "attempts": len(shot_attempts),
                },
            )
        )

    revisions_out: list[Revision] = []
    for shot_id, revs in state["shots"].items():
        if len(revs) > 1:
            revisions_out.append(
                Revision(
                    label=f"{shot_id} r1→r{revs[-1]['revision']}",
                    summary=f"샷 개정 {len(revs)}회, 시드·의도·입력 해시 변경 이력",
                )
            )
    failed = [a for a in attempts if a["state"] in ("failed", "cancelled")]
    if failed:
        revisions_out.append(
            Revision(
                label="실패·취소 시도",
                summary=f"{len(failed)}건의 시도가 끝까지 가지 못했습니다.",
                defects=[
                    Defect(
                        id=a["attempt_id"][:8],
                        location=a["shot_id"],
                        symptom=(a.get("error_kind") or a["state"])
                        + (
                            ": " + a["error_details"][0]
                            if a.get("error_details")
                            else ""
                        ),
                        status="open",
                    )
                    for a in failed[:40]
                ],
            )
        )

    covered = {a["shot_id"] for a in attempts if a["state"] == "review_pending"}
    all_shots = set(manifest["active_shot_revisions"])
    if state.get("cancelled"):
        status: Status = "failed"
    elif not attempts:
        status = "draft"
    elif any(a["state"] in _ATTEMPT_ACTIVE for a in attempts):
        status = "in_progress"
    elif not covered:
        status = "failed"
    elif covered >= all_shots and reviews:
        status = "completed"
    else:
        status = "partial"

    _verdict, review = _g0_verdict(reviews)
    engines = sorted({s.engine for s in shots if s.engine})
    tools = [
        Tool(name=e, kind="engine", role="샷 실행 엔진(G0 어댑터)") for e in engines
    ]
    for key, digest in (workflow.get("model_hashes") or {}).items():
        tools.append(
            Tool(name=key, kind="model", version=str(digest)[:12], role="모델 해시")
        )
    attempt_seconds = [
        s
        for s in (_seconds_between(a["created_at"], a["updated_at"]) for a in attempts)
        if s is not None
    ]
    metrics = [
        Metric(
            key="duration",
            label="타임라인 길이",
            value=duration,
            unit="초",
            source="timeline",
        ),
        Metric(key="shots", label="샷 수", value=len(all_shots), source="manifest"),
        Metric(key="attempts", label="시도 수", value=len(attempts), source="attempts"),
        Metric(
            key="attempt_seconds_total",
            label="시도 소요 합계",
            value=round(sum(attempt_seconds), 3) if attempt_seconds else None,
            unit="초",
            source="attempt.created_at→updated_at",
        ),
        Metric(
            key="peak_memory",
            label="최대 메모리",
            value=None,
            note="G0 receipt는 메모리를 기록하지 않습니다.",
        ),
    ]
    stages = [
        Stage(
            key="brief",
            label="브리프",
            status="done",
            summary=_first_line(manifest["brief_ref"], 400),
        ),
        Stage(
            key="plan",
            label="기획·스타일",
            status="done",
            summary=f"스타일 바이블 r{bible['revision']}, 팔레트 {len(bible['palette'])}색, 금지 규칙 {len(bible['negative_rules'])}개",
        ),
        Stage(
            key="timeline",
            label="오디오·타임라인",
            status="done",
            summary=f"{timeline['duration_frames']}프레임 @ {timeline['fps_num']}/{timeline['fps_den']}fps, 샷 {len(timeline['shots'])}개, 비트 {len(timeline['beats'])}, 큐 {len(timeline['cues'])}",
        ),
        Stage(
            key="shots",
            label="샷 정의",
            status="done" if shots else "not_run",
            summary=", ".join(f"{s.id}:{s.engine}" for s in shots)[:400],
        ),
        Stage(
            key="render",
            label="렌더(시도)",
            status="not_run"
            if not attempts
            else ("done" if status == "completed" else "partial"),
            summary=f"시도 {len(attempts)}건, 검수 대기 {len([a for a in attempts if a['state'] == 'review_pending'])}건, 실패·취소 {len(failed)}건",
        ),
        Stage(
            key="review",
            label="검수",
            status="done" if reviews else "not_run",
            summary=review.summary,
        ),
        Stage(
            key="revision",
            label="수정·재렌더",
            status="done" if revisions_out else "skipped",
            summary=f"기록된 수정 항목 {len(revisions_out)}개",
        ),
    ]
    thumb = next((m.id for m in media if m.kind == "image"), None)
    return ProductionRecord(
        id=f"g0-{pid}",
        source="g0",
        source_label=SOURCE_LABELS["g0"],
        title=_first_line(manifest["brief_ref"]) or "제목 없는 제작",
        kind="production",
        summary=f"G0 제작 제어부 기록. 정책 {manifest['policy_ref']}.",
        status=status,
        created_at=manifest["created_at"],
        updated_at=manifest["updated_at"],
        brief=manifest["brief_ref"][:6000],
        engines=engines,
        tags=[manifest["policy_ref"]],
        duration_seconds=duration,
        thumbnail_media_id=thumb,
        stages=stages,
        shots=shots,
        tools=tools,
        metrics=metrics,
        cost=Cost(
            usd=0.0, note="G0 CPU/fake 어댑터는 외부 유료 서비스를 호출하지 않습니다."
        )
        if engines and all(e.startswith("cpu") for e in engines)
        else None,
        media=media[:60],
        revisions=revisions_out,
        review=review,
    )


# --- v1 ----------------------------------------------------------------------------------

_V1_KINDS = {"music_video": "music_video"}
_V1_STATUS = {
    "succeeded": "completed",
    "failed": "failed",
    "queued": "in_progress",
    "running": "in_progress",
}


def record_from_v1(project: dict) -> ProductionRecord:
    jobs = project.get("render_jobs", [])
    latest = jobs[-1] if jobs else None
    done = [j for j in jobs if j["status"] == "succeeded" and j.get("output")]
    status: Status = _V1_STATUS.get(latest["status"], "draft") if latest else "draft"
    media: list[Media] = []
    for job in done[-3:]:
        media.append(
            Media(
                id=job["id"],
                kind="video",
                role="result",
                label=f"렌더 {job['id'][:8]}",
                duration_seconds=project.get("duration"),
                caption=f"{job.get('elapsed_seconds')}초 소요",
            )
        )
    for asset in project.get("assets", [])[:40]:
        if asset["kind"] not in ("image", "audio", "video"):
            continue
        media.append(
            Media(
                id=asset["id"],
                kind=asset["kind"],
                role="keyframe"
                if asset["kind"] == "image"
                else "audio"
                if asset["kind"] == "audio"
                else "reference",
                label=asset["name"][:120],
                width=(asset.get("metadata") or {}).get("width"),
                height=(asset.get("metadata") or {}).get("height"),
                duration_seconds=(asset.get("metadata") or {}).get("duration"),
                caption=asset.get("provenance", ""),
            )
        )
    scene_assets = {a["id"]: a for a in project.get("assets", [])}
    shots = []
    for index, scene in enumerate(project.get("scenes", [])[:80], start=1):
        asset = scene_assets.get(scene.get("asset_id"))
        shots.append(
            Shot(
                id=scene["id"][:80],
                label=f"장면 {index}",
                engine="ffmpeg",
                model=scene.get("template_id"),
                prompt=scene.get("prompt") or scene.get("caption") or None,
                seconds=None,
                output_media_id=None,
                keyframe_media_id=asset["id"]
                if asset and asset["kind"] == "image"
                else None,
                status="ok" if asset else "not_run",
                params={
                    "duration": scene["duration"],
                    "animation": scene["animation"],
                    "transition": scene["transition"],
                    "caption_style": scene.get("caption_style"),
                },
                note=scene.get("caption", ""),
            )
        )
    timed = [j for j in done if j.get("elapsed_seconds") is not None]
    last = timed[-1] if timed else None
    rss = last.get("peak_rss_bytes") if last else None
    metrics = [
        Metric(
            key="duration",
            label="프로젝트 길이",
            value=project.get("duration"),
            unit="초",
            source="project.json",
        ),
        Metric(
            key="render_seconds",
            label="최근 성공 렌더 소요",
            value=last["elapsed_seconds"] if last else None,
            unit="초",
            source="render_jobs.elapsed_seconds",
        ),
        Metric(
            key="peak_rss",
            label="렌더 최대 메모리(RSS)",
            value=round(rss / 1024 / 1024, 1) if rss else None,
            unit="MB",
            source="render_jobs.peak_rss_bytes",
            note="" if rss else "이 환경에서 측정되지 않았습니다.",
        ),
        Metric(
            key="render_jobs", label="렌더 시도", value=len(jobs), source="render_jobs"
        ),
    ]
    revisions = [
        Revision(
            label=f"렌더 {i}",
            summary=f"{job['status']}, 진행 {job.get('progress', 0)}%, 캐시된 장면 {job.get('cached_scenes', 0)}개",
            defects=[
                Defect(id=job["id"][:8], symptom=str(job["error"])[:400], status="open")
            ]
            if job.get("error")
            else [],
        )
        for i, job in enumerate(jobs[-20:], start=max(len(jobs) - 19, 1))
    ]
    fmt = project.get("content_type") or "project"
    kind: Kind = _V1_KINDS.get(fmt, "project")  # type: ignore[assignment]
    thumb = next((m.id for m in media if m.kind == "image"), None)
    return ProductionRecord(
        id=f"v1-{project['id']}",
        source="v1",
        source_label=SOURCE_LABELS["v1"],
        title=project["name"],
        kind=kind,
        summary=f"포맷 {fmt}, 장면 {len(shots)}개, {project.get('aspect', '')}",
        status=status,
        created_at=project["created_at"],
        updated_at=project.get("updated_at"),
        brief=project.get("brief", ""),
        style=project.get("style", ""),
        engines=["ffmpeg"],
        tags=[fmt],
        duration_seconds=project.get("duration"),
        thumbnail_media_id=thumb,
        stages=[
            Stage(
                key="brief",
                label="브리프",
                status="done" if project.get("brief") else "skipped",
                summary=_first_line(project.get("brief", ""), 400),
            ),
            Stage(
                key="plan",
                label="기획·스타일",
                status="done",
                summary=f"{fmt} · {project.get('style', '')}"[:400],
            ),
            Stage(
                key="timeline",
                label="장면·타임라인",
                status="done",
                summary=f"장면 {len(shots)}개, 총 {project.get('duration')}초",
            ),
            Stage(
                key="shots",
                label="자산 연결",
                status="done"
                if all(s.status == "ok" for s in shots) and shots
                else "partial",
                summary=f"자산 {len(project.get('assets', []))}개",
            ),
            Stage(
                key="render",
                label="렌더",
                status="done"
                if done
                else "failed"
                if status == "failed"
                else "not_run",
                summary=f"시도 {len(jobs)}건, 성공 {len(done)}건",
                seconds=last["elapsed_seconds"] if last else None,
            ),
            Stage(
                key="review",
                label="검수",
                status="not_run",
                summary="v1 프로젝트에는 검수 기록이 없습니다.",
            ),
            Stage(
                key="revision",
                label="수정·재렌더",
                status="done" if len(jobs) > 1 else "skipped",
                summary=f"렌더 {len(jobs)}회",
            ),
        ],
        shots=shots,
        tools=[
            Tool(name="FFmpeg", kind="renderer", role="장면 클립 인코딩과 최종 mux"),
            Tool(name="libx264", kind="encoder", role="H.264 인코딩"),
        ],
        metrics=metrics,
        cost=Cost(usd=0.0, note="로컬 FFmpeg 렌더. 외부 유료 서비스 없음."),
        media=media[:60],
        revisions=revisions,
        review=Review(
            summary="검수 기록 없음",
            limitations=["v1 프로젝트는 검수 영수증을 만들지 않습니다."],
        ),
    )


# --- service -----------------------------------------------------------------------------


def summarize(record: ProductionRecord) -> RecordSummary:
    thumb = record.thumbnail_media_id
    if thumb not in {m.id for m in record.media if m.available}:
        thumb = None
    if thumb is None:
        pick = next(
            (
                m
                for role in ("contact_sheet", "keyframe", "compare", "before", "after")
                for m in record.media
                if m.kind == "image" and m.role == role and m.available
            ),
            None,
        )
        thumb = pick.id if pick else None
    return RecordSummary(
        id=record.id,
        source=record.source,
        source_label=record.source_label,
        title=record.title,
        kind=record.kind,
        summary=record.summary,
        status=record.status,
        verdict=record.review.verdict,
        engines=record.engines,
        tags=record.tags,
        duration_seconds=record.duration_seconds,
        created_at=record.created_at,
        shot_count=len(record.shots),
        media_count=len(record.media),
        thumbnail_media_id=thumb,
        complete_media=all(m.available for m in record.media),
    )


class RecordsService:
    def __init__(self, *, director, studio, lab_root: Path | None):
        self.director = director
        self.studio = studio
        self.lab = LabSource(lab_root)

    # g0
    def _g0_all(self) -> tuple[list[ProductionRecord], list[dict]]:
        records, skipped = [], []
        for folder in sorted(self.director.root.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                state = self.director.get_production(folder.name)
                records.append(record_from_g0(state))
            except (
                OSError,
                ValueError,
                KeyError,
                ValidationError,
                json.JSONDecodeError,
            ) as exc:
                skipped.append(
                    {"path": f"productions/{folder.name}", "reason": _short(exc)}
                )
        return records, skipped

    def _v1_all(self) -> tuple[list[ProductionRecord], list[dict]]:
        records, skipped = [], []
        for project in self.studio.list():
            try:
                records.append(record_from_v1(project))
            except (KeyError, ValueError, ValidationError) as exc:
                skipped.append(
                    {"path": f"projects/{project.get('id')}", "reason": _short(exc)}
                )
        return records, skipped

    def list(self, *, source=None, kind=None, engine=None, q=None, limit=100) -> dict:
        g0, g0_skipped = self._g0_all()
        v1, v1_skipped = self._v1_all()
        lab, lab_skipped = self.lab.load_all()
        everything = [*g0, *v1, *lab]
        everything.sort(key=lambda r: _parse_time(r.created_at), reverse=True)

        def keep(record: ProductionRecord) -> bool:
            if source and record.source != source:
                return False
            if kind and record.kind != kind:
                return False
            if engine and engine.lower() not in {e.lower() for e in record.engines}:
                return False
            if q:
                hay = " ".join(
                    [record.title, record.summary, *record.tags, *record.engines]
                ).lower()
                return q.lower() in hay
            return True

        selected = [r for r in everything if keep(r)]
        return {
            "items": [summarize(r) for r in selected[:limit]],
            "total": len(selected),
            "counts": {
                "g0": len(g0),
                "v1": len(v1),
                "lab": len(lab),
            },
            "kinds": sorted({r.kind for r in everything}),
            "engines": sorted({e for r in everything for e in r.engines}),
            "lab": {
                "configured": self.lab.root is not None,
                "available": self.lab.available,
                "skipped": lab_skipped,
            },
            "skipped": [*g0_skipped, *v1_skipped],
        }

    def get(self, record_id: str) -> ProductionRecord:
        source, _, key = record_id.partition("-")
        if source == "lab":
            return self.lab.get(key)
        try:
            canonical = str(uuid.UUID(key))
        except ValueError as exc:
            raise RecordNotFound("제작 기록이 없습니다.") from exc
        if source == "g0":
            return record_from_g0(self.director.get_production(canonical))
        if source == "v1":
            return record_from_v1(self.studio.get(canonical))
        raise RecordNotFound("제작 기록이 없습니다.")

    def media_file(self, record_id: str, media_id: str) -> MediaFile:
        source, _, key = record_id.partition("-")
        if source == "lab":
            return self.lab.media_file(key, media_id)
        try:
            canonical = str(uuid.UUID(key))
        except ValueError as exc:
            raise RecordNotFound("미디어가 없습니다.") from exc
        if source == "g0":
            return self._g0_file(canonical, media_id)
        if source == "v1":
            return self._v1_file(canonical, media_id)
        raise RecordNotFound("미디어가 없습니다.")

    def _g0_file(self, pid: str, media_id: str) -> MediaFile:
        state = self.director.get_production(pid)
        for name, art in state.get("artifacts", {}).items():
            if _g0_media_id(art["sha256"]) != media_id:
                continue
            folder = self.director.production_path(pid) / "artifacts"
            target = folder / art["sha256"]
            if folder.is_symlink() or target.is_symlink():
                raise RecordNotFound("미디어가 없습니다.")
            try:
                resolved = target.resolve(strict=True)
                if (
                    resolved.parent != folder.resolve(strict=True)
                    or not resolved.is_file()
                ):
                    raise RecordNotFound("미디어가 없습니다.")
            except OSError as exc:
                raise RecordNotFound("미디어가 없습니다.") from exc
            kind = media_kind_for_mime(art["media_type"], name) or "document"
            mime = art["media_type"]
            if kind == "document" or mime == "application/octet-stream":
                mime = "application/octet-stream"
            return MediaFile(resolved, name, mime)
        raise RecordNotFound("미디어가 없습니다.")

    def _v1_file(self, pid: str, media_id: str) -> MediaFile:
        project = self.studio.get(pid)
        folder = self.studio.path(pid)
        candidates = [
            (a["file"], a["name"], a["kind"])
            for a in project["assets"]
            if a["id"] == media_id
        ] + [
            (j["output"], project["name"] + ".mp4", "video")
            for j in project["render_jobs"]
            if j["id"] == media_id and j["status"] == "succeeded" and j.get("output")
        ]
        if not candidates:
            raise RecordNotFound("미디어가 없습니다.")
        relative, name, kind = candidates[0]
        try:
            base = folder.resolve(strict=True)
            resolved = (base / relative).resolve(strict=True)
        except OSError as exc:
            raise RecordNotFound("미디어가 없습니다.") from exc
        if not resolved.is_relative_to(base) or not resolved.is_file():
            raise RecordNotFound("미디어가 없습니다.")
        exts = _EXT_BY_KIND.get(kind, {})
        mime = exts.get(resolved.suffix.lower())
        if mime is None:
            raise RecordNotFound("미디어가 없습니다.")
        return MediaFile(resolved, name, mime)
