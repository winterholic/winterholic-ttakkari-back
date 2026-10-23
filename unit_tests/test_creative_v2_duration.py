import json
import math

import pytest
from pydantic import ValidationError

from ttakkari.creative.v2.duration import (
    Beat,
    ContentProfile,
    DurationRequest,
    SceneEdit,
    estimate_speech_seconds,
    plan_duration,
    policy_summary,
    recheck_duration,
)

NARRATION = "이 장면에서는 시스템이 요청을 받아 처리하는 과정을 차례대로 설명합니다. 먼저 입력을 검증합니다."


def explainer(count: int, narration: str = NARRATION, **beat_kw) -> ContentProfile:
    return ContentProfile(
        beats=[
            Beat(id=f"b{i}", title=f"단계 {i}", narration=narration, **beat_kw)
            for i in range(count)
        ]
    )


def test_auto_without_request_uses_content_ideal_and_exact_frames():
    plan = plan_duration(explainer(6))
    assert plan.status == "auto"
    assert plan.request.mode == "auto"
    assert plan.planned_seconds == pytest.approx(plan.needed.ideal_seconds)
    assert plan.planned_frames == plan.scenes[-1].end_frame
    assert sum(s.end_frame - s.start_frame for s in plan.scenes) == plan.planned_frames
    assert plan.needed.min_seconds <= plan.planned_seconds <= plan.needed.max_seconds
    assert any("희망 시간이 없어" in r for r in plan.reasons)


def test_auto_length_grows_with_content_not_with_fixed_default():
    short = plan_duration(explainer(3))
    long = plan_duration(explainer(24))
    assert long.planned_seconds > short.planned_seconds * 6


def test_three_minutes_preferred_met_by_adjusting_scenes():
    profile = explainer(30, narration="핵심 개념을 짧게 설명합니다.")
    plan = plan_duration(profile, DurationRequest(mode="preferred", seconds=180))
    assert plan.status == "target_met"
    assert plan.planned_seconds == 180
    for scene in plan.scenes:
        assert scene.floor <= scene.seconds <= scene.ceiling + 1e-6
    assert sum(s.seconds for s in plan.scenes) == pytest.approx(180)


def test_ten_minute_longform_from_content_has_distinct_scenes():
    narration = "이 장면은 서로 다른 사건 하나를 자세히 다루며 인물의 선택과 그 결과를 차분하게 설명합니다."
    profile = ContentProfile(
        beats=[
            Beat(
                id=f"s{i}", title=f"사건 {i}", narration=narration + f" 사건 번호 {i}."
            )
            for i in range(36)
        ]
    )
    plan = plan_duration(profile, DurationRequest(mode="preferred", seconds=600))
    assert plan.status == "target_met"
    assert plan.planned_frames == 600 * 30
    assert len({s.beat_id for s in plan.scenes}) == 36
    assert plan.fps == 30


def test_too_short_soft_target_is_deviated_with_alternatives_not_truncation():
    plan = plan_duration(explainer(10), DurationRequest(mode="preferred", seconds=15))
    assert plan.status == "deviated"
    assert plan.deviation_level == "excessive"
    assert plan.planned_seconds == pytest.approx(plan.needed.min_seconds)
    assert plan.planned_seconds > 15
    kinds = {a.kind for a in plan.alternatives}
    assert {"relax_to_feasible", "split_parts"} <= kinds
    for scene in plan.scenes:
        assert scene.seconds >= scene.speech_seconds  # 발화가 잘리지 않는다


def test_too_long_soft_target_is_capped_at_non_filler_limit():
    plan = plan_duration(explainer(3), DurationRequest(mode="preferred", seconds=600))
    assert plan.status == "deviated"
    assert plan.planned_seconds == pytest.approx(plan.needed.max_seconds)
    assert plan.planned_seconds < 600
    assert any(a.kind == "extend_content" for a in plan.alternatives)
    assert all(s.seconds <= s.ceiling + 1e-6 for s in plan.scenes)


def test_exact_inside_range_is_met_to_the_frame():
    profile = explainer(6)
    needed = plan_duration(profile).needed
    target = round((needed.min_seconds + needed.ideal_seconds) / 2, 1)
    plan = plan_duration(profile, DurationRequest(mode="exact", seconds=target))
    assert plan.status == "strict_met"
    assert plan.planned_frames == round(target * 30)


def test_exact_conflict_returns_no_plan_and_nondestructive_alternatives():
    profile = explainer(10)
    plan = plan_duration(profile, DurationRequest(mode="exact", seconds=30))
    assert plan.status == "conflict"
    assert plan.planned_seconds is None and plan.scenes == []
    assert {a.kind for a in plan.alternatives} >= {
        "relax_to_feasible",
        "split_parts",
        "speed_up_speech",
    }
    speed = next(a for a in plan.alternatives if a.kind == "speed_up_speech")
    assert speed.feasible is False  # 필요 배속이 한계를 넘으면 가능하다고 말하지 않는다
    assert all(a.requires_user_approval for a in plan.alternatives)


def test_exact_above_content_range_asks_for_more_content_instead_of_padding():
    plan = plan_duration(explainer(2), DurationRequest(mode="exact", seconds=300))
    assert plan.status == "conflict"
    assert any(a.kind == "extend_content" for a in plan.alternatives)


def test_platform_cap_compresses_but_keeps_essentials():
    profile = explainer(12, narration="핵심만 간단히 설명합니다.")
    base = plan_duration(profile)
    cap = base.needed.min_seconds + 5
    assert cap < base.planned_seconds
    request = DurationRequest(
        mode="platform_cap",
        seconds=cap,
        platform="user",
        source="사용자 지정",
        as_of="2026-10-22",
    )
    plan = plan_duration(profile, request)
    assert plan.status == "strict_met"
    assert plan.planned_seconds <= cap + 1e-6
    assert any("압축" in r for r in plan.reasons)


def test_platform_cap_below_minimum_conflicts():
    request = DurationRequest(mode="platform_cap", platform="youtube_shorts")
    assert request.seconds == 180 and request.source and request.as_of
    plan = plan_duration(explainer(40), request)
    assert plan.status == "conflict"
    assert plan.alternatives and plan.alternatives[0].kind in {
        "relax_to_feasible",
        "drop_nonessential",
    }


def test_optional_beats_can_be_proposed_for_removal_when_cap_is_tight():
    profile = ContentProfile(
        beats=[
            Beat(id="core1", narration="핵심 내용을 설명합니다."),
            Beat(id="core2", narration="두 번째 핵심입니다."),
            Beat(
                id="extra",
                narration="부가 설명입니다. 있으면 좋은 내용입니다.",
                essential=False,
            ),
        ]
    )
    needed = plan_duration(profile).needed
    extra_floor = next(b.floor for b in needed.beats if b.beat_id == "extra")
    plan = plan_duration(
        profile, DurationRequest(mode="exact", seconds=needed.min_seconds - extra_floor)
    )
    drop = next(a for a in plan.alternatives if a.kind == "drop_nonessential")
    assert "extra" in drop.detail and drop.requires_user_approval


@pytest.mark.parametrize(
    "bad", [0, 0.999, -5, 900.001, 1000, math.nan, math.inf, -math.inf]
)
def test_invalid_requests_are_rejected(bad):
    with pytest.raises(ValidationError):
        DurationRequest(mode="preferred", seconds=bad)
    with pytest.raises(ValidationError):
        DurationRequest(mode="exact", seconds=bad)


def test_json_nan_literal_and_bool_rejected():
    with pytest.raises(ValidationError):
        DurationRequest.model_validate_json('{"mode":"preferred","seconds":NaN}')
    with pytest.raises(ValidationError):
        DurationRequest.model_validate({"mode": "preferred", "seconds": True})


def test_auto_cannot_carry_seconds_and_missing_seconds_rejected():
    with pytest.raises(ValidationError):
        DurationRequest(mode="auto", seconds=30)
    with pytest.raises(ValidationError):
        DurationRequest(mode="preferred")
    with pytest.raises(ValidationError):
        DurationRequest(mode="platform_cap", seconds=60)  # 근거 없는 상한


def test_boundaries_one_and_nine_hundred_seconds_are_accepted():
    assert DurationRequest(mode="preferred", seconds=1).seconds == 1
    assert DurationRequest(mode="preferred", seconds=900).seconds == 900
    one = ContentProfile(beats=[Beat(id="a", narration="안녕")])
    plan = plan_duration(one, DurationRequest(mode="exact", seconds=1))
    assert plan.status == "strict_met" and plan.planned_frames == 30
    big = plan_duration(explainer(3), DurationRequest(mode="preferred", seconds=900))
    assert big.status == "deviated" and big.planned_seconds < 900


def test_policy_exposes_presets_and_platform_source():
    policy = policy_summary()
    assert policy["presets_seconds"] == [15, 30, 60, 120, 180, 300, 600, 900]
    assert policy["max_seconds"] == 900 and policy["min_seconds"] == 1
    assert policy["platform_caps"]["youtube_shorts"]["source"].startswith("https://")


def test_music_anchors_runtime_and_flags_overflow():
    profile = ContentProfile(
        beats=[Beat(id=f"m{i}", narration="가사 한 줄을 부릅니다.") for i in range(4)],
        music_seconds=48,
    )
    plan = plan_duration(profile, DurationRequest(mode="preferred", seconds=20))
    assert plan.planned_seconds == pytest.approx(48)
    assert plan.status == "deviated"
    overflow = ContentProfile(
        beats=[Beat(id=f"m{i}", narration="가사 한 줄.") for i in range(2)],
        music_seconds=200,
    )
    assert any("음악 길이" in w for w in plan_duration(overflow).warnings)


def test_speech_estimate_is_monotonic_and_measured_overrides():
    assert estimate_speech_seconds("") == 0
    assert estimate_speech_seconds("안녕하세요") < estimate_speech_seconds(
        "안녕하세요 반갑습니다. 오늘도 좋은 하루입니다."
    )
    profile = ContentProfile(
        beats=[Beat(id="a", narration=NARRATION, measured_speech_seconds=30)]
    )
    need = plan_duration(profile).needed.beats[0]
    assert need.speech_source == "measured" and need.floor >= 30.4


def test_recompute_after_scene_edit_updates_total_and_flags_cut_speech():
    edits = [SceneEdit(id=f"b{i}", narration=NARRATION, seconds=11) for i in range(3)]
    ok = recheck_duration(edits, DurationRequest(mode="preferred", seconds=33))
    assert ok.status == "ok" and ok.total_seconds == 33 and ok.target_delta_seconds == 0
    edits[0] = edits[0].model_copy(update={"seconds": 2})
    cut = recheck_duration(edits, DurationRequest(mode="exact", seconds=33))
    assert cut.status == "violation"
    assert cut.total_seconds == 24
    assert any("잘립니다" in e for e in cut.errors) and any(
        "정확한 길이" in e for e in cut.errors
    )
    assert cut.fit_suggestion is not None


def test_recompute_reflects_narration_change_and_platform_cap():
    edits = [SceneEdit(id="a", narration="짧은 말.", seconds=20)]
    request = DurationRequest(
        mode="platform_cap",
        seconds=15,
        platform="user",
        source="사용자",
        as_of="2026-10-22",
    )
    result = recheck_duration(edits, request)
    assert result.status == "violation" and "초과" in result.errors[0]
    longer = edits[0].model_copy(update={"narration": NARRATION * 4, "seconds": 12})
    result = recheck_duration([longer], DurationRequest())
    assert result.status == "violation"  # 늘어난 대본이 12초 장면을 넘친다


def test_plan_is_deterministic_and_json_serializable():
    profile = explainer(8)
    request = DurationRequest(mode="preferred", seconds=120)
    first = plan_duration(profile, request)
    second = plan_duration(profile, request)
    assert first.basis_sha256 == second.basis_sha256
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    json.dumps(first.model_dump(mode="json"), allow_nan=False)
