"""스캔 PDF 글자 읽기. macOS Vision 을 Swift 헬퍼 서브프로세스로 호출한다.

헬퍼는 처음 쓸 때 한 번 컴파일해 캐시한다. 한 번에 한 문서만(프로세스 간 파일 락), 낮은 우선순위로 돌려
임베딩 워커처럼 CPU 를 독점하지 않게 한다.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER_SOURCE = Path(__file__).with_name("vision_ocr.swift")
DEFAULT_MAX_PAGES = 100
DEFAULT_TIMEOUT_SECONDS = 900
COMPILE_TIMEOUT_SECONDS = 180


class OcrUnavailable(RuntimeError):
    """이 환경에서는 OCR 을 돌릴 수 없다(비 macOS, swiftc 없음, 사용자가 껐음)."""


class OcrFailed(RuntimeError):
    """OCR 실행이 실패했다(컴파일·실행·타임아웃)."""


@dataclasses.dataclass(frozen=True)
class OcrPage:
    page_no: int
    text: str
    confidence: float


def enabled() -> bool:
    return os.environ.get("TTAKKARI_PDF_OCR", "on").strip().lower() not in ("off", "0", "false", "no")


def _home() -> Path:
    return Path(os.environ.get("TTAKKARI_HOME") or Path.home() / ".ttakkari")


def _helper_binary() -> Path:
    if sys.platform != "darwin":
        raise OcrUnavailable("ocr_requires_macos")
    if not HELPER_SOURCE.is_file():
        raise OcrUnavailable("ocr_helper_source_missing")
    digest = hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest()[:12]
    cache = _home() / "cache" / "ocr"
    binary = cache / f"vision_ocr-{digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    with open(cache / "compile.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        tmp = binary.with_suffix(".tmp")
        try:
            subprocess.run(
                ["/usr/bin/swiftc", "-O", "-o", str(tmp), str(HELPER_SOURCE)],
                check=True, capture_output=True, timeout=COMPILE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise OcrUnavailable("swiftc_missing") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise OcrFailed("ocr_helper_compile_failed") from exc
        os.replace(tmp, binary)
    return binary


def ocr_pdf(data: bytes, *, max_pages: int | None = None, timeout: float | None = None) -> list[OcrPage]:
    """PDF 바이트의 앞쪽 max_pages 쪽을 읽어 쪽 순서대로 돌려준다. 글자가 없는 쪽은 text 가 빈 문자열이다."""
    if not enabled():
        raise OcrUnavailable("ocr_disabled")
    limit = max_pages or int(os.environ.get("TTAKKARI_PDF_OCR_MAX_PAGES", DEFAULT_MAX_PAGES))
    deadline = timeout or float(os.environ.get("TTAKKARI_PDF_OCR_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    binary = _helper_binary()
    lock_path = _home() / "cache" / "ocr" / "run.lock"
    with open(lock_path, "w") as lock, tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pdf.write(data)
        pdf.flush()
        try:
            done = subprocess.run(
                ["/usr/bin/nice", "-n", "10", str(binary), pdf.name, str(limit)],
                capture_output=True, timeout=deadline, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed("ocr_timeout") from exc
    if done.returncode != 0:
        raise OcrFailed(f"ocr_exit_{done.returncode}")
    pages: list[OcrPage] = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if "error" in row:
            pages.append(OcrPage(int(row.get("page", 0)), "", 0.0))
            continue
        pages.append(OcrPage(int(row["page"]), str(row.get("text", "")), float(row.get("confidence", 0.0))))
    return pages
