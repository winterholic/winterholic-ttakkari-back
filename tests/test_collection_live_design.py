"""Opt-in: the design-system goal against the real public web, into a disposable database. Prints a report.

COLLECTION_LIVE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -s tests/test_collection_live_design.py
"""
import json
import os
import time
from pathlib import Path

import pytest

from ttakkari import kb_delivery
from ttakkari.collector import pipeline, runner

pytestmark = pytest.mark.skipif(os.environ.get("COLLECTION_LIVE") != "1", reason="explicit public-network run only")
BASE = "/api/assistant/collect"
GOAL = (Path(__file__).parent / "fixtures" / "collect_design_goal.md").read_text()


@pytest.fixture(autouse=True)
def _no_live_paper_providers():
    yield


async def test_design_goal_runs_end_to_end(client, auth):
    pipeline.configure(None)
    pipeline.install()
    pages = int(os.environ.get("LIVE_PAGES", "120"))
    started = time.monotonic()
    try:
        made = await client.post(BASE + "/missions", headers=auth, json={"goal": GOAL, "max_pages": pages, "max_depth": 2})
        assert made.status_code == 202, made.text
        ident = made.json()["id"]
        budget_seconds = int(os.environ.get("LIVE_SECONDS", "1200"))
        ticks = 0
        while time.monotonic() - started < budget_seconds:
            ran = await runner.consume_once(limit=3, managed_only=True)
            delivered = await kb_delivery.deliver_once()
            ticks += 1
            if ticks % 10 == 0:
                response = await client.get(BASE + "/missions/" + ident, headers=auth)
                if response.status_code != 200:
                    print(f"[{int(time.monotonic() - started)}s] GET mission -> {response.status_code} {response.text[:600]}")
                    continue
                detail = response.json()
                print(f"[{int(time.monotonic() - started)}s] status={detail['status']} counts="
                      f"{ {k: v for k, v in detail['counts'].items() if v} } jobs={len(detail['jobs'])}")
            if not ran and not delivered:
                break
        detail = (await client.get(BASE + "/missions/" + ident, headers=auth)).json()
        Path(os.environ.get("LIVE_REPORT", "/tmp/live-design-report.json")).write_text(json.dumps(detail, ensure_ascii=False, indent=1))
        print("STATUS", detail["status"], "ELAPSED", int(time.monotonic() - started))
        print("COUNTS", {k: v for k, v in detail["counts"].items() if v})
        failed = [j for j in detail["jobs"] if j["status"] == "failed"]
        print("FAILED JOBS", len(failed), [j["error"] for j in failed][:5])
        print("GAPS", {g["reason"] for g in detail["gaps"]}, len(detail["gaps"]))
        for c in (detail.get("research") or {}).get("coverage", []):
            print("COVER", c["state"], c["documents"], c["topic"])
    finally:
        if pipeline._default_fetcher is not None:
            await pipeline._default_fetcher.aclose()
        pipeline.configure(None)
        runner.register_handler(None)
