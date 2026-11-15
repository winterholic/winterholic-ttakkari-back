"""저장소 가치 평가 루브릭 순수 함수: 유형 분류·결정론 점수·가중·등급·약점 Top3(ADR 0073)."""
from __future__ import annotations

import pytest

from ttakkari.repo_appraisal import rubric


def _facts(**over) -> dict:
    base = {
        "tree": [], "readme_text": "", "description": "",
        "repo_meta": {"default_branch": "main", "license": None, "open_issues_count": 0,
                      "archived": False, "fork": False, "language": None, "age_days": 400},
        "commits": [], "contributors": [], "releases": [],
        "issues_sample": [], "pulls_sample": [],
        "workflows": [], "samples": {}, "harness_found": [], "harness_texts": {},
    }
    base.update(over)
    return base


def _tree(*paths, sizes=None):
    return [{"path": p, "size": (sizes or {}).get(p, 1000), "type": "blob"} for p in paths]


# ---- 유형 분류 ----

def test_classify_docs_by_markdown_ratio():
    facts = _facts(tree=_tree(*[f"guide/{i}.md" for i in range(8)], "README.md"),
                   readme_text="# 문서\n" + "가이드 " * 500)
    kind, reasons = rubric.classify_repo_type(facts)
    assert kind == "docs" and reasons


def test_classify_library_by_manifest_and_tests():
    facts = _facts(tree=_tree("pyproject.toml", "src/acme/__init__.py", "src/acme/core.py",
                              "tests/test_core.py", "README.md"),
                   readme_text="# acme\n" + "설명 " * 200)
    assert rubric.classify_repo_type(facts)[0] == "library"


def test_classify_app_by_entrypoint_and_deploy():
    facts = _facts(tree=_tree("package.json", "src/main.ts", "Dockerfile", "README.md"),
                   readme_text="# app\n" + "설명 " * 200)
    assert rubric.classify_repo_type(facts)[0] == "app"


def test_classify_experiment_when_tiny_and_bare():
    facts = _facts(tree=_tree("poc.py", "notes.md"), readme_text="") 
    assert rubric.classify_repo_type(facts)[0] == "experiment"


def test_classify_unknown_when_ambiguous():
    facts = _facts(tree=_tree("a.txt", "b.txt", "c.md", "d.md", "e.md", "f.py", "g.py",
                              "h.py", "i.py", "j.py", "k.py", "l.py", "m.py", "n.py",
                              "o.py", "p.py", "q.py", "r.py", "s.py", "t.py"),
                   readme_text="") 
    kind, _ = rubric.classify_repo_type(facts)
    assert kind == "unknown"


def test_classify_unknown_when_empty_tree():
    assert rubric.classify_repo_type(_facts())[0] == "unknown"


# ---- 결정론 점수: 근거 없는 점수 없음·후하지 않음 ----

def test_tests_ratio_bands_and_evidence():
    files = [f"src/m{i}.py" for i in range(10)]
    scored = rubric.score_code_quality(_facts(tree=_tree(*files, *[f"tests/t{i}.py" for i in range(8)]),
                                              samples={}))
    tests = next(i for i in scored if i.item_id == "tests")
    assert tests.score == 5 and tests.evidence["ratio"] == pytest.approx(0.8)
    scored = rubric.score_code_quality(_facts(tree=_tree(*files), samples={}))
    tests = next(i for i in scored if i.item_id == "tests")
    assert tests.score == 0 and tests.evidence["test_files"] == 0


def test_tests_item_skipped_for_docs_type():
    scored = rubric.score_code_quality(_facts(tree=_tree("a.md"), samples={}), repo_type="docs")
    tests = next(i for i in scored if i.item_id == "tests")
    assert tests.status == "skipped" and tests.score is None


def test_lint_config_needs_all_three_categories_for_five():
    full = _facts(tree=_tree("pyproject.toml", "ruff.toml", "mypy.ini", ".editorconfig", "a.py"), samples={})
    item = next(i for i in rubric.score_code_quality(full) if i.item_id == "lint_config")
    assert item.score == 5 and set(item.evidence["categories"]) == {"lint", "format", "types"}
    bare = _facts(tree=_tree(".editorconfig", "a.py"), samples={})
    item = next(i for i in rubric.score_code_quality(bare) if i.item_id == "lint_config")
    assert item.score == 1


def test_oversize_files_ratio_bands():
    paths = [f"src/m{i}.py" for i in range(20)]
    sizes = {paths[0]: 200_000}
    facts = _facts(tree=_tree(*paths, sizes=sizes), samples={})
    item = next(i for i in rubric.score_code_quality(facts) if i.item_id == "file_size")
    assert item.score == 3 and item.evidence["oversize"] == 1
    facts = _facts(tree=_tree(*paths), samples={})
    item = next(i for i in rubric.score_code_quality(facts) if i.item_id == "file_size")
    assert item.score == 5


def test_todo_density_unavailable_without_samples():
    item = next(i for i in rubric.score_code_quality(_facts()) if i.item_id == "todo_density")
    assert item.status == "unavailable" and item.score is None


def test_todo_density_counts_per_kloc():
    text = ("x = 1  # TODO: 고치기\n" * 2) + ("y = 2\n" * 998)
    facts = _facts(tree=_tree("a.py"), samples={"a.py": text})
    item = next(i for i in rubric.score_code_quality(facts) if i.item_id == "todo_density")
    assert item.score == 3 and item.evidence["todos"] == 2


def test_build_lock_prefers_both():
    facts = _facts(tree=_tree("pyproject.toml", "uv.lock", "a.py"), samples={})
    item = next(i for i in rubric.score_code_quality(facts) if i.item_id == "build_lock")
    assert item.score == 5
    facts = _facts(tree=_tree("a.py"), samples={})
    item = next(i for i in rubric.score_code_quality(facts) if i.item_id == "build_lock")
    assert item.score == 0


def test_structure_rewards_depth_and_spread():
    facts = _facts(tree=_tree("src/a/x.py", "src/b/y.py", "tests/t.py", "docs/g.md", "README.md"))
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "structure")
    assert item.score == 5 and item.evidence["max_depth"] == 2
    facts = _facts(tree=_tree(*[f"m{i}.py" for i in range(40)]))
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "structure")
    assert item.score == 0


def test_docs_adr_counts_readme_docs_and_adr():
    facts = _facts(tree=_tree("README.md", "docs/guide.md", "docs/adr/0001-x.md", "docs/adr/0002-y.md"),
                   readme_text="# t\n" + "본문 " * 800)
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "docs_adr")
    assert item.score == 5
    item = next(i for i in rubric.score_architecture(_facts()) if i.item_id == "docs_adr")
    assert item.score == 0


def test_entrypoints_manifest_plus_file_is_five():
    facts = _facts(tree=_tree("pyproject.toml", "src/acme/__main__.py"))
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "entrypoints")
    assert item.score == 5 and item.evidence["entries"]


def test_cycles_sample_limited_never_five():
    samples = {"a.py": "import b\n", "b.py": "x = 1\n"}
    facts = _facts(tree=_tree("a.py", "b.py"), samples=samples)
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "cycles")
    assert item.score == 4 and item.evidence["pairs"] == []
    samples = {"a.py": "import b\n", "b.py": "import a\n"}
    facts = _facts(tree=_tree("a.py", "b.py"), samples=samples)
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "cycles")
    assert item.score == 2 and item.evidence["pairs"] == [["a.py", "b.py"]]


def test_cycles_unavailable_with_few_samples():
    facts = _facts(tree=_tree("a.py"), samples={"a.py": "x=1"})
    item = next(i for i in rubric.score_architecture(facts) if i.item_id == "cycles")
    assert item.status == "unavailable"


def test_maintainability_recency_and_activity():
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    commits = [{"sha": f"{i:040x}"[-40:], "date": (now - timedelta(days=i * 20)).isoformat(),
                "author": "a"} for i in range(12)]
    facts = _facts(commits=commits)
    by_id = {i.item_id: i for i in rubric.score_maintainability(facts)}
    assert by_id["recent_commits"].score == 3 and by_id["recent_commits"].evidence["count"] == 5
    assert by_id["last_commit"].score == 5
    assert by_id["active_months"].score == 4
    assert by_id["active_months"].evidence["months"] in (7, 8, 9)  # 월 경계에 따라 파동


def test_bus_factor_solo_scores_one():
    facts = _facts(contributors=[{"login": "solo", "contributions": 120}])
    item = next(i for i in rubric.score_maintainability(facts) if i.item_id == "contributors_bus")
    assert item.score == 1 and item.evidence["bus"] == 1


def test_ci_releases_license_and_hygiene():
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    releases = [{"tag": f"v{i}", "published_at": (now - timedelta(days=30 * i)).isoformat(),
                 "draft": False, "prerelease": False} for i in range(1, 5)]
    issues = [{"state": "closed"}] * 24 + [{"state": "open"}] * 6
    facts = _facts(workflows=[".github/workflows/ci.yml", ".github/workflows/docs.yml"],
                   releases=releases, issues_sample=issues, pulls_sample=[],
                   repo_meta={"default_branch": "main", "license": "mit", "open_issues_count": 6,
                              "archived": False, "fork": False, "language": None, "age_days": 400})
    by_id = {i.item_id: i for i in rubric.score_maintainability(facts)}
    assert by_id["ci"].score == 4
    assert by_id["releases"].score == 4
    assert by_id["issue_hygiene"].score == 4
    assert by_id["license"].score == 5


def test_issue_hygiene_unavailable_on_tiny_sample():
    facts = _facts(issues_sample=[{"state": "open"}], pulls_sample=[])
    item = next(i for i in rubric.score_maintainability(facts) if i.item_id == "issue_hygiene")
    assert item.status == "unavailable"


def test_none_endpoints_become_unavailable_not_zero():
    facts = _facts(releases=None, issues_sample=None, pulls_sample=None, workflows=None,
                   contributors=None)
    by_id = {i.item_id: i for i in rubric.score_maintainability(facts)}
    assert by_id["releases"].status == "unavailable"
    assert by_id["issue_hygiene"].status == "unavailable"
    assert by_id["ci"].status == "unavailable"
    assert by_id["contributors_bus"].status == "unavailable"


def test_stats_months_union_with_commit_dates():
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    facts = _facts(commits=[{"sha": "a" * 40, "date": now.isoformat(), "author": "a"}],
                   stats_months=["2025-01", "2025-02", "2025-03"])
    item = next(i for i in rubric.score_maintainability(facts) if i.item_id == "active_months")
    assert item.evidence["months"] >= 4


def test_harness_presence_and_length_and_refs():
    found = ["AGENTS.md", "CLAUDE.md", ".claude/skills/a/SKILL.md", "MEMORY.md"]
    texts = {"AGENTS.md": "# 지침\n짧다\n",
             "CLAUDE.md": "줄\n" * 500,
             ".claude/skills/a/SKILL.md": "본문 [깨짐](gone.md)\n",
             "MEMORY.md": "기억\n"}
    facts = _facts(tree=_tree(*found, "src/a.py"), harness_found=found, harness_texts=texts)
    by_id = {i.item_id: i for i in rubric.score_ai_harness(facts)}
    assert by_id["presence"].score == 4
    assert by_id["length"].score == 3 and by_id["length"].evidence["over"] == ["CLAUDE.md"]
    assert by_id["refs"].score == 3 and by_id["refs"].evidence["broken"]


def test_harness_axis_na_without_files():
    scored = rubric.score_ai_harness(_facts())
    assert all(i.status == "na" for i in scored)


def test_harness_duplicates_flagged():
    found = [".claude/skills/a/SKILL.md", ".agents/skills/a/SKILL.md", "AGENTS.md"]
    facts = _facts(tree=_tree(*found), harness_found=found, harness_texts={p: "x\n" for p in found})
    item = next(i for i in rubric.score_ai_harness(facts) if i.item_id == "dup_names")
    assert item.score == 3 and item.evidence["groups"]


# ---- LLM 점수 적용: 인용 검증·clamp ----

def test_apply_llm_rejects_missing_citation():
    items = [rubric.ItemScore("code_quality", "readability", None, "pending", {})]
    out = rubric.apply_llm_scores(
        items, {"scores": [{"item": "code_quality.readability", "score": 4,
                            "citations": ["src/nope.py"], "confidence": "high"}]},
        valid_paths={"src/a.py"})
    assert out[0].status == "unverified" and out[0].score is None


def test_apply_llm_rejects_empty_citation():
    items = [rubric.ItemScore("code_quality", "readability", None, "pending", {})]
    out = rubric.apply_llm_scores(
        items, {"scores": [{"item": "code_quality.readability", "score": 4,
                            "citations": [], "confidence": "high"}]},
        valid_paths={"src/a.py"})
    assert out[0].status == "unverified"


def test_apply_llm_clamps_and_accepts_evidence_id():
    items = [rubric.ItemScore("originality", "verdict", None, "pending", {})]
    out = rubric.apply_llm_scores(
        items, {"scores": [{"item": "originality.verdict", "score": 9,
                            "citations": ["evidence:readme"], "confidence": "low"}]},
        valid_paths={"README.md"})
    assert out[0].score == 5 and out[0].status == "scored"
    assert out[0].evidence.get("low_reliability") is True


def test_apply_llm_ignores_unknown_item():
    out = rubric.apply_llm_scores([], {"scores": [{"item": "nope.x", "score": 3,
                                                  "citations": ["a"], "confidence": "low"}]},
                                  valid_paths={"a"})
    assert out == []


# ---- 가중·등급·Top3 ----

def test_weights_sum_to_one_per_type():
    for repo_type, weights in rubric.WEIGHTS.items():
        assert set(weights) == set(rubric.AXES), repo_type
        assert sum(weights.values()) == pytest.approx(1.0), repo_type


def test_weighted_score_renormalizes_na_axis():
    axes = {"code_quality": 4.0, "architecture": 4.0, "maintainability": 4.0,
            "ai_harness": None, "originality": 2.0}
    score, contrib = rubric.weighted_score(axes, "unknown")
    # 가중치표 unknown 행 기준: (80*.25 + 80*.20 + 80*.30 + 40*.10) / .85
    assert score == pytest.approx(75.29, abs=0.05)
    assert contrib["ai_harness"] == 0.0


def test_weighted_score_all_null_is_zero():
    score, _ = rubric.weighted_score({a: None for a in rubric.AXES}, "unknown")
    assert score == 0.0


@pytest.mark.parametrize(("score", "grade"), [(100, "A+"), (90, "A+"), (89.9, "A"), (85, "A"),
                                              (84.9, "B+"), (80, "B+"), (79.9, "B"), (70, "B"),
                                              (69.9, "C"), (60, "C"), (59.9, "D"), (0, "D")])
def test_grade_bands(score, grade):
    assert rubric.grade_for(score) == grade


def test_top_weaknesses_orders_by_weighted_loss():
    items = [rubric.ItemScore("code_quality", "tests", 1.0, "scored", {}),
             rubric.ItemScore("maintainability", "license", 0.0, "scored", {}),
             rubric.ItemScore("architecture", "docs_adr", 2.0, "scored", {}),
             rubric.ItemScore("originality", "verdict", 5.0, "scored", {})]
    axes = {"code_quality": 1.0, "architecture": 2.0, "maintainability": 0.0,
            "ai_harness": None, "originality": 5.0}
    top = rubric.top_weaknesses(items, axes, "unknown")
    assert [t["item"] for t in top] == ["maintainability.license", "code_quality.tests",
                                        "architecture.docs_adr"]
    assert all(t["advice"] for t in top)


def test_top_weaknesses_skips_perfect_and_unscored():
    items = [rubric.ItemScore("code_quality", "tests", 5.0, "scored", {}),
             rubric.ItemScore("code_quality", "lint_config", None, "unavailable", {})]
    axes = {"code_quality": 5.0, "architecture": None, "maintainability": None,
            "ai_harness": None, "originality": None}
    assert rubric.top_weaknesses(items, axes, "unknown") == []


def test_axis_scores_average_scored_only():
    items = [rubric.ItemScore("code_quality", "tests", 4.0, "scored", {}),
             rubric.ItemScore("code_quality", "lint_config", None, "unavailable", {}),
             rubric.ItemScore("code_quality", "file_size", 2.0, "scored", {})]
    assert rubric.axis_scores({"code_quality": items})["code_quality"] == pytest.approx(3.0)


def test_item_defs_cover_declared_items():
    for axis, ids in rubric.ITEMS.items():
        assert ids, axis
        for item_id in ids:
            assert f"{axis}.{item_id}" in rubric.ADVICE or axis == "originality", item_id
