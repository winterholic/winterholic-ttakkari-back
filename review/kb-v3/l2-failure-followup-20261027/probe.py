"""Reproduce commit f774928b's held-job success gate on isolated SQLite; no CLI calls."""
from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[3]
TARGET = "f774928b2c999da28e33e5139841742ed8544f56"


async def held_zero_claim_counterexample() -> None:
    tests = importlib.import_module("test_kb_v3_verify_operation")
    fixture = tests.mem_db.__wrapped__()
    db, sf = await fixture.__anext__()
    patch = MonkeyPatch()
    try:
        settings = tests.Settings(
            kb_l2_adapter="subscription_cli",
            kb_l2_subscription_model="gpt-6-luna",
            _env_file=None,
        )
        patch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: settings)
        patch.setattr("ttakkari.kb_pipeline.stages.get_settings", lambda: settings)

        class HoldAllWorker:
            _tasks: set = set()  # noqa: RUF012

            async def pump(self) -> int:
                # Feed the product function a terminal state its gate explicitly accepts.
                async with sf() as session:
                    jobs = list((await session.scalars(select(tests.KbPipelineJob))).all())
                    for job in jobs:
                        job.status = "held"
                    await session.commit()
                return 1

        result = await tests.verify_kb_v3(db, session_factory=sf, worker=HoldAllWorker())
        counts = result["counts"]
        statuses = [job["status"] for job in result["jobs"]]
        l2_calls = sum(
            job["details"].get("l2", {}).get("calls_made", 0)
            for job in result["jobs"]
        )
        print(
            f"target={TARGET} counterexample held_terminal:",
            f"overall={result['status']} jobs={statuses} assertions={counts['assertions']} "
            f"evidence={counts['evidence']} conflicts={counts['conflicts']} l2_calls={l2_calls}",
        )
        assert result["status"] == "succeeded"
        assert statuses == ["held", "held", "held"]
        assert counts["assertions"] == counts["evidence"] == counts["conflicts"] == 0
        assert l2_calls == 0
    finally:
        patch.undo()
        await fixture.aclose()


def real_pdf_parser() -> None:
    from ttakkari.kb_parsers import create_default_registry

    pdf = ROOT / "tests/fixtures/kb_v3/sample_spec.pdf"
    parsed = create_default_registry().parse(
        pdf.read_bytes(), filename=pdf.name, content_type="application/pdf"
    )
    print(
        f"target={TARGET} real_pdf_parser:",
        f"bytes={pdf.stat().st_size} status={parsed.status} parser={parsed.metadata.get('parser')} "
        f"pages={parsed.metadata.get('page_count')} chars={len(parsed.normalized_body)} spans={len(parsed.locator_spans)}",
    )
    assert parsed.status == "succeeded"
    assert parsed.metadata.get("parser") == "pdf_pypdf_v1"
    assert len(parsed.normalized_body) > 0 and len(parsed.locator_spans) == 3


async def main() -> None:
    real_pdf_parser()
    await held_zero_claim_counterexample()


if __name__ == "__main__":
    asyncio.run(main())
