"""Render requests on existing production records, consumed by the asset worker.

No new queue/service or credentials. A plan-local flock fences CPU reuse renders;
GPU/model producers retain their own managed paths and are not admitted here.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import uuid
from copy import deepcopy

from ttakkari import queued_work
from ttakkari.creative.service import default_studio
from ttakkari.creative.v2 import dispatch
from ttakkari.creative.v2.plans import default_plan_store

log = logging.getLogger(__name__)
SUPPORTED = {"slideshow", "generative_video"}


def _verify_asset_files(store, plan_id, data):
    assets = [
        *data["assets"]["scenes"].values(),
        *data["assets"].get("voices", {}).values(),
    ]
    if data["assets"].get("music"):
        assets.append(data["assets"]["music"])
    for asset in assets:
        path = store.asset_path(plan_id, asset)
        if not path.is_file() or dispatch._sha256_file(path) != asset["sha256"]:
            raise dispatch.ProductionError(
                "render asset file is missing or its hash changed"
            )


def enqueue(plans, plan_id, revision, *, retry=False):
    plan = plans.assert_renderable(plan_id, revision)
    if not plan.get("director"):
        return {
            "status": "blocked",
            "reasons": ["legacy V1 plans use the existing Studio render queue"],
            "plan_id": plan_id,
            "revision": revision,
        }
    unsupported = sorted(
        {
            s.get("strategy")
            for s in plan["scenes"]
            if s.get("strategy") and s["strategy"] not in SUPPORTED
        }
    )
    if unsupported:
        return {
            "status": "blocked",
            "reasons": [
                "CPU reuse executor does not admit these producers: "
                + ", ".join(unsupported)
            ],
            "plan_id": plan_id,
            "revision": revision,
        }
    store = dispatch.ProductionStore(plans)
    with store.tx(plan_id, blocking=False) as data:
        record = dispatch._record(data, revision)
        if not record:
            raise dispatch.ProductionError(
                "dispatch the approved revision before enqueueing render"
            )
        assets = dispatch._assets_state(plan, data, store)
        if not assets["complete"]:
            raise dispatch.ProductionError("render assets are incomplete")
        _verify_asset_files(store, plan_id, data)
        key = dispatch._digest(
            {
                "compositor_version": dispatch.COMPOSITOR_VERSION,
                "revision": revision,
                "plan_sha256": record["plan_sha256"],
                "assets": data["assets"],
                "caption_mode": record["caption_mode"],
            }
        )
        old = record.get("render_request")
        if (
            old
            and old["key"] == key
            and (not retry or old["status"] in {"queued", "running", "succeeded"})
        ):
            return deepcopy(old)
        if old and old["status"] == "running":
            raise dispatch.ProductionError(
                "running render requires explicit orphan reconciliation"
            )
        if old:
            record.setdefault("render_request_history", []).append(deepcopy(old))
        request = {
            "id": str(uuid.uuid4()),
            "key": key,
            "revision": revision,
            "plan_sha256": record["plan_sha256"],
            "assets_sha256": dispatch._digest(data["assets"]),
            "status": "queued",
            "requested_at": dispatch._now(),
            "error": None,
            "cancel_policy": "queued_only; running cancellation is refused",
        }
        record["render_request"] = request
        return deepcopy(request)


def cancel(plans, plan_id, revision):
    with dispatch.ProductionStore(plans).tx(plan_id, blocking=False) as data:
        record = dispatch._record(data, revision)
        request = (record or {}).get("render_request")
        if not request:
            raise FileNotFoundError("render request not found")
        if request["status"] == "running":
            raise dispatch.ProductionError(
                "running render cannot be cancelled safely; request not changed"
            )
        if request["status"] == "queued":
            request.update(status="cancelled", finished_at=dispatch._now())
        return deepcopy(request)


def _requests(plans):
    import json

    result = []
    if not plans.root.exists():
        return result
    for folder in sorted(plans.root.iterdir()):
        path = folder / "production.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
            for record in data.get("dispatches", []):
                request = record.get("render_request") or {}
                if request.get("status") == "queued":
                    result.append((folder.name, record["revision"], request["id"]))
        except (OSError, ValueError, KeyError):
            log.exception("invalid creative production request: %s", path)
    return result


def execute(plans, studio, plan_id, revision, request_id):
    store = dispatch.ProductionStore(plans)
    with (plans._folder(plan_id) / "render-consumer.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            with store.tx(plan_id, blocking=False) as data:
                record = dispatch._record(data, revision)
                request = (record or {}).get("render_request")
                if (
                    not request
                    or request["id"] != request_id
                    or request["status"] != "queued"
                ):
                    return False
                try:
                    plan = plans.assert_renderable(plan_id, revision)
                    if (
                        plan["approval"]["snapshot"]["plan_sha256"]
                        != request["plan_sha256"]
                        or dispatch._digest(data["assets"]) != request["assets_sha256"]
                    ):
                        raise ValueError("render request inputs changed")
                except ValueError as exc:
                    request.update(
                        status="blocked", error=str(exc), finished_at=dispatch._now()
                    )
                    return True
                try:
                    _verify_asset_files(store, plan_id, data)
                except ValueError as exc:
                    request.update(
                        status="blocked", error=str(exc), finished_at=dispatch._now()
                    )
                    return True
                request.update(
                    status="running",
                    started_at=dispatch._now(),
                    lease_id=str(uuid.uuid4()),
                )
            try:
                result = dispatch.request_render(plans, studio, plan_id, revision)
                state = "succeeded" if result["state"] == "succeeded" else "failed"
                error = (
                    None
                    if state == "succeeded"
                    else "; ".join(result.get("reasons", [])) or result["state"]
                )
            except (
                Exception
            ) as exc:  # Isolate one CPU request; never silently retry a partial render.
                log.exception("creative render request %s failed", request_id)
                state, error = "failed", f"{type(exc).__name__}: {exc}"
            with store.tx(plan_id) as data:
                request = dispatch._record(data, revision)["render_request"]
                request.update(status=state, error=error, finished_at=dispatch._now())
            return True
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


async def consume_once(*, plans=None, studio=None):
    if queued_work.draining() or queued_work.delegated():
        return False
    plans = plans or default_plan_store()
    requests = await asyncio.to_thread(_requests, plans)
    if not requests:
        return False
    studio = studio or default_studio()
    for plan_id, revision, request_id in requests:
        if queued_work.draining():
            return False
        if await queued_work.run_tracked(
            asyncio.to_thread(execute, plans, studio, plan_id, revision, request_id)
        ):
            return True
    return False
