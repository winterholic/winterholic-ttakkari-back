"""Validation test suite for catalog_seed/*.json integrity, contracts, and claims.

Structure contract (slug, taxonomy, parameter schema) and semantic contract (evidence, numeric claims,
hardware/runtime tuples, deprecated methods) are separate. Each semantic rule is a small validator that
takes one method dict and returns violations, so it can run on the real seed and on negative fixtures.
"""

import copy
import json
import re
from pathlib import Path

import pytest

SEED_DIR = Path(__file__).parent.parent / "src/ttakkari/research/catalog_seed"
REPO_ROOT = Path(__file__).parent.parent

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

ALLOWED_EVIDENCE_KINDS = {"paper", "doc", "repo", "benchmark"}

# local_executable: the registered adapter can run this tuple on this Mac Studio (Apple Silicon, llama.cpp/Metal).
# upstream_supported: upstream docs/papers say it works, but nothing here runs it.
# different_mechanism: the runtime offers a different feature under the same idea; the method's parameters do not apply.
# external_only: results can only be registered through external_report.
# unsupported / unverified: must never be listed in the hardware/runtimes facet arrays.
COMBINATION_STATUSES = {
    "local_executable",
    "upstream_supported",
    "different_mechanism",
    "external_only",
    "unsupported",
    "unverified",
}
FACET_ELIGIBLE_STATUSES = {"local_executable", "upstream_supported", "different_mechanism", "external_only"}

# Methods whose runtime/hardware support is easy to over-read; each must spell out explicit tuples.
TUPLE_REQUIRED_SLUGS = {"qlora", "kv_cache_quantization", "flash_attention", "speculative_decoding"}

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

# Magnitude intensifiers carry a quantitative claim without a number. They need the same sourcing as digits,
# so the seed states the direction only and leaves size to the cited source or a local measurement.
INTENSIFIER_PATTERN = re.compile(
    r"대폭|획기적|극적|비약적|월등|압도적|극대화|극소화|수배|수십\s*배|수백\s*배|초고속|초극소|극도로|최상급|무손실"
)

_NUM = r"\d+(?:\.\d+)?"
_NOT_IDENT_BEFORE = r"(?<![A-Za-z0-9_.])"
# Digits that read as an effect size: relative change, speedup multiples, capacity/time quantities,
# decimal bit savings, and metric values. Plain configuration ranges (beam 1~2, 30초 window) are not matched.
NUMERIC_EFFECT_PATTERNS = [
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*(?:[~–-]\s*" + _NUM + r"\s*)?%"),
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*(?:[~–-]\s*" + _NUM + r"\s*)?배(?![가-힣])"),
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*[×x](?![A-Za-z0-9_])"),
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*(?:GB|MB|TB|ms|μs)(?![A-Za-z0-9/])"),
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*(?:마이크로초|밀리초)"),
    re.compile(_NOT_IDENT_BEFORE + r"\d+\.\d+\s*(?:비트|bit)(?![A-Za-z])"),
    re.compile(r"(?:TTFA|RTF|FID|MRR@?\d*|nDCG@?\d*|CER|WER|perplexity|pass@\d+)\s*[<>=:]?\s*" + _NUM, re.I),
    re.compile(_NOT_IDENT_BEFORE + _NUM + r"\s*(?:[~–-]\s*" + _NUM + r"\s*)?초(?![가-힣])\s*(?:이내|미만|만에|단축|소요)"),
]
# Words that mark "this number comes from a source" in the claim text itself.
SOURCE_MARKER_PATTERN = re.compile(r"논문|원논문|초록|README|공식|문서|보고|evidence|산술")
CONDITION_MARKER = "조건:"
# Local method text is checked against evidence only for claim-bearing fields. Parameter defaults/ranges/options
# are configuration, not claims; their description/label text is scanned like any other claim text.
CLAIM_PATHS = ("summary", "principle", "expected_effects", "side_effects")


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


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def claim_texts(m):
    """(path, text) pairs of the claim-bearing method text, excluding evidence entries."""
    out = []
    for key in CLAIM_PATHS:
        for i, t in enumerate(_as_list(m.get(key))):
            if isinstance(t, str):
                out.append((f"{key}[{i}]", t))
    comp = m.get("compatibility") or {}
    if isinstance(comp.get("notes"), str):
        out.append(("compatibility.notes", comp["notes"]))
    for i, c in enumerate(comp.get("combinations") or []):
        if isinstance(c.get("note"), str):
            out.append((f"compatibility.combinations[{i}].note", c["note"]))
    for i, rel in enumerate(m.get("relations") or []):
        if isinstance(rel.get("note"), str):
            out.append((f"relations[{i}].note", rel["note"]))
    for p in m.get("parameters") or []:
        for field in ("label", "description"):
            if isinstance(p.get(field), str):
                out.append((f"parameters.{p.get('key')}.{field}", p[field]))
        for j, opt in enumerate(p.get("options") or []):
            if isinstance(opt, dict) and isinstance(opt.get("label"), str):
                out.append((f"parameters.{p.get('key')}.options[{j}].label", opt["label"]))
    return out


def evidence_claims(m):
    return [(f"evidence[{i}].claim", e.get("claim", "")) for i, e in enumerate(m.get("evidence") or [])]


def numeric_effect_tokens(text):
    tokens = []
    for pat in NUMERIC_EFFECT_PATTERNS:
        for hit in pat.finditer(text):
            tokens.append(hit.group(0).strip())
    return tokens


def _number_core(token):
    """The digits of an effect token without unit, so '2.4배' in the claim matches '2.4×' in the source."""
    return re.findall(_NUM, token)


# ---------------------------------------------------------------------------
# semantic validators: each returns a list of violation strings naming slug and field
# ---------------------------------------------------------------------------

def check_evidence(m):
    slug = m["slug"]
    status = m.get("evidence_status")
    ev = m.get("evidence") or []
    v = []
    if status not in ALLOWED_EVIDENCE_STATUSES:
        v.append(f"{slug}: invalid evidence_status {status!r}")
        return v
    if not ev:
        if status != "unverified":
            v.append(f"{slug}: empty evidence requires evidence_status=unverified, got {status}")
        return v
    if status == "unverified":
        v.append(f"{slug}: unverified must not carry evidence entries")
    for i, e in enumerate(ev):
        where = f"{slug}.evidence[{i}]"
        if e.get("kind") not in ALLOWED_EVIDENCE_KINDS:
            v.append(f"{where}: invalid kind {e.get('kind')!r}")
        for key in ("title", "url", "claim"):
            if not (isinstance(e.get(key), str) and e[key].strip()):
                v.append(f"{where}: missing {key}")
        if not isinstance(e.get("year"), int):
            v.append(f"{where}: year must be int")
        url = e.get("url") or ""
        if not re.match(r"https?://", url) and url and not (REPO_ROOT / url).exists():
            v.append(f"{where}: repo-relative url does not exist: {url}")
    if status == "widely_practiced":
        for path, text in evidence_claims(m):
            if numeric_effect_tokens(text):
                v.append(f"{slug}.{path}: widely_practiced evidence must not report effect numbers: {numeric_effect_tokens(text)}")
    return v


def check_numeric_claims(m):
    """Effect numbers outside evidence need a matching, conditioned evidence claim; no intensifiers anywhere."""
    slug = m["slug"]
    status = m.get("evidence_status")
    v = []
    ev_claims = [c for _, c in evidence_claims(m)]
    for path, text in evidence_claims(m):
        tokens = numeric_effect_tokens(text)
        if tokens and CONDITION_MARKER not in text:
            v.append(f"{slug}.{path}: effect numbers {tokens} need an explicit '{CONDITION_MARKER}' (model/hardware/length)")
    for path, text in claim_texts(m):
        for token in numeric_effect_tokens(text):
            if status != "source_reported":
                v.append(f"{slug}.{path}: effect number {token!r} in a {status} method has no source")
                continue
            cores = _number_core(token)
            backing = [c for c in ev_claims if all(n in c for n in cores) and CONDITION_MARKER in c]
            if not backing:
                v.append(f"{slug}.{path}: effect number {token!r} is not backed by a conditioned evidence claim")
            elif not SOURCE_MARKER_PATTERN.search(text):
                v.append(f"{slug}.{path}: effect number {token!r} must be attributed to its source in the same text")
    for path, text in claim_texts(m) + evidence_claims(m):
        if INTENSIFIER_PATTERN.search(text):
            v.append(f"{slug}.{path}: unquantified magnitude word {INTENSIFIER_PATTERN.search(text).group(0)!r}")
        for pat in FORBIDDEN_ABSOLUTE_PATTERNS:
            if pat.search(text):
                v.append(f"{slug}.{path}: forbidden absolute claim {pat.pattern}")
    return v


def check_compat_tuples(m, adapter_lookup=None):
    slug = m["slug"]
    comp = m.get("compatibility") or {}
    combos = comp.get("combinations")
    v = []
    if slug in TUPLE_REQUIRED_SLUGS and not combos:
        v.append(f"{slug}: compatibility.combinations (explicit hardware×runtime tuples) is required")
        return v
    if not combos:
        return v
    hw_seen, rt_seen = set(), set()
    for i, c in enumerate(combos):
        where = f"{slug}.compatibility.combinations[{i}]"
        for key in ("hardware", "runtime", "status", "note"):
            if not (isinstance(c.get(key), str) and c[key].strip()):
                v.append(f"{where}: missing {key}")
        if c.get("status") not in COMBINATION_STATUSES:
            v.append(f"{where}: invalid status {c.get('status')!r}")
        if c.get("status") == "unverified" and "확인 필요" not in (c.get("note") or ""):
            v.append(f"{where}: unverified tuple must say '확인 필요'")
        if c.get("status") in FACET_ELIGIBLE_STATUSES:
            hw_seen.add(c.get("hardware"))
            rt_seen.add(c.get("runtime"))
        if c.get("status") == "local_executable" and adapter_lookup is not None:
            if not adapter_lookup(m):
                v.append(f"{where}: local_executable but no registered adapter hint runs this method/parameters")
        if c.get("status") == "external_only" and set(m.get("adapter_hints") or []) - {"external_report"}:
            v.append(f"{where}: external_only conflicts with local adapter hints")
    for hw in comp.get("hardware") or []:
        if hw not in hw_seen:
            v.append(f"{slug}: compatibility.hardware lists {hw!r} without a supported combination")
    for rt in comp.get("runtimes") or []:
        if rt not in rt_seen:
            v.append(f"{slug}: compatibility.runtimes lists {rt!r} without a supported combination")
    if slug in TUPLE_REQUIRED_SLUGS and not any(c.get("hardware") == "apple_silicon_metal" for c in combos):
        v.append(f"{slug}: must state the apple_silicon_metal status explicitly")
    return v


def _mentions_kokoro(value):
    return "kokoro" in json.dumps(value, ensure_ascii=False).lower()


def check_deprecated(m):
    """A deprecated method keeps history only: nothing a loader or readiness could treat as runnable."""
    slug = m["slug"]
    if m.get("lifecycle_status") != "deprecated":
        return []
    comp = m.get("compatibility") or {}
    v = []
    if m.get("adapter_hints"):
        v.append(f"{slug}: deprecated must have empty adapter_hints")
    if m.get("parameters"):
        v.append(f"{slug}: deprecated must not expose selectable execution parameters")
    if m.get("metrics"):
        v.append(f"{slug}: deprecated must not declare measurable metrics")
    for key in ("requires", "hardware", "runtimes"):
        if comp.get(key):
            v.append(f"{slug}: deprecated compatibility.{key} must be empty")
    if comp.get("combinations"):
        v.append(f"{slug}: deprecated must not declare hardware×runtime combinations")
    if comp.get("execution") != "unsupported":
        v.append(f"{slug}: deprecated compatibility.execution must be 'unsupported'")
    if m.get("relations"):
        v.append(f"{slug}: deprecated must not declare relations to live methods")
    return v


# ---------------------------------------------------------------------------
# structure contract
# ---------------------------------------------------------------------------

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
        keys = set()
        for p in m.get("parameters", []):
            total_params += 1
            pkey = p.get("key")
            ptype = p.get("type")
            assert pkey, f"[{m['_file']}] Missing param key in {m['slug']}"
            assert pkey not in keys, f"[{m['_file']}] Duplicate param key {pkey} in {m['slug']}"
            keys.add(pkey)
            assert ptype in ALLOWED_PARAM_TYPES, f"[{m['_file']}] Invalid param type {ptype} in {m['slug']}::{pkey}"
            assert "required" in p, f"[{m['_file']}] Missing 'required' in {m['slug']}::{pkey}"
            assert isinstance(p["required"], bool), f"[{m['_file']}] 'required' must be bool in {m['slug']}::{pkey}"
            assert "sweepable" in p, f"[{m['_file']}] Missing 'sweepable' in {m['slug']}::{pkey}"
            assert isinstance(p["sweepable"], bool), f"[{m['_file']}] 'sweepable' must be bool in {m['slug']}::{pkey}"

            if p["sweepable"]:
                sweepable_count += 1
            if p["required"]:
                required_count += 1

    # 185 before the third calibration: +1 speculative_decoding.spec_type (the only parameter the adapter runs),
    # -2 tts_kokoro_pipeline_tuning.inference_backend/sample_rate (deprecated methods expose no parameters).
    assert total_params == 184
    # Not all sweepable, not all required. The old lower bound (required > 0) was only met by
    # speculative_decoding.draft_model, which the adapter rejects, so a parameter nobody can satisfy no longer counts.
    assert 0 <= required_count < total_params, f"Required count {required_count} must be a strict subset"
    assert 0 < sweepable_count < total_params, f"Sweepable count {sweepable_count} must be realistic subset"


# ---------------------------------------------------------------------------
# semantic contract on the real seed
# ---------------------------------------------------------------------------

def test_no_unsupported_absolute_claims():
    methods = load_all_methods()
    violations = []
    for m in methods:
        for path, text in claim_texts(m) + evidence_claims(m):
            for pat in FORBIDDEN_ABSOLUTE_PATTERNS:
                if pat.search(text):
                    violations.append((m["_file"], m["slug"], path, pat.pattern))
    assert not violations, f"Forbidden absolute claims found: {violations}"


def test_empty_evidence_semantics_and_no_quantitative_claims():
    """Evidence status semantics.

    - empty evidence is only allowed for unverified methods
    - widely_practiced and source_reported both require at least one doc/repo/paper/benchmark entry
    - widely_practiced evidence states that the practice exists; it carries no effect numbers
    - unverified/widely_practiced method text carries no effect numbers at all
    """
    methods = load_all_methods()
    violations = []
    counts = {"source_reported": 0, "widely_practiced": 0, "unverified": 0}
    for m in methods:
        counts[m["evidence_status"]] += 1
        violations.extend(check_evidence(m))
    assert not violations, "\n".join(violations)
    assert counts["unverified"] > 0, "At least some uncertain/hypothetical methods must be marked 'unverified'"
    assert counts["widely_practiced"] > 0, "Standard practices should be marked 'widely_practiced'"


def test_widely_practiced_requires_citable_source():
    methods = load_all_methods()
    wp = [m for m in methods if m["evidence_status"] == "widely_practiced"]
    assert wp
    for m in wp:
        assert m.get("evidence"), f"{m['slug']}: widely_practiced requires at least one source"
        assert any(e["kind"] in {"doc", "repo", "paper", "benchmark"} for e in m["evidence"])


def test_numeric_claims_need_sources_and_conditions():
    """Every effect number anywhere in a method (not only expected_effects) is sourced with its conditions."""
    violations = []
    for m in load_all_methods():
        violations.extend(check_numeric_claims(m))
    assert not violations, "\n".join(violations)


def test_hardware_runtime_tuples_are_explicit():
    from ttakkari.research.adapters.registry import get_adapter_registry

    reg = get_adapter_registry()

    def adapter_lookup(m):
        keys = {p.get("key") for p in m.get("parameters") or []}
        for hint in m.get("adapter_hints") or []:
            ad = reg.get_adapter(hint)
            if ad is None or hint == "external_report":
                continue
            if m["slug"] in ad.supported_methods or keys & set(ad.supported_parameters):
                return True
        return False

    methods = load_all_methods()
    by_slug = {m["slug"]: m for m in methods}
    assert TUPLE_REQUIRED_SLUGS <= by_slug.keys()
    violations = []
    for m in methods:
        violations.extend(check_compat_tuples(m, adapter_lookup))
    assert not violations, "\n".join(violations)

    # Apple Silicon (this Mac Studio) status of the four easy-to-over-read methods
    def statuses(slug, hardware):
        return {
            (c["runtime"], c["status"])
            for c in by_slug[slug]["compatibility"]["combinations"]
            if c["hardware"] == hardware
        }

    assert ("llama.cpp", "local_executable") in statuses("flash_attention", "apple_silicon_metal")
    assert ("llama.cpp", "local_executable") in statuses("kv_cache_quantization", "apple_silicon_metal")
    assert ("llama.cpp", "local_executable") in statuses("speculative_decoding", "apple_silicon_metal")
    assert all(s != "local_executable" for _, s in statuses("qlora", "apple_silicon_metal"))
    # vLLM is not offered for kv_cache_quantization's q8_0/q4_0 types: different mechanism (fp8) or unverified
    assert all(
        s in {"different_mechanism", "unverified", "unsupported"}
        for rt, s in {(c["runtime"], c["status"]) for c in by_slug["kv_cache_quantization"]["compatibility"]["combinations"]}
        if rt == "vllm"
    )


def test_speculative_decoding_exposes_only_adapter_runnable_mode_as_local():
    """The adapter runs spec_type=ngram-mod only; a draft-model run must not be presented as locally executable."""
    from ttakkari.research.adapters.registry import get_adapter_registry

    m = {x["slug"]: x for x in load_all_methods()}["speculative_decoding"]
    params = {p["key"]: p for p in m["parameters"]}
    ad = get_adapter_registry().get_adapter("llm_inference")
    assert {o["value"] for o in params["spec_type"]["options"]} == {"none", "ngram-mod"}
    assert "spec_type" in ad.supported_parameters
    for key in ("draft_model", "draft_tokens"):
        assert key not in ad.supported_parameters
        assert params[key]["required"] is False
        assert "어댑터" in params[key]["description"]
        assert params[key]["sweepable"] is False


def test_kokoro_retirement_and_runtime_integrity():
    """Kokoro TTS is isolated as a deprecated record and not referenced as a runnable runtime anywhere."""
    methods = load_all_methods()
    by_slug = {m["slug"]: m for m in methods}

    kokoro_m = by_slug.get("tts_kokoro_pipeline_tuning")
    assert kokoro_m is not None, "tts_kokoro_pipeline_tuning must exist in catalog"
    assert kokoro_m.get("lifecycle_status") == "deprecated"
    assert check_deprecated(kokoro_m) == []

    deprecated = {m["slug"] for m in methods if m.get("lifecycle_status") == "deprecated"}
    assert deprecated == {"tts_kokoro_pipeline_tuning"}

    for m in methods:
        slug = m["slug"]
        if slug == "tts_kokoro_pipeline_tuning":
            continue
        comp = m.get("compatibility", {})
        for key in ("requires", "hardware", "runtimes", "combinations"):
            assert not _mentions_kokoro(comp.get(key)), f"[{m['_file']}] {slug} references Kokoro in compatibility.{key}"
        assert not _mentions_kokoro(m.get("tags")), f"[{m['_file']}] {slug} includes 'kokoro' in tags"
        assert not _mentions_kokoro(m.get("parameters")), f"[{m['_file']}] {slug} exposes Kokoro in parameters"
        for path, text in claim_texts(m):
            if "kokoro" in text.lower():
                assert re.search(r"미지원|폐기|지원되지", text), (
                    f"[{m['_file']}] {slug}.{path} mentions Kokoro without saying it is unsupported/retired"
                )
        for rel in m.get("relations", []):
            target = rel.get("target") or rel.get("to_slug")
            assert target not in deprecated, f"[{m['_file']}] {slug} relates to deprecated {target}"


# ---------------------------------------------------------------------------
# negative controls: each bad example must be flagged with its slug and field, and the good example must pass
# ---------------------------------------------------------------------------

GOOD_METHOD = {
    "slug": "good_example",
    "evidence_status": "source_reported",
    "summary": "정상 항목",
    "principle": "",
    "expected_effects": ["원논문 보고: 처리량 2.4배 개선(조건: 논문 모델·GPU)"],
    "side_effects": "",
    "compatibility": {"requires": [], "hardware": ["apple_silicon_metal"], "runtimes": ["llama.cpp"], "notes": "",
                      "combinations": [{"hardware": "apple_silicon_metal", "runtime": "llama.cpp",
                                        "status": "upstream_supported", "note": "문서 근거"}]},
    "parameters": [],
    "relations": [],
    "adapter_hints": [],
    "evidence": [{"kind": "paper", "title": "t", "url": "https://example.org/p", "year": 2022,
                  "claim": "처리량 2.4배 개선 보고 (조건: 논문 모델·GPU)"}],
}


def _bad(**overrides):
    m = copy.deepcopy(GOOD_METHOD)
    m["slug"] = "bad_example"
    for key, value in overrides.items():
        if key.startswith("comp_"):
            m["compatibility"][key[5:]] = value
        else:
            m[key] = value
    return m


def _violations(m):
    return check_evidence(m) + check_numeric_claims(m) + check_compat_tuples(m) + check_deprecated(m)


def test_validators_accept_a_well_formed_method():
    assert _violations(copy.deepcopy(GOOD_METHOD)) == []


@pytest.mark.parametrize(
    "mutation, needle",
    [
        # evidence status
        (dict(evidence=[], evidence_status="widely_practiced"), "empty evidence requires evidence_status=unverified"),
        (dict(evidence=[], evidence_status="source_reported"), "empty evidence requires evidence_status=unverified"),
        (dict(evidence_status="unverified"), "unverified must not carry evidence"),
        (dict(evidence_status="widely_practiced",
              evidence=[{"kind": "doc", "title": "t", "url": "https://example.org", "year": 2024,
                         "claim": "속도 3배 향상"}]), "widely_practiced evidence must not report effect numbers"),
        (dict(evidence=[{"kind": "blog", "title": "t", "url": "https://example.org", "year": 2024, "claim": "x"}]),
         "invalid kind"),
        # numeric claims outside expected_effects (reviewer's counterexamples)
        (dict(comp_notes="KV 50~75% 절감"), "compatibility.notes"),
        (dict(side_effects="0.37비트 추가 절감"), "side_effects"),
        (dict(parameters=[{"key": "k", "label": "k", "type": "boolean", "required": False, "sweepable": False,
                           "description": "사용 시 메모리 70% 절감"}]), "parameters.k.description"),
        (dict(expected_effects=["메모리 99GB 절감"]), "expected_effects"),
        (dict(expected_effects=["원논문 보고: 속도 5배(조건 없음 문구 제외)"]), "not backed by a conditioned evidence claim"),
        (dict(evidence=[{"kind": "paper", "title": "t", "url": "https://example.org", "year": 2022,
                         "claim": "처리량 2.4배 개선"}]), "need an explicit '조건:'"),
        (dict(expected_effects=["처리량 2.4배 개선"]), "must be attributed to its source"),
        (dict(expected_effects=["TTFA 0.70초 달성"]), "effect number"),
        # unquantified magnitude
        (dict(summary="속도가 대폭 향상됩니다"), "unquantified magnitude word"),
        # deprecated
        (dict(lifecycle_status="deprecated", adapter_hints=["tts_synthesis"]), "deprecated must have empty adapter_hints"),
        (dict(lifecycle_status="deprecated",
              parameters=[{"key": "backend", "type": "enum", "required": False, "sweepable": True}]),
         "deprecated must not expose selectable execution parameters"),
        (dict(lifecycle_status="deprecated", comp_runtimes=["kokoro"]), "deprecated compatibility.runtimes must be empty"),
        # tuples
        (dict(comp_hardware=["cuda"]), "lists 'cuda' without a supported combination"),
        (dict(comp_combinations=[{"hardware": "apple_silicon_metal", "runtime": "vllm", "status": "unverified",
                                  "note": "문서 없음"}]), "unverified tuple must say '확인 필요'"),
        (dict(comp_combinations=[{"hardware": "cuda", "runtime": "llama.cpp", "status": "works", "note": "x"}]),
         "invalid status"),
    ],
)
def test_validators_flag_negative_controls(mutation, needle):
    found = _violations(_bad(**mutation))
    assert any("bad_example" in f and needle in f for f in found), (needle, found)


def test_tuple_required_methods_without_combinations_fail():
    m = _bad(slug="flash_attention")
    m["slug"] = "flash_attention"
    m["compatibility"].pop("combinations")
    assert any("combinations" in f for f in check_compat_tuples(m))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
