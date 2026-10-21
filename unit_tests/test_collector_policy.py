"""CP-S2-03: source policy validation, request narrowing, rights mapping and the sample source inventory."""
from __future__ import annotations

import csv
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ttakkari.collector.contracts import (
    ALLOWED_PERMISSIONS,
    CollectorContractError,
    parse_iso_datetime,
)
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector.policy import CollectRequest, SourcePolicySpec

INVENTORY = Path(__file__).resolve().parents[1] / "reports/collector/s2-03/source-policy.csv"


def base(**over) -> dict:
    d = {
        "allowed_hosts": ["docs.example.test"], "permissions": {"store": "allowed"}, "license_id": "CC-BY-4.0",
        "policy_url": "https://example.test/terms", "checked_at": "2026-10-21T00:00:00+09:00", "basis": "terms 1.2",
    }
    d.update(over)
    return d


class SpecValidationTests(unittest.TestCase):
    def test_minimal_policy_is_conservative(self):
        spec = SourcePolicySpec.from_dict(base())
        self.assertEqual((spec.schemes, spec.ports, spec.max_depth, spec.robots_required, spec.store_raw), (("https",), (443,), 0, True, True))
        self.assertTrue(spec.allows("store"))
        for purpose in ("train", "export", "knowledge"):
            self.assertFalse(spec.allows(purpose), f"{purpose} is unknown unless stated")
        self.assertEqual(spec.granted_purposes(), ["store"])

    def test_invalid_policies_are_rejected(self):
        bad = [
            {"unknown_field": 1}, {"allowed_hosts": []}, {"allowed_hosts": ["a.test/path"]}, {"allowed_hosts": ["*.a.test"]},
            {"allowed_hosts": ["a.test:8080"]}, {"permissions": {"store": "maybe"}}, {"permissions": {"publish": "allowed"}},
            {"basis": "  "}, {"checked_at": "2026-10-21T00:00:00"}, {"checked_at": "yesterday"},
            {"policy_url": None}, {"license_id": None}, {"policy_url": "http://insecure.test/terms"},
            {"schemes": ["ftp"]}, {"schemes": []}, {"ports": [0]}, {"ports": [70000]}, {"ports": [True]},
            {"min_delay_seconds": -1}, {"min_delay_seconds": 99999}, {"max_pages_per_job": 0}, {"max_pages_per_job": 501},
            {"max_depth": 4}, {"retain_days": 0}, {"robots_required": "yes"}, {"store_raw": 1},
            {"excluded_path_prefixes": ["admin"]}, {"default_profile": "nope"}, {"allowed_profiles": ["general"], "default_profile": "news"},
        ]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(CollectorContractError):
                SourcePolicySpec.from_dict(base(**over))

    def test_an_allowed_purpose_needs_its_source_of_truth_but_unknown_does_not(self):
        SourcePolicySpec.from_dict(base(permissions={"store": "unknown"}, license_id=None, policy_url=None))
        with self.assertRaises(CollectorContractError):
            SourcePolicySpec.from_dict(base(license_id=None))

    def test_hash_is_stable_and_changes_with_any_field(self):
        a, b = SourcePolicySpec.from_dict(base()), SourcePolicySpec.from_dict(base())
        self.assertEqual(a.spec_hash(), b.spec_hash())
        self.assertEqual(SourcePolicySpec.from_dict(a.to_dict()).spec_hash(), a.spec_hash())
        for over in ({"basis": "terms 1.3"}, {"min_delay_seconds": 2}, {"permissions": {"store": "allowed", "export": "allowed"}}):
            self.assertNotEqual(SourcePolicySpec.from_dict(base(**over)).spec_hash(), a.spec_hash())


class UrlPolicyTests(unittest.TestCase):
    def test_host_matching_is_exact_or_explicit_subdomain(self):
        spec = SourcePolicySpec.from_dict(base(allowed_hosts=["a.test", ".b.test"]))
        self.assertTrue(all(spec.host_allowed(h) for h in ("a.test", "A.TEST", "b.test", "x.b.test", "y.x.b.test")))
        self.assertFalse(any(spec.host_allowed(h) for h in ("evil-a.test", "a.test.evil.test", "xb.test", "sub.a.test")))

    def test_check_url_reasons(self):
        spec = SourcePolicySpec.from_dict(base(allowed_hosts=["a.test"], excluded_path_prefixes=["/admin"]))
        u = lambda s: normalize_url(s, allowed_schemes=("http", "https"), allowed_ports=(80, 443, 8080))
        self.assertIsNone(spec.check_url(u("https://a.test/docs")))
        self.assertEqual(spec.check_url(u("https://b.test/docs")), "host_not_allowed")
        self.assertEqual(spec.check_url(u("http://a.test/docs")), "scheme_not_allowed")
        self.assertEqual(spec.check_url(u("https://a.test:8080/docs")), "port_not_allowed")
        self.assertEqual(spec.check_url(u("https://a.test/admin/users?x=1")), "path_excluded_by_policy")
        self.assertEqual(spec.check_url(u("https://a.test/administrator")), "path_excluded_by_policy")  # string prefix like robots: the safer reading


class RightsMappingTests(unittest.TestCase):
    def test_rights_block_reflects_only_the_policy(self):
        spec = SourcePolicySpec.from_dict(base(permissions={"store": "allowed", "train": "denied"}, retain_days=30))
        fetched = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
        rights = spec.rights_block("src@v3+abc", fetched, "allowed")
        self.assertEqual((rights["permission"], rights["purposes"], rights["robots"]), ("allowed", ["store"], "allowed"))
        self.assertEqual(parse_iso_datetime(rights["retain_until"]), fetched + timedelta(days=30))
        self.assertEqual((rights["policy_version"], rights["license_id"]), ("src@v3+abc", "CC-BY-4.0"))

    def test_permission_is_the_most_restrictive_known_state(self):
        denied = SourcePolicySpec.from_dict(base(permissions={"store": "denied"}, license_id=None, policy_url=None))
        unknown = SourcePolicySpec.from_dict(base(permissions={"export": "unknown"}, license_id=None, policy_url=None))
        t = datetime(2026, 1, 1, tzinfo=UTC)
        self.assertEqual(denied.rights_block("v", t, "not_applicable")["permission"], "denied")
        self.assertEqual(unknown.rights_block("v", t, "not_applicable")["permission"], "unknown")
        self.assertEqual(unknown.rights_block("v", t, "not_applicable")["purposes"], [])


class RequestTests(unittest.TestCase):
    spec = SourcePolicySpec.from_dict(base(max_pages_per_job=10, max_depth=1, allowed_profiles=["general", "news"], default_profile="general"))

    def env(self, request=None, url="https://docs.example.test/a"):
        ext = {} if request is None else {"collector:request": request}
        return {"source": {"url": url}, "extensions": ext}

    def test_defaults_come_from_the_policy_and_source_url(self):
        req = CollectRequest.from_envelope(self.env(), self.spec)
        self.assertEqual((req.seeds, req.profile, req.max_pages, req.max_depth, req.l1.mode), (("https://docs.example.test/a",), "general", 10, 1, "off"))

    def test_a_request_can_narrow_but_never_widen(self):
        req = CollectRequest.from_envelope(self.env({"seeds": ["https://docs.example.test/x"], "max_pages": 3, "max_depth": 0, "profile": "news", "profile_params": {"max_age_days": 3}}), self.spec)
        self.assertEqual((req.max_pages, req.max_depth, req.profile, req.profile_params), (3, 0, "news", {"max_age_days": 3}))
        for widen in ({"max_pages": 11}, {"max_depth": 2}, {"profile": "tech_docs"}, {"seeds": []}, {"seeds": ["a"] * 51}, {"seeds": [1]}):
            with self.subTest(widen=widen), self.assertRaises(CollectorContractError):
                CollectRequest.from_envelope(self.env(widen), self.spec)
        with self.assertRaises(CollectorContractError):
            CollectRequest.from_envelope(self.env(None, url=None), self.spec)  # nothing to collect

    def test_l1_request_shape(self):
        ok = CollectRequest.from_envelope(self.env({"l1": {"mode": "gate", "scorer": "s", "hold_below": {"education": 0.3}}}), self.spec)
        self.assertEqual((ok.l1.mode, ok.l1.hold_below), ("gate", {"education": 0.3}))
        with self.assertRaises(CollectorContractError):
            CollectRequest.from_envelope(self.env({"l1": {"mode": "gate", "scorer": "s", "extra": 1}}), self.spec)


class InventoryTests(unittest.TestCase):
    """reports/collector/s2-03/source-policy.csv: URL, check time, license and purpose scope for every sample source."""

    @classmethod
    def setUpClass(cls):
        with INVENTORY.open(encoding="utf-8", newline="") as f:
            cls.rows = list(csv.DictReader(f))

    def test_five_domains_by_two_languages(self):
        self.assertEqual(len(self.rows), 10)
        self.assertEqual(len({r["domain"] for r in self.rows}), 5)
        for domain in {r["domain"] for r in self.rows}:
            self.assertEqual(sorted(r["language"] for r in self.rows if r["domain"] == domain), ["en", "ko"], domain)
        self.assertEqual(len({r["source_key"] for r in self.rows}), 10)

    def test_every_row_is_complete_and_unknown_is_never_promoted(self):
        for r in self.rows:
            with self.subTest(source=r["source_key"]):
                parse_iso_datetime(r["policy_checked_at"])
                for p in ("store", "train", "export", "knowledge"):
                    self.assertIn(r[p], ALLOWED_PERMISSIONS)
                self.assertTrue(r["robots_observation"] and r["evidence_level"] and r["notes"])
                self.assertTrue(r["seed_url"].startswith("https://"))
                normalize_url(r["seed_url"])
                if "allowed" in (r["store"], r["train"], r["export"], r["knowledge"]):
                    self.assertTrue(r["policy_url"].startswith("https://") and r["license_id"], "an allowed purpose needs its source")
                    self.assertNotIn("not_found", r["evidence_level"])
                    self.assertNotIn("not_verified", r["evidence_level"])
                else:
                    self.assertEqual(r["store"], "unknown")
                self.assertNotEqual(r["train"], "allowed", "no training grant was verified for any sample source")

    def test_allowed_rows_load_as_valid_policies(self):
        loaded = 0
        for r in self.rows:
            if r["store"] != "allowed":
                continue
            SourcePolicySpec.from_dict(base(
                allowed_hosts=[r["host"]], license_id=r["license_id"], policy_url=r["policy_url"],
                checked_at=r["policy_checked_at"], basis=r["notes"][:200], default_profile=r["profile"],
            ))
            loaded += 1
        self.assertGreaterEqual(loaded, 4)
        self.assertGreaterEqual(sum(r["store"] == "unknown" for r in self.rows), 3, "the inventory must show denied-by-default sources too")


if __name__ == "__main__":
    unittest.main()
