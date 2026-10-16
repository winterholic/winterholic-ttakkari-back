"""사용량 화면·집계가 쓰는 접수 Qwen 서버 상태 창구. 수명주기 자체는 runs/desk_qwen_service 가 소유한다.

이 모듈은 프로세스를 띄우거나 끝내지 않는다(서비스에 위임). 예전 요청 시점 지연 기동(ensure_running)은 서비스의 lifespan 기동·health 감시로 대체됐다.
"""
from __future__ import annotations

from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen
from ttakkari.runs.desk_qwen_service import service


async def ensure_running() -> str:
    """지금 쓸 수 있는 Qwen 서버 주소. 없으면 RuntimeError(기다리지 않는다)."""
    base = desk_qwen.endpoint()
    if base is None:
        raise RuntimeError(service.reason or f"qwen server {service.state}")
    return base


async def stop() -> None:
    await service.stop()


def status() -> dict:
    s = get_settings()
    if not s.desk_qwen_enabled:
        return {"enabled": False, "status": "disabled", "detail": None}
    state = service.state
    if state == "external":
        status = "external" if service.external_ready is not False else "unavailable"
    else:
        status = {"ready": "running", "starting": "starting", "failed": "unavailable",
                  "stopped": "not_started", "disabled": "disabled"}.get(state, "unknown")
    return {"enabled": True, "status": status, "detail": service.reason or None}
