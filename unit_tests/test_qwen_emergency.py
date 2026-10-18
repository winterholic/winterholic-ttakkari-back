"""Qwen emergency fallback and task-contract behavior; no DB, model or host sampling."""
import time
import unittest
import uuid

from ttakkari.agents.qwen_local import QwenLocalAdapter
from ttakkari.runs import profiles, usage, worker_routing


def _flash_profile():
    return profiles.Profile(uuid.uuid4(), "flash", "antigravity", model="gemini-3.8-flash-high")


def _qwen_profile(pool="personal"):
    return worker_routing.emergency_qwen_profile(pool)


class EmergencyEligibilityTests(unittest.TestCase):
    def test_personal_general_worker_is_eligible(self):
        self.assertTrue(worker_routing.qwen_emergency_eligible(
            pool="personal", task_type="general", strict_engine=False, session_kind="worker"))
        self.assertTrue(worker_routing.qwen_emergency_eligible(
            pool="personal", task_type="routine", strict_engine=False, session_kind="worker"))

    def test_work_pool_never_leaks_to_local_model(self):
        for task in ("general", "routine"):
            self.assertFalse(worker_routing.qwen_emergency_eligible(
                pool="work", task_type=task, strict_engine=False, session_kind="worker"))

    def test_strict_engine_lock_is_preserved(self):
        self.assertFalse(worker_routing.qwen_emergency_eligible(
            pool="personal", task_type="general", strict_engine=True, session_kind="worker"))

    def test_design_verification_reasoning_keep_waiting(self):
        for task in ("design", "verification", "reasoning"):
            self.assertFalse(worker_routing.qwen_emergency_eligible(
                pool="personal", task_type=task, strict_engine=False, session_kind="worker"))

    def test_orchestrator_joins_degraded_without_task_gate(self):
        # The planner must stay alive or nothing can spawn: orchestrator skips
        # the worker task gate and runs degraded (plan + direct work + intents).
        for task in ("general", "routine", "design", "verification", "reasoning", None):
            self.assertTrue(worker_routing.qwen_emergency_eligible(
                pool="personal", task_type=task, strict_engine=False, session_kind="orchestrator"))

    def test_orchestrator_still_gated_on_pool_and_lock(self):
        self.assertFalse(worker_routing.qwen_emergency_eligible(
            pool="work", task_type=None, strict_engine=False, session_kind="orchestrator"))
        self.assertFalse(worker_routing.qwen_emergency_eligible(
            pool="personal", task_type=None, strict_engine=True, session_kind="orchestrator"))

    def test_unknown_task_type_does_not_trigger(self):
        self.assertFalse(worker_routing.qwen_emergency_eligible(
            pool="personal", task_type="unknown-xyz", strict_engine=False, session_kind="worker"))


class EmergencyProfileTests(unittest.TestCase):
    def test_deterministic_per_pool(self):
        self.assertEqual(_qwen_profile("personal").id, _qwen_profile("personal").id)
        self.assertNotEqual(_qwen_profile("personal").id, _qwen_profile("work").id)

    def test_engine_and_model(self):
        prof = _qwen_profile()
        self.assertEqual(prof.engine, "qwen-local")
        self.assertTrue(prof.model)


class RoutingBoundaryTests(unittest.TestCase):
    def test_qwen_is_absent_from_every_auto_route_single_emergency_path(self):
        # RED (반례 2): 평시 auto 경로(ROUTES)에 Qwen이 있으면 초기 비상 분기가
        # 도달 불가하고 영수증 trigger가 initial로 찍힌다. 유일한 진입로는
        # _emergency_qwen → emergency_qwen_profile이다.
        for task, stages in worker_routing.ROUTES.items():
            self.assertNotIn("qwen-local", [engine for stage in stages for engine, _ in stage],
                             f"qwen-local must not be an auto stage of {task}")
        for task in ("general", "routine"):
            staged = [entry for stage in worker_routing.policy()[task] for entry in stage]
            self.assertNotIn("qwen-local", [entry["engine"] for entry in staged])

    def test_first_stage_rejects_qwen_only_candidates(self):
        # Qwen 후보만으로는 단계 선택이 불가하다(비상 단일 경로를 우회하지 못한다).
        with self.assertRaises(ValueError):
            worker_routing.first_stage([_qwen_profile()], "routine")

    def test_emergency_profile_uses_the_configured_model(self):
        # RED (반례 3의 전제): 비상 프로필 모델이 engine_models 게이트 값과
        # 같아야 게이트 준수가 가능하다.
        from ttakkari.config import get_settings
        expected = get_settings().worker_default_models.get("qwen-local", {}).get("model")
        prof = _qwen_profile()
        self.assertEqual(prof.engine, "qwen-local")
        self.assertEqual(prof.model, expected)

    def test_desk_sessions_are_never_emergency_eligible(self):
        # 접수(desk)는 별도 _execute_desk → desk.handle 경로이며 desk_qwen을
        # 쓰므로 작업/기획 비상 대상이 아니다.
        for pool in ("personal", "work"):
            for task in ("general", "routine", "design", None):
                self.assertFalse(worker_routing.qwen_emergency_eligible(
                    pool=pool, task_type=task, strict_engine=False, session_kind="desk"))


class MeasuredCloudBeatsUnknownTests(unittest.TestCase):
    def test_stage_priority_then_measured_then_recovery(self):
        # 한도 회복 후 클라우드 복귀: 측정된 클라우드가 미측정 후보를 이긴다.
        # Qwen은 ROUTES에 없어 first_stage 대상이 아니다(단일 비상 경로).
        flash = _flash_profile()
        from ttakkari.runs.worker_routing import NATIVE_SONNET
        native = profiles.Profile(uuid.uuid4(), "native", NATIVE_SONNET[0], model=NATIVE_SONNET[1])
        # Nobody measured: earlier stage wins among unknowns (Flash stage 0 beats native stage 3).
        self.assertEqual(worker_routing.first_stage([flash, native], "routine"), [flash])
        now = time.time()
        # A measured later-stage cloud beats unknown earlier-stage capacity.
        usage.seed(native.id, usage.Usage(
            windows=[usage.Window("five_hour", "5h", 10, int(now + 1000)),
                     usage.Window("seven_day", "weekly", 10, int(now + 1000))],
            updated_at=now), refresh=True)
        self.assertEqual(worker_routing.first_stage([flash, native], "routine"), [native])
        # After a fresh measured earlier-stage observation, the earlier stage wins again.
        usage.seed(flash.id, usage.Usage(
            windows=[usage.Window("agy_gemini_models_5h", "agy_gemini_models", 10, int(now + 1000), 90)],
            updated_at=now), refresh=True)
        try:
            self.assertEqual(worker_routing.first_stage([flash, native], "routine"), [flash])
        finally:
            usage.seed(flash.id, usage.Usage(windows=[], updated_at=now), refresh=True)
            usage.seed(native.id, usage.Usage(windows=[], updated_at=now), refresh=True)


class LimitSignalTests(unittest.TestCase):
    def test_real_usage_limit_patterns_match(self):
        self.assertTrue(profiles.is_usage_limit("You've hit your usage limit. try again at Oct 12th, 2026 12:16 AM."))
        self.assertTrue(profiles.is_usage_limit("error 429 too many requests"))

    def test_generic_and_unknown_failures_are_not_limits(self):
        self.assertFalse(profiles.is_usage_limit("connection reset by peer"))
        self.assertFalse(profiles.is_usage_limit("weird unknown blip 418"))
        self.assertFalse(profiles.is_usage_limit(None))
        self.assertFalse(profiles.is_usage_limit(""))
        # A permission failure mentioning quota must not replay through another provider.
        self.assertFalse(profiles.is_usage_limit("permission denied writing quota file"))


class TaskContractTests(unittest.TestCase):
    def test_header_lineage_and_verbatim_body(self):
        body = QwenLocalAdapter.build_task_contract(
            system_note="SYS", prompt="Do the thing.", workspace="/w", turns=20, lineage="new")
        self.assertIn("qwen-task-contract-v1", body.splitlines()[0])
        self.assertIn("lineage: new", body)
        self.assertIn("turns: 20", body)
        self.assertIn("Do the thing.", body)
        self.assertIn("SYS", body)
        # Caller text is never trimmed or rewritten by the backend.
        long_prompt = "x" * 20000
        kept = QwenLocalAdapter.build_task_contract(
            system_note="S", prompt=long_prompt, workspace="/w", turns=20, lineage="new")
        self.assertIn(long_prompt, kept)

    def test_resume_lineage_is_stamped(self):
        body = QwenLocalAdapter.build_task_contract(
            system_note="S", prompt="P", workspace="/w", turns=20, lineage="resume:abc")
        self.assertIn("lineage: resume:abc", body)


class SpawnIntentTests(unittest.TestCase):
    def test_valid_intent_with_defaults(self):
        intent, reason = worker_routing.parse_qwen_spawn_intent(
            {"title": "작은 수정", "prompt": "파일을 고치고 테스트를 돌린다."})
        self.assertEqual(reason, "")
        self.assertEqual(intent["engine"], "auto")
        self.assertEqual(intent["task_type"], "general")
        self.assertFalse(intent["strict_engine"])
        self.assertEqual(intent["title"], "작은 수정")

    def test_valid_intent_explicit_fields(self):
        intent, _ = worker_routing.parse_qwen_spawn_intent(
            {"title": "T", "prompt": "P", "engine": "qwen-local", "task_type": "routine",
             "effort": "low", "strict_engine": True, "model": "m"})
        self.assertEqual((intent["engine"], intent["task_type"], intent["effort"]), ("qwen-local", "routine", "low"))
        self.assertTrue(intent["strict_engine"])

    def test_fail_closed(self):
        bad = [
            ("not a dict", "not_an_object"),
            ({"prompt": "P"}, "missing_title"),
            ({"title": "T"}, "missing_prompt"),
            ({"title": "  ", "prompt": "P"}, "missing_title"),
            ({"title": "T", "prompt": "P", "bogus": 1}, "unknown_keys:bogus"),
            ({"title": "T", "prompt": "P", "engine": "nope"}, "bad_engine"),
            ({"title": "T", "prompt": "P", "task_type": "nap"}, "bad_task_type"),
            ({"title": "T", "prompt": "P", "effort": "ultra"}, "bad_effort"),
            ({"title": "T", "prompt": "P", "strict_engine": "yes"}, "bad_strict_engine"),
            ({"title": "x" * 121, "prompt": "P"}, "title_too_long"),
            ({"title": "T", "prompt": "x" * 20001}, "prompt_too_long"),
        ]
        for data, reason in bad:
            with self.subTest(reason=reason):
                intent, got = worker_routing.parse_qwen_spawn_intent(data)
                self.assertIsNone(intent)
                self.assertTrue(got.startswith(reason))


class DegradedNoteTests(unittest.TestCase):
    def test_note_names_queue_schema_and_cap(self):
        note = worker_routing.qwen_orchestrator_degraded_note("/outbox/spawn-queue")
        self.assertIn("/outbox/spawn-queue", note)
        self.assertIn("title", note)
        self.assertIn("self-contained", note)
        self.assertIn(str(worker_routing.QWEN_SPAWN_QUEUE_MAX_PER_RUN), note)
        self.assertIn("spawn_worker", note)


class ShortSystemNoteTests(unittest.TestCase):
    def test_qwen_task_uses_a_short_contract_note_not_the_cloud_note(self):
        # RED (반례 7): 클라우드용 시스템 노트(5.6KB)가 Qwen 작업 파일에
        # 그대로 들어가 6,397자가 된다. Qwen용 짧은 계약만 전달해야 한다.
        long_cloud_note = "CLOUD-NOTE:" + "x" * 5600
        short = QwenLocalAdapter.build_short_system_note(
            outbox="/w/outbox", write_roots=["/w", "/w/outbox"], kind="worker")
        self.assertLess(len(short), 1000, f"short note must stay small, got {len(short)}")
        body = QwenLocalAdapter.build_task_contract(
            system_note=short, prompt="Do the thing.", workspace="/w", turns=20, lineage="new")
        self.assertLess(len(body), len(long_cloud_note))
        self.assertIn("Do the thing.", body)


class ResumeGuardTests(unittest.TestCase):
    def test_engine_switch_refuses_cross_engine_resume(self):
        self.assertFalse(QwenLocalAdapter.should_resume("provider-conv-1", "antigravity"))
        self.assertFalse(QwenLocalAdapter.should_resume("provider-conv-1", "claude"))

    def test_same_engine_continuation_keeps_resume(self):
        self.assertTrue(QwenLocalAdapter.should_resume("qwen-conv-1", "qwen-local"))
        self.assertTrue(QwenLocalAdapter.should_resume("qwen-conv-1", None))

    def test_missing_id_never_resumes(self):
        self.assertFalse(QwenLocalAdapter.should_resume(None, "qwen-local"))
        self.assertFalse(QwenLocalAdapter.should_resume("", None))


class QwenResumeIdWiringTests(unittest.TestCase):
    """반례 5: should_resume 호출처 없는 죽은 코드 → qwen_resume_id가 호출한다."""

    def test_wiring_calls_should_resume(self):
        from ttakkari.runs.manager import qwen_resume_id
        # 엔진 전환이면 저장된 id가 있어도 재사용하지 않는다.
        self.assertIsNone(qwen_resume_id({"k": "s1"}, "k", "antigravity"))
        self.assertIsNone(qwen_resume_id({"k": "s1"}, "k", "claude"))
        # 같은 Qwen 안이면 이어쓴다.
        self.assertEqual(qwen_resume_id({"k": "s1"}, "k", "qwen-local"), "s1")
        self.assertEqual(qwen_resume_id({"k": "s1"}, "k", None), "s1")
        # 없으면 None, legacy 키는 명시 지정 세션의 옛 키를 잇는다.
        self.assertIsNone(qwen_resume_id({}, "k", None))
        self.assertEqual(qwen_resume_id({"legacy": "s9"}, "k", None, legacy_key="legacy"), "s9")
        # 전환이면 legacy도 잇지 않는다.
        self.assertIsNone(qwen_resume_id({"legacy": "s9"}, "k", "antigravity", legacy_key="legacy"))


if __name__ == "__main__":
    unittest.main()
