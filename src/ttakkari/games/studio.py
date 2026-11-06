"""게임 worker 에게 스튜디오 제작 키트(도구·스킬)를 전달한다 (docs/design/game-studio.md §6.1 확장).

정본은 `studio_kit/ttakkari-game-studio/`(SKILL.md + node 도구, 의존성 없음)이다. worker 는 게임 저장소 workspace 밖을 읽지 않으므로
요청이 시작될 때 키트를 저장소 안 `.agents/skills/`·`.claude/skills/` 로 복사하고(엔진마다 스킬 발견 위치가 다르다),
prompt 에 열어야 할 SKILL.md 와 실행 명령을 직접 적는다. 스킬 자동 발견은 보조 경로이고 prompt 가 주 경로다.

저장소 파일을 새로 커밋 대상으로 만들지 않도록 `.git/info/exclude`(로컬 전용)에만 제외 규칙을 쓴다.
스탬프 없는 같은 이름의 기존 폴더는 사용자·다른 도구의 것으로 보고 덮어쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

SKILL_NAME = "ttakkari-game-studio"
KIT_SRC = Path(__file__).with_name("studio_kit") / SKILL_NAME
# 엔진별 workspace 스킬 발견 위치: Claude Code(.claude/skills), Codex·Antigravity(.agents/skills)
TARGETS = (Path(".agents/skills") / SKILL_NAME, Path(".claude/skills") / SKILL_NAME)
STAMP = ".ttakkari-kit-stamp"
ENTRY = f".agents/skills/{SKILL_NAME}/bin/studio.mjs"
EXCLUDE_LINES = (f"/.agents/skills/{SKILL_NAME}/", f"/.claude/skills/{SKILL_NAME}/", "/.studio-tmp/")


@dataclass
class KitInstall:
    ok: bool
    status: str  # installed | current | updated | conflict | error
    detail: str = ""
    kit_hash: str = ""

    @property
    def entry(self) -> str:
        return ENTRY


def kit_hash() -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in KIT_SRC.rglob("*") if p.is_file()):
        h.update(f.relative_to(KIT_SRC).as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _refuse_symlink(root: Path, rel: Path) -> None:
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"{cur.relative_to(root)} 가 심볼릭 링크다")


def _exclude(root: Path) -> None:
    git = root / ".git"
    if not git.is_dir() or git.is_symlink():
        return
    _refuse_symlink(root, Path(".git/info/exclude"))
    info = git / "info"
    info.mkdir(exist_ok=True)
    f = info / "exclude"
    have = f.read_text().splitlines() if f.is_file() else []
    add = [line for line in EXCLUDE_LINES if line not in have]
    if add:
        with f.open("a") as fh:
            fh.write(("\n" if have and have[-1] != "" else "") + "\n".join(add) + "\n")


def install_kit(root: Path) -> KitInstall:
    """게임 저장소 root 에 키트를 설치한다. 멱등. 실패해도 예외 대신 결과를 돌려 요청을 막지 않고 prompt 에 사유를 싣는다."""
    try:
        if not (KIT_SRC / "SKILL.md").is_file():
            return KitInstall(False, "error", "키트 원본이 배포본에 없다")
        digest = kit_hash()
        # 모든 설치 경계를 쓰기 전에 검사한다. 두 번째 대상 충돌로 첫 대상을 갱신하지 않는다.
        _refuse_symlink(root, Path(".git/info/exclude"))
        for rel in TARGETS:
            _refuse_symlink(root, rel / STAMP)
            dest = root / rel
            if dest.exists() and not (dest / STAMP).is_file():
                return KitInstall(False, "conflict", f"{rel} 가 이미 있고 따까리 키트가 아니다(덮어쓰지 않음)", digest)
        statuses: list[str] = []
        for rel in TARGETS:
            _refuse_symlink(root, rel)
            dest = root / rel
            stamp = dest / STAMP
            if dest.exists():
                if not stamp.is_file():
                    return KitInstall(False, "conflict", f"{rel} 가 이미 있고 따까리 키트가 아니다(덮어쓰지 않음)", digest)
                if stamp.read_text().strip() == digest:
                    statuses.append("current")
                    continue
                shutil.rmtree(dest)
                statuses.append("updated")
            else:
                statuses.append("installed")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(KIT_SRC, dest)
            stamp.write_text(digest + "\n")
        _exclude(root)
        status = "installed" if "installed" in statuses else "updated" if "updated" in statuses else "current"
        return KitInstall(True, status, "", digest)
    except (OSError, ValueError) as e:
        return KitInstall(False, "error", str(e)[:200])


def install_shared_skill(skills_root: Path) -> KitInstall:
    """로컬 공유 스킬 루트(예: ~/.claude/skills)에 같은 키트를 설치한다. 저장소 설치와 같은 스탬프 규칙(멱등·충돌 시 중단)을 쓴다.

    Claude Code 로컬 세션과 Codex(~/.agents/skills 심볼릭)가 이 폴더를 읽는다. 호출자가 skill-list.md 등록을 따로 맞춘다.
    """
    try:
        digest = kit_hash()
        dest = skills_root / SKILL_NAME
        if skills_root.is_symlink():
            return KitInstall(False, "error", f"{skills_root} 가 심볼릭 링크다", digest)
        if dest.is_symlink():
            return KitInstall(False, "conflict", f"{dest} 가 심볼릭 링크다(덮어쓰지 않음)", digest)
        if (dest / STAMP).is_symlink():
            return KitInstall(False, "error", f"{dest / STAMP} 가 심볼릭 링크다", digest)
        status = "installed"
        if dest.exists():
            if not (dest / STAMP).is_file():
                return KitInstall(False, "conflict", f"{dest} 가 이미 있고 따까리 키트가 아니다(덮어쓰지 않음)", digest)
            if (dest / STAMP).read_text().strip() == digest:
                return KitInstall(True, "current", "", digest)
            shutil.rmtree(dest)
            status = "updated"
        skills_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(KIT_SRC, dest)
        (dest / STAMP).write_text(digest + "\n")
        return KitInstall(True, status, str(dest), digest)
    except OSError as e:
        return KitInstall(False, "error", str(e)[:200])


def main(argv: list[str] | None = None) -> int:
    """`python -m ttakkari.games.studio install-skill --dest ~/.claude/skills` (로컬 공유 설치) 또는 `install-repo <게임 저장소>`."""
    import argparse

    p = argparse.ArgumentParser(prog="ttakkari.games.studio")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("install-skill")
    s.add_argument("--dest", required=True, type=Path)
    r = sub.add_parser("install-repo")
    r.add_argument("root", type=Path)
    args = p.parse_args(argv)
    res = install_shared_skill(args.dest.expanduser()) if args.cmd == "install-skill" else install_kit(args.root.expanduser())
    print(f"{res.status}: {res.detail or 'ok'} (kit {res.kit_hash[:12]})")
    return 0 if res.ok else 1


def studio_section(kit: KitInstall | None, kind: str) -> str:
    """worker prompt 의 [스튜디오 제작 계약] 절. kit 이 None 이면(설치 시도 안 함) 빈 문자열."""
    if kit is None:
        return ""
    if not kit.ok:
        return (
            "[스튜디오 제작 계약]\n"
            f"스튜디오 키트를 이 저장소에 설치하지 못했다({kit.status}: {kit.detail}). 도구 없이 진행하되 "
            "'테스트·빌드 통과'를 완료·해결이라고 보고하지 않는다. 시각·재미·서사·실제 플레이 가능 여부는 확인하지 못한 것으로 따로 적는다."
        )
    studio = f"node {ENTRY}"
    head = f"[스튜디오 제작 계약]\n스튜디오 키트(스킬 {SKILL_NAME})가 설치돼 있다. 먼저 `.agents/skills/{SKILL_NAME}/SKILL.md` 를 열어 읽고 따른다. 명령: `{studio} help`."
    if kind == "verify":
        body = [
            f"독립 검증이므로 `{studio} gate` 와 `{studio} ledger check` 를 직접 실행하고, 원장의 증거 파일(docs/studio/evidence/)을 열어 실제 내용을 대조한다.",
            "원장이 비었거나 증거가 없는 항목은 앞선 작업자의 '해결' 주장을 믿지 말고 미검증으로 보고한다. 소스와 원장은 고치지 않는다.",
            "보고에는 gate 판정과 [막는 문제]·[검증되지 않은 것]을 그대로 옮긴다.",
        ]
    else:
        body = [
            f"요청에 사용자 피드백이 있으면 원문 그대로 `{studio} ledger add --quote \"<원문>\"` 로 원장에 넣고 범주를 확인한다. 정전·금지 사항이 있으면 docs/studio/canon.json 에 옮긴다(`init`).",
            "버그는 고치기 전에 재현(`play --phase before`)하고, 고친 뒤 같은 재현과 `reach --spec <명세> --attach ID`·스크린샷으로 증거를 붙인다. reach 단독 통과는 실플레이가 아니다. 기계 테스트는 로그 수기 등록 대신 `ledger test ID --program ... --args-json ...` 로 직접 실행한다. 증거 종류는 SKILL.md 와 references/production-contract.md 를 따른다.",
            f"끝내기 전 `{studio} gate` 를 실행한다. 판정이 COMPLETE 가 아니면 '완료·해결'이라고 쓰지 않고, 최종 답변에 판정과 [막는 문제]·[검증되지 않은 것]을 그대로 옮긴다.",
            "'테스트·빌드 통과'는 완료 증거가 아니다. 브라우저·재생이 불가능하면(play 가 unsupported) not-run 으로 기록하고 사용자 플레이 확인 대기로 보고한다. 로컬 CLI는 사용자 발화 출처를 인증하지 못하므로 quote/source 문자열은 gate에서 미검증이다. 사용자 확인을 지어내지 않는다.",
            "키트 폴더(.agents/.claude 아래 ttakkari-game-studio)는 플랫폼이 관리하므로 수정하지 않는다. docs/studio/ 의 원장·정전·증거는 이 저장소의 산출물이니 커밋한다.",
        ]
    return head + "\n" + "\n".join(f"- {b}" for b in body)


if __name__ == "__main__":
    raise SystemExit(main())
