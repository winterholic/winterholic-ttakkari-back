"""비밀값 마스킹(ADR 0033). 원문이 한 글자도 남지 않아야 하고, 진단에 필요한 값(SHA·개수)은 남아야 한다."""
from __future__ import annotations

import pytest

from ttakkari.oplog.scrub import MASK, scrub, scrub_value

pytestmark = [pytest.mark.security]


SECRETS = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
    "sk-proj1234567890abcdefghij",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
    "xoxb-1234567890-abcdefghij",
    "AKIAABCDEFGHIJKLMNOP",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "npm_abcdefghijklmnopqrstuvwxyz0123456789",
    "glpat-abcdefghij0123456789",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_known_token_formats_are_removed_anywhere(secret):
    out = scrub(f"upload failed for {secret} twice")
    assert secret not in out and MASK in out


@pytest.mark.parametrize("text,secret", [
    ("Authorization: Bearer abc123def456ghi789", "abc123def456ghi789"),
    ("authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
    ("Cookie: refresh=aaaa1111bbbb2222; sid=zzzz", "aaaa1111bbbb2222"),
    ("Set-Cookie: ttk_refresh=ffff0000eeee; HttpOnly", "ffff0000eeee"),
    ("X-Api-Key: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    ("connect postgresql://winter:hunter2pass@localhost/ttakkari", "hunter2pass"),
    ("password=hunter2pass next", "hunter2pass"),
    ("TTAKKARI_JWT_SECRET=very-secret-value-123", "very-secret-value-123"),
    ('{"api_key": "abcd1234efgh5678", "ok": 1}', "abcd1234efgh5678"),
    ("{'access_token': 'tok_live_9988776655'}", "tok_live_9988776655"),
    ("curl --token mytokenvalue123 http://x", "mytokenvalue123"),
    ("client_secret: 'sh h h secret with spaces'", "sh h h secret with spaces"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----", "MIIEvQIBADANBgkq"),
    ("PASSWORD: correct horse battery", "correct"),
])
def test_contextual_secrets_are_removed(text, secret):
    out = scrub(text)
    assert secret not in out, out
    assert MASK in out


def test_diagnostic_values_survive():
    sha = "b808e353c81de1d0e74b2009931720fa58cbd514"
    out = scrub(f"revision {sha} failed; max_tokens=4000 token_count=12 author=winter tokens: 55 exit 7")
    assert sha in out
    for kept in ("max_tokens=4000", "token_count=12", "author=winter", "exit 7"):
        assert kept in out


def test_exact_environment_values_are_removed_without_context(monkeypatch):
    monkeypatch.setenv("TTAKKARI_SOME_SECRET", "plain-looking-value-xyz")
    assert "plain-looking-value-xyz" not in scrub("it crashed near plain-looking-value-xyz here")


def test_scrub_is_idempotent():
    once = scrub("password=hunter2pass Authorization: Bearer abcdefgh12345678")
    assert scrub(once) == once


def test_scrub_value_handles_nested_structures_and_secret_keys():
    out = scrub_value({
        "job": "ok", "password": "p@ss", "headers": {"authorization": "Bearer abcdefgh12345678"},
        "lines": ["token=abcdef123456", "fine"], "tokens": 5,
    })
    assert out["password"] == MASK
    assert "abcdefgh12345678" not in str(out) and "abcdef123456" not in str(out)
    assert out["tokens"] == 5 and out["job"] == "ok"


@pytest.mark.parametrize("blob", ["s" * 200_000, "a1_" * 60_000, ("tokenx" * 50 + " ") * 2000, "=" * 100_000, "password" * 20_000])
def test_scrub_is_fast_on_pathological_long_inputs(blob):
    import time
    t = time.perf_counter()
    scrub(blob)
    assert time.perf_counter() - t < 1.5


@pytest.mark.parametrize('value', [{'value': 'nested-plain-secret'}, ['nested-plain-secret'], False])
def test_sensitive_key_masks_the_entire_value_regardless_of_type(value):
    assert scrub_value({'password': value, 'credentials': value, 'safe': 'kept'}) == {'password': MASK, 'credentials': MASK, 'safe': 'kept'}


def test_escaped_quotes_do_not_terminate_secret_redaction():
    raw = '{"password": "part-one\\\"private-tail", "safe": "kept"}'
    out = scrub(raw)
    assert 'part-one' not in out and 'private-tail' not in out
    assert 'kept' in out
