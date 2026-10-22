"""엔진 능력·가용성 레지스트리(ADR §7).

한 가지 ready 값 대신 세 축을 따로 둔다.
- installation: absent|present|probe_failed|available, environment·runtime_path·revision·measured_at 포함. 실측(`probe`)으로만 채운다.
- verification: untested|lab_executed|contract_passed|quality_pilot_passed. 실제 smoke 증거가 있어야 올라간다.
- policy: experimental|auto_eligible|held.

자동 선택 대상은 세 축이 모두 조건을 만족할 때만이다. 측정하지 않았거나 측정이 오래됐거나 mock 어댑터이면 후보에서 뺀다.
mock 계약 시험 통과는 `mock_contract`에 따로 기록하고 verification 단계를 올리지 않는다.
사용자가 엔진을 명시한 lab 실행은 후보 필터와 별개이며 receipt에 explicit-lab으로 남는다.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ttakkari.creative.engines.base import EngineAdapter, EngineError, now_iso

VERIFICATION_LEVELS = (
    "untested",
    "lab_executed",
    "contract_passed",
    "quality_pilot_passed",
)
POLICIES = ("experimental", "auto_eligible", "held")
DEFAULT_FRESH_SECONDS = 6 * 3600


class RegistryError(ValueError):
    pass


class EngineRegistry:
    def __init__(
        self,
        path: Path,
        adapters: Iterable[EngineAdapter],
        *,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        clock: Callable[[], float] = time.time,
        allow_mock_selection: bool = False,
        resource_busy: Callable[[str], str | None] | None = None,
    ) -> None:
        self.path = Path(path)
        listed = list(adapters)
        self.adapters = {a.engine_id: a for a in listed}
        if len(self.adapters) != len(listed):
            raise RegistryError("엔진 ID가 중복됐습니다.")
        self.fresh_seconds = fresh_seconds
        self.clock = clock
        # 테스트 전용 통로다. 켜도 receipt의 execution_mode는 mock으로 남는다.
        self.allow_mock_selection = allow_mock_selection
        # 자원군 이름을 받아 사용 중이면 사유 문자열, 비어 있으면 None. gpu 프로필의 공용 lease 상태를 반영한다.
        self.resource_busy = resource_busy
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ---- 저장
    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(".lock")
        with lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                try:
                    data = json.loads(self.path.read_text())
                except FileNotFoundError:
                    data = {}
                yield data
                fd, name = tempfile.mkstemp(prefix=".registry.", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(
                            data, stream, ensure_ascii=False, sort_keys=True, indent=1
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                except BaseException:
                    Path(name).unlink(missing_ok=True)
                    raise
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {
            "installation": {"state": "absent", "measured_at": None},
            "verification": {"level": "untested", "evidence": []},
            "mock_contract": [],
            "policy": {"value": "experimental", "reason": "측정 전", "at": None},
        }

    def adapter(self, engine_id: str) -> EngineAdapter:
        try:
            return self.adapters[engine_id]
        except KeyError as exc:
            raise RegistryError(f"등록되지 않은 엔진입니다: {engine_id}") from exc

    def installation(self, engine_id: str) -> dict[str, Any]:
        self.adapter(engine_id)
        return (self._read().get(engine_id) or self._blank())["installation"]

    # ---- 측정
    def measure(self, engine_id: str) -> dict[str, Any]:
        adapter = self.adapter(engine_id)
        try:
            result = adapter.probe().as_dict()
        except Exception as exc:  # noqa: BLE001 -- 측정 실패도 사실로 기록한다
            result = {
                "state": "probe_failed",
                "environment": "unknown",
                "runtime_path": None,
                "revision": None,
                "measured_at": now_iso(),
                "details": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
        result["measured_epoch"] = self.clock()
        result["execution_mode"] = adapter.execution_mode
        with self._locked() as data:
            row = data.setdefault(engine_id, self._blank())
            row["installation"] = result
            # 측정이 available이 아니면 자동 선택 정책은 내린다. 올리는 일은 set_policy만 한다.
            if (
                result["state"] != "available"
                and row["policy"]["value"] == "auto_eligible"
            ):
                row["policy"] = {
                    "value": "experimental",
                    "reason": f"재측정 결과 {result['state']}",
                    "at": now_iso(),
                }
        return result

    def run_smoke(self, engine_id: str) -> dict[str, Any]:
        adapter = self.adapter(engine_id)
        if adapter.execution_mode != "real":
            raise RegistryError("mock 어댑터의 smoke는 실제 실행 증거가 아닙니다.")
        installation = self.measure(engine_id)
        if installation["state"] != "available":
            raise EngineError(
                "unavailable",
                f"{engine_id} 측정 결과가 {installation['state']}라 smoke를 건너뜁니다.",
            )
        evidence = adapter.smoke()
        self.record_verification(
            engine_id,
            "lab_executed",
            {"kind": "real_smoke", "at": now_iso(), **evidence},
        )
        return evidence

    def record_verification(
        self, engine_id: str, level: str, evidence: dict[str, Any]
    ) -> None:
        adapter = self.adapter(engine_id)
        if level not in VERIFICATION_LEVELS or level == "untested":
            raise RegistryError("알 수 없는 검증 단계입니다.")
        if adapter.execution_mode != "real" or evidence.get("kind") != "real_smoke":
            raise RegistryError(
                "실제 smoke 증거(kind=real_smoke)가 있는 real 어댑터만 검증 단계를 올릴 수 있습니다."
            )
        with self._locked() as data:
            row = data.setdefault(engine_id, self._blank())
            current = row["verification"]["level"]
            if VERIFICATION_LEVELS.index(level) > VERIFICATION_LEVELS.index(current):
                row["verification"]["level"] = level
            row["verification"]["evidence"] = [
                *row["verification"]["evidence"],
                evidence,
            ][-10:]

    def record_mock_contract(self, engine_id: str, evidence: dict[str, Any]) -> None:
        """CPU mock 계약 시험 결과. 정보 기록일 뿐 verification 단계와 자동 선택에 영향이 없다."""
        self.adapter(engine_id)
        with self._locked() as data:
            row = data.setdefault(engine_id, self._blank())
            row["mock_contract"] = [
                *row["mock_contract"],
                {"at": now_iso(), **evidence},
            ][-10:]

    def set_policy(self, engine_id: str, policy: str, reason: str) -> dict[str, Any]:
        self.adapter(engine_id)
        if policy not in POLICIES:
            raise RegistryError("알 수 없는 정책입니다.")
        if not reason.strip():
            raise RegistryError("정책 변경 사유가 필요합니다.")
        with self._locked() as data:
            row = data.setdefault(engine_id, self._blank())
            if policy == "auto_eligible":
                problems = self._eligibility_problems(
                    engine_id, row, ignore_policy=True
                )
                if problems:
                    raise RegistryError(
                        "자동 선택으로 올릴 수 없습니다: " + "; ".join(problems)
                    )
            row["policy"] = {"value": policy, "reason": reason, "at": now_iso()}
            return row["policy"]

    # ---- 판정
    def _fresh(self, installation: dict[str, Any]) -> bool:
        epoch = installation.get("measured_epoch")
        return epoch is not None and 0 <= self.clock() - epoch <= self.fresh_seconds

    def _eligibility_problems(
        self, engine_id: str, row: dict[str, Any], *, ignore_policy: bool = False
    ) -> list[str]:
        adapter = self.adapters[engine_id]
        problems: list[str] = []
        if adapter.execution_mode != "real" and not self.allow_mock_selection:
            problems.append("mock 어댑터는 자동 선택 대상이 아닙니다")
        installation = row["installation"]
        if installation["state"] != "available":
            problems.append(f"설치 측정 결과가 {installation['state']}입니다")
        elif not self._fresh(installation):
            problems.append("설치 측정이 오래됐습니다")
        if adapter.execution_mode == "real":
            level = row["verification"]["level"]
            if VERIFICATION_LEVELS.index(level) < VERIFICATION_LEVELS.index(
                "lab_executed"
            ):
                problems.append("실제 smoke 증거가 없습니다")
        if not ignore_policy and row["policy"]["value"] != "auto_eligible":
            problems.append(f"정책이 {row['policy']['value']}입니다")
        return problems

    def status(self, engine_id: str) -> dict[str, Any]:
        adapter = self.adapter(engine_id)
        row = self._read().get(engine_id) or self._blank()
        problems = self._eligibility_problems(engine_id, row)
        return {
            "engine_id": engine_id,
            "display_name": adapter.display_name,
            "execution_mode": adapter.execution_mode,
            "rights": adapter.rights,
            "installation": row["installation"],
            "verification": row["verification"],
            "mock_contract": row["mock_contract"],
            "policy": row["policy"],
            "auto_eligible": not problems,
            "ineligible_reasons": problems,
            "profiles": {
                pid: {
                    "capabilities": sorted(p.capabilities),
                    "description": p.description,
                    "output_kind": p.output_kind,
                    "resource_family": p.resource_family,
                    "params": p.params,
                }
                for pid, p in adapter.profiles.items()
            },
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [self.status(engine_id) for engine_id in self.adapters]

    def candidates(
        self, capabilities: Iterable[str] | None = None, *, use_scope: str = "personal"
    ) -> list[dict[str, str]]:
        """자동 선택 후보 (engine, profile). 요구 능력 → 권리 범위 → 측정·검증·정책 → 자원 상태 순으로 거른다."""
        wanted = set(capabilities or [])
        data = self._read()
        found: list[dict[str, str]] = []
        for engine_id, adapter in self.adapters.items():
            if adapter.rights.get("use_scope", "personal") not in {use_scope, "any"}:
                continue
            row = data.get(engine_id) or self._blank()
            if self._eligibility_problems(engine_id, row):
                continue
            for pid, profile in adapter.profiles.items():
                if wanted and not wanted <= profile.capabilities:
                    continue
                if self.resource_busy and self.resource_busy(profile.resource_family):
                    continue
                found.append({"engine": engine_id, "profile": pid})
        return found

    def assert_auto_choice(
        self, engine_id: str, profile_id: str, capabilities: Iterable[str] = ()
    ) -> None:
        """감독(LLM)이 고른 엔진·프로필이 자동 선택 후보인지 확인한다. 아니면 RegistryError."""
        if {"engine": engine_id, "profile": profile_id} not in self.candidates(
            capabilities
        ):
            row = self._read().get(engine_id) or self._blank()
            adapter = self.adapters.get(engine_id)
            reasons = (
                self._eligibility_problems(engine_id, row)
                if adapter is not None
                else ["등록되지 않은 엔진입니다"]
            )
            raise RegistryError(
                f"{engine_id}/{profile_id}는 자동 선택 후보가 아닙니다: "
                + ("; ".join(reasons) or "프로필·능력 불일치")
            )
