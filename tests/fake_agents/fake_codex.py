#!/usr/bin/env python3
"""codex exec --json 을 흉내내는 가짜 엔진. 테스트 전용.

호출 형태: codex exec [resume] <opts...> [thread_id] -- <prompt>
"""
import json
import os
import sys
import time
import uuid


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    argv = sys.argv[1:]
    dash = argv.index("--")
    prompt = " ".join(argv[dash + 1:])
    resume = len(argv) > 1 and argv[1] == "resume"
    thread = argv[dash - 1] if resume else str(uuid.uuid4())

    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"engine": "codex", "argv": argv, "pid": os.getpid(), "cwd": os.getcwd(),
                                "ttk_env": sorted(k for k in os.environ if k.startswith("TTAKKARI_"))}) + "\n")

    emit({"type": "thread.started", "thread_id": thread})
    if "SLEEP" in prompt:
        time.sleep(60)
        return 0
    if "FAIL" in prompt:
        emit({"type": "turn.failed", "error": {"message": "fake codex failure"}})
        return 1
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "codex done"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
