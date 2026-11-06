#!/usr/bin/env python3
"""Render the owned Kafka HyperFrames benchmark from its checked-in spec."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ttakkari.creative.v2.hyperframes import render


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path, help="MP4 output path; must not exist")
    parser.add_argument("--workdir", required=True, type=Path, help="Dedicated empty work directory")
    args = parser.parse_args()
    spec_path = Path(__file__).resolve().parents[1] / "templates/hyperframes/kafka_message_flow/input_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    result = render(spec, output_path=args.output, workdir=args.workdir)
    args.workdir.mkdir(parents=True, exist_ok=True)
    receipt = args.workdir / "render_receipt.json"
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
