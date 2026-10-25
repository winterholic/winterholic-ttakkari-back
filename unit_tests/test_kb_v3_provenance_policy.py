"""Legacy classification must never let a personal signal override work provenance."""

import pytest

from ttakkari.kb_access_backfill import classify


@pytest.mark.parametrize(
    ("doc_key", "origin_meta", "prefixes", "expected"),
    [
        ("research:personal:x", {"via": "collector", "scope": "work"}, (), "work"),
        ("research:work:x", {"via": "collector", "scope": "personal"}, (), "work"),
        ("research:personal:x", {"via": "collector", "scope": "work"}, ("research:personal:",), "work"),
        ("collector:x", {"via": "collector", "scope": "work"}, ("collector:",), "work"),
        ("research:work:x", None, ("research:",), "work"),
        ("research:personal:x", None, (), "personal"),
        ("collector:x", {"via": "collector", "scope": "personal"}, (), "personal"),
        ("operator:x", None, ("operator:",), "personal"),
        ("unknown:x", None, (), "work"),
        ("unknown:x", {"scope": "personal"}, (), "work"),
    ],
)
def test_legacy_classification_keeps_the_most_restrictive_trusted_scope(
    doc_key, origin_meta, prefixes, expected
):
    scope, _rule = classify(doc_key, origin_meta, prefixes)
    assert scope == expected
