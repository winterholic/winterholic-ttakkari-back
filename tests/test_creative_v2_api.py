"""Creative v2 API: 인증, 입력 경계, 계획 생성→편집→승인 흐름."""

NARRATION = "이 단계에서는 시스템이 요청을 받아 검증하고 다음 처리로 넘기는 과정을 차례대로 설명합니다."
BASE = "/api/creative/v2"


def intent_body(count=14):
    return {
        "selections": [
            {
                "entry_id": "C01.tech_explainer",
                "role": "primary",
                "strength": "hard",
                "source": "user_explicit",
                "confidence": 0.9,
                "evidence": [{"quote": "기술 설명"}],
            },
            {"entry_id": "C08.explainer", "role": "primary", "confidence": 0.7},
            {
                "entry_id": "C03.vector_motion_graphics",
                "role": "primary",
                "confidence": 0.6,
            },
        ],
        "info_items": [
            {"id": f"i{i}", "title": f"단계 {i}", "narration": NARRATION}
            for i in range(count)
        ],
    }


async def test_v2_requires_auth_and_serves_catalog_registries_and_matrix(client, auth):
    for path in (
        "/catalog",
        "/registries",
        "/matrix",
        "/duration/policy",
        "/plans",
        "/style-dna",
    ):
        assert (await client.get(BASE + path)).status_code == 401, path
    catalog = (await client.get(BASE + "/catalog", headers=auth)).json()
    assert (
        len(catalog["catalogs"]) == 12
        and len(catalog["catalogs"]["C03"]["entries"]) == 19
    )
    matrix = (await client.get(BASE + "/matrix", headers=auth)).json()
    assert [r["recipe_id"] for r in matrix["recipes"]] == [
        f"R{i:02d}" for i in range(1, 21)
    ]
    assert matrix["snapshot"]["available"] is True
    registries = (await client.get(BASE + "/registries", headers=auth)).json()
    assert (
        registries["recipe_verdicts"]
        and registries["tools"]
        and registries["models"]
        and registries["skills"]
    )
    policy = (await client.get(BASE + "/duration/policy", headers=auth)).json()
    assert policy["presets_seconds"] == [15, 30, 60, 120, 180, 300, 600, 900]


async def test_duration_boundaries_rejected_at_api(client, auth):
    for bad in (
        '{"mode":"preferred","seconds":0}',
        '{"mode":"preferred","seconds":0.999}',
        '{"mode":"preferred","seconds":901}',
        '{"mode":"preferred","seconds":-3}',
        '{"mode":"preferred","seconds":NaN}',
        '{"mode":"exact","seconds":Infinity}',
        '{"mode":"preferred","seconds":true}',
    ):
        r = await client.post(
            BASE + "/plans",
            headers={**auth, "content-type": "application/json"},
            content=('{"brief":"기술 설명 영상","duration":' + bad + "}").encode(),
        )
        assert r.status_code == 422, (bad, r.status_code)
    for ok in (1, 900):
        r = await client.post(
            BASE + "/duration/preview",
            headers=auth,
            json={
                "profile": {"beats": [{"id": "a", "narration": "안녕"}]},
                "request": {"mode": "preferred", "seconds": ok},
            },
        )
        assert r.status_code == 200, r.text


async def test_plan_flow_from_duration_choice_to_edit_and_approval(client, auth):
    r = await client.post(
        BASE + "/plans",
        headers=auth,
        json={
            "brief": "기술 설명 영상. 요청 처리 과정을 설명해 주세요.",
            "intent": intent_body(),
            "duration": {"mode": "preferred", "seconds": 180},
        },
    )
    assert r.status_code == 201, r.text
    plan = r.json()
    assert (
        plan["duration"]["status"] == "target_met"
        and plan["scenes"][-1]["end_time"] == 180
    )
    assert plan["match"]["primary_recipe"] and plan["render_gate"] is False
    pid = plan["id"]

    edited_scenes = [
        {
            "id": s["scene_id"],
            "title": s["title"],
            "narration": s["narration"],
            "seconds": s["seconds"],
        }
        for s in plan["scenes"]
    ]
    edited_scenes[0]["seconds"] += 4
    r = await client.patch(
        f"{BASE}/plans/{pid}",
        headers=auth,
        json={"revision": plan["revision"], "scenes": edited_scenes},
    )
    assert r.status_code == 200, r.text
    edited = r.json()
    assert edited["revision"] == plan["revision"] + 1
    assert (
        edited["duration_check"]["total_seconds"] == 184
        and edited["scenes"][-1]["end_time"] == 184
    )

    stale = await client.patch(
        f"{BASE}/plans/{pid}",
        headers=auth,
        json={"revision": plan["revision"], "duration": {"mode": "auto"}},
    )
    assert stale.status_code == 409

    cut = [{**s, "seconds": 2} if i == 3 else s for i, s in enumerate(edited_scenes)]
    r = await client.patch(
        f"{BASE}/plans/{pid}",
        headers=auth,
        json={
            "revision": edited["revision"],
            "duration": {"mode": "exact", "seconds": 184},
            "scenes": cut,
        },
    )
    violation = r.json()
    assert violation["duration_check"]["status"] == "violation"
    refused = await client.post(
        f"{BASE}/plans/{pid}/approve",
        headers=auth,
        json={"revision": violation["revision"]},
    )
    assert (
        refused.status_code == 422 and "승인할 수 없습니다" in refused.json()["message"]
    )

    r = await client.patch(
        f"{BASE}/plans/{pid}",
        headers=auth,
        json={
            "revision": violation["revision"],
            "duration": {"mode": "preferred", "seconds": 170},
        },
    )
    replanned = r.json()
    assert (
        replanned["duration"]["status"] == "target_met"
        and replanned["scenes"][-1]["end_time"] == 170
    )
    approved = await client.post(
        f"{BASE}/plans/{pid}/approve",
        headers=auth,
        json={"revision": replanned["revision"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["render_gate"] is False
    assert approved.json()["approval"]["planned_frames"] == 170 * 30
    rp = approved.json()["render_plan"]
    assert rp["executed"] is False and rp["segments"][-1]["end_frame"] == 170 * 30
    assert (await client.get(f"{BASE}/plans/{pid}", headers=auth)).json()[
        "render_gate"
    ] is False
    assert any(
        p["id"] == pid for p in (await client.get(BASE + "/plans", headers=auth)).json()
    )


async def test_brief_only_plan_is_flagged_and_forged_intent_rejected(client, auth):
    r = await client.post(
        BASE + "/plans", headers=auth, json={"brief": "사이버펑크 SF 영상"}
    )
    assert r.status_code == 201
    assert (
        r.json()["intent_status"] == "unconfirmed_hint"
        and r.json()["render_gate"] is False
    )
    forged = intent_body(3)
    forged["selections"][0]["evidence"] = [{"quote": "브리프에 없는 문장"}]
    r = await client.post(
        BASE + "/plans",
        headers=auth,
        json={"brief": "기술 설명 영상", "intent": forged},
    )
    assert (
        r.status_code == 422 and "근거 문구가 브리프에 없습니다" in r.json()["message"]
    )
    assert (
        await client.get(
            f"{BASE}/plans/33333333-3333-4333-8333-333333333333", headers=auth
        )
    ).status_code == 404


async def test_llm_intent_cannot_skip_user_review_by_omitting_questions(client, auth):
    candidate = intent_body(2)
    candidate["selections"][0]["strength"] = "soft"
    candidate["selections"][0]["source"] = "llm_inferred"
    for selection in candidate["selections"]:
        selection["evidence"] = [{"quote": "기술 설명"}]
    for item in candidate["info_items"]:
        item["evidence"] = [{"quote": "기술 설명"}]
    candidate["extraction"] = {"method": "llm", "model": "fixture-model"}
    candidate["open_questions"] = []
    response = await client.post(
        BASE + "/plans", headers=auth,
        json={"brief": "기술 설명 영상", "intent": candidate},
    )
    assert response.status_code == 422, response.text
    assert "검토 전 의도 초안" in response.json()["message"]


async def test_style_dna_compose_and_pin_over_api(client, auth):
    inputs = [
        {"style_id": "cute_3d", "version": "1.0.0"},
        {"style_id": "horror_lighting", "version": "1.0.0"},
    ]
    r = await client.post(
        BASE + "/style-dna/compose", headers=auth, json={"inputs": inputs}
    )
    assert r.status_code == 200 and r.json()["status"] == "unresolved"
    assert {c["field"] for c in r.json()["conflicts"]} == {"lighting", "sound_identity"}
    work = "44444444-4444-4444-8444-444444444444"
    r = await client.post(
        f"{BASE}/style-dna/works/{work}/pin", headers=auth, json={"inputs": inputs}
    )
    assert (
        r.status_code == 409
        and r.json()["code"] == "style_conflict"
        and r.json()["conflicts"]
    )
    resolutions = [
        {
            "field": f,
            "decision": "take",
            "winner": "horror_lighting@1.0.0",
            "rationale": "공포 장면이라 이쪽을 우선한다",
            "decided_by": "user",
        }
        for f in ("lighting", "sound_identity")
    ]
    r = await client.post(
        f"{BASE}/style-dna/works/{work}/pin",
        headers=auth,
        json={"inputs": inputs, "resolutions": resolutions, "expected_revision": 0},
    )
    assert r.status_code == 200 and r.json()["revision"] == 1
    stale = await client.post(
        f"{BASE}/style-dna/works/{work}/pin",
        headers=auth,
        json={"inputs": inputs, "resolutions": resolutions, "expected_revision": 0},
    )
    assert stale.status_code == 409
    got = (await client.get(f"{BASE}/style-dna/cute_3d/latest", headers=auth)).json()
    assert got["ref"] == "cute_3d@1.0.0" and got["maturity"] == "draft"


async def test_intent_validate_hint_and_match_endpoints(client, auth):
    brief = "기술 설명 영상"
    r = await client.post(
        BASE + "/intent/validate",
        headers=auth,
        json={"brief": brief, "intent": intent_body(2)},
    )
    assert r.json()["valid"] is True
    hint = (
        await client.post(
            BASE + "/intent/hint", headers=auth, json={"brief": "사이버펑크 45초"}
        )
    ).json()
    assert (
        hint["intent"]["extraction"]["method"] == "lexical_hint"
        and hint["duration_hint"]["applied"] is False
    )
    r = await client.post(
        BASE + "/match", headers=auth, json={"brief": brief, "intent": intent_body(2)}
    )
    assert r.status_code == 200 and r.json()["primary_recipe"] and r.json()["trace"]
    bad = await client.post(
        BASE + "/match",
        headers=auth,
        json={"brief": "아무 말", "intent": intent_body(2)},
    )
    assert bad.status_code == 422


async def test_legacy_direct_intent_extract_route_is_removed(client, auth):
    response = await client.post(BASE + "/intent/extract", headers=auth, json={"brief": "기술 설명"})
    assert response.status_code == 404


async def test_intent_run_endpoints_require_auth(client):
    assert (await client.post(BASE + "/intent/runs", json={"brief": "기술 설명"})).status_code == 401
    assert (await client.get(BASE + "/intent/runs/00000000-0000-4000-8000-000000000001")).status_code == 401
    assert (await client.post(BASE + "/intent/runs/00000000-0000-4000-8000-000000000001/resume")).status_code == 401


async def test_intent_run_start_and_poll_use_run_lifecycle(client, auth, monkeypatch):
    from ttakkari.api import creative_v2

    calls = []

    async def fake_start(_db, brief, *, request_key=None):
        calls.append((brief, request_key))
        return {
            "session_id": "11111111-1111-4111-8111-111111111111",
            "run_id": "22222222-2222-4222-8222-222222222222",
            "status": "queued",
            "engine": "qwen-local",
            "model": "Qwen3.8-Flash-Next-Q2_K",
        }

    async def fake_read(_db, run_id):
        assert str(run_id) == "22222222-2222-4222-8222-222222222222"
        return {"run_id": str(run_id), "status": "running", "engine": "qwen-local", "intent": None}

    async def fake_resume(_db, run_id, *, request_key=None):
        assert str(run_id) == "22222222-2222-4222-8222-222222222222"
        return {"run_id": str(run_id), "status": "queued", "engine": "qwen-local", "request_key": request_key}

    monkeypatch.setattr(creative_v2, "start_intent_run", fake_start)
    monkeypatch.setattr(creative_v2, "read_intent_run", fake_read)
    monkeypatch.setattr(creative_v2, "resume_intent_run", fake_resume)

    started = await client.post(BASE + "/intent/runs", headers={**auth, "Idempotency-Key": "req-1"}, json={"brief": "기술 설명"})
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"
    assert calls == [("기술 설명", "req-1")]
    polled = await client.get(BASE + "/intent/runs/22222222-2222-4222-8222-222222222222", headers=auth)
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "running"
    resumed = await client.post(
        BASE + "/intent/runs/22222222-2222-4222-8222-222222222222/resume",
        headers={**auth, "Idempotency-Key": "resume-1"},
    )
    assert resumed.status_code == 202 and resumed.json()["request_key"] == "resume-1"
