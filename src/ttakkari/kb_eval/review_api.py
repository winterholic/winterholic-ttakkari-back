"""Unmounted mobile review API skeleton. Human authentication is not implemented.

The application must supply authentication/authorization before mounting this
router. It is deliberately absent from ttakkari.main and refuses all human writes.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .schema import Key, Strict, Time
from .state import review_update


class ReviewEvent(Strict):
    id: Key
    label_version: Annotated[int, Field(ge=0)]
    decision: Literal["approve", "correct", "discard"]
    actor_kind: Literal["agent", "llm", "human"]
    actor: Key
    at: Time
    correction: str | None = None


def create_router(bundle, state_path):
    router = APIRouter(prefix="/kb-eval/review", tags=["kb-e0-unmounted-skeleton"])
    cases = {c["id"]: c for c in bundle["cases"] if c["split"] == "dev"}
    sources = {s["doc_key"]: s for s in bundle["sources"]}

    @router.get("/state")
    def state():
        return review_update(state_path, None)

    @router.get("/items/{query_id}")
    def item(query_id: str):
        if query_id not in cases:
            raise HTTPException(404, "unknown_dev_item")
        case = cases[query_id]
        return {
            "question": case,
            "sources": [sources[key] for key in case["documents"]],
            "pool": bundle["predictions"].get(query_id),
            "human_auth": "unimplemented",
            "browser_verification": "unverified",
        }

    @router.post("/events")
    def event(value: ReviewEvent):
        if value.decision == "correct" and not value.correction:
            raise HTTPException(422, "correction_required")
        if value.id not in cases:
            raise HTTPException(404, "unknown_dev_item")
        try:
            return review_update(state_path, value.model_dump())
        except ValueError as exc:
            raise HTTPException(
                501 if "human_attestation" in str(exc) else 409, str(exc)
            ) from exc

    return router
