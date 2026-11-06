#!/usr/bin/env python3
"""Persist real configured-browser observations as a contract-v2 web comparison."""
import argparse
import json
from pathlib import Path

from ttakkari.reverse_compare import run_web_comparison

parser = argparse.ArgumentParser()
parser.add_argument("--workspace", type=Path, required=True)
parser.add_argument("--original", type=Path, required=True, help="JSON from the original browser observation")
parser.add_argument("--replica", type=Path, required=True, help="JSON from the replica browser observation")
parser.add_argument("--screenshots", type=Path, required=True, help="JSON mapping original/replica to screenshot paths")
parser.add_argument("--visual-assessment", type=Path, help="Separate screenshot comparison assessment JSON")
parser.add_argument("--run-id", default="web-browser-20261107")
args = parser.parse_args()
load = lambda p: json.loads(p.read_text(encoding="utf-8"))
result = run_web_comparison(args.workspace, comparison_id="web-button-state", behavior="실행 버튼 상태 반영",
    input_data={"event_vector": [{"type": "click", "selector": "#run"}], "viewport": "configured browser default"},
    original_observation=load(args.original), replica_observation=load(args.replica), run_id=args.run_id,
    screenshots=load(args.screenshots),
    visual_assessment=load(args.visual_assessment) if args.visual_assessment else None,
    environment={"browser": "configured ttakkari_browser", "capture_source": "browser tools"})
print(json.dumps(result, ensure_ascii=False))
