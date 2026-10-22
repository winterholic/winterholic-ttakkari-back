"""CPU 계약 시험용 대역 어댑터. 실제 렌더·생성을 하지 않으며 `execution_mode="mock"`으로 receipt에 남는다.

실패 주입(crash·빈 결과·수집 실패·지연·취소 대기)을 쓰는 시험에서 어댑터 수명주기와 G0 attempt 흐름의 계약을 확인한다.
mock 통과는 실제 생성·렌더 성공의 증거가 아니다.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, ClassVar

from ttakkari.creative.engines.base import (
    EngineAdapter,
    EngineCancelled,
    EngineEmptyOutput,
    EngineError,
    EngineOutput,
    EngineRun,
    ExecutionContext,
    ProbeResult,
    ProfileSpec,
    now_iso,
)


class SimulatedCrash(BaseException):
    """프로세스 사망을 흉내 낸다. G0는 Exception만 잡으므로 attempt가 running에 남는다."""


class MockEngineAdapter(EngineAdapter):
    display_name = "CPU mock 어댑터"
    execution_mode = "mock"
    rights: ClassVar[dict[str, str]] = {
        "license": "mock",
        "use_scope": "personal",
    }

    def __init__(self, engine_id: str = "mock", *, frame_exact: bool = True) -> None:
        self.engine_id = engine_id
        self.profiles = {
            "clip": ProfileSpec(
                "clip",
                frozenset({"mock-video", "placeholder"}),
                "결정적 바이트를 돌려주는 대역",
                "video",
                frame_exact=frame_exact,
                params={"label": {"type": "str", "default": "", "max_len": 40}},
            )
        }
        self.calls: list[dict[str, Any]] = []
        self.behavior: dict[str, Any] = {}
        self.hold = threading.Event()
        self.hold.set()

    # 시험에서 한 번만 쓰는 실패 예약: crash_times=1이면 첫 호출만 SimulatedCrash.
    def plan(self, **behavior: Any) -> None:
        self.behavior = behavior

    def probe(self) -> ProbeResult:
        return ProbeResult(
            "available", "mock", None, "mock-1", now_iso(), {"mock": True}
        )

    def validate(self, spec: dict[str, Any]) -> list[str]:
        return [] if spec.get("profile") in self.profiles else ["프로필 없음"]

    def execute(self, ctx: ExecutionContext) -> EngineRun:
        self.calls.append(json.loads(json.dumps(ctx.spec)))
        number = len(self.calls)
        if self.behavior.get("emit"):
            ctx.emit("running")
            ctx.emit("running")
        if number <= self.behavior.get("crash_times", 0):
            raise SimulatedCrash()
        if self.behavior.get("fail"):
            raise EngineError("mock_failed", str(self.behavior["fail"]))
        if self.behavior.get("wait_for_cancel"):
            self.hold.clear()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ctx.check_cancel()
                time.sleep(0.02)
            raise EngineError("timeout", "취소가 오지 않았습니다.")
        if self.behavior.get("empty"):
            raise EngineEmptyOutput("mock이 빈 결과를 돌려줬습니다.")
        if self.behavior.get("empty_bytes"):
            return EngineRun([EngineOutput("o.bin", "video/mp4", b"", "primary")])
        if ctx.cancelled():
            raise EngineCancelled()
        digest = hashlib.sha256(
            json.dumps(
                {k: ctx.spec[k] for k in ("shot_id", "params", "seed", "range")},
                sort_keys=True,
            ).encode()
        ).digest()
        outputs = [EngineOutput("clip.bin", "video/mp4", b"MOCK" + digest, "primary")]
        for index in range(self.behavior.get("extra_outputs", 0)):
            outputs.append(
                EngineOutput(
                    f"aux{index}.bin", "application/octet-stream", digest, "aux"
                )
            )
        return EngineRun(outputs, metrics={"mock": True}, provenance={"mock": True})
