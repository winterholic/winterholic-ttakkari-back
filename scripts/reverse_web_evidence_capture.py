#!/usr/bin/env python3
"""Validate and store a bounded configured-browser observation pair."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ttakkari.broker.policy import ensure_regular_file, resolve_in_root
from ttakkari.reverse_web_evidence import (
    MAX_CAPTURE_BYTES,
    CaptureError,
    create_web_capture,
)


def _read_json(root: Path, rel: str) -> dict:
    path = resolve_in_root(root, rel, must_exist=True).path
    ensure_regular_file(path)
    blob = path.read_bytes()
    if len(blob) > MAX_CAPTURE_BYTES:
        raise CaptureError("input JSON exceeds 1 MiB")
    value = json.loads(blob)
    if not isinstance(value, dict):
        raise CaptureError("input JSON must be an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--observations", required=True, help="bounded JSON file relative to --root")
    args = parser.parse_args()
    try:
        spec = _read_json(args.root, args.observations)
        allowed = {"comparison_id", "behavior", "input", "original", "replica", "run_id",
                   "expected_viewport", "origin_map", "environment", "pre_state", "verified_at"}
        if set(spec) - allowed:
            raise CaptureError("unexpected input keys: " + ", ".join(sorted(set(spec) - allowed)))
        record = create_web_capture(
            args.root,
            comparison_id=spec["comparison_id"],
            behavior=spec["behavior"],
            input_data=spec["input"],
            original=spec.get("original"),
            replica=spec.get("replica"),
            run_id=spec["run_id"],
            expected_viewport=spec["expected_viewport"],
            origin_map=spec.get("origin_map"),
            environment=spec.get("environment"),
            pre_state=spec.get("pre_state"),
            verified_at=spec.get("verified_at"),
        )
        report_rel = f".reverse/comparisons/{record['id']}-{record['run_id']}.web.json"
        report_path = resolve_in_root(args.root, report_rel, must_exist=False).path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"report": report_rel, "record": record}, ensure_ascii=False))
        return 0
    except (CaptureError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
