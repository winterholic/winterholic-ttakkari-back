"""목표 계약이 쓰는 결정론적 판정 도구. LLM 을 부르지 않는다.

work.py 의 영속 목표 계약을 보조한다: 복합 요청 판정, 원본 요청 항목 추출·대조, 실패 종류 분류, Run 이벤트에서
뽑은 실행 증거(진전 판정 재료). DB 상태를 바꾸지 않는다(증거 수집만 읽기).

용어를 코드로 가른다(A3).
- Run 종료: Run.status 가 terminal 이 된 것. 엔진 턴이 끝났다는 뜻일 뿐 일이 끝났다는 뜻이 아니다.
- Task 완료: 작업자의 `상태: 완료` 선언(runs/completion.py). 보고일 뿐 증거가 아니다.
- Goal complete: 계약 status=complete. 요구 전부 done+증거, 작업자 실행 없음, PMS 미완료 없음, 원본 요청 항목
  대조를 모두 통과해야 하며 문구나 정상 종료로는 만들어지지 않는다.
- Blocked: 더 돌려도 풀리지 않는 이유가 기록됨(blocker + blocker_kind). Paused: 사용자 중단 요청.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import Run, RunEvent

DESK_HEADER = "[접수에서 넘긴 사용자 요청]"
_HISTORY_MARK = "[최근 대화"

# ---- 복합 요청 판정 ---------------------------------------------------------------------------------------
MIN_COMPLEX_CHARS = 10  # 짧은 복합 자연어("조사하고 고쳐서 배포해")도 동사 수로 판정하므로 길이 문턱은 낮게 둔다
_ITEM_RE = re.compile(
    r"^\s*(?:(?P<num>\d{1,2})\s*[.)]|\((?P<pnum>\d{1,2})\)|(?P<circ>[①-⑳])|(?P<tag>[A-Za-z]{1,3}\d{1,2})\s*[.:)])\s*(?P<body>\S.*)$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+\S")
_SECTION_RE = re.compile(r"^\s*(?:#{1,4}\s*(?P<h>.{1,30})|[\[【(](?P<b>[^\]】)]{1,30})[\]】)])\s*:?\s*$")
_ACTION_RE = re.compile(
    r"수정|구현|추가|제거|삭제|작성|테스트|검증|푸시|push|배포|deploy|커밋|commit|병합|머지|merge|리팩|refactor|조사|분석|"
    r"정리|고쳐|고치|만들|적용|마이그레이션|migrat|implement|fix\b|build|설치|연결|통합|재현|보고서|문서화|"
    r"개선|개편|변경|교체|최적화|보완|정비", re.IGNORECASE)
# 파일·배포·코드를 실제로 바꾸는 동사. 분석·정리·설명만 있는 요청은 계약 대상이 아니다.
_MUTATING_RE = re.compile(
    r"수정|구현|추가|제거|삭제|작성|푸시|push|배포|deploy|커밋|commit|병합|머지|merge|리팩|refactor|고쳐|고치|만들|적용|"
    r"마이그레이션|migrat|implement|fix\b|build|설치|개선|개편|변경|교체|최적화|보완|정비", re.IGNORECASE)
_IMPERATIVE_RE = re.compile(
    r"해줘|해 줘|해라|하세요|해주세요|해 주세요|하자|바꿔|고쳐|만들어|추가해|수정해|적용해|진행해|이어서|계속|"
    r"배포|push|푸시|구현|실행해|돌려|\bplease\b|\bdo\b|\bfix\b|\badd\b|\bimplement", re.IGNORECASE)
_COMMAND_END_RE = re.compile(
    r"(?:해|하|써|고쳐|만들어|돌려|올려|넣어|바꿔|붙여|지워|줘|해줘)\s*(?:줘|라|세요|주세요|줄래|자|요)\b|부탁|\bplease\b|[^\w]해줘|해주세요|해 주세요",
    re.IGNORECASE)
_SCOPE_CHANGE_RE = re.compile(
    r"대신|말고|빼고|빼줘|제외|취소하고|변경|바꿔|바꾸|추가로|그리고 또|또한|범위|instead|rather than|also add|additionally|scope|drop\b",
    re.IGNORECASE)
_QUESTION_END_RE = re.compile(r"[?？]\s*$|(?:까|냐|니|죠|가요|어요|았어|었어|됐어|뭐야|뭐임|어때)\s*$")
# 실제로 바꾸라는 명령형. 명사로만 나온 동사("수정, 테스트, 구현 방식을 설명해줘")는 계약 대상이 아니다.
_CMD_RE = re.compile(
    r"(?:(?:수정|구현|추가|제거|삭제|작성|푸시|push|배포|deploy|커밋|commit|병합|머지|merge|리팩터링|refactor|적용|설치|"
    r"마이그레이션|fix|build|개선|개편|변경|교체|최적화|보완|정비|조사|분석|진행|수행|실행|처리)(?:하고|하며|해서|하여|한 뒤|한 다음|해줘|해 줘|해주세요|해 주세요|하세요|해라|하자|해)|"
    r"고치(?:고|세요|며)|고쳐|만들(?:고|어|며)|바꿔|바꾸고|돌려|올려|붙여|넣어)", re.IGNORECASE)
# 부정 지시가 든 문장("수정하지 말고", "…요청을 하지 않는다")은 시킨 일이 아니라 금지다. 문장째 센다.
_NEG_RE = re.compile(r"지\s*(?:말|마|않|못)|금지|않는다|않습니다|않음|안\s*한다|마세요|\bdo not\b|\bdon't\b|\bnever\b|\bwithout\b",
                     re.IGNORECASE)
# 설명·질문을 청하는 문장. 이 문장 속 동사는 시킨 일이 아니라 설명 대상이다("수정, 배포 방식을 설명해줘").
_ASK_END_RE = re.compile(r"(?:설명|알려|말해|가르쳐|요약|비교|보여|궁금)\S{0,8}[\s.!?]*$")
# 같은 일을 가리키는 동사는 한 번만 센다(수정·고치, 커밋·푸시·병합 은 한 묶음의 git 작업).
_CANON = {"고쳐": "수정", "고치": "수정", "커밋": "git", "commit": "git", "푸시": "git", "push": "git", "병합": "git",
          "머지": "git", "merge": "git", "deploy": "배포", "refactor": "리팩", "fix": "수정"}
_INLINE_MARK_RE = re.compile(r"(?:^|(?<=[\s:]))(\d{1,2})[.)]\s+(?=\S)")
_READONLY_DECL_RE = re.compile(r"읽기\s*전용|read[- ]only", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?。])\s+|\n")
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_LOG_LINE_RE = re.compile(
    r"^\s*(?:Traceback\b|File \".*\", line \d+|at\s+\S+[(:]|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|"
    r"\[?(?:ERROR|WARN(?:ING)?|INFO|DEBUG|FATAL)\]?[\s:]|[\w.]+(?:Error|Exception)\b.*:|\$ |>>> )", re.IGNORECASE)


def request_text(prompt: str, origin: str) -> str:
    """사용자의 말만 남긴다. 접수 머리글과 참고용 최근 대화는 요청이 아니다."""
    text = (prompt or "").strip()
    if origin == "desk" and text.startswith(DESK_HEADER):
        text = text[len(DESK_HEADER):].strip()
    cut = text.find(f"\n\n{_HISTORY_MARK}")
    if cut >= 0:
        text = text[:cut].strip()
    return text


def prose(text: str) -> str:
    """코드블록·들여쓴 코드·로그 줄을 뺀 사람의 문장. 붙여넣은 에러 로그는 요청이 아니다."""
    body = _FENCE_RE.sub("\n", text or "")
    kept = [ln for ln in body.splitlines()
            if not ln.startswith(("    ", "\t")) and not _LOG_LINE_RE.match(ln)]
    return "\n".join(kept).strip()


def drop_negated(text: str) -> str:
    """부정·금지 표현이 든 문장을 뺀다."""
    return "\n".join(sent for sent in _SENTENCE_RE.split(text or "") if sent.strip() and not _NEG_RE.search(sent))


def _line_items(line: str) -> list[tuple[str, str]]:
    """한 줄의 (라벨, 본문). `1. 원인 조사 / 2. 코드 수정 / 3. 테스트` 처럼 한 줄에 이어 쓴 번호 항목도 나눈다.

    한 줄 안의 번호는 1부터 1씩 늘 때만 항목으로 본다(날짜·버전 숫자를 항목으로 오인하지 않는다)."""
    marks = list(_INLINE_MARK_RE.finditer(line))
    if len(marks) >= 2 and [int(m.group(1)) for m in marks] == list(range(1, len(marks) + 1)):
        out = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
            out.append((m.group(1), line[m.end():end].strip(" /;,·|\t")))
        return out
    found = _ITEM_RE.match(line)
    if not found:
        return []
    label = found.group("tag") or found.group("num") or found.group("pnum") or found.group("circ")
    return [(label, found.group("body").strip())]


def extract_items(text: str) -> list[dict[str, str]]:
    """번호·라벨이 붙은 줄만 요구 항목으로 본다. 불릿은 경로·보조 설명이 많아 제외한다.

    `[백엔드] 1..3 / [프런트] 1..3` 처럼 라벨이 겹치면 구역 이름을 붙여 id 를 유일하게 만든다(요구 id 는 유일해야 한다)."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    used: set[str] = set()
    section = ""
    for line in (text or "").splitlines():
        head = _SECTION_RE.match(line)
        if head:
            section = (head.group("h") or head.group("b") or "").strip()
            continue
        for label, body in _line_items(line):
            key = f"{section}|{label}:{body[:40]}"
            if key in seen or len(body) < 3:
                continue
            seen.add(key)
            ident = label
            if ident in used:
                ident = f"{section}-{label}" if section else f"{label}-2"
                n = 2
                while ident in used:
                    ident = f"{section or label}-{label}#{n}"
                    n += 1
            ident = ident[:80]
            used.add(ident)
            items.append({"label": ident, "text": body[:300]})
    return items[:60]


def _canon(words: set[str]) -> set[str]:
    return {_CANON.get(w, w) for w in words}


def is_complex(text: str) -> bool:
    """여러 단계를 책임져야 하는 요청인가. 짧은 질문·잡담·로그 붙여넣기·설명·읽기 전용 지시에는 계약도 추가 추론도 강제하지 않는다.

    길이나 특정 어미로 판정하지 않는다. 사람의 문장(로그·코드블록·부정 지시·설명 요청 문장 제외)에서
    - 번호 항목이 3개 이상(줄마다든 한 줄에 `1. … / 2. …` 로 이어 쓰든)이고, 명령형이 있거나 항목에 작업 명사가 2개 이상이며
    - 아니면 서로 다른 작업 동사·명사형 동작이 3개 이상(그중 하나는 실제로 바꾸는 것)일 때 복합이다.
    설명을 청하는 문장만 있고 바꾸라는 명령형이 없으면 동사가 몇 개든 복합이 아니다."""
    whole = prose(text)
    if _READONLY_DECL_RE.search(whole):
        return False
    asked = False
    kept = []
    for sent in _SENTENCE_RE.split(drop_negated(whole)):
        if _ASK_END_RE.search(sent) or _QUESTION_END_RE.search(sent):
            asked = True
            continue
        if sent.strip():
            kept.append(sent)
    body = "\n".join(kept)
    if len(body) < MIN_COMPLEX_CHARS:
        return False
    mutating = {m.group(0).lower() for m in _MUTATING_RE.finditer(body)}
    if not mutating:
        return False
    cmd = bool(_CMD_RE.search(body))
    items = extract_items(body)
    if len(items) >= 3:
        if cmd:
            return True
        joined = " ".join(it["text"] for it in items)
        acted = sum(1 for it in items if _ACTION_RE.search(it["text"]))
        return not asked and acted >= 2 and bool(_MUTATING_RE.search(joined))
    if asked and not cmd:
        return False
    actions = _canon({m.group(0).lower() for m in _ACTION_RE.finditer(body)})
    bullets = sum(1 for line in body.splitlines() if _BULLET_RE.match(line))
    if len(actions) >= 3:
        return True
    return len(actions) >= 2 and (len(items) >= 2 or bullets >= 3) and len(body) >= 80


def is_actionable(text: str) -> bool:
    """후속 메시지가 새 일을 시키는가(상태 질문과 구분)."""
    body = drop_negated(prose(text))
    if not body:
        return False
    if is_complex(body):
        return True
    if _QUESTION_END_RE.search(body) and not _IMPERATIVE_RE.search(body):
        return False
    return bool(_IMPERATIVE_RE.search(body)) and len(body) >= 12


def classify_followup(text: str) -> str:
    """진행 중 계약에 들어온 사용자 메시지: question | followup | scope_change."""
    if not is_actionable(text):
        return "question"
    return "scope_change" if _SCOPE_CHANGE_RE.search(drop_negated(prose(text))) else "followup"


# ---- 증거 문구 ---------------------------------------------------------------------------------------------
_WEAK_EVIDENCE_RE = re.compile(
    r"^\W*(완료|완료함|완료됨|끝|끝남|done|finished|complete[d]?|ok|okay|pass(?:ed)?|확인|확인함|했음|함|good|lgtm)\W*$",
    re.IGNORECASE)


def weak_evidence(text: str) -> bool:
    """완료 문구만 있고 대상·명령·결과 위치가 없는 증거."""
    return bool(_WEAK_EVIDENCE_RE.match((text or "").strip()))


# ---- 원본 요청 항목 대조 -----------------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")
_TAG_RE = re.compile(r"^[A-Za-z]{1,3}\d{1,2}$")
_STOP = frozenset({"the", "and", "for", "with", "that", "this", "한다", "하고", "에서", "으로", "통과", "확인"})


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2 and t.lower() not in _STOP}


def _token_match(token: str, pool: set[str]) -> bool:
    if token in pool:
        return True
    if len(token) < 3:
        return False
    stem = token[:3]
    return any(len(other) >= 3 and (other.startswith(stem) or token.startswith(other[:3])) for other in pool)


def unmapped_items(items: list[dict[str, str]], requirements: list[dict[str, Any]]) -> list[dict[str, str]]:
    """계약 요구가 덮지 못한 원본 요청 항목. 항목이 둘 미만이면 대조하지 않는다."""
    if len(items) < 2:
        return []
    corpus = " ".join(f"{r.get('id', '')} {r.get('text', '')} {r.get('evidence', '')}" for r in requirements)
    pool = _tokens(corpus)
    missing = []
    for item in items:
        label = item["label"]
        if _TAG_RE.match(label) and label.lower() in pool:
            continue
        mine = _tokens(item["text"])
        if not mine:
            continue
        hit = sum(1 for tok in mine if _token_match(tok, pool))
        if hit / len(mine) < 0.5:
            missing.append(item)
    return missing


# ---- 실패 종류 ---------------------------------------------------------------------------------------------
_AUTH_OR_PERMISSION_RE = re.compile(
    r"authentication failed|\bunauthorized\b|\b401\b|login required|invalid credentials|permission denied|"
    r"operation not permitted|forbidden|\[ttakkari-guard\]|invalid[_ ]model|model not found|setup_required",
    re.IGNORECASE)
_ENGINE_TRANSIENT_RE = re.compile(
    r"timed? ?out|connection (?:reset|refused|closed|aborted)|\beconn\w*|network|temporar|\b50[234]\b|service unavailable|"
    r"internal server error|capacity (?:exhausted|exceeded)|engine unavailable|stream (?:ended|closed)|unexpected eof|서버 종료|세대|at capacity|"
    r"mcp.*(?:binding|startup)|process (?:exited|terminated) unexpectedly",
    re.IGNORECASE)


_WEEKLY_LIMIT_RE = re.compile(r"(?:weekly|daily|monthly|5[- ]?hour|session) (?:usage )?limit|reached .{0,30}limit", re.IGNORECASE)


def failure_kind(error: str | None) -> str:
    """usage_limit | model_capacity | engine_transient | permanent.

    usage_limit·model_capacity 는 계정 초기화나 폴백을 기다리는 일시 상태다. 즉시 재시도하지 않고 대기 후 이어간다.
    engine_transient 는 짧은 백오프 뒤 한정 횟수만 다시 본다. permanent 만 바로 blocked 로 굳힌다."""
    text = (error or "").strip()
    if not text:
        return "engine_transient"  # 원인을 못 남긴 종료는 일시 오류 예산 안에서만 한 번씩 본다.
    from ttakkari.runs.profiles import is_model_capacity, is_usage_limit
    if _AUTH_OR_PERMISSION_RE.search(text):
        return "permanent"
    if is_model_capacity(text):
        return "model_capacity"
    if is_usage_limit(text) or _WEEKLY_LIMIT_RE.search(text):
        return "usage_limit"
    return "engine_transient" if _ENGINE_TRANSIENT_RE.search(text) else "permanent"


def error_signature(error: str | None) -> str:
    """숫자·UUID·시각을 지운 오류의 지문. 같은 실패의 반복과 새 시도를 가른다."""
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", (error or "").lower())
    text = re.sub(r"\d+", "#", text)
    text = " ".join(text.split())[:300]
    return hashlib.sha1(text.encode()).hexdigest()[:12]


# ---- Run 실행 증거 -----------------------------------------------------------------------------------------
_WRITE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit", "apply_patch", "str_replace_editor",
                          "write_file", "edit_file", "create_file", "replace_file_content"})
_SHELL_TOOLS = frozenset({"bash", "shell", "exec", "run_command", "command_execution"})
_COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|merge|cherry-pick)\b")
_TEST_CMD_RE = re.compile(r"pytest|npm (?:run )?test|swift test|cargo test|go test|vitest|jest|unittest|\bmake test|ruff check", re.IGNORECASE)
_TEST_LINE_RE = re.compile(r"(\d+)\s+passed|(\d+)\s+failed|\bOK\b|tests? passed|exit code 0|Executed \d+ tests", re.IGNORECASE)
_SHELL_WRITE_RE = re.compile(r"(?:^|[\s;&|])(?:sed -i|tee |mv |cp |rm |mkdir |touch |git add|npm install|uv add|pip install)|>>?\s*\S")
MAX_EVENTS_SCANNED = 4000


async def run_evidence(db: AsyncSession, run: Run) -> dict[str, Any]:
    """Run 이벤트에서 실제로 일어난 일을 센다. 계약 필드를 고쳤는지가 아니라 도구·파일·커밋·테스트를 본다."""
    rows = list(await db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type.in_(("tool.call", "tool.result", "run.diff")))
        .order_by(RunEvent.seq).limit(MAX_EVENTS_SCANNED)))
    files: set[str] = set()
    commits = writes = calls = 0
    tests: list[str] = []
    test_pending = False
    for ev in rows:
        payload = ev.payload or {}
        if ev.type == "run.diff":
            files.update(str(f.get("path")) for f in payload.get("files", []) if f.get("path"))
            files.update(str(p) for p in payload.get("new_files", []))
        elif ev.type == "tool.call":
            calls += 1
            name = str(payload.get("name") or "").lower().rsplit("__", 1)[-1]
            raw = payload.get("input")
            cmd = str((raw.get("command") or raw.get("cmd") or "") if isinstance(raw, dict) else raw or "")
            if name in _WRITE_TOOLS:
                writes += 1
            elif name in _SHELL_TOOLS or "command" in (raw if isinstance(raw, dict) else {}):
                if _COMMIT_RE.search(cmd):
                    commits += 1
                if _SHELL_WRITE_RE.search(cmd):
                    writes += 1
            test_pending = bool(_TEST_CMD_RE.search(cmd))
        elif ev.type == "tool.result" and test_pending:
            test_pending = False
            for line in str(payload.get("output") or "").splitlines()[-12:]:
                if _TEST_LINE_RE.search(line):
                    tests.append(line.strip()[:160])
                    break
    tests = tests[-3:]
    effective = bool(files or commits or writes or tests)
    fingerprint = hashlib.sha1("|".join([",".join(sorted(files)), str(commits), str(writes), "|".join(tests)])
                               .encode()).hexdigest()[:12]
    return {"tool_calls": calls, "write_calls": writes, "commits": commits, "files": sorted(files)[:30],
            "file_count": len(files), "tests": tests, "effective": effective, "fingerprint": fingerprint}


def evidence_line(ev: dict[str, Any] | None) -> str:
    """사람이 읽는 한 줄 요약."""
    if not ev:
        return "실행 증거 없음"
    if not ev.get("effective"):
        return f"실행 증거 없음(도구 호출 {ev.get('tool_calls', 0)}회, 파일·커밋·테스트 변화 없음)"
    parts = [f"파일 {ev['file_count']}개 변경" if ev.get("file_count") else None,
             f"커밋/푸시 명령 {ev['commits']}회" if ev.get("commits") else None,
             f"쓰기 도구 {ev['write_calls']}회" if ev.get("write_calls") else None,
             "테스트: " + "; ".join(ev["tests"]) if ev.get("tests") else None]
    return ", ".join(p for p in parts if p)
