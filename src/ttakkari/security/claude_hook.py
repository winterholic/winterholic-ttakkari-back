"""Claude Code PreToolUse hook. ttakkari 가 띄운 claude 프로세스에만 --settings 로 주입된다.

bypassPermissions 에서도 hook 의 deny 는 적용된다.
가드 훅 실행 중 오류나 비정상 입력이 발생하면 안전을 위해 차단(fail-closed)한다.
"""

from __future__ import annotations

import json
import os
import sys
import time

from ttakkari.security.guard import GuardConfig, evaluate

MARKER = "[ttakkari-guard]"


def _log(entry: dict) -> None:
    path = os.environ.get("TTAKKARI_GUARD_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        tool = data.get("tool_name", "")
        reason = evaluate(tool, data.get("tool_input") or {}, data.get("cwd"), GuardConfig.from_env())
    except Exception as e:  # noqa: BLE001
        _log({"at": time.time(), "run_id": os.environ.get("TTAKKARI_RUN_ID"), "error": repr(e)})
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{MARKER} 가드 훅 실행 중 오류가 발생하여 안전을 위해 차단합니다: {type(e).__name__}",
            }
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        return 0
    if reason is None:
        return 0
    _log({"at": time.time(), "run_id": os.environ.get("TTAKKARI_RUN_ID"), "tool": tool,
          "input": data.get("tool_input"), "reason": reason})
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{MARKER} {reason}. 이 작업은 원격 자동 실행에서 금지되어 있다. 다른 방법을 찾거나 사용자에게 직접 실행을 요청하라.",
        }
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
