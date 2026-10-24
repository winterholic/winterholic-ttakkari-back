"""승인된 v2 계획의 생성 자산을 검증해 production store에 등록한다.

이 계층은 `director.py`, `pipeline.py`, `managed_comfy.py`를 수정하지 않고 생성기를 주입받는다.
생성기가 scene·audio 전체를 제공하지 못하면 일부만 만든 척 성공하지 않고 실패 기록과 blocker를 남긴다.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ttakkari.creative.v2.dispatch import ProductionStore, put_asset
from ttakkari.creative.v2.plans import PlanStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _key(plan: dict[str, Any], generator: AssetGenerator) -> str:
    payload = {
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "plan_sha256": plan.get("approval", {}).get("snapshot", {}).get("plan_sha256"),
        "recipe": plan["match"].get("primary_recipe"),
        "engine": generator.engine_id,
        "version": generator.version,
        "scenes": [
            {"id": s["scene_id"], "title": s["title"], "narration": s["narration"]}
            for s in plan["scenes"]
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


class AssetGenerationError(ValueError):
    """계획을 자동 생성 자산으로 채울 수 없는 이유."""


class AssetGenerationBusy(AssetGenerationError):
    """같은 생성 키를 다른 실행자가 살아서 돌리고 있다. 요청 기록은 건드리지 않는다."""


class AssetGenerator(Protocol):
    engine_id: str
    version: str

    def supported_recipes(self) -> set[str]: ...

    def blockers(self, plan: dict[str, Any]) -> list[str]: ...

    def generate(
        self,
        plan: dict[str, Any],
        *,
        workdir: Path,
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]: ...


def _workdir(store: ProductionStore, plan_id: str, key: str) -> Path:
    return store.folder(plan_id) / "generation-work" / key


def _owner_is_alive(workdir: Path) -> bool:
    """생성 실행자는 workdir 의 generation.lock 을 잡고 있다. 프로세스가 죽으면 커널이 풀어 주므로 살아 있는지 알 수 있다."""
    lock_path = workdir / "generation.lock"
    if not lock_path.is_file():
        return False
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def enqueue_assets(plans: PlanStore, plan_id: str, revision: int, generator: AssetGenerator) -> str:
    """API 가 하는 일의 전부: 승인 revision 의 자산 생성 요청을 production.json 에 queued 로 남기고 현재 상태를 돌려준다.

    실행은 하지 않는다. 같은 요청이 이미 대기·실행 중이면 새 기록을 만들지 않고, 이미 성공한 같은 키는 아무것도 하지 않는다
    (기존 응답 계약: queued). 소비자가 맡지 않은 채 남은 running 기록은 사용자가 다시 요청할 때만 같은 기록으로 이어 대기시킨다.
    """
    try:
        plan = plans.assert_renderable(plan_id, revision)
    except (ValueError, FileNotFoundError) as exc:
        raise AssetGenerationError(f"승인된 현재 revision만 생성할 수 있습니다: {exc}") from exc
    store = ProductionStore(plans)
    key = _key(plan, generator)
    with store.tx(plan_id) as data:
        generations = data.setdefault("asset_generations", [])
        existing = next((g for g in generations if g["key"] == key), None)
        if existing and existing["status"] in {"queued", "succeeded"}:
            return "queued"
        if existing and existing["status"] == "running" and _owner_is_alive(_workdir(store, plan_id, key)):
            return "running"
        record = {
            "id": existing["id"] if existing and existing["status"] == "running" else str(uuid.uuid4()),
            "key": key,
            "plan_revision": revision,
            "recipe_id": plan["match"].get("primary_recipe"),
            "engine": {"id": generator.engine_id, "version": generator.version},
            "status": "queued",
            "requested_at": _now(),
            "started_at": None,
            "finished_at": None,
            "progress": list(existing.get("progress", [])) if existing else [],
            "assets": {},
            "receipt": None,
            "error": None,
        }
        if existing:
            generations[generations.index(existing)] = record
        else:
            generations.append(record)
    return "queued"


def queued_requests(plans: PlanStore) -> list[dict[str, Any]]:
    """대기 중인 요청을 요청 순서로 돌려준다. queued 만 보며 running 이던 기록은 후보가 아니다."""
    found: list[dict[str, Any]] = []
    for folder in sorted(plans.root.iterdir()):
        path = folder / "production.json"
        if not path.is_file():
            continue
        try:
            raw = path.read_text()
            if '"queued"' not in raw:
                continue
            data = json.loads(raw)
            plan_id = str(uuid.UUID(folder.name))
        except (OSError, ValueError):
            continue
        for item in data.get("asset_generations", []):
            if item.get("status") == "queued":
                found.append({
                    "plan_id": plan_id, "revision": item["plan_revision"], "id": item["id"],
                    "requested_at": item.get("requested_at") or "",
                })
    return sorted(found, key=lambda r: (r["requested_at"], r["plan_id"]))


def fail_if_queued(plans: PlanStore, plan_id: str, record_id: str, error: str) -> bool:
    """실행이 시작되지 못한 대기 요청을 실패로 닫는다. 맨 앞 요청이 영원히 소비자를 막지 못하게 한다."""
    store = ProductionStore(plans)
    with store.tx(plan_id) as data:
        for item in data.get("asset_generations", []):
            if item["id"] == record_id and item["status"] == "queued":
                item.update(status="failed", finished_at=_now(), error=error)
                return True
    return False


def generate_assets(
    plans: PlanStore,
    plan_id: str,
    revision: int,
    generator: AssetGenerator,
) -> dict[str, Any]:
    """현재 승인 revision에 scene 이미지와 전체 길이 오디오 mix를 만들고 자산·receipt를 기록한다."""
    try:
        plan = plans.assert_renderable(plan_id, revision)
    except (ValueError, FileNotFoundError) as exc:
        raise AssetGenerationError(f"승인된 현재 revision만 생성할 수 있습니다: {exc}") from exc
    store = ProductionStore(plans)
    generation_key = _key(plan, generator)
    workdir = store.folder(plan_id) / "generation-work" / generation_key
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workdir / "generation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AssetGenerationBusy("같은 계획 revision의 자산 생성 작업이 이미 실행 중입니다.") from exc
        try:
            return _generate_assets_locked(plans, plan_id, revision, generator)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _generate_assets_locked(
    plans: PlanStore,
    plan_id: str,
    revision: int,
    generator: AssetGenerator,
) -> dict[str, Any]:
    try:
        plan = plans.assert_renderable(plan_id, revision)
    except (ValueError, FileNotFoundError) as exc:
        raise AssetGenerationError(f"승인된 현재 revision만 생성할 수 있습니다: {exc}") from exc

    recipe_id = plan["match"].get("primary_recipe")
    generation_key = _key(plan, generator)
    store = ProductionStore(plans)
    record_id = str(uuid.uuid4())
    workdir = store.folder(plan_id) / "generation-work" / generation_key
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with store.tx(plan_id) as data:
        generations = data.setdefault("asset_generations", [])
        existing = next((g for g in generations if g["key"] == generation_key), None)
        if existing is None:
            # API 가 접수한 뒤 실행부가 새 엔진 버전으로 교체되면 키가 달라진다. 같은 revision 의 대기 요청은 현재 엔진이 이어받는다.
            existing = next(
                (g for g in generations if g["status"] == "queued" and g["plan_revision"] == revision), None
            )
        if existing and existing["status"] == "succeeded":
            return existing
        if existing and existing["status"] in {"running", "queued"}:
            record_id = existing["id"]
        generation = {
            "id": record_id,
            "key": generation_key,
            "plan_revision": revision,
            "recipe_id": recipe_id,
            "engine": {"id": generator.engine_id, "version": generator.version},
            "status": "running",
            "requested_at": existing.get("requested_at") if existing else None,
            "started_at": _now(),
            "finished_at": None,
            "progress": list(existing.get("progress", [])) if existing else [],
            "assets": {},
            "receipt": None,
            "error": None,
        }
        if existing:
            generations[generations.index(existing)] = generation
        else:
            generations.append(generation)

    try:
        if recipe_id not in generator.supported_recipes():
            raise AssetGenerationError(
                f"레시피 {recipe_id or '미지정'}는 자동 자산 생성 엔진 {generator.engine_id}을 지원하지 않습니다."
            )
        blockers = generator.blockers(plan)
        if blockers:
            raise AssetGenerationError("자동 자산 생성 차단: " + " / ".join(blockers))

        def record_progress(event: dict[str, Any]) -> None:
            safe_event = {**event, "at": _now()}
            with store.tx(plan_id) as data:
                current_generation = next(
                    (
                        item
                        for item in data.setdefault("asset_generations", [])
                        if item["key"] == generation_key and item["id"] == record_id
                    ),
                    None,
                )
                if current_generation is None or current_generation["status"] != "running":
                    raise AssetGenerationError("자산 생성 기록이 실행 도중 바뀌었습니다.")
                current_generation.setdefault("progress", []).append(safe_event)

        generated = generator.generate(plan, workdir=workdir, progress=record_progress)
        # 승인·revision은 생성 중 바뀔 수 있다. 시작 때의 계획과 다르면 결과를 공개하지 않는다.
        current = plans.assert_renderable(plan_id, revision)
        if current.get("approval", {}).get("snapshot", {}).get("plan_sha256") != plan.get(
            "approval", {}
        ).get("snapshot", {}).get("plan_sha256"):
            raise AssetGenerationError("자산 생성 중 승인된 계획 snapshot이 바뀌었습니다.")

        expected = {scene["scene_id"] for scene in plan["scenes"]}
        scenes = generated.get("scenes")
        if not isinstance(scenes, dict) or set(scenes) != expected:
            missing = sorted(expected - set(scenes or {}))
            extra = sorted(set(scenes or {}) - expected)
            raise AssetGenerationError(
                f"생성 장면 자산 집합이 계획과 다릅니다. 누락={missing}, 초과={extra}"
            )
        music = generated.get("music")
        if not isinstance(music, dict):
            raise AssetGenerationError("전체 길이 음악·음성 mix 자산이 없습니다.")
        voices = generated.get("voices")
        if not isinstance(voices, dict) or set(voices) != expected:
            missing = sorted(expected - set(voices or {}))
            extra = sorted(set(voices or {}) - expected)
            raise AssetGenerationError(
                f"장면별 음성 자산 집합이 계획과 다릅니다. 누락={missing}, 초과={extra}"
            )
        receipt = generated.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("execution_mode") != "real":
            raise AssetGenerationError("실제 생성 engine receipt가 없습니다.")
        for asset in [*scenes.values(), *voices.values(), music]:
            path = Path(asset["path"])
            if not path.is_file() or path.stat().st_size == 0:
                raise AssetGenerationError(f"생성 자산이 없거나 비어 있습니다: {path.name}")

        saved_scenes: dict[str, Any] = {}
        for scene in plan["scenes"]:
            scene_id = scene["scene_id"]
            asset = scenes[scene_id]
            saved = put_asset(
                plans,
                plan_id,
                kind="scene",
                scene_id=scene_id,
                source=Path(asset["path"]),
                name=str(asset.get("name") or f"{scene_id}.png"),
                meta={**(asset.get("meta") or {}), "generation_id": record_id},
            )
            saved_scenes[scene_id] = {k: saved[k] for k in ("id", "sha256", "file", "name", "kind", "meta")}
        saved_voices: dict[str, Any] = {}
        for scene in plan["scenes"]:
            scene_id = scene["scene_id"]
            asset = voices[scene_id]
            saved = put_asset(
                plans,
                plan_id,
                kind="voice",
                scene_id=scene_id,
                source=Path(asset["path"]),
                name=str(asset.get("name") or f"{scene_id}-voice.wav"),
                meta={**(asset.get("meta") or {}), "generation_id": record_id},
            )
            saved_voices[scene_id] = {k: saved[k] for k in ("id", "sha256", "file", "name", "kind", "meta")}
        saved_music = put_asset(
            plans,
            plan_id,
            kind="music",
            scene_id=None,
            source=Path(music["path"]),
            name=str(music.get("name") or "generated-mix.wav"),
            meta={**(music.get("meta") or {}), "generation_id": record_id},
        )
        stored_generation = {
            "id": record_id,
            "key": generation_key,
            "plan_revision": revision,
            "recipe_id": recipe_id,
            "engine": {"id": generator.engine_id, "version": generator.version},
            "status": "succeeded",
            "requested_at": generation["requested_at"],
            "started_at": generation["started_at"],
            "finished_at": _now(),
            "progress": [],
            "assets": {
                "scenes": saved_scenes,
                "voices": saved_voices,
                "music": {k: saved_music[k] for k in ("id", "sha256", "file", "name", "kind", "meta")},
            },
            "receipt": receipt,
            "error": None,
        }
        with store.tx(plan_id) as data:
            generations = data.setdefault("asset_generations", [])
            index = next((i for i, g in enumerate(generations) if g["key"] == generation_key), None)
            if index is None:
                raise AssetGenerationError("생성 작업 기록이 사라졌습니다.")
            stored_generation["progress"] = generations[index].get("progress", [])
            generations[index] = stored_generation
        return stored_generation
    except Exception as exc:
        with store.tx(plan_id) as data:
            generations = data.setdefault("asset_generations", [])
            for item in generations:
                if item["key"] == generation_key and item["id"] == record_id:
                    item.update(status="failed", finished_at=_now(), error=f"{type(exc).__name__}: {exc}")
                    break
        if isinstance(exc, AssetGenerationError):
            raise
        raise AssetGenerationError(f"자산 생성에 실패했습니다: {type(exc).__name__}: {exc}") from exc
