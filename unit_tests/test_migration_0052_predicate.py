"""0052 재분류 SQL을 sqlite에서 직접 실행한다. 정상 문서는 보수적으로 유지한다."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

MOVE = [  # files → agent
    ("watch-worker.log", "/fake/outbox/R/watch-worker.log", "agent_output"),
    ("edd84bf-worker-evidence.json", "/fake/outbox/R/edd84bf-worker-evidence.json", "agent_output"),
    ("pms-final-snapshot.json", "/fake/outbox/R/pms-final-snapshot.json", "agent_output"),
    ("replay-routing-snapshot.py", "/fake/outbox/R/replay-routing-snapshot.py", "agent_output"),
    ("snapshot_checkpoint.py", "/fake/outbox/R/luna/snapshot_checkpoint.py", "agent_output"),
    ("resource-cleanup.json", "/fake/outbox/R/resource-cleanup.json", "agent_output"),
    ("cleanup-red.xml", "/fake/outbox/R/cleanup-red.xml", "agent_output"),
    ("membership-bindings.json", "/fake/outbox/R/membership-bindings.json", "agent_output"),
    ("final-main-ci.json", "/fake/outbox/R/final-main-ci.json", "agent_output"),
    ("check-mutations.py", "/fake/outbox/R/check-mutations.py", "agent_output"),
    ("page-2026-10-19T02-25-08-307Z.png", "/fake/outbox/R/browser/page-2026-10-19T02-25-08-307Z.png", "agent_output"),
    ("keychain-selection.txt", "/fake/outbox/R/evidence/keychain-selection.txt", "agent_output"),
    ("build.log", "/ws/build.log", "agent_modified"),
]
# 단어가 겹쳐도 결과물일 수 있으면 유지한다.
STAY = [
    ("report.md", "/fake/outbox/R/report.md", "agent_output"),
    ("cleanup-plan.md", "/fake/outbox/R/cleanup-plan.md", "agent_output"),
    ("snapshot-analysis.md", "/fake/outbox/R/snapshot-analysis.md", "agent_output"),
    ("snapshot-notes.txt", "/fake/outbox/R/snapshot-notes.txt", "agent_output"),
    ("aip-wbs-evidence-20261015.zip", "/fake/outbox/R/aip-wbs-evidence-20261015.zip", "agent_output"),
    ("up.log", "/u/up.log", "upload"),
    ("reg.log", "/ws/reg.log", "user_registered"),
]


def _load():
    p = Path(__file__).parent.parent / "alembic" / "versions" / "0052_artifact_area_byproducts.py"
    spec = importlib.util.spec_from_file_location("m0052_sql", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOp:
    def __init__(self, sink: list):
        self._sink = sink

    def __getattr__(self, name):
        if name == "execute":
            return self._sink.append
        return lambda *a, **k: None


def _db():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE artifacts (id INTEGER PRIMARY KEY, filename TEXT,"
        " original_path TEXT, source TEXT, area TEXT, deleted_at TEXT)"
    )
    rows = [(f, p, s, "files", None) for f, p, s in MOVE + STAY]
    rows.append(("old.log", "/fake/outbox/R/old.log", "agent_output", "agent", None))
    rows.append(("gone.log", "/fake/outbox/R/gone.log", "agent_output", "files", "deleted"))
    con.executemany(
        "INSERT INTO artifacts (filename, original_path, source, area, deleted_at)"
        " VALUES (?,?,?,?,?)",
        rows,
    )
    return con


def test_0052_moves_byproducts_and_keeps_documents():
    mod = _load()
    assert mod.revision == "0052_artifact_area_byproducts"
    stmts: list[str] = []
    mod.op = _FakeOp(stmts)
    mod.upgrade()
    assert len(stmts) == 2
    con = _db()
    for s in stmts:
        con.execute(s)
    got = dict(con.execute("SELECT filename, area FROM artifacts").fetchall())
    for f, _, _ in MOVE:
        assert got[f] == "agent", f
    for f, _, _ in STAY:
        assert got[f] == "files", f
    assert got["old.log"] == "agent" and got["gone.log"] == "files"
    assert con.execute("SELECT COUNT(*) FROM artifact_area_backup_0052").fetchone()[0] == len(MOVE)


def test_0052_downgrade_restores_backup():
    mod = _load()
    up: list[str] = []
    mod.op = _FakeOp(up)
    mod.upgrade()
    down: list[str] = []
    mod.op = _FakeOp(down)
    mod.downgrade()
    assert len(down) == 2
    con = _db()
    for s in up:
        con.execute(s)
    for s in down:
        con.execute(s)
    got = dict(con.execute("SELECT filename, area FROM artifacts").fetchall())
    assert got["watch-worker.log"] == "files"
    assert got["pms-final-snapshot.json"] == "files"
    assert got["old.log"] == "agent"
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'artifact_area_backup_0052'"
    ).fetchone()[0] == 0
