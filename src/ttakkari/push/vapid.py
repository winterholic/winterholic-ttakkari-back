"""VAPID 키 쌍. ~/.ttakkari/vapid.json(0600)에 PEM 개인키와 공개키(base64url)를 둔다."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02

from ttakkari.config import get_settings

log = logging.getLogger(__name__)

# 일부 푸시 서비스(Apple)는 sub 가 유효한 mailto/https 가 아니면 거절한다. 실제 주소로 vapid.json 의 subject 를 바꾼다.
DEFAULT_SUBJECT = "mailto:ttakkari@example.com"


def vapid_path() -> Path:
    return get_settings().home / "vapid.json"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def generate(path: Path | None = None) -> Path:
    """새 키를 만든다. 이미 있으면 덮어쓰지 않는다(기존 구독이 모두 무효가 되므로)."""
    path = path or vapid_path()
    if path.exists():
        return path
    v = Vapid02()
    v.generate_keys()
    pem = v.private_pem().decode()
    pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    body = json.dumps({"private_key_pem": pem, "public_key": _b64url(pub), "subject": DEFAULT_SUBJECT}, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    log.info("VAPID 키를 새로 만들었습니다: %s", path)
    return path


def load() -> dict[str, str]:
    path = vapid_path()
    if not path.exists():
        generate(path)
    return json.loads(path.read_text(encoding="utf-8"))


def public_key() -> str:
    return load()["public_key"]


def signer() -> tuple[Vapid02, dict[str, str]]:
    d = load()
    sub = get_settings().vapid_subject or d.get("subject") or DEFAULT_SUBJECT
    return Vapid02.from_pem(d["private_key_pem"].encode()), {"sub": sub}
