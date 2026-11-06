from pathlib import Path


def test_benchmark_script_never_deletes_worktrees_or_targets_another_run():
    script = Path(__file__).parents[1] / "scripts" / "run_frame_animation_benchmark.py"
    source = script.read_text(encoding="utf-8")

    assert "shutil.rmtree" not in source
    assert "83968b68-3f2f-4e0e-89c7-2a87aeb5a6ee" not in source
    assert "20d17a92-f8b1-423c-a035-a80f0c440433" not in source
    assert "--outbox-dir" in source


def test_run_id_cannot_escape_project_output_directory():
    import importlib.util
    import pytest

    script = Path(__file__).parents[1] / "scripts" / "run_frame_animation_benchmark.py"
    spec = importlib.util.spec_from_file_location("frame_animation_benchmark", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.validate_run_id("manual_run-01") == "manual_run-01"
    with pytest.raises(ValueError):
        module.validate_run_id("../outside")
