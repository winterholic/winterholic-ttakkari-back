import json
from collections import Counter

from ttakkari.creative.v2 import registry_data
from ttakkari.creative.v2.capabilities import (
    CAPABILITIES,
    GLOBAL_PREFERS,
    GLOBAL_REQUIRES,
    LEVELS,
)
from ttakkari.creative.v2.catalog_data import CATALOG_NAMES, ENTRIES
from ttakkari.creative.v2.probe import PROBE_IDS
from ttakkari.creative.v2.registry import SNAPSHOT_PATH, Registry


def snap(probes=None, records=None, skills=None):
    return {
        "schema": 1,
        "generated_at": "t",
        "host": {},
        "command": "",
        "probes": probes or {},
        "records": records or {},
        "skills": skills or {},
    }


def p(ok):
    return {"installed": ok, "version": None, "detail": ""}


def test_twelve_catalogs_with_required_entry_shape():
    assert sorted(CATALOG_NAMES) == [f"C{i:02d}" for i in range(1, 13)]
    counts = Counter(e.catalog for e in ENTRIES.values())
    assert all(counts[c] >= 9 for c in CATALOG_NAMES)
    assert counts["C01"] == 15 and counts["C03"] == 19 and counts["C12"] == 10
    for entry in ENTRIES.values():
        assert (
            entry.id == f"{entry.catalog}.{entry.slug}"
            and entry.version
            and entry.knowledge
        )
        assert entry.evidence_status in ("linked", "none")


def test_every_relation_target_resolves_to_entry_or_capability():
    for entry in ENTRIES.values():
        for rel in entry.relations:
            for target in rel.target.split("|"):
                if rel.type in ("produces",):
                    continue
                assert target in ENTRIES or target in CAPABILITIES, (
                    entry.id,
                    rel.type,
                    target,
                )
                if rel.type in (
                    "conflicts_with",
                    "alternative_to",
                    "compatible_with",
                    "validates_with",
                ):
                    assert target in ENTRIES, (entry.id, target)


def test_relations_cover_all_seven_types():
    kinds = {rel.type for e in ENTRIES.values() for rel in e.relations}
    assert kinds >= {
        "requires",
        "prefers",
        "compatible_with",
        "conflicts_with",
        "alternative_to",
        "validates_with",
    }


def test_unevidenced_entries_do_not_fake_evidence():
    assert ENTRIES["C03.japanese_anime"].evidence_status == "none"
    assert ENTRIES["C01.music_video"].evidence_status == "linked"


def test_twenty_recipes_reference_existing_entries_and_capabilities():
    recipes = registry_data.RECIPES
    assert [r.id for r in recipes] == [f"R{i:02d}" for i in range(1, 21)]
    for r in recipes:
        for e in [*r.fits, *r.conflicts]:
            assert e in ENTRIES, (r.id, e)
        for group in [*r.requires, *r.prefers]:
            assert group and all(c in CAPABILITIES for c in group), (r.id, group)


def test_r19_cases_and_requirements_do_not_claim_a_complete_quality_pass():
    recipe = next(recipe for recipe in registry_data.RECIPES if recipe.id == "R19")
    music_video_cases = ENTRIES["C01.music_video"].cases
    assert all(case.outcome == "partial" for case in music_video_cases)
    assert ["anim.lipsync"] not in recipe.requires
    assert ["anim.lipsync"] in recipe.prefers
    assert any("춤" in constraint for constraint in recipe.constraints)
    assert any("품질 통과" in constraint for constraint in recipe.constraints)
    evaluation = Registry().evaluate_recipe("R19")
    assert evaluation.verdict == "recipe_demonstrated"
    assert "품질 통과가 아니며 완성된 레시피를 입증하지 않는다" in evaluation.summary
    assert any("conditional" in case for case in evaluation.case_evidence)
    assert any("unreviewed" in case for case in evaluation.case_evidence)
    assert all(c in CAPABILITIES for c in [*GLOBAL_REQUIRES, *GLOBAL_PREFERS])


def test_registry_claims_and_probe_ids_are_consistent():
    entries = [*registry_data.TOOLS, *registry_data.MODELS, *registry_data.SKILLS]
    assert len({e.id for e in entries}) == len(entries)
    for e in entries:
        for claim in e.claims:
            assert claim.capability in CAPABILITIES, (e.id, claim.capability)
        for probe in e.probes:
            assert probe in PROBE_IDS or probe.startswith("skill:"), (e.id, probe)
        for dep in e.depends_on:
            assert dep in {x.id for x in entries}, (e.id, dep)


def test_committed_snapshot_covers_registry_references():
    snapshot = json.loads(SNAPSHOT_PATH.read_text())
    assert set(PROBE_IDS) <= set(snapshot["probes"])
    for e in registry_data.SKILLS:
        for probe in e.probes:
            if probe.startswith("skill:"):
                assert probe[6:] in snapshot["skills"], probe
    for e in [*registry_data.TOOLS, *registry_data.MODELS, *registry_data.SKILLS]:
        for claim in e.claims:
            for slug in claim.records:
                assert slug in snapshot["records"], (e.id, slug)
    assert "/Users/" not in SNAPSHOT_PATH.read_text()


def test_without_snapshot_nothing_is_called_installed_or_executed():
    reg = Registry(use_default_snapshot=False)
    for s in reg.states():
        if s.id == "model.multimodal_judge":
            assert s.installed is False  # 미등록은 스냅샷이 없어도 미설치다
            continue
        assert s.installed is None
        assert set(s.levels.values()) <= {"declared"}
    assert all(m.verdict == "blocked" for m in reg.recipe_matrix())


def test_missing_probe_makes_claims_missing_and_blocks_recipe():
    reg = Registry(
        snap(probes={pid: p(True) for pid in PROBE_IDS} | {"godot": p(False)})
    )
    godot = reg.state("tool.godot")
    assert godot.installed is False and set(godot.levels.values()) == {"missing"}
    r13 = reg.evaluate_recipe("R13")
    assert r13.verdict == "blocked" and r13.blockers


def test_installed_without_record_is_not_executed_and_record_evidence_raises_level():
    probes = {pid: p(True) for pid in PROBE_IDS}
    records = {
        "remotion-intro-v1-v2": {
            "status": "completed",
            "verdict": "conditional",
            "independent": False,
            "media_count": 3,
            "kind": "x",
            "title": "t",
            "path": "",
        }
    }
    reg = Registry(snap(probes=probes, records=records))
    remotion = reg.state("tool.remotion")
    assert remotion.levels["render.dataviz"] == "installed"
    assert remotion.levels["render.react_motion"] == "executed"
    assert any(
        "remotion-beat-v1-v2" in e and "기록 없음" in e
        for e in remotion.evidence["render.react_motion"]
    )


def test_partial_record_is_partial_and_independent_pass_is_verified():
    probes = {pid: p(True) for pid in PROBE_IDS}
    records = {
        "comfy-qwen-image-edit-2511": {
            "status": "partial",
            "verdict": "conditional",
            "independent": False,
            "media_count": 5,
            "kind": "x",
            "title": "t",
            "path": "",
        },
        "acestep-ko-ballad-run1": {
            "status": "completed",
            "verdict": "pass",
            "independent": True,
            "media_count": 2,
            "kind": "x",
            "title": "t",
            "path": "",
        },
    }
    reg = Registry(snap(probes=probes, records=records))
    assert (
        reg.state("model.qwen_image_edit_2511").levels["gen.image_edit"]
        == "executed_partial"
    )
    assert reg.state("model.acestep15").levels["audio.music_generate"] == "verified"


def test_skill_without_code_is_capped_at_declared_and_missing_dependency_propagates():
    probes = {pid: p(True) for pid in PROBE_IDS} | {"three": p(False)}
    skills = {
        s.id.removeprefix("skill."): {"has_code": s.id != "skill.ttakkari-motion-rules"}
        for s in registry_data.SKILLS
    }
    reg = Registry(snap(probes=probes, skills=skills))
    assert (
        reg.state("skill.ttakkari-motion-rules").levels["render.kinetic_typo"]
        == "declared"
    )
    assert "실행 코드 없음" in reg.state("skill.ttakkari-motion-rules").blockers[0]
    assert reg.state("skill.studio-3d-cinematic").installed is False
    assert set(reg.state("skill.studio-3d-cinematic").levels.values()) == {"missing"}


def test_recipe_verdict_separates_capability_evidence_from_recipe_evidence():
    reg = Registry()
    verdicts = {m.recipe_id: m for m in reg.recipe_matrix()}
    assert len(verdicts) == 20
    for m in verdicts.values():
        if m.verdict == "blocked":
            assert m.blockers
        if m.verdict in ("recipe_demonstrated", "studio_executable"):
            assert any("completed" in c for c in m.case_evidence)
        if m.verdict == "capabilities_executed":
            assert not any("completed" in c for c in m.case_evidence)
    assert not any(m.verdict == "studio_executable" for m in verdicts.values()), (
        "스튜디오 제작 런타임에 연결된 레시피는 아직 없다"
    )


def test_no_blocked_group_hides_behind_executed_level():
    reg = Registry()
    for m in reg.recipe_matrix():
        levels = [g.level for g in m.groups]
        worst = min(LEVELS.index(lv) for lv in levels)
        expected = {
            0: "blocked",
            1: "blocked",
            2: "unverified",
            3: "partially_demonstrated",
        }.get(worst)
        if expected:
            assert m.verdict == expected, (m.recipe_id, levels)


def test_committed_capability_matrix_doc_is_generated_from_current_code():
    from pathlib import Path

    from ttakkari.creative.v2.report import matrix_markdown

    doc = (
        Path(__file__).resolve().parents[1]
        / "docs/design/creative-studio-v2-capability-matrix.md"
    )
    assert doc.read_text() == matrix_markdown(), (
        "python -m ttakkari.creative.v2.report 로 문서를 다시 생성하세요."
    )
