"""Load the committed fixture data (extracted once from the 2026-10-18 dev fixtures)."""

import json
from pathlib import Path

DATA = Path(__file__).with_name("fixture_data")


def load(key: str) -> dict:
    return json.loads((DATA / f"{key}.json").read_text(encoding="utf-8"))


def files(key: str) -> dict[str, str]:
    return dict(load(key)["files"])


def sources(key: str) -> dict[str, str]:
    return dict(load(key)["sources"])
