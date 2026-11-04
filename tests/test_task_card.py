"""작업 카드: 항목 필드를 항상 같은 읽기 쉬운 틀로 조립해 작업자에게 보낸다."""
import inspect

import pytest
from test_orchestrator import _orch, _session

from ttakkari.orchestrator import mcp_server
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import task_card
from ttakkari.runs.task_card import TaskCard

CARD = {
    "goal": "Nemotron Nano 4B 의 기존 78행 실패 원인을 행별로 분류한다.",
    "location": "/Users/me/regna-ios",
    "read_first": ["AGENTS.md", "docs/research/core-agent-20261103/RESEARCH.md"],
    "limits": ["쓰기는 docs/research/core-agent-20261103/nemotron/ 에만 한다.", "다른 작업자 파일은 수정하지 않는다."],
    "done_criteria": ["78행 모두 최초 원인과 후속 증상으로 분류했다.", "근거 없는 능력 한계 단정이 없다."],
    "verification": ["python3 -m unittest discover -s tools/core_agent_research/nemotron"],
    "context": "이전 단계에서 27행은 성공했고 51행은 잘림과 형식 오류로 실패했다. 원시 응답은 보존되어 있다.",
}


def test_render_has_fixed_readable_format():
    text = task_card.render(TaskCard(**CARD))
    assert text.startswith("[작업 카드]\n목표: Nemotron Nano 4B")
    for heading in ("작업 위치:", "먼저 읽을 것:", "범위와 금지:", "완료 기준:", "검증 방법:", "배경과 결정된 것:", "보고 형식:"):
        assert heading in text, heading
    assert "1. 78행 모두 최초 원인" in text and "2. 근거 없는 능력 한계" in text  # 완료 기준은 번호
    assert "- AGENTS.md" in text
    assert text.rstrip().endswith("막혔으면 시도한 것, 관찰한 오류, 다음 후보를 적는다.")
    assert "`상태: 완료 | 부분 | 막힘`" in text  # 보고 형식은 틀에 고정


def test_optional_sections_are_omitted_and_extra_is_appended():
    text = task_card.render(TaskCard(goal="보고서를 만든다.", done_criteria=["파일이 저장됐다."]), "추가로 표를 넣어라.")
    assert "작업 위치:" not in text and "범위와 금지:" not in text and "검증 방법:" not in text
    assert "추가 지시:\n추가로 표를 넣어라." in text
    assert task_card.compose("그냥 지시", None) == "그냥 지시"


def test_done_criteria_is_required():
    with pytest.raises(ValueError):
        TaskCard(goal="x", done_criteria=[])


def test_tool_signature_keeps_prompt_first_and_adds_card_fields():
    params = list(inspect.signature(mcp_server.spawn_worker).parameters)
    assert params[:3] == ["title", "prompt", "engine"]  # 기존 호출 호환
    for name in ("goal", "done_criteria", "location", "read_first", "limits", "verification", "context"):
        assert name in params
    assert "goal" in inspect.signature(mcp_server.message_worker).parameters
    assert mcp_server._card(None, ["x"], None, None, None, None, None) is None  # goal 이 없으면 카드를 쓰지 않는다
    assert mcp_server._card("목표", ["기준"], None, None, None, None, "배경") == {
        "goal": "목표", "done_criteria": ["기준"], "context": "배경"}


async def test_spawn_with_card_sends_rendered_prompt(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", headers=h, json={"title": "카드", "engine": "claude", "card": CARD})
    assert r.status_code == 201, r.text
    wid = r.json()["worker_id"]
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    assert runs[0]["prompt"].startswith("[작업 카드]\n목표: Nemotron Nano 4B")
    assert "완료 기준:\n1. 78행 모두" in runs[0]["prompt"] and "보고 형식:" in runs[0]["prompt"]


async def test_spawn_needs_prompt_or_card(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    r = await client.post("/api/orchestrator/workers", headers=_orch(issue_token(orch["id"])), json={"title": "빈 지시", "engine": "claude"})
    assert r.status_code == 422, r.text


async def test_plain_prompt_still_works_and_card_context_is_checked_for_compression(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    ok = await client.post("/api/orchestrator/workers", headers=h, json={"title": "일반", "engine": "claude", "prompt": "write report"})
    assert ok.status_code == 201, ok.text
    squeezed = ("원3무효셀만재실행은앞지시와같으며먼저대조하라중앙재계산에서v3는9attempt이지만receipt는14개다따라서보고의실제모델요청18회는"
                "attempt18과HTTP를혼동한것이다추후보고에서따로집계하고미확정을구별하라원자료수정금지다새3제한도새cell을뜻하며") * 3
    bad = await client.post("/api/orchestrator/workers", headers=h, json={
        "title": "압축 배경", "engine": "claude", "card": {**CARD, "context": squeezed}})
    assert bad.status_code == 422 and "prompt_compressed" in bad.text


async def test_follow_up_with_card(client, auth, make_dir):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h, json={"title": "t", "engine": "claude", "prompt": "write report"})).json()["worker_id"]
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"card": CARD})
    assert r.status_code == 202, r.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    assert runs[-1]["prompt"].startswith("[작업 카드]")


def test_read_budget_warns_for_large_file_without_range(tmp_path):
    """큰 파일을 범위 없이 먼저 읽으라고 하면 알리고, 범위가 있거나 작은 파일이면 조용하다."""
    big = tmp_path / "docs" / "RESEARCH.md"
    big.parent.mkdir()
    big.write_text("가" * 90_000)  # 한글은 파일 크기가 3배라 270KB
    (tmp_path / "AGENTS.md").write_text("작은 파일")
    card = TaskCard(goal="g", done_criteria=["d"], read_first=[
        "AGENTS.md", "docs/RESEARCH.md", "docs/RESEARCH.md:120-260", "docs/RESEARCH.md 의 '현재 상태' 섹션"])
    got = task_card.read_budget_warnings(card, [tmp_path])
    assert len(got) == 1
    assert "docs/RESEARCH.md" in got[0] and "KB" in got[0] and "토큰" in got[0]


def test_read_budget_ignores_missing_and_absolute_paths(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * 200_000)
    card = TaskCard(goal="g", done_criteria=["d"], read_first=[f"- {big}", "없는/파일.md"])
    assert len(task_card.read_budget_warnings(card, [tmp_path / "elsewhere"])) == 1


async def test_spawn_and_follow_up_report_read_budget_warnings(client, auth, make_dir, tmp_path):
    """경고는 응답에 실리고 지시는 거절되지 않는다."""
    big = tmp_path / "RESEARCH.md"
    big.write_text("x" * 200_000)
    card = {**CARD, "read_first": [str(big), "AGENTS.md"]}
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    r = await client.post("/api/orchestrator/workers", headers=h, json={"title": "t", "engine": "claude", "card": card})
    assert r.status_code == 201, r.text
    assert len(r.json()["read_budget_warnings"]) == 1 and "RESEARCH.md" in r.json()["read_budget_warnings"][0]
    wid = r.json()["worker_id"]
    r2 = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"card": card})
    assert r2.status_code == 202, r2.text
    assert len(r2.json()["read_budget_warnings"]) == 1
    ok = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"card": CARD})
    assert ok.json()["read_budget_warnings"] == []
