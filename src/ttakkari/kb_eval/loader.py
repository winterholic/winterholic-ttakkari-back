"""Offline rights-aware loader. Never downloads data or changes source text."""

import json
from pathlib import Path

from .integrity import components, digest
from .schema import Bundle
from .state import canonical


def load_bundle(path):
    raw = Path(path).read_bytes()

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key:" + key)
            result[key] = value
        return result

    bundle = Bundle.model_validate(
        json.loads(raw, object_pairs_hook=unique_keys)
    ).model_dump()
    rights = {r["component"]: r for r in bundle["rights"]}
    if len(rights) != len(bundle["rights"]):
        raise ValueError("duplicate_rights_component")
    for component in ("code", "data", "qrels", "corpus"):
        r = rights.get(component)
        if not r or r["status"] != "verified" or r["storage_allowed"] is not True:
            raise ValueError("rights_unverified:" + component)
        if bundle["links"] and r["transform_allowed"] is not True:
            raise ValueError("rights_transform_unverified:" + component)
    source_ids, passage_ids, revisions = set(), set(), set()
    for s in bundle["sources"]:
        if s["doc_key"] in source_ids or s["source_revision_id"] in revisions:
            raise ValueError("duplicate_source_identity")
        source_ids.add(s["doc_key"])
        revisions.add(s["source_revision_id"])
        if (
            digest(s["body"]) != s["body_sha256"]
            or digest(s["raw_utf8"]) != s["raw_sha256"]
        ):
            raise ValueError("source_hash_mismatch:" + s["doc_key"])
        for p in s["passages"]:
            if p["id"] in passage_ids or not 0 <= p["start"] < p["end"] <= len(
                s["body"]
            ):
                raise ValueError("invalid_or_duplicate_passage")
            passage_ids.add(p["id"])
    for c in bundle["cases"]:
        if c["verification"] == "human_verified":
            raise ValueError(
                "human_attestation_required: authenticated import not implemented"
            )
        if (
            not set(c["documents"]) <= source_ids
            or not set(c["relevance"]) <= passage_ids
        ):
            raise ValueError("gold_identity_not_in_manifest")
        if c["answerable"] and not c["anchors"]:
            raise ValueError("missing_gold_anchor")
        if any(a["doc_key"] not in c["documents"] for a in c["anchors"]):
            raise ValueError("anchor_document_mismatch")
    if any(not set(pair) <= source_ids for pair in bundle["links"]):
        raise ValueError("unknown_link_document")
    ids = {c["id"] for c in bundle["cases"]}
    if not bundle["predictions"].keys() <= ids:
        raise ValueError("unknown_prediction")
    for p in bundle["predictions"].values():
        if not set(p["ranked"] + p["candidates"]) <= passage_ids:
            raise ValueError("prediction_identity_not_in_manifest")
        if p["score_bytes"] and len(p["score_bytes"]) != len(p["ranked"]):
            raise ValueError("score_bytes_count_mismatch")
    obs = bundle["observations"]
    if len({o["id"] for o in obs["events"]}) != len(obs["events"]):
        raise ValueError("duplicate_observation")
    if any(o["source_revision_id"] not in revisions for o in obs["events"]):
        raise ValueError("observation_revision_unknown")
    if obs["kind"] == "database_export" and not obs["database_revision"]:
        raise ValueError("database_revision_required")
    # Exact source bytes and declared links are connected automatically too.
    links = list(bundle["links"])
    by_body = {}
    for s in bundle["sources"]:
        if s["body_sha256"] in by_body:
            links.append([by_body[s["body_sha256"]], s["doc_key"]])
        by_body[s["body_sha256"]] = s["doc_key"]
    bundle["components"] = components(bundle["cases"], links)
    manifest = bundle["manifest"]
    bundle["identity"] = {
        "corpus": digest(
            canonical(
                {
                    k: bundle[k]
                    for k in (
                        "corpus_id",
                        "corpus_version",
                        "sources",
                        "links",
                        "rights",
                        "observations",
                    )
                }
            )
        ),
        "gold": digest(canonical(bundle["cases"])),
        "policy": digest(canonical(bundle["policy"])),
        "build": manifest["build_id"],
        "model": manifest["model_sha256"],
        "analyzer": manifest["analyzer_sha256"],
        "quantization": manifest["quantization"],
        "config": digest(
            canonical(
                {
                    "manifest": manifest,
                    "predictions": bundle["predictions"],
                    "exposure": bundle["exposure"],
                    "kind": bundle["kind"],
                }
            )
        ),
        "seed": manifest["seed"],
        "code": manifest["code_sha"],
        **{k: manifest["config"][k] for k in ("role", "window")},
    }
    bundle["input_sha256"] = digest(raw)
    return bundle
