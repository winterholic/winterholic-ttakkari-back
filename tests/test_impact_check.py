"""변경 영향 범위 점검. 모델을 부르지 않는다."""
from types import SimpleNamespace

from helpers import wait_run
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import code_map, impact_check

A_PY = '''def place(order):
    validate(order)
    return order


def validate(order):
    return True
'''

B_PY = '''from a import place

def checkout(cart):
    return place(cart)
'''

PATCH = '''diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@ def place(
 def place(order):
+    log(order)
     validate(order)
     return order
'''


def _entries():
    return {"a.py": code_map.parse(A_PY), "b.py": code_map.parse(B_PY)}


def _settings(percent):
    return SimpleNamespace(worker_impact_check_ab_percent=percent, home=None)


def test_changed_function_detection_and_unverified_caller():
    warnings = impact_check.check(PATCH, _entries())
    assert len(warnings) == 1
    assert "b.py:checkout" in warnings[0] and "a.py:place" in warnings[0]
    assert "영향 범위 미확인" in warnings[0]


def test_caller_inside_changed_files_is_excluded(tmp_path):
    patch = PATCH + '''diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -3,2 +3,3 @@ def checkout(
 def checkout(cart):
+    log(cart)
     return place(cart)
'''
    assert impact_check.check(patch, _entries()) == []


def test_caller_is_excluded_when_its_tests_updated():
    patch = PATCH + '''diff --git a/tests/test_b.py b/tests/test_b.py
--- a/tests/test_b.py
+++ b/tests/test_b.py
@@ -1 +1,2 @@
+def test_checkout(): ...
'''
    assert impact_check.check(patch, _entries()) == []


def test_unrelated_test_update_does_not_exclude():
    patch = PATCH + '''diff --git a/tests/test_z.py b/tests/test_z.py
--- a/tests/test_z.py
+++ b/tests/test_z.py
@@ -1 +1,2 @@
+def test_z(): ...
'''
    assert len(impact_check.check(patch, _entries())) == 1


def test_warnings_are_capped_at_twenty():
    callers = "\n".join(f"import a\ndef c{i}():\n    return a.place(1)" for i in range(30))
    entries = {"a.py": code_map.parse(A_PY), "many.py": code_map.parse(callers)}
    warnings = impact_check.check(PATCH, entries)
    assert len(warnings) == 20


def test_deleted_file_functions_match_callers_by_name():
    patch = '''diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def helper(x):
-    return x
'''
    entries = {"b.py": code_map.parse("from gone import helper\ndef use():\n    return helper(1)")}
    warnings = impact_check.check(patch, entries)
    assert len(warnings) == 1 and "helper" in warnings[0]


async def test_record_off_when_percent_zero(tmp_path):
    out = await impact_check.record(session_id="s1", settings=_settings(0), is_git=True,
                                    checkpoint_ref="abc", root=tmp_path, new_files=[])
    assert out == {}


async def test_record_control_assigns_without_computing(tmp_path, monkeypatch):
    from ttakkari.runs import ctx_ab

    monkeypatch.setattr(ctx_ab, "arm", lambda *a: "control")

    def _boom(*a, **k):
        raise AssertionError("control must not compute")

    monkeypatch.setattr(impact_check, "evaluate", _boom)
    out = await impact_check.record(session_id="s1", settings=SimpleNamespace(
        worker_impact_check_ab_percent=100, home=tmp_path), is_git=True,
        checkpoint_ref="abc", root=tmp_path, new_files=[])
    assert out == {"impact_check_arm": "control"}


async def test_record_on_evaluates_git_repo(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "a.py").write_text(A_PY, encoding="utf-8")
    (tmp_path / "b.py").write_text(B_PY, encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@t.tt")
    git("config", "user.name", "t")
    git("add", ".")
    git("commit", "-qm", "base")
    (tmp_path / "a.py").write_text(A_PY.replace("    validate(order)\n", "    log(order)\n    validate(order)\n"),
                                   encoding="utf-8")
    settings = SimpleNamespace(worker_impact_check_ab_percent=100, home=tmp_path / "home")
    out = await impact_check.record(session_id="s1", settings=settings, is_git=True,
                                    checkpoint_ref="HEAD", root=tmp_path, new_files=[])
    assert out["impact_check_arm"] == "on"
    assert out["impact_check_unverified"] == 1
    assert "b.py:checkout" in out["impact_check_warnings"][0]


async def test_finished_run_records_arm_and_percent_zero_records_nothing(client, auth, make_dir, monkeypatch):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h,
                             json={"title": "영향", "engine": "claude", "prompt": "write report"})).json()["worker_id"]
    first = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    assert (await wait_run(client, auth, first["id"]))["status"] == "succeeded"
    done = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[0]
    assert "impact_check_arm" not in (done.get("usage") or {})  # percent=0이면 기록 없음
    monkeypatch.setattr(get_settings(), "worker_impact_check_ab_percent", 100)
    m = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"prompt": "again"})
    assert m.status_code == 202, m.text
    second = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[-1]
    assert (await wait_run(client, auth, second["id"]))["status"] == "succeeded"
    redone = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()[-1]
    assert (redone.get("usage") or {})["impact_check_arm"] == "on"
