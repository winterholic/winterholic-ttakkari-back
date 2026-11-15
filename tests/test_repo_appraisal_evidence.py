"""증거 패킷(글자 상한·인용 경로)과 LLM 판정(JSON 파싱·desk 재사용 호출)을 확인한다."""
from __future__ import annotations

from ttakkari.repo_appraisal import evidence as evidence_mod
from ttakkari.repo_appraisal import judge as judge_mod
from ttakkari.runs import desk as desk_mod


def _facts():
    return {
        "tree": [{"path": p, "size": 100, "type": "blob"} for p in
                 ("README.md", "pyproject.toml", "src/a.py", "src/b.py", "tests/t.py",
                  "docs/g.md", "AGENTS.md")],
        "readme_text": "# t\n" + "본문 " * 5000,
        "repo_meta": {"language": "Python"},
        "samples": {"src/a.py": "x = 1\n" * 3000, "src/b.py": "y = 2\n",
                    "tests/t.py": "def test_x():\n    assert True\n"},
        "harness_found": ["AGENTS.md"],
        "harness_texts": {"AGENTS.md": "# 지침\n" + "규칙 " * 3000},
    }


def _packet(**over):
    kw = {"owner": "acme", "repo": "widget", "head_sha": "a" * 40, "repo_type": "library",
          "facts": _facts(), "tree_truncated": False, "mode": "quick", "max_chars": 40000}
    kw.update(over)
    return evidence_mod.build_packet(**kw)


def test_packet_within_budget_and_sections():
    packet = _packet()
    assert packet.evidence_chars == len(packet.text) <= 40000
    assert "evidence:tree" in packet.text and "evidence:readme" in packet.text
    assert "src/a.py" in packet.text and "AGENTS.md" in packet.text


def test_valid_paths_match_included_files():
    packet = _packet()
    assert packet.valid_paths <= {"src/a.py", "src/b.py", "tests/t.py", "AGENTS.md", "README.md"}
    assert "src/a.py" in packet.valid_paths
    for path in packet.valid_paths:
        assert path in packet.text


def test_deep_budget_doubles_and_slices_grow():
    quick = _packet(mode="quick", max_chars=40000)
    deep = _packet(mode="deep", max_chars=40000)
    assert len(deep.text) > len(quick.text) <= 80000


def test_over_budget_file_sliced_with_marker():
    packet = _packet(max_chars=40000)
    assert "중간 생략" in packet.text  # src/a.py·README 가 잘린다


def test_tiny_budget_keeps_header_and_tree():
    packet = _packet(max_chars=800)
    assert len(packet.text) <= 800 and "acme/widget" in packet.text


def test_empty_facts_minimal_packet():
    packet = _packet(facts={"tree": [], "readme_text": "", "repo_meta": {},
                            "samples": {}, "harness_found": [], "harness_texts": {}})
    assert packet.valid_paths == set() and packet.evidence_chars > 0


def test_truncated_tree_noted():
    assert "잘림" in _packet(tree_truncated=True).text


def test_judge_prompt_names_pending_items():
    prompt = judge_mod.build_prompt("PACKET", "library", ["code_quality.readability"])
    assert "PACKET" in prompt and "code_quality.readability" in prompt and "JSON" in prompt


def test_judge_parses_fenced_json():
    out = judge_mod.parse_output('```json\n{"scores": [], "summary": "ok"}\n```')
    assert out == {"scores": [], "summary": "ok"}


def test_judge_rejects_non_dict_and_missing_scores():
    assert judge_mod.parse_output("전혀 JSON 아님") is None
    assert judge_mod.parse_output('{"summary": "x"}') is None
    assert judge_mod.parse_output('{"scores": {}}') is None


async def test_judge_default_runner_reuses_desk_oneshot(monkeypatch):
    seen = {}

    async def fake_ask(prof, system, prompt, **kw):
        seen.update({"engine": prof.engine, "system": system, "prompt": prompt, **kw})
        return desk_mod.Attempt('{"scores": []}', None, 5)

    monkeypatch.setattr(desk_mod, "_ask", fake_ask)
    text, error = await judge_mod.call_model("SYS", "P", engine="codex", model="m1", timeout=7.0)
    assert (text, error) == ('{"scores": []}', None)
    assert seen["engine"] == "codex" and seen["cfg_override"] == {"model": "m1"}
    assert seen["prompt_via_stdin"] is True and seen["use_warm"] is False
    assert seen["timeout_seconds"] == 7.0


async def test_judge_custom_runner_called_once():
    calls = []

    async def runner(system, prompt):
        calls.append((system, prompt))
        return "{}", None

    await judge_mod.call_model("S", "P", engine="codex", model="m", timeout=1.0, runner=runner)
    assert len(calls) == 1
