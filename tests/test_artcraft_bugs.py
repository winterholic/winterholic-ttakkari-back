"""BUGS.md 파서 단위 테스트(DB·파일 없음)."""
from ttakkari import artcraft_bugs

APPS = {"alpha", "beta"}


def entry(bug_id="BUG-010", **over):
    meta = {"Repos": "storytold/alpha", "Apps": "alpha", "Severity": "S3", "Kind": "issue", "Difficulty": "easy",
            "Upstream": "unchecked", "Found": "2026-10-18", "Verified": "yes", **over}
    lines = "\n".join(f"- **{k}:** {v}" for k, v in meta.items() if v is not None)
    return f"## {bug_id}: title {bug_id}\n\n{lines}\n\n### Summary\n\nS\n\n### Repro\n\nR\n\n### Evidence\n\nE\n\n"


def test_empty_and_unrelated_text():
    assert artcraft_bugs.parse_bugs("", APPS) == []
    assert artcraft_bugs.parse_bugs("# BUGS\n\n| ID |\n|---|\n\n## Things checked\nx\n", APPS) == []


def test_well_formed_entry_has_no_problems_and_new_entries_appear():
    text = entry("BUG-008") + entry("BUG-009", Apps="alpha, beta", Kind="pr")
    bugs = artcraft_bugs.parse_bugs(text, APPS)
    assert [b["id"] for b in bugs] == ["BUG-008", "BUG-009"]
    assert all(not b["problems"] for b in bugs)
    assert bugs[1]["apps"] == ["alpha", "beta"] and bugs[1]["kind"] == "pr"


def test_upstream_states_and_notes():
    parse = lambda value: artcraft_bugs.parse_bugs(entry(Upstream=value), APPS)[0]["upstream"]
    assert parse("unchecked") == {"state": "unchecked", "note": ""}
    assert parse("none: checked 2026-10-18") == {"state": "none", "note": "checked 2026-10-18"}
    assert parse("related — alpha#1, alpha#2") == {"state": "related", "note": "alpha#1, alpha#2"}
    assert parse("duplicate of alpha#7")["state"] == "duplicate"
    bad = artcraft_bugs.parse_bugs(entry(Upstream="whatever"), APPS)[0]
    assert bad["upstream"]["state"] == "unchecked" and any("Upstream" in p for p in bad["problems"])


def test_all_expands_to_registry_and_first_duplicate_id_wins():
    bugs = artcraft_bugs.parse_bugs(entry(Apps="all") + entry(Apps="beta"), APPS)
    assert len(bugs) == 1 and bugs[0]["apps"] == ["alpha", "beta"]


def test_missing_metadata_is_reported_not_hidden():
    bug = artcraft_bugs.parse_bugs(entry(Kind=None, Difficulty="impossible"), APPS)[0]
    assert bug["kind"] is None and bug["difficulty"] is None
    assert any("Kind" in p for p in bug["problems"]) and any("Difficulty" in p for p in bug["problems"])


def test_long_sections_are_cut_and_flagged():
    text = entry().replace("### Evidence\n\nE", "### Evidence\n\n" + "x" * (artcraft_bugs.SECTION_LIMIT + 50))
    bug = artcraft_bugs.parse_bugs(text, APPS)[0]
    assert len(bug["sections"]["evidence"]) == artcraft_bugs.SECTION_LIMIT and bug["truncated"] == ["evidence"]


def test_handoff_mentions_every_identifier_and_the_posting_guard():
    bug = artcraft_bugs.parse_bugs(entry(Kind="pr", Apps="all"), APPS)[0]
    text = artcraft_bugs.handoff(bug)
    for needle in ("BUG-010", "storytold/alpha", "alpha, beta", "PR 가능", "BUGS.md"):
        assert needle in text, needle
    assert "올리라고 말한 뒤에만" in text
