"""Unit tests for diff guard rules (ADR 0066 D5)."""

from __future__ import annotations

from ttakkari.delivery.diff_guard import check_forbidden_diff


def test_clean_diff_passes():
    diff = """\
diff --git a/src/feature.py b/src/feature.py
--- a/src/feature.py
+++ b/src/feature.py
@@ -1,3 +1,4 @@
 def foo():
+    x = 1
     return 42
"""
    res = check_forbidden_diff(diff, commit_message="Add x in foo")
    assert res.is_valid
    assert len(res.violations) == 0


def test_deleted_test_is_blocked():
    diff = """\
diff --git a/tests/test_feature.py b/tests/test_feature.py
--- a/tests/test_feature.py
+++ b/tests/test_feature.py
@@ -10,4 +10,1 @@
-def test_critical_security():
-    assert True
"""
    res = check_forbidden_diff(diff)
    assert not res.is_valid
    assert any(v.rule == "no_test_deletion" for v in res.violations)


def test_added_skip_or_xfail_is_blocked():
    diff = """\
diff --git a/tests/test_flaky.py b/tests/test_flaky.py
--- a/tests/test_flaky.py
+++ b/tests/test_flaky.py
@@ -5,3 +5,4 @@
+@pytest.mark.skip(reason="temp")
 def test_network_issue():
     pass
"""
    res = check_forbidden_diff(diff)
    assert not res.is_valid
    assert any(v.rule == "no_skip_or_xfail" for v in res.violations)


def test_added_pytest_skip_call_is_blocked():
    diff = """\
diff --git a/tests/test_flaky.py b/tests/test_flaky.py
--- a/tests/test_flaky.py
+++ b/tests/test_flaky.py
@@ -5,3 +5,4 @@
 def test_network_issue():
+    pytest.skip("cannot run")
     pass
"""
    res = check_forbidden_diff(diff)
    assert not res.is_valid
    assert any(v.rule == "no_skip_or_xfail" for v in res.violations)


def test_timeout_bump_is_blocked():
    diff = """\
diff --git a/tests/test_slow.py b/tests/test_slow.py
--- a/tests/test_slow.py
+++ b/tests/test_slow.py
@@ -1,3 +1,4 @@
+@pytest.mark.timeout(300)
 def test_slow_job():
     pass
"""
    res = check_forbidden_diff(diff)
    assert not res.is_valid
    assert any(v.rule == "no_timeout_bump" for v in res.violations)


def test_swallowed_exception_is_blocked():
    diff_inline = """\
diff --git a/src/worker.py b/src/worker.py
--- a/src/worker.py
+++ b/src/worker.py
@@ -10,3 +10,4 @@
 try:
     do_work()
+except Exception: pass
"""
    res1 = check_forbidden_diff(diff_inline)
    assert not res1.is_valid
    assert any(v.rule == "no_swallowed_exceptions" for v in res1.violations)

    diff_multiline = """\
diff --git a/src/worker.py b/src/worker.py
--- a/src/worker.py
+++ b/src/worker.py
@@ -10,3 +10,5 @@
 try:
     do_work()
+except:
+    pass
"""
    res2 = check_forbidden_diff(diff_multiline)
    assert not res2.is_valid
    assert any(v.rule == "no_swallowed_exceptions" for v in res2.violations)


def test_ast_test_source_guard_blocks_import_alias_skip_addition():
    previous = "def test_route():\n    assert route() == 200\n"
    changed = "import pytest as pt\ndef test_route():\n    pt.skip('temporary')\n    assert route() == 200\n"
    result = check_forbidden_diff(
        "diff --git a/tests/test_route.py b/tests/test_route.py\n",
        "Add temporary test setup explanation.",
        test_source_pairs=[("tests/test_route.py", previous, changed)],
    )
    assert not result.is_valid
    assert any(item.rule == "no_skip_or_xfail" for item in result.violations)


def test_ast_test_source_guard_blocks_removed_test_even_if_diff_header_is_malformed():
    result = check_forbidden_diff(
        "",
        test_source_pairs=[("tests/test_route.py", "def test_route():\n    pass\n", "")],
    )
    assert not result.is_valid
    assert any(item.rule == "no_test_deletion" for item in result.violations)


def test_unexplained_assertion_change():
    diff = """\
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- a/tests/test_calc.py
+++ b/tests/test_calc.py
@@ -5,3 +5,3 @@
 def test_add():
-    assert add(1, 2) == 3
+    assert add(1, 2) == 4
"""
    # 짧거나 설명 없는 커밋 메시지는 거부
    res_bad = check_forbidden_diff(diff, commit_message="fix test")
    assert not res_bad.is_valid
    assert any(v.rule == "no_unexplained_assertion_change" for v in res_bad.violations)

    # 충분한 설명이 있는 커밋 메시지는 통과
    res_good = check_forbidden_diff(
        diff,
        commit_message="Update calculation specification to handle new tax policy in 2026",
    )
    assert res_good.is_valid
