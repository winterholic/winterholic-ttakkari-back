"""Resource limits and zip safety validation for document parsers."""

from __future__ import annotations

import dataclasses
import io
import zipfile


@dataclasses.dataclass(frozen=True)
class ParserResourceLimits:
    """Configurable resource upper bounds to guard against zip bombs and DoS vectors."""

    max_file_bytes: int = 100 * 1024 * 1024  # 100 MiB
    max_zip_uncompressed_bytes: int = 100 * 1024 * 1024  # 100 MiB total uncompressed
    max_zip_compression_ratio: float = 100.0  # Max uncompressed:compressed ratio
    max_zip_entries: int = 10_000  # Max file entries inside zip
    max_xlsx_rows_per_sheet: int = 50_000
    max_xlsx_total_cells: int = 200_000
    max_pdf_pages: int = 1_000
    max_text_chars: int = 10 * 1024 * 1024  # 10M characters


DEFAULT_LIMITS = ParserResourceLimits()


def validate_zip_safety(
    data: bytes,
    limits: ParserResourceLimits = DEFAULT_LIMITS,
) -> None:
    """Inspect zip directory entries without decompressing to detect zip bombs and nested explosions."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infolist = zf.infolist()
            if len(infolist) > limits.max_zip_entries:
                raise ValueError(
                    f"Zip entry count {len(infolist)} exceeds limit {limits.max_zip_entries}"
                )

            total_uncompressed = sum(info.file_size for info in infolist)
            if total_uncompressed > limits.max_zip_uncompressed_bytes:
                raise ValueError(
                    f"Zip uncompressed size {total_uncompressed} exceeds limit {limits.max_zip_uncompressed_bytes}"
                )

            compressed_size = len(data)
            # Detect extreme compression ratios for payloads > 1MB uncompressed
            if compressed_size > 0 and total_uncompressed > 1024 * 1024:
                ratio = total_uncompressed / compressed_size
                if ratio > limits.max_zip_compression_ratio:
                    raise ValueError(
                        f"Zip compression ratio {ratio:.1f}:1 exceeds safety limit {limits.max_zip_compression_ratio}:1"
                    )
    except zipfile.BadZipFile:
        # Not a valid zip; let underlying office parser or corrupt handler report it
        pass
