"""In-process schema upgrades must preserve the application's diagnostic loggers."""
import os
import subprocess
import sys
from pathlib import Path


def test_migration_keeps_transfer_diagnostics_enabled(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Alembic configures process-wide logging. Isolate that side effect while
    # exercising the actual env.py and upgrade functions without a database.
    source = '''
import io
import json
import logging
from alembic import command
from alembic.config import Config
from fastapi import Request
from ttakkari.transfer import diagnostics

events = []
class Capture(logging.Handler):
    def emit(self, record):
        events.append(json.loads(record.getMessage()))

diagnostics.log.addHandler(Capture())
diagnostics.log.setLevel(logging.INFO)
config = Config("alembic.ini", output_buffer=io.StringIO())
command.upgrade(config, "0073_research_reuse_source:head", sql=True)
assert not diagnostics.log.disabled, "migration disabled the transfer diagnostic logger"
request = Request({"type": "http", "state": {"raw_transfer": {"requestId": "migration-check"}}})
diagnostics.emit(request, "request_received")
assert len(events) == 1
assert events[0]["event"] == "request_received"
assert events[0]["requestId"] == "migration-check"
print("migration_diagnostics_ok")
'''
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(root / "src"),
        TTAKKARI_NO_ENV_FILE="1",
        TTAKKARI_HOME=str(tmp_path / "home"),
    )
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=root, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "migration_diagnostics_ok" in result.stdout
