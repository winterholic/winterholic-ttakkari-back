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

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import Run
from ttakkari.orchestrator import observed
from ttakkari.orchestrator.observed import (
    MAX_EVENTS_SCANNED,  # noqa: F401  (옛 이름 호환)
)

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
    r"마이그레이션|fix|build|개선|개편|변경|교체|최적화|보완|정비|조사|분석|진행|수행|실행|처리)(?:하고|하며|하되|해서|하여|한 뒤|한 다음|해줘|해 줘|해주세요|해 주세요|하세요|해라|하자|해)|"
    r"고치(?:고|세요|며)|고쳐|만들(?:고|어|며)|바꿔|바꾸고|돌려|올려|붙여|넣어)", re.IGNORECASE)
# 부정·금지 표지. 금지는 문장 전체가 아니라 그 절만 시킨 일에서 뺀다("수정하되 계정은 변경하지 말고 테스트하고 배포해" 에서
# 수정·테스트·배포는 남는다).
_NEG_MARK = (r"지\s*(?:말고|말아|마라|마세요|마\b|않(?:고|으며|는다|습니다|음)|못)|금지|안\s*한다|않는다|않습니다|않음|"
             r"\bdo not\b|\bdon't\b|\bnever\b|\bwithout\b")
_NEG_RE = re.compile(_NEG_MARK, re.IGNORECASE)
_NEG_END_RE = re.compile(rf"(?:{_NEG_MARK})\S{{0,6}}[\s.!?。]*$", re.IGNORECASE)
# 절 경계. 쉼표·"~하고"·"~한 뒤"·"~하되" 뒤에서 새 절이 시작한다.
_CLAUSE_END_RE = re.compile(r"(?:고|한\s*뒤|한\s*다음|하되|다만)[\s,]|[,;，]")
_VERB_END_RE = re.compile(r"(?:고|한\s*뒤|한\s*다음|하되|다만)[\s,]")
# 설명·질문을 청하는 문장. 이 문장 속 동사는 시킨 일이 아니라 설명 대상이다("수정, 배포 방식을 설명해줘").
_ASK_END_RE = re.compile(r"(?:설명|알려|말해|가르쳐|요약|비교|보여|궁금)\S{0,8}[\s.!?]*$")
# 같은 일을 가리키는 동사는 한 번만 센다(수정·고치, 커밋·푸시·병합 은 한 묶음의 git 작업).
_CANON = {"고쳐": "수정", "고치": "수정", "커밋": "git", "commit": "git", "푸시": "git", "push": "git", "병합": "git",
          "머지": "git", "merge": "git", "deploy": "배포", "refactor": "리팩", "fix": "수정"}
_INLINE_MARK_RE = re.compile(r"(?:^|(?<=[\s:]))(\d{1,2})[.)]\s+(?=\S)")
# 읽기 전용: 요청 전체의 제한(독립 문장이거나 "이 작업은 읽기 전용")과 한 단계만 제한하는 표현("운영 기록은 읽기 전용으로 조사하고")을 가른다.
_READONLY_RE = re.compile(r"읽기\s*전용|read[- ]only", re.IGNORECASE)
_READONLY_WHOLE_RE = re.compile(
    r"^\W*(?:(?:읽기\s*전용|read[- ]only)\s*(?:검증|작업|모드|조사|점검|리뷰|요청)?\s*(?:이다|입니다|임|이며|으로만|만)|"
    r"(?:이번|이|본)?\s*(?:작업|검증|요청|조사|단계|카드)(?:\s*전체)?\s*(?:은|는|도)\s*(?:모두\s*)?(?:읽기\s*전용|read[- ]only)|"
    r"(?:전체|모든\s*단계)\S*\s*(?:읽기\s*전용|read[- ]only))", re.IGNORECASE)
# 번호 항목의 "1. " 마침표에서는 문장을 끊지 않는다.
_SENTENCE_RE = re.compile(r"(?<=[^\d\s][.!?。])\s+|\n")
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_LOG_LINE_RE = re.compile(
    r"^\s*(?:Traceback\b|File \".*\", line \d+|at\s+\S+[(:]|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}|"
    r"\[?(?:ERROR|WARN(?:ING)?|INFO|DEBUG|FATAL)\]?[\s:]|[\w.]+(?:Error|Exception)\b.*:|\$ |>>> )", re.IGNORECASE)


def request_text(prompt: str, origin: str) -> str:
    """사용자의 말만 남긴다. 접수 머리글과 참고용 최근 대화는 요청이 아니다."""
    text = (prompt or "").strip()
    if text.startswith(DESK_HEADER):  # 접수 머리글은 구역 제목이 아니다. 출처 표기와 무관하게 뗀다.
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


def _cut(sent: str, start: int, end: int) -> str:
    return (sent[:start] + " " + sent[end:]).strip()


def _scope_clean(sent: str) -> tuple[str, bool]:
    """한 문장에서 금지·읽기 전용 절을 뺀 나머지와, 문장 전체가 요청 전체의 읽기 전용 선언인지.

    - 문장 끝 금지("…요청을 하지 않는다."): 쉼표로 나열한 금지 목록이므로 마지막 `고`/`하되` 경계 뒤부터 끝까지를 뺀다.
    - 문장 중간 금지("계정은 변경하지 말고 …"): 직전 절 경계부터 금지 표지 끝까지만 뺀다.
    - 읽기 전용 표현: 요청 전체 선언이면 whole, 아니면 그 절만 뺀다."""
    plain = re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+", "", sent)
    if _READONLY_RE.search(plain) and _READONLY_WHOLE_RE.search(plain):
        return "", True
    for _ in range(6):  # 한 문장의 표지 개수를 넘지 않는 상한
        ro = _READONLY_RE.search(sent)
        neg = _NEG_RE.search(sent)
        hit = min((m for m in (ro, neg) if m), key=lambda m: m.start(), default=None)
        if hit is None:
            break
        verb_bounds = [m.end() for m in _VERB_END_RE.finditer(sent) if m.end() <= hit.start()]
        if hit is neg and _NEG_END_RE.search(sent) and _NEG_END_RE.search(sent).start() <= hit.start():
            start = verb_bounds[-1] if verb_bounds else 0
            sent = sent[:start].strip()
            break
        bounds = [m.end() for m in _CLAUSE_END_RE.finditer(sent) if m.end() <= hit.start()]
        start = bounds[-1] if bounds else 0
        if hit is neg:
            end = hit.end()
        else:
            after = [m.end() for m in _CLAUSE_END_RE.finditer(sent) if m.end() > hit.end()]
            end = after[0] if after else len(sent)
        sent = _cut(sent, start, end)
    return sent, False


def scoped_sentences(text: str) -> tuple[list[str], bool]:
    """금지·읽기 전용 절을 뺀 문장들과, 요청 전체가 읽기 전용이라는 선언이 있었는지."""
    out, whole_readonly = [], False
    for sent in _SENTENCE_RE.split(text or ""):
        if not sent.strip():
            continue
        cleaned, whole = _scope_clean(sent)
        whole_readonly = whole_readonly or whole
        if cleaned.strip():
            out.append(cleaned)
    return out, whole_readonly


def drop_negated(text: str) -> str:
    """금지·읽기 전용으로 제한된 절을 뺀 문장(시킨 일만 남긴다)."""
    return "\n".join(scoped_sentences(text)[0])


def extract_constraints(text: str) -> list[str]:
    """원본 요청의 금지·읽기 전용 문장. 계약을 만들거나 바꿔도 금지 조건이 사라지지 않게 보존한다."""
    found = [sent.strip() for sent in _SENTENCE_RE.split(prose(text)) if sent.strip()
             and (_NEG_RE.search(sent) or _READONLY_RE.search(sent))]
    return [c[:300] for c in found][:20]


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


def _is_numbered_heading(label: str, body: str, rest: list[str]) -> bool:
    """`1. 작업 배경 및 목적` 처럼 동사 없는 짧은 번호 제목. 아래에 불릿·들여쓴 내용이 이어질 때만 제목으로 본다.

    제목은 요구가 아니라 내용을 묶는 이름이라 대조 대상에서 뺀다. 아래 내용이 없는 동사 없는 항목("3. 로그인 화면")은
    실제 요구일 수 있어 남긴다."""
    if not label.isdigit() or len(body) > 40 or _ACTION_RE.search(body) or body.rstrip().endswith((".", "다", "요")):
        return False
    nxt = next((ln for ln in rest if ln.strip()), "")
    return bool(_BULLET_RE.match(nxt)) or (nxt.startswith(("  ", "\t")) and not _ITEM_RE.match(nxt))


def extract_items(text: str) -> list[dict[str, str]]:
    """번호·라벨이 붙은 줄만 요구 항목으로 본다. 불릿은 경로·보조 설명이 많아 제외한다.

    `[백엔드] 1..3 / [프런트] 1..3` 처럼 라벨이 겹치면 구역 이름을 붙여 id 를 유일하게 만든다(요구 id 는 유일해야 한다)."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    used: set[str] = set()
    section = ""
    lines = (text or "").splitlines()
    for idx, line in enumerate(lines):
        head = _SECTION_RE.match(line)
        if head:
            section = (head.group("h") or head.group("b") or "").strip()
            continue
        for label, body in _line_items(line):
            if _is_numbered_heading(label, body, lines[idx + 1:]):
                continue
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
    sentences, whole_readonly = scoped_sentences(prose(text))
    if whole_readonly:
        return False
    asked = False
    kept = []
    for sent in sentences:
        if _ASK_END_RE.search(sent) or _QUESTION_END_RE.search(sent):
            asked = True
            continue
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


_LOCATOR_RE = re.compile(
    r"[\w.-]+/[\w./-]+|[\w-]+\.(?:py|tsx?|jsx?|md|json|ya?ml|toml|swift|sql|sh|css|html|go|rs|java|kt|txt)\b|"
    r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b|\d+\s*(?:passed|failed|tests?|건|개|줄|lines?)|https?://|`[^`]+`|:\d+\b|\$\s|→|"
    r"exit code", re.IGNORECASE)
_DONE_MARK_RE = re.compile(
    r"완료|했습니다|했음|하였|됨\b|되었|성공|정상|문제\s*없|이상\s*없|\bdone\b|\bcomplete|\bsuccess|\bfinished\b|\bfixed\b|"
    r"\bworks?\b|\bverified\b|\bpassed\b", re.IGNORECASE)
# 완료를 말할 뿐 내용이 없는 단어(한국어는 어간 접두, 영어는 낱말 일치). 이 단어를 뺀 내용어가 적으면 일반 완료 문장이다.
_GENERIC_KO = ("모든", "모두", "전체", "작업", "요구", "요청", "항목", "정상", "수행", "완료", "문제", "없", "성공", "처리", "확인",
               "이상", "완벽", "끝", "진행", "마무리", "충족", "달성", "추가", "이슈", "잘", "마쳤", "했", "됨", "되었", "되어",
               "및", "그리고", "해당", "관련", "통과", "사항")
_GENERIC_EN = frozenset({
    "all", "task", "tasks", "complete", "completed", "completely", "successfully", "success", "done", "finished", "works",
    "working", "fine", "verified", "verify", "everything", "requirement", "requirements", "item", "items", "no", "issue",
    "issues", "problem", "problems", "without", "and", "the", "was", "were", "has", "have", "been", "are", "is", "it", "this",
    "that", "ok", "okay", "good", "properly", "correctly", "normally", "as", "expected", "of", "to", "in", "with"})
MIN_CONTENT_TOKENS = 3


def generic_claim(text: str) -> bool:
    """위치·명령·결과 값이 없고 완료 문구뿐인 긴 문장. 길이를 늘려도 내용어가 없으면 일반 주장이다.

    파일 경로·SHA·`N passed`·URL·백틱 코드·줄 번호 같은 위치가 하나라도 있거나, 완료 문구를 뺀 내용어가 충분하면(조사 결과·원인
    설명 같은 정성 증거) 일반 주장이 아니다."""
    body = (text or "").strip()
    if not body or _LOCATOR_RE.search(body) or not _DONE_MARK_RE.search(body):
        return False
    content = [t for t in (w.lower() for w in re.findall(r"[0-9A-Za-z가-힣_]+", body))
               if len(t) >= 2 and t not in _GENERIC_EN and not t.startswith(_GENERIC_KO)]
    return len(content) < MIN_CONTENT_TOKENS


def weak_evidence(text: str) -> bool:
    """완료 문구만 있고 대상·명령·결과 위치가 없는 증거. 한 단어 완료 문구와 긴 일반 완료 문장을 모두 본다."""
    body = (text or "").strip()
    return bool(_WEAK_EVIDENCE_RE.match(body)) or generic_claim(body)


# 보고의 항목 표기: `1.` `1)` `항목 1:` `1번:` 표의 `| 1 |`.
_REPORT_LABEL_RE = re.compile(
    r"^\W*(?:항목\s*)?(?P<label>\d{1,2}|[A-Za-z]{1,3}\d{1,2})\s*(?:번\s*)?[).:\]|](?P<rest>.*)$")


def unreported_items(items: list[dict[str, str]], body: str) -> list[dict[str, str]]:
    """작업자 지시의 번호 항목 중 완료 보고가 항목별로 다루지 않은 것. 항목이 둘 미만이면 대조하지 않는다.

    보고의 한 줄이 그 항목의 라벨로 시작하고 라벨 뒤에 내용이 있거나(`3) 완료` 처럼 일반 완료 문구뿐이면 제외),
    한 줄이 항목 단어를 절반 이상 담으면 덮은 것으로 본다. 라벨만 맞거나 비슷한 단어만 있는 줄은 대응으로 보지 않는다."""
    if len(items) < 2:
        return []
    lines = [ln.strip() for ln in (body or "").splitlines() if ln.strip() and not set(ln.strip()) <= set("|-: ")]
    # 번호 없이 항목마다 한 줄씩 쓴 보고(지시가 요구하는 형식)는 문구를 바꿔 쓰면 단어 대조로 대응을 못 찾는다. 항목 표기가 하나도
    # 없고 줄 수가 항목 수 이상이면 코드로 대응을 판정할 수 없으므로 누락으로 몰지 않는다(미확인).
    unlabeled = not any(_REPORT_LABEL_RE.match(ln) for ln in lines)
    if unlabeled and len(lines) >= len(items):
        return []
    missing = []
    for item in items:
        mine = _tokens(item["text"])
        title = _tokens(re.split(r"[:：(（—]|\s-\s", item["text"], maxsplit=1)[0])
        label = item["label"].lower().rsplit("-", 1)[-1]
        covered = False
        for ln in lines:
            m = _REPORT_LABEL_RE.match(ln)
            rest = m.group("rest") if m else ln
            if len(rest) < 3 or weak_evidence(rest):
                continue
            by_label = bool(m) and m.group("label").lower() == label
            by_words = bool(mine) and sum(1 for t in mine if _token_match(t, _tokens(ln))) / len(mine) >= 0.5
            # 항목 앞머리(제목)를 그대로 쓴 줄 `- 거짓 완료 차단 반례 탐색: 결과…` 도 그 항목을 다룬 것이다.
            by_title = len(title) >= 2 and sum(1 for t in title if _token_match(t, _tokens(ln))) / len(title) >= 0.8
            if by_label or by_words or by_title:
                covered = True
                break
        if not covered:
            missing.append(item)
    return missing


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
    """계약 요구 하나가 덮지 못한 원본 요청 항목. 항목이 둘 미만이면 대조하지 않는다.

    요구 전체를 한 덩어리로 합쳐 겹침을 보지 않고 요구 하나씩 비교한다(서로 무관한 요구들의 단어를 모아 항목을 채운 것으로
    치지 않는다). 번호 라벨은 요구 id·본문에 그 라벨이 있을 때만 대응으로 본다(증거 문자열의 우연한 일치는 제외).
    여기서 못 찾은 항목은 항목별 대응 또는 범위 제외 기록(work._resolve_gaps)이 있어야 통과한다."""
    if len(items) < 2:
        return []
    reqs = [(str(r.get("id", "")).lower(), _tokens(r.get("text", "")),
             _tokens(f"{r.get('id', '')} {r.get('text', '')} {r.get('evidence', '')}")) for r in requirements]
    missing = []
    for item in items:
        label = item["label"].lower()
        if _TAG_RE.match(item["label"]) and any(rid == label or label in text_tokens for rid, text_tokens, _ in reqs):
            continue
        mine = _tokens(item["text"])
        if not mine:
            continue
        best = max((sum(1 for tok in mine if _token_match(tok, pool)) / len(mine) for _, _, pool in reqs), default=0.0)
        if best < 0.5:
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
async def run_evidence(db: AsyncSession, run: Run) -> dict[str, Any]:
    """Run 이벤트에서 실제로 일어난 일을 센다. 계약 필드를 고쳤는지가 아니라 성공한 도구·파일·커밋·테스트를 본다.

    호출과 결과는 엔진 call_id 로 잇고, 실패·취소·결과 없는 호출은 성과로 세지 않는다. 진전 여부는 이 관측의 사실 지문을
    목표 전체 이력과 비교해 observed.assess_progress 가 판정한다(직전 Run 과의 지문 비교가 아니다)."""
    return observed.legacy_summary(await observed.observe_run(db, run))


def evidence_line(ev: dict[str, Any] | None) -> str:
    """사람이 읽는 한 줄 요약."""
    if not ev:
        return "실행 증거 없음"
    failed = ev.get("failed") or {}
    tail = f"; 실패한 시도 {', '.join(f'{k} {n}회' for k, n in sorted(failed.items()))}" if failed else ""
    if not ev.get("effective"):
        return f"실행 증거 없음(도구 호출 {ev.get('tool_calls', 0)}회, 파일·커밋·테스트 변화 없음{tail})"
    parts = [f"파일 {ev['file_count']}개 변경" if ev.get("file_count") else None,
             f"커밋/푸시 {ev['commits']}회 성공" if ev.get("commits") else None,
             f"쓰기 도구 {ev['write_calls']}회" if ev.get("write_calls") else None,
             "테스트: " + "; ".join(ev["tests"]) if ev.get("tests") else None]
    return ", ".join(p for p in parts if p) + tail
