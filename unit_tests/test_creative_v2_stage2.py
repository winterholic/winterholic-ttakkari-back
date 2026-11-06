"""Stage 2 boundaries. Evaluator callables here are fixtures, never model reviews."""

import copy
import fcntl
import hashlib
import subprocess

import httpx
import pytest
from fastapi import FastAPI
from test_creative_v2_core_integration import direction
from test_creative_v2_dispatch import approved, intent, make_wav
from test_creative_v2_dispatch import stores as _stores

from ttakkari.api import creative_v2 as api
from ttakkari.creative.render import ffmpeg, inspect_media
from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.duration import SceneEdit
from ttakkari.creative.v2.planner import ProductionDirection
from ttakkari.security.auth import require_user


@pytest.fixture
def stores(tmp_path):
    return _stores.__wrapped__(tmp_path)


@pytest.fixture
def dispatched(stores, tmp_path):
    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("generative_video",) * 3)
        ),
    )
    dispatch.dispatch(plans, studio, plan["id"], 1, "title_only")
    store = dispatch.ProductionStore(plans)
    final = store.folder(plan["id"]) / "before.mp4"
    final.write_bytes(b"contract-only final; not a decodable video")
    with store.tx(plan["id"]) as data:
        record = data["dispatches"][0]
        record["final"] = {
            "file": "before.mp4",
            "sha256": dispatch._sha256_file(final),
            "status": "qa_passed",
        }
    return plans, studio, plan


def proposal(plans, plan, code="text_legibility", layer="captions"):
    return dispatch.plan_repair(
        plans,
        plan["id"],
        1,
        [{"scene_id": "i2", "shot_id": "shot-2", "code": code, "layer": layer}],
    )


def update(text="수정한 문구"):
    return [
        {
            "scene_id": "i2",
            "shot_id": "shot-2",
            "layer": "captions",
            "field": "title",
            "text": text,
        }
    ]


def test_repair_changes_only_target_and_closes_approval(dispatched):
    plans, studio, plan = dispatched
    repair = proposal(plans, plan)
    before = dispatch.ProductionStore(plans).read(plan["id"])
    result = dispatch.apply_repair(
        plans, plan["id"], 1, repair["proposal_sha256"], update()
    )
    changed = plans.get(plan["id"])
    assert result["status"] == "applied_needs_approval"
    assert changed["revision"] == 2 and not changed["render_gate"]
    assert changed["scenes"][:2] == plan["scenes"][:2]
    assert changed["scenes"][2]["title"] == "수정한 문구"
    assert (
        dispatch.ProductionStore(plans).read(plan["id"])["assets"] == before["assets"]
    )
    with pytest.raises(ValueError):
        dispatch.request_render(plans, studio, plan["id"], 2)
    with pytest.raises(ValueError):
        dispatch.apply_repair(plans, plan["id"], 1, repair["proposal_sha256"], update())


@pytest.mark.parametrize(
    "case", ["hash", "output", "target", "noop", "generation", "lease"]
)
def test_repair_rejects_stale_or_unsafe_changes_without_side_effects(dispatched, case):
    plans, _studio, plan = dispatched
    repair = proposal(plans, plan)
    edits = update()
    digest = repair.get("proposal_sha256", "missing")
    store = dispatch.ProductionStore(plans)
    held = None
    if case == "hash":
        digest = "0" * 64
    elif case == "output":
        (store.folder(plan["id"]) / "before.mp4").write_bytes(b"tampered")
    elif case == "target":
        edits[0]["scene_id"] = "i1"
    elif case == "noop":
        edits[0]["text"] = plan["scenes"][2]["title"]
    elif case == "generation":
        with store.tx(plan["id"]) as data:
            data["asset_generations"] = [{"status": "running", "plan_revision": 1}]
    elif case == "lease":
        held = (store.folder(plan["id"]) / "production.lock").open("a")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    before = store.read(plan["id"])
    try:
        with pytest.raises(ValueError):
            dispatch.apply_repair(plans, plan["id"], 1, digest, edits)
    finally:
        if held:
            held.close()
    assert plans.get(plan["id"])["revision"] == 1
    assert store.read(plan["id"]) == before


def test_unsupported_motion_is_blocked_without_applying(dispatched):
    plans, _studio, plan = dispatched
    repair = proposal(plans, plan, "motion", "motion")
    result = dispatch.apply_repair(
        plans, plan["id"], 1, repair["proposal_sha256"], update()
    )
    assert (
        result["status"] == "blocked" and "captions" in result["blocked"][0]["reason"]
    )
    assert plans.get(plan["id"])["revision"] == 1


async def test_http_repair_review_and_results_require_auth_and_reject_claimed_independence(
    dispatched, monkeypatch
):
    plans, studio, plan = dispatched
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(api, "default_studio", lambda: studio)
    app = FastAPI()
    app.include_router(api.router)
    base = f"/api/creative/v2/plans/{plan['id']}/production"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path, body in [
            (
                "/repair",
                {"revision": 1, "proposal_sha256": "0" * 64, "updates": update()},
            ),
            ("/review", {"revision": 1}),
            ("/result-link", {"revision": 1, "result_id": "final"}),
        ]:
            assert (await client.post(base + path, json=body)).status_code == 401
        app.dependency_overrides[require_user] = lambda: "owner"
        assert (
            await client.post(
                base + "/review",
                json={"revision": 1, "independent": True, "quality_evidence": []},
            )
        ).status_code == 422
        review = await client.post(base + "/review", json={"revision": 1})
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "not_connected"
        assert (
            dispatch.ProductionStore(plans)
            .read(plan["id"])["dispatches"][0]
            .get("quality_evidence", [])
            == []
        )
        repair = proposal(plans, plan)
        applied = await client.post(
            base + "/repair",
            json={
                "revision": 1,
                "proposal_sha256": repair["proposal_sha256"],
                "updates": update(),
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied_needs_approval"


def test_attestation_filters_forged_receipt_and_evidence_mutation(tmp_path):
    from ttakkari.creative.v2 import production_review as reviews

    evidence = tmp_path / "frame.png"
    evidence.write_bytes(b"frame")
    receipt = {
        "output_sha256": "out",
        "rubric_sha256": "rubric",
        "evaluator": {"independent": True},
        "criteria": [
            {
                "evidence": [
                    {
                        "path": str(evidence),
                        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                    }
                ]
            }
        ],
    }
    record = {"quality_evidence": [receipt]}
    assert reviews.attested_receipts(record) == []
    att = reviews.attest(
        receipt, execution_id="server-test", manifest_sha256="manifest"
    )
    record["quality_attestations"] = [att]
    assert reviews.attested_receipts(record) == [receipt]
    record["quality_evidence"][0]["evaluator"]["id"] = "forged"
    assert reviews.attested_receipts(record) == []
    record["quality_evidence"] = [copy.deepcopy(receipt)]
    # Restore the originally attested body, then mutate its file bytes.
    record["quality_evidence"][0]["evaluator"].pop("id")
    evidence.write_bytes(b"changed")
    assert reviews.attested_receipts(record) == []


def make_real_production(plans, studio, tmp_path, source=None):
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("generative_video",) * 3)
        ),
    )
    edits = [
        SceneEdit(
            id=s["scene_id"],
            title=("통제 결함 문구" if n == 2 else f"보존 장면 {n + 1}"),
            narration="",
            seconds=3,
            info_weight=s["info_weight"],
            essential=s["essential"],
        )
        for n, s in enumerate(plan["scenes"])
    ]
    plan = plans.edit(plan["id"], 1, scenes=edits)
    plan = plans.approve(plan["id"], 2)
    if source is None:
        source = tmp_path / "moving.mp4"
        subprocess.run(
            [
                ffmpeg(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24",
                "-t",
                "3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
    for scene in plan["scenes"]:
        dispatch.put_asset(
            plans,
            plan["id"],
            kind="scene",
            scene_id=scene["scene_id"],
            source=source,
            name=source.name,
            meta=inspect_media(source),
        )
    audio = make_wav(tmp_path / "music.wav", 10)
    dispatch.put_asset(
        plans,
        plan["id"],
        kind="music",
        scene_id=None,
        source=audio,
        name=audio.name,
        meta=inspect_media(audio),
    )
    dispatch.dispatch(plans, studio, plan["id"], 2, "title_only")
    result = dispatch.request_render(plans, studio, plan["id"], 2)
    assert result["state"] == "succeeded", result
    return plan, result


def fixture_evaluator(package, payloads):
    assert payloads["video"] and payloads["frames"] and payloads["audio"]
    return {
        "stage": "semantic_qa",
        "evaluator": {"id": "client-claimed", "independent": True},
        "criteria": [
            {
                "criterion_id": "text_legibility",
                "scene_ids": ["i0", "i1", "i2"],
                "verdict": "pass",
                "uncertainty": 0.1,
                "evidence": [package["media"]],
            }
        ],
        "defects": [],
    }


async def test_real_partial_repair_and_trusted_review_preserve_other_runs(
    stores, tmp_path, monkeypatch
):
    from ttakkari.creative.v2 import production_review as reviews

    plans, studio = stores
    plan, before = make_real_production(plans, studio, tmp_path)
    untrusted = reviews.EvaluatorBinding(
        "self-review-fixture", "1", "test self review", False, fixture_evaluator
    )
    reviews.run_review(plans, studio, plan["id"], 2, binding=untrusted)
    untrusted_record = dispatch.ProductionStore(plans).read(plan["id"])["dispatches"][
        -1
    ]
    assert untrusted_record["quality_evidence"][0]["evaluator"]["independent"] is False
    assert untrusted_record["quality_report"]["semantic_qa"]["verdict"] == "unknown"
    binding = reviews.EvaluatorBinding(
        "contract-fixture", "1", "in-process test callable", True, fixture_evaluator
    )
    reviewed = reviews.run_review(plans, studio, plan["id"], 2, binding=binding)
    assert reviewed["status"] == "recorded"
    store = dispatch.ProductionStore(plans)
    original = store.read(plan["id"])
    r = original["dispatches"][-1]
    assert r["quality_evidence"][-1]["evaluator"]["id"] == "contract-fixture"
    assert r["quality_attestations"][0]["receipt_sha256"]
    assert r["quality_attestations"][0]["evidence_sha256"]
    assert r["quality_report"]["semantic_qa"]["evaluators"]
    assert any(
        c["criterion_id"] == "text_legibility" and c["verdict"] == "pass"
        for c in r["quality_report"]["semantic_qa"]["checks"]
    )
    repair = dispatch.plan_repair(
        plans,
        plan["id"],
        2,
        [
            {
                "scene_id": "i2",
                "shot_id": "shot-2",
                "code": "text_legibility",
                "layer": "captions",
            }
        ],
    )
    applied = dispatch.apply_repair(
        plans,
        plan["id"],
        2,
        repair["proposal_sha256"],
        update("수정 후 원본 동작 보존"),
    )
    assert applied["revision"] == 3
    plans.approve(plan["id"], 3)
    dispatch.dispatch(plans, studio, plan["id"], 3, "title_only")
    done = dispatch.request_render(plans, studio, plan["id"], 3)
    assert done["state"] == "succeeded", done
    data = store.read(plan["id"])
    new = data["dispatches"][-1]
    assert original["assets"] == data["assets"]
    for old, segment in zip(r["segments"][:2], new["segments"][:2], strict=True):
        assert segment["cached_from_previous_revision"] is True
        assert (
            segment["run_key"] == old["run_key"] and segment["output"] == old["output"]
        )
    assert r["segments"][2]["run_key"] != new["segments"][2]["run_key"]
    assert before["final"]["sha256"] != done["final"]["sha256"]
    assert done["quality_report"]["semantic_qa"]["verdict"] == "unknown"
    # Even copied authentic receipts cannot bless a changed output/rubric.
    with store.tx(plan["id"]) as saved:
        current = saved["dispatches"][-1]
        current["quality_evidence"] = copy.deepcopy(r["quality_evidence"])
        current["quality_attestations"] = copy.deepcopy(r["quality_attestations"])
    from ttakkari.creative.v2.quality import evaluate_production

    report = evaluate_production(
        plans.get(plan["id"]),
        {**new, "quality_evidence": r["quality_evidence"]},
        media_path=store.folder(plan["id"]) / new["final"]["file"],
    )
    assert report["semantic_qa"]["verdict"] == "unknown"
    assert report["semantic_qa"]["rejected_receipts"]
    assert (
        reviews.attested_receipts(
            {
                **new,
                "quality_evidence": r["quality_evidence"],
                "quality_attestations": r["quality_attestations"],
            }
        )
        == []
    )
    path, _, sha = dispatch.result_media(plans, plan["id"], 2, "seg-03")
    assert path.is_file() and sha == r["segments"][2]["output"]["sha256"]
    historic = dispatch.revision_result(plans, plan["id"], 2)
    assert historic["scenes"][2]["title"] == "통제 결함 문구"
    from types import SimpleNamespace

    from ttakkari.security import auth

    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(
            jwt_secret="stage2-test-only-key-abcdefghijklmnopqrstuvwxyz",
            download_token_ttl_seconds=60,
        ),
    )
    monkeypatch.setattr(api, "default_plan_store", lambda: plans)
    monkeypatch.setattr(api, "default_studio", lambda: studio)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    base = f"/api/creative/v2/plans/{plan['id']}/production"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for rev in (2, 3):
            for result_id in ("final", "seg-03"):
                response = await client.post(
                    base + "/result-link",
                    json={"revision": rev, "result_id": result_id},
                )
                assert response.status_code == 200, response.text
                link = response.json()
                downloaded = await client.get(link["url"])
                assert downloaded.status_code == 200, downloaded.text
                assert hashlib.sha256(downloaded.content).hexdigest() == link["sha256"]
                wrong = link["url"].replace(f"/results/{rev}/", f"/results/{5 - rev}/")
                assert (await client.get(wrong)).status_code == 403
        old = await client.get(base + "/revisions/2")
        assert (
            old.status_code == 200
            and old.json()["scenes"][2]["title"] == "통제 결함 문구"
        )
        app.dependency_overrides.clear()
        assert (await client.get(base + "/revisions/2")).status_code == 401
        assert (
            await client.post(base + "/result-link", json={"revision": 2})
        ).status_code == 401
        assert (
            await client.get(base + "/results/2/seg-03?token=forged")
        ).status_code == 403
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        dispatch.result_media(plans, plan["id"], 2, "seg-03")


def test_core_hyperframes_projection_accepts_current_owned_template(
    tmp_path, monkeypatch
):
    from ttakkari.creative.v2 import engines, hyperframes
    from ttakkari.creative.v2.plans import build_plan

    direction_input = direction(("code_motion",) * 3)
    for scene in direction_input["scenes"]:
        scene["preset"] = "kafka_flow"
    plan = build_plan(
        "Kafka 메시지 흐름",
        intent(),
        director=ProductionDirection.model_validate(direction_input),
    )
    from ttakkari.creative.v2.plans import render_plan

    plan["render_plan"] = render_plan(plan)
    assert len(plan["render_plan"]["segments"]) == 1
    assert len(plan["render_plan"]["segments"][0]["scene_ids"]) == 3
    adapter = engines.adapter_by_id("skill.hyperframes_motion")
    captured = {}
    style_dna = {
        "status": "resolved",
        "sha256": "a" * 64,
        "dna": {
            "visual_identity": {
                "text": "technical",
                "params": {"theme": "dark", "caption_style": "bottom_card"},
            },
            "motion_language": {
                "text": "technical",
                "params": {"animation": "technical", "transition": "cut"},
            },
        },
    }

    def boundary(spec, **kwargs):
        captured.update(spec)
        hyperframes._prepare_spec_project(spec, tmp_path / "prepared")
        raise RuntimeError("current template accepted; no browser/render invoked")

    monkeypatch.setattr(adapter.delegate, "render_segment", boundary)
    spec = {
        "plan_id": plan["id"],
        "revision": 1,
        "segment_id": "seg-01",
        "seconds": 16,
        "aspect": "16:9",
        "width": 1280,
        "height": 720,
        "fps": 24,
        "scenes": plan["scenes"],
        "style_dna": style_dna,
        "editorial_scenes": plan["scenes"],
    }
    with pytest.raises(RuntimeError, match="current template accepted"):
        adapter.render_segment(
            spec, output_path=tmp_path / "unused.mp4", workdir=tmp_path / "work"
        )
    assert all(
        not set(s) & {"strategy", "shots", "camera", "motion"}
        for s in captured["scenes"]
    )
    assert captured["style_dna"] == style_dna
    assert not adapter.validate_spec(plan["scenes"])
    assert adapter.validate_spec(plan["scenes"][:2])


def test_review_rejects_altered_modality_and_concurrent_edit(stores, tmp_path):
    from ttakkari.creative.v2 import production_review as reviews

    plans, studio = stores
    plan, _ = make_real_production(plans, studio, tmp_path)

    def wrong_modality(package, payloads):
        receipt = fixture_evaluator(package, payloads)
        receipt["criteria"][0]["evidence"][0]["kind"] = "audio"
        return receipt

    binding = reviews.EvaluatorBinding(
        "fixture", "1", "contract test", True, wrong_modality
    )
    with pytest.raises(ValueError, match="evidence"):
        reviews.run_review(plans, studio, plan["id"], 2, binding=binding)
    assert (
        not dispatch.ProductionStore(plans)
        .read(plan["id"])["dispatches"][-1]
        .get("quality_evidence")
    )

    def concurrent_edit(package, payloads):
        plans.edit(plan["id"], 2, caption_updates={"i2": {"title": "동시 편집"}})
        return fixture_evaluator(package, payloads)

    binding = reviews.EvaluatorBinding(
        "fixture", "1", "contract test", True, concurrent_edit
    )
    with pytest.raises(ValueError):
        reviews.run_review(plans, studio, plan["id"], 2, binding=binding)
    assert (
        not dispatch.ProductionStore(plans)
        .read(plan["id"])["dispatches"][-1]
        .get("quality_evidence")
    )


def test_caption_repair_does_not_restart_a_fixed_producer(stores):
    plans, studio = stores
    plan = approved(
        plans,
        director=ProductionDirection.model_validate(
            direction(("slideshow", "slideshow", "frame_animation_2d"))
        ),
    )
    dispatch.dispatch(plans, studio, plan["id"], 1, "title_only")
    store = dispatch.ProductionStore(plans)
    path = store.folder(plan["id"]) / "before.mp4"
    path.write_bytes(b"contract-only output")
    with store.tx(plan["id"]) as data:
        data["dispatches"][-1]["final"] = {
            "file": "before.mp4",
            "sha256": dispatch._sha256_file(path),
            "status": "qa_passed",
        }
    repair = proposal(plans, plan)
    result = dispatch.apply_repair(
        plans, plan["id"], 1, repair["proposal_sha256"], update()
    )
    assert result["status"] == "blocked"
    assert "producer" in result["blocked"][0]["reason"]
    assert plans.get(plan["id"])["revision"] == 1
