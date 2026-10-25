"""Show whether external_benchmark requires a traceable source (no DB/network)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from ttakkari.research.adapters.external_report import ExternalReportAdapter  # noqa: E402

print(
    "runtime:",
    {
        "python": sys.executable,
        "adapter_module": __import__("ttakkari.research.adapters.external_report", fromlist=["__file__"]).__file__,
        "database_target": None,
    },
)

adapter = ExternalReportAdapter()
config = {
    "provenance": "external_benchmark",
    "measurements": [{"metric_key": "decode_tps", "value": 185.4}],
}
print("config_has_source:", {k: k in config for k in ("source_title", "source_url", "source_year")})
print("validate_config_errors:", adapter.validate_config(config, scope="personal"))
