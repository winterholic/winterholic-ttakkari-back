"""비서 지침을 역할별로 나눈다(ADR 0013). 비서 관리 > 지침에서 사용자가 고치고, 에이전트는 못 고친다(~/.ttakkari 안).

common 은 접수를 뺀 모든 실행에, lead·worker 는 그 역할에만 붙는다. 접수(desk)는 전역 하네스도 공통 지침도 받지 않고
자기 지침만 받는다. 짧게 빨리 고르는 일이라 긴 지침은 지연과 오분류만 늘린다."""

from __future__ import annotations

from typing import Literal

from ttakkari.config import get_settings

Role = Literal["common", "desk", "lead", "worker"]
FILES: dict[Role, str] = {
    "common": "instructions.md",
    "desk": "desk.md",
    "lead": "lead.md",
    "worker": "worker.md",
}

DESK_DEFAULT = """\
너는 따까리의 접수 담당이다. 사용자는 주로 휴대폰에서 짧게 말을 건다.
아래 [작업판]과 [최근 대화]만 보고, 바로 답할지(answer) 기획 세션에 넘길지(delegate) 정한다.

바로 답한다(answer):
- 인사, 감사, 잡담.
- [작업판]이나 [최근 대화]에 이미 있는 내용으로 답할 수 있는 진행 상황·결과 질문.

넘긴다(delegate):
- 코드·파일 작업, 조사, 설계, 의견, 설명, 일정·메모 조회나 변경처럼 생각하거나 도구를 써야 하는 모든 일.
- 애매하면 넘긴다. 모르는 사실은 지어내지 않는다.

넘길 때:
- [작업판]에 같은 일을 맡은 기획 세션이 있으면 그 lead_id 를 고른다. 새 일이면 lead_id 는 null 로 두고 title 에 짧은 제목을 쓴다.
- reply 에는 무엇을 넘겼는지 한 문장만 쓴다.

출력은 JSON 한 줄만 쓴다. 다른 말은 쓰지 않는다.
{"action":"answer","reply":"..."}
{"action":"delegate","reply":"...","lead_id":null,"title":"..."}

말투는 사용자를 따라 짧게, 한국어로."""

DEFAULTS: dict[Role, str] = {"desk": DESK_DEFAULT}


def read(role: Role) -> str:
    """저장된 지침. 파일이 없으면 기본값(접수만 있다)."""
    f = get_settings().harness_dir / FILES[role]
    try:
        text = f.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULTS.get(role, "")
    return text[: get_settings().instructions_max_chars]


def is_default(role: Role) -> bool:
    return not (get_settings().harness_dir / FILES[role]).exists()


def write(role: Role, text: str) -> None:
    d = get_settings().harness_dir
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / FILES[role]).write_text(text, encoding="utf-8")


def reset(role: Role) -> None:
    """기본값이 있는 역할을 기본값으로 되돌린다(파일을 지운다)."""
    (get_settings().harness_dir / FILES[role]).unlink(missing_ok=True)
