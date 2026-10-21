"""설정·파라미터 → 워커 요청 어댑터. STT 의 local_stt/runner.py 에 대응한다."""
from __future__ import annotations

from ttakkari.model_process import run_json_worker


async def run_worker(*, module: str, request: dict, settings) -> dict:
    return await run_json_worker(
        module=module,
        request=request,
        python=settings.local_tts_python,
        timeout=settings.local_tts_timeout_seconds,
        error_prefix="tts_worker",
    )
