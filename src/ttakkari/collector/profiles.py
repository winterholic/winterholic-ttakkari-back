"""Collector Platform: purpose-specific evaluation profiles, selected per job (CP-S2-06).

One document set can be judged for different purposes: a news feed cares about freshness, technical documentation
about the version a statement is true for. A job names its profile (collector:request.profile); the source policy
supplies the default and may restrict the choice. A profile may only add *hold* reasons and signals. It cannot
accept something the rights or robots checks refused, and it never invents a measurement: signals it cannot
determine are recorded as null.

Adding a profile means adding one class here and registering it in PROFILES. The pipeline has no profile-specific
branches.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ttakkari.collector.contracts import CollectorContractError
from ttakkari.collector.parse import find_version_hint, is_latest_alias
from ttakkari.collector.quality import L0Thresholds


@dataclass(frozen=True)
class ProfileInput:
    url: str
    text: str
    title: str | None
    published: datetime | None
    published_precision: str | None
    last_modified: datetime | None
    fetched_at: datetime
    meta: dict[str, str]


@dataclass(frozen=True)
class ProfileResult:
    signals: dict[str, Any]
    hold_reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    published_at: datetime | None = None
    valid_from: datetime | None = None


class EvaluationProfile(Protocol):
    name: str
    version: str
    param_schema: dict[str, tuple[type, float, float]]  # name -> (type, min, max) for job-level overrides
    defaults: dict[str, Any]

    def l0_thresholds(self, params: dict[str, Any]) -> L0Thresholds: ...

    def evaluate(self, doc: ProfileInput, params: dict[str, Any]) -> ProfileResult: ...


def resolve_params(profile: EvaluationProfile, overrides: dict[str, Any]) -> dict[str, Any]:
    """Defaults plus job overrides. Unknown names and out-of-range values are rejected, not clamped."""
    params = dict(profile.defaults)
    for key, value in overrides.items():
        spec = profile.param_schema.get(key)
        if spec is None:
            raise CollectorContractError(f"profile {profile.name!r} has no parameter {key!r}")
        kind, low, high = spec
        if kind is bool:
            if not isinstance(value, bool):
                raise CollectorContractError(f"profile parameter {key!r} must be boolean")
        elif isinstance(value, bool) or not isinstance(value, kind) or not (low <= value <= high):
            raise CollectorContractError(f"profile parameter {key!r} must be {kind.__name__} in [{low}, {high}]")
        params[key] = value
    return params


@dataclass(frozen=True)
class GeneralProfile:
    """No purpose assumption: only the L0 rules decide."""

    name: str = "general"
    version: str = "general-1"
    param_schema: dict[str, tuple[type, float, float]] = field(default_factory=lambda: {"min_chars": (int, 20, 100_000)})
    defaults: dict[str, Any] = field(default_factory=lambda: {"min_chars": 200})

    def l0_thresholds(self, params: dict[str, Any]) -> L0Thresholds:
        return L0Thresholds(min_chars=params["min_chars"])

    def evaluate(self, doc: ProfileInput, params: dict[str, Any]) -> ProfileResult:
        return ProfileResult(signals={"profile": self.name})


@dataclass(frozen=True)
class NewsProfile:
    """Freshness matters. A page without a publication date cannot be judged fresh, so it is held, not guessed.

    The date comes from the page metadata only. A Last-Modified header is recorded as a signal but is not used
    as a publication date, because it also changes when a page is merely re-rendered.
    """

    name: str = "news"
    version: str = "news-1"
    param_schema: dict[str, tuple[type, float, float]] = field(
        default_factory=lambda: {
            "max_age_days": (int, 1, 3650),
            "min_chars": (int, 20, 100_000),
            "require_published_at": (bool, 0, 1),
        }
    )
    defaults: dict[str, Any] = field(
        default_factory=lambda: {"max_age_days": 14, "min_chars": 300, "require_published_at": True}
    )

    def l0_thresholds(self, params: dict[str, Any]) -> L0Thresholds:
        # News bodies are short and link-rich (related-article lists), so the link and short-line rules are relaxed.
        return L0Thresholds(min_chars=params["min_chars"], max_url_density=0.30, max_short_line_ratio=0.92)

    def evaluate(self, doc: ProfileInput, params: dict[str, Any]) -> ProfileResult:
        holds: list[str] = []
        age_days: float | None = None
        published = doc.published
        if published is None:
            if params["require_published_at"]:
                holds.append("news_published_at_unknown")
        else:
            age_days = round((doc.fetched_at - published).total_seconds() / 86400, 2)
            if age_days < -1:
                holds.append("news_published_in_future")
            elif age_days > params["max_age_days"]:
                holds.append("news_stale")
        signals = {
            "profile": self.name,
            "published_at": published.isoformat() if published else None,
            "published_precision": doc.published_precision,
            "age_days": age_days,
            "max_age_days": params["max_age_days"],
            "last_modified_header": doc.last_modified.isoformat() if doc.last_modified else None,
        }
        return ProfileResult(signals, tuple(holds), published_at=published, valid_from=None)


@dataclass(frozen=True)
class TechDocsProfile:
    """Accuracy and version matter. A statement in documentation is true for a version, so the version is a signal.

    `latest`/`stable` style URLs change under the same address; that is recorded so a later fetch is a new revision.
    Code blocks are common in documentation, so the symbol and alphabet rules are relaxed.
    """

    name: str = "tech_docs"
    version: str = "tech-docs-1"
    param_schema: dict[str, tuple[type, float, float]] = field(
        default_factory=lambda: {"require_version": (bool, 0, 1), "min_chars": (int, 20, 100_000)}
    )
    defaults: dict[str, Any] = field(default_factory=lambda: {"require_version": True, "min_chars": 120})

    def l0_thresholds(self, params: dict[str, Any]) -> L0Thresholds:
        return L0Thresholds(
            min_chars=params["min_chars"], max_symbol_ratio=0.55, min_alpha_ratio=0.30,
            max_short_line_ratio=0.97, max_duplicate_line_ratio=0.70,
        )

    def evaluate(self, doc: ProfileInput, params: dict[str, Any]) -> ProfileResult:
        version, source = find_version_hint(doc.url, doc.text, doc.meta)
        alias = is_latest_alias(doc.url)
        holds: list[str] = []
        if version is None and params["require_version"] and not alias:
            holds.append("tech_docs_version_unknown")
        notes = ["tech_docs_latest_alias"] if alias else []
        signals = {
            "profile": self.name,
            "version_hint": version,
            "version_source": source,
            "latest_alias_url": alias,
            "has_modified_hint": "modified" in doc.meta,
            "modified_hint": doc.meta.get("modified"),
            "generator": doc.meta.get("generator"),
        }
        return ProfileResult(signals, tuple(holds), tuple(notes), published_at=doc.published)


PROFILES: dict[str, EvaluationProfile] = {p.name: p for p in (GeneralProfile(), NewsProfile(), TechDocsProfile())}


def get_profile(name: str) -> EvaluationProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise CollectorContractError(f"unknown evaluation profile {name!r}; known: {sorted(PROFILES)}") from None


def utc_now() -> datetime:
    return datetime.now(UTC)
