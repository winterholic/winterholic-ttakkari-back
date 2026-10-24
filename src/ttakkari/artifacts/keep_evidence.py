"""내 보관함 보관(keep) 근거: 사용자가 실제로 한 말의 문장을 인용하고, 그 말이 이 파일을 가리키는지 확인한다.

사용자 메시지는 별도 테이블이 아니라 Run.prompt 로 저장된다. 근거가 될 수 있는 출처는 둘뿐이다.
- message: 이 세션에 사용자가 직접 보낸 메시지(origin=user Run 의 prompt 전체).
- run_prompt: 접수가 기획 세션에 넘긴 사용자 요청(origin=desk Run 의 prompt 에서 '[접수에서 넘긴 사용자 요청]' 머리글과
  '[최근 대화 — 참고만]' 이후를 뺀 사용자 발화 부분).
기획이 작업 세션에 쓴 지시(orchestrator)·작업 보고(report)·시스템 Run 은 사용자 발화가 아니므로 근거가 되지 않는다.

검사는 모두 문자열 규칙이고 의도를 추론하거나 모델에 묻지 않는다.
1. 인용 단위: quote 는 사용자 발화의 한 문장(또는 한 줄)과 정규화 후 정확히 같아야 한다. 부분 단어 매칭은 없다.
   문장 경계는 줄바꿈, 마침표·물음표·느낌표(뒤에 공백이나 끝이 올 때만), 한국어 종결 어미(다·요·죠·까·줘·라·자·네) 뒤 공백이다.
   공백 제외 MIN_QUOTE_CHARS 자 이상이어야 한다.
2. 부정 방어: 인용 문장에 보관 부정 표현('보관하지', '넣지 마', "don't keep" 등)이 있으면 거부한다. 정상 요청까지 막히는
   쪽으로 틀리는 것은 허용한다(앱의 보관 버튼으로 대체 가능). 의도 추론이 아니라 명백한 반대 의사를 막는 가드다.
3. 대상 결속: 인용 문장에 대상 파일의 이름이 있거나(filename), 대상 파일이 그 발화 뒤 이 세션(또는 이 세션이 띄운
   작업 세션)의 Run 이 만든 것이어야 한다(same_run).
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import ChatSession, Run, RunOrigin

# 이 Run(포함)과 그 이전 사용자·접수 Run 중 최근 몇 개에서 인용을 찾을지.
LOOKBACK_RUNS = 20
MIN_QUOTE_CHARS = 8
# 파일명 확장자를 뺀 이름으로 맞출 때 필요한 최소 길이. 너무 짧은 이름이 아무 문장에나 맞는 것을 막는다.
MIN_STEM_CHARS = 3
DESK_HEADER = "[접수에서 넘긴 사용자 요청]"
DESK_HISTORY_MARKER = "[최근 대화 — 참고만]"
EXCERPT_CHARS = 1000

_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")
_KOREAN_END = re.compile(r"(?<=[다요죠까줘라자네])\s+")
_TRAILING = re.compile(r"[\s.!?。！？~]+$")

# 공백을 뺀 소문자 문장에서 찾는 한국어 부정 표현.
_NEGATIVE_KO = (
    "보관하지", "보관말", "보관은말", "보관안", "보관금지", "보관취소", "보관필요없", "보관할필요없", "보관하면안", "보관않",
    "넣지마", "넣지말", "넣지않", "넣으면안", "넣을필요없", "넣지는",
    "저장하지", "저장말", "저장안", "저장금지", "저장않",
    "올리지마", "올리지말", "올리지않",
)
_NEGATIVE_EN = re.compile(
    r"\b(?:don'?t|do not|dont|never|no need to|shouldn'?t|should not)\b.{0,24}\b(?:keep|save|store|archive|put|add)\b")


@dataclass(frozen=True)
class QuoteMatch:
    run: Run
    source: str
    excerpt: str


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


def is_negated(sentence: str) -> bool:
    flat = "".join(unicodedata.normalize("NFC", sentence).split()).casefold()
    if any(token in flat for token in _NEGATIVE_KO):
        return True
    return _NEGATIVE_EN.search(normalize(sentence).casefold()) is not None


def names_file(sentence: str, path: Path) -> str | None:
    """인용 문장이 대상 파일을 이름으로 가리키면 맞은 표기를 돌려준다. 파일명 전체·확장자 뺀 이름·발화에 나온 경로 끝부분 순이다."""
    text = normalize(sentence).casefold()
    name = path.name.casefold()
    stem = path.stem.casefold()
    candidates = [name]
    if path.parent.name:
        parent = path.parent.name.casefold()
        candidates += [f"{parent}/{name}", f"{parent}/{stem}"]
    if len(stem.replace(" ", "")) >= MIN_STEM_CHARS:
        candidates.append(stem)
    # 긴 표기부터 확인해 가장 구체적인 맞춤을 기록한다.
    for cand in sorted(set(candidates), key=len, reverse=True):
        if cand and cand in text:
            return cand
    return None


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


async def made_after_request(db: AsyncSession, *, session_id: uuid.UUID, match: QuoteMatch, path: Path,
                             created_by: uuid.UUID | None) -> bool:
    """대상 파일이 그 발화 뒤 이 세션(또는 이 세션이 띄운 작업 세션)의 Run 이 만든 것인지.

    기획이 작업 세션에 일을 시키면 파일은 작업 세션의 Run outbox 에 생기므로 부모 세션이 session_id 인 작업 세션도 같은 계열로 본다.
    Run 이 기록된 시각만으로는 보관 직전에 공유한 오래된 파일도 통과하므로 파일 수정 시각도 발화 뒤여야 한다."""
    if created_by is None:
        return False
    creator = await db.get(Run, created_by)
    if creator is None or creator.created_at < match.run.created_at:
        return False
    if creator.session_id != session_id:
        owner = await db.get(ChatSession, creator.session_id)
        if owner is None or owner.parent_id != session_id:
            return False
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return False
    return modified >= match.run.created_at


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
