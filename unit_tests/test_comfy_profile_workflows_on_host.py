"""등록된 프로필이 가리키는 lab 워크플로가 호스트 디스크의 모델 파일명과 정확히 맞고, 입력이 올바른 노드에 묶이는지 본다.

GPU 도 Comfy 서버도 쓰지 않는 정적 검사다. lab 저장소가 없는 환경에서는 건너뛴다.
"""
from __future__ import annotations

import importlib
import json
import sys

import pytest

from ttakkari import managed_comfy

pytestmark = pytest.mark.skipif(not managed_comfy.REGISTRY_ROOT.is_dir(), reason="lab ComfyUI registry is absent")

COMFY_PROFILES = [p for p in managed_comfy.PROFILES.values() if p.engine == "comfy"]


@pytest.fixture(scope="module")
def workflow():
    if str(managed_comfy.ADAPTER_ROOT) not in sys.path:
        sys.path.insert(0, str(managed_comfy.ADAPTER_ROOT))
    return importlib.import_module("ttk_comfy.workflow")


def _model_names(api):
    return {v for node in api.values() for v in node["inputs"].values()
            if isinstance(v, str) and v.endswith(".safetensors")}


@pytest.mark.parametrize("profile", COMFY_PROFILES, ids=lambda p: p.profile_id)
def test_pinned_workflow_hash_models_and_nodes(workflow, profile):
    api, meta, _h = workflow.Registry(root=managed_comfy.REGISTRY_ROOT).load(profile.workflow, profile.revision)
    assert workflow.graph_hash(api) == profile.revision
    assert _model_names(api) == set(profile.models)
    models_root = managed_comfy.COMFY_ROOT / "ComfyUI" / "models"
    for name in profile.models:
        assert list(models_root.glob(f"*/{name}")), f"{name} is not on disk"
    assert profile.preset in meta["presets"]
    info_path = managed_comfy.COMFY_ROOT / "notes" / "object_info.json"
    if info_path.is_file():
        report = workflow.validate(api, json.loads(info_path.read_text()))
        assert not report["missing_nodes"] and not report["broken_links"]


@pytest.mark.parametrize("profile", [p for p in COMFY_PROFILES if p.parametric], ids=lambda p: p.profile_id)
def test_profile_inputs_reach_the_bound_nodes(workflow, profile):
    api, meta, _h = workflow.Registry(root=managed_comfy.REGISTRY_ROOT).load(profile.workflow, profile.revision)
    for width, height in profile.sizes:
        params, _spec = profile.resolve(width, height, None)
        bound = workflow.bind_params(api, meta, {"prompt": "p", "seed": 7, **params}, profile.preset)
        for param in ("width", "height"):
            for node_id, key in meta["params"][param]["bind"]:
                assert bound[node_id]["inputs"][key] == params[param]
    if profile.frames:
        for frames in profile.frames:
            params, _spec = profile.resolve(None, None, frames / profile.fps)
            bound = workflow.bind_params(api, meta, {"prompt": "p", "seed": 7, **params}, profile.preset)
            (node_id, key), = meta["params"]["length"]["bind"]
            assert bound[node_id]["inputs"][key] == frames
            (create_id, fps_key), = meta["params"]["fps"]["bind"]
            assert bound[create_id]["inputs"][fps_key] == profile.fps
    if profile.needs_image:
        assert meta["params"]["image"]["kind"] == "image"
