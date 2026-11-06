#!/usr/bin/env python3
"""Compare common requests against two disposable local demo API servers."""
from __future__ import annotations

import argparse
import json
import logging
import threading
from pathlib import Path

from reverse_compare_api_demo import serve

from ttakkari.reverse_compare import run_api_comparison


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="api-demo-20261107")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    servers = [serve(), serve()]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    original = f"http://127.0.0.1:{servers[0].server_port}"
    replica = f"http://127.0.0.1:{servers[1].server_port}"
    logging.basicConfig(filename=output / "api-run.log", level=logging.INFO, format="%(asctime)s %(message)s")
    logger = logging.getLogger(__name__)
    cases = [
        ("api-json-state", "POST JSON 직렬화와 상태 변화", "/items", {"json": {"value": "한글 payload"}, "state_path": "/state"}, "POST"),
        ("api-validation-error", "422 검증 오류 응답", "/items", {"json": {}}, "POST"),
        ("api-missing-route", "404 경계 응답", "/missing", {}, "GET"),
        ("api-response-boundary", "응답 8KiB 직렬화 경계", "/large", {}, "GET"),
    ]
    results = []
    try:
        for case_id, behavior, path, input_data, method in cases:
            logger.info("start id=%s method=%s path=%s", case_id, method, path)
            record = run_api_comparison(output, comparison_id=case_id, behavior=behavior,
                input_data=input_data, original_url=original + path, replica_url=replica + path,
                run_id=args.run_id, method=method, headers={"X-Fixture-Mode": "local"}, timeout_seconds=3)
            results.append(record)
            logger.info("finish id=%s result=%s diff=%s", case_id, record["result"], record["difference"])
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    report = {"scope": "self-owned local API test servers", "run_id": args.run_id, "comparisons": results}
    (output / "api-comparisons.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(results), "results": {key: sum(r["result"] == key for r in results)
          for key in ("match", "mismatch", "unverified")}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
