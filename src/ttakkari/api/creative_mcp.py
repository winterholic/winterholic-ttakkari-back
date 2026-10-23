"""Personal-session MCP bridge to the existing Creative Studio v2 services."""

from __future__ import annotations

import asyncio
import base64
import binascii
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ttakkari.api import creative_v2 as c2
from ttakkari.api.creative import _normalize, perform
from ttakkari.api.deps import Db
from ttakkari.creative.v2 import dispatch as production
from ttakkari.creative.v2.plans import default_plan_store
from ttakkari.creative.v2.style_dna import (
    SEMVER,
    SLUG,
    StyleDNADoc,
    default_style_store,
)
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession

router = APIRouter(prefix="/api/agent/creative-v2", tags=["creative-studio-v2-agent"])

AVAILABLE_OPERATIONS = frozenset({
    "catalog", "capabilities", "recipe_matrix", "registries", "plans", "plan",
    "plan_create", "plan_edit", "plan_approve", "production_status", "auto_assets_status",
    "auto_assets_start", "auto_assets_retry",
    "production_dispatch", "production_render", "production_asset", "production_media_link",
    "styles", "style", "style_put",
})
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    data: dict[str, Any] = Field(default_factory=dict)


async def agent_access(sid: OrchestratorSession, db: Db) -> str:
    row = await db.get(ChatSession, sid)
    if row is None:
        raise HTTPException(403, "세션이 없습니다.")
    if row.pool == "work":
        raise HTTPException(403, "개인 세션에서만 Creative Studio를 사용할 수 있습니다.")
    return "personal"


def _uuid(data: dict[str, Any], key: str = "plan_id") -> uuid.UUID:
    try:
        return uuid.UUID(str(data[key]))
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"{key} must be a UUID") from exc


def _asset(data: dict[str, Any], plan_id: uuid.UUID) -> dict[str, Any]:
    filename = data.get("filename")
    kind = data.get("kind")
    encoded = data.get("content_base64")
    scene_id = data.get("scene_id")
    if not isinstance(filename, str) or len(filename) > 160:
        raise HTTPException(422, "filename is required and must be at most 160 characters")
    if kind not in {"scene", "music"}:
        raise HTTPException(422, "kind must be scene or music")
    suffix = Path(filename).suffix.lower()
    if suffix not in (IMAGE_SUFFIXES if kind == "scene" else AUDIO_SUFFIXES):
        raise HTTPException(422, "asset file extension is not supported for this kind")
    if kind == "scene" and (not isinstance(scene_id, str) or not 1 <= len(scene_id) <= 50):
        raise HTTPException(422, "scene_id is required for scene assets")
    if not isinstance(encoded, str) or len(encoded) > 14_000_000:
        raise HTTPException(413, "base64 asset payload is required and capped at 10 MiB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "content_base64 is invalid") from exc
    if not raw or len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "asset payload must be between 1 byte and 10 MiB")
    perform(default_plan_store().get, str(plan_id))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ("upload" + suffix)
        path.write_bytes(raw)
        normalized, media_kind, info = perform(_normalize, path, suffix)
        if (kind == "scene") != (media_kind == "image"):
            raise HTTPException(422, "asset content type does not match kind")
        asset = perform(
            production.put_asset, default_plan_store(), str(plan_id), kind=kind,
            scene_id=scene_id, source=normalized, name=Path(filename).name, meta=info,
        )
    return {key: asset[key] for key in ("id", "sha256", "name", "kind", "uploaded_at", "meta")}


def _style_put(data: dict[str, Any]) -> dict[str, Any]:
    """Register one immutable Style DNA version from a strictly validated document.

    The document is the existing Strict `StyleDNADoc` model: unknown fields are rejected, refs are slugs or
    safe relative names with pinned hashes, and nothing here reads or executes a path from it. Existing
    (style_id, version) pairs and built-in seeds cannot change, so a plan's pinned style never moves.
    """
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise HTTPException(422, "dry_run must be a boolean")
    try:
        doc = StyleDNADoc.model_validate(data.get("doc"))
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        raise HTTPException(422, f"invalid style doc: {problems}") from exc
    store = default_style_store()
    problems = perform(store.validate_doc, doc)
    ref = f"{doc.style_id}@{doc.version}"
    dumped = doc.model_dump(mode="json")
    effective = (
        perform(store.effective, doc.parent).model_dump(mode="json") if doc.parent and not problems else {}
    )
    for field in doc.own_fields():
        effective[field] = dumped[field]
    preview = production.style_render_preview(effective) if not problems else None
    if dry_run:
        return {"dry_run": True, "valid": not problems, "ref": ref, "problems": problems, "render_effects": preview}
    perform(store.put, doc)
    return {"dry_run": False, "saved": True, "ref": ref, "render_effects": preview}


@router.post("/{operation}")
async def command(operation: str, body: Command, _: str = Depends(agent_access)) -> Any:
    if operation not in AVAILABLE_OPERATIONS:
        raise HTTPException(404, "Creative Studio MCP operation is unavailable")
    data = body.data
    allowed_fields = {
        "catalog": set(), "capabilities": set(), "recipe_matrix": set(), "registries": set(), "plans": set(),
        "plan": {"plan_id"}, "plan_create": {"brief", "name", "intent", "duration", "aspect", "style", "resolutions"},
        "plan_edit": {"plan_id", "revision", "scenes", "duration"},
        "plan_approve": {"plan_id", "revision"}, "production_status": {"plan_id"},
        "auto_assets_status": set(), "auto_assets_start": {"plan_id", "revision"},
        "auto_assets_retry": {"plan_id", "revision"},
        "production_dispatch": {"plan_id", "revision", "caption_mode"},
        "production_render": {"plan_id", "revision"},
        "production_asset": {"plan_id", "kind", "filename", "content_base64", "scene_id"},
        "production_media_link": {"plan_id", "id"},
        "styles": set(), "style": {"style_id", "version"}, "style_put": {"doc", "dry_run"},
    }
    unknown = set(data) - allowed_fields[operation]
    if unknown:
        raise HTTPException(422, "unsupported input fields: " + ", ".join(sorted(unknown)))
    if operation == "styles":
        return {
            "styles": await c2.style_list("owner"),
            "render_controls": production.style_render_controls(),
        }
    if operation == "style":
        style_id, version = data.get("style_id"), data.get("version", "latest")
        if not isinstance(style_id, str) or not SLUG.match(style_id):
            raise HTTPException(422, "style_id must be a lowercase slug")
        if not isinstance(version, str) or not (version == "latest" or SEMVER.match(version)):
            raise HTTPException(422, "version must be x.y.z or latest")
        found = await c2.style_get(style_id, version, "owner")
        dna = found["dna"].model_dump(mode="json")
        return {**found, "dna": dna, "render_effects": production.style_render_preview(dna)}
    if operation == "style_put":
        return await asyncio.to_thread(_style_put, data)
    if operation == "catalog":
        return await c2.catalog("owner")
    if operation == "capabilities":
        result = await c2.capabilities("owner")
        result["automatic_asset_generation"] = {
            "available": True,
            "supported_recipes": ["R21"],
            "engines": ["managed-comfy Wan 2.2 draft", "local Supertonic", "local ACE-Step 1.5"],
            "cancel_available": False,
            "reason": "Readiness is checked by comfy_readiness and again immediately before generation. Whole-job cancellation is not supported.",
        }
        result["style_dna_render_controls"] = production.style_render_controls()
        result["share_to_library"] = {
            "available": False,
            "reason": "This revision can issue a short-lived media download link but has no production-to-library artifact registration service.",
        }
        return result
    if operation == "recipe_matrix":
        return await c2.matrix("owner")
    if operation == "registries":
        return await c2.registries("owner")
    if operation == "plans":
        return await c2.plan_list("owner")
    if operation == "plan":
        return await c2.plan_get(_uuid(data), "owner")
    if operation == "plan_create":
        try:
            plan = c2.PlanCreate.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False)
            )
            raise HTTPException(422, f"invalid plan_create input: {problems}") from exc
        return await c2.plan_create(plan, "owner")
    if operation == "plan_edit":
        plan_id = _uuid(data)
        edit = c2.PlanEdit.model_validate({k: v for k, v in data.items() if k != "plan_id"})
        return await c2.plan_edit(plan_id, edit, "owner")
    if operation == "plan_approve":
        plan_id = _uuid(data)
        approve = c2.PlanApprove.model_validate({k: v for k, v in data.items() if k != "plan_id"})
        return await c2.plan_approve(plan_id, approve, "owner")
    if operation == "production_status":
        result = await c2.production_get(_uuid(data), "owner")
        result["log"] = (result.get("log") or [])[-100:]
        while len(str(result)) > 48_000 and result["log"]:
            result["log"].pop(0)
        if len(str(result)) > 48_000:
            result["log"] = [{"event": "truncated", "detail": "production status exceeded the 48 KiB MCP response limit"}]
        return result
    if operation == "auto_assets_status":
        return {
            "available": True,
            "supported_recipes": ["R21"],
            "engines": ["managed-comfy Wan 2.2 draft", "local Supertonic", "local ACE-Step 1.5"],
            "start": "creative_v2_auto_assets_start",
            "retry": "creative_v2_auto_assets_retry",
            "cancel_available": False,
            "cancel_reason": "The v2 generation service has no safe whole-job cancellation contract. A Comfy scene task must not be interrupted independently from its generation receipt and cleanup.",
            "result_flow": ["generated asset IDs and engine receipts", "creative_v2_production_dispatch", "creative_v2_production_render", "creative_v2_production_status", "creative_v2_production_media_link"],
            "readiness": "Check comfy_readiness before starting. The generation service repeats exact runtime, model, local TTS and ACE-Step preflight checks.",
        }
    if operation in {"auto_assets_start", "auto_assets_retry"}:
        plan_id = _uuid(data)
        request = c2.PlanApprove.model_validate({k: v for k, v in data.items() if k != "plan_id"})
        if operation == "auto_assets_retry":
            current = await c2.production_get(plan_id, "owner")
            generation = current.get("asset_generation") or {}
            if generation.get("status") != "failed":
                raise HTTPException(409, "Only a recorded failed generation can be retried; inspect production_status first.")
        return await c2.production_generate_assets(plan_id, request, "owner")
    if operation == "production_dispatch":
        plan_id = _uuid(data)
        caption_mode = data.get("caption_mode", "narration")
        if caption_mode not in {"narration", "title_only"}:
            raise HTTPException(422, "caption_mode must be narration or title_only")
        approve = c2.PlanApprove.model_validate(
            {k: v for k, v in data.items() if k not in {"plan_id", "caption_mode"}}
        )
        return await c2.production_dispatch(plan_id, approve, "owner", caption_mode)
    if operation == "production_render":
        plan_id = _uuid(data)
        approve = c2.PlanApprove.model_validate({k: v for k, v in data.items() if k != "plan_id"})
        return await c2.production_render(plan_id, approve, "owner")
    if operation == "production_asset":
        return await asyncio.to_thread(_asset, data, _uuid(data))
    if operation == "production_media_link":
        plan_id = _uuid(data)
        link = c2.MediaLinkInput.model_validate({"id": data.get("id")})
        return await c2.production_media_link(plan_id, link, "owner")
    raise HTTPException(404, "Creative Studio MCP operation is unavailable")
