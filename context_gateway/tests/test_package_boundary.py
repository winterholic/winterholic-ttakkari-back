from __future__ import annotations

import importlib.util


def test_gateway_is_an_independent_importable_package() -> None:
    assert importlib.util.find_spec("context_gateway") is not None
