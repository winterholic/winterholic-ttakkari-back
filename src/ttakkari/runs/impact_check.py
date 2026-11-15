"""Run 완료 시 변경 함수의 호출자를 대조한다. 거절하지 않고 경고만 남긴다.

워커 workspace의 git diff로 변경된 함수를 찾고 코드 맵 인덱스로 그 호출자를 구한다.
호출자 중 변경 파일 밖에 있고 같은 diff에서 시험이 갱신되지 않은 것만
"영향 범위 미확인" 목록으로 만든다(상한 20개).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ttakkari.runs import code_map, ctx_ab

MAX_ITEMS = 20
MAX_DIFF_BYTES = 500_000  # 큰 diff는 앞에서만 읽는다. git 출력 순서가 결정론적이라 항상 같은 부분이다.
_HUNK = re.compile(r"\+(\d+)(?:,(\d+))?")
_REMOVED_DEF = re.compile(r"^-\s*(?:async\s+def\s+(\w+)|def\s+(\w+)|class\s+(\w+))")
_TEST_SUFFIXES = ("_test", "_tests")


def parse_hunks(patch: str) -> dict[str, dict[str, Any]]:
    """파일별 새 쪽 변경 범위. 지운 파일은 지운 정의 이름만 모은다."""
    hunks: dict[str, dict[str, Any]] = {}
    cur: str | None = None
    src: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                cur = src
                if cur is not None:
                    hunks.setdefault(cur, {"ranges": [], "deleted": True, "removed_defs": []})
            else:
                cur = path.removeprefix("b/")
                hunks.setdefault(cur, {"ranges": [], "deleted": False, "removed_defs": []})
        elif line.startswith("--- "):
            path = line[4:].strip()
            src = None if path == "/dev/null" else path.removeprefix("a/")
        elif line.startswith("@@ ") and cur is not None:
            if (m := _HUNK.search(line)) and not hunks[cur]["deleted"]:
                hunks[cur]["ranges"].append((int(m.group(1)), int(m.group(2) or 1)))
        elif (cur is not None and hunks.get(cur, {}).get("deleted") and cur.endswith(".py")
                and line.startswith("-") and not line.startswith("---")):
            if m := _REMOVED_DEF.match(line):
                hunks[cur]["removed_defs"].append(next(g for g in m.groups() if g))
    return hunks


def changed_functions(hunks: dict[str, dict[str, Any]],
                      entries: dict[str, dict[str, list]]) -> set[tuple[str, str]]:
    """변경 범위에 걸리는 (파일, 심볼). 지운 파일은 지운 정의 이름으로 남긴다."""
    out: set[tuple[str, str]] = set()
    for file, hunk in hunks.items():
        if hunk["deleted"]:
            out.update((file, name) for name in hunk["removed_defs"])
            continue
        for sym in entries.get(file, {}).get("symbols", []):
            for start, count in hunk["ranges"]:
                end = start + max(count, 1) - 1
                if sym["line"] <= end and sym["end"] >= start:
                    out.add((file, sym["qual"]))
                    break
    return out


def _test_stem(name: str) -> str:
    stem = Path(name).stem.removeprefix("test_")
    for suffix in _TEST_SUFFIXES:
        stem = stem.removesuffix(suffix)
    return stem


def _looks_like_test(path: str) -> bool:
    parts = Path(path).parts
    if any(p.lower() in ("test", "tests") for p in parts[:-1]):
        return True
    stem = Path(path).stem
    return stem.startswith("test_") or stem.endswith(_TEST_SUFFIXES)


def test_updated(caller: str, changed: set[str]) -> bool:
    """호출 파일의 관례적 시험 파일이 같은 diff에 있는지. 이름이 다르면 갱신으로 보지 않는다."""
    stem = Path(caller).stem
    return any(_looks_like_test(p) and _test_stem(p) == stem for p in changed)


def check_hunks(hunks: dict[str, dict[str, Any]], entries: dict[str, dict[str, list]]) -> list[str]:
    """영향 범위 미확인 경고 목록. 호출자 파일이 변경 파일 밖일 때만 남긴다."""
    changed = changed_functions(hunks, entries)
    if not changed:
        return []
    by_caller = code_map.callers(entries)
    shorts: dict[str, list[tuple[str, str]]] = {}
    raw_calls: dict[str, list[tuple[str, str]]] = {}
    for file, entry in entries.items():
        for sym in entry["symbols"]:
            shorts.setdefault(sym["qual"].rsplit(".", 1)[-1], []).append((file, sym["qual"]))
        for caller, callee, _lineno in entry["calls"]:
            raw_calls.setdefault(callee.rsplit(".", 1)[-1], []).append((file, caller or ""))
    changed_files = set(hunks)
    out: list[str] = []
    for file, qual in sorted(changed):
        hits = by_caller.get((file, qual), [])
        if not hits:  # 지운 정의는 인덱스에 없어 원시 호출을 짧은 이름으로 대조한다.
            for alt in shorts.get(qual.rsplit(".", 1)[-1], []):
                if alt[0] != file:
                    hits += by_caller.get(alt, [])
            hits += raw_calls.get(qual.rsplit(".", 1)[-1], [])
        for caller_file, caller_qual in sorted(set(hits)):
            if not caller_qual or caller_file in changed_files:
                continue
            if test_updated(caller_file, changed_files):
                continue
            out.append(f"영향 범위 미확인: {caller_file}:{caller_qual} → {file}:{qual} (관련 시험 갱신 없음)")
            if len(out) >= MAX_ITEMS:
                return out
    return out


def check(patch: str, entries: dict[str, dict[str, list]]) -> list[str]:
    """diff 텍스트 한 건을 점검한다."""
    return check_hunks(parse_hunks(patch), entries)


def evaluate(root: Path, patch: str, new_files: list[str], cache_dir: Path) -> list[str]:
    """작업 공간 기준으로 diff 전체를 점검한다. 추적 전 새 파일은 통째로 변경으로 본다."""
    cut = patch[:MAX_DIFF_BYTES]
    patch = cut[: cut.rfind("\n")] if len(patch) > MAX_DIFF_BYTES else patch
    hunks = parse_hunks(patch)
    for rel in new_files:
        if rel.endswith(".py"):
            hunks.setdefault(rel, {"ranges": [(1, 10**9)], "deleted": False, "removed_defs": []})
    if not hunks:
        return []
    paths = [root / f for f in hunks if f.endswith(".py")]
    entries = code_map.load(paths + code_map.scan([root]), cache_dir)
    rel_entries = {str(Path(k).relative_to(root)) if k.startswith(str(root) + "/") else Path(k).name: v
                   for k, v in entries.items()}
    return check_hunks(hunks, rel_entries)


async def _git_diff(root: Path, ref: str) -> str | None:
    """매니저의 git 호출과 같은 모양. 순환 참조를 피하려고 여기서 직접 실행한다."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "-U0", ref, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace")


async def record(*, session_id: str, settings: object, is_git: bool, checkpoint_ref: str | None,
                 root: Path, new_files: list[str]) -> dict[str, Any]:
    """usage에 합칠 기록. control 이하면 배정만 남기고 계산하지 않는다."""
    arm = ctx_ab.arm("impact_check", session_id, ctx_ab.percent_of(settings, "worker_impact_check_ab_percent"))
    if arm == "off":
        return {}
    out: dict[str, Any] = {"impact_check_arm": arm}
    if arm != "on" or not is_git or not checkpoint_ref:
        return out
    patch = await _git_diff(root, checkpoint_ref)
    if patch is None:
        return out
    warnings = await asyncio.to_thread(evaluate, root, patch, new_files, settings.home / "cache")
    out["impact_check_unverified"] = len(warnings)
    out["impact_check_warnings"] = warnings
    return out
