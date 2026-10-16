"""Traversal must follow actual endpoints and retain space for a second hop."""

from test_knowledge import BASE, assertion, first_unit, ingest, review


async def claim(client, auth, key, subject, predicate, obj):
    body = f"{subject} {predicate} {obj}"
    u = await first_unit(client, auth, await ingest(client, auth, key=key, body=body))
    return await review(
        client,
        auth,
        await assertion(
            client, auth, u, subject=subject, predicate=predicate, object=obj
        ),
    )


async def test_two_hops_keep_budget_and_return_an_exact_evidence_path(client, auth):
    seed = await claim(
        client, auth, "generator", "MakeAIP", "official_generator_for", "AIP"
    )
    target = await claim(
        client, auth, "case-source", "AIP", "reality_test_uses_case_source", "아리아리"
    )
    await claim(client, auth, "other-first", "MakeAIP 비교표", "documents", "요금")
    result = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "MakeAIP", "hops": 2, "k": 2}
        )
    ).json()
    assert len(result["assertions"]) == 2
    byid = {a["id"]: a for a in result["assertions"]}
    assert target["id"] in byid
    path = byid[target["id"]]["retrieval"]
    assert path["hop"] == 2
    assert path["matched_entity"] == "AIP"
    assert path["path_assertion_ids"] == [seed["id"], target["id"]]


async def test_second_hop_does_not_connect_entities_by_shared_substring(client, auth):
    await claim(client, auth, "seed", "MakeAIP", "official_generator_for", "AIP")
    unrelated = await claim(
        client, auth, "unrelated", "AIP-extra", "uses", "엉뚱한 도구"
    )
    result = (
        await client.post(
            BASE + "/recall", headers=auth, json={"query": "MakeAIP", "hops": 2, "k": 8}
        )
    ).json()
    assert unrelated["id"] not in {a["id"] for a in result["assertions"]}
