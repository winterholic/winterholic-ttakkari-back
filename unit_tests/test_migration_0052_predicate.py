"""0052 재분류 SQL을 sqlite에서 직접 실행한다. 정상 문서는 보수적으로 유지한다."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

# (filename, original_path, source, rel_path). 자동 수집 행은 원본 경로가 outbox/<run>/<rel_path> 이다.
MOVE = [  # files → agent: 기계 부산물이 확실한 것만
    ("watch-worker.log", "/fake/outbox/R/watch-worker.log", "agent_output", "watch-worker.log"),
    ("edd84bf-worker-evidence.json", "/fake/outbox/R/edd84bf-worker-evidence.json", "agent_output",
     "edd84bf-worker-evidence.json"),
    ("page-2026-10-19T02-25-08-307Z.png", "/fake/outbox/R/browser/page-2026-10-19T02-25-08-307Z.png",
     "agent_output", "browser/page-2026-10-19T02-25-08-307Z.png"),
    ("keychain-selection.json", "/fake/outbox/R/evidence/keychain-selection.json", "agent_output",
     "evidence/keychain-selection.json"),
    ("SHOT.PNG", "/fake/outbox/R/evidence/SHOT.PNG", "agent_output", "evidence/SHOT.PNG"),
    ("page-2026-10-15T21-56-39-728Z.yml", "/fake/outbox/R/browser/page-2026-10-15T21-56-39-728Z.yml",
     "agent_output", "browser/page-2026-10-15T21-56-39-728Z.yml"),
    ("dom.html", "/fake/outbox/R/evidence/dom.html", "agent_output", "evidence/dom.html"),
    ("build.log", "/ws/build.log", "agent_modified", None),
]
# 애매하면 옮기지 않는다. 이름이 부산물처럼 보여도 정상 산출물일 수 있다.
STAY = [
    ("report.md", "/fake/outbox/R/report.md", "agent_output", "report.md"),
    ("cleanup-plan.md", "/fake/outbox/R/cleanup-plan.md", "agent_output", "cleanup-plan.md"),
    ("snapshot-analysis.md", "/fake/outbox/R/snapshot-analysis.md", "agent_output", "snapshot-analysis.md"),
    ("snapshot-notes.txt", "/fake/outbox/R/snapshot-notes.txt", "agent_output", "snapshot-notes.txt"),
    ("aip-wbs-evidence-20261015.zip", "/fake/outbox/R/aip-wbs-evidence-20261015.zip", "agent_output",
     "aip-wbs-evidence-20261015.zip"),
    # 반례: 리뷰에서 정상 산출물로 지적된 이름들
    ("snapshot-analysis.json", "/fake/outbox/R/snapshot-analysis.json", "agent_output", "snapshot-analysis.json"),
    ("cleanup-plan.json", "/fake/outbox/R/cleanup-plan.json", "agent_output", "cleanup-plan.json"),
    ("final-report.json", "/fake/outbox/R/final-report.json", "agent_output", "final-report.json"),
    ("check-results.py", "/fake/outbox/R/check-results.py", "agent_output", "check-results.py"),
    ("pms-final-snapshot.json", "/fake/outbox/R/pms-final-snapshot.json", "agent_output", "pms-final-snapshot.json"),
    ("replay-routing-snapshot.py", "/fake/outbox/R/replay-routing-snapshot.py", "agent_output",
     "replay-routing-snapshot.py"),
    ("cleanup-red.xml", "/fake/outbox/R/cleanup-red.xml", "agent_output", "cleanup-red.xml"),
    ("membership-bindings.json", "/fake/outbox/R/membership-bindings.json", "agent_output",
     "membership-bindings.json"),
    ("final-main-ci.json", "/fake/outbox/R/final-main-ci.json", "agent_output", "final-main-ci.json"),
    ("check-mutations.py", "/fake/outbox/R/check-mutations.py", "agent_output", "check-mutations.py"),
    ("worker-evidence-summary.json", "/fake/outbox/R/worker-evidence-summary.json", "agent_output",
     "worker-evidence-summary.json"),
    # evidence/·browser/page-* 아래라도 문서·납품물 확장자는 사용자 결과물일 수 있어 유지한다.
    ("summary.md", "/fake/outbox/R/evidence/summary.md", "agent_output", "evidence/summary.md"),
    ("page-notes.md", "/fake/outbox/R/browser/page-notes.md", "agent_output", "browser/page-notes.md"),
    ("keychain-selection.txt", "/fake/outbox/R/evidence/keychain-selection.txt", "agent_output",
     "evidence/keychain-selection.txt"),
    ("evidence-report.pdf", "/fake/outbox/R/evidence/evidence-report.pdf", "agent_output", "evidence/evidence-report.pdf"),
    ("page-capture.docx", "/fake/outbox/R/browser/page-capture.docx", "agent_output", "browser/page-capture.docx"),
    ("evidence-bundle.zip", "/fake/outbox/R/evidence/evidence-bundle.zip", "agent_output", "evidence/evidence-bundle.zip"),
    ("no-ext", "/fake/outbox/R/evidence/no-ext", "agent_output", "evidence/no-ext"),
    # 폴더 규칙은 outbox 바로 아래 폴더만 본다. 더 깊은 경로는 사용자 결과물일 수 있다.
    ("nested.txt", "/fake/outbox/R/docs/evidence/nested.txt", "agent_output", "docs/evidence/nested.txt"),
    ("page-notes.md", "/fake/outbox/R/docs/browser/page-notes.md", "agent_output", "docs/browser/page-notes.md"),
    # share 로 올린 행은 rel_path 앞에 run 폴더가 붙는다. 사용자에게 넘기려고 공유한 것이므로 유지한다.
    ("shared.log", "/fake/outbox/R/shared.log", "agent_output", "R/shared.log"),
    ("ws-shared.log", "/ws/ws-shared.log", "agent_output", "ws-shared.log"),
    ("no-rel.log", "/fake/outbox/R/no-rel.log", "agent_output", None),
    ("up.log", "/u/up.log", "upload", None),
    ("reg.log", "/ws/reg.log", "user_registered", None),
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
        " original_path TEXT, rel_path TEXT, source TEXT, area TEXT, deleted_at TEXT)"
    )
    rows = [(f, p, r, s, "files", None) for f, p, s, r in MOVE + STAY]
    rows.append(("old.log", "/fake/outbox/R/old.log", "old.log", "agent_output", "agent", None))
    rows.append(("gone.log", "/fake/outbox/R/gone.log", "gone.log", "agent_output", "files", "deleted"))
    con.executemany(
        "INSERT INTO artifacts (filename, original_path, rel_path, source, area, deleted_at)"
        " VALUES (?,?,?,?,?,?)",
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
    for f, *_ in MOVE:
        assert got[f] == "agent", f
    for f, *_ in STAY:
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
    assert got["keychain-selection.txt"] == "files"
    assert got["old.log"] == "agent"
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'artifact_area_backup_0052'"
    ).fetchone()[0] == 0
