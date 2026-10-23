"""Make `local_llm_eval` importable when pytest is started from the repository root."""

import sys
from pathlib import Path

BENCH = str(Path(__file__).resolve().parents[2])
if BENCH not in sys.path:
    sys.path.insert(0, BENCH)
