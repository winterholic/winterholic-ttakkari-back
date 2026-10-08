#!/usr/bin/env python3
"""claude -p ... --output-format stream-json 을 흉내내는 가짜 엔진. 테스트 전용.

프롬프트 키워드로 시나리오를 고른다: SLEEP, FAIL, GARBAGE, SENSITIVE, DOCX, MODIFY.
받은 argv 는 $FAKE_AGENT_LOG(JSONL) 에 기록한다. (매니저가 TTAKKARI_* env 를 지우므로 접두사를 달리 썼다.)
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


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
                                "ttk_env": sorted(k for k in os.environ if k.startswith("TTAKKARI_"))}) + "\n")

    emit({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-haiku-4-5", "cwd": os.getcwd()})

    if "SLEEP" in prompt:
        time.sleep(60)
        return 0
    if "FAIL" in prompt:
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "fake failure",
              "session_id": sid, "total_cost_usd": 0.0, "num_turns": 1, "duration_ms": 5})
        return 1
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
    if "MODIFY" in prompt:
        readme = Path(os.getcwd()) / "README.md"
        write_tool(readme, readme.read_text() + "modified by fake agent\n")
        write_tool(Path(os.getcwd()) / "new_file.txt", "brand new\n")

    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
          "total_cost_usd": 0.01, "num_turns": 2, "duration_ms": 100})
    return 0


if __name__ == "__main__":
    sys.exit(main())
