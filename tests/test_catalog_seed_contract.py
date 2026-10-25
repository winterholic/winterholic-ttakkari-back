"""Validation test suite for catalog_seed/*.json integrity, contracts, and claims."""

import fnmatch
import json
import re
from pathlib import Path
import pytest

SEED_DIR = Path(__file__).parent.parent / "src/ttakkari/research/catalog_seed"

ALLOWED_PARAM_TYPES = {
    "number",
    "integer",
    "range",
    "enum",
    "boolean",
    "multi_enum",
    "model",
    "dataset",
    "file",
    "environment",
    "text",
}

FORBIDDEN_ABSOLUTE_PATTERNS = [
    re.compile(r"오류(율)?\s*0%\s*보장"),
    re.compile(r"오류\s*박멸"),
    re.compile(r"100%\s*문법\s*준수를\s*보장"),
    re.compile(r"완벽한\s*음색\s*일치성"),
    re.compile(r"완벽한\s*구도와\s*자세"),
    re.compile(r"완벽한\s*도메인\s*어휘"),
    re.compile(r"출력\s*분포를\s*완벽히\s*유지"),
    re.compile(r"하드웨어에\s*완벽히\s*피팅"),
    re.compile(r"24GB.*65B.*미세조정|65B.*24GB.*미세조정"),
]


def load_taxonomy():
    tax_file = SEED_DIR / "taxonomy.json"
    assert tax_file.is_file(), f"Taxonomy seed missing: {tax_file}"
    tax = json.loads(tax_file.read_text(encoding="utf-8"))
    axes = {}
    for axis, items in tax.items():
        axes[axis] = {item.get("id") or item.get("code") for item in items}
    return axes


def load_all_methods():
    methods = []
    for p in sorted(SEED_DIR.glob("*.json")):
        if p.name == "taxonomy.json":
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        for m in data.get("methods", []):
            m["_file"] = p.name
            methods.append(m)
    return methods


def test_seed_files_exist_and_parseable():
    assert SEED_DIR.is_dir()
    seed_files = list(SEED_DIR.glob("*.json"))
    assert len(seed_files) >= 10
    for sf in seed_files:
        data = json.loads(sf.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


def test_taxonomy_codes_valid():
    taxes = load_taxonomy()
    methods = load_all_methods()
    assert len(methods) == 112

    singular_map = {
        "targets": "targets",
        "layers": "layers",
        "goals": "goals",
        "interventions": "interventions",
        "research_methods": "research_methods",
    }

    for m in methods:
        for plural, singular in singular_map.items():
            codes = m.get(plural, [])
            valid_codes = taxes.get(singular, set())
            for c in codes:
                assert c in valid_codes, f"[{m['_file']}] Method {m['slug']} has invalid {plural} code: {c}"


def test_relation_references_valid():
    methods = load_all_methods()
    slugs = {m["slug"] for m in methods}
    assert len(slugs) == 112, "Slug count must be unique 112"

    for m in methods:
        for rel in m.get("relations", []):
            target = rel.get("target") or rel.get("to_slug")
            assert target in slugs, f"[{m['_file']}] Method {m['slug']} references non-existent target: {target}"


def test_parameter_schemas():
    methods = load_all_methods()
    total_params = 0
    sweepable_count = 0
    required_count = 0

    for m in methods:
        for p in m.get("parameters", []):
            total_params += 1
            pkey = p.get("key")
            ptype = p.get("type")
            assert pkey, f"[{m['_file']}] Missing param key in {m['slug']}"
            assert ptype in ALLOWED_PARAM_TYPES, f"[{m['_file']}] Invalid param type {ptype} in {m['slug']}::{pkey}"
            assert "required" in p, f"[{m['_file']}] Missing 'required' in {m['slug']}::{pkey}"
            assert isinstance(p["required"], bool), f"[{m['_file']}] 'required' must be bool in {m['slug']}::{pkey}"
            assert "sweepable" in p, f"[{m['_file']}] Missing 'sweepable' in {m['slug']}::{pkey}"
            assert isinstance(p["sweepable"], bool), f"[{m['_file']}] 'sweepable' must be bool in {m['slug']}::{pkey}"

            if p["sweepable"]:
                sweepable_count += 1
            if p["required"]:
                required_count += 1

    assert total_params == 185
    # Enforce realistic ratio: not all sweepable, not all required
    assert 0 < required_count < total_params, f"Required count {required_count} must be realistic subset"
    assert 0 < sweepable_count < total_params, f"Sweepable count {sweepable_count} must be realistic subset"


def test_no_unsupported_absolute_claims():
    methods = load_all_methods()
    violations = []
    for m in methods:
        full_text = " ".join([
            m.get("summary", ""),
            m.get("principle", ""),
            " ".join(m.get("expected_effects", []) if isinstance(m.get("expected_effects"), list) else [m.get("expected_effects", "")]),
            " ".join(m.get("side_effects", []) if isinstance(m.get("side_effects"), list) else [m.get("side_effects", "")]),
        ])
        for pat in FORBIDDEN_ABSOLUTE_PATTERNS:
            if pat.search(full_text):
                violations.append((m["_file"], m["slug"], pat.pattern))

    assert not violations, f"Forbidden absolute claims found: {violations}"


if __name__ == "__main__":
    print("Running test_seed_files_exist_and_parseable...")
    test_seed_files_exist_and_parseable()
    print("  PASS")
    print("Running test_taxonomy_codes_valid...")
    test_taxonomy_codes_valid()
    print("  PASS")
    print("Running test_relation_references_valid...")
    test_relation_references_valid()
    print("  PASS")
    print("Running test_parameter_schemas...")
    test_parameter_schemas()
    print("  PASS")
    print("Running test_no_unsupported_absolute_claims...")
    test_no_unsupported_absolute_claims()
    print("  PASS")
    print("\nALL CATALOG SEED INTEGRITY CONTRACTS PASSED.")

