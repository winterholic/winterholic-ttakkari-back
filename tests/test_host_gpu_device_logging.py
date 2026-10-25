"""Host test servers must emit backend placement logs for device evidence."""

from pathlib import Path

from ttakkari.host_gpu import StartRequest, build_args


def test_host_gpu_server_enables_backend_info_logs():
    argv = build_args(Path("llama-server"), Path("model.gguf"), 19300, StartRequest(model="tiny"))
    assert "--log-verbosity" in argv, "llama.cpp defaults to 3, which drops GGML info device-placement logs"
    assert argv[argv.index("--log-verbosity") + 1] == "4"
