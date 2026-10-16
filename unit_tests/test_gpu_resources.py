from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from ttakkari import sysinfo

# Shape and keys observed on the Mac Studio's AGXAcceleratorG17X driver.
SAMPLE = '''+-o AGXAcceleratorG17X <class AGXAcceleratorG17X>
    {
      "PerformanceStatistics" = {"Alloc system memory"=2917007360,"Device Utilization %"=3,"In use system memory"=507625472}
      "model" = "Apple M5 Max"
      "gpu-core-count" = 40
    }
'''


def mock_ioreg(monkeypatch, output=SAMPLE, returncode=0):
    monkeypatch.setattr(sysinfo.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sysinfo.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=output, returncode=returncode))


def test_gpu_reads_driver_utilization_and_unified_memory(monkeypatch):
    mock_ioreg(monkeypatch)
    assert sysinfo._gpu() == {
        "status": "available", "reason": None, "model": "Apple M5 Max", "cores": 40,
        "percent": 3.0, "memory_used_bytes": 507625472, "memory_allocated_bytes": 2917007360,
    }


def test_gpu_zero_is_a_valid_reading(monkeypatch):
    mock_ioreg(monkeypatch, SAMPLE.replace('Utilization %"=3', 'Utilization %"=0'))
    assert sysinfo._gpu()["status"] == "available"
    assert sysinfo._gpu()["percent"] == 0


@pytest.mark.parametrize("value", ["-1", "101", '"unknown"', "nan", "true"])
def test_gpu_invalid_utilization_is_unavailable_not_zero(monkeypatch, value):
    mock_ioreg(monkeypatch, SAMPLE.replace('Utilization %"=3', f'Utilization %"={value}'))
    result = sysinfo._gpu()
    assert result["status"] == "unavailable"
    assert result["percent"] is None
    assert result["cores"] == 40


@pytest.mark.parametrize("output,code", [("", 0), ("ioreg: error: can't open file.", 0), (SAMPLE, 1)])
def test_gpu_missing_or_failed_command_is_unavailable(monkeypatch, output, code):
    mock_ioreg(monkeypatch, output, code)
    result = sysinfo._gpu()
    assert result["status"] == "unavailable"
    assert result["percent"] is None


@pytest.mark.parametrize("error", [PermissionError("denied"), subprocess.TimeoutExpired("ioreg", 2)])
def test_gpu_command_failure_does_not_break_resources(monkeypatch, error):
    monkeypatch.setattr(sysinfo.platform, "system", lambda: "Darwin")
    def fail(*a, **kw):
        raise error
    monkeypatch.setattr(sysinfo.subprocess, "run", fail)
    result = sysinfo._gpu()
    assert result["status"] == "unavailable"
    assert result["percent"] is None


def test_gpu_unsupported_os_does_not_launch_command(monkeypatch):
    monkeypatch.setattr(sysinfo.platform, "system", lambda: "Linux")
    def fail(*a, **kw):
        pytest.fail("ioreg must not run outside macOS")
    monkeypatch.setattr(sysinfo.subprocess, "run", fail)
    assert sysinfo._gpu()["status"] == "unsupported"


def test_gpu_absent_optional_memory_stays_unknown(monkeypatch):
    mock_ioreg(monkeypatch, SAMPLE.replace('"In use system memory"=507625472', '"Other"=1'))
    assert sysinfo._gpu()["memory_used_bytes"] is None


def test_gpu_does_not_combine_statistics_from_different_adapters(monkeypatch):
    other = SAMPLE.replace('"Device Utilization %"=3,', '').replace('"gpu-core-count" = 40', '"gpu-core-count" = 8')
    mock_ioreg(monkeypatch, other + SAMPLE)
    assert sysinfo._gpu()["cores"] == 40
    assert sysinfo._gpu()["percent"] == 3
