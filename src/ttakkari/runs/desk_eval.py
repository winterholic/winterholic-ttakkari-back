"""접수 재생 평가(`ttakkari desk-eval`). 사용자 판정 모음(eval-set)을 지금의 접수 지침·모델로 다시 분류해 본다.

- 엔진 호출·지침·프롬프트·파싱은 운영 접수(runs/desk)의 함수를 그대로 쓴다. 평가만 따로 맞춘 경로가 생기면 숫자가
  운영과 어긋난다.
- 작업판·최근 대화는 비운다. 판정 시점의 작업판은 남기지 않았고, "그거 이어서" 같은 메시지는 맥락 없이 분류가 달라질
  수 있다(평가 한계).
- 계정은 운영처럼 엔진 순서 → 우선순위로 고르고, 한도·시간 초과·출력 오류면 다음 계정으로 넘어간다. 다만 한도 걸린
  계정을 쉬게 하지는 않는다(평가가 운영 계정 상태를 바꾸지 않게).
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import ChatSession, DeskFeedback, Run
from ttakkari.runs import desk
from ttakkari.runs import instructions as instructions_mod
from ttakkari.runs import profiles as profiles_mod


@dataclass
class Case:
    message: str
    expected: str
    got: str | None  # None: 모든 계정이 실패했거나 출력 형식 오류
    account: str | None
    ms: int
    error: str | None = None


@dataclass
class Report:
    cases: list[Case] = field(default_factory=list)

    @property
    def correct(self) -> int:
        return sum(c.got == c.expected for c in self.cases)

    @property
    def wrong(self) -> list[Case]:
        return [c for c in self.cases if c.got != c.expected]

    @property
    def median_ms(self) -> float | None:
        return statistics.median(c.ms for c in self.cases) if self.cases else None


async def _prompt(db: AsyncSession, message: str) -> str:
    # DB 에 넣지 않은 빈 세션·Run 을 넘겨 작업판·최근 대화가 비게 한다.
    run = Run(id=uuid.uuid4(), prompt=message)
    sess = ChatSession(id=uuid.uuid4())
    return await desk._prompt(db, run, sess, [])


async def eval_set(db: AsyncSession, limit: int | None = None) -> list[tuple[str, str]]:
    """(message, expected) 를 최근 판정부터. '맞아'도 문항이 된다(접수가 맞힌 것도 계속 맞혀야 한다)."""
    q = select(DeskFeedback).order_by(DeskFeedback.updated_at.desc())
    if limit:
        q = q.limit(limit)
    return [(fb.message, fb.expected) for fb in await db.scalars(q)]


async def run_eval(db: AsyncSession, limit: int | None = None, engine: str | None = None,
                   pool: str = "personal") -> Report:
    if engine == "qwen":
        return await _run_eval_qwen(db, limit)
    s = get_settings()
    engines = [engine] if engine else list(s.desk_engine_order)
    order = {e: i for i, e in enumerate(engines)}
    profiles, _blocked = await profiles_mod.availability(db, "auto", pool)
    accounts = sorted((p for p in profiles if p.engine in order), key=lambda p: order[p.engine])
    if not accounts:
        raise RuntimeError(f"쓸 수 있는 접수 계정이 없습니다(엔진: {', '.join(engines)}).")
    system = instructions_mod.read("desk")

    report = Report()
    for message, expected in await eval_set(db, limit):
        prompt = await _prompt(db, message)
        case = Case(message, expected, None, None, 0)
        for prof in accounts[:desk.MAX_ATTEMPTS]:
            res = await desk._ask(prof, system, prompt)
            case.ms += res.elapsed_ms
            decision = desk._parse(res.text) if res.text else None
            if decision is not None:
                case.got, case.account, case.error = decision.action, prof.name, None
                break
            case.error = res.error or "출력 형식 오류"
        report.cases.append(case)
    return report


async def _run_eval_qwen(db: AsyncSession, limit: int | None) -> Report:
    """로컬 Qwen 하네스로 같은 문항을 돌린다. 작업판·최근 대화는 비운 채(CLI 평가와 같은 조건)."""
    from ttakkari.runs import desk_qwen

    report = Report()
    for message, expected in await eval_set(db, limit):
        case = Case(message, expected, None, None, 0)
        try:
            d = await desk_qwen.decide([], [], message, [])
            case.got, case.account, case.error, case.ms = d.action, "qwen-local", None, d.ms
        except desk_qwen.QwenUnavailable as exc:
            case.error = exc.reason
        report.cases.append(case)
    return report


def format_report(report: Report) -> str:
    n = len(report.cases)
    if not n:
        return "평가 문항이 없습니다. 접수 대화에서 판정을 먼저 남겨 주세요."
    med = report.median_ms
    lines = [f"정확도 {report.correct}/{n} ({report.correct / n:.0%}) · 지연 중앙값 {med / 1000:.1f}초"]
    if report.wrong:
        lines.append("오답:")
        for c in report.wrong:
            got = c.got or f"실패({(c.error or '')[:80]})"
            msg = " ".join(c.message.split())[:80]
            lines.append(f"- 기대 {c.expected} / 결과 {got} · {c.account or '-'} · {c.ms / 1000:.1f}초 · {msg}")
    return "\n".join(lines)
