"""Scope-local, permission-bound learning. Models are shadow candidates, never automatic gates."""
import asyncio
import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import select

from ttakkari.collector import learning, store
from ttakkari.collector.control_models import CollectorQualityModel, CollectorReview
from ttakkari.collector.quality import AxisScore, ScorerInfo, register_scorer
from ttakkari.collector_models import CollectorItem


async def latest_model(db, tenant, scope):
    return await db.scalar(select(CollectorQualityModel).where(
        CollectorQualityModel.tenant_id == tenant, CollectorQualityModel.scope == scope,
    ).order_by(CollectorQualityModel.created_at.desc(), CollectorQualityModel.id.desc()).limit(1))


async def load_requested(db, tenant, scope, name):
    prefix = f"utility:{tenant}:{scope}:"
    if not name.startswith(prefix):
        raise ValueError("quality_model_scope_mismatch")
    digest = name[len(prefix):]
    model = await db.scalar(select(CollectorQualityModel).where(
        CollectorQualityModel.tenant_id == tenant, CollectorQualityModel.scope == scope,
        CollectorQualityModel.digest == digest,
    ).order_by(CollectorQualityModel.created_at.desc()).limit(1))
    if model is not None:
        install(model)


def scorer_name(tenant, scope, digest):
    return f"utility:{tenant}:{scope}:{digest}"


def install(model):
    spec = dict(model.artifact)
    digest = spec.pop("digest")
    if hashlib.sha256(json.dumps(spec, sort_keys=True, allow_nan=False).encode()).hexdigest() != digest:
        raise ValueError("quality_model_digest_mismatch")
    class Scorer:
        info = ScorerInfo(scorer_name(model.tenant_id, model.scope, model.digest),
                          learning.VERSION, model.digest, "char-tfidf-v1")

        async def score(self, text, language):
            value = await asyncio.to_thread(learning.predict, model.artifact, text)
            calibration = model.artifact.get("calibration")
            calibration_id = None
            probability = None
            gold_hash = None
            if calibration is not None:
                calibration_id = hashlib.sha256(json.dumps(
                    calibration, sort_keys=True, allow_nan=False, separators=(",", ":")
                ).encode()).hexdigest()
                probability = learning.calibrated_probability(calibration, value)
                gold_hash = calibration["gold_hash"]
            return {"utility": AxisScore(value=value, scale="raw_logit",
                    calibrated_probability=probability, calibration_id=calibration_id,
                    calibration_gold_hash=gold_hash,
                    evidence=(f"input_chars={len(text)}; feature_prefix_chars={min(20000, len(text))}; shadow_only",))}
    register_scorer(Scorer())


async def reviewed_items(db, tenant, scope):
    rows = (await db.execute(select(CollectorReview, CollectorItem).join(CollectorItem).where(
        CollectorItem.tenant_id == tenant, CollectorItem.scope == scope,
    ).order_by(CollectorReview.created_at.desc(), CollectorReview.id.desc()).limit(10000))).all()
    latest = {}
    for review, item in rows:
        latest.setdefault(item.id, (review, item))
    return list(latest.values())


async def train(db, tenant, scope):
    samples = []
    for review, item in await reviewed_items(db, tenant, scope):
        if review.decision not in ("accept", "reject") or not item.envelope or item.text_sha256 != review.text_sha256:
            continue
        normalized = item.envelope.get("normalized")
        active = await store.active_policy(db, tenant, scope, item.source_key)
        if (normalized is None or normalized["truncated"] or active is None or not active[0].allows("train")
                or "train" not in item.envelope["rights"]["purposes"]):
            continue
        rights = item.envelope["rights"]
        samples.append({"id": str(review.id), "group": urlsplit(item.final_url or item.url).hostname or item.source_key,
                        "text": normalized["text"], "label": int(review.decision == "accept"),
                        "review_id": str(review.id), "review_actor": review.actor,
                        "reviewed_at": review.created_at.isoformat(), "source_key": item.source_key,
                        "source_policy_hash": active[2], "source_policy_version": rights.get("policy_version"),
                        "license_id": rights.get("license_id"), "policy_url": active[0].policy_url,
                        "train_permission": True})
    artifact = await asyncio.to_thread(learning.fit, samples)
    model = CollectorQualityModel(tenant_id=tenant, scope=scope, digest=artifact["digest"], artifact=artifact)
    db.add(model)
    await db.flush()
    return model
