"""작업 카드용 압축 코드 맵. 맡은 범위 파일을 더듬지 않고 읽게 하는 것이 목적이다.

대상 파일의 함수·클래스 시그니처와, 이 파일들 사이 및 직접 호출자·피호출자(1단계)만 담는다.
Python은 표준 ast로 읽는다. 다른 언어는 쓸 파서가 의존성에 없어 건너뛴다(빈 맵 허용, 오류 금지).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ttakkari.runs.task_card import TaskCard

CACHE_FILE = "code_map.json"
MAX_FILES = 300  # 호출자 탐색 범위. 정렬된 앞부분만 읽어 결정론적이다.
MAX_FILE_BYTES = 300_000  # 초대형 생성 파일이 인덱스를 잠식하지 않게 버린다.
MAX_SIG_CHARS = 120
MAX_EDGE_NAMES = 8  # 한 심볼의 호출·불림 표시 상한. 초과분은 개수로만 적는다.
_TOKEN = re.compile(r"[^\s()（）\[\]'\"`,;]+")


def targets_from_card(card: TaskCard, bases: list[Path]) -> list[Path]:
    """read_first와 카드 본문에 적힌 경로 중 실제로 있는 파일. read_first 순서를 앞에 둔다."""
    texts = [*card.read_first, card.goal or "", card.location or "", *card.limits,
             *card.done_criteria, *card.verification, card.context or ""]
    out: list[Path] = []
    seen: set[str] = set()
    for text in texts:
        for token in _TOKEN.findall(text.strip().lstrip("-* ").strip()):
            raw = token.rstrip(":")
            path = Path(raw).expanduser()
            cands = [path] if path.is_absolute() else [b / path for b in bases]
            hit = next((c for c in cands if c.is_file()), None)
            if hit is None:
                continue
            try:
                key = str(hit.resolve())
            except OSError:
                key = str(hit)
            if key not in seen:
                seen.add(key)
                out.append(hit)
    return out


def parse(text: str) -> dict[str, list]:
    """한 Python 파일의 시그니처와 호출. 파싱 실패는 빈 결과로 돌린다(호출자가 건너뛴다)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {"symbols": [], "calls": []}
    symbols: list[dict[str, Any]] = []
    calls: list[tuple[str | None, str, int]] = []

    def args_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        names = [a.arg for a in node.args.args]
        if node.args.vararg:
            names.append("*" + node.args.vararg.arg)
        names += [a.arg for a in node.args.kwonlyargs]
        if node.args.kwarg:
            names.append("**" + node.args.kwarg.arg)
        return ", ".join(names)

    def callee_of(node: ast.Call) -> str | None:
        cur = node.func
        parts: list[str] = []
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        return None

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                qual = f"{prefix}{node.name}"
                symbols.append({"qual": qual, "kind": "class", "sig": f"class {node.name}",
                                "line": node.lineno, "end": node.end_lineno or node.lineno})
                visit(node.body, qual + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                symbols.append({"qual": qual, "kind": "def",
                                "sig": f"def {qual}({args_of(node)})"[:MAX_SIG_CHARS],
                                "line": node.lineno, "end": node.end_lineno or node.lineno})
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and (name := callee_of(child)):
                        calls.append((qual, name, child.lineno))
                visit([n for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], qual + ".")
    visit(tree.body, "")
    return {"symbols": symbols, "calls": calls}


def _read_cache(cache_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((cache_dir / CACHE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(paths: list[Path], cache_dir: Path) -> dict[str, dict[str, list]]:
    """파일별 인덱스. 내용 해시가 캐시와 같으면 파싱을 건너뛴다."""
    cache = _read_cache(cache_dir)
    out: dict[str, dict[str, list]] = {}
    dirty = False
    for path in paths:
        key = str(path)
        try:
            if path.suffix != ".py" or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        sha = hashlib.sha256(text.encode()).hexdigest()[:16]
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("sha") == sha:
            out[key] = {"symbols": hit.get("symbols", []), "calls": hit.get("calls", [])}
            continue
        entry = parse(text)
        out[key] = entry
        cache[key] = {"sha": sha, **entry}
        dirty = True
    if dirty:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / CACHE_FILE).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 캐시 실패는 맵 없이가 아니라 캐시 없이로만 이어진다.
    return out


def _scan_roots(roots: list[Path]) -> list[Path]:
    """호출자를 찾을 후보. 숨김 경로를 빼고 정렬해 결정론적이다."""
    found: set[Path] = set()
    for root in roots:
        try:
            for path in root.rglob("*.py"):
                if any(part.startswith(".") for part in path.relative_to(root).parts):
                    continue
                found.add(path)
        except OSError:
            continue
    return sorted(found)[:MAX_FILES]


def scan(roots: list[Path]) -> list[Path]:
    """호출자 탐색용 작업 공간 후보. build와 같은 상한·순서를 쓴다."""
    return _scan_roots(roots)


def _short(callee: str) -> str:
    return callee.rsplit(".", 1)[-1]


def callers(entries: dict[str, dict[str, list]]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(파일, 심볼)별 직접 호출자 목록. 짧은 이름으로 대조해 별칭 호출도 놓치지 않는다."""
    by_short: dict[str, list[tuple[str, str]]] = {}
    for file, entry in entries.items():
        for sym in entry["symbols"]:
            by_short.setdefault(_short(sym["qual"]), []).append((file, sym["qual"]))
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for file, entry in entries.items():
        for caller, callee, _lineno in entry["calls"]:
            for hit in by_short.get(_short(callee), []):
                out.setdefault(hit, []).append((file, caller or ""))
    return out


def build(targets: list[Path], scan_roots: list[Path], max_chars: int, cache_dir: Path) -> str:
    """대상 파일 순서대로 맵을 만든다. 상한 초과는 뒷부분부터 자르고 한 줄로 표기한다."""
    ordered = [p for p in dict.fromkeys(targets) if p.suffix == ".py" and p.is_file()]
    if not ordered:
        return ""
    index = load(list(dict.fromkeys([*ordered, *_scan_roots(scan_roots)])), cache_dir)
    who_by_target = callers(index)
    base = scan_roots[0] if scan_roots else None

    def rel(file: str) -> str:
        try:
            if base is not None:
                return str(Path(file).relative_to(base))
        except ValueError:
            pass
        return Path(file).name

    blocks: list[str] = []
    for path in ordered:
        entry = index.get(str(path), {"symbols": [], "calls": []})
        if not entry["symbols"]:
            continue
        lines = [f"{rel(str(path))}:"]
        for sym in entry["symbols"]:
            lines.append(f"- {sym['sig']} ({sym['line']}행)")
            in_file = index.get(str(path), {"calls": []})["calls"]
            callees = sorted({_short(c) for q, c, _ in in_file if q == sym["qual"]})
            if callees:
                lines.append(f"  호출: {', '.join(callees[:MAX_EDGE_NAMES])}"
                             + (f" 외 {len(callees) - MAX_EDGE_NAMES}개" if len(callees) > MAX_EDGE_NAMES else ""))
            who = sorted({(rel(f), q) for f, q in who_by_target.get((str(path), sym["qual"]), []) if q})
            if who:
                shown = ", ".join(f"{f}:{q}" if f != rel(str(path)) else q for f, q in who[:MAX_EDGE_NAMES])
                lines.append(f"  불림: {shown}"
                             + (f" 외 {len(who) - MAX_EDGE_NAMES}개" if len(who) > MAX_EDGE_NAMES else ""))
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    head = f"[코드 맵 — {len(blocks)}개 파일, 1단계 호출만]"
    text = head + "\n" + "\n".join(blocks)
    if len(text) <= max_chars:
        return text
    note = f"\n+ 나머지 생략(상한 {max_chars}자)"
    cut = text[:max_chars - len(note)]
    return cut[:cut.rfind("\n")] + note
