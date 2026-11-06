#!/usr/bin/env python3
"""Run real /usr/bin/basename against the source replica; writes raw and summary evidence."""
from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
from pathlib import Path

from ttakkari.reverse_compare import run_comparison

VECTORS = [
    ("plain", ["report.txt"], "일반 파일명"),
    ("spaces-unicode", ["폴더 이름/한글 파일.txt", ".txt"], "한글·공백·suffix"),
    ("trailing-slashes", ["folder///"], "후행 슬래시 경계"),
    ("root", ["/"], "루트 경계"),
    ("empty-string", [""], "빈 문자열 경계"),
    ("suffix-option", ["-s", ".txt", "hello world.txt"], "-s 옵션"),
    ("all-option", ["-a", "one.txt", "two words.txt"], "-a 다중 인자 옵션"),
    ("missing-argument", [], "인자 누락 오류"),
    ("invalid-option", ["-z"], "알 수 없는 옵션 오류"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="basename-live-20261107")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("reverse_compare_basename_replica.py").resolve()
    source_copy = output / "reverse_compare_basename_replica.py"
    source_copy.write_bytes(source.read_bytes())
    logging.basicConfig(filename=output / "run.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger(__name__)
    records = []
    for case_id, vector, behavior in VECTORS:
        logger.info("starting case=%s argv=%r", case_id, vector)
        record = run_comparison(
            output, comparison_id=f"basename-{case_id}", behavior=behavior, target_type="cli",
            input_data={"argv": vector, "vector_id": case_id}, original_command=["/usr/bin/basename"],
            replica_command=[sys.executable, str(source_copy)], pre_state={"cwd": str(output), "fixture_state": "no files required"},
            environment={"os": platform.platform(), "python": platform.python_version(), "original": "/usr/bin/basename"},
            run_id=args.run_id, timeout_seconds=3,
        )
        records.append(record)
        logger.info("finished case=%s result=%s difference=%s", case_id, record["result"], record["difference"])
    report = {"harness": "target harness run; not platform agent E2E", "target": "/usr/bin/basename",
              "run_id": args.run_id, "run_environment": {"os": platform.platform(), "python": platform.python_version()},
              "cases": records}
    (output / "comparisons.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, "cases": len(records),
                      "results": {status: sum(item["result"] == status for item in records)
                                  for status in ("match", "mismatch", "unverified")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
