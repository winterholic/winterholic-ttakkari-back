"""내 보관함 보관(keep) 근거: 사용자가 실제로 한 말의 문장을 인용하고, 그 말이 이 파일을 가리키는지 확인한다.

사용자 메시지는 별도 테이블이 아니라 Run.prompt 로 저장된다. 근거가 될 수 있는 출처는 둘뿐이다.
- message: 이 세션에 사용자가 직접 보낸 메시지(origin=user Run 의 prompt 전체).
- run_prompt: 접수가 기획 세션에 넘긴 사용자 요청(origin=desk Run 의 prompt 에서 '[접수에서 넘긴 사용자 요청]' 머리글과
  '[최근 대화 — 참고만]' 이후를 뺀 사용자 발화 부분).
기획이 작업 세션에 쓴 지시(orchestrator)·작업 보고(report)·시스템 Run 은 사용자 발화가 아니므로 근거가 되지 않는다.

검사는 모두 문자열 규칙이고 의도를 추론하거나 모델에 묻지 않는다. 애매하면 거부하고 사용자는 앱의 보관 버튼으로 대체한다.
1. 인용 단위: quote 는 사용자 발화의 한 문장(또는 한 줄)과 정규화 후 정확히 같아야 한다. 부분 단어 매칭은 없다.
   문장 경계는 줄바꿈, 마침표·물음표·느낌표(뒤에 공백이나 끝이 올 때만), 한국어 종결 어미(다·요·죠·까·줘·라·자·네) 뒤 공백이다.
   공백 제외 MIN_QUOTE_CHARS 자 이상이어야 한다.
2. 허용 템플릿: 따옴표 안 텍스트·파일명/경로 토큰·'…라는' 앞 구절을 지운 문장이 한국어 …(보관함에) 보관/저장/간직해 줘, …보관함에 넣어/남겨 줘,
   영어 (please) keep/save/store/archive <object> to|in|into (my) library|vault|saved files|files 형식으로 끝나야 한다. 맞지 않으면
   모두 거부하고 사용자는 앱 버튼으로 보관한다. 의도 추론이 아니라 문장 형식의 최소 요건이다.
3. 중단·부정·제외 표지: (같은 정리를 거친) 보관 표현과 같은 문장에 그만, 더 이상, 중단, 취소, 해제, 빼, 제외, 말고, 지 마, 하지, 안 , 않, 없이, not, without 등이 하나라도
   있으면 거부한다. 정상 요청이 막히는 쪽으로 틀리는 것은 허용한다.
4. 대상 결속: (filename) 확장자를 포함한 전체 파일명이 **보관 절**(연결어미·접속 표지로 나눈 절 중 보관 서술어가 있는 마지막 절)에
   단어 경계로 있고(폴더가 적혔다면 경로 끝부분이 일치, 같은 이름의
   다른 파일이 있으면 경로로 구분돼야 함), 또는 (same_run) 대상 파일이 발화를 담은 Run 자체, 혹은 그 Run 실행 중에 시작된 자식
   작업 세션 Run 이 만든 결과물이어야 한다. 보관 절이 대상이 아닌 다른 파일을 지목하면 same_run 도 쓰지 않는다. 부산물은 same_run 에서 제외한다.
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import Artifact, ChatSession, Run, RunOrigin

# 이 Run(포함)과 그 이전 사용자·접수 Run 중 최근 몇 개에서 인용을 찾을지.
LOOKBACK_RUNS = 20
MIN_QUOTE_CHARS = 8
# 같은 이름의 다른 파일을 찾을 때 훑는 최근 Run 수(이 세션과 이 세션이 띄운 작업 세션).
AMBIGUITY_RUNS = 50
DESK_HEADER = "[접수에서 넘긴 사용자 요청]"
DESK_HISTORY_MARKER = "[최근 대화 — 참고만]"
EXCERPT_CHARS = 1000

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")
_KOREAN_END = re.compile(r"(?<=[다요죠까줘라자네])\s+")
_TRAILING = re.compile(r"[\s.!?。！？~]+$")

# 보관 동작 표현. 공백을 뺀 소문자 문장에서 부분 문자열로 찾고, 영어는 단어로 찾는다.
_KEEP_KO = ("보관", "간직", "저장해", "저장하", "저장할", "남겨둬", "남겨줘", "남겨주", "남겨두", "남겨놔")
_KEEP_EN = re.compile(r"\b(?:keep|save|store|archive)\b")
# 부정·금지·제외 형태소. 보관 표현과 같은 문장에 하나라도 있으면 거부한다(정상 요청이 막혀도 허용한다).
_NEGATIVE_KO = (
    "지마", "지말", "말아", "말고", "마세요", "마라", "마요", "말자", "말것", "하지", "아니", "아님", "안해", "안넣", "안돼", "안되", "않", "못",
    "없이", "필요없", "빼고", "빼줘", "빼주", "제외", "금지", "싫",
)
_NEGATIVE_EN = re.compile(
    r"\b(?:not|never|no need|don'?t|dont|do not|without|cannot|can'?t|shouldn'?t|should not|exclude|except|skip)\b|n't\b")

# same_run 에서도 제외하는 부산물. 0052 마이그레이션의 기준(로그, 증거·캡처 폴더, 워커 증거 JSON)을 따르고, 스크린샷·임시·검증 산출물을 더한다.
_BYPRODUCT_DIRS = frozenset({"evidence", "browser", "screenshots", "screenshot", "captures", "capture", ".verification",
                             "tmp", "temp", "__pycache__", "node_modules", ".git"})
_BYPRODUCT_EXT = frozenset({".log", ".tmp", ".temp", ".bak", ".pyc", ".pid"})
_BYPRODUCT_NAME = ("screenshot", "capture", "evidence")


@dataclass(frozen=True)
class QuoteMatch:
    run: Run
    source: str
    excerpt: str


@dataclass(frozen=True)
class FileMention:
    token: str
    path_token: str | None  # 문장에 폴더가 함께 적혀 있으면 그 경로 끝부분(소문자, 슬래시 구분)


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _unit(text: str) -> str:
    return _TRAILING.sub("", normalize(text)).casefold()


def sentence_units(text: str) -> set[str]:
    """발화에서 인용으로 인정하는 단위(줄, 문장, 한국어 종결 단위)를 정규화해 모은다."""
    units: set[str] = set()
    for line in text.splitlines():
        line = normalize(line)
        if not line:
            continue
        units.add(_unit(line))
        for sentence in _SENTENCE_END.split(line):
            units.add(_unit(sentence))
            units.update(_unit(part) for part in _KOREAN_END.split(sentence))
    units.discard("")
    return units


def is_whole_sentence(quote: str, text: str) -> bool:
    needle = _unit(quote)
    return len(needle.replace(" ", "")) >= MIN_QUOTE_CHARS and needle in sentence_units(text)


def _flat(sentence: str) -> str:
    return "".join(unicodedata.normalize("NFC", sentence).split()).casefold()


_QUOTED = re.compile(
    r"\"[^\"]*\"|(?<!\w)'[^']*'(?!\w)|“[^”]*”|‘[^’]*’|「[^」]*」|`[^`]*`")
_PATH_TOKEN = re.compile(r"\S*/\S*")
_FILE_TOKEN = re.compile(r"[\w.~\-]*\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9_])")
_CITED = re.compile(r"(?:이?라는|이?란)(?![가-힣])")
_BREAK = ",.;:!?，。"
# 요청 끝에 붙는 공손·감사 표현. 마지막 서술 요청을 판정할 때 떼어 낸다.
_FILLER = frozenset({"부탁해요", "부탁해", "부탁합니다", "부탁드려요", "부탁드립니다", "고마워요", "고마워", "감사해요", "감사해",
                     "감사합니다", "제발", "좀", "플리즈", "ㅎㅎ", "ㅋㅋ", "^^", "ㅠㅠ"})
_TRAIL_PUNCT = re.compile(r"[\s.!?~,…^ㅎㅋㅠ]+$")
_REQ = r"(?:줘요?|주세요|주라|줘라|줄래요?|주실래요|주십시오|주시겠어요|줄수있(?:어요?|나요))"
_HOLD = r"(?:둬요?|두세요|놔요?|놓아줘)"
# 허용 템플릿. 이 형식에 맞지 않으면 모두 거부하고, 사용자는 앱의 '내 보관함에 보관' 버튼으로 대신 보관한다.
# 한국어(공백을 뺀 문장 끝): [내] [보관함|라이브러리+에] + 보관/저장/간직/넣어/남겨 + 요청 어미.
# 넣어·남겨 는 '그대로 남겨줘', '서랍에 넣어줘'처럼 보관이 아닌 뜻이 흔해 보관함/라이브러리 목적지가 있을 때만 쓴다.
_KO_KEEP_REQUEST = re.compile(
    r"(?:"
    rf"(?:보관|저장|간직)(?:해{_REQ}|해(?:요|라)?|하세요|해{_HOLD})"
    rf"|(?:보관함|라이브러리)에(?:넣어|남겨)(?:{_REQ}|{_HOLD})"
    r")$")
# 영어(문장 전체): (please |can you |could you )* keep|save|store|archive <object> to|in|into [my] library|vault|saved files|files [please]
_EN_KEEP_REQUEST = re.compile(
    r"^(?:(?:please|can you|could you)\s+)*(?:keep|save|store|archive)\s+(?:(?P<obj>.+?)\s+)?(?:to|in|into)\s+"
    r"(?:my\s+)?(?:library|vault|saved files|files)(?:\s+please)?$")
# 영어 목적어 자리에 이런 말이 있으면 보관 요청이 아니다(out of/outside/away/from/not/unchanged/else ...).
_EN_OBJECT_BLOCK = re.compile(
    r"\b(?:out|outside|away|from|not|never|no|don'?t|dont|unchanged|without|except|instead|else|elsewhere|somewhere|"
    r"separate|apart|off|only)\b|n't\b")
# 한국어에서 문장 어디에 있든 보관 요청을 무효로 하는 중단·제외 표지(공백을 뺀 문장에서 찾는다). '안 ' 은 단어로 따로 본다.
_KO_STOP = ("그만", "더이상", "더는", "중단", "취소", "해제", "빼", "제외", "말고", "지마", "지말", "없이", "필요없")
_KO_AN_WORD = re.compile(r"(?:^|\s)안(?:\s|$)")


def scrub(sentence: str) -> str:
    """보관 표현·부정을 판정하기 전에 문장에서 지운다: 따옴표 안 텍스트, 파일명·경로 토큰, '…라는/…이라는/…란' 앞의 인용 구절.

    인용된 말, 파일 이름 속 단어(archive.md), 설명 대상('…라는 문장')이 보관 요청처럼 보이는 것을 막는다."""
    text = normalize(sentence)
    text = _QUOTED.sub(" ", text)
    text = _PATH_TOKEN.sub(" ", text)
    text = _FILE_TOKEN.sub(" ", text)
    while (m := _CITED.search(text)) is not None:
        start = max((text.rfind(ch, 0, m.start()) for ch in _BREAK), default=-1) + 1
        text = text[:start] + " " + text[m.end():]
    return normalize(text)


def has_keep_expression(sentence: str) -> bool:
    """scrub 한 문장에 보관 동작 표현이 있는지(부정 판정의 전제). 보관 요청인지는 is_keep_request 가 본다."""
    text = scrub(sentence)
    if any(token in "".join(text.split()).casefold() for token in _KEEP_KO):
        return True
    return _KEEP_EN.search(text.casefold()) is not None


def is_keep_request(sentence: str) -> bool:
    """scrub 한 문장의 마지막 서술 요청이 보관 동작인지. 보관을 요청하는 문장 형식의 최소 요건이며 의도 추론이 아니다.

    허용 템플릿에 맞아야 한다. 한국어: 문장 끝이 [내] [보관함|라이브러리에] + 보관/저장/간직해 줘·주세요·줄래·해, (보관함|라이브러리)에
    넣어/남겨 줘·주세요·둬. 앞 절에 다른 동사가 있어도(만들어서 보관함에 넣어줘) 마지막이 보관이면 된다.
    영어: 문장 전체가 (please|can you|could you)* keep|save|store|archive <object> to|in|into [my] library|vault|saved files|files [please]
    이고 object 에 out/outside/away/from/not/unchanged 같은 말이 없어야 한다. 중단·부정·제외 표지는 is_negated 가 따로 막는다."""
    text = scrub(sentence)
    if re.search(r"[가-힣]", text):
        words = _TRAIL_PUNCT.sub("", text).split()
        while words and _TRAIL_PUNCT.sub("", words[-1]) in _FILLER:
            words.pop()
            words = _TRAIL_PUNCT.sub("", " ".join(words)).split()
        flat = "".join(words).casefold()
        flat = _TRAIL_PUNCT.sub("", flat)
        return _KO_KEEP_REQUEST.search(flat) is not None
    m = _EN_KEEP_REQUEST.match(_TRAIL_PUNCT.sub("", text.casefold()))
    return m is not None and not (m.group("obj") and _EN_OBJECT_BLOCK.search(m.group("obj")))


def is_negated(sentence: str) -> bool:
    """scrub 한 문장에 부정·금지·제외 형태소가 하나라도 있는지."""
    text = scrub(sentence)
    flat = "".join(text.split()).casefold()
    if any(token in flat for token in (*_NEGATIVE_KO, *_KO_STOP)) or _KO_AN_WORD.search(text):
        return True
    return _NEGATIVE_EN.search(text.casefold()) is not None


# 절을 나누는 연결어미·접속 표지: -고/-하고, -해서/-서, -며/-면서, -은 뒤/후, 그리고, 그다음, 그 후, 및, 쉼표·세미콜론, 영어 and/then/after.
# '-서' 는 해서·어서·아서·가서처럼 용언 연결형일 때만 나눈다(기획서·보고서·문서 같은 명사는 나누지 않는다). '-고' 로 끝나는 명사 앞에서는
# 나뉠 수 있지만, 그러면 파일명이 보관 절 밖으로 밀려 same_run 으로만 판단하는 거부 쪽으로 틀린다.
_CLAUSE_SPLIT = re.compile(
    r"\s*[,;，；]\s*"
    r"|(?<=고)\s+|(?<=[해어아여라가와나워줘]서)\s+|(?<=며)\s+|(?<=면서)\s+|(?<=뒤)\s+|(?<=후)\s+"
    r"|(?:^|\s)(?:그리고|그다음|및)\s+"
    r"|\s+(?:and|then|after)\s+", re.IGNORECASE)


def keep_clause(sentence: str) -> str:
    """문장을 절로 나눠, 보관 서술어가 있는 마지막 절(보관 절)을 돌려준다. 보관 서술어가 있는 절이 없으면 마지막 절."""
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(normalize(sentence)) if c and c.strip()]
    if not clauses:
        return ""
    return next((c for c in reversed(clauses) if has_keep_expression(c)), clauses[-1])


def names_other_file(clause: str) -> bool:
    """보관 절에 파일명(확장자가 있는 토큰)이나 경로 토큰이 있는지. 대상 파일과 맞지 않을 때 다른 파일을 지목한 문장인지 가르는 데 쓴다."""
    return _PATH_TOKEN.search(clause) is not None or _FILE_TOKEN.search(clause) is not None


def names_file(sentence: str, path: Path) -> FileMention | None:
    """인용 문장에 확장자를 포함한 전체 파일명이 단어 경계로 있고, 폴더가 함께 적혔다면 경로 끝부분이 대상 경로와 맞는지.

    확장자를 뺀 이름(stem)으로는 맞추지 않는다. 폴더 없이 이름만 적은 경우 같은 이름의 다른 파일이 있는지는 호출한 쪽이 따로 본다."""
    text = normalize(sentence).casefold()
    name = path.name.casefold()
    full = path.resolve().as_posix().casefold()
    pattern = re.compile(r"(?P<folder>/?(?:[\w.~\-]+/)*)" + re.escape(name) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])")
    bare = False
    for m in pattern.finditer(text):
        folder = m.group("folder")
        if not folder:
            if m.start() > 0 and re.match(r"[A-Za-z0-9_\-.]", text[m.start() - 1]):
                continue  # 다른 이름의 일부(예: my.report.md)
            bare = True
            continue
        token = os.path.expanduser(folder + name) if folder.startswith("~/") else (folder + name).removeprefix("./")
        if token.startswith("/"):
            matched = full == token
        else:
            matched = full.endswith("/" + token)
        if matched:
            return FileMention(name, token)
    return FileMention(name, None) if bare else None


def is_byproduct(path: Path) -> bool:
    """로그·스크린샷·캡처·임시·증거·검증 산출물처럼 사용자가 요청한 결과물이 아닐 가능성이 큰 부산물."""
    parts = [p.casefold() for p in path.parts]
    name = path.name.casefold()
    if path.suffix.casefold() in _BYPRODUCT_EXT or any(p in _BYPRODUCT_DIRS for p in parts[:-1]):
        return True
    if any(token in name for token in _BYPRODUCT_NAME):
        return True
    return path.suffix.casefold() == ".json" and name.startswith(("tmp", "temp"))


def user_utterance(run: Run) -> tuple[str, str] | None:
    """Run 에서 사용자가 실제로 한 말과 출처 이름. 사용자 발화가 아닌 Run 은 None."""
    if run.origin == RunOrigin.user:
        return run.prompt, "message"
    if run.origin == RunOrigin.desk:
        body = run.prompt.removeprefix(DESK_HEADER)
        # 첫 표지에서 자른다. 사용자 본문에 표지가 들어 있으면 그 앞까지만 인정하는 보수적인 방향이다.
        body = body.split(DESK_HISTORY_MARKER, 1)[0]
        return body, "run_prompt"
    return None


async def find_user_quotes(db: AsyncSession, session_id: uuid.UUID, anchor: Run, quote: str) -> list[QuoteMatch]:
    """anchor Run 이 속한 세션에서 quote 가 온전한 한 문장인 사용자 발화를 최근 순으로 모두 찾는다. 같은 세션 밖은 보지 않는다."""
    rows = await db.scalars(
        select(Run).where(
            Run.session_id == session_id, Run.origin.in_([RunOrigin.user, RunOrigin.desk]),
            Run.created_at <= anchor.created_at)
        .order_by(Run.created_at.desc(), Run.id.desc()).limit(LOOKBACK_RUNS))
    out: list[QuoteMatch] = []
    for run in rows:
        said = user_utterance(run)
        if said is None:
            continue
        text, source = said
        if is_whole_sentence(quote, text):
            out.append(QuoteMatch(run=run, source=source, excerpt=normalize(text)[:EXCERPT_CHARS]))
    return out


def creator_run_id(path: Path, outbox_root: Path, artifact_run_id: uuid.UUID | None) -> uuid.UUID | None:
    """파일을 만든 Run. Run outbox(outbox/<run id>/…) 안이면 그 Run 이고, 아니면 등록된 행의 run_id 이다."""
    try:
        first = path.resolve().relative_to(outbox_root.resolve()).parts[0]
        return uuid.UUID(first)
    except (ValueError, IndexError):
        return artifact_run_id


async def made_by_request_run(db: AsyncSession, *, session_id: uuid.UUID, match: QuoteMatch, path: Path,
                              created_by: uuid.UUID | None) -> bool:
    """대상 파일이 인용 발화를 처리한 Run 이 만든 결과물인지(same_run).

    허용: (1) 발화를 담은 Run 자체가 만든 파일, (2) 그 Run 이 실행 중이던 시간(시작~종료, 아직 실행 중이면 지금까지) 안에 시작된
    이 세션의 자식 작업 세션 Run 이 만든 파일. 그 뒤 턴의 Run 이나 Run 종료 뒤에 시작된 자식 Run 은 허용하지 않는다.
    부산물(로그·스크린샷·캡처·증거·임시 파일)은 허용하지 않으며 사용자 문장에 파일명이 있어야 한다.
    Run 이 기록된 시각만으로는 보관 직전에 공유한 오래된 파일도 통과하므로 파일 수정 시각도 발화 뒤여야 한다."""
    if created_by is None or is_byproduct(path):
        return False
    creator = await db.get(Run, created_by)
    if creator is None:
        return False
    if creator.id != match.run.id:
        owner = await db.get(ChatSession, creator.session_id)
        if owner is None or owner.parent_id != session_id or creator.session_id == session_id:
            return False
        started = match.run.started_at or match.run.created_at
        if creator.created_at < started or (match.run.finished_at is not None and creator.created_at > match.run.finished_at):
            return False
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False
    return modified >= match.run.created_at


async def same_basename_paths(db: AsyncSession, *, session_id: uuid.UUID, path: Path, outbox_root: Path) -> list[str]:
    """이 세션과 이 세션이 띄운 작업 세션의 artifact 및 최근 Run outbox 에 있는, 같은 파일명의 다른 경로."""
    real = path.resolve().as_posix()
    name = path.name
    sessions = [session_id, *(await db.scalars(select(ChatSession.id).where(ChatSession.parent_id == session_id)))]
    found = {r.resolve().as_posix() if (r := Path(p)).is_absolute() else p for p in await db.scalars(
        select(Artifact.original_path).where(
            Artifact.deleted_at.is_(None), Artifact.session_id.in_(sessions), func.lower(Artifact.filename) == name.lower()))}
    run_ids = await db.scalars(select(Run.id).where(Run.session_id.in_(sessions)).order_by(Run.created_at.desc()).limit(AMBIGUITY_RUNS))
    for run_id in run_ids:
        box = outbox_root / str(run_id)
        if box.is_dir():
            found.update(f.resolve().as_posix() for f in box.rglob(name) if f.is_file())
    found.discard(real)
    return sorted(found)


def build_evidence(match: QuoteMatch, *, session_id: uuid.UUID, run: Run, quote: str, kept_at: datetime,
                   binding: str, binding_detail: str) -> dict[str, Any]:
    """keep_evidence JSON. message_id 는 사용자 발화를 담은 Run(= 저장된 사용자 메시지)의 ID 이다.

    binding: filename(인용 문장에 파일 이름이 있음) | same_run(그 발화 뒤 이 요청을 처리한 Run 이 만든 파일)."""
    return {
        "session_id": str(session_id),
        "run_id": str(run.id),
        "message_id": str(match.run.id),
        "message_source": match.source,
        "message_origin": str(match.run.origin),
        "message_at": match.run.created_at.isoformat(),
        "quote": normalize(quote),
        "quote_sentence": _unit(quote),
        "binding": binding,
        "binding_detail": binding_detail,
        "user_message": match.excerpt,
        "requested_at": match.run.created_at.isoformat(),
        "kept_at": kept_at.isoformat(),
    }
