#!/usr/bin/env python3
"""claude -p ... --output-format stream-json 을 흉내내는 가짜 엔진. 테스트 전용.

프롬프트 키워드로 시나리오를 고른다: SLEEP, HOLD<n>(n초 지연 후 성공), FAIL, GARBAGE, SENSITIVE, DOCX, MODIFY, CTX<n>, ART_OUT(ArtCraft out/ 에 결과 저장).
받은 argv 는 $FAKE_AGENT_LOG(JSONL) 에 기록한다. (매니저가 TTAKKARI_* env 를 지우므로 접두사를 달리 썼다.)
"""
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def _redacted_servers(cfg):
    """서버 명세에서 env 값만 키 목록으로 바꾼다. 값(토큰)은 절대 기록하지 않는다."""
    out = {}
    for name, srv in (cfg.get("mcpServers") or {}).items():
        redacted = {k: v for k, v in srv.items() if k != "env"}
        redacted["env"] = sorted((srv.get("env") or {}).keys())
        out[name] = redacted
    return out


def mcp_config_meta(argv):
    """--mcp-config 전달 형태를 비밀값 없이 기록한다(SEC2-AGT-01 회귀용)."""
    value = opt(argv, "--mcp-config")
    if value is None:
        return {"mode": "absent"}
    if os.path.exists(value):
        try:
            with open(value, encoding="utf-8") as f:
                cfg = json.load(f)
            servers = _redacted_servers(cfg)
            return {"mode": "file", "exists": True,
                    "file_mode": oct(os.stat(value).st_mode & 0o777),
                    "servers": {name: spec["env"] for name, spec in servers.items()},
                    "details": servers}
        except (ValueError, OSError):
            return {"mode": "file", "exists": True, "unparsable": True}
    try:
        cfg = json.loads(value)
        servers = _redacted_servers(cfg)
        return {"mode": "inline",
                "servers": {name: spec["env"] for name, spec in servers.items()},
                "details": servers}
    except ValueError:
        return {"mode": "unknown"}


def desk_reply(prompt, engine):
    """접수 호출(ADR 0013)의 가짜 답. 사용자 메시지 부분의 키워드로 고른다(작업판·대화에 같은 말이 있어도 무시)."""
    msg = prompt.split("[사용자 메시지]")[-1]
    if "DESK_SLOW" in msg:
        time.sleep(30)
    if "DESK_GARBAGE" in msg:
        return "not json at all"
    if "DESK_CONTINUE" in msg:
        m = re.search(r"lead_id=([0-9a-f-]{36})", prompt)
        return json.dumps({"action": "delegate", "reply": "이어서 넘겼어요", "lead_id": m.group(1) if m else None,
                           "same_project": "yes", "same_task": "yes"})
    if "DESK_NEWSESSION" in msg:
        # 작업판 첫 기획을 고르면서도 새 세션을 명시한다. 하네스가 기존 선택을 무시하는지 본다.
        m = re.search(r"lead_id=([0-9a-f-]{36})", prompt)
        t = re.search(r"DESK_TITLE:(\S+)", msg)
        return json.dumps({"action": "delegate", "reply": "새로 넘겼어요", "lead_id": m.group(1) if m else None,
                           "title": t.group(1).replace("_", " ") if t else "새 일", "new_session": True},
                          ensure_ascii=False)
    m = re.search(r"DESK_TITLE:(\S+)", msg)
    if m:
        # 제목을 정해 새 기획으로 넘긴다. 밑줄은 공백으로 바꾼다(중복 기획 판단 테스트).
        return json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": None,
                           "title": m.group(1).replace("_", " ")}, ensure_ascii=False)
    if "DESK_DELEGATE" in msg:
        return json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": None, "title": "desk title"})
    return json.dumps({"action": "answer", "reply": f"desk answer by {engine}"})


def desk_stream(argv, log, sid):
    """접수 상주 모드(desk_pool): stdin 의 stream-json 사용자 메시지마다 결과 한 줄. 띄울 때는 아무것도 쓰지 않는다."""
    for line in sys.stdin:
        msg = json.loads(line)
        prompt = msg["message"]["content"]
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"engine": "claude", "argv": argv, "pid": os.getpid(), "mode": "stream-turn",
                                    "prompt": prompt, "account_dir": os.environ.get("CLAUDE_CONFIG_DIR")}) + "\n")
        if "DESK_DIE" in prompt.split("[사용자 메시지]")[-1]:
            return 1
        emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-haiku-4-5"})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": desk_reply(prompt, "claude"),
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
    return 0


def meeting_reply(prompt):
    # 회의록 분석(ADR 0010). 프롬프트에 후보로 준 [M1]·[A1] 참조를 읽어 충돌을 지목하고, 없는 참조(M99)도 일부러 섞는다.
    refs = re.findall(r"^\[([MA]\d+)\] ", prompt, re.MULTILINE)
    m_ref = next((r for r in refs if r.startswith("M")), None)
    a_ref = next((r for r in refs if r.startswith("A")), None)
    analysis = {
        "summary": "릴리스 일정과 모바일 우선 전략을 논의했다.",
        "participants": ["민수", "지연"],
        "discussions": [{"topic": "릴리스 일정", "summary": "금요일 대신 목요일 배포 논의", "evidence": "목요일에 내자"}],
        "decisions": [
            {"text": "릴리스는 목요일로 앞당긴다", "rationale": "고객 일정", "evidence": "목요일에 내자",
             "conflicts": ([{"ref": m_ref, "reason": "기존 결정은 금요일 릴리스", "change": "supersede"}] if m_ref else [])
             + [{"ref": "M99", "reason": "없는 참조"}]
             + ([{"ref": a_ref, "reason": "금요일 배포 준비 액션이 필요 없어짐", "change": "cancel"}] if a_ref else [])},
            {"text": "모바일 화면을 먼저 만든다", "rationale": None, "evidence": None, "conflicts": []},
        ],
        "action_items": [{"text": "배포 체크리스트를 작성한다", "owner": "민수", "due": "수요일", "evidence": None, "conflicts": []}],
        "open_issues": [{"text": "데이터 이전 시점이 정해지지 않았다", "detail": "지연은 다음 주를 선호", "evidence": None}],
    }
    return "{ not json" if "FAKE_MEETING_BADJSON" in prompt else "분석했습니다.\n```json\n" + json.dumps(analysis, ensure_ascii=False) + "\n```"


def main():
    argv = sys.argv[1:]
    prompt = opt(argv, "-p") or ""
    outbox = opt(argv, "--add-dir")
    resume = opt(argv, "--resume")
    sid = resume or str(uuid.uuid4())

    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"engine": "claude", "argv": argv, "pid": os.getpid(), "cwd": os.getcwd(),
                                "ttk_env": sorted(k for k in os.environ if k.startswith("TTAKKARI_")),
                                "guard_protected": os.environ.get("TTAKKARI_GUARD_PROTECTED"),
                                "guard_write_roots": os.environ.get("TTAKKARI_GUARD_WRITE_ROOTS"),
                                "account_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                                "mcp_config": mcp_config_meta(argv)}) + "\n")

    if "--system-prompt" in argv and opt(argv, "--input-format") == "stream-json":
        return desk_stream(argv, log, sid)
    if "--system-prompt" in argv:
        emit({"type": "result", "subtype": "success", "is_error": False, "result": desk_reply(prompt, "claude"),
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 0
    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-haiku-4-5", "cwd": os.getcwd()})
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": 0.42, "resetsAt": 1791732000},
        "seven_day": {"utilization": 0.1, "resetsAt": 1792000000},
        "seven_day_overage_included": {"utilization": 0, "resetsAt": 1792000000}}}})

    if hold := re.search(r"HOLD(\d+)", prompt):
        time.sleep(int(hold.group(1)))  # 지연 뒤 일반 성공 경로로 이어진다(실행부 교체 중 running 구간 확보)
    if "SLEEP" in prompt:
        time.sleep(60)
        return 0
    lock = (log or "") + ".lock"
    if "LIMIT_LOCK" in prompt and log and os.path.exists(lock):
        # 락 파일이 있는 동안 한도. 내용은 초기화 시각(unix 초)이다. 파일을 지우면 한도가 풀린다.
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "progress marker alpha"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True,
              "result": f"Claude AI usage limit reached|{Path(lock).read_text().strip()}",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
    if "STRUCT_LIMIT" in prompt and not os.environ.get("CLAUDE_CONFIG_DIR"):
        # 문구는 한도로 읽히지 않지만 구조화된 이벤트가 한도라고 알려 주는 경우.
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": int(time.time()) + 3600}})
        emit({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "Please come back later"}]}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Please come back later",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
    if "CLAUDE_SESSION_LIMIT" in prompt and not os.environ.get("CLAUDE_CONFIG_DIR"):
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Existing worker was already created"}]}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "You've hit your session limit \u00b7 resets 7:20pm (Asia/Seoul)",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
    if "CLAUDE_LIMIT" in prompt:
        emit({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1760000000",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
    if "FAIL" in prompt:
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "fake failure",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
    if "FAKE_MEETING" in prompt:
        body = meeting_reply(prompt)
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": body}]}})
        emit({"type": "result", "subtype": "success", "is_error": False, "result": body, "session_id": sid,
              "total_cost_usd": 0.01, "num_turns": 1, "duration_ms": 100})
        return 0
    if "NULBYTE" in prompt:
        # 도구 출력에 NUL(\u0000)이 섞인 경우. Postgres text/jsonb 는 NUL 을 받지 않는다.
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_nul", "name": "Bash", "input": {"command": "cat bin\u0000file"}}]}})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_nul", "content": "head\u0000tail", "is_error": False}]}})
        print("raw\u0000stdout line", flush=True)
        emit({"type": "result", "subtype": "success", "is_error": False, "result": "done\u0000ok", "session_id": sid,
              "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 0
    if "GARBAGE" in prompt:
        print("this line is not json", flush=True)
        print("{broken json", flush=True)

    n = 0

    def write_tool(path, content):
        nonlocal n
        n += 1
        tid = f"toolu_{n}"
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content) if isinstance(content, str) else Path(path).write_bytes(content)
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Write",
             "input": {"file_path": str(path), "content": content if isinstance(content, str) else "<bytes>"}}]}})
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "File created successfully", "is_error": False}]}})

    if outbox:
        write_tool(Path(outbox) / "report.md", "# report\n")
        if "SENSITIVE" in prompt:
            write_tool(Path(outbox) / ".env", "SECRET=1\n")
        if "DOCX" in prompt:
            write_tool(Path(outbox) / "a.docx", b"not really a docx")
    if "ART_OUT" in prompt:
        # ArtCraft 작업(ADR 0039): 안내받은 "최종 결과 저장 위치" 에 결과를 쓴다. 라운드 번호는 머리말에서 읽는다.
        out = re.search(r"최종 결과 저장 위치: (\S+)", prompt)
        rnd = re.search(r"라운드 (\d+)", prompt)
        if out:
            write_tool(Path(out.group(1)) / f"art-result-{rnd.group(1) if rnd else 0}.txt", "art result\n")
    if "MODIFY" in prompt:
        readme = Path(os.getcwd()) / "README.md"
        write_tool(readme, readme.read_text() + "modified by fake agent\n")
        write_tool(Path(os.getcwd()) / "new_file.txt", "brand new\n")

    # CTX<n> 이면 컨텍스트 창(200k)의 n% 를 쓴 것처럼 마지막 요청 토큰을 알린다. 없으면 10%.
    m = re.search(r"CTX(\d+)", prompt)
    used = int(m.group(1) if m else 10) * 2000
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": used - 1015, "output_tokens": 5}
    emit({"type": "assistant", "message": {"model": "claude-haiku-4-5", "usage": usage,
                                           "content": [{"type": "text", "text": "done"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
          "total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 100,
          "modelUsage": {"claude-haiku-4-5": {"contextWindow": 200000}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
