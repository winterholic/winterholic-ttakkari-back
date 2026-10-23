"""Data model: tasks, stages, checks, attempts, results."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codes import CHANGE_DIFF, KIND_DELTA


@dataclass
class CheckOutcome:
    passed: bool | None  # None = pending (needs a trace the harness cannot supply yet)
    code: str | None = None
    detail: str = ""
    metrics: dict = field(default_factory=dict)


@dataclass
class Check:
    """One requirement-mapped check. Exactly one of `snippet` / `native` is set.

    snippet: python run in the hidden runner (workspace on sys.path, `run_unittest` available).
    native:  callable(ctx) -> CheckOutcome, runs in the evaluator process.
    """

    id: str
    kind: str = KIND_DELTA
    requirement: str = ""
    snippet: str | None = None
    native: Callable[["Ctx"], CheckOutcome] | None = None
    fail_code: str = "FAIL_HIDDEN"
    public: bool = (
        False  # part of the public suite: pass here + hidden fail => FAIL_HIDDEN_ONLY
    )
    timeout: float = 20.0


@dataclass
class Stage:
    """One evaluated step. Single-turn tasks have one stage; the long task has five."""

    id: str
    change_class: str = CHANGE_DIFF
    prompt: str = ""
    checks: list[Check] = field(default_factory=list)
    required_change_paths: tuple[str, ...] = ()
    allowed_writes: tuple[
        str, ...
    ] = ()  # read_only_report: artifact paths that may appear
    result_artifact: str | None = None  # read_only_report: file that must exist
    report_required: bool = True  # structured final report reconciled with the diff


@dataclass
class Negative:
    """A negative control. `end` returns the end files of every stage; None entries mean unchanged."""

    id: str  # N1..N14
    name: str
    end: Callable[[dict[str, str], list[dict[str, str]]], list[dict[str, str]]]
    expect_verdict: str = "FAIL"
    expect_codes: tuple[str, ...] = ()
    start: Callable[[dict[str, str], list[dict[str, str]]], dict[str, str]] | None = (
        None
    )
    report: (
        Callable[[dict[str, str], list[dict[str, str]]], list[dict | None]] | None
    ) = None
    session: dict | None = None  # overrides Task.gold_session when set


@dataclass
class Task:
    id: str
    type: str
    prompt: str
    base_files: dict[str, str]
    stages: list[Stage]
    frozen_paths: tuple[str, ...] = ()
    gold: Callable[[dict[str, str]], list[dict[str, str]]] | None = None
    alt_gold: Callable[[dict[str, str]], list[dict[str, str]]] | None = None
    gold_reports: (
        Callable[[dict[str, str], list[dict[str, str]]], list[dict | None]] | None
    ) = None
    gold_session: dict | None = None
    negatives: list[Negative] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    # Harness-dependent gates (resume/cancel). Interface only until WBS 2.2 settles.
    session_check: Callable[["Attempt"], list[tuple[str, "CheckOutcome"]]] | None = None

    def stage_checks(self) -> list[tuple[Stage, Check]]:
        return [(s, c) for s in self.stages for c in s.checks]


@dataclass
class Ctx:
    """Everything a native check may look at."""

    task: Task
    stage: Stage
    base_files: dict[str, str]  # original pristine task files
    stage_base_files: dict[str, str]  # files at the start of this stage
    end_files: dict[str, str]  # files at the end of this stage
    eval_dir: Path  # clean tree = base + candidate changes, frozen paths restored
    changed: dict[str, list[str]]
    report: dict | None
    trace: list[dict] | None
    scratch: Path
    cache: dict = field(default_factory=dict)


@dataclass
class StageAttempt:
    start_manifest: dict[str, str] | None
    end_dir: Path
    report: dict | None = None
    trace: list[dict] | None = None


@dataclass
class Attempt:
    attempt_id: str
    task_id: str
    profile_id: str
    repeat: int
    stages: list[StageAttempt] = field(default_factory=list)
    session: dict | None = (
        None  # harness-dependent (resume/cancel); interface only in 4.2
    )
    infra: dict | None = (
        None  # {"code": "INFRA_x" | None, "proven": bool, "http_status": ...}
    )
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    stage_id: str
    verdict: str
    fail_code: str | None
    flags: list[str] = field(default_factory=list)
    gates_pending: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    changed: dict = field(default_factory=dict)
    start_hash_ok: bool | None = None
    detail: str = ""


@dataclass
class Evaluation:
    verdict: str
    fail_code: str | None
    flags: list[str]
    gates_pending: list[str]
    stages: list[StageResult]
    evaluated: bool = True
    detail: str = ""
