"""Offline KB-E0 CLI. Exit 0 means fixture execution only, never quality approval."""

import argparse
import json
from pathlib import Path

from .comparison import compare_runs
from .loader import load_bundle
from .runner import run
from .schema import Bundle
from .state import atomic_json, review_update


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("bundle", type=Path)
    runner.add_argument("--out", type=Path, required=True)
    runner.add_argument("--limit", type=int)
    comparison = sub.add_parser("compare")
    comparison.add_argument("control", type=Path)
    comparison.add_argument("candidate", type=Path)
    comparison.add_argument("--treatment", action="append", default=[])
    comparison.add_argument("--out", type=Path, required=True)
    schema = sub.add_parser("schema")
    schema.add_argument("--out", type=Path, required=True)
    review = sub.add_parser("review-state")
    review.add_argument("state", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare_runs(
                args.control, args.candidate, treatments=args.treatment
            )
            atomic_json(args.out, result)
        elif args.command == "schema":
            atomic_json(args.out, Bundle.model_json_schema())
            result = {"schema": str(args.out)}
        elif args.command == "review-state":
            result = review_update(args.state, None)
        else:
            result = run(load_bundle(args.bundle), args.out, limit=args.limit)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 3 if result.get("state") == "interrupted" else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        reason = str(exc)
        category = (
            "rights"
            if "rights_" in reason
            else "integrity"
            if any(s in reason for s in ("cache", "replay", "hash", "resume"))
            else "holdout"
            if "holdout" in reason
            else "input"
        )
        result = {
            "state": "failed",
            "failure": {
                "category": category,
                "reason": reason,
                "error_type": type(exc).__name__,
            },
            "final_quality": "not_authorized",
        }
        if args.command == "run":
            atomic_json(args.out / "failure.json", result)
        print(json.dumps(result, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
