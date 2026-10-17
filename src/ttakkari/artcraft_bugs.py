"""BUGS.md 버그 후보 파서와 에이전트 요청 문구. 파일·DB·네트워크를 만지지 않는 순수 함수만 둔다.

에이전트는 ~/development/projects/artcraft/BUGS.md 에 아래 고정 형식으로 기록만 한다(AGENTS.md "Bug candidate recording rules").
상태(미검토 → 기여 선택 → 진행 중 → 보고됨 → 해결, 보류·제외)는 파일이 아니라 따까리가 따로 저장한다.

    ## BUG-008: 제목
    - **Repos:** storytold/<app>[, ...]
    - **Apps:** <app>[, ...] | all
    - **Severity:** S1|S2|S3
    - **Kind:** issue|pr|security
    - **Difficulty:** easy|medium|hard
    - **Upstream:** unchecked|none|related|duplicate[: 메모]
    - **Found:** YYYY-MM-DD
    - **Verified:** yes|no|partly[, 방법]
    ### Summary / ### Repro / ### Evidence   (필수)
    ### Impact / ### Suggestion / ### Unverified   (선택)
"""
from __future__ import annotations

import re
from typing import Any

STATUSES = ("candidate", "selected", "in_progress", "reported", "resolved", "deferred", "excluded")
SEVERITIES = ("S1", "S2", "S3")
KINDS = ("issue", "pr", "security")
DIFFICULTIES = ("easy", "medium", "hard")
UPSTREAM_STATES = ("unchecked", "none", "related", "duplicate")
SECTION_NAMES = ("summary", "impact", "repro", "evidence", "suggestion", "unverified")
REQUIRED_SECTIONS = ("summary", "repro", "evidence")
META_KEYS = ("repos", "apps", "severity", "kind", "difficulty", "upstream", "found", "verified")
SECTION_LIMIT = 8000

KIND_LABEL = {"issue": "이슈만", "pr": "PR 가능", "security": "보안 비공개 신고 필요"}
DIFFICULTY_LABEL = {"easy": "쉬움", "medium": "보통", "hard": "어려움"}
ARTCRAFT_BUGS_PATH = "~/development/projects/artcraft/BUGS.md"

_SECTION = re.compile(r"^## (BUG-\d{3}): (.+?)\n(.*?)(?=^## |^---$|\Z)", re.MULTILINE | re.DOTALL)
_META_LINE = re.compile(r"^- \*\*([A-Za-z]+):\*\* (.+)$", re.MULTILINE)
_REPO = re.compile(r"^storytold/[a-z0-9-]+$")
_NAME = re.compile(r"^[a-z0-9-]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _upstream(value: str) -> tuple[str, str]:
    head, _, rest = value.strip().partition(" ")
    state = head.rstrip(":;,—-").lower()
    return state, rest.strip().lstrip(":—- ").strip() if state in UPSTREAM_STATES else value.strip()


def parse_bugs(text: str, app_names: set[str]) -> list[dict[str, Any]]:
    """`## BUG-nnn` 절을 읽는다. 형식이 어긋난 항목도 버리지 않고 problems 에 사유를 담아 보여 준다."""
    bugs: dict[str, dict[str, Any]] = {}
    for m in _SECTION.finditer(text):
        bug_id, title, body = m.group(1), m.group(2).strip(), m.group(3)
        head, *chunks = re.split(r"^### ", body, flags=re.MULTILINE)
        meta = {k.lower(): v.strip() for k, v in _META_LINE.findall(head)}
        problems: list[str] = []
        if bug_id in bugs:
            continue  # 같은 ID 가 둘이면 앞의 것만. scripts/check_bugs.py 가 중복을 잡는다
        for key in META_KEYS:
            if not meta.get(key):
                problems.append(f"메타데이터 '{key.capitalize()}' 가 없습니다.")
        for key in set(meta) - set(META_KEYS):
            problems.append(f"알 수 없는 메타데이터 '{key}' 입니다.")
        severity = meta.get("severity", "")
        kind = meta.get("kind", "")
        difficulty = meta.get("difficulty", "")
        if meta.get("severity") and severity not in SEVERITIES:
            problems.append("Severity 는 S1, S2, S3 중 하나여야 합니다.")
        if meta.get("kind") and kind not in KINDS:
            problems.append("Kind 는 issue, pr, security 중 하나여야 합니다.")
        if meta.get("difficulty") and difficulty not in DIFFICULTIES:
            problems.append("Difficulty 는 easy, medium, hard 중 하나여야 합니다.")
        state, note = _upstream(meta.get("upstream", "unchecked"))
        if meta.get("upstream") and state not in UPSTREAM_STATES:
            problems.append("Upstream 은 unchecked, none, related, duplicate 로 시작해야 합니다.")
            state = "unchecked"
        found = meta.get("found", "")
        if found and not _DATE.match(found):
            problems.append("Found 는 YYYY-MM-DD 형식이어야 합니다.")
        repos = _csv(meta.get("repos", ""))
        for repo in repos:
            if not _REPO.match(repo):
                problems.append(f"저장소 '{repo}' 는 storytold/<이름> 형식이어야 합니다.")
        apps = sorted(app_names) if _csv(meta.get("apps", "")) == ["all"] else _csv(meta.get("apps", ""))
        for app in apps:
            if not _NAME.match(app):
                problems.append(f"앱 이름 '{app}' 형식이 올바르지 않습니다.")
        sections: dict[str, str] = {}
        truncated: list[str] = []
        for chunk in chunks:
            name, _, content = chunk.partition("\n")
            key = name.strip().lower()
            if key not in SECTION_NAMES:
                problems.append(f"알 수 없는 소절 '### {name.strip()}' 입니다.")
                continue
            content = content.strip()
            if len(content) > SECTION_LIMIT:
                truncated.append(key)
            sections[key] = content[:SECTION_LIMIT]
        for key in REQUIRED_SECTIONS:
            if not sections.get(key):
                problems.append(f"필수 소절 '### {key.capitalize()}' 가 없거나 비었습니다.")
        bugs[bug_id] = {
            "id": bug_id, "title": title, "repos": repos, "apps": apps,
            "severity": severity if severity in SEVERITIES else None,
            "kind": kind if kind in KINDS else None,
            "difficulty": difficulty if difficulty in DIFFICULTIES else None,
            "upstream": {"state": state if state in UPSTREAM_STATES else "unchecked", "note": note},
            "found": found if _DATE.match(found) else None, "verified": meta.get("verified", ""),
            "sections": sections, "truncated": truncated, "problems": problems,
        }
    return list(bugs.values())


def _first_lines(text: str, limit: int) -> str:
    text = re.sub(r"```\w*", "", text).strip()  # 재현은 코드 블록이 대부분이라 울타리 표시만 걷는다
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def handoff(bug: dict[str, Any]) -> str:
    """사용자가 '기여 선택' 뒤 에이전트에게 붙여 넣을 요청 문구. 외부에 올리는 일은 사용자 확인 뒤에만 하도록 못박는다."""
    kind = bug.get("kind")
    repos = ", ".join(bug["repos"]) or "(저장소 미기재)"
    lines = [
        f"ArtCraft 버그 후보 {bug['id']} 를 업스트림에 기여하려고 해. 내가 이 후보를 골랐어.",
        f"- 제목: {bug['title']}",
        f"- 앱: {', '.join(bug['apps']) or '(미기재)'}",
        f"- 저장소: {repos}",
        f"- 심각도 {bug['severity'] or '?'} · 종류 {KIND_LABEL.get(kind, '?')} · 난이도 {DIFFICULTY_LABEL.get(bug['difficulty'], '?')}(기록한 에이전트의 추정)",
        f"- 업스트림 중복 확인: {bug['upstream']['state']}" + (f" ({bug['upstream']['note']})" if bug["upstream"]["note"] else ""),
        f"- 요약: {_first_lines(bug['sections'].get('summary', ''), 400) or bug['title']}",
        f"- 재현 요약: {_first_lines(bug['sections'].get('repro', ''), 500) or '(BUGS.md 참고)'}",
        f"- 전체 기록(재현·증거·미확인): {ARTCRAFT_BUGS_PATH} 의 {bug['id']} 절",
        "",
        "해 줄 일:",
        "1. 업스트림 최신 main 에서 재현을 다시 확인하고, 열린·닫힌 이슈에서 중복을 검색해.",
    ]
    if kind == "security":
        lines += [
            "2. 보안 항목이야. 공개 이슈·PR·커밋 메시지에 내용을 쓰지 마. 저장소의 SECURITY.md 가 안내하는 비공개 채널로 보낼 신고 초안만 로컬 파일로 써서 보여 줘.",
        ]
    elif kind == "pr":
        lines += [
            "2. 저장소를 포크하지 말고 먼저 로컬(artcraft/apps/<앱>)에서 수정과 테스트를 준비해. 이슈 초안과 PR 설명 초안도 같이 써서 보여 줘.",
        ]
    else:
        lines += ["2. 이슈 초안(제목·재현·기대 동작·환경)을 로컬 파일로 써서 보여 줘."]
    lines += [
        "3. GitHub 에 올리는 일(이슈 생성, 댓글, PR, push)은 내가 초안을 확인하고 올리라고 말한 뒤에만 해. 그 전에는 아무것도 외부로 보내지 마.",
        "4. 끝나면 ArtCraft 관제 페이지에서 내가 상태를 바꿀 수 있게 이슈·PR 링크를 알려 줘.",
    ]
    return "\n".join(lines)
