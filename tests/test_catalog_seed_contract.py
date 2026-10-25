"""Validation test suite for catalog_seed/*.json integrity, contracts, and claims."""

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

ALLOWED_EVIDENCE_STATUSES = {
    "source_reported",
    "widely_practiced",
    "unverified",
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
    re.compile(r"완전(히)?\s*(제거|해결|차단|방지|무결|보장|일치)"),
    re.compile(r"완벽(히)?\s*(유지|피팅|연동|제거|동기화|일치|보장|억제)"),
    re.compile(r"(무조건|100%)\s*(보장|준수|활용|차단|제거|일치)"),
    re.compile(r"99%\s*(차단|무력화|제거|방어)"),
    re.compile(r"90%\s*(제거|차단|감소)"),
    re.compile(r"끊김\s*없는\s*오디오\s*재생\s*보장"),
    re.compile(r"지연시간\s*0초"),
]

QUANTITATIVE_EFFECT_PATTERNS = [
    re.compile(r"\d+(\.\d+)?\s*%"),
    re.compile(r"\d+(\.\d+)?\s*배"),
]


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Bypass conftest DB initialization for pure seed catalog validation."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Bypass conftest DB clean fixture for pure seed catalog validation."""
    yield


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
        relation_notes = [rel.get("note", "") for rel in m.get("relations", [])]
        comp_notes = m.get("compatibility", {}).get("notes", "") if isinstance(m.get("compatibility"), dict) else ""
        full_text = " ".join([
            m.get("summary", ""),
            m.get("principle", ""),
            " ".join(m.get("expected_effects", []) if isinstance(m.get("expected_effects"), list) else [m.get("expected_effects", "")]),
            " ".join(m.get("side_effects", []) if isinstance(m.get("side_effects"), list) else [m.get("side_effects", "")]),
            comp_notes,
            " ".join(relation_notes),
        ])
        for pat in FORBIDDEN_ABSOLUTE_PATTERNS:
            if pat.search(full_text):
                violations.append((m["_file"], m["slug"], pat.pattern))

    assert not violations, f"Forbidden absolute claims found: {violations}"


def test_empty_evidence_semantics_and_no_quantitative_claims():
    """Verify evidence semantic consistency:

    1. All evidence_status values belong to ALLOWED_EVIDENCE_STATUSES.
    2. When evidence is empty, status must not be 'source_reported'.
    3. When evidence is empty and status is 'widely_practiced' (not 'unverified'),
       expected_effects must not contain quantitative claims (% or 배).
    4. Not all empty-evidence methods are blindly marked 'widely_practiced' (unverified count > 0).
    5. Methods with evidence must be 'source_reported'.
    """
    methods = load_all_methods()
    unverified_count = 0
    widely_practiced_count = 0
    violations = []

    for m in methods:
        status = m.get("evidence_status")
        assert status in ALLOWED_EVIDENCE_STATUSES, (
            f"[{m['_file']}] Method {m['slug']} has invalid evidence_status: {status}"
        )

        ev = m.get("evidence", [])
        if not ev:
            assert status in {"widely_practiced", "unverified"}, (
                f"[{m['_file']}] Empty evidence method {m['slug']} cannot be {status}"
            )
            if status == "unverified":
                unverified_count += 1
            elif status == "widely_practiced":
                widely_practiced_count += 1
                effects = m.get("expected_effects", [])
                effects_text = " ".join(effects if isinstance(effects, list) else [effects])
                for pat in QUANTITATIVE_EFFECT_PATTERNS:
                    if pat.search(effects_text):
                        violations.append((m["_file"], m["slug"], pat.pattern, effects_text))
        else:
            assert status == "source_reported", (
                f"[{m['_file']}] Method {m['slug']} with evidence must be source_reported, got {status}"
            )

    assert not violations, f"Empty evidence widely_practiced methods have quantitative claims: {violations}"
    assert unverified_count > 0, "At least some uncertain/hypothetical methods must be marked 'unverified'"
    assert widely_practiced_count > 0, "Standard practices should be marked 'widely_practiced'"


def test_kokoro_retirement_and_runtime_integrity():
    """Verify Kokoro TTS is isolated as deprecated hypothesis and not referenced as runnable runtime."""
    methods = load_all_methods()
    by_slug = {m["slug"]: m for m in methods}

    # 1. tts_kokoro_pipeline_tuning must be deprecated and not offer execution adapters
    kokoro_m = by_slug.get("tts_kokoro_pipeline_tuning")
    assert kokoro_m is not None, "tts_kokoro_pipeline_tuning must exist in catalog"
    assert kokoro_m.get("lifecycle_status") == "deprecated", (
        "tts_kokoro_pipeline_tuning must be marked lifecycle_status='deprecated'"
    )
    assert kokoro_m.get("adapter_hints") == [], (
        "tts_kokoro_pipeline_tuning must have empty adapter_hints to prevent runnable execution"
    )

    # 2. No other method may use Kokoro in runtimes, combines_with, or tags
    for m in methods:
        slug = m["slug"]
        if slug == "tts_kokoro_pipeline_tuning":
            continue

        comp = m.get("compatibility", {})
        runtimes = [r.lower() for r in comp.get("runtimes", [])]
        assert "kokoro" not in runtimes, (
            f"[{m['_file']}] Method {slug} references Kokoro in compatibility.runtimes"
        )

        tags = [t.lower() for t in m.get("tags", [])]
        assert "kokoro" not in tags, (
            f"[{m['_file']}] Method {slug} includes 'kokoro' in tags"
        )

        for rel in m.get("relations", []):
            if (rel.get("target") or rel.get("to_slug")) == "tts_kokoro_pipeline_tuning":
                rel_type = rel.get("type")
                assert rel_type != "combines_with", (
                    f"[{m['_file']}] Method {slug} has combines_with link to deprecated Kokoro pipeline"
                )


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
    print("Running test_empty_evidence_semantics_and_no_quantitative_claims...")
    test_empty_evidence_semantics_and_no_quantitative_claims()
    print("  PASS")
    print("Running test_kokoro_retirement_and_runtime_integrity...")
    test_kokoro_retirement_and_runtime_integrity()
    print("  PASS")
    print("\nALL CATALOG SEED INTEGRITY CONTRACTS PASSED.")
