"""맥락 공유 초안의 민감정보 탐지. 합성 값만 쓴다."""
from __future__ import annotations

import pytest

from ttakkari.context_sharing.scanner import scan_text

pytestmark = pytest.mark.security

# 합성 비밀. 실제 서비스에서 발급된 값이 아니다.
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
JWT = "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJ0ZXN0LXVzZXIifQ" + ".c2lnbmF0dXJlLXZhbHVlLXN5bnRoZXRpYw"


def rules(text: str) -> set[str]:
    return {f.rule for f in scan_text(text)}


@pytest.mark.parametrize("text,rule", [
    ("-----BEGIN RSA PRIVATE KEY-----\nabc", "private_key"),
    (f"aws key {AWS}", "aws_access_key"),
    (f"token {GH}", "github_token"),
    (f"jwt {JWT}", "jwt"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789", "bearer_token"),
    ("DB_PASSWORD = hunter2-synthetic", "credential_assignment"),
    ("비밀번호: 합성값1234", "credential_assignment"),
    ("postgres://app:s3cretpw@db.example.com/main", "url_credentials"),
    ("주민번호 900101-1234567", "korean_rrn"),
])
def test_high_risk_credentials_are_detected(text, rule):
    found = scan_text(text)
    assert rule in {f.rule for f in found}
    assert all(f.severity == "high" for f in found if f.rule == rule)


@pytest.mark.parametrize("text,rule", [
    ("http://localhost:8787/api", "internal_url"),
    ("see https://192.168.0.12/admin", "internal_url"),
    ("http://10.1.2.3:9000", "internal_url"),
    ("https://wiki.corp/page", "internal_url"),
    ("https://build.internal/x", "internal_url"),
    ("파일은 /Users/winterholic/development/app/.env 에 있다", "local_path"),
    ("C:\\Users\\alice\\Documents\\a.txt", "local_path"),
    ("/Volumes/Backup/ttakkari-data", "local_path"),
    ("~/.ssh/id_ed25519", "local_path"),
])
def test_internal_urls_and_local_paths_are_high_risk(text, rule):
    found = [f for f in scan_text(text) if f.rule == rule]
    assert found and all(f.severity == "high" for f in found)


def test_credential_value_is_never_echoed_in_the_excerpt():
    found = scan_text(f"password = hunter2-synthetic\nkey {AWS}\ntoken {GH}")
    assert found
    dumped = repr(found)
    for secret in ("hunter2-synthetic", AWS, GH):
        assert secret not in dumped


def test_findings_report_one_based_line_and_field():
    found = scan_text("ok\nfine\nhttp://localhost:1/x", field="content", document_id="d1")
    assert [(f.line, f.field, f.document_id) for f in found] == [(3, "content", "d1")]


@pytest.mark.parametrize("text", [
    "password = <your-password>",
    "api_key: ${API_KEY}",
    "token = ********",
    "https://example.com/docs",
    "공개 문서의 일반 설명입니다.",
    "비밀번호는 환경 변수로 주입한다.",
])
def test_placeholders_and_public_text_are_not_flagged(text):
    assert rules(text) == set()


def test_email_and_phone_are_warnings_not_blocking():
    found = scan_text("문의 owner@example.com 또는 010-1234-5678")
    assert {f.rule for f in found} == {"email", "phone_kr"}
    assert all(f.severity == "medium" for f in found)


def test_every_match_on_a_line_is_reported():
    found = scan_text("http://localhost:1/a http://localhost:2/b")
    assert len([f for f in found if f.rule == "internal_url"]) == 2
