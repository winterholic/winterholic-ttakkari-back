"""CP-S2-01: Unit tests for collector envelope, budget policy contracts, and RFC 8785 JCS canonicalization.

Tests fixture expectations defined in docs/design/collector-platform/contracts/fixtures/
including positive and negative controls for strict schema, hash integrity, timezone awareness,
quality axis invariants, RFC 8785 JCS vectors, and scope binding.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ttakkari.collector.contracts import (
    JCS_VECTORS,
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    ScopeBindingMismatchError,
    _jcs_number,
    compute_payload_hash,
    jcs,
    validate_budget_policy,
    validate_envelope,
    verify_auth_binding,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "docs/design/collector-platform/contracts/fixtures"


class TestCollectorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expectations_path = FIXTURES_DIR / "expectations.json"
        with open(cls.expectations_path, "r", encoding="utf-8") as f:
            cls.expectations = json.load(f)

    def test_fixtures_manifest_integrity(self):
        """Ensure all valid-* and invalid-* fixture files are accounted for in expectations.json."""
        manifest_files = set(self.expectations.keys())
        disk_files = {p.name for p in FIXTURES_DIR.glob("valid-*.json")} | {
            p.name for p in FIXTURES_DIR.glob("invalid-*.json")
        }
        self.assertEqual(manifest_files, disk_files, "Fixture manifest expectations mismatch")

    def test_envelope_fixtures_match_expectations(self):
        """All 15 design envelope fixtures must strictly match their accept/reject expectation."""
        for filename, expected in self.expectations.items():
            fixture_path = FIXTURES_DIR / filename
            with open(fixture_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if expected == "accept":
                try:
                    result = validate_envelope(data)
                    self.assertIsInstance(result, dict)
                except CollectorContractError as e:
                    self.fail(f"Fixture {filename} expected accept but rejected: {e}")
            elif expected == "reject":
                with self.assertRaises(
                    CollectorContractError,
                    msg=f"Fixture {filename} expected reject but accepted",
                ):
                    validate_envelope(data)

    def test_budget_policy_fixtures(self):
        """Budget policy fixtures must enforce schema and token/call/usd cross limits."""
        valid_path = FIXTURES_DIR / "budget-valid.json"
        with open(valid_path, "r", encoding="utf-8") as f:
            valid_data = json.load(f)
        validate_budget_policy(valid_data)

        # Cross-limit violation
        cross_limit_path = FIXTURES_DIR / "budget-invalid-cross-limit.json"
        with open(cross_limit_path, "r", encoding="utf-8") as f:
            cross_data = json.load(f)
        with self.assertRaises(CollectorContractError):
            validate_budget_policy(cross_data)

        # Unknown setting
        unknown_path = FIXTURES_DIR / "budget-invalid-unknown-setting.json"
        with open(unknown_path, "r", encoding="utf-8") as f:
            unknown_data = json.load(f)
        with self.assertRaises(CollectorContractError):
            validate_budget_policy(unknown_data)

    def test_rfc8785_jcs_test_vectors(self):
        """All 11 RFC 8785 JCS number vectors must format exactly according to ECMAScript toString."""
        for raw, want in JCS_VECTORS:
            got = _jcs_number(raw)
            self.assertEqual(got, want, f"JCS number formatting failed for {raw!r}: got {got}, want {want}")

        # UTF-16 code unit ordering test from RFC 8785
        test_obj = {"b": 1, "a": [True, None, "\u20ac"], "\u00e9": 0.5}
        expected_json = '{"a":[true,null,"\u20ac"],"b":1,"\u00e9":0.5}'
        self.assertEqual(jcs(test_obj), expected_json)

    def test_canonical_fingerprint_invariants(self):
        """Canonical payload fingerprint must be invariant to key order and transmission metadata."""
        valid_path = FIXTURES_DIR / "valid-accept.json"
        with open(valid_path, "r", encoding="utf-8") as f:
            orig = json.load(f)

        hash1 = compute_payload_hash(orig)
        self.assertEqual(len(hash1), 64)

        # Transmission metadata recorded_at should NOT change payload hash
        re_recorded = json.loads(json.dumps(orig))
        re_recorded["recorded_at"] = "2026-10-20T15:30:00Z"
        hash2 = compute_payload_hash(re_recorded)
        self.assertEqual(hash1, hash2, "Transmission metadata recorded_at changed payload hash")

        # Reversed key order should NOT change payload hash
        reversed_order = {k: orig[k] for k in reversed(list(orig.keys()))}
        hash3 = compute_payload_hash(reversed_order)
        self.assertEqual(hash1, hash3, "Key ordering changed canonical payload hash")

        # Semantic change MUST change payload hash
        tampered_state = json.loads(json.dumps(orig))
        tampered_state["decision"]["state"] = "hold"
        hash4 = compute_payload_hash(tampered_state)
        self.assertNotEqual(hash1, hash4, "Semantic decision state change did not change payload hash")

        # Envelope ID change MUST change payload hash
        new_env_id = json.loads(json.dumps(orig))
        new_env_id["envelope_id"] = "00000000-0000-0000-0000-000000000000"
        hash5 = compute_payload_hash(new_env_id)
        self.assertNotEqual(hash1, hash5, "Envelope ID change did not change payload hash")

    def test_intake_fixtures_binding_and_tampering(self):
        """Intake fixtures must strictly verify auth bindings and reject scope escalations."""
        intake_dir = FIXTURES_DIR / "intake"
        if not intake_dir.exists():
            return

        expectations_file = intake_dir / "expectations.json"
        with open(expectations_file, "r", encoding="utf-8") as f:
            expectations = json.load(f)

        for filename, want_statuses in expectations.items():
            fixture_path = intake_dir / filename
            with open(fixture_path, "r", encoding="utf-8") as f:
                case = json.load(f)

            auth = AuthContext(
                tenant_id=case["auth"]["tenant_id"],
                scope=case["auth"]["session_pool"],
                principal_id=case["auth"]["principal_id"],
            )

            for idx, submission in enumerate(case["submissions"]):
                want = want_statuses[idx]
                if want == "reject:scope_binding_mismatch":
                    with self.assertRaises(ScopeBindingMismatchError, msg=f"{filename}[{idx}] expected scope mismatch"):
                        validate_envelope(submission)
                        verify_auth_binding(auth, submission)
                else:
                    # Valid or conflict submission: validate and check binding
                    validate_envelope(submission)
                    derived = verify_auth_binding(auth, submission)
                    self.assertEqual(derived["tenant_id"], case["auth"]["tenant_id"])
                    self.assertEqual(derived["scope"], case["auth"]["session_pool"])
                    self.assertEqual(derived["principal_ids"], {case["auth"]["principal_id"]})

    def test_unencodable_values_raise_contract_error_from_every_entry_point(self):
        """Lone surrogates, integers beyond 2**53 and NaN/Infinity are contract errors, not ValueError/Unicode errors."""
        with open(FIXTURES_DIR / "valid-accept.json", "r", encoding="utf-8") as f:
            valid = json.load(f)

        for label, bad in {
            "lone surrogate": "bad \ud800 text",
            "int above 2**53": 2**53 + 1,
            "int below -2**53": -(2**53) - 1,
            "nan": float("nan"),
            "infinity": float("inf"),
        }.items():
            env = json.loads(json.dumps(valid))
            env["extensions"]["x-test:value"] = bad
            for entry in (validate_envelope, compute_payload_hash, jcs):
                with self.subTest(label=label, entry=entry.__name__), self.assertRaises(CollectorContractError):
                    entry(env)

        env = json.loads(json.dumps(valid))
        env["normalized"]["text"] = "lone \udfff surrogate"
        with self.assertRaises(CollectorContractError):
            validate_envelope(env)
        with self.assertRaises(CollectorContractError):
            jcs({"\ud800": 1})

        env = json.loads(json.dumps(valid))
        env["extensions"]["x-test:value"] = 2**53
        validate_envelope(env)
        self.assertEqual(jcs(2**53), "9007199254740992")

    def test_scope_mismatch_is_not_a_contract_error(self):
        """A 403 must not be catchable by a 422 handler (and the reverse)."""
        self.assertFalse(issubclass(ScopeBindingMismatchError, CollectorContractError))
        self.assertFalse(issubclass(CollectorContractError, ScopeBindingMismatchError))
        self.assertFalse(issubclass(CollectorConflictError, CollectorContractError))
        self.assertEqual(
            [e.http_status for e in (CollectorContractError, ScopeBindingMismatchError, CollectorConflictError)],
            [422, 403, 409],
        )

    def test_malformed_leaf_values_never_leak_generic_exceptions(self):
        """Replacing any leaf of a valid envelope by a wrongly typed value is accepted or a contract error."""
        def leaf_paths(node, prefix=()):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from leaf_paths(v, prefix + (k,))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield from leaf_paths(v, prefix + (i,))
            yield prefix

        def replaced(path, value):
            env = json.loads(json.dumps(valid))
            if not path:
                return value
            cur = env
            for key in path[:-1]:
                cur = cur[key]
            cur[path[-1]] = value
            return env

        weird = [None, True, 0, 1.5, "", "x", [], {}, [[]], {"a": 1}, "\ud800", 2**60]
        for fixture in sorted(FIXTURES_DIR.glob("valid-*.json")):
            valid = json.loads(fixture.read_text(encoding="utf-8"))
            for path in leaf_paths(valid):
                for value in weird:
                    env = replaced(path, value)
                    with self.subTest(fixture=fixture.name, path=path, value=repr(value)):
                        try:
                            validate_envelope(env)
                        except CollectorContractError:
                            pass


if __name__ == "__main__":
    unittest.main()
