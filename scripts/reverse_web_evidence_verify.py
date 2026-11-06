#!/usr/bin/env python3
"""Recompute web evidence from its hashed raw capture and image files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ttakkari.broker.policy import ensure_regular_file, resolve_in_root
from ttakkari.reverse_web_evidence import MAX_CAPTURE_BYTES, verify_web_capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--comparison", required=True, help="comparison JSON relative to --root")
    parser.add_argument("--expected-run-id")
    args = parser.parse_args()
    try:
        path = resolve_in_root(args.root, args.comparison, must_exist=True).path
        ensure_regular_file(path)
        blob = path.read_bytes()
        if len(blob) > MAX_CAPTURE_BYTES:
            raise ValueError("comparison JSON exceeds 1 MiB")
        record = json.loads(blob)
        result = verify_web_capture(args.root, record, expected_run_id=args.expected_run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("result") != "unverified" else 1
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"result": "unverified", "difference": {"reason": str(exc)}},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
