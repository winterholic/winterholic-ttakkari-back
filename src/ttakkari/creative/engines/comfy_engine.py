"""Comfy 어댑터: G1 managed 경로(`ComfyManagedController`)를 G0 attempt 흐름에 붙인다.

이 어댑터는 Comfy 서버를 직접 띄우거나 HTTP를 부르지 않는다. 등록된 managed profile만 컨트롤러에 맡기고, 공용 host resource lease는
컨트롤러가 소유한다. 생성 제출은 재시도하지 않는다(ADR §6.3). 같은 idempotency_key로 다시 실행되면(crash 재개) 저장해 둔 task를
이어서 관측·reconcile하고 새 제출을 하지 않는다. 이 세션에서는 실제 생성을 하지 않았고, 계약 시험은 컨트롤러 대역으로만 했다.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.creative.engines.base import (
    EngineAdapter,
    EngineEmptyOutput,
    EngineError,
    EngineOutput,
    EngineRun,
    ExecutionContext,
    ProbeResult,
    ProfileSpec,
    apply_defaults,
    now_iso,
    validate_params,
)

TERMINAL = {"succeeded", "failed", "cancelled", "needs_reconcile"}
POLL_SECONDS = 1.0

PROFILES = {
    "wan22-5b-draft": ProfileSpec(
        "wan22-5b-draft",
        frozenset({"generative-video", "t2v"}),
        "Wan2.2 TI2V 5B 초안 T2V(managed Comfy 고정 profile). 길이·해상도는 profile이 정하며 프레임 정확성을 보장하지 않는다.",
        "video",
        resource_family="gpu",
        params={"prompt": {"type": "str", "required": True, "max_len": 1000}},
        frame_exact=False,
    )
}


class ComfyAdapter(EngineAdapter):
    engine_id = "comfy"
    display_name = "ComfyUI managed 생성(G1)"
    profiles = PROFILES
    rights: ClassVar[dict[str, str]] = {
        "license": "Wan2.2 Apache-2.0 표기 확인(설치된 encoder/LoRA 포함 범위는 pending)",
        "use_scope": "personal",
    }

    def __init__(
        self, controller=None, map_dir: Path | None = None, *, mock: bool = False
    ) -> None:
        # mock=True는 컨트롤러가 대역인 계약 시험용이다. receipt에 mock으로 남고 자동 선택·검증 단계 상승에 쓰이지 않는다.
        self._controller = controller
        self._map_dir = Path(map_dir) if map_dir else None
        self.execution_mode = "mock" if mock else "real"

    @property
    def controller(self):
        if self._controller is None:
            from ttakkari.config import get_settings
            from ttakkari.managed_comfy import default_controller

            self._controller = default_controller(get_settings().data_dir)
        return self._controller

    @property
    def map_dir(self) -> Path:
        if self._map_dir is None:
            from ttakkari.config import get_settings

            self._map_dir = get_settings().home / "creative-studio" / "comfy-map"
        self._map_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self._map_dir

    # ---- probe: 파일만 읽는다. 서버를 띄우거나 포트에 접속하지 않는다.
    def probe(self) -> ProbeResult:
        try:
            from ttakkari import managed_comfy
            from ttakkari.managed_comfy_receipt import comfy_runtime, model_entry

            profile = managed_comfy.PROFILES["wan22-5b-draft"]
            models_root = managed_comfy.COMFY_ROOT / "ComfyUI" / "models"
            models = [
                model_entry(name, models_root, hash_limit=0) for name in profile.models
            ]
            runtime = comfy_runtime(managed_comfy.COMFY_ROOT)
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                "probe_failed",
                "host-managed",
                None,
                None,
                now_iso(),
                error=f"{type(exc).__name__}: {exc}",
            )
        missing = [m["name"] for m in models if not m.get("found")]
        details = {
            "profile_revision": profile.revision,
            "models_found": len(models) - len(missing),
            "models_missing": missing,
            "runtime": runtime,
            "runtime_started": False,
            "note": "파일 존재만 확인했다. managed runtime을 시작하지 않았으므로 available로 올리지 않는다(G2 smoke 필요).",
        }
        if missing:
            return ProbeResult(
                "absent",
                "host-managed",
                str(managed_comfy.COMFY_ROOT),
                profile.revision,
                now_iso(),
                details,
                error="필수 모델 파일이 없습니다: " + ", ".join(missing),
            )
        return ProbeResult(
            "present",
            "host-managed",
            str(managed_comfy.COMFY_ROOT),
            profile.revision,
            now_iso(),
            details,
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        profile = self.profiles.get(spec.get("profile", ""))
        if profile is None:
            return ["등록되지 않은 managed profile입니다."]
        errors = validate_params(profile.params, spec.get("params") or {})
        seed = spec.get("seed")
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= 2**32 - 1
        ):
            errors.append("seed는 0~2^32-1 정수여야 합니다(Comfy 재현 키).")
        if "idempotency_key" in spec and not spec["idempotency_key"]:
            errors.append("idempotency_key가 비어 있습니다.")
        return errors

    # ---- task 매핑: 제출 전에 영속 저장한다.
    def _map_path(self, key: str) -> Path:
        return self.map_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _load_task(self, key: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._map_path(key).read_text())
        except FileNotFoundError:
            return None

    def _save_task(self, key: str, record: dict[str, Any]) -> None:
        path = self._map_path(key)
        fd, name = tempfile.mkstemp(prefix=".task.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def reconcile(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        record = self._load_task(spec.get("idempotency_key", ""))
        if record is None:
            return None
        try:
            job = self.controller.get(record["task_id"])
        except FileNotFoundError:
            return {"task_id": record["task_id"], "status": "missing"}
        if job.get("status") == "needs_reconcile":
            job = self.controller.reconcile(
                record["task_id"], owner_task=record["task_id"]
            )
        return job

    def cancel(self, spec: dict[str, Any]) -> None:
        record = self._load_task(spec.get("idempotency_key", ""))
        if record is not None:
            self.controller.cancel(record["task_id"], owner_task=record["task_id"])

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        spec = ctx.spec
        errors = self.validate(spec)
        if errors:
            raise EngineError("invalid_spec", "; ".join(errors))
        profile = self.profiles[spec["profile"]]
        params = apply_defaults(profile.params, spec.get("params") or {})
        key = spec.get("idempotency_key")
        if not key:
            raise EngineError(
                "invalid_spec",
                "idempotency_key가 필요합니다(crash 재개 시 중복 제출 방지).",
            )
        ctx.check_cancel()
        started = time.monotonic()
        record = self._load_task(key)
        resumed = record is not None
        if record is None:
            # lease를 얻으면서 task가 만들어진다. 응답을 못 받아도 저장된 key로 대조할 수 있도록 start 전에 기록한다.
            job = self.controller.prepare(
                spec["profile"], prompt=params["prompt"], seed=spec["seed"]
            )
            record = {
                "task_id": job["task_id"],
                "profile": spec["profile"],
                "seed": spec["seed"],
                "prepared_at": now_iso(),
            }
            self._save_task(key, record)
            self.controller.start(job["task_id"])
            ctx.emit("running")
        task_id = record["task_id"]
        job = self.controller.get(task_id)
        while job.get("status") not in TERMINAL:
            if ctx.cancelled():
                try:
                    self.controller.cancel(task_id, owner_task=task_id)
                finally:
                    self._finish_cancel(task_id)
                raise EngineError(
                    "cancelled", "취소 요청으로 Comfy 작업을 취소했습니다."
                )
            if time.monotonic() - started > ctx.timeout_seconds:
                raise EngineError(
                    "timeout",
                    "Comfy 작업이 제한 시간 안에 끝나지 않았습니다. 재제출하지 않고 reconcile이 필요합니다.",
                    retryable=False,
                )
            time.sleep(POLL_SECONDS)
            job = self.controller.get(task_id)
        if job["status"] == "needs_reconcile":
            job = self.controller.reconcile(task_id, owner_task=task_id)
        if job["status"] == "cancelled":
            raise EngineError("cancelled", "Comfy 작업이 취소됐습니다.")
        if job["status"] != "succeeded":
            raise EngineError(
                "comfy_failed",
                f"Comfy 작업 상태 {job['status']}: {job.get('error') or (job.get('result') or {}).get('failure')}",
            )
        receipt = job.get("receipt")
        if not receipt:
            raise EngineError(
                "receipt_missing",
                f"managed Comfy receipt가 없습니다: {job.get('receipt_error')}",
            )
        if not receipt.get("graph_hash_consistent") or not receipt.get(
            "seed_consistent"
        ):
            raise EngineError(
                "receipt_inconsistent",
                "resolved graph 또는 seed가 저장된 요청과 일치하지 않습니다.",
            )
        outputs: list[EngineOutput] = []
        for entry in receipt.get("outputs") or []:
            if entry.get("intermediate"):
                continue
            source = Path(entry["source_path"])
            data = source.read_bytes()
            if not data:
                raise EngineEmptyOutput(
                    f"Comfy 출력 {entry.get('name')}이 비어 있습니다."
                )
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise EngineError(
                    "collect_failed",
                    f"Comfy 출력 {entry.get('name')}의 해시가 receipt와 다릅니다.",
                )
            outputs.append(
                EngineOutput(
                    str(entry["name"]),
                    str(entry.get("media_type") or "video/mp4"),
                    data,
                    "primary" if not outputs else "aux",
                )
            )
        if not outputs:
            raise EngineEmptyOutput(
                "managed Comfy가 성공했지만 최종 출력 파일이 없습니다."
            )
        return EngineRun(
            outputs,
            metrics={
                "wall_seconds": round(time.monotonic() - started, 3),
                "peak_rss_gb": receipt.get("peak_rss_gb"),
                "resumed_existing_task": resumed,
            },
            provenance={
                "comfy_task_id": task_id,
                "external_prompt_id": receipt.get("external_prompt_id"),
                "resolved_graph_sha256": receipt.get("resolved_graph_sha256"),
                "seed": receipt.get("seed"),
                "model_hashes": receipt.get("model_hashes"),
                "runtime": receipt.get("runtime"),
                "lease": receipt.get("lease"),
                "workflow_revision_hash": receipt.get("workflow_revision_hash"),
            },
        )

    def _finish_cancel(self, task_id: str) -> None:
        """취소 직후 상태를 한 번 더 읽어 최종 상태 확인 전에 성공으로 오인하지 않게 한다."""
        for _ in range(5):
            if self.controller.get(task_id).get("status") in TERMINAL:
                return
            time.sleep(POLL_SECONDS)
