"""Tests for Platform V2 Backend Audit Scanner (coverage reconciliation contract).

DB-free scanner tests. 계약 요약:
- 고정 최소 개수(sanity floor)는 휴리스틱일 뿐, coverage 판정 근거가 아니다.
- coverage=reconciled 는 독립 discovery 후보 집합 + root/source fingerprint + 수집 record ID가
  모두 일치하고 source binding이 확인된 범주에만 부여한다. 독립 discovery가 불가능하거나
  source binding이 불명하면 coverage=unknown 이 정답이다(예: routes).
- 단순 개수 일치, 같은 개수의 중복/치환, wrong root, import source 불일치는 reconciled가 될 수 없다.
- extraction 성공 / 분류 / 수집 범위 / coverage는 서로 다른 필드이며 DB/제품 runtime 건강과 섞지 않는다.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import scripts.platform_v2_inventory as pvi
from scripts.platform_v2_inventory import (
    MODULE_DEFINITIONS,
    AuditContext,
    DiscoveryResult,
    analyze_import_dependencies,
    classify_config,
    classify_mcp_tool,
    classify_migration,
    classify_route,
    classify_source_file,
    classify_table,
    discover_config_fields,
    discover_migrations,
    discover_routes,
    discover_source_files,
    discover_tables,
    discover_tools,
    evaluate_import_binding,
    extract_alembic_migrations,
    extract_db_tables,
    extract_source_files,
    reconcile_category,
    run_audit,
)

CATEGORIES = ("routes", "tools", "tables", "migrations", "configs", "source_files")
REPO_ROOT = Path(__file__).resolve().parent.parent


# ==============================================================================
# Helpers / fixtures
# ==============================================================================


def _build_mini_repo(root: Path) -> AuditContext:
    pkg = root / "src" / "ttakkari"
    (pkg / "orchestrator").mkdir(parents=True)
    versions = root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "orchestrator" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.py").write_text(
        "from pydantic_settings import BaseSettings\n\n\n"
        "class Settings(BaseSettings):\n    alpha: int = 1\n    beta: str = 'x'\n",
        encoding="utf-8",
    )
    (pkg / "models.py").write_text(
        "class A:\n    __tablename__ = 'alpha_things'\n\n\nclass B:\n    __tablename__ = 'beta_things'\n",
        encoding="utf-8",
    )
    (pkg / "orchestrator" / "mcp_server.py").write_text(
        "server = object()\n\n\n@server.tool()\nasync def tool_one():\n    return 1\n\n\n"
        "@server.tool()\nasync def tool_two():\n    return 2\n",
        encoding="utf-8",
    )
    (pkg / "caller.py").write_text(
        "from ttakkari.models import A\nfrom ttakkari.config import Settings\n",
        encoding="utf-8",
    )
    (versions / "0001_initial.py").write_text(
        '"""initial"""\nfrom alembic import op\nimport sqlalchemy as sa\n\nrevision = "0001"\ndown_revision = None\n\n\n'
        'def upgrade():\n    op.create_table("alpha_things", sa.Column("id", sa.Integer()))\n\n\n'
        'def downgrade():\n    op.drop_table("alpha_things")\n',
        encoding="utf-8",
    )
    (versions / "0002_second.py").write_text(
        '"""second"""\nfrom alembic import op\nimport sqlalchemy as sa\n\nrevision = "0002"\ndown_revision = "0001"\n\n\n'
        'def upgrade():\n    op.create_table("beta_things", sa.Column("id", sa.Integer()))\n\n\n'
        'def downgrade():\n    op.drop_table("beta_things")\n',
        encoding="utf-8",
    )
    return AuditContext(
        repo_root=root, src_dir=root / "src", alembic_dir=root / "alembic"
    )


@pytest.fixture()
def mini_ctx(tmp_path: Path) -> AuditContext:
    return _build_mini_repo(tmp_path / "mini")


def _source_ids_and_hashes(ctx: AuditContext) -> tuple[list[str], dict[str, str]]:
    files, _, _ = extract_source_files(ctx)
    return sorted(files), {k: v["sha256"] for k, v in files.items()}


def _real_snapshot() -> dict[str, Any]:
    """실제 트리의 extractor 원본 출력 (monkeypatch 전에 호출)."""
    return {
        "routes": pvi.extract_routes(),
        "tools": pvi.extract_mcp_tools(),
        "tables": pvi.extract_db_tables(),
        "migrations": pvi.extract_alembic_migrations(),
        "configs": pvi.extract_config_fields(),
        "sources": pvi.extract_source_files(),
    }


@pytest.fixture(scope="module")
def real():
    return _real_snapshot()


def _cov(data: dict[str, Any], cat: str) -> dict[str, Any]:
    return data["scan_report"]["coverage"]["by_category"][cat]


def _states(data: dict[str, Any]) -> dict[str, str]:
    return {c: _cov(data, c)["state"] for c in CATEGORIES}


def _ids_of(cat: str, real_data: dict[str, Any]) -> list[str]:
    if cat == "tools":
        return sorted(real_data["tools"])
    if cat == "tables":
        return sorted(real_data["tables"])
    if cat == "configs":
        return sorted(real_data["configs"])
    raise AssertionError(cat)


# ==============================================================================
# 0. 실행 증거: 어떤 source를 import했는지
# ==============================================================================


def test_scanner_and_ttakkari_import_from_this_worktree():
    """source import __file__ 이 이 worktree 안인지 확인한다 (다른 checkout 실행 방지)."""
    import ttakkari.config as tcfg

    assert Path(pvi.__file__).resolve().is_relative_to(REPO_ROOT)
    assert Path(tcfg.__file__).resolve().is_relative_to(REPO_ROOT / "src")


# ==============================================================================
# 1. 기존 분류기 / 정확 개수 회귀 (corrected AST counts / Alembic errors / SQL call-site)
# ==============================================================================


def test_unknown_inputs_negative_controls():
    assert classify_route("GET", "/api/alien_service/teleport") == "unmatched"
    assert classify_mcp_tool("quantum_hyperdrive_engage") == "unmatched"
    assert classify_table("galactic_wormhole_records") == "unmatched"
    assert classify_migration("9999_brand_new_alien_migration") == "unmatched"
    assert classify_config("brand_new_unseen_alien_setting_field") == "unmatched"
    assert classify_source_file("src/ttakkari/alien/teleport.py") == "unmatched"
    assert "unknown_plane.alien_module" not in MODULE_DEFINITIONS


def test_import_exact_count_fixture(mini_ctx):
    """ImportFrom 구문 1개는 대상 파일 count를 정확히 1회만 올린다 (이중 누적 회귀 방지)."""
    files, _, _ = extract_source_files(mini_ctx)
    _, counts, _, _, _ = analyze_import_dependencies(files, mini_ctx)
    assert counts.get("src/ttakkari/models.py") == 1
    assert counts.get("src/ttakkari/config.py") == 1
    assert counts.get("src/ttakkari/caller.py", 0) == 0


def test_alembic_annotated_down_revisions_restored():
    migrations, crosscheck_errs = extract_alembic_migrations()
    assert crosscheck_errs == []
    base = next(m for m in migrations if m["revision"] == "0001")
    assert base["down_revision"] is None
    missing_down = [
        m["stem"]
        for m in migrations
        if m["revision"] != "0001" and m["down_revision"] is None
    ]
    assert missing_down == []


def test_alembic_ddl_call_sites_categorized():
    migrations, _ = extract_alembic_migrations()
    sites = [cs for m in migrations for cs in m.get("ddl_call_sites", [])]
    assert len(sites) >= 70
    kinds = {cs["kind"] for cs in sites}
    assert {"create_table", "execute"} <= kinds
    executes = [cs for cs in sites if cs["kind"] == "execute"]
    assert {cs["sql_classification"] for cs in executes} <= {
        "static_literal",
        "parameterized",
        "interpolated",
        "unknown",
    }


def test_artcraft_tasks_foreign_key_session_id():
    tables = extract_db_tables()
    fks = tables["artcraft_tasks"]["foreign_keys"]
    assert (
        len(
            [
                fk
                for fk in fks
                if fk["column"] == "session_id" and "sessions.id" in fk["target"]
            ]
        )
        == 1
    )


def test_delivery_plane_and_ops_plane_reclassification():
    assert MODULE_DEFINITIONS["delivery.git_cas"]["plane"] == "Delivery"
    assert MODULE_DEFINITIONS["ops.monitoring"]["plane"] == "Control"


# ==============================================================================
# 2. reconcile_category 순수 단위 (fixture root 기준)
# ==============================================================================


def test_sanity_floor_is_decoupled_from_coverage(mini_ctx):
    """floor를 그대로 둔 소규모 fixture도 ID/fingerprint 일치면 reconciled. floor 위반은 경고일 뿐이다."""
    assert all(
        v > 0 for v in mini_ctx.sanity_floor_counts.values()
    )  # 0으로 낮춰 통과시키지 않는다
    ids, hashes = _source_ids_and_hashes(mini_ctx)
    cov = reconcile_category(
        "source_files",
        ids,
        discover_source_files(mini_ctx),
        None,
        collected_hashes=hashes,
    )
    assert cov["state"] == "reconciled", cov


def test_sources_nonempty_subset_is_mismatch(mini_ctx):
    ids, hashes = _source_ids_and_hashes(mini_ctx)
    dropped = ids[-1]
    cov = reconcile_category(
        "source_files",
        ids[:-1],
        discover_source_files(mini_ctx),
        None,
        collected_hashes={k: v for k, v in hashes.items() if k != dropped},
    )
    assert cov["state"] == "mismatch"
    assert cov["missing_from_collected"] == [dropped]


def test_same_count_duplicate_and_substitution_is_mismatch(mini_ctx):
    ids, hashes = _source_ids_and_hashes(mini_ctx)
    tampered = list(ids)
    tampered[-1] = tampered[0]  # 같은 개수, 마지막은 중복으로 치환
    assert len(tampered) == len(ids)
    cov = reconcile_category(
        "source_files",
        tampered,
        discover_source_files(mini_ctx),
        None,
        collected_hashes=hashes,
    )
    assert cov["state"] == "mismatch"
    assert cov["duplicates_in_collected"] == [ids[0]]
    assert cov["missing_from_collected"] == [ids[-1]]

    substituted = list(ids[:-1]) + ["src/ttakkari/ghost.py"]
    cov = reconcile_category(
        "source_files",
        substituted,
        discover_source_files(mini_ctx),
        None,
        collected_hashes=hashes,
    )
    assert cov["state"] == "mismatch"
    assert cov["extra_in_collected"] == ["src/ttakkari/ghost.py"]
    assert cov["missing_from_collected"] == [ids[-1]]


def test_same_ids_but_different_content_fingerprint_is_mismatch(mini_ctx):
    ids, hashes = _source_ids_and_hashes(mini_ctx)
    bad = dict(hashes)
    bad[ids[0]] = "0" * 64
    cov = reconcile_category(
        "source_files", ids, discover_source_files(mini_ctx), None, collected_hashes=bad
    )
    assert cov["state"] == "mismatch"
    assert cov["fingerprint_mismatches"] == [ids[0]]


def test_missing_collected_hash_cannot_be_reconciled(mini_ctx):
    ids, _ = _source_ids_and_hashes(mini_ctx)
    cov = reconcile_category(
        "source_files",
        ids,
        discover_source_files(mini_ctx),
        None,
        collected_hashes=None,
    )
    assert cov["state"] == "unknown"


def test_empty_equals_empty_is_not_reconciled(tmp_path):
    ctx = AuditContext(
        repo_root=tmp_path, src_dir=tmp_path / "src", alembic_dir=tmp_path / "alembic"
    )
    (tmp_path / "src").mkdir()
    cov = reconcile_category(
        "source_files", [], discover_source_files(ctx), None, collected_hashes={}
    )
    assert cov["state"] == "unknown"


def test_discovery_errors_make_coverage_unknown(tmp_path):
    ctx = AuditContext(
        repo_root=tmp_path,
        src_dir=tmp_path / "missing_src",
        alembic_dir=tmp_path / "missing_alembic",
    )
    for disc in (
        discover_source_files(ctx),
        discover_migrations(ctx),
        discover_config_fields(ctx),
        discover_tables(ctx),
        discover_tools(ctx),
    ):
        assert disc.errors, disc.category
        cov = reconcile_category(
            disc.category, [], disc, {"state": "bound", "reasons": []}
        )
        assert cov["state"] == "unknown"


def test_unbound_import_source_blocks_reconciliation(mini_ctx):
    disc = discover_config_fields(mini_ctx)
    assert disc.candidate_ids == ["alpha", "beta"]
    bound = reconcile_category(
        "configs", ["alpha", "beta"], disc, {"state": "bound", "reasons": []}
    )
    assert bound["state"] == "reconciled"
    for state in ("unbound", "unknown"):
        cov = reconcile_category(
            "configs",
            ["alpha", "beta"],
            disc,
            {"state": state, "reasons": ["root 불일치"]},
        )
        assert cov["state"] == "unknown"
        assert any("binding" in r for r in cov["reasons"])


def test_discovery_on_fixture_is_independent_of_extractors(mini_ctx):
    assert discover_tables(mini_ctx).candidate_ids == ["alpha_things", "beta_things"]
    assert discover_tools(mini_ctx).candidate_ids == ["tool_one", "tool_two"]
    mig = discover_migrations(mini_ctx)
    assert mig.candidate_ids == ["0001_initial.py::0001", "0002_second.py::0002"]


def test_dynamic_table_names_are_unresolved_not_silently_reconciled(tmp_path):
    ctx = _build_mini_repo(tmp_path / "dyn")
    (ctx.src_dir / "ttakkari" / "dyn_models.py").write_text(
        "def make(name):\n    class C:\n        __tablename__ = name\n    return C\n",
        encoding="utf-8",
    )
    disc = discover_tables(ctx)
    assert disc.unresolved, "동적 __tablename__ 은 unresolved ledger에 남아야 한다"
    cov = reconcile_category(
        "tables",
        ["alpha_things", "beta_things"],
        disc,
        {"state": "bound", "reasons": []},
    )
    assert cov["state"] == "unknown"


def test_routes_have_no_independent_discovery():
    disc = discover_routes(AuditContext())
    assert disc.candidate_ids is None
    assert disc.unresolved
    cov = reconcile_category(
        "routes", ["GET /a"], disc, {"state": "bound", "reasons": []}
    )
    assert cov["state"] == "unknown"
    assert cov["auxiliary"], (
        "decorator site 수 등 보조 신호는 기록하되 보증 근거로 쓰지 않는다"
    )


# ==============================================================================
# 3. import root binding
# ==============================================================================


def test_real_tree_paths_do_not_attest_loaded_source_bytes():
    run_audit()  # extractor import 유발
    b = evaluate_import_binding(AuditContext())
    assert b["state"] == "unknown", b
    assert "source-byte identity is unverified" in b["reasons"][0]


def test_binding_unbound_when_context_root_differs(mini_ctx):
    """context root가 Settings/MCP/ORM import root로 전파되지 않으면 binding은 unbound."""
    run_audit()  # 실제 root의 ttakkari를 import해 둔다
    b = evaluate_import_binding(mini_ctx)
    assert b["state"] == "unbound"
    assert b["outside_root"], "context root 밖에서 import된 모듈 목록이 있어야 한다"
    for cat in ("configs", "tables", "tools", "routes"):
        assert b["categories"][cat]["state"] == "unbound"


def test_binding_unbound_when_foreign_module_loaded(monkeypatch, tmp_path):
    run_audit()
    foreign = tmp_path / "elsewhere" / "zz_outside.py"
    foreign.parent.mkdir()
    foreign.write_text("", encoding="utf-8")
    mod = types.ModuleType("ttakkari.zz_outside")
    mod.__file__ = str(foreign)
    monkeypatch.setitem(sys.modules, "ttakkari.zz_outside", mod)
    b = evaluate_import_binding(AuditContext())
    assert b["state"] == "unbound"
    assert any("zz_outside" in x for x in b["outside_root"])


# ==============================================================================
# 4. run_audit: 실제 트리 + 축소/치환 주입 (fixture min을 0으로 낮추지 않는다)
# ==============================================================================


def test_real_tree_reconciliation_states(real):
    data = run_audit()
    states = _states(data)
    assert states["source_files"] == "reconciled"
    assert states["migrations"] == "reconciled"
    assert states["configs"] == "unknown"  # runtime source-byte binding unverified
    assert states["tables"] == "unknown"  # runtime source-byte binding unverified
    assert states["tools"] == "unknown"  # runtime source-byte binding unverified
    # routes는 독립 discovery가 없으므로 unknown이 올바른 결과다.
    assert states["routes"] == "unknown"
    assert data["scan_report"]["coverage"]["all_reconciled"] is False
    assert data["scan_report"]["verdict"] == "coverage_partially_unverified"


@pytest.mark.parametrize("keep", [496, 400, 1])
def test_routes_nonempty_subset_stays_unknown(monkeypatch, real, keep):
    """497->496/400 같은 nonempty 축소도, 전체 497도 routes는 unknown (독립 route discovery 없음)."""
    full = real["routes"]
    subset = dict(list(full.items())[:keep])
    monkeypatch.setattr(pvi, "extract_routes", lambda ctx=None: subset)
    data = run_audit()
    assert _cov(data, "routes")["state"] == "unknown"
    assert data["scan_report"]["coverage"]["all_reconciled"] is False
    assert data["scan_report"]["verdict"] != "all_categories_reconciled"


def test_tools_tables_configs_subset_by_one_is_mismatch(monkeypatch, real):
    tools = sorted(real["tools"])[:-1]
    tables = dict(list(real["tables"].items())[:-1])
    configs = sorted(real["configs"])[:-1]
    monkeypatch.setattr(pvi, "extract_mcp_tools", lambda ctx=None: tools)
    monkeypatch.setattr(pvi, "extract_db_tables", lambda ctx=None: tables)
    monkeypatch.setattr(pvi, "extract_config_fields", lambda ctx=None: configs)
    data = run_audit()
    for cat in ("tools", "tables", "configs"):
        cov = _cov(data, cat)
        assert cov["state"] == "unknown", (cat, cov)
        assert cov["static_state"] == "mismatch", (cat, cov)
        assert len(cov["missing_from_collected"]) == 1
    assert data["scan_report"]["verdict"] == "coverage_partially_unverified"


def test_subset_still_mismatch_with_floor_zero(monkeypatch, real):
    """floor를 0으로 낮춰도 subset은 mismatch, 이 방향으로 reconciled가 되지 않는다."""
    ctx = AuditContext(
        sanity_floor_counts={
            k: 0
            for k in (
                "routes",
                "tools",
                "tables",
                "migrations",
                "configs",
                "source_files",
            )
        }
    )
    tools = sorted(real["tools"])[:-1]
    monkeypatch.setattr(pvi, "extract_mcp_tools", lambda c=None: tools)
    data = run_audit(ctx)
    assert _cov(data, "tools")["state"] == "unknown"
    assert _cov(data, "tools")["static_state"] == "mismatch"


def test_same_count_duplicate_replacement_in_run_audit(monkeypatch, real):
    tools = sorted(real["tools"])
    tools[-1] = tools[0]  # 같은 개수, 중복 치환
    configs = sorted(real["configs"])
    configs[-1] = "totally_unknown_replacement_field"  # 같은 개수, 치환
    monkeypatch.setattr(pvi, "extract_mcp_tools", lambda ctx=None: tools)
    monkeypatch.setattr(pvi, "extract_config_fields", lambda ctx=None: configs)
    data = run_audit()
    t = _cov(data, "tools")
    assert t["state"] == "unknown" and t["duplicates_in_collected"] == [tools[0]]
    assert t["static_state"] == "mismatch"
    c = _cov(data, "configs")
    assert c["state"] == "unknown"
    assert c["static_state"] == "mismatch"
    assert c["extra_in_collected"] == ["totally_unknown_replacement_field"]
    assert len(c["missing_from_collected"]) == 1


def test_migrations_subset_and_sources_subset_are_mismatch(monkeypatch, real):
    migs, fails = real["migrations"]
    files, perrs, mroots = real["sources"]
    monkeypatch.setattr(
        pvi, "extract_alembic_migrations", lambda ctx=None: (migs[:-1], list(fails))
    )
    monkeypatch.setattr(
        pvi,
        "extract_source_files",
        lambda ctx=None: (dict(list(files.items())[:-1]), list(perrs), list(mroots)),
    )
    data = run_audit()
    assert _cov(data, "migrations")["state"] == "mismatch"
    assert _cov(data, "source_files")["state"] == "mismatch"


def test_source_content_replacement_same_ids_is_mismatch(monkeypatch, real):
    files, perrs, mroots = real["sources"]
    tampered = {k: dict(v) for k, v in files.items()}
    first = min(tampered)
    tampered[first]["sha256"] = "f" * 64
    monkeypatch.setattr(
        pvi,
        "extract_source_files",
        lambda ctx=None: (tampered, list(perrs), list(mroots)),
    )
    data = run_audit()
    cov = _cov(data, "source_files")
    assert cov["state"] == "mismatch"
    assert cov["fingerprint_mismatches"] == [first]


# ==============================================================================
# 5. wrong root / import source 違い / missing / parse / crosscheck
# ==============================================================================


def test_wrong_root_context_does_not_certify_import_categories(mini_ctx):
    """ctx root만 fixture로 바꾸면 Settings/MCP/ORM/routes는 실제 root에서 import되므로 unknown이어야 한다."""
    data = run_audit(mini_ctx)
    states = _states(data)
    assert (
        states["source_files"] == "reconciled"
    )  # fixture root를 파일 시스템으로 직접 검증
    assert states["migrations"] == "reconciled"
    for cat in ("configs", "tables", "tools", "routes"):
        assert states[cat] == "unknown", (cat, states)
        assert any("binding" in r for r in _cov(data, cat)["reasons"])
    report = data["scan_report"]
    assert report["target"]["claims_product_inventory"] is False
    assert report["coverage"]["all_reconciled"] is False
    assert any("binding" in item["reason"] for item in report["unresolved_ledger"])


def test_foreign_import_source_makes_import_categories_unknown(
    monkeypatch, tmp_path, real
):
    foreign = tmp_path / "other_checkout" / "ttakkari_other.py"
    foreign.parent.mkdir()
    foreign.write_text("", encoding="utf-8")
    mod = types.ModuleType("ttakkari.zz_foreign")
    mod.__file__ = str(foreign)
    monkeypatch.setitem(sys.modules, "ttakkari.zz_foreign", mod)
    data = run_audit()
    states = _states(data)
    for cat in ("configs", "tables", "tools", "routes"):
        assert states[cat] == "unknown", states
    assert states["source_files"] == "reconciled"  # 파일시스템 범주는 영향받지 않는다


def test_missing_roots_report_extraction_failure_and_unknown(tmp_path):
    ctx = AuditContext(
        repo_root=tmp_path,
        src_dir=tmp_path / "no_src",
        alembic_dir=tmp_path / "no_alembic",
    )
    data = run_audit(ctx)
    ext = data["scan_report"]["extraction"]["by_category"]
    assert ext["source_files"]["state"] == "failed"
    assert ext["migrations"]["state"] == "failed"
    assert _cov(data, "source_files")["state"] == "unknown"
    assert _cov(data, "migrations")["state"] == "unknown"
    assert data["scan_report"]["extraction"]["ok"] is False
    assert data["scan_report"]["verdict"] == "extraction_failed"


def test_parse_error_is_extraction_error_not_success(mini_ctx):
    (mini_ctx.src_dir / "ttakkari" / "broken.py").write_text(
        "def broken(:\n", encoding="utf-8"
    )
    data = run_audit(mini_ctx)
    ext = data["scan_report"]["extraction"]["by_category"]["source_files"]
    assert ext["state"] == "succeeded_with_errors"
    assert any("broken.py" in e for e in ext["errors"])
    assert data["scan_report"]["extraction"]["ok"] is False
    assert data["scan_report"]["verdict"] == "extraction_failed"
    assert any(
        "broken.py" in item["item"] for item in data["scan_report"]["unresolved_ledger"]
    )


def test_alembic_script_directory_failure_reported(monkeypatch):
    from alembic.script import ScriptDirectory

    def _fail(*args, **kwargs):
        raise RuntimeError("Simulated ScriptDirectory filesystem failure")

    monkeypatch.setattr(ScriptDirectory, "from_config", _fail)
    data = run_audit()
    ext = data["scan_report"]["extraction"]["by_category"]["migrations"]
    assert any("ScriptDirectory" in e for e in ext["errors"])
    assert _cov(data, "migrations")["state"] == "unknown"
    assert data["scan_report"]["verdict"] != "all_categories_reconciled"


def test_filename_fallback_revision_is_reported(tmp_path):
    ctx = _build_mini_repo(tmp_path / "fb")
    (ctx.alembic_dir / "versions" / "0003_noreview.py").write_text(
        "def upgrade():\n    pass\n", encoding="utf-8"
    )
    data = run_audit(ctx)
    ext = data["scan_report"]["extraction"]["by_category"]["migrations"]
    assert any("fallback" in e for e in ext["errors"])
    assert data["scan_report"]["verdict"] != "all_categories_reconciled"


# ==============================================================================
# 6. Schema 분리: extraction / classification / collection scope / coverage
# ==============================================================================


def test_report_schema_separates_extraction_classification_scope_coverage():
    data = run_audit()
    report = data["scan_report"]
    for key in (
        "extraction",
        "classification",
        "collection_scope",
        "coverage",
        "sanity_heuristics",
        "unresolved_ledger",
        "target",
        "verdict",
        "scope_note",
    ):
        assert key in report, key
    assert set(report["extraction"]["by_category"]) == set(CATEGORIES)
    assert set(report["classification"]["by_category"]) == set(CATEGORIES)
    assert set(report["collection_scope"]["by_category"]) == set(CATEGORIES)
    assert set(report["coverage"]["by_category"]) == set(CATEGORIES)
    for cat in CATEGORIES:
        scope = report["collection_scope"]["by_category"][cat]
        assert scope["collected_count"] == data["collected_counts"][cat]
        assert len(scope["collected_ids_sha256"]) == 64
        cls = report["classification"]["by_category"][cat]
        assert cls["classified"] + cls["unmatched"] == cls["collected"]
    # 분류 완료(unmatched=0)는 coverage 증명과 별개다.
    assert report["classification"]["all_collected_classified"] is True
    assert report["coverage"]["all_reconciled"] is False


def test_report_never_uses_complete_vocabulary_or_product_health():
    data = run_audit()
    blob = json.dumps(data["scan_report"], ensure_ascii=False).lower()
    assert "bounded_complete" not in blob
    assert '"status": "complete"' not in blob
    assert "audit_health" not in data
    assert "denominators" not in data
    for forbidden in (
        "db_health",
        "runtime_health",
        "product_health",
        "service_health",
    ):
        assert forbidden not in blob
    assert (
        "건강" in data["scan_report"]["scope_note"]
        or "health" in data["scan_report"]["scope_note"].lower()
    )


def test_sanity_floor_is_recorded_as_heuristic_only(monkeypatch, real):
    tools = sorted(real["tools"])
    monkeypatch.setattr(pvi, "extract_mcp_tools", lambda ctx=None: tools[:-1])
    data = run_audit()
    sanity = data["scan_report"]["sanity_heuristics"]
    assert sanity["used_as_coverage_evidence"] is False
    assert (
        sanity["floor_counts"]["tools"] == AuditContext().sanity_floor_counts["tools"]
    )
    # floor(80) 위에 있는 101개여도 coverage는 floor와 무관하게 mismatch
    assert _cov(data, "tools")["state"] == "unknown"
    assert _cov(data, "tools")["static_state"] == "mismatch"
    assert "tools" not in sanity["below_floor"]


def test_scan_target_records_tree_hash_and_manifest(mini_ctx):
    # Test Git binding against an owned fixture, including in deployment archives
    # that intentionally do not contain the development repository's .git.
    from ttakkari.delivery.worktree import run_git

    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Audit fixture"],
        ["config", "user.email", "fixture@example.invalid"],
        ["add", "."],
        ["commit", "-m", "audit inputs"],
    ):
        run_git(args, mini_ctx.repo_root)
    data = run_audit(mini_ctx)
    target = data["scan_report"]["target"]
    assert target["scanner_file"].endswith("scripts/platform_v2_inventory.py")
    assert target["git"]["available"] is True
    assert len(target["git"]["head"]) == 40
    assert len(target["git"]["src_tree"]) == 40
    assert target["git"]["src_dirty"] is False
    assert target["declared_baseline"]["src_tree_matches_scanned"] in (
        True,
        False,
        None,
    )
    manifest = data["source_manifest"]
    assert len(manifest) == data["collected_counts"]["source_files"]
    assert all(len(v) == 64 for v in manifest.values())
    # 모듈 행은 고정 baseline 상수가 아니라 실제 스캔 대상의 tree를 가리킨다.
    entry = data["inventory"]["core.auth_session"]
    assert "sha" not in entry
    assert entry["src_tree"] == target["git"]["src_tree"]
    assert entry["scan_commit"] == target["git"]["head"]


# ==============================================================================
# 7. CLI 출력 계약
# ==============================================================================


def _report(**over: Any) -> dict[str, Any]:
    cov = {c: {"state": "reconciled", "reasons": []} for c in CATEGORIES}
    base = {
        "extraction": {
            "ok": True,
            "by_category": {
                c: {"state": "succeeded", "errors": []} for c in CATEGORIES
            },
        },
        "classification": {"all_collected_classified": True, "by_category": {}},
        "coverage": {
            "by_category": cov,
            "all_reconciled": True,
            "summary": {"reconciled": list(CATEGORIES), "unknown": [], "mismatch": []},
        },
        "verdict": "all_categories_reconciled",
        "unresolved_ledger": [],
    }
    base.update(over)
    return base


def test_cli_exit_codes_and_wording():
    lines, code = pvi.format_cli_summary(_report())
    text = "\n".join(lines)
    assert code == 0
    assert "수집 범위 한정" in text
    assert "전 분모" not in text and "SUCCESS" not in text

    unknown = _report()
    unknown["coverage"] = {
        "by_category": {
            **unknown["coverage"]["by_category"],
            "routes": {"state": "unknown", "reasons": ["x"]},
        },
        "all_reconciled": False,
        "summary": {
            "reconciled": [c for c in CATEGORIES if c != "routes"],
            "unknown": ["routes"],
            "mismatch": [],
        },
    }
    unknown["verdict"] = "coverage_partially_unverified"
    lines, code = pvi.format_cli_summary(unknown)
    text = "\n".join(lines)
    assert code == 2
    assert "routes" in text and "unknown" in text
    assert "완전" not in text and "SUCCESS" not in text

    mismatch = _report()
    mismatch["coverage"] = {
        "by_category": {
            **mismatch["coverage"]["by_category"],
            "tools": {"state": "mismatch", "reasons": ["x"]},
        },
        "all_reconciled": False,
        "summary": {"reconciled": [], "unknown": [], "mismatch": ["tools"]},
    }
    mismatch["verdict"] = "coverage_mismatch"
    assert pvi.format_cli_summary(mismatch)[1] == 1

    failed = _report()
    failed["extraction"] = {
        "ok": False,
        "by_category": {c: {"state": "failed", "errors": ["e"]} for c in CATEGORIES},
    }
    failed["verdict"] = "extraction_failed"
    assert pvi.format_cli_summary(failed)[1] == 1


def test_main_prints_unverified_summary_for_real_tree(monkeypatch, capsys, tmp_path):
    real_ctx = AuditContext()
    out_ctx = dataclasses.replace(real_ctx, repo_root=tmp_path)
    monkeypatch.setattr(pvi, "AuditContext", lambda: out_ctx)
    # 출력 root만 tmp로 바꾸고 scan root(src/alembic)는 실제 값을 유지한다.
    code = pvi.main([])
    captured = capsys.readouterr().out
    assert code == 2
    assert "routes" in captured and "unknown" in captured
    assert "SUCCESS" not in captured and "bounded" not in captured.lower()


# ==============================================================================
# 8. 산출물(MD/CSV/JSON) 계약
# ==============================================================================


def test_write_outputs_reflects_new_contract(tmp_path):
    ctx = AuditContext()
    data = run_audit(ctx)
    pvi.write_outputs(data, dataclasses.replace(ctx, repo_root=tmp_path))
    design = tmp_path / "docs" / "design" / "platform-v2"
    review = tmp_path / "review" / "platform-v2" / "20261024" / "backend"

    md = (design / "backend-module-inventory.md").read_text(encoding="utf-8")
    assert "100.0%" not in md and "bounded_complete" not in md
    assert "coverage" in md and "unknown" in md
    assert "routes" in md

    summary = json.loads((review / "audit_summary.json").read_text(encoding="utf-8"))
    assert "audit_health" not in summary and "denominators" not in summary
    assert summary["scan_report"]["verdict"] == "coverage_partially_unverified"
    assert summary["collected_counts"]["routes"] == data["collected_counts"]["routes"]
    assert any(
        "routes" in item["item"] for item in summary["scan_report"]["unresolved_ledger"]
    )

    cov_csv = (design / "backend-scan-coverage.csv").read_text(encoding="utf-8")
    assert cov_csv.splitlines()[0].startswith("category,collected_count")
    assert "routes" in cov_csv and ",unknown," in cov_csv

    manifest = json.loads((review / "source_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] and all(len(v) == 64 for v in manifest["files"].values())
    assert manifest["src_tree"] == data["scan_report"]["target"]["git"]["src_tree"]

    inv_csv = (design / "backend-module-inventory.csv").read_text(encoding="utf-8")
    assert "src_tree" in inv_csv.splitlines()[0]
    limits = "\n".join(data["limits_ledger"])
    assert "unknown" in limits and "전수성" in limits


# ==============================================================================
# 9. 문서 가드: 과거 snapshot 구분 / 미확인 가설 표기
# ==============================================================================


def test_baseline_transition_doc_marks_unverified_hypothesis_and_unknown_binding():
    text = (
        REPO_ROOT / "docs/design/platform-v2/baseline-transition-607-to-c8.md"
    ).read_text(encoding="utf-8")
    assert "오염 충돌이었음이 확인" not in text
    assert "원인이 밝혀졌습니다" not in text
    assert "미확인 가설" in text
    assert "소스 blob 대응" in text and "unknown" in text


# ==============================================================================
# 10. 교정 검증: 독립 검증 반례 재현 방지 (Positive & Negative Controls)
# ==============================================================================


def test_source_parse_error_marks_category_coverage_unknown(tmp_path):
    """Negative control: broken syntax file lowers source_files coverage to unknown (not reconciled)."""
    src = tmp_path / "src" / "ttakkari"
    src.mkdir(parents=True)
    (src / "valid.py").write_text("x = 1\n", encoding="utf-8")
    (src / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    ctx = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")

    disc = discover_source_files(ctx)
    assert any("SyntaxError" in err for err in disc.errors)

    _files, parse_errors, _missing = extract_source_files(ctx)
    assert len(parse_errors) == 1
    assert parse_errors[0]["file"] == "src/ttakkari/broken.py"

    report = run_audit(ctx)
    assert report["scan_report"]["extraction"]["ok"] is False
    assert report["scan_report"]["verdict"] == "extraction_failed"
    cov_sf = report["scan_report"]["coverage"]["by_category"]["source_files"]
    assert cov_sf["state"] == "unknown"
    assert (
        "source_files" not in report["scan_report"]["coverage"]["summary"]["reconciled"]
    )
    assert "source_files" in report["scan_report"]["coverage"]["summary"]["unknown"]


def test_source_valid_files_reconcile_coverage(tmp_path):
    """Positive control: valid syntax files reconcile cleanly."""
    src = tmp_path / "src" / "ttakkari"
    src.mkdir(parents=True)
    (src / "valid.py").write_text("x = 1\n", encoding="utf-8")
    ctx = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")

    disc = discover_source_files(ctx)
    assert len(disc.errors) == 0
    assert "src/ttakkari/valid.py" in disc.candidate_ids

    _files, parse_errors, _missing = extract_source_files(ctx)
    assert len(parse_errors) == 0
    res = reconcile_category(
        "source_files",
        ["src/ttakkari/valid.py"],
        disc,
        None,
        collected_hashes={
            "src/ttakkari/valid.py": disc.fingerprints["src/ttakkari/valid.py"]
        },
    )
    assert res["state"] == "reconciled"


def test_fake_module_without_loader_unbound_and_configs_unknown(tmp_path, monkeypatch):
    """Negative control: in-process fake module with __loader__=None is detected as unbound."""
    src = tmp_path / "src" / "ttakkari"
    src.mkdir(parents=True)
    cfg_file = src / "config.py"
    cfg_file.write_text(
        "from pydantic_settings import BaseSettings\nclass Settings(BaseSettings):\n    test_val: str = 'a'\n",
        encoding="utf-8",
    )

    fake_mod = types.ModuleType("ttakkari.config")
    fake_mod.__file__ = str(cfg_file)
    fake_mod.__loader__ = None
    fake_mod.__spec__ = None

    monkeypatch.setitem(sys.modules, "ttakkari.config", fake_mod)
    ctx = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")
    binding = evaluate_import_binding(ctx)
    assert binding["state"] == "unbound"
    assert binding["categories"]["configs"]["state"] == "unbound"

    disc = DiscoveryResult(category="configs", candidate_ids=["test_val"])
    res = reconcile_category(
        "configs", ["test_val"], disc, binding["categories"]["configs"]
    )
    assert res["state"] == "unknown"


def test_conditional_mcp_registration_unresolved_and_unknown(tmp_path):
    """Negative control: @server.tool nested inside conditional block is reported as unknown."""
    src = tmp_path / "src" / "ttakkari" / "orchestrator"
    src.mkdir(parents=True)
    code = (
        "if ENABLE_TOOL:\n"
        "    @server.tool()\n"
        "    async def conditional_tool():\n"
        "        pass\n"
    )
    (src / "mcp_server.py").write_text(code, encoding="utf-8")
    ctx = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")

    disc = discover_tools(ctx)
    assert len(disc.unresolved) >= 1
    assert disc.unresolved[0]["item"] == "conditional_tool"

    res = reconcile_category(
        "tools", ["conditional_tool"], disc, {"state": "bound", "reasons": []}
    )
    assert res["state"] == "unknown"


def test_settings_factory_and_custom_inheritance_unresolved_and_unknown(tmp_path):
    """Negative control: Settings factory assignment and unsupported inheritance report unknown."""
    src = tmp_path / "src" / "ttakkari"
    src.mkdir(parents=True)

    # 1. Factory assignment
    (src / "config.py").write_text("Settings = build_settings()\n", encoding="utf-8")
    ctx1 = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")
    disc1 = discover_config_fields(ctx1)
    assert any("factory" in u["reason"].lower() for u in disc1.unresolved)
    res1 = reconcile_category(
        "configs", ["inherited_field"], disc1, {"state": "bound", "reasons": []}
    )
    assert res1["state"] == "unknown"

    # 2. Unsupported inheritance
    code = (
        "from some_module import CustomBase\n"
        "class Settings(CustomBase):\n"
        "    my_field: str = ''\n"
    )
    (src / "config.py").write_text(code, encoding="utf-8")
    ctx2 = AuditContext(repo_root=tmp_path, src_dir=tmp_path / "src")
    disc2 = discover_config_fields(ctx2)
    assert any("inherits" in u["reason"].lower() for u in disc2.unresolved)
    res2 = reconcile_category(
        "configs",
        ["my_field", "inherited_field"],
        disc2,
        {"state": "bound", "reasons": []},
    )
    assert res2["state"] == "unknown"


def test_migration_read_error_structured_ledger_no_abort(tmp_path, monkeypatch):
    """Negative control: PermissionError on migration read does not raise uncaught exception."""
    alembic_dir = tmp_path / "alembic" / "versions"
    alembic_dir.mkdir(parents=True)
    mig_file = alembic_dir / "0001_initial.py"
    mig_file.write_text("revision = '0001'\ndown_revision = None\n", encoding="utf-8")

    orig_read_text = Path.read_text

    def mock_read_text(self, *args, **kwargs):
        if "alembic" in str(self) or "0001_initial.py" in str(self):
            raise PermissionError("simulated migration read failure")
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mock_read_text)

    ctx = AuditContext(
        repo_root=tmp_path, alembic_dir=tmp_path / "alembic", src_dir=REPO_ROOT / "src"
    )
    report = run_audit(ctx)
    assert isinstance(report, dict)
    assert "scan_report" in report
    sr = report["scan_report"]
    assert sr["extraction"]["ok"] is False
    assert sr["extraction"]["by_category"]["migrations"]["state"] == "failed"
    assert sr["coverage"]["by_category"]["migrations"]["state"] == "unknown"
    assert sr["verdict"] == "extraction_failed"


def test_baseline_delta_json_and_md_causality_agreement():
    """Consistency check: baseline_delta JSON and markdown transition doc must both treat DB contamination as unverified hypothesis."""
    md_text = (
        REPO_ROOT / "docs/design/platform-v2/baseline-transition-607-to-c8.md"
    ).read_text(encoding="utf-8")
    json_path = (
        REPO_ROOT / "review/platform-v2/20261024/backend/baseline_delta_607_to_c8.json"
    )
    json_text = json_path.read_text(encoding="utf-8")
    json_data = json.loads(json_text)

    assert "오염 충돌이었음이 확인" not in md_text
    assert "오염 충돌이었음이 확인" not in json_text
    assert "유발했음이 확인됨" not in json_text
    assert "미확인 가설" in json_text
    assert any("미확인 가설" in item for item in json_data["unverified_runtime_ledger"])


@pytest.mark.parametrize("field", ["opencode_bin", "copilot_bin"])
def test_copilot_engine_executables_are_execution_settings(field):
    assert classify_config(field) == "exec.run_engine"


@pytest.mark.parametrize("filename", ["opencode_accounts.py", "copilot_accounts.py"])
def test_copilot_account_stores_are_account_management(filename):
    assert classify_source_file(f"src/ttakkari/{filename}") == "core.accounts_quota"
