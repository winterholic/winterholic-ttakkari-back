"""엔진 어댑터를 G0 attempt/receipt 흐름에 붙이는 제작 파이프라인.

흐름: 샷 revision → render spec(CAS) → `submit_attempt`(spec 전문이 resolved_graph, spec sha가 입력) → `execute_attempt`
→ 어댑터 실행 → 출력 + engine receipt(마지막 출력)가 한 번에 CAS에 모인다 → 검수.
engine receipt를 출력의 일부로 두는 이유: G0가 출력과 receipt를 같은 원자 단계로 수집하므로 crash 뒤에도 출력만 있고 receipt가 없는 상태가 생기지 않는다.
오디오 선행 합성(TTS)과 최종 조립은 샷 attempt가 아니므로 `run_unattached`로 같은 어댑터·receipt 규칙을 쓰되 sidecar에 기록한다(멱등 키).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ttakkari.creative.director import ConflictError, DirectorStore
from ttakkari.creative.engines import media
from ttakkari.creative.engines.base import (
    EngineAdapter,
    EngineCancelled,
    EngineEmptyOutput,
    EngineError,
    ExecutionContext,
    now_iso,
)
from ttakkari.creative.engines.registry import EngineRegistry, RegistryError
from ttakkari.creative.review import Reviewer, find_attempt_review

SPEC_SCHEMA = "ttakkari.render-spec.v1"
PARAMS_SCHEMA = "ttakkari.shot-params.v1"
RECEIPT_SCHEMA = "ttakkari.engine-receipt.v1"
DEFAULT_SIZE = (1280, 720)


def _input_assets(state: dict, shas: list[str]) -> list[dict[str, str]]:
    """샷의 입력 자산을 이 제작 manifest의 (이름, sha256)로 확정한다. manifest에 없는 sha는 거부한다(다른 제작·임의 파일 차단)."""
    by_sha: dict[str, str] = {}
    for name, record in state["artifacts"].items():
        by_sha.setdefault(record["sha256"], name)
    found = []
    for sha in shas:
        if sha not in by_sha:
            raise PipelineError("입력 자산 sha256이 이 제작의 자산 목록에 없습니다.")
        found.append({"sha256": sha, "name": by_sha[sha], "role": "source"})
    return found


class PipelineError(ValueError):
    """사용자에게 그대로 보일 수 있는 입력·상태 오류(API에서 422)."""


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProductionPipeline:
    def __init__(
        self,
        store: DirectorStore,
        registry: EngineRegistry,
        *,
        work_root: Path | None = None,
        max_concurrent_runs: int = 1,
    ) -> None:
        self.store = store
        self.registry = registry
        self.work_root = (
            Path(work_root) if work_root else store.root.parent / "engine-work"
        )
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.reviewer = Reviewer(store, self.work_root / "review")
        self._cancel_cache: dict[tuple[str, str], tuple[float, bool]] = {}
        # Remotion·Three는 Chrome까지 합쳐 GB급 RSS를 쓴다. 호스트 CPU/RAM 예산 때문에 기본은 동시에 하나만 실행한다.
        self._slots = threading.BoundedSemaphore(max_concurrent_runs)

    # ---- 읽기 도우미
    def params_doc(self, pid: str, shot: dict) -> dict[str, Any]:
        """샷 revision의 첫 입력 자산이 shot-params 문서이면 읽는다. 없으면 빈 문서."""
        for sha in shot.get("input_sha256", [])[:1]:
            try:
                doc = json.loads(self.store.read_artifact(pid, sha))
            except (FileNotFoundError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("schema") == PARAMS_SCHEMA:
                return doc
        return {
            "schema": PARAMS_SCHEMA,
            "params": {},
            "selection": "fixed",
            "reason": "",
            "source": "none",
        }

    def put_params(
        self, pid: str, *, params: dict, selection: str, reason: str, source: str
    ) -> str:
        if selection not in {"auto", "explicit-lab", "fixed"}:
            raise PipelineError("selection 값이 잘못됐습니다.")
        doc = {
            "schema": PARAMS_SCHEMA,
            "params": params,
            "selection": selection,
            "reason": reason,
            "source": source,
        }
        body = canonical(doc)
        record = self.store.put_artifact(
            pid,
            f"shot-params-{sha256_hex(body)[:16]}.json",
            body,
            media_type="application/json",
        )
        return record["sha256"]

    @staticmethod
    def build_spec_core(state: dict, shot: dict, params_doc: dict) -> dict[str, Any]:
        bible = state["bibles"][-1]
        timeline = state["timelines"][-1]
        start, end = shot["start_f"], shot["end_f"]
        size = bible.get("composition", {}).get("size") or list(DEFAULT_SIZE)
        cues = [
            {
                "frame": c["frame"] - start,
                "timeline_frame": c["frame"],
                "kind": c.get("kind", "cue"),
                "label": c.get("label", ""),
            }
            for c in timeline.get("cues", [])
            if isinstance(c.get("frame"), int)
            and start <= c["frame"] < end
            and c.get("kind") != "allow_silence"
        ]
        return {
            "schema": SPEC_SCHEMA,
            "production_id": state["manifest"]["production_id"],
            "shot_id": shot["shot_id"],
            "shot_revision": shot["revision"],
            "bible_revision": bible["revision"],
            "bible_hash": bible["content_hash"],
            "timeline_revision": timeline["revision"],
            "timeline_hash": timeline["content_hash"],
            "engine": shot["engine"],
            "profile": shot["profile"],
            "params": params_doc.get("params", {}),
            "selection": params_doc.get("selection", "fixed"),
            "range": {"start_f": start, "end_f": end, "frames": end - start},
            "fps": {"num": timeline["fps_num"], "den": timeline["fps_den"]},
            "sample_rate": timeline["sample_rate"],
            "format": {"width": int(size[0]), "height": int(size[1])},
            "bible": {
                "palette": bible["palette"],
                "typography": bible["typography"],
                "composition": bible["composition"],
                "camera": bible["camera"],
                "negative_rules": bible["negative_rules"],
            },
            "cues": cues,
            "seed": shot["seed"],
            "input_assets": _input_assets(state, shot["input_sha256"][1:]),
            "acceptance_criteria": shot["acceptance_criteria"],
            "expect_audio": bool(
                shot["engine"] in {"ffmpeg"}
                and (params_doc.get("params") or {}).get("audio") in {"silence", "tone"}
            ),
            "intent": shot["intent"],
        }

    def build_spec(
        self,
        pid: str,
        shot_id: str,
        *,
        shot: dict | None = None,
        params_doc: dict | None = None,
        suffix: str = "",
    ) -> dict[str, Any]:
        state = self.store.get_production(pid)
        if shot_id not in state["shots"]:
            raise PipelineError("샷이 없습니다.")
        row = shot or state["shots"][shot_id][-1]
        core = self.build_spec_core(state, row, params_doc or self.params_doc(pid, row))
        digest = sha256_hex(canonical(core))
        key = f"{shot_id}:r{core['shot_revision']}:b{core['bible_revision']}:t{core['timeline_revision']}:{digest[:16]}{suffix}"
        return {**core, "idempotency_key": key}

    def validate_spec(
        self, spec: dict[str, Any], *, require_auto: bool = False
    ) -> None:
        engine = spec["engine"]
        try:
            adapter = self.registry.adapter(engine)
        except RegistryError as exc:
            raise PipelineError(str(exc)) from exc
        if spec["profile"] not in adapter.profiles:
            raise PipelineError(
                f"{engine}에 등록되지 않은 프로필입니다: {spec['profile']}"
            )
        errors = adapter.validate(json.loads(canonical(spec)))
        if errors:
            raise PipelineError(
                f"{engine}/{spec['profile']} 검증 실패: " + "; ".join(errors)
            )
        if require_auto and spec.get("selection") == "auto":
            try:
                self.registry.assert_auto_choice(engine, spec["profile"])
            except RegistryError as exc:
                raise PipelineError(str(exc)) from exc

    # ---- 제출·실행
    def submit_shot(
        self, pid: str, shot_id: str, *, retry: bool = False
    ) -> dict[str, Any]:
        spec = self.build_spec(pid, shot_id)
        self.validate_spec(spec)
        state = self.store.get_production(pid)
        base_key = spec["idempotency_key"]
        suffix = ""
        if retry:
            failed = sum(
                1
                for a in state["attempts"]
                if a["idempotency_key"].split("#retry")[0] == base_key
                and a["state"] in {"failed", "cancelled"}
            )
            suffix = f"#retry{failed}"
            spec = {**spec, "idempotency_key": base_key + suffix}
        body = canonical(spec)
        record = self.store.put_artifact(
            pid,
            f"render-spec-{shot_id}-r{spec['shot_revision']}-{sha256_hex(body)[:12]}.json",
            body,
            media_type="application/json",
        )
        return self.store.submit_attempt(
            pid,
            shot_id,
            input_sha256=record["sha256"],
            idempotency_key=spec["idempotency_key"],
            resolved_graph=spec,
        )

    def execute(self, pid: str, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.execute_attempt(
            pid, attempt_id, renderer=self._renderer(pid, attempt_id)
        )
        if attempt["state"] == "review_pending":
            self.store.resolve_shot_stale(pid, attempt["shot_id"])
        return attempt

    def render_shot(
        self, pid: str, shot_id: str, *, retry: bool = False, review: bool = True
    ) -> dict[str, Any]:
        attempt = self.execute(
            pid, self.submit_shot(pid, shot_id, retry=retry)["attempt_id"]
        )
        if review and attempt["state"] == "review_pending":
            self.review(pid, attempt["attempt_id"])
            attempt = self._attempt(pid, attempt["attempt_id"])
        return attempt

    def resume(self, pid: str, *, review: bool = True) -> list[dict[str, Any]]:
        """crash로 queued/running/collecting에 남은 시도를 이어서 실행한다. 어댑터마다 자기 재개 규칙(예: Comfy task 재사용)을 가진다."""
        state = self.store.get_production(pid)
        if state["cancelled"]:
            return []
        pending = [
            a["attempt_id"]
            for a in state["attempts"]
            if a["state"] in {"queued", "running", "collecting"}
        ]
        done = [self.execute(pid, attempt_id) for attempt_id in pending]
        if review:
            for attempt in done:
                if attempt["state"] == "review_pending":
                    self.review(pid, attempt["attempt_id"])
        return [self._attempt(pid, a["attempt_id"]) for a in done]

    def review(
        self, pid: str, attempt_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        attempt = self._attempt(pid, attempt_id)
        spec = json.loads(self.store.read_artifact(pid, attempt["input_sha256"]))
        profile = self.registry.adapter(spec["engine"]).profiles[spec["profile"]]
        return self.reviewer.review_attempt(
            pid, attempt_id, profile_exact=profile.frame_exact, force=force
        )

    def _attempt(self, pid: str, attempt_id: str) -> dict[str, Any]:
        state = self.store.get_production(pid)
        found = next(
            (a for a in state["attempts"] if a["attempt_id"] == attempt_id), None
        )
        if found is None:
            raise FileNotFoundError("시도가 없습니다.")
        return found

    def _cancelled(self, pid: str, attempt_id: str | None) -> bool:
        key = (pid, attempt_id or "")
        cached = self._cancel_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < 0.25:
            return cached[1]
        state = self.store.get_production(pid)
        flag = bool(state["cancelled"])
        if attempt_id and not flag:
            flag = any(
                a["attempt_id"] == attempt_id and a["state"] == "cancelled"
                for a in state["attempts"]
            )
        self._cancel_cache[key] = (now, flag)
        return flag

    def _renderer(self, pid: str, attempt_id: str):
        def render(spec_bytes: bytes) -> list[bytes]:
            spec = json.loads(spec_bytes)
            outputs, _receipt = self._run_adapter(pid, spec, attempt_id=attempt_id)
            return outputs

        return render

    def _run_adapter(
        self, pid: str, spec: dict[str, Any], *, attempt_id: str | None
    ) -> tuple[list[bytes], dict[str, Any]]:
        """어댑터를 실행하고 [출력..., engine receipt] 바이트 목록을 만든다. 실패는 예외로 올려 G0가 failed로 기록하게 한다."""
        adapter = self.registry.adapter(spec["engine"])
        errors = adapter.validate(json.loads(canonical(spec)))
        if errors:
            raise PipelineError("; ".join(errors))
        self._acquire_slot(pid, attempt_id)
        try:
            return self._run_in_slot(pid, spec, attempt_id, adapter)
        finally:
            self._slots.release()

    def _acquire_slot(self, pid: str, attempt_id: str | None) -> None:
        """엔진 실행 슬롯을 얻는다. 기다리는 동안에도 취소를 확인해 취소된 시도가 슬롯을 붙잡지 않게 한다."""
        while not self._slots.acquire(timeout=0.2):
            if self._cancelled(pid, attempt_id):
                raise EngineCancelled("슬롯 대기 중 취소 요청을 받았습니다.")
        if self._cancelled(pid, attempt_id):
            self._slots.release()
            raise EngineCancelled()

    def _run_in_slot(
        self,
        pid: str,
        spec: dict[str, Any],
        attempt_id: str | None,
        adapter: EngineAdapter,
    ) -> tuple[list[bytes], dict[str, Any]]:
        workdir = Path(tempfile.mkdtemp(dir=self.work_root, prefix="run-"))

        def emit(state_name: str) -> None:
            if attempt_id is None:
                return
            current = self._attempt(pid, attempt_id)
            sequence = current["last_event_sequence"] + 1
            self.store.ingest_event(
                pid,
                attempt_id,
                event_id=f"engine:{attempt_id}:{state_name}",
                sequence=sequence,
                state=state_name,
            )

        # 어댑터에는 사본을 주고, 실행 뒤 사본이 요청과 같은지 확인해 receipt가 실제 요청만 가리키게 한다.
        spec_bytes = canonical(spec)
        ctx = ExecutionContext(
            spec=json.loads(spec_bytes),
            workdir=workdir,
            cancelled=lambda: self._cancelled(pid, attempt_id),
            read_asset=lambda sha: self.store.read_artifact(pid, sha),
            emit=emit,
        )
        started_at = now_iso()
        t0 = time.monotonic()
        try:
            run = adapter.execute(ctx)
            if canonical(ctx.spec) != spec_bytes:
                raise EngineError(
                    "input_mutated",
                    "어댑터가 실행 입력 spec을 변경해 결과를 요청의 것으로 발행하지 않습니다.",
                )
            installation = self.registry.installation(spec["engine"])
            outputs = [o.data for o in run.outputs]
            if not run.outputs or any(not data for data in outputs):
                raise EngineEmptyOutput(
                    "엔진이 비었거나 0바이트인 출력을 돌려줬습니다."
                )
            receipt = self._receipt(
                adapter, spec, run, started_at, time.monotonic() - t0, installation
            )
        finally:
            adapter.release(ctx)
            shutil.rmtree(workdir, ignore_errors=True)
        return [*outputs, canonical(receipt)], receipt

    def _receipt(
        self,
        adapter: EngineAdapter,
        spec: dict[str, Any],
        run,
        started_at: str,
        wall: float,
        installation: dict,
    ) -> dict[str, Any]:
        entries = []
        with tempfile.TemporaryDirectory(dir=self.work_root, prefix="probe-") as tmp:
            for index, output in enumerate(run.outputs):
                entry: dict[str, Any] = {
                    "index": index,
                    "name": output.name,
                    "media_type": output.media_type,
                    "role": output.role,
                    "sha256": sha256_hex(output.data),
                    "bytes": len(output.data),
                    "media": None,
                }
                if output.media_type.startswith(("video/", "audio/")):
                    path = Path(tmp) / f"o{index}"
                    path.write_bytes(output.data)
                    try:
                        entry["media"] = media.probe_media(path)
                    except media.MediaToolError as exc:
                        entry["media_error"] = str(exc)
                entries.append(entry)
        return {
            "schema": RECEIPT_SCHEMA,
            "engine": spec["engine"],
            "profile": spec["profile"],
            "execution_mode": adapter.execution_mode,
            # mock이면 실제 생성·렌더 성공의 증거가 아니다. 계약 시험의 대역 실행이다.
            "real_execution": adapter.execution_mode == "real",
            "selection": spec.get("selection"),
            "idempotency_key": spec.get("idempotency_key"),
            "spec_sha256": sha256_hex(canonical(spec)),
            "started_at": started_at,
            "finished_at": now_iso(),
            "wall_seconds": round(wall, 3),
            "metrics": run.metrics,
            "provenance": run.provenance,
            "installation_at_run": {
                k: installation.get(k)
                for k in (
                    "state",
                    "revision",
                    "runtime_path",
                    "measured_at",
                    "environment",
                )
            },
            "outputs": entries,
        }

    # ---- 결과 읽기
    @staticmethod
    def attempt_receipt(attempt: dict[str, Any], read) -> dict[str, Any] | None:
        """attempt의 마지막 출력이 engine receipt이면 파싱해서 돌려준다."""
        if not attempt.get("outputs"):
            return None
        try:
            doc = json.loads(read(attempt["outputs"][-1]["sha256"]))
        except (ValueError, FileNotFoundError):
            return None
        return (
            doc
            if isinstance(doc, dict) and doc.get("schema") == RECEIPT_SCHEMA
            else None
        )

    def current_attempt(self, state: dict, shot_id: str) -> dict[str, Any] | None:
        revision = state["shots"][shot_id][-1]["revision"]
        rows = [
            a
            for a in state["attempts"]
            if a["shot_id"] == shot_id and a["shot_revision"] == revision
        ]
        return rows[-1] if rows else None

    def current_outputs(self, state: dict) -> dict[str, str]:
        """샷별 '현재' 출력 sha. 샷 revision뿐 아니라 바이블·타임라인 revision까지 지금과 같은 시도의 검수 대기 출력만 센다."""
        bible_revision = state["bibles"][-1]["revision"]
        timeline_revision = state["timelines"][-1]["revision"]
        result = {}
        for shot_id, rows in state["shots"].items():
            revision = rows[-1]["revision"]
            ok = [
                a
                for a in state["attempts"]
                if a["shot_id"] == shot_id
                and a["shot_revision"] == revision
                and a["bible_revision"] == bible_revision
                and a["timeline_revision"] == timeline_revision
                and a["state"] == "review_pending"
                and a["outputs"]
            ]
            if ok:
                result[shot_id] = ok[-1]["outputs"][0]["sha256"]
        return result

    # ---- 샷 수정·재제작
    def revise_shot(
        self,
        pid: str,
        shot_id: str,
        *,
        expected_revision: int,
        engine: str | None = None,
        profile: str | None = None,
        params: dict | None = None,
        drop_params: list[str] | None = None,
        seed: int | None = None,
        intent: str | None = None,
        acceptance_criteria: list[str] | None = None,
        selection: str = "explicit-lab",
        reason: str = "",
        source: str = "feedback",
    ) -> dict[str, Any]:
        """엔진·프로필(모델)·매개변수·seed를 바꾼 새 샷 revision을 만든다. 실행 전에 어댑터 검증을 통과해야 한다."""
        state = self.store.get_production(pid)
        if shot_id not in state["shots"]:
            raise PipelineError("샷이 없습니다.")
        current = state["shots"][shot_id][-1]
        if current["revision"] != expected_revision:
            raise ConflictError("expected_revision이 현재 샷 revision과 다릅니다.")
        new_engine = engine or current["engine"]
        new_profile = profile or current["profile"]
        old_doc = self.params_doc(pid, current)
        merged = (
            dict(old_doc.get("params", {}))
            if (new_engine, new_profile) == (current["engine"], current["profile"])
            else {}
        )
        merged.update(params or {})
        for key in drop_params or []:
            merged.pop(key, None)
        values: dict[str, Any] = {"engine": new_engine, "profile": new_profile}
        if seed is not None:
            values["seed"] = seed
        if intent is not None:
            values["intent"] = intent
        if acceptance_criteria is not None:
            values["acceptance_criteria"] = acceptance_criteria
        values["bible_hash"] = state["bibles"][-1]["content_hash"]
        values["timeline_hash"] = state["timelines"][-1]["content_hash"]
        candidate = {**current, **values}
        doc = {
            "schema": PARAMS_SCHEMA,
            "params": merged,
            "selection": selection,
            "reason": reason,
            "source": source,
        }
        spec = self.build_spec_core(state, candidate, doc)
        self.validate_spec(spec, require_auto=selection == "auto")
        sha = self.put_params(
            pid, params=merged, selection=selection, reason=reason, source=source
        )
        values["input_sha256"] = [sha, *current["input_sha256"][1:]]
        return self.store.update_shot(
            pid, shot_id, expected_revision=expected_revision, values=values
        )

    def _plan_specs(
        self, state: dict, plans: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for plan in plans:
            selection = plan.get("selection", "auto")
            doc = {
                "schema": PARAMS_SCHEMA,
                "params": plan.get("params", {}),
                "selection": selection,
                "reason": plan.get("reason", ""),
                "source": "director",
            }
            candidate = {
                "shot_id": plan["shot_id"],
                "revision": 1,
                "intent": plan.get("intent", ""),
                "start_f": plan["start_f"],
                "end_f": plan["end_f"],
                "engine": plan["engine"],
                "profile": plan["profile"],
                "input_sha256": ["", *plan.get("assets", [])],
                "seed": plan.get("seed"),
                "acceptance_criteria": plan.get("acceptance_criteria", []),
            }
            spec = self.build_spec_core(state, candidate, doc)
            self.validate_spec(spec, require_auto=selection == "auto")
            specs.append(spec)
        return specs

    def apply_dry_run(
        self, pid: str, expected_timeline_revision: int, plans: list[dict[str, Any]]
    ) -> None:
        """기록 없이 계획만 검증한다(감독 응답의 repair 판단용)."""
        state = self.store.get_production(pid)
        if state["timelines"][-1]["revision"] != expected_timeline_revision:
            raise ConflictError(
                "expected_timeline_revision이 현재 revision과 다릅니다."
            )
        self._plan_specs(state, plans)

    def apply_shot_plan(
        self, pid: str, expected_timeline_revision: int, plans: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """감독 계획을 검증한 뒤 G0 샷 계획으로 한 번에 기록한다. 하나라도 어댑터 검증이나 자동 선택 조건을 못 넘으면 아무것도 기록하지 않는다."""
        self.apply_dry_run(pid, expected_timeline_revision, plans)
        shots = []
        for plan in plans:
            sha = self.put_params(
                pid,
                params=plan.get("params", {}),
                selection=plan.get("selection", "auto"),
                reason=plan.get("reason", ""),
                source="director",
            )
            shots.append(
                {
                    "shot_id": plan["shot_id"],
                    "start_f": plan["start_f"],
                    "end_f": plan["end_f"],
                    "intent": plan.get("intent", ""),
                    "engine": plan["engine"],
                    "profile": plan["profile"],
                    "input_sha256": [sha, *plan.get("assets", [])],
                    "seed": plan.get("seed"),
                    "acceptance_criteria": plan.get("acceptance_criteria", []),
                }
            )
        return self.store.set_shot_plan(
            pid, expected_timeline_revision=expected_timeline_revision, shots=shots
        )

    def defective_shots(self, pid: str) -> list[dict[str, Any]]:
        """현재 revision 출력이 실패했거나 검수에서 fail인 샷과 이유. 취소된 시도는 결함이 아니다."""
        state = self.store.get_production(pid)
        found = []
        for shot_id in state["shots"]:
            attempt = self.current_attempt(state, shot_id)
            if attempt is None:
                continue
            if attempt["state"] == "failed":
                found.append(
                    {
                        "shot_id": shot_id,
                        "attempt_id": attempt["attempt_id"],
                        "kind": "attempt_failed",
                        "detail": attempt.get("error_kind"),
                    }
                )
            elif attempt["state"] == "review_pending" and attempt["outputs"]:
                match = find_attempt_review(state, attempt["attempt_id"])
                if match:
                    failing = sorted(
                        k for k, v in match["review"]["criteria"].items() if v == "fail"
                    )
                    if failing:
                        found.append(
                            {
                                "shot_id": shot_id,
                                "attempt_id": attempt["attempt_id"],
                                "kind": "review_failed",
                                "detail": failing,
                                "review_id": match["review"]["review_id"],
                            }
                        )
        return found

    # ---- 샷이 아닌 엔진 실행(오디오 선행 합성, 최종 조립)
    def run_unattached(
        self,
        pid: str,
        *,
        section: str,
        key: str,
        spec: dict[str, Any],
        extra: dict[str, Any] | None = None,
        retry_unknown_execution: bool = False,
    ) -> dict[str, Any]:
        """같은 요청만 재사용한다. 외부 실행이 불명확한 crash는 명시적 재시도 전까지 중단한다.

        identity·adapter 입력·sidecar는 호출 시점에 직렬화한 같은 요청 snapshot만 뜻한다. 호출자의 mutable
        객체는 이후 어디에도 쓰지 않고, adapter에는 매번 새 사본을 줘서 사본이 바뀌었는지 직렬화 비교로 확인한다.
        """
        spec_bytes = canonical(spec)
        snapshot_spec = json.loads(spec_bytes)
        snapshot_extra = json.loads(canonical(extra or {}))
        request_identity = sha256_hex(
            canonical(
                {
                    "section": section,
                    "key": key,
                    "spec": snapshot_spec,
                    "extra": snapshot_extra,
                }
            )
        )

        def existing_sidecar() -> dict[str, Any] | None:
            prior = self.store.get_production(pid).get(section, {}).get(key)
            if prior is None:
                return None
            if prior.get("request_identity_version") != 1:
                raise ConflictError(
                    "이전 실행 기록에는 요청 identity가 없어 자동 재사용하지 않습니다. 새 키 또는 수동 조정이 필요합니다."
                )
            if prior.get("request_identity") != request_identity:
                raise ConflictError(
                    "같은 unattached 키에 다른 요청 내용을 실행할 수 없습니다."
                )
            return prior

        # 확인→실행→기록을 잠금 안에서 처리하고, 사전 읽기도 요청 identity와 함께 검증한다.
        if prior := existing_sidecar():
            return prior
        with self.store.locked_unattached_execution(pid, section, key):
            state = self.store.get_production(pid)
            prior = state.get(section, {}).get(key)
            if prior is not None:
                if prior.get("request_identity_version") != 1:
                    raise ConflictError(
                        "이전 실행 기록에는 요청 identity가 없어 자동 재사용하지 않습니다. 새 키 또는 수동 조정이 필요합니다."
                    )
                if prior.get("request_identity") != request_identity:
                    raise ConflictError(
                        "같은 unattached 키에 다른 요청 내용을 실행할 수 없습니다."
                    )
                return prior
            execution = self.store.get_unattached_execution(pid, section, key)
            if execution and execution.get("request_identity") != request_identity:
                raise ConflictError(
                    "같은 unattached 키에 다른 요청 내용을 실행할 수 없습니다."
                )
            if execution and execution.get("status") == "completed":
                sidecar_value = execution["sidecar_value"]
                return self.store.record_sidecar(pid, section, key, sidecar_value)
            if state["cancelled"]:
                raise PipelineError("취소된 제작에는 새 실행을 할 수 없습니다.")
            if execution and execution.get("status") == "executing":
                if not retry_unknown_execution:
                    raise PipelineError(
                        "이전 엔진 실행의 완료 여부를 확인할 수 없습니다. 외부 상태를 확인한 뒤 retry_unknown_execution=True로 재시도를 명시하세요."
                    )
                attempt_number = int(execution.get("execution_attempts", 1)) + 1
            else:
                attempt_number = 1
            execution = self.store.write_unattached_execution(
                pid,
                section,
                key,
                {
                    "request_identity": request_identity,
                    "request_identity_version": 1,
                    "status": "executing",
                    "execution_attempts": attempt_number,
                },
            )
            adapter_spec = json.loads(spec_bytes)
            outputs, receipt = self._run_adapter(pid, adapter_spec, attempt_id=None)
            if canonical(adapter_spec) != spec_bytes:
                # 결과가 원 요청이 아니라 변경된 입력의 것일 수 있다. 발행하지 않고 checkpoint는 unknown으로 둔다.
                raise PipelineError(
                    "어댑터가 실행 입력 spec을 변경해 결과를 원 요청의 것으로 발행하지 않습니다."
                )
            stored = []
            for index, (output, entry) in enumerate(
                zip(outputs, receipt["outputs"] + [None], strict=False)
            ):
                output_sha = sha256_hex(output)
                if entry is None:
                    name, media_type = (
                        f"unattached-{request_identity}-{index}-{output_sha}.json",
                        "application/json",
                    )
                else:
                    name, media_type = (
                        f"unattached-{request_identity}-{index}-{output_sha}.bin",
                        entry["media_type"],
                    )
                stored.append(
                    self.store.put_artifact(pid, name, output, media_type=media_type)
                )
            sidecar_value = {
                **snapshot_extra,
                "engine": snapshot_spec["engine"],
                "profile": snapshot_spec["profile"],
                "spec": snapshot_spec,
                "outputs": stored,
                "receipt_sha256": stored[-1]["sha256"],
                "output_sha256": stored[0]["sha256"],
                "execution_mode": receipt["execution_mode"],
                "real_execution": receipt["real_execution"],
                "request_identity_version": 1,
                "request_identity": request_identity,
            }
            self.store.write_unattached_execution(
                pid,
                section,
                key,
                {
                    "request_identity": request_identity,
                    "request_identity_version": 1,
                    "status": "completed",
                    "execution_attempts": attempt_number,
                    "sidecar_value": sidecar_value,
                },
            )
            return self.store.record_sidecar(pid, section, key, sidecar_value)

    def generate_speech(
        self,
        pid: str,
        *,
        lines: list[str],
        voice: str = "F2",
        speed: float = 1.0,
        lang: str = "ko",
        retry_unknown_execution: bool = False,
    ) -> dict[str, Any]:
        """오디오 선행 단계: TTS 음성과 줄별 정수 샘플 시점을 만든다. 결과는 타임라인 cue의 원시 시점이다."""
        state = self.store.get_production(pid)
        timeline = state["timelines"][-1]
        spec = {
            "schema": SPEC_SCHEMA,
            "production_id": pid,
            "shot_id": "speech",
            "engine": "tts",
            "profile": "supertonic-speech",
            "params": {"lines": lines, "voice": voice, "speed": speed, "lang": lang},
            "selection": "explicit-lab",
            "fps": {"num": timeline["fps_num"], "den": timeline["fps_den"]},
            "sample_rate": timeline["sample_rate"],
            "range": {"start_f": 0, "end_f": 1, "frames": 1},
            "format": {"width": 2, "height": 2},
            "cues": [],
            "input_assets": [],
            "seed": None,
        }
        self.validate_spec(spec)
        key = (
            "speech:"
            + sha256_hex(
                canonical(
                    {"lines": lines, "voice": voice, "speed": speed, "lang": lang}
                )
            )[:24]
        )
        spec["idempotency_key"] = key
        return self.run_unattached(
            pid,
            section="engine_runs",
            key=key,
            spec=spec,
            retry_unknown_execution=retry_unknown_execution,
        )

    def assert_composable(self, pid: str) -> dict[str, Any]:
        """조립 전제 검사. stale 샷, 현재 출력이 없는 샷, 결함 샷이 있으면 PipelineError(fail-closed)."""
        state = self.store.get_production(pid)
        if state["cancelled"]:
            raise PipelineError("취소된 제작은 조립할 수 없습니다.")
        if state["stale"]["shots"] or state["stale"]["bible_assets"]:
            raise PipelineError(
                "stale 샷이 있습니다: "
                + ", ".join(
                    sorted(
                        set(state["stale"]["shots"])
                        | set(state["stale"]["bible_assets"])
                    )
                )
            )
        timeline = state["timelines"][-1]
        outputs = self.current_outputs(state)
        missing = [
            s["shot_id"] for s in timeline["shots"] if s["shot_id"] not in outputs
        ]
        if missing:
            raise PipelineError(
                "현재 revision의 검수 대기 출력이 없는 샷이 있습니다: "
                + ", ".join(missing)
            )
        defects = self.defective_shots(pid)
        if defects:
            raise PipelineError(
                "결함 샷이 있어 조립하지 않습니다: "
                + ", ".join(d["shot_id"] for d in defects)
            )
        return state

    def compose_final(
        self,
        pid: str,
        *,
        audio_sha256: str | None = None,
        audio_gain_db: float = 0.0,
        retry_unknown_execution: bool = False,
    ) -> dict[str, Any]:
        """현재 revision의 샷 출력들을 타임라인 순서로 조립해 최종 영상을 만든다. stale 샷이나 fail 검수가 있으면 거부한다(fail-closed)."""
        state = self.assert_composable(pid)
        timeline = state["timelines"][-1]
        outputs = self.current_outputs(state)
        size = state["bibles"][-1].get("composition", {}).get("size") or list(
            DEFAULT_SIZE
        )
        assets = [
            {
                "sha256": outputs[s["shot_id"]],
                "role": "clip",
                "frames": s["end_f"] - s["start_f"],
                "shot_id": s["shot_id"],
            }
            for s in timeline["shots"]
        ]
        if audio_sha256:
            if not any(
                a["sha256"] == audio_sha256 for a in state["artifacts"].values()
            ):
                raise PipelineError("오디오 sha256이 제작 자산에 없습니다.")
            assets.append({"sha256": audio_sha256, "role": "audio"})
        spec = {
            "schema": SPEC_SCHEMA,
            "production_id": pid,
            "shot_id": "final",
            "engine": "ffmpeg",
            "profile": "assemble",
            "params": {"audio_gain_db": audio_gain_db},
            "selection": "fixed",
            "range": {
                "start_f": 0,
                "end_f": timeline["duration_frames"],
                "frames": timeline["duration_frames"],
            },
            "fps": {"num": timeline["fps_num"], "den": timeline["fps_den"]},
            "sample_rate": timeline["sample_rate"],
            "format": {"width": int(size[0]), "height": int(size[1])},
            "cues": [],
            "input_assets": assets,
            "seed": None,
            "timeline_revision": timeline["revision"],
            "bible_revision": state["bibles"][-1]["revision"],
        }
        key = (
            "final:"
            + sha256_hex(
                canonical(
                    {
                        "outputs": outputs,
                        "audio": audio_sha256,
                        "gain": audio_gain_db,
                        "t": timeline["content_hash"],
                        "b": state["bibles"][-1]["content_hash"],
                    }
                )
            )[:24]
        )
        spec["idempotency_key"] = key
        self.validate_spec(spec)
        return self.run_unattached(
            pid,
            section="compositions",
            key=key,
            spec=spec,
            extra={
                "shot_outputs": outputs,
                "audio_sha256": audio_sha256,
                "format": spec["format"],
                "composition_key": key,
            },
            retry_unknown_execution=retry_unknown_execution,
        )

    def review_final(self, pid: str, composition_key: str) -> dict[str, Any]:
        state = self.store.get_production(pid)
        composition = state["compositions"][composition_key]
        spec = composition["spec"]
        expect_audio = any(a["role"] == "audio" for a in spec["input_assets"])
        result = self.reviewer.check_final(
            pid, composition_key, expect_audio=expect_audio
        )
        self.store.resolve_final_stale(pid, composition_key)
        return result
