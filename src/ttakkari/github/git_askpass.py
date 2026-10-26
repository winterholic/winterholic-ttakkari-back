#!/usr/bin/env python3
"""One-operation Git askpass: only answers for github.com, with token kept out of argv/output logs."""
from __future__ import annotations

import os
import re
import sys


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    lowered = prompt.lower()
    if re.search(r"https://(?:[^/@:\s]+@)?github\.com(?::443)?(?:/|'|:|\s|$)", lowered) is None:
        return 1
    if "username" in lowered:
        print("x-access-token")
        return 0
    if "password" in lowered:
        token = os.environ.get("TTAKKARI_GITHUB_TOKEN", "")
        if token:
            print(token)
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
