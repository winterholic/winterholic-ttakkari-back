"""Host test servers must emit backend placement logs for device evidence."""

from pathlib import Path

from ttakkari.host_gpu import StartRequest, build_args, device_evidence


def test_host_gpu_server_enables_backend_info_logs():
    argv = build_args(Path("llama-server"), Path("model.gguf"), 19300, StartRequest(model="tiny"))
    assert "--log-verbosity" in argv, "llama.cpp defaults to 3, which drops GGML info device-placement logs"
    assert argv[argv.index("--log-verbosity") + 1] == "4"


def test_device_evidence_does_not_lose_layer_placement_after_initialization_noise(tmp_path):
    log = tmp_path / 'server.log'
    startup = [f'ggml_metal_device_init: GPU capability {i}' for i in range(12)]
    placement = ['load_tensors: offloading 32 repeating layers to GPU',
                 'load_tensors: offloaded 33/33 layers to GPU',
                 'llama_kv_cache: Metal KV buffer size = 256 MiB']
    log.write_text('\n'.join(startup + placement) + '\n', encoding='utf-8')
    evidence = device_evidence(log)
    assert len(evidence) == 8
    assert all(line in evidence for line in placement)
    assert evidence[:3] == placement


def test_device_evidence_keeps_zero_offload_honest_and_does_not_invent_placement(tmp_path):
    log = tmp_path / 'server.log'
    log.write_text('ggml_metal_device_init: GPU name MTL0\nload_tensors: offloaded 0/33 layers to GPU\n', encoding='utf-8')
    assert device_evidence(log, limit=1) == ['load_tensors: offloaded 0/33 layers to GPU']
    log.write_text('ggml_metal_device_init: GPU name MTL0\n', encoding='utf-8')
    assert device_evidence(log) == ['ggml_metal_device_init: GPU name MTL0']
    assert device_evidence(tmp_path / 'missing.log') == []
