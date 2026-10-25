"""Preset datasets for research adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PRESETS_DIR = Path(__file__).parent


def load_retrieval_preset(preset_id: str = "ko_tech_docs_v1") -> dict[str, Any]:
    """Load a retrieval evaluation preset by ID."""
    preset_file = _PRESETS_DIR / f"{preset_id}.json"
    if not preset_file.is_file():
        raise FileNotFoundError(f"Retrieval preset {preset_id} not found at {preset_file}")
    with preset_file.open(encoding="utf-8") as f:
        return json.load(f)


def load_tts_preset(preset_id: str = "ko_mixed_pronunciation_v1") -> dict[str, Any]:
    """Load a TTS pronunciation preset by ID."""
    preset_file = _PRESETS_DIR / f"{preset_id}.json"
    if not preset_file.is_file():
        raise FileNotFoundError(f"TTS preset {preset_id} not found at {preset_file}")
    with preset_file.open(encoding="utf-8") as f:
        return json.load(f)
