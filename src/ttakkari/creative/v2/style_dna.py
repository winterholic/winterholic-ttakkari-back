"""Style DNA: 사용자 콘텐츠 15필드와 시스템 version, 참조·상속·명시적 충돌 해결.

규칙 하나가 전부를 지배한다. 서로 다른 출처의 값이 부딪치면 조용히 덮어쓰지 않고 충돌로 올린다.
충돌은 근거와 결정자를 남긴 해결(Resolution)이 있어야만 닫힌다. assistant가 정한 해결은 사용자 확인 전까지 pending으로 남는다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ttakkari.creative.v2.capabilities import Strict

USER_CONTENT_FIELDS: tuple[str, ...] = (
    "style_id",
    "visual_identity",
    "palette",
    "character_proportions",
    "materials",
    "lighting",
    "camera_language",
    "motion_language",
    "environment_rules",
    "sound_identity",
    "reference_assets",
    "positive_examples",
    "negative_examples",
    "quality_constraints",
    "compatible_skills",
)
SYSTEM_FIELDS: tuple[str, ...] = ("version",)
REQUIRED_FIELDS: tuple[str, ...] = (*USER_CONTENT_FIELDS, *SYSTEM_FIELDS)
FACET_FIELDS = (
    "visual_identity",
    "character_proportions",
    "materials",
    "lighting",
    "camera_language",
    "motion_language",
    "environment_rules",
    "sound_identity",
)
MERGE_FIELDS = (
    *FACET_FIELDS,
    "palette",
    "reference_assets",
    "positive_examples",
    "negative_examples",
    "quality_constraints",
    "compatible_skills",
)
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
SEMVER = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class StyleError(ValueError):
    pass


class UnresolvedStyleConflict(StyleError):
    def __init__(self, conflicts: list[Conflict]):
        self.conflicts = conflicts
        super().__init__(
            f"해결되지 않은 스타일 충돌 {len(conflicts)}건: "
            + ", ".join(c.label for c in conflicts)
        )


Param = float | str | bool


class Facet(Strict):
    text: str = Field(min_length=1, max_length=1000)
    params: dict[str, Param] = Field(default_factory=dict, max_length=30)


class PaletteColor(Strict):
    hex: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    role: str = Field(min_length=1, max_length=40)


class ReferenceAsset(Strict):
    kind: Literal["image", "video", "code", "render_settings", "outcome"]
    ref: str = Field(min_length=1, max_length=200)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    language: str | None = Field(default=None, max_length=30)
    params: dict[str, Param] = Field(default_factory=dict, max_length=40)
    polarity: Literal["success", "failure"] | None = None
    note: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _by_kind(self):
        if not SAFE_REF.match(self.ref) or ".." in self.ref or "//" in self.ref:
            raise ValueError("참조 경로는 안전한 상대 경로나 기록 slug여야 합니다.")
        if self.kind in ("image", "video", "code") and not self.sha256:
            raise ValueError(f"{self.kind} 참조는 sha256으로 내용을 고정해야 합니다.")
        if self.kind == "code" and not self.language:
            raise ValueError("코드 참조는 language가 필요합니다.")
        if self.kind == "render_settings" and not self.params:
            raise ValueError("렌더 설정 참조는 params가 필요합니다.")
        if self.kind == "outcome" and not self.polarity:
            raise ValueError("성공/실패 사례 참조는 polarity가 필요합니다.")
        return self


class Example(Strict):
    text: str = Field(min_length=1, max_length=400)
    ref: str | None = Field(default=None, max_length=200)


class QualityConstraint(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,59}$")
    text: str = Field(min_length=1, max_length=400)
    hard: bool = False
    params: dict[str, Param] = Field(default_factory=dict, max_length=20)


class StyleRef(Strict):
    style_id: str
    version: str

    @field_validator("style_id")
    @classmethod
    def _slug(cls, v):
        if not SLUG.match(v):
            raise ValueError("style_id 형식이 잘못됐습니다.")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not SEMVER.match(v):
            raise ValueError("version은 x.y.z 형식이어야 합니다.")
        return v

    def __str__(self) -> str:
        return f"{self.style_id}@{self.version}"


class Override(Strict):
    field: str
    reason: str = Field(min_length=5, max_length=400)


class StyleDNA(Strict):
    """완성된 Style DNA. 15개 콘텐츠 필드와 시스템 version이 필요하다."""

    style_id: str
    version: str
    visual_identity: Facet
    palette: list[PaletteColor] = Field(min_length=1, max_length=24)
    character_proportions: Facet
    materials: Facet
    lighting: Facet
    camera_language: Facet
    motion_language: Facet
    environment_rules: Facet
    sound_identity: Facet
    reference_assets: list[ReferenceAsset] = Field(max_length=60)
    positive_examples: list[Example] = Field(max_length=40)
    negative_examples: list[Example] = Field(max_length=40)
    quality_constraints: list[QualityConstraint] = Field(min_length=1, max_length=40)
    compatible_skills: list[str] = Field(max_length=40)

    @field_validator("style_id")
    @classmethod
    def _slug(cls, v):
        if not SLUG.match(v):
            raise ValueError("style_id 형식이 잘못됐습니다.")
        return v

    @field_validator("version")
    @classmethod
    def _semver(cls, v):
        if not SEMVER.match(v):
            raise ValueError("version은 x.y.z 형식이어야 합니다.")
        return v

    @property
    def maturity(self) -> str:
        return (
            "grounded"
            if (
                self.reference_assets
                or self.positive_examples
                or self.negative_examples
            )
            else "draft"
        )


class StyleDNADoc(Strict):
    """저장 단위. 부모가 있으면 바꾸는 필드만 적고, 부모와 겹치는 필드는 overrides에 사유를 적어야 한다."""

    style_id: str
    version: str
    parent: StyleRef | None = None
    overrides: list[Override] = Field(default_factory=list, max_length=20)
    visual_identity: Facet | None = None
    palette: list[PaletteColor] | None = None
    character_proportions: Facet | None = None
    materials: Facet | None = None
    lighting: Facet | None = None
    camera_language: Facet | None = None
    motion_language: Facet | None = None
    environment_rules: Facet | None = None
    sound_identity: Facet | None = None
    reference_assets: list[ReferenceAsset] | None = None
    positive_examples: list[Example] | None = None
    negative_examples: list[Example] | None = None
    quality_constraints: list[QualityConstraint] | None = None
    compatible_skills: list[str] | None = None
    note: str = Field(default="", max_length=400)

    def own_fields(self) -> list[str]:
        return [f for f in MERGE_FIELDS if getattr(self, f) is not None]


def _encode(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"직렬화할 수 없는 값: {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=_encode, allow_nan=False
    )


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Resolution(Strict):
    field: str
    key: str | None = Field(default=None, max_length=120)
    decision: Literal["take", "custom"]
    winner: str | None = Field(default=None, max_length=100)
    custom: Any = None
    rationale: str = Field(min_length=10, max_length=600)
    decided_by: Literal["user", "assistant"]
    confirmed_by_user: bool = False

    @model_validator(mode="after")
    def _shape(self):
        if self.decision == "take" and not self.winner:
            raise ValueError("take 해결은 winner(style_id@version)가 필요합니다.")
        if self.decision == "custom" and self.custom is None:
            raise ValueError("custom 해결은 custom 값이 필요합니다.")
        if self.decided_by == "user":
            self.confirmed_by_user = True
        return self


class Candidate(Strict):
    source: str
    value: Any


class Conflict(Strict):
    field: str
    key: str | None
    candidates: list[Candidate]

    @property
    def label(self) -> str:
        return self.field + (f"[{self.key}]" if self.key else "")


class Provenance(Strict):
    field: str
    sources: list[str]
    via: Literal["claim", "inherited_default", "resolution", "merged"]


class ComposeResult(Strict):
    status: Literal["resolved", "resolved_pending_confirmation", "unresolved"]
    conflicts: list[Conflict]
    resolutions_applied: list[Resolution]
    dna: StyleDNA | None
    provenance: list[Provenance]
    warnings: list[str]
    sha256: str | None
    inputs: list[str]


def _facet_equal(a: Facet, b: Facet) -> bool:
    return canonical(a) == canonical(b)


class StyleStore:
    """버전이 불변인 파일 저장소. 같은 (style_id, version)에 다른 내용을 쓰면 거부한다."""

    def __init__(self, root: Path, seeds: list[StyleDNADoc] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._seeds: dict[tuple[str, str], StyleDNADoc] = {
            (d.style_id, d.version): d for d in (seeds or [])
        }

    # --- 저장 ---
    def _path(self, ref: StyleRef) -> Path:
        return self.root / "styles" / ref.style_id / f"{ref.version}.json"

    def _read(self, ref: StyleRef) -> StyleDNADoc:
        seed = self._seeds.get((ref.style_id, ref.version))
        if seed:
            return seed
        path = self._path(ref)
        if not path.is_file():
            raise FileNotFoundError(f"스타일이 없습니다: {ref}")
        return StyleDNADoc.model_validate_json(path.read_text())

    def versions(self, style_id: str) -> list[str]:
        found = {v for (s, v) in self._seeds if s == style_id}
        folder = self.root / "styles" / style_id
        if folder.is_dir():
            found |= {p.stem for p in folder.glob("*.json")}
        return sorted(found, key=lambda v: tuple(int(x) for x in v.split(".")))

    def list(self) -> list[dict[str, Any]]:
        ids = {s for (s, _) in self._seeds}
        base = self.root / "styles"
        if base.is_dir():
            ids |= {p.name for p in base.iterdir() if p.is_dir()}
        return [
            {
                "style_id": sid,
                "versions": self.versions(sid),
                "seed": any(s == sid for (s, _) in self._seeds),
            }
            for sid in sorted(ids)
        ]

    def resolve_ref(self, style_id: str, version: str = "latest") -> StyleRef:
        if version == "latest":
            versions = self.versions(style_id)
            if not versions:
                raise FileNotFoundError(f"스타일이 없습니다: {style_id}")
            version = versions[-1]
        return StyleRef(style_id=style_id, version=version)

    def put(self, doc: StyleDNADoc) -> StyleDNADoc:
        ref = StyleRef(style_id=doc.style_id, version=doc.version)
        problems = self.validate_doc(doc)
        if problems:
            raise StyleError("; ".join(problems))
        if (ref.style_id, ref.version) in self._seeds:
            raise StyleError("내장 시드 버전은 바꿀 수 없습니다. 새 버전으로 만드세요.")
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = doc.model_dump_json(indent=2) + "\n"
        with (path.parent / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if path.exists():
                    if path.read_text() == payload:
                        return doc
                    raise StyleError(
                        f"{ref}은 이미 있고 내용이 다릅니다. 버전은 불변이므로 새 버전을 만드세요."
                    )
                temp = path.parent / f"{uuid.uuid4()}.tmp"
                with temp.open("w") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                temp.replace(path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return doc

    def validate_doc(self, doc: StyleDNADoc) -> list[str]:
        problems: list[str] = []
        if doc.parent is None:
            missing = [f for f in MERGE_FIELDS if getattr(doc, f) is None]
            if missing:
                problems.append(
                    "부모가 없으면 16필드를 모두 채워야 합니다. 빠진 필드: "
                    + ", ".join(missing)
                )
            if doc.overrides:
                problems.append("부모가 없는 스타일에는 overrides를 둘 수 없습니다.")
            if not problems:
                try:
                    self._complete(doc, {f: getattr(doc, f) for f in MERGE_FIELDS})
                except (ValidationError, StyleError) as e:
                    problems.append(str(e))
            return problems
        try:
            parent = self.effective(doc.parent)
        except FileNotFoundError as e:
            return [str(e)]
        except StyleError as e:
            return [f"부모를 해석할 수 없습니다: {e}"]
        declared = {o.field for o in doc.overrides}
        unknown = declared - set(MERGE_FIELDS)
        if unknown:
            problems.append("알 수 없는 override 필드: " + ", ".join(sorted(unknown)))
        for field in doc.own_fields():
            if (
                canonical(getattr(doc, field)) != canonical(getattr(parent, field))
                and field not in declared
            ):
                problems.append(
                    f"{field}가 부모 {doc.parent}와 다른데 overrides에 사유가 없습니다(묵시적 덮어쓰기 금지)."
                )
        for field in declared - set(doc.own_fields()):
            if field in MERGE_FIELDS:
                problems.append(f"{field}를 override한다고 했지만 값이 없습니다.")
        return problems

    # --- 해석 ---
    @staticmethod
    def _complete(doc: StyleDNADoc, values: dict[str, Any]) -> StyleDNA:
        return StyleDNA.model_validate(
            {"style_id": doc.style_id, "version": doc.version, **values}
        )

    def _lineage(self, ref: StyleRef, _seen: tuple[str, ...] = ()) -> list[StyleDNADoc]:
        if str(ref) in _seen:
            raise StyleError("스타일 상속이 순환합니다.")
        doc = self._read(ref)
        if doc.parent is None:
            return [doc]
        return [*self._lineage(doc.parent, (*_seen, str(ref))), doc]

    def effective(self, ref: StyleRef) -> StyleDNA:
        """조상부터 덮어 쓴 완성 DNA. 덮어쓰기는 저장 시 이미 override로 승인된 것만 존재한다."""
        values: dict[str, Any] = {}
        for doc in self._lineage(ref):
            for field in doc.own_fields():
                values[field] = getattr(doc, field)
        leaf = self._read(ref)
        return self._complete(leaf, values)

    def provenance_of(self, ref: StyleRef) -> dict[str, str]:
        owner: dict[str, str] = {}
        for doc in self._lineage(ref):
            for field in doc.own_fields():
                owner[field] = f"{doc.style_id}@{doc.version}"
        return owner

    # --- 합성 ---
    def compose(
        self, inputs: list[StyleRef], resolutions: list[Resolution] | None = None
    ) -> ComposeResult:
        if not inputs:
            raise StyleError("합성할 스타일이 없습니다.")
        if len({str(i) for i in inputs}) != len(inputs):
            raise StyleError("같은 스타일을 두 번 넣을 수 없습니다.")
        resolutions = resolutions or []
        effective = {str(r): self.effective(r) for r in inputs}
        claims: dict[str, list[tuple[str, Any]]] = {f: [] for f in MERGE_FIELDS}
        for ref in inputs:
            leaf = self._read(ref)
            own = MERGE_FIELDS if leaf.parent is None else leaf.own_fields()
            for field in own:
                claims[field].append((str(ref), getattr(effective[str(ref)], field)))
        conflicts: list[Conflict] = []
        merged: dict[str, Any] = {}
        provenance: list[Provenance] = []
        warnings: list[str] = []
        by_key = {(r.field, r.key): r for r in resolutions}
        used: set[tuple[str, str | None]] = set()

        def settle(
            field: str, key: str | None, candidates: list[tuple[str, Any]]
        ) -> tuple[Any, str] | None:
            conflict = Conflict(
                field=field,
                key=key,
                candidates=[Candidate(source=s, value=v) for s, v in candidates],
            )
            res = by_key.get((field, key))
            if res is None:
                conflicts.append(conflict)
                return None
            used.add((field, key))
            if res.decision == "take":
                pick = next((v for s, v in candidates if s == res.winner), None)
                if pick is None:
                    raise StyleError(
                        f"{conflict.label}의 winner {res.winner}가 후보에 없습니다."
                    )
                return pick, res.winner or ""
            return res.custom, "custom"

        for field in FACET_FIELDS:
            entries = claims[field]
            distinct = {canonical(v): (s, v) for s, v in entries}
            if not entries:
                continue
            if len(distinct) == 1:
                merged[field] = entries[0][1]
                provenance.append(
                    Provenance(
                        field=field, sources=[s for s, _ in entries], via="claim"
                    )
                )
                continue
            picked = settle(field, None, entries)
            if picked:
                value = (
                    picked[0]
                    if isinstance(picked[0], Facet)
                    else Facet.model_validate(picked[0])
                )
                merged[field] = value
                provenance.append(
                    Provenance(field=field, sources=[picked[1]], via="resolution")
                )
        # palette: 역할 단위 충돌
        palette: dict[str, PaletteColor] = {}
        by_role: dict[str, list[tuple[str, PaletteColor]]] = {}
        for source, colors in claims["palette"]:
            for color in colors:
                by_role.setdefault(color.role, []).append((source, color))
        for role, items in by_role.items():
            if len({c.hex.lower() for _, c in items}) == 1:
                palette[role] = items[0][1]
                continue
            picked = settle("palette", role, items)
            if picked:
                palette[role] = (
                    picked[0]
                    if isinstance(picked[0], PaletteColor)
                    else PaletteColor.model_validate(picked[0])
                )
        if claims["palette"]:
            merged["palette"] = list(palette.values())
            provenance.append(
                Provenance(
                    field="palette",
                    sources=[s for s, _ in claims["palette"]],
                    via="merged",
                )
            )
        # 참조·예시: 합집합. 같은 문장이 한쪽에서는 좋은 예, 다른 쪽에서는 나쁜 예이면 충돌.
        refs: dict[tuple, ReferenceAsset] = {}
        for _, items in claims["reference_assets"]:
            for item in items:
                refs.setdefault((item.kind, item.ref, item.sha256), item)
        if claims["reference_assets"]:
            merged["reference_assets"] = list(refs.values())
            provenance.append(
                Provenance(
                    field="reference_assets",
                    sources=[s for s, _ in claims["reference_assets"]],
                    via="merged",
                )
            )
        pos: dict[str, tuple[str, Example]] = {}
        neg: dict[str, tuple[str, Example]] = {}
        for source, items in claims["positive_examples"]:
            for item in items:
                pos.setdefault(item.text.strip().casefold(), (source, item))
        for source, items in claims["negative_examples"]:
            for item in items:
                neg.setdefault(item.text.strip().casefold(), (source, item))
        for text in sorted(pos.keys() & neg.keys()):
            picked = settle(
                "positive_examples",
                text,
                [
                    (pos[text][0] + ":positive", pos[text][1]),
                    (neg[text][0] + ":negative", neg[text][1]),
                ],
            )
            if picked:
                kept = picked[1].endswith(":positive") or picked[1] == "custom"
                (neg if kept else pos).pop(text)
        if claims["positive_examples"] or claims["negative_examples"]:
            merged["positive_examples"] = [e for _, e in pos.values()]
            merged["negative_examples"] = [e for _, e in neg.values()]
            provenance.append(
                Provenance(
                    field="positive_examples",
                    sources=[s for s, _ in claims["positive_examples"]],
                    via="merged",
                )
            )
        # 품질 제약: id 단위 충돌
        by_id: dict[str, list[tuple[str, QualityConstraint]]] = {}
        for source, items in claims["quality_constraints"]:
            for item in items:
                by_id.setdefault(item.id, []).append((source, item))
        constraints: dict[str, QualityConstraint] = {}
        for cid, items in by_id.items():
            if len({canonical(c) for _, c in items}) == 1:
                constraints[cid] = items[0][1]
                continue
            picked = settle("quality_constraints", cid, items)
            if picked:
                constraints[cid] = (
                    picked[0]
                    if isinstance(picked[0], QualityConstraint)
                    else QualityConstraint.model_validate(picked[0])
                )
        if claims["quality_constraints"]:
            merged["quality_constraints"] = list(constraints.values())
            provenance.append(
                Provenance(
                    field="quality_constraints",
                    sources=[s for s, _ in claims["quality_constraints"]],
                    via="merged",
                )
            )
        # 호환 스킬: 모든 입력이 호환한다고 한 것만 남긴다.
        skill_claims = claims["compatible_skills"]
        if skill_claims:
            common = set(skill_claims[0][1])
            for _, skills in skill_claims[1:]:
                common &= set(skills)
            merged["compatible_skills"] = sorted(common)
            if not common and len(skill_claims) > 1:
                warnings.append(
                    "모든 입력 스타일이 함께 호환하는 스킬이 없습니다. 단계별로 스킬을 나눠 써야 합니다."
                )
            dropped = {s for _, skills in skill_claims for s in skills} - common
            if dropped:
                warnings.append(
                    "일부 입력만 호환해 제외된 스킬: " + ", ".join(sorted(dropped))
                )
            provenance.append(
                Provenance(
                    field="compatible_skills",
                    sources=[s for s, _ in skill_claims],
                    via="merged",
                )
            )
        stray = [
            f"{f}{'[' + k + ']' if k else ''}"
            for (f, k) in by_key
            if (f, k) not in used
            and not any(c.field == f and c.key == k for c in conflicts)
        ]
        if stray:
            raise StyleError(
                "충돌이 없는 곳에 해결 기록이 있습니다: " + ", ".join(stray)
            )
        label_inputs = [str(i) for i in inputs]
        if conflicts:
            return ComposeResult(
                status="unresolved",
                conflicts=conflicts,
                resolutions_applied=[],
                dna=None,
                provenance=provenance,
                warnings=warnings,
                sha256=None,
                inputs=label_inputs,
            )
        # 어떤 입력도 주장하지 않은 필드는 첫 입력의 상속 기본값으로 채우되 출처를 남긴다.
        first = effective[label_inputs[0]]
        for field in MERGE_FIELDS:
            if field not in merged:
                merged[field] = getattr(first, field)
                provenance.append(
                    Provenance(
                        field=field, sources=[label_inputs[0]], via="inherited_default"
                    )
                )
        composed_id = "+".join(i.style_id for i in inputs)[:60]
        composed_id = re.sub(r"[^a-z0-9_]", "_", composed_id)
        dna = StyleDNA.model_validate(
            {
                "style_id": composed_id,
                "version": "0.0.0",
                **{f: merged[f] for f in MERGE_FIELDS},
            }
        )
        applied = [by_key[k] for k in sorted(used, key=lambda x: (x[0], x[1] or ""))]
        pending = any(not r.confirmed_by_user for r in applied)
        return ComposeResult(
            status="resolved_pending_confirmation" if pending else "resolved",
            conflicts=[],
            resolutions_applied=applied,
            dna=dna,
            provenance=provenance,
            warnings=warnings,
            sha256=sha256_of(
                {"inputs": label_inputs, "dna": dna, "resolutions": applied}
            ),
            inputs=label_inputs,
        )

    # --- 작품 단위 고정 ---
    def pin_work(
        self,
        work_id: str,
        inputs: list[StyleRef],
        resolutions: list[Resolution] | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        uuid.UUID(work_id)
        result = self.compose(inputs, resolutions)
        if result.status == "unresolved":
            raise UnresolvedStyleConflict(result.conflicts)
        folder = self.root / "works"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{work_id}.json"
        with (folder / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = json.loads(path.read_text()) if path.exists() else None
                revision = (current or {"revision": 0})["revision"]
                if expected_revision is not None and expected_revision != revision:
                    raise StyleError(
                        "다른 변경이 먼저 저장됐습니다. 새로고침한 뒤 다시 고정해 주세요."
                    )
                record = {
                    "work_id": work_id,
                    "revision": revision + 1,
                    "pinned_at": datetime.now(UTC).isoformat(),
                    "inputs": result.inputs,
                    "status": result.status,
                    "sha256": result.sha256,
                    "resolutions": [
                        r.model_dump(mode="json") for r in result.resolutions_applied
                    ],
                    "dna": result.dna.model_dump(mode="json") if result.dna else None,
                    "provenance": [
                        p.model_dump(mode="json") for p in result.provenance
                    ],
                }
                temp = folder / f"{uuid.uuid4()}.tmp"
                with temp.open("w") as f:
                    json.dump(record, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                temp.replace(path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        return record

    def get_work(self, work_id: str) -> dict[str, Any]:
        uuid.UUID(work_id)
        path = self.root / "works" / f"{work_id}.json"
        if not path.is_file():
            raise FileNotFoundError("작품 스타일이 고정된 적 없습니다.")
        return json.loads(path.read_text())


def _facet(text: str, **params: Param) -> Facet:
    return Facet(text=text, params=params)


def seed_docs() -> list[StyleDNADoc]:
    """내장 시드. 참조 자산·예시가 없는 draft이며 근거가 쌓이면 새 버전으로 올린다."""
    base = StyleDNADoc(
        style_id="neutral_base",
        version="1.0.0",
        note="상속용 중립 기본값. 단독 스타일로 쓰지 않는다.",
        visual_identity=_facet("특정 스타일을 가정하지 않는 중립 기본값"),
        palette=[PaletteColor(hex="#808080", role="primary")],
        character_proportions=_facet("실제 비율"),
        materials=_facet("기본 재질"),
        lighting=_facet("균일한 중립 조명"),
        camera_language=_facet("안정된 고정·완만한 이동"),
        motion_language=_facet("절제된 모션"),
        environment_rules=_facet("세계관 지정 전까지 제약 없음"),
        sound_identity=_facet("무음에 가까운 중립 환경음"),
        reference_assets=[],
        positive_examples=[],
        negative_examples=[],
        quality_constraints=[
            QualityConstraint(id="no_black_frames", text="검은 프레임 0장", hard=True)
        ],
        compatible_skills=[
            "skill.studio-3d-cinematic",
            "skill.ttakkari-remotion-setup",
        ],
    )
    cute = StyleDNADoc(
        style_id="cute_3d",
        version="1.0.0",
        note="귀여운 로우폴리 3D. 특정 상용 게임 자산·캐릭터를 복제하지 않는다.",
        visual_identity=_facet(
            "둥근 형태와 파스텔 색의 로우폴리 3D 미니어처", polygon_budget="low"
        ),
        palette=[
            PaletteColor(hex="#FFD6E0", role="primary"),
            PaletteColor(hex="#CFE8FF", role="secondary"),
            PaletteColor(hex="#FFF6D6", role="background"),
        ],
        character_proportions=_facet("머리가 큰 2~3등신", head_to_body=0.4),
        materials=_facet("무광 플라스틱과 부드러운 점토 질감"),
        lighting=_facet(
            "부드러운 확산광, 낮은 대비, 따뜻한 색 온도", contrast="low", kelvin=5200.0
        ),
        camera_language=_facet("완만한 오비트와 눈높이 중간 샷"),
        motion_language=_facet("느린 이징과 약한 바운스"),
        environment_rules=_facet("작은 소품이 가득한 미니어처 세트"),
        sound_identity=_facet("가벼운 멜로디와 부드러운 효과음"),
        reference_assets=[],
        positive_examples=[],
        negative_examples=[],
        quality_constraints=[
            QualityConstraint(id="no_black_frames", text="검은 프레임 0장", hard=True),
            QualityConstraint(
                id="soft_shadows", text="그림자 경계는 부드럽게", hard=False
            ),
        ],
        compatible_skills=[
            "skill.studio-3d-cinematic",
            "skill.ttakkari-remotion-setup",
        ],
    )
    horror = StyleDNADoc(
        style_id="horror_lighting",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[
            Override(
                field="lighting", reason="공포 연출을 위해 저조도 단일 광원으로 바꾼다"
            ),
            Override(
                field="sound_identity", reason="긴장을 위한 저주파 드론과 정적 대비"
            ),
            Override(field="quality_constraints", reason="암부 디테일 보존 기준 추가"),
            Override(
                field="compatible_skills", reason="3D 시네마틱 스킬에서만 검증한 조명"
            ),
        ],
        lighting=_facet(
            "저조도 단일 광원, 깊은 그림자, 차가운 색 온도",
            contrast="high",
            kelvin=3600.0,
        ),
        sound_identity=_facet("저주파 드론과 갑작스러운 정적"),
        quality_constraints=[
            QualityConstraint(id="no_black_frames", text="검은 프레임 0장", hard=True),
            QualityConstraint(
                id="shadow_detail",
                text="암부 디테일이 완전히 뭉개지지 않을 것",
                hard=True,
            ),
        ],
        compatible_skills=["skill.studio-3d-cinematic"],
    )
    sf = StyleDNADoc(
        style_id="sf_world",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[
            Override(
                field="environment_rules", reason="우주 SF 세계관 규칙을 적용한다"
            ),
            Override(field="sound_identity", reason="우주 환경의 공간음 규칙"),
        ],
        environment_rules=_facet(
            "우주선 내부와 행성 표면. 진공에서는 외부 소리가 없다", vacuum_silence=True
        ),
        sound_identity=_facet("기계 험과 먼 통신음, 진공 외부는 무음"),
        compatible_skills=[
            "skill.studio-3d-cinematic",
            "skill.ttakkari-remotion-setup",
        ],
    )
    anime = StyleDNADoc(
        style_id="anime_cel",
        version="1.0.0",
        parent=StyleRef(style_id="neutral_base", version="1.0.0"),
        overrides=[
            Override(field="visual_identity", reason="셀 애니메이션 룩"),
            Override(field="character_proportions", reason="애니메이션 비율"),
            Override(field="motion_language", reason="제한 애니메이션 타이밍"),
            Override(field="compatible_skills", reason="이미지 편집 스킬 기반 제작"),
        ],
        visual_identity=_facet("굵은 윤곽선과 단계 음영의 셀 룩"),
        character_proportions=_facet("큰 눈과 단순한 코·입, 6~7등신"),
        motion_language=_facet("2~3프레임 홀드의 제한 애니메이션"),
        compatible_skills=["skill.edit1-flux2-klein", "skill.edit2-flux2-klein"],
    )
    return [base, cute, horror, sf, anime]


def default_style_store() -> StyleStore:
    from ttakkari.config import get_settings

    return StyleStore(
        get_settings().home / "creative-studio" / "style-dna", seeds=seed_docs()
    )
