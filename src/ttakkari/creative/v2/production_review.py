"""Trusted in-process review bridge, deliberately unconfigured by default.

Only server bootstrap may supply EvaluatorBinding. HTTP accepts a revision only;
there is no receipt upload or evaluator registration endpoint. Hashes attest
integrity, not identity: identity comes from the server-owned callable binding
and the private production store. This is not a cryptographic remote protocol.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ttakkari.creative.v2 import quality, quality_worker


@dataclass(frozen=True)
class EvaluatorBinding:
    id: str
    version: str
    source: str
    independent: bool
    execute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


# No discovered registry entry or client assertion installs an evaluator.
EVALUATOR_BINDING: EvaluatorBinding | None = None


def _evidence(receipt):
    return [
        item
        for section in ("criteria", "defects")
        for row in receipt.get(section, [])
        for item in row.get("evidence", [])
    ]


def attest(
    receipt: dict[str, Any], *, execution_id: str, manifest_sha256: str
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "receipt_sha256": quality.content_hash(receipt),
        "output_sha256": receipt["output_sha256"],
        "rubric_sha256": receipt["rubric_sha256"],
        "evidence_sha256": quality.content_hash(_evidence(receipt)),
        "input_manifest_sha256": manifest_sha256,
    }


def attested_receipts(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter unrecorded, edited, stale, or file-mutated assertions at core ingress."""
    from pathlib import Path

    accepted = []
    for receipt in record.get("quality_evidence", []):
        if not isinstance(receipt, dict):
            continue
        try:
            evidence = _evidence(receipt)
            digest = quality.content_hash(receipt)
            attestation = next(
                (
                    a
                    for a in record.get("quality_attestations", [])
                    if a.get("receipt_sha256") == digest
                ),
                None,
            )
            if (
                not attestation
                or not attestation.get("execution_id")
                or not attestation.get("input_manifest_sha256")
                or attestation.get("output_sha256") != receipt.get("output_sha256")
                or attestation.get("rubric_sha256") != receipt.get("rubric_sha256")
                or attestation.get("evidence_sha256") != quality.content_hash(evidence)
            ):
                continue
            if record.get("final") and receipt.get("output_sha256") != record[
                "final"
            ].get("sha256"):
                continue
            if record.get("rubric") and receipt.get("rubric_sha256") != record[
                "rubric"
            ].get("sha256"):
                continue
            if any(quality.file_hash(Path(e["path"])) != e["sha256"] for e in evidence):
                continue
        except (KeyError, TypeError, ValueError, OSError):
            continue
        accepted.append(deepcopy(receipt))
    return accepted


def run_review(
    plans,
    studio,
    plan_id: str,
    revision: int,
    *,
    binding: EvaluatorBinding | None = None,
) -> dict[str, Any]:
    """Deliver bytes and CAS-store the result of a server-owned evaluator only.

    Preparation/delivery is not independent perception. Default execution remains
    unknown until the operator connects an actual independent harness.
    """
    from ttakkari.creative.v2 import dispatch

    binding = binding or EVALUATOR_BINDING
    store = dispatch.ProductionStore(plans)
    with store.tx(plan_id, blocking=False) as data:
        plan = plans.assert_renderable(plan_id, revision)
        record = dispatch._record(data, revision)
        if not record or not record.get("final"):
            raise dispatch.ProductionError(
                "render a final output before requesting review"
            )
        if binding is None:
            state = {
                "status": "not_connected",
                "verdict": "unknown",
                "reason": "production_review.EVALUATOR_BINDING has no trusted independent evaluator; director cpu-check and registry discovery do not qualify",
            }
            record["review_execution"] = state
            dispatch._log(record, "review_not_connected", reason=state["reason"])
            return state
        if not all(
            isinstance(v, str) and v.strip()
            for v in (binding.id, binding.version, binding.source)
        ) or not callable(binding.execute):
            raise dispatch.ProductionError("invalid server evaluator binding")
        media, _, output_sha = dispatch.result_media(plans, plan_id, revision, "final")
        rubric_sha = quality.build_rubric(plan)["sha256"]
        if record["rubric"]["sha256"] != rubric_sha:
            raise dispatch.ProductionError("review rubric changed after dispatch")
        assets_sha = quality.content_hash(data["assets"])
    execution_id = str(uuid.uuid4())
    package = quality_worker.prepare_review(
        plan, media, store.folder(plan_id) / "reviews" / execution_id
    )
    receipt = quality_worker.deliver_review(package, binding.execute)
    if receipt.get("stage") not in {"semantic_qa", "viewing_qa"} or not isinstance(
        receipt.get("criteria"), list
    ):
        raise dispatch.ProductionError("evaluator returned an invalid stage/criteria")
    # An evaluator response cannot declare its own independence or identity.
    receipt["evaluator"] = {
        "id": binding.id,
        "version": binding.version,
        "source": binding.source,
        "independent": binding.independent,
    }
    artifacts = [
        package["media"],
        *package["frames"],
        *([package["audio"]] if package.get("audio") else []),
    ]
    allowed = {(a["path"], a["sha256"]): a for a in artifacts}
    evidence = _evidence(receipt)
    identity_fields = ("source_sha256", "kind", "start_time", "end_time")
    if not evidence or any(
        not isinstance(e, dict)
        or (e.get("path"), e.get("sha256")) not in allowed
        or any(
            e.get(key) != allowed[(e["path"], e["sha256"])].get(key)
            for key in identity_fields
        )
        for e in evidence
    ):
        raise dispatch.ProductionError(
            "evaluator evidence must name hash-verified delivered media"
        )
    attestation = attest(
        receipt,
        execution_id=execution_id,
        manifest_sha256=receipt["transport"]["input_manifest_sha256"],
    )
    with store.tx(plan_id, blocking=False) as data:
        current = plans.assert_renderable(plan_id, revision)
        record = dispatch._record(data, revision)
        _, _, current_sha = dispatch.result_media(plans, plan_id, revision, "final")
        if (
            current_sha != output_sha
            or quality.build_rubric(current)["sha256"] != rubric_sha
            or quality.content_hash(data["assets"]) != assets_sha
        ):
            raise dispatch.ProductionError(
                "review output/rubric/assets changed during evaluator execution"
            )
        candidate = {
            **record,
            "quality_evidence": [receipt],
            "quality_attestations": [attestation],
        }
        if attested_receipts(candidate) != [receipt]:
            raise dispatch.ProductionError(
                "review evidence changed during evaluator execution"
            )
        record.setdefault("quality_evidence", []).append(receipt)
        record.setdefault("quality_attestations", []).append(attestation)
        record["quality_report"] = quality.evaluate_production(
            current,
            {
                **record,
                "assets": data["assets"],
                "quality_evidence": attested_receipts(record),
            },
            media_path=media,
        )
        record["repair_plan"] = record["quality_report"]["repair_plan"]
        state = {
            "status": "recorded",
            "execution_id": execution_id,
            "receipt_sha256": attestation["receipt_sha256"],
            "output_sha256": output_sha,
            "rubric_sha256": rubric_sha,
            "evidence_sha256": attestation["evidence_sha256"],
            "verdict": record["quality_report"]["overall_verdict"],
        }
        record["review_execution"] = state
        dispatch._log(record, "trusted_review_recorded", **state)
        return state
