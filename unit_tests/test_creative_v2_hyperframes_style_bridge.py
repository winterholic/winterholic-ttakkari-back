"""Pure A/Core style bridge contracts. Never launch preflight, producer or render."""

from copy import deepcopy

import pytest
from test_creative_v2_core_integration import direction
from test_creative_v2_dispatch import intent

from ttakkari.creative.service import Studio
from ttakkari.creative.v2 import dispatch, engines
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.creative.v2.plans import PlanStore, build_plan


def style():
    return {
        "status": "resolved",
        "sha256": "a" * 64,
        "dna": {
            "visual_identity": {
                "text": "technical cards",
                "params": {
                    "theme": "dark",
                    "caption_style": "bottom_card",
                    "density": "dense",
                },
            },
            "motion_language": {
                "text": "precise motion",
                "params": {"animation": "technical", "transition": "cut"},
            },
            "palette": [{"hex": "#123456", "role": "background"}],
        },
    }


def raw_plan(dna):
    directed = direction(("code_motion",) * 3)
    for scene in directed["scenes"]:
        scene["preset"] = "kafka_flow"
    plan = build_plan(
        "Kafka 메시지 흐름",
        intent(),
        director=ProductionDirection.model_validate(directed),
    )
    plan["style"] = dna
    return plan


@pytest.fixture(autouse=True)
def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight/render forbidden in the style bridge checks")

    monkeypatch.setattr(engines.ModuleAdapter, "preflight", forbidden)
    monkeypatch.setattr(engines.V1SlideshowAdapter, "preflight", forbidden)
    monkeypatch.setattr(engines, "inspect_permissions", forbidden)
    from ttakkari.creative.v2 import hyperframes

    monkeypatch.setattr(hyperframes.ADAPTER, "render_segment", forbidden)
    monkeypatch.setattr(hyperframes.ADAPTER, "preflight", forbidden)


def test_wrapper_delegates_exact_compose_result_without_mutation():
    dna = style()
    before = deepcopy(dna)
    adapter = engines.adapter_by_id("skill.hyperframes_motion")
    contract = adapter.validate_style_dna(dna)
    assert contract["matched_params"] == {
        "theme": "dark",
        "caption_style": "bottom_card",
        "animation": "technical",
        "transition": "cut",
    }
    unapplied = {item["path"]: item["value"] for item in contract["unapplied_fields"]}
    assert unapplied["palette"] == dna["dna"]["palette"]
    assert unapplied["visual_identity.params.density"] == "dense"
    assert unapplied["motion_language.text"] == "precise motion"
    assert dna == before


@pytest.mark.parametrize(
    "facet,param,value,allowed",
    [
        ("visual_identity", "theme", "light", "dark"),
        ("visual_identity", "caption_style", "plain", "bottom_card"),
        ("motion_language", "animation", "pop", "technical"),
        ("motion_language", "transition", "dissolve", "cut"),
    ],
)
def test_invalid_style_is_rejected_before_dispatch_preflight(
    tmp_path, facet, param, value, allowed
):
    dna = style()
    dna["dna"][facet]["params"][param] = value
    plans = PlanStore(tmp_path / "plans")
    studio = Studio(tmp_path / "studio")
    plan = plans.create(raw_plan(dna))
    plan = plans.approve(plan["id"], 1)
    reasons = dispatch.dispatch_blockers(plan)
    assert any(
        f"{facet}.params.{param}" in reason
        and repr(value) in reason
        and allowed in reason
        for reason in reasons
    )
    with pytest.raises(ValueError, match=f"{facet}.params.{param}"):
        dispatch.dispatch(plans, studio, plan["id"], 1)
    assert dispatch.ProductionStore(plans).read(plan["id"])["dispatches"] == []
    assert not studio.list()


def test_unapplied_fields_are_visible_before_approval_and_dispatch(tmp_path):
    plans = PlanStore(tmp_path / "plans")
    studio = Studio(tmp_path / "studio")
    plan = plans.create(raw_plan(style()))
    view = dispatch.view(plans, studio, plan["id"])
    assert view["state"] == "not_dispatched" and not view["dispatchable"]
    review = view["producer_style_reviews"][0]
    assert review["engine"] == "skill.hyperframes"
    assert review["segment_ids"] == ["seg-01"]
    assert review["contract"]["status"] == "partial_match"
    assert review["contract"]["matched_params"]["theme"] == "dark"
    assert {r["path"]: r["value"] for r in review["contract"]["unapplied_fields"]}[
        "palette"
    ] == style()["dna"]["palette"]
    assert plans.get(plan["id"])["scenes"] == plan["scenes"]


def test_valid_contract_is_preserved_in_dispatch_and_revision_review(
    tmp_path, monkeypatch
):
    # Test-only metadata preflight. No process or producer can execute.
    monkeypatch.setattr(
        engines.ModuleAdapter,
        "preflight",
        lambda _: engines.Preflight(True, "fixture-not-executed", []),
    )
    monkeypatch.setattr(
        engines.V1SlideshowAdapter,
        "preflight",
        lambda _: engines.Preflight(True, "fixture-not-executed", []),
    )
    plans = PlanStore(tmp_path / "plans")
    studio = Studio(tmp_path / "studio")
    plan = plans.create(raw_plan(style()))
    plan = plans.approve(plan["id"], 1)
    view = dispatch.dispatch(plans, studio, plan["id"], 1)
    data = dispatch.ProductionStore(plans).read(plan["id"])
    assert (
        data["dispatches"][0]["producer_style_reviews"]
        == view["producer_style_reviews"]
    )
    assert (
        dispatch.revision_result(plans, plan["id"], 1)["producer_style_reviews"]
        == view["producer_style_reviews"]
    )
    assert (
        view["producer_style_reviews"][0]["contract"]["matched_params"]["theme"]
        == "dark"
    )
    assert not studio.list()
    assert plan["scenes"] == plans.get(plan["id"])["scenes"]


def test_wrapper_direct_call_rejects_invalid_style_before_delegate(tmp_path):
    dna = style()
    dna["dna"]["motion_language"]["params"]["animation"] = "pop"
    plan = raw_plan(dna)
    adapter = engines.adapter_by_id("skill.hyperframes_motion")
    with pytest.raises(ValueError, match="motion_language.params.animation"):
        adapter.render_segment(
            {"scenes": plan["scenes"], "style_dna": dna},
            output_path=tmp_path / "unused.mp4",
            workdir=tmp_path / "unused",
        )
