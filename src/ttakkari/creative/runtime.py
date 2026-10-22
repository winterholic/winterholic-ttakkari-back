"""제작 파이프라인의 기본 구성. API와 스크립트가 같은 방식으로 레지스트리·파이프라인·감독을 얻는다."""

from __future__ import annotations

from pathlib import Path

from ttakkari.creative.direction import CreativeDirector
from ttakkari.creative.director import DirectorStore, default_director_store
from ttakkari.creative.director_llm import DirectorLlm, SessionRunDirectorLlm
from ttakkari.creative.engines.comfy_engine import ComfyAdapter
from ttakkari.creative.engines.ffmpeg_engine import FfmpegAdapter
from ttakkari.creative.engines.registry import EngineRegistry
from ttakkari.creative.engines.remotion_engine import RemotionAdapter
from ttakkari.creative.engines.three_engine import ThreeAdapter
from ttakkari.creative.engines.tts_engine import TtsAdapter
from ttakkari.creative.pipeline import ProductionPipeline

_PIPELINES: dict[Path, ProductionPipeline] = {}


def _gpu_lease_busy(family: str) -> str | None:
    """gpu 자원군은 공용 host resource lease가 비어 있을 때만 후보가 된다. 읽을 수 없으면 사용 중으로 본다(fail-closed)."""
    if family != "gpu":
        return None
    try:
        from ttakkari.config import get_settings
        from ttakkari.host_resource import ResourceLeaseManager

        current = ResourceLeaseManager(data_dir=get_settings().data_dir).current()
    except Exception as exc:  # noqa: BLE001 - lease 상태를 모르면 gpu 후보를 내보내지 않는다
        return f"lease 상태를 읽지 못했습니다: {exc}"
    return "공용 host resource lease가 사용 중입니다." if current else None


def default_registry(store: DirectorStore | None = None) -> EngineRegistry:
    base = (store or default_director_store()).root.parent
    return EngineRegistry(
        base / "engine-registry.json",
        [
            FfmpegAdapter(),
            RemotionAdapter(),
            ThreeAdapter(),
            TtsAdapter(),
            ComfyAdapter(),
        ],
        resource_busy=_gpu_lease_busy,
    )


def default_pipeline() -> ProductionPipeline:
    store = default_director_store()
    if store.root not in _PIPELINES:
        _PIPELINES[store.root] = ProductionPipeline(store, default_registry(store))
    return _PIPELINES[store.root]


def default_director_llm() -> DirectorLlm:
    return SessionRunDirectorLlm()


def default_director() -> CreativeDirector:
    return CreativeDirector(default_pipeline(), default_director_llm())
