"""스튜디오 키트의 DB 없는 부분: 설치(멱등·충돌·심볼릭), prompt 절, compose_prompt 연결, node 도구 테스트.

실제 요청 경로(API → Run → prompt)는 test_game_studio_kit_flow.py 가 본다(DB 필요).
node 도구 동작은 tests/studio_kit/studio_kit.test.mjs 가 검증하고, 브라우저 재생 테스트는 playwright 가 없으면 사유와 함께 skip 한다.
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ttakkari.games import studio, worker
from ttakkari.games.studio import (
    ENTRY,
    SKILL_NAME,
    STAMP,
    TARGETS,
    install_kit,
    kit_hash,
    studio_section,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "g"
    (root / ".git" / "info").mkdir(parents=True)
    return root


def test_kit_source_is_a_valid_skill_bundle():
    skill = (studio.KIT_SRC / "SKILL.md").read_text()
    assert skill.startswith("---\nname: ttakkari-game-studio\n")
    desc = skill.split("description:", 1)[1].split("\n---", 1)[0].strip()
    assert len(desc) <= 1024
    for rel in ("bin/studio.mjs", "references/production-contract.md", "references/play-and-level-spec.md", "templates/canon.json"):
        assert (studio.KIT_SRC / rel).is_file(), rel
    for ref in ("references/production-contract.md", "references/play-and-level-spec.md"):
        assert ref in skill  # 죽은 번들 참조 없이 SKILL.md 가 번들을 가리킨다


def test_install_is_idempotent_and_excluded_from_git(repo: Path):
    first = install_kit(repo)
    assert first.ok and first.status == "installed"
    for rel in TARGETS:
        assert (repo / rel / "bin" / "studio.mjs").is_file() and (repo / rel / STAMP).read_text().strip() == kit_hash()
    exclude = (repo / ".git/info/exclude").read_text()
    assert f"/.agents/skills/{SKILL_NAME}/" in exclude and f"/.claude/skills/{SKILL_NAME}/" in exclude
    again = install_kit(repo)
    assert again.ok and again.status == "current"
    assert (repo / ".git/info/exclude").read_text() == exclude  # 규칙이 중복되지 않는다


def test_install_updates_stale_kit_but_refuses_foreign_folder(repo: Path):
    install_kit(repo)
    stale = repo / TARGETS[0]
    (stale / STAMP).write_text("old\n")
    (stale / "bin" / "studio.mjs").write_text("// 낡은 사본")
    assert install_kit(repo).status == "updated"
    assert "낡은 사본" not in (stale / "bin" / "studio.mjs").read_text()
    shutil.rmtree(repo / TARGETS[1])
    (repo / TARGETS[1]).mkdir(parents=True)
    (repo / TARGETS[1] / "mine.md").write_text("사용자 파일")
    r = install_kit(repo)
    assert not r.ok and r.status == "conflict"
    assert (repo / TARGETS[1] / "mine.md").read_text() == "사용자 파일"  # 덮어쓰지 않는다


def test_install_refuses_symlinked_skill_dir(repo: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".agents").symlink_to(outside, target_is_directory=True)
    r = install_kit(repo)
    assert not r.ok and r.status == "error" and "심볼릭" in r.detail
    assert list(outside.iterdir()) == []  # 링크 밖에 쓰지 않았다


def test_install_does_not_write_through_git_exclude_symlinks(repo: Path, tmp_path: Path):
    target = tmp_path / "outside-exclude"
    target.write_text("keep\n")
    (repo / ".git/info/exclude").symlink_to(target)
    result = install_kit(repo)
    assert not result.ok and "심볼릭" in result.detail
    assert target.read_text() == "keep\n"


def test_install_refuses_symlink_stamp_before_reading_or_updating(repo: Path, tmp_path: Path):
    dest = repo / TARGETS[0]
    dest.mkdir(parents=True)
    target = tmp_path / "outside-stamp"
    target.write_text("private marker")
    (dest / STAMP).symlink_to(target)
    result = install_kit(repo)
    assert not result.ok and "심볼릭" in result.detail
    assert (dest / STAMP).is_symlink()
    assert target.read_text() == "private marker"


def test_section_variants_and_failure_is_reported_not_hidden(repo: Path):
    ok = install_kit(repo)
    dev = studio_section(ok, "modify")
    assert "SKILL.md" in dev and ENTRY in dev and "COMPLETE 가 아니면" in dev and "테스트·빌드 통과'는 완료 증거가 아니다" in dev
    ver = studio_section(ok, "verify")
    assert "독립 검증" in ver and "ledger check" in ver and "소스와 원장은 고치지 않는다" in ver and "COMPLETE 가 아니면" not in ver
    assert studio_section(None, "modify") == ""
    bad = studio_section(studio.KitInstall(False, "conflict", "충돌"), "modify")
    assert "설치하지 못했다" in bad and "충돌" in bad and "확인하지 못한 것으로 따로 적는다" in bad


def test_shared_skill_install_is_idempotent_and_never_overwrites_foreign(tmp_path: Path):
    root = tmp_path / "skills"
    first = studio.install_shared_skill(root)
    assert first.ok and first.status == "installed" and (root / SKILL_NAME / "SKILL.md").is_file()
    assert studio.install_shared_skill(root).status == "current"
    foreign = tmp_path / "s2"
    (foreign / SKILL_NAME).mkdir(parents=True)
    (foreign / SKILL_NAME / "SKILL.md").write_text("다른 스킬")
    r = studio.install_shared_skill(foreign)
    assert not r.ok and r.status == "conflict" and (foreign / SKILL_NAME / "SKILL.md").read_text() == "다른 스킬"


def _ctx(root: Path):
    game = SimpleNamespace(name="g", slug="g", engine_id="web-phaser", id="gid", locked_revision=1)
    return SimpleNamespace(game=game, workspace=SimpleNamespace(root_path=str(root)), manifest=None, lock="unreadable", build=None, failed_build=None)


def test_compose_prompt_places_contract_before_user_request_and_keeps_old_behavior(repo: Path):
    kit = install_kit(repo)
    with_kit = worker.compose_prompt(_ctx(repo), "modify", "1장에서 이동이 안 돼서 진행을 못 하겠어", None, kit)
    assert with_kit.index("[스튜디오 제작 계약]") < with_kit.index("[사용자 요청]\n") and with_kit.endswith("진행을 못 하겠어\n")
    without = worker.compose_prompt(_ctx(repo), "modify", "x")
    assert "[스튜디오 제작 계약]" not in without  # 기존 호출자(키트 미전달)는 그대로
    assert "[작업 지침]" in with_kit and "locked_decisions" in with_kit  # 기존 규칙은 보존


def test_installed_copy_runs_exactly_as_the_prompt_instructs(repo: Path):
    install_kit(repo)
    out = subprocess.run(["node", ENTRY, "help"], cwd=repo, capture_output=True, text=True, check=False)
    assert out.returncode == 0 and "gate" in out.stdout
    gate = subprocess.run(["node", ENTRY, "gate", "--json"], cwd=repo, capture_output=True, text=True, check=False)
    assert gate.returncode == 0 and '"verdict": "NEEDS_HUMAN"' in gate.stdout  # 원장 없이는 COMPLETE 가 나오지 않는다


@pytest.mark.skipif(shutil.which("node") is None, reason="node 가 없다")
def test_node_studio_tools_pass():
    back = Path(__file__).resolve().parent.parent
    p = subprocess.run(["node", "--test", "tests/studio_kit/"], cwd=back, capture_output=True, text=True, check=False, timeout=300)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-1000:]
    assert "ℹ fail 0" in p.stdout or "# fail 0" in p.stdout
