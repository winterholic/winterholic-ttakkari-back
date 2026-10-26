"""Collector Platform: per-source rights/usage policy and the per-job collection request (CP-S2-03).

Two inputs with different trust:
- SourcePolicySpec is registered server side (collector_source_policies, append-only versions). It says which hosts
  may be fetched and which purposes (store/train/export/knowledge) the operator has verified for the source. It is
  the only origin of `rights` in produced envelopes. A client-supplied `rights` object in the intake envelope is
  never copied, because intake binding only authenticates tenant/scope/principals.
- CollectRequest rides in the job envelope as extensions["collector:request"]. It can narrow what the policy
  allows (fewer pages, shallower depth, a stricter profile) and can never widen it.

Unknown means deny: no policy, an unverified purpose, or a host outside allowed_hosts all stop the fetch.
robots.txt allowing a URL is politeness only; it does not grant a purpose.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ttakkari.collector.contracts import (
    ALLOWED_PERMISSIONS,
    ALLOWED_PURPOSES,
    CollectorContractError,
    jcs,
    parse_iso_datetime,
)
from ttakkari.collector.netguard import ParsedUrl, UrlRejected, normalize_url
from ttakkari.collector.profiles import PROFILES, get_profile, resolve_params
from ttakkari.collector.quality import L1Policy

MAX_PAGES_HARD_CAP = 500
MAX_DEPTH_HARD_CAP = 3
MAX_SEEDS = 50
POLICY_FIELDS = {
    "allowed_hosts", "schemes", "ports", "permissions", "license_id", "policy_url", "checked_at", "basis",
    "robots_required", "min_delay_seconds", "max_pages_per_job", "max_depth", "store_raw", "retain_days",
    "excluded_path_prefixes", "allowed_path_prefixes", "default_profile", "allowed_profiles",
}
REQUEST_FIELDS = {"seeds", "profile", "profile_params", "max_pages", "max_depth", "l1"}


def _fail(msg: str) -> CollectorContractError:
    return CollectorContractError(msg)


@dataclass(frozen=True)
class SourcePolicySpec:
    allowed_hosts: tuple[str, ...]
    permissions: dict[str, str]
    basis: str
    checked_at: datetime
    schemes: tuple[str, ...] = ("https",)
    ports: tuple[int, ...] = (443,)
    license_id: str | None = None
    policy_url: str | None = None
    robots_required: bool = True
    min_delay_seconds: float = 1.0
    max_pages_per_job: int = 50
    max_depth: int = 0
    store_raw: bool = True
    retain_days: int | None = None
    excluded_path_prefixes: tuple[str, ...] = ()
    allowed_path_prefixes: tuple[str, ...] = ()
    default_profile: str = "general"
    allowed_profiles: tuple[str, ...] = tuple(sorted(PROFILES))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourcePolicySpec:
        if not isinstance(d, dict):
            raise _fail("source policy must be an object")
        unknown = set(d) - POLICY_FIELDS
        if unknown:
            raise _fail(f"unknown source policy fields {sorted(unknown)}")
        hosts = d.get("allowed_hosts")
        if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) and h for h in hosts):
            raise _fail("allowed_hosts must be a non-empty list of hosts")
        norm_hosts = []
        for h in hosts:
            h = h.strip().lower()
            if "/" in h or ":" in h or "*" in h or h in (".", ""):
                raise _fail(f"allowed_hosts entry {h!r}: use a bare host, or a leading dot for subdomains")
            norm_hosts.append(h)
        perms = d.get("permissions")
        if not isinstance(perms, dict) or set(perms) - ALLOWED_PURPOSES:
            raise _fail(f"permissions keys must be within {sorted(ALLOWED_PURPOSES)}")
        for purpose, value in perms.items():
            if value not in ALLOWED_PERMISSIONS:
                raise _fail(f"permissions.{purpose} must be one of {sorted(ALLOWED_PERMISSIONS)}")
        basis = d.get("basis")
        if not isinstance(basis, str) or not basis.strip():
            raise _fail("basis must state why these permissions hold (terms section, license name)")
        checked = parse_iso_datetime(d.get("checked_at"))
        policy_url = d.get("policy_url")
        if policy_url is not None:
            try:
                normalize_url(policy_url, allowed_schemes=("https",), allowed_ports=None, strip_tracking=False)
            except UrlRejected as e:
                raise _fail(f"policy_url is not a valid https URL: {e.code}") from e
        if any(v == "allowed" for v in perms.values()) and (policy_url is None or not d.get("license_id")):
            raise _fail("an allowed purpose needs policy_url and license_id (the verified source of the grant)")
        schemes = tuple(d.get("schemes", ("https",)))
        if not schemes or set(schemes) - {"http", "https"}:
            raise _fail("schemes must be a subset of http, https")
        ports = tuple(d.get("ports", (443,)))
        if not ports or not all(isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 for p in ports):
            raise _fail("ports must be integers in 1..65535")
        delay = d.get("min_delay_seconds", 1.0)
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 0 <= delay <= 3600:
            raise _fail("min_delay_seconds must be within 0..3600")
        pages = d.get("max_pages_per_job", 50)
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= MAX_PAGES_HARD_CAP:
            raise _fail(f"max_pages_per_job must be within 1..{MAX_PAGES_HARD_CAP}")
        depth = d.get("max_depth", 0)
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_DEPTH_HARD_CAP:
            raise _fail(f"max_depth must be within 0..{MAX_DEPTH_HARD_CAP}")
        retain = d.get("retain_days")
        if retain is not None and (isinstance(retain, bool) or not isinstance(retain, int) or retain < 1):
            raise _fail("retain_days must be a positive integer or null")
        for key in ("robots_required", "store_raw"):
            if key in d and not isinstance(d[key], bool):
                raise _fail(f"{key} must be boolean")
        excluded = tuple(d.get("excluded_path_prefixes", ()))
        if not all(isinstance(p, str) and p.startswith("/") for p in excluded):
            raise _fail("excluded_path_prefixes entries must start with /")
        allowed_paths = d.get("allowed_path_prefixes", [])
        if not isinstance(allowed_paths, list) or not all(isinstance(p, str) and p.startswith("/") for p in allowed_paths):
            raise _fail("allowed_path_prefixes must be a list of paths starting with /")
        allowed_profiles = tuple(d.get("allowed_profiles", sorted(PROFILES)))
        default_profile = d.get("default_profile", "general")
        for name in (*allowed_profiles, default_profile):
            get_profile(name)
        if default_profile not in allowed_profiles:
            raise _fail("default_profile must be in allowed_profiles")
        return cls(
            allowed_hosts=tuple(norm_hosts), permissions=dict(perms), basis=basis.strip(), checked_at=checked,
            schemes=schemes, ports=ports, license_id=d.get("license_id"), policy_url=policy_url,
            robots_required=d.get("robots_required", True), min_delay_seconds=float(delay),
            max_pages_per_job=pages, max_depth=depth, store_raw=d.get("store_raw", True), retain_days=retain,
            excluded_path_prefixes=excluded, allowed_path_prefixes=tuple(allowed_paths),
            default_profile=default_profile, allowed_profiles=allowed_profiles,
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "allowed_hosts": list(self.allowed_hosts), "schemes": list(self.schemes), "ports": list(self.ports),
            "permissions": dict(self.permissions), "license_id": self.license_id, "policy_url": self.policy_url,
            "checked_at": self.checked_at.isoformat(), "basis": self.basis, "robots_required": self.robots_required,
            "min_delay_seconds": self.min_delay_seconds, "max_pages_per_job": self.max_pages_per_job,
            "max_depth": self.max_depth, "store_raw": self.store_raw, "retain_days": self.retain_days,
            "excluded_path_prefixes": list(self.excluded_path_prefixes), "default_profile": self.default_profile,
            "allowed_profiles": list(self.allowed_profiles),
        }
        # Omit the additive empty field to preserve hashes of existing policy versions.
        if self.allowed_path_prefixes:
            value["allowed_path_prefixes"] = list(self.allowed_path_prefixes)
        return value

    def spec_hash(self) -> str:
        return hashlib.sha256(jcs(self.to_dict()).encode("utf-8")).hexdigest()

    def allows(self, purpose: str) -> bool:
        return self.permissions.get(purpose, "unknown") == "allowed"

    def granted_purposes(self) -> list[str]:
        return [p for p in sorted(ALLOWED_PURPOSES) if self.allows(p)]

    def host_allowed(self, host: str) -> bool:
        host = host.lower()
        for entry in self.allowed_hosts:
            if entry.startswith("."):
                if host.endswith(entry) or host == entry[1:]:
                    return True
            elif host == entry:
                return True
        return False

    def check_url(self, url: ParsedUrl) -> str | None:
        """None when fetching `url` is within the policy, else a stable reason code."""
        if not self.host_allowed(url.host):
            return "host_not_allowed"
        if url.scheme not in self.schemes:
            return "scheme_not_allowed"
        if url.port not in self.ports:
            return "port_not_allowed"
        path = url.target.split("?", 1)[0]
        if self.allowed_path_prefixes and not any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                                                   for p in self.allowed_path_prefixes):
            return "path_outside_policy"
        if any(path.startswith(p) for p in self.excluded_path_prefixes):
            return "path_excluded_by_policy"
        return None

    def retain_until(self, fetched_at: datetime) -> datetime | None:
        return fetched_at + timedelta(days=self.retain_days) if self.retain_days else None

    def rights_block(self, version_label: str, fetched_at: datetime, robots: str) -> dict[str, Any]:
        """The envelope `rights` object. Permission is 'allowed' only when store is verified; otherwise it is the
        most restrictive state recorded for any purpose."""
        if self.allows("store"):
            permission = "allowed"
        elif "denied" in self.permissions.values():
            permission = "denied"
        else:
            permission = "unknown"
        return {
            "license_id": self.license_id, "policy_version": version_label, "policy_url": self.policy_url,
            "checked_at": self.checked_at.isoformat(), "permission": permission, "purposes": self.granted_purposes(),
            "robots": robots, "basis": self.basis,
            "retain_until": self.retain_until(fetched_at).isoformat() if self.retain_days else None,
        }


@dataclass(frozen=True)
class CollectRequest:
    seeds: tuple[str, ...]
    profile: str
    profile_params: dict[str, Any]
    max_pages: int
    max_depth: int
    l1: L1Policy = field(default_factory=L1Policy)

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any], policy: SourcePolicySpec) -> CollectRequest:
        """Read collector:request, defaulting from the envelope source URL. Raises CollectorContractError."""
        raw = (envelope.get("extensions") or {}).get("collector:request")
        d: dict[str, Any] = {}
        if raw is not None:
            if not isinstance(raw, dict):
                raise _fail("collector:request must be an object")
            unknown = set(raw) - REQUEST_FIELDS
            if unknown:
                raise _fail(f"unknown collector:request fields {sorted(unknown)}")
            d = raw
        seeds = d.get("seeds")
        if seeds is None:
            url = (envelope.get("source") or {}).get("url")
            seeds = [url] if url else []
        if not isinstance(seeds, list) or not seeds or len(seeds) > MAX_SEEDS or not all(isinstance(s, str) for s in seeds):
            raise _fail(f"seeds must be 1..{MAX_SEEDS} URLs (or set source.url)")
        profile_name = d.get("profile", policy.default_profile)
        if profile_name not in policy.allowed_profiles:
            raise _fail(f"profile {profile_name!r} is not allowed for this source (allowed: {list(policy.allowed_profiles)})")
        profile = get_profile(profile_name)
        params = d.get("profile_params", {})
        if not isinstance(params, dict):
            raise _fail("profile_params must be an object")
        resolve_params(profile, params)  # validate now so a bad job fails at start, not per document
        max_pages = d.get("max_pages", policy.max_pages_per_job)
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= policy.max_pages_per_job:
            raise _fail(f"max_pages must be within 1..{policy.max_pages_per_job} (source policy limit)")
        max_depth = d.get("max_depth", policy.max_depth)
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= policy.max_depth:
            raise _fail(f"max_depth must be within 0..{policy.max_depth} (source policy limit)")
        l1_raw = d.get("l1", {})
        if not isinstance(l1_raw, dict) or set(l1_raw) - {"mode", "scorer", "hold_below"}:
            raise _fail("l1 must be an object with mode, scorer, hold_below")
        l1 = L1Policy(l1_raw.get("mode", "off"), l1_raw.get("scorer"), dict(l1_raw.get("hold_below", {})))
        return cls(tuple(seeds), profile_name, dict(params), max_pages, max_depth, l1)


def policy_version_label(source_key: str, version: int, spec_hash: str) -> str:
    return f"{source_key}@v{version}+{spec_hash[:12]}"


def spec_json(spec: SourcePolicySpec) -> str:
    return json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False)


def utc_now() -> datetime:
    return datetime.now(UTC)
