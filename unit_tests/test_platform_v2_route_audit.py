"""Unit tests for Platform V2 Frontend Route Audit Scanner (Worker C)
TDD: Verify route extraction with multiline, nested, wildcard, dynamic support,
and negative controls (NC-ROUTE-COMMENT, NC-MISSING-ROUTE-SOURCE).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.platform_v2_test_audit import audit_frontend_routes


def test_baseline_app_tsx_route_count_and_multiline_presence():
    """Verify App.tsx route extraction:
    - Total path attributes declared: 38 (37 concrete + 1 wildcard)
    - Must detect multiline declared routes: /context-library and /calendar
    """
    app_tsx_path = Path(
        "/Users/winterholic/development/projects/winterholic-ttakkari/front/src/App.tsx"
    )
    assert app_tsx_path.exists(), "App.tsx must exist"

    result = audit_frontend_routes(app_tsx_path=app_tsx_path)
    routes = result["routes"]
    paths = [r["path"] for r in routes]

    assert len(routes) == 38, (
        f"Expected 38 path declarations, got {len(routes)}: {paths}"
    )
    assert "/context-library" in paths, "/context-library must be detected"
    assert "/calendar" in paths, "/calendar must be detected"
    assert "*" in paths, "Wildcard route '*' must be detected"


def test_route_metrics_separation():
    """Verify separation of:
    1. path 선언 수 (path_declarations_count: 38)
    2. concrete paths (37)
    3. wildcard paths (1)
    4. layout routes (1: AppShell)
    5. unique pages (29)
    6. redirects (1: * -> /chat)
    """
    app_tsx_path = Path(
        "/Users/winterholic/development/projects/winterholic-ttakkari/front/src/App.tsx"
    )
    result = audit_frontend_routes(app_tsx_path=app_tsx_path)

    metrics = result["metrics"]
    assert metrics["path_declarations_count"] == 38
    assert metrics["concrete_paths_count"] == 37
    assert metrics["wildcard_paths_count"] == 1
    assert metrics["layout_routes_count"] == 1
    assert metrics["unique_pages_count"] == 29
    assert len(metrics["unique_pages"]) == 29
    assert metrics["redirect_routes_count"] == 1
    assert len(result["unresolved_ledger"]) == 0
    assert result["coverage_status"] == "complete"


def test_unresolved_dynamic_ledger():
    """Verify that unresolvable dynamic route declarations are recorded in unresolved_ledger, not silently skipped."""
    sample_jsx = """
    <Routes>
      <Route path="/static" element={<StaticPage />} />
      <Route path={DYNAMIC_ROUTE_PATH} element={<DynamicPage />} />
      <Route path={"/computed/" + variable} element={<ComputedPage />} />
    </Routes>
    """
    result = audit_frontend_routes(source_text=sample_jsx)
    routes = result["routes"]
    unresolved = result["unresolved_ledger"]

    assert len(routes) == 1
    assert routes[0]["path"] == "/static"
    assert len(unresolved) == 2
    assert any(
        "DYNAMIC_ROUTE_PATH" in u["expression"] or "DYNAMIC_ROUTE_PATH" in u["raw"]
        for u in unresolved
    )
    assert any(
        "variable" in u["expression"] or "variable" in u["raw"] for u in unresolved
    )


def test_nc_route_comment_negative_control():
    """NC-ROUTE-COMMENT: Commented JSX route must not become a real route."""
    sample_jsx = """
    // <Route path="/comment" element={<Fake />} />
    /* <Route path="/block-comment" element={<FakeBlock />} /> */
    <Routes>
      <Route path="/active" element={<ActivePage />} />
    </Routes>
    """
    result = audit_frontend_routes(source_text=sample_jsx)
    routes = result["routes"]
    paths = [r["path"] for r in routes]

    assert "/comment" not in paths, "Commented route must not be extracted"
    assert "/block-comment" not in paths, "Block-commented route must not be extracted"
    assert paths == ["/active"], f"Expected only active route, got {paths}"
    assert result["metrics"]["path_declarations_count"] == 1


def test_nc_missing_route_source_negative_control():
    """NC-MISSING-ROUTE-SOURCE: Missing App.tsx must return typed error/incomplete,

    not an apparent zero-route success.
    """
    nonexistent = REPO_ROOT / "nonexistent_front/src/App.tsx"
    result = audit_frontend_routes(app_tsx_path=nonexistent)

    assert result["coverage_status"] == "incomplete"
    assert "error" in result
    assert result["metrics"]["status"] == "missing_source"
    assert result["metrics"]["coverage"] == "incomplete"
    assert len(result["unresolved_ledger"]) > 0


def test_route_nesting_and_layout_separation():
    """Verify nested routes, layout element without path, and redirect Navigate element."""
    sample_jsx = """
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/nested-child" element={<ChildPage />} />
        <Route path="*" element={<Navigate to="/home" />} />
      </Route>
    </Routes>
    """
    result = audit_frontend_routes(source_text=sample_jsx)
    assert len(result["routes"]) == 2
    assert len(result["layout_routes"]) == 1
    assert result["layout_routes"][0]["component"] == "AppShell"
    assert result["metrics"]["redirect_routes_count"] == 1
