#!/usr/bin/env python3
"""codex exec --json 을 흉내내는 가짜 엔진. 테스트 전용.

호출 형태: codex exec [resume] <opts...> [thread_id] -- <prompt>
"""
import json
import os
import re
import sys
import time
import uuid


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def desk_reply(prompt, engine):
    """접수 호출(ADR 0013)의 가짜 답. 사용자 메시지 부분의 키워드로 고른다(작업판·대화에 같은 말이 있어도 무시)."""
    msg = prompt.split("[사용자 메시지]")[-1]
    if "DESK_SLOW" in msg:
        time.sleep(30)
    if "DESK_GARBAGE" in msg:
        return "not json at all"
    if "DESK_CONTINUE" in msg:
        m = re.search(r"lead_id=([0-9a-f-]{36})", prompt)
        return json.dumps({"action": "delegate", "reply": "이어서 넘겼어요", "lead_id": m.group(1) if m else None})
    m = re.search(r"DESK_TITLE:(\S+)", msg)
    if m:
        # 제목을 정해 새 기획으로 넘긴다. 밑줄은 공백으로 바꾼다(중복 기획 판단 테스트).
        return json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": None,
                           "title": m.group(1).replace("_", " ")}, ensure_ascii=False)
    if "DESK_DELEGATE" in msg:
        return json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": None, "title": "desk title"})
    return json.dumps({"action": "answer", "reply": f"desk answer by {engine}"})


def log_call(record):
    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"engine": "codex", "pid": os.getpid(), "cwd": os.getcwd(),
                                "ttk_env": sorted(k for k in os.environ if k.startswith("TTAKKARI_")),
                                "account_dir": os.environ.get("CODEX_HOME"), **record}) + "\n")


def app_server(argv):
    """codex app-server(stdio JSON-RPC)의 접수용 부분만 흉내낸다(desk_pool). 스레드 하나, 턴 하나면 충분하다."""
    log_call({"argv": argv, "mode": "app-server-spawn"})
    thread = {}
    for line in sys.stdin:
        msg = json.loads(line)
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "initialize":
            emit({"id": rid, "result": {"userAgent": "fake", "codexHome": os.environ.get("CODEX_HOME")}})
        elif method == "thread/start":
            thread = params | {"id": str(uuid.uuid4())}
            emit({"id": rid, "result": {"thread": {"id": thread["id"]}, "model": params.get("model")}})
            emit({"method": "thread/started", "params": {"thread": {"id": thread["id"]}}})
        elif method == "turn/start":
            prompt = params["input"][0]["text"]
            msg_part = prompt.split("[사용자 메시지]")[-1]
            log_call({"argv": argv, "mode": "app-server-turn", "thread": thread, "prompt": prompt})
            if "DESK_DIE" in msg_part:
                return 1
            turn = {"id": str(uuid.uuid4()), "status": "inProgress", "error": None}
            emit({"id": rid, "result": {"turn": turn}})
            emit({"method": "turn/started", "params": {"threadId": thread["id"], "turn": turn}})
            if "DESK_CODEX_LIMIT" in msg_part:
                err = {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
                emit({"method": "error", "params": {"error": err, "willRetry": False}})
                emit({"method": "turn/completed", "params": {"turn": turn | {"status": "failed", "error": err}}})
                continue
            text = desk_reply(prompt, "codex")
            emit({"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "m0", "text": text}}})
            emit({"method": "turn/completed", "params": {"turn": turn | {"status": "completed"}}})
    return 0


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "app-server":
        return app_server(argv)
    dash = argv.index("--")
    prompt = " ".join(argv[dash + 1:])
    resume = len(argv) > 1 and argv[1] == "resume"
    thread = argv[dash - 1] if resume else str(uuid.uuid4())

    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"engine": "codex", "argv": argv, "pid": os.getpid(), "cwd": os.getcwd(),
                                "ttk_env": sorted(k for k in os.environ if k.startswith("TTAKKARI_")),
                                "account_dir": os.environ.get("CODEX_HOME")}) + "\n")

    emit({"type": "thread.started", "thread_id": thread})
    if "--ephemeral" in argv:
        if "DESK_CODEX_LIMIT" in prompt.split("[사용자 메시지]")[-1]:
            emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
            return 1
        emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": desk_reply(prompt, "codex")}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
        return 0
    if "SLEEP" in prompt:
        time.sleep(60)
        return 0
    lock = (log or "") + ".lock"
    if "LIMIT_LOCK" in prompt and log and os.path.exists(lock):
        with open(lock) as f:
            reset = f.read().strip()
        emit({"type": "turn.failed", "error": {"message": f"You've hit your usage limit. limit reached|{reset}"}})
        return 1
    if "CODEX_LIMIT" in prompt:
        emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        return 1
    if "FAIL" in prompt:
        emit({"type": "turn.failed", "error": {"message": "fake codex failure"}})
        return 1
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "codex done"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
