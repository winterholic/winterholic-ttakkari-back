"""NotebookLM 이 쓰는 세션 진입점(ADR 0045, 0051).

세션 파일 저장·권한·마스킹은 Google 계정 공통 모듈(ttakkari.google_account.session)이 맡는다.
여기에는 기존 import 경로를 위한 재노출과 NotebookLM 고유 위치(업로드 임시 폴더)만 남긴다.
"""

from __future__ import annotations

from pathlib import Path

from ttakkari.config import get_settings
from ttakkari.google_account.session import (  # noqa: F401
    DEFAULT_ACCOUNT,
    REQUIRED_COOKIES,
    STORAGE_NAME,
    account_dir,
    build_storage_state,
    check_account,
    cookie_names,
    delete,
    exists,
    is_google,
    load_secrets,
    mode_ok,
    profile_dir,
    save_cookies,
    storage_path,
)


def uploads_dir() -> Path:
    return get_settings().notebooklm_dir / "uploads"
