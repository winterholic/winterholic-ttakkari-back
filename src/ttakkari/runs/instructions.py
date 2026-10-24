"""비서 지침을 역할별로 나눈다(ADR 0013). 비서 관리 > 지침에서 사용자가 고치고, 에이전트는 못 고친다(~/.ttakkari 안).

common 은 접수를 뺀 모든 실행에, lead·worker 는 그 역할에만 붙는다. 접수(desk)는 전역 하네스도 공통 지침도 받지 않고
자기 지침만 받는다. 짧게 빨리 고르는 일이라 긴 지침은 지연과 오분류만 늘린다."""

from __future__ import annotations

from pathlib import Path
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
- 사용자가 새 세션·새로 시작을 말하거나, [작업판]의 어느 일과도 다른 일이면 lead_id 를 고르지 말고 new_session 을 true 로 둔다. 제목이 비슷해도 다른 일이면 null 이다.
- 최신 사용자 메시지에 명시된 대상 프로젝트와 관심사를 우선한다. 최근 대화는 지시어 해석용이며 대상을 바꾸지 못한다. 같은 프로젝트라도 다른 관심사·목표는 새 기획이다.
- 배포·테스트 같은 공통 단어만 같아서는 이어 붙이지 않는다. AIP 대화 뒤 따까리 배포 분석 요청은 따까리의 새 일이다. 사용자가 잘못 넘겼다고 지적한 세션을 다시 고르지 않는다.
- reply 에는 무엇을 넘겼는지 한 문장만 쓴다.

출력은 JSON 한 줄만 쓴다. 다른 말은 쓰지 않는다.
{"action":"answer","reply":"..."}
{"action":"delegate","reply":"...","lead_id":null,"title":"...","new_session":false}

말투는 사용자를 따라 짧게, 한국어로."""

DESK_ROUTING_CONTRACT = """[기존 기획 재사용 계약]
최신 사용자 메시지의 대상 프로젝트와 관심사를 기준으로 고른다. 최근 대화는 지시어 해석용이다.
기존 lead_id를 고를 때는 JSON에 same_project와 same_task를 각각 yes/no로 포함한다.
같은 프로젝트이면서 같은 작업의 후속일 때만 둘 다 yes다. 하나라도 불명확하거나 다르면 lead_id=null, new_session=true다.
배포·테스트 같은 공통 단어만 같아서는 같은 일이 아니다. 잘못 연결했다고 지적받은 세션을 다시 고르지 않는다.
이 두 필드가 없거나 yes가 아니면 하네스가 기존 세션 재사용을 거부한다."""

DEFAULTS: dict[Role, str] = {"desk": DESK_DEFAULT}


def access_note(outbox: Path) -> str:
    """Expose path metadata, never protected contents or additional permissions."""
    s = get_settings()
    return f"""[Ttakkari filesystem access — plan before using file tools]
- `{s.home}` is protected by default, including harness files, logs, accounts, backups and other Runs' outboxes.
  A workspace permission folder or a path returned by MCP does not grant access through every guard/sandbox.
  Use this Run's outbox `{outbox}`; transfer locations are `{s.uploads_root}` and `{s.inbox_root}` only when
  included in this Run's writable roots. A resumed conversation gets a new Run outbox.
- Logs/deployments: use `ops_list_issues`, `ops_search_logs`, `ops_deploy_log`, `ops_list_deploys` and
  `backend_deploy_status`; do not scan protected log files. Quota/capabilities: `account_usage` / `execution_capabilities`.
  Other work: `read_session` / `get_worker`; `list_artifacts` provides metadata, not file-read permission.
  Keep reviewable evidence in an authorized repository; if another Run's original is inaccessible, label it unverified.
- Standing/role instructions are already injected below. Change the repository's harness source when authorized;
  do not read or edit protected harness/config.env/account files to rediscover instructions or settings.
  A deployed release's read-only runtime exception does not allow installation, cache writes or service restarts there.
- After a permission denial, do not retry the same target via another command, interpreter, symlink or engine.
  Use the supported tool or authorized source instead. If none exists, report the exact blocked step once,
  continue independent work, and leave that check unverified; do not infer host-wide failure."""


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
