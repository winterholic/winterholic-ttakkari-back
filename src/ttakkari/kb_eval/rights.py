"""Deny real data evaluation until an authenticated rights review flow exists.

Input status/URL/evidence hashes and claimed reviewer context are descriptive;
none creates authority. Synthetic-only fixtures carry no external rights grant.
"""


def validate_rights(bundle):
    if bundle["kind"] != "fixture":
        raise ValueError(
            "rights_review_authentication_required: public/custom evaluation held; authenticated review flow unavailable"
        )
    rights = {r["component"]: r for r in bundle["rights"]}
    if len(rights) != len(bundle["rights"]):
        raise ValueError("duplicate_rights_component")
    if bundle["observations"]["kind"] != "fixture" or any(
        not s["source_uri"].startswith("fixture://") for s in bundle["sources"]
    ):
        raise ValueError(
            "rights_synthetic_provenance_required: external source in fixture"
        )
    for component in ("code", "data", "qrels", "corpus"):
        r = rights.get(component)
        if (
            r is None
            or r.get("provenance_kind") != "synthetic"
            or not r["original_url"].startswith("fixture://")
            or r.get("review_evidence") is not None
        ):
            raise ValueError("rights_synthetic_provenance_required:" + component)
        if r["status"] != "verified" or r["storage_allowed"] is not True:
            raise ValueError("rights_unverified:" + component)
        if bundle["links"] and r["transform_allowed"] is not True:
            raise ValueError("rights_transform_unverified:" + component)
    return {
        "status": "synthetic_fixture_only",
        "authority": "none",
        "declaration": "caller_declared_synthetic_fixture_not_authenticated_license_review",
        "external_data_evaluation": "held",
        "authenticated_review_flow": "unimplemented",
    }
