"""Parallel-fit Qwen routing policy; no DB, model or host sampling.

The steady-state ("parallel") path never enters ROUTES stages: it is a
single-entry pre-step like the emergency path, and orchestrator sessions are
excluded from it by construction (emergency only, i.e. lowest priority).
"""
import unittest

from ttakkari.runs import profiles, worker_routing


def _assess(**over):
    base = {"pool": "personal", "task_type": "general", "session_kind": "worker",
            "session_engine": "auto", "strict_engine": False, "parallel_siblings": 1,
            "needs_mcp": False, "narrow_scope": True, "verifiable": True}
    base.update(over)
    return worker_routing.qwen_fit_assessment(**base)


class FitAssessmentTests(unittest.TestCase):
    def test_fully_declared_parallel_worker_fits(self):
        for task in ("general", "routine"):
            got = _assess(task_type=task)
            self.assertTrue(got["fit"], got)
            self.assertEqual(got["reasons"], [])
            self.assertEqual(got["parallel_siblings"], 1)

    def test_single_run_is_not_parallel(self):
        got = _assess(parallel_siblings=0)
        self.assertFalse(got["fit"])
        self.assertIn("single_run", got["reasons"])

    def test_fit_needs_all_three_hints(self):
        cases = [({"needs_mcp": True}, "mcp_required_or_unknown"),
                 ({"needs_mcp": None}, "mcp_required_or_unknown"),
                 ({"narrow_scope": False}, "scope_not_narrow"),
                 ({"narrow_scope": None}, "scope_not_narrow"),
                 ({"verifiable": False}, "not_verifiable"),
                 ({"verifiable": None}, "not_verifiable")]
        for hints, reason in cases:
            with self.subTest(hints=hints):
                got = _assess(**hints)
                self.assertFalse(got["fit"], got)
                self.assertIn(reason, got["reasons"])

    def test_routine_defaults_to_fit_without_declarations(self):
        got = _assess(task_type="routine", needs_mcp=None, narrow_scope=None, verifiable=None)
        self.assertTrue(got["fit"])
        self.assertEqual(got["basis"], "default_routine_fit")
        self.assertEqual(got["reasons"], [])

    def test_routine_explicit_false_declarations_respected(self):
        cases = [
            ({"needs_mcp": True}, "mcp_required_or_unknown"),
            ({"narrow_scope": False}, "scope_not_narrow"),
            ({"verifiable": False}, "not_verifiable"),
        ]
        for hints, reason in cases:
            with self.subTest(hints=hints):
                over = {"task_type": "routine", "needs_mcp": None, "narrow_scope": None, "verifiable": None}
                over.update(hints)
                got = _assess(**over)
                self.assertFalse(got["fit"], got)
                self.assertIn(reason, got["reasons"])

    def test_company_scope_is_excluded(self):
        got = _assess(pool="work")
        self.assertFalse(got["fit"])
        self.assertIn("company_scope_excluded", got["reasons"])

    def test_design_verification_reasoning_are_excluded(self):
        for task in ("design", "verification", "reasoning", "unknown-xyz"):
            with self.subTest(task=task):
                got = _assess(task_type=task)
                self.assertFalse(got["fit"], got)
                self.assertIn("task_excluded", got["reasons"])

    def test_strict_engine_lock_is_excluded(self):
        got = _assess(strict_engine=True)
        self.assertFalse(got["fit"])
        self.assertIn("strict_engine_excluded", got["reasons"])

    def test_pinned_engine_takes_the_explicit_path(self):
        for engine in ("claude", "codex", "qwen-local", "muse"):
            with self.subTest(engine=engine):
                got = _assess(session_engine=engine)
                self.assertFalse(got["fit"], got)
                self.assertIn("explicit_engine_separate_path", got["reasons"])

    def test_orchestrator_is_lowest_priority_excluded_from_parallel(self):
        # 기획 세션은 평시 경로로 Qwen에 갈 수 없다. 비상 경로만 남는다.
        for task in ("general", "routine", "design", "verification", "reasoning", None):
            with self.subTest(task=task):
                got = _assess(session_kind="orchestrator", task_type=task)
                self.assertFalse(got["fit"], got)
                self.assertIn("orchestrator_lowest_priority", got["reasons"])

    def test_other_kinds_are_excluded(self):
        for kind in ("desk", "chat", "lead"):
            with self.subTest(kind=kind):
                got = _assess(session_kind=kind)
                self.assertFalse(got["fit"], got)
                self.assertIn("kind_excluded", got["reasons"])

    def test_reasons_accumulate(self):
        got = _assess(pool="work", task_type="design", parallel_siblings=0)
        self.assertFalse(got["fit"])
        for reason in ("company_scope_excluded", "task_excluded", "single_run"):
            self.assertIn(reason, got["reasons"])

    def test_parallel_constants(self):
        self.assertEqual(worker_routing.QWEN_PARALLEL_TASKS, ("general", "routine"))
        self.assertEqual(worker_routing.QWEN_PARALLEL_TRIGGER, "parallel")
        self.assertEqual(worker_routing.QWEN_PARALLEL_MODE, "worker-parallel")
        self.assertEqual(worker_routing.QWEN_SLOTS, 1)


class ParallelBoundaryTests(unittest.TestCase):
    def test_parallel_path_adds_no_routes_stage(self):
        for task, stages in worker_routing.ROUTES.items():
            self.assertNotIn("qwen-local", [engine for stage in stages for engine, _ in stage],
                             f"qwen-local must not be an auto stage of {task}")

    def test_receipt_carries_qwen_block(self):
        import uuid
        selected = profiles.Profile(uuid.uuid4(), "flash", "antigravity", model="gemini-3.8-flash-high")
        block = {"fit": False, "reasons": ["single_run"], "decision": "cloud"}
        out = worker_routing.receipt("general", selected, [selected], [], {}, trigger="initial", qwen=block)
        self.assertEqual(out["qwen"], block)
        self.assertNotIn("qwen", worker_routing.receipt(
            "general", selected, [selected], [], {}, trigger="initial"))

    def test_parallel_qwen_result_needs_independent_verification(self):
        from ttakkari.agents.qwen_local import MODEL
        for task in ("general", "routine"):
            self.assertTrue(worker_routing.needs_independent_verification(task, MODEL))
        rec = worker_routing.verifier_recommendation(MODEL)
        self.assertEqual(rec["task_type"], "verification")
        self.assertTrue(rec["preferred_routes"])

    def test_process_slot_lock_excludes_another_process(self):
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        from ttakkari.runs.qwen_slot import acquire_process_lock, release_process_lock

        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "qwen.lock"
            held = acquire_process_lock(lock_path)
            self.assertIsNotNone(held)
            try:
                code = (
                    "from pathlib import Path; "
                    "from ttakkari.runs.qwen_slot import acquire_process_lock; "
                    f"print('busy' if acquire_process_lock(Path({str(lock_path)!r})) is None else 'acquired')"
                )
                child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
                self.assertEqual(child.stdout.strip(), "busy")
            finally:
                release_process_lock(held)

            available = acquire_process_lock(lock_path)
            self.assertIsNotNone(available)
            release_process_lock(available)

    def test_status_probe_detects_lock_held_outside_database(self):
        import tempfile
        from pathlib import Path

        from ttakkari.runs.qwen_slot import (
            acquire_process_lock,
            is_process_lock_busy,
            release_process_lock,
        )

        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "qwen.lock"
            self.assertFalse(is_process_lock_busy(lock_path))
            held = acquire_process_lock(lock_path)
            self.assertIsNotNone(held)
            try:
                self.assertTrue(is_process_lock_busy(lock_path))
            finally:
                release_process_lock(held)
            self.assertFalse(is_process_lock_busy(lock_path))

    def test_lock_remains_held_if_backend_releases_after_child_launch(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        from ttakkari.runs.qwen_slot import acquire_process_lock, release_process_lock

        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "qwen.lock"
            held = acquire_process_lock(lock_path)
            self.assertIsNotNone(held)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"], pass_fds=(held,))
            os.close(held)  # Simulate backend death: close without explicitly unlocking.
            try:
                self.assertIsNone(acquire_process_lock(lock_path))
            finally:
                child.wait(timeout=2)
            available = acquire_process_lock(lock_path)
            self.assertIsNotNone(available)
            release_process_lock(available)


class DecisionSimulationTests(unittest.TestCase):
    """적합/부적합 케이스별 라우팅 결정 시뮬레이션: 판정표 전체를 한 번에 돌린다."""

    def test_decision_table(self):
        table = [
            # (설명, 입력, 기대 fit, 기대 사유 부분집합)
            ("병렬+선언 완료", {}, True, []),
            ("형제 3명도 병렬", {"parallel_siblings": 3}, True, []),
            ("routine 병렬(선언 완료)", {"task_type": "routine"}, True, []),
            ("routine 힌트 없음(기본 적합)", {"task_type": "routine", "needs_mcp": None, "narrow_scope": None, "verifiable": None}, True, []),
            ("routine MCP 명시 필요(탈락)", {"task_type": "routine", "needs_mcp": True, "narrow_scope": None, "verifiable": None}, False, ["mcp_required_or_unknown"]),
            ("routine 범위 넓음 명시(탈락)", {"task_type": "routine", "narrow_scope": False}, False, ["scope_not_narrow"]),
            ("routine 검증 불가 명시(탈락)", {"task_type": "routine", "verifiable": False}, False, ["not_verifiable"]),
            ("routine 단일 작업(탈락)", {"task_type": "routine", "parallel_siblings": 0, "needs_mcp": None, "narrow_scope": None, "verifiable": None}, False, ["single_run"]),
            ("routine 회사 범위(탈락)", {"task_type": "routine", "pool": "work", "needs_mcp": None, "narrow_scope": None, "verifiable": None}, False, ["company_scope_excluded"]),
            ("단일 작업", {"parallel_siblings": 0}, False, ["single_run"]),
            ("힌트 없음", {"needs_mcp": None, "narrow_scope": None, "verifiable": None},
             False, ["mcp_required_or_unknown", "scope_not_narrow", "not_verifiable"]),
            ("MCP 필요", {"needs_mcp": True}, False, ["mcp_required_or_unknown"]),
            ("범위 넓음", {"narrow_scope": False}, False, ["scope_not_narrow"]),
            ("검증 불가", {"verifiable": False}, False, ["not_verifiable"]),
            ("회사 범위", {"pool": "work"}, False, ["company_scope_excluded"]),
            ("design 제외", {"task_type": "design"}, False, ["task_excluded"]),
            ("verification 제외", {"task_type": "verification"}, False, ["task_excluded"]),
            ("reasoning 제외", {"task_type": "reasoning"}, False, ["task_excluded"]),
            ("strict 잠금", {"strict_engine": True}, False, ["strict_engine_excluded"]),
            ("고정 엔진", {"session_engine": "claude"}, False, ["explicit_engine_separate_path"]),
            ("기획 최하위", {"session_kind": "orchestrator"}, False, ["orchestrator_lowest_priority"]),
            ("접수 제외", {"session_kind": "desk"}, False, ["kind_excluded"]),
        ]
        for label, inputs, expected_fit, expected_reasons in table:
            with self.subTest(label):
                got = _assess(**inputs)
                self.assertEqual(got["fit"], expected_fit, got)
                for reason in expected_reasons:
                    self.assertIn(reason, got["reasons"], got)
                if expected_fit:
                    self.assertEqual(got["reasons"], [], got)


if __name__ == "__main__":
    unittest.main()
