"""Independent regression checks for limits whose old work-scope assertions were replaced."""

import pytest
from context_sharing_support import FakeGateway, agent_headers, make_project

from ttakkari.api import context_sharing as api
from ttakkari.context_sharing.gateway import GatewayControl
from ttakkari.main import app


@pytest.fixture
def gateway():
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(
        fake, public_url="https://ctx.example.test"
    )
    yield fake
    app.dependency_overrides.pop(api.get_gateway, None)


async def test_personal_publish_rejects_more_than_max_documents(client, gateway):
    project = await make_project(
        "FinalContractMany",
        {f"doc-{index}.md": f"synthetic document {index}" for index in range(51)},
    )
    headers = await agent_headers()
    response = await client.post(
        "/api/agent/context-sharing/publish",
        headers=headers,
        json={"entity_id": project["entity_id"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "too_many_documents"
    assert gateway.calls == []


async def test_personal_publish_rejects_document_over_character_limit(client, gateway):
    project = await make_project("FinalContractLarge", {"large.md": "x" * 200_001})
    headers = await agent_headers()
    response = await client.post(
        "/api/agent/context-sharing/publish",
        headers=headers,
        json={"entity_id": project["entity_id"]},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "draft_too_large"
    assert gateway.calls == []
