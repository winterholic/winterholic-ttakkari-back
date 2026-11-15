"""증거 패킷: LLM 에게 줄 저장소 발췌. 총 글자 상한을 지키고 인용 가능 경로를 확정한다."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

CUT_MARK = "\n…[중간 생략]…\n"
SLICES = {"quick": {"readme": 6000, "sample": 5000, "harness": 3000},
          "deep": {"readme": 12000, "sample": 10000, "harness": 6000}}


@dataclass
class Packet:
    text: str
    valid_paths: set[str] = field(default_factory=set)
    evidence_chars: int = 0


def _slice(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    return text[:head] + CUT_MARK + text[len(text) - (limit - head):]


def _tree_summary(tree: list[dict], truncated: bool) -> str:
    blobs = [t["path"] for t in tree if t.get("type") == "blob"]
    tops = Counter(p.split("/", 1)[0] + ("/" if "/" in p else "") for p in blobs)
    lines = [f"파일 {len(blobs)}개" + ("(트리 잘림: 일부만 봄)" if truncated else "")]
    lines += [f"- {d}: {n}개" for d, n in tops.most_common(40)]
    return "\n".join(lines)


def build_packet(*, owner: str, repo: str, head_sha: str, repo_type: str, facts: dict[str, Any],
                 tree_truncated: bool, mode: str, max_chars: int) -> Packet:
    """우선순위(머리·트리·README·표본·하네스)대로 상한 안에 채운다. deep 은 상한 2배다."""
    budget = max_chars * (2 if mode == "deep" else 1)
    slices = SLICES[mode]
    valid: set[str] = set()
    chunks: list[str] = []
    used = 0

    def push(text: str) -> bool:
        nonlocal used
        if used + len(text) > budget:
            return False
        chunks.append(text)
        used += len(text)
        return True

    push(f"# {owner}/{repo} @ {head_sha[:12]} · 유형 {repo_type} · 모드 {mode}\n")
    push(f"## evidence:tree\n{_tree_summary(facts.get('tree', []), tree_truncated)}\n")
    readme = facts.get("readme_text", "")
    if readme:
        room = max(500, min(slices["readme"], budget - used - 500))
        push(f"## evidence:readme · README.md\n{_slice(readme, room)}\n")
        valid.add("README.md")
    for path, text in (facts.get("samples") or {}).items():
        body = f"## {path}\n{_slice(text, slices['sample'])}\n"
        if len(body) < 600 or not push(body):
            continue
        valid.add(path)
    for path, text in (facts.get("harness_texts") or {}).items():
        body = f"## {path}\n{_slice(text, slices['harness'])}\n"
        if len(body) < 400 or not push(body):
            continue
        valid.add(path)
    text = "".join(chunks)[:budget]
    return Packet(text=text, valid_paths=valid, evidence_chars=len(text))
