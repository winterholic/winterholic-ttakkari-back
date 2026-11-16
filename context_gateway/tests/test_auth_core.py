from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta


def _auth_module():
    assert importlib.util.find_spec("context_gateway.auth") is not None
    return importlib.import_module("context_gateway.auth")


def test_issued_key_is_256_bit_random_and_expires_in_one_hour() -> None:
    auth = _auth_module()
    now = datetime(2026, 11, 16, tzinfo=UTC)

    raw_key, key_hash, expires_at = auth.issue_key(now=now)

    assert len(auth.decode_key(raw_key)) == 32
    assert key_hash == auth.hash_credential(raw_key)
    assert expires_at == now + timedelta(hours=1)


def test_key_hashing_is_one_way_and_uses_constant_time_comparison() -> None:
    auth = _auth_module()
    raw_key = auth.encode_key(b"x" * 32)
    digest = auth.hash_credential(raw_key)

    assert raw_key not in digest
    assert len(digest) == 64
    assert auth.credentials_match(raw_key, digest)
    assert not auth.credentials_match(raw_key + "x", digest)
