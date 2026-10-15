"""Read-only macOS IPC policy checks. Never launches a browser or changes policy.

Execute this file *inside the command sandbox* that would launch the browser.
Running it in the backend process would inspect the wrong permissions.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys


def inspect_permissions() -> dict:
    result: dict = {"status": "unknown", "browser_launched": False, "checks": []}
    if sys.platform != "darwin":
        result["status"] = "not_applicable"
        return result
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        check = lib.sandbox_check
        check.restype = ctypes.c_int
        # Fixed argument types are needed for ARM64 variadic calls.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        no_report = ctypes.c_int.in_dll(lib, "SANDBOX_CHECK_NO_REPORT").value
        pid = os.getpid()
        for operation, name in (
            ("mach-register", f"org.chromium.Chromium.MachPortRendezvousServer.{pid}"),
            ("mach-lookup", "com.apple.coreservices.launchservicesd"),
            ("mach-lookup", "com.apple.coreservices.coreservicesd"),
        ):
            ctypes.set_errno(0)
            code = check(pid, operation.encode(), 2 | no_report, ctypes.c_char_p(name.encode()))
            result["checks"].append({"operation": operation, "name": name,
                                     "result": {0: "allowed", 1: "denied"}.get(code, "query_error"),
                                     "errno": ctypes.get_errno()})
    except (OSError, AttributeError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    states = {row["result"] for row in result["checks"]}
    if "denied" in states:
        result["status"] = "blocked"
    elif "query_error" not in states:
        result["status"] = "permissions_allowed"
    # Allowed IPC is a prerequisite, not proof that browser launch will succeed.
    return result


if __name__ == "__main__":
    report = inspect_permissions()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["status"] in ("permissions_allowed", "not_applicable") else 78)
