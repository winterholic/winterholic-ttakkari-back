"""Bounded per-node receipt for Research Catalog's DB-backed deploy-gate cases."""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy.engine import make_url

from ttakkari.oplog.scrub import scrub

DB4_NODEIDS = (
    "tests/test_research_catalog_selection.py::test_catalog_selection_keeps_failure_conditions_scope_and_event",
    "tests/test_research_catalog_selection.py::test_catalog_selection_reports_bounded_candidate_and_failure_coverage",
    "tests/test_research_catalog_selection.py::test_catalog_relation_preserves_other_source_node_condition",
    "tests/test_research_catalog_selection.py::test_catalog_selection_initializes_seed_catalog_for_direct_callers",
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MAX_REASON = 180


def database_name(database_url: str) -> str:
    """Return only the isolated database name, never URL credentials or endpoint details."""
    try:
        name = make_url(database_url).database
    except (TypeError, ValueError):
        return "unavailable"
    if not isinstance(name, str) or not _DB_NAME.fullmatch(name):
        return "unavailable"
    return name


def _safe_text(value: Any, limit: int = _MAX_REASON) -> str:
    return scrub(str(value)).replace("\n", " ").replace("\r", " ")[:limit]


def _skip_reason(report: Any) -> str:
    reason = getattr(report, "wasxfail", None)
    if reason:
        return _safe_text(reason)
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        longrepr = longrepr[2]
    return _safe_text(longrepr) if longrepr else "pytest reported skipped without a reason"


def terminal_outcome(reports: list[Any], *, interrupted: bool = False) -> tuple[str, str | None]:
    """Reduce pytest phase reports to a terminal result without inventing success."""
    failures = [report for report in reports if getattr(report, "outcome", None) == "failed"]
    if failures:
        phases = ",".join(dict.fromkeys(str(getattr(report, "when", "unknown")) for report in failures))
        return "failed", f"pytest {phases} phase failed"

    calls = [report for report in reports if getattr(report, "when", None) == "call"]
    if calls:
        call = calls[-1]
        if getattr(call, "outcome", None) == "skipped":
            if getattr(call, "wasxfail", None):
                return "xfailed", _skip_reason(call)
            return "skipped", _skip_reason(call)
        if getattr(call, "outcome", None) == "passed":
            if getattr(call, "wasxfail", None):
                return "xpassed", _safe_text(call.wasxfail)
            return "passed", None

    skips = [report for report in reports if getattr(report, "outcome", None) == "skipped"]
    if skips:
        return "skipped", _skip_reason(skips[-1])
    if interrupted:
        return "not_run", "not reached before pytest stepwise interruption"
    return "not_run", "selected but no terminal test report was recorded"


def format_receipt_lines(*, revision: str, argv: list[str], marker: str, skip_tests: bool,
                         skip_reason: str | None, database: str,
                         outcomes: dict[str, tuple[str, str | None]]) -> list[str]:
    """Render short line-oriented fields so supported deploy-log reads retain the full receipt."""
    safe_argv = [_safe_text(arg, 240) for arg in argv]
    safe_marker = _safe_text(marker, 120) if marker else "<none>"
    reason = _safe_text(skip_reason) if skip_reason else "<none>"
    db = database if _DB_NAME.fullmatch(database) else "unavailable"
    lines = [
        (f"db4 receipt revision={revision if _REVISION.fullmatch(revision) else 'invalid'} "
         f"skip_tests={str(bool(skip_tests)).lower()} skip_reason={reason} db={db}"),
        f"db4 argv_count={len(safe_argv)}",
        f"db4 marker={safe_marker}",
    ]
    lines.extend(f"db4 argv[{index}]={json.dumps(arg, ensure_ascii=True)}"
                 for index, arg in enumerate(safe_argv))
    for nodeid in DB4_NODEIDS:
        outcome, case_reason = outcomes.get(nodeid, ("not_selected", "not selected by this invocation"))
        reason_text = _safe_text(case_reason) if case_reason else "<none>"
        lines.append(f"db4 case {nodeid} outcome={outcome} reason={reason_text}")
    return lines


class _DB4ReceiptPlugin:
    def __init__(self, config, *, revision: str, db: str):
        self.config = config
        self.revision = revision
        self.db = db
        self.selected: set[str] = set()
        self.deselected: set[str] = set()
        self.reports: dict[str, list[Any]] = defaultdict(list)
        self.exitstatus: int | None = None
        self.argv = [sys.executable, "-m", "pytest", *sys.argv[1:]]

    def pytest_collection_finish(self, session):
        self.selected.update(item.nodeid for item in session.items if item.nodeid in DB4_NODEIDS)

    def pytest_deselected(self, items):
        self.deselected.update(item.nodeid for item in items if item.nodeid in DB4_NODEIDS)

    def pytest_runtest_logreport(self, report):
        if report.nodeid in DB4_NODEIDS:
            self.reports[report.nodeid].append(report)

    def pytest_sessionfinish(self, session, exitstatus):
        self.exitstatus = int(exitstatus)

    def pytest_terminal_summary(self, terminalreporter):
        stepwise_interrupted = self.exitstatus == 2 and "--sw" in self.argv
        outcomes: dict[str, tuple[str, str | None]] = {}
        for nodeid in DB4_NODEIDS:
            if self.reports.get(nodeid):
                outcomes[nodeid] = terminal_outcome(
                    self.reports[nodeid],
                    interrupted=stepwise_interrupted and nodeid in self.selected,
                )
            elif nodeid in self.deselected:
                outcomes[nodeid] = ("not_selected", "deselected by pytest selector or marker")
            elif nodeid in self.selected and stepwise_interrupted:
                outcomes[nodeid] = terminal_outcome([], interrupted=True)
            elif nodeid in self.selected:
                outcomes[nodeid] = ("not_run", "selected but no terminal test report was recorded")
            else:
                outcomes[nodeid] = ("not_selected", "not selected by this invocation")

        marker = str(getattr(self.config.option, "markexpr", "") or "")
        for line in format_receipt_lines(
            revision=self.revision,
            argv=self.argv,
            marker=marker,
            skip_tests=False,
            skip_reason="pytest was invoked; skip_tests bypass did not apply",
            database=self.db,
            outcomes=outcomes,
        ):
            terminalreporter.write_line(line)


def register_receipt_plugin(config, environ=None) -> bool:
    """Enable only for deploy runs using the per-job isolated test database."""
    env = environ if environ is not None else os.environ
    revision = env.get("TTAKKARI_RELEASE_REVISION", "")
    db_url = env.get("TTAKKARI_TEST_DB", "")
    db = database_name(db_url)
    if not _REVISION.fullmatch(revision) or not db.startswith(("ttakkari_gen_", "ttakkari_deploy_")):
        return False
    config.pluginmanager.register(_DB4ReceiptPlugin(config, revision=revision, db=db), "deploy-db4-receipt")
    return True
