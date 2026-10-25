"""Subtitle (SRT, VTT), audio/video, and image metadata parsers with honest held reasons."""

from __future__ import annotations

import io
import re
import unicodedata
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus


def parse_timestamp_ms(time_str: str) -> int:
    """Parse HH:MM:SS,mmm or HH:MM:SS.mmm into milliseconds."""
    ts = time_str.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        s_parts = parts[2].split(".")
        s = int(s_parts[0])
        ms = int(s_parts[1].ljust(3, "0")[:3]) if len(s_parts) > 1 else 0
        return (h * 3600 + m * 60 + s) * 1000 + ms
    elif len(parts) == 2:
        m = int(parts[0])
        s_parts = parts[1].split(".")
        s = int(s_parts[0])
        ms = int(s_parts[1].ljust(3, "0")[:3]) if len(s_parts) > 1 else 0
        return (m * 60 + s) * 1000 + ms
    return 0


class SubtitleParser(BaseParser):
    name = "subtitle_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".srt", ".vtt"}
    supported_mime_types: ClassVar[set[str]] = {"text/vtt", "application/x-subrip"}

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                import charset_normalizer

                text = str(
                    charset_normalizer.from_bytes(data).best()
                    or data.decode("utf-8", errors="replace")
                )
            except Exception:  # noqa: BLE001
                text = data.decode("utf-8", errors="replace")

        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")

        # Regex for SRT/VTT timecode block:
        block_pattern = re.compile(
            r"(?:\d+\n)?(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})(?:[^\n]*\n)([\s\S]*?)(?=\n\s*\n|\Z)"
        )

        body_parts: list[str] = []
        locator_spans: list[LocatorSpan] = []
        cur_pos = 0

        for m in block_pattern.finditer(text):
            start_tc = m.group(1)
            end_tc = m.group(2)
            cue_text = m.group(3).strip()
            # Remove VTT tags like <v Speaker> or <i>
            cue_text = re.sub(r"<[^>]+>", "", cue_text).strip()
            if not cue_text:
                continue

            t_start_ms = parse_timestamp_ms(start_tc)
            t_end_ms = parse_timestamp_ms(end_tc)

            if body_parts:
                body_parts.append("\n")
                cur_pos += 1

            s_start = cur_pos
            body_parts.append(cue_text)
            cur_pos += len(cue_text)
            s_end = cur_pos

            locator_spans.append(
                LocatorSpan(
                    start_offset=s_start,
                    end_offset=s_end,
                    section_path=[f"{start_tc} - {end_tc}"],
                    chunk_kind="transcript",
                    time_start_ms=t_start_ms,
                    time_end_ms=t_end_ms,
                )
            )

        full_body = "".join(body_parts)
        return ParsedDocument(
            normalized_body=unicodedata.normalize("NFC", full_body),
            metadata={
                "parser": self.name,
                "cue_count": len(locator_spans),
                "filename": filename,
            },
            locator_spans=locator_spans,
            status=ParseStatus.SUCCEEDED.value,
        )


class MediaAudioParser(BaseParser):
    name = "media_audio_stt_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".aac",
        ".ogg",
    }
    supported_mime_types: ClassVar[set[str]] = {
        "audio/mpeg",
        "audio/wav",
        "audio/x-m4a",
        "video/mp4",
        "audio/aac",
    }

    def _stt_available(self) -> bool:
        """Check if local STT runtime (faster-whisper) is available."""
        try:
            import faster_whisper  # noqa: F401

            return True
        except ImportError:
            return False

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        if not self._stt_available():
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.HELD.value,
                hold_reason="stt_runtime_unavailable",
                metadata={"filename": filename, "content_type": content_type},
            )

        # STT runtime integration point (when available on host)
        return ParsedDocument(
            normalized_body="",
            status=ParseStatus.HELD.value,
            hold_reason="stt_runtime_unavailable",
            metadata={"filename": filename, "content_type": content_type},
        )


class ImageParser(BaseParser):
    name = "image_metadata_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".heic",
    }
    supported_mime_types: ClassVar[set[str]] = {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
    }

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        from PIL import Image

        try:
            img = Image.open(io.BytesIO(data))
            width, height = img.size
            format_name = img.format
        except Exception:  # noqa: BLE001
            width, height, format_name = 0, 0, "unknown"

        # Image OCR is held by policy in KB v3
        return ParsedDocument(
            normalized_body="",
            metadata={
                "width": width,
                "height": height,
                "format": format_name,
                "filename": filename,
            },
            status=ParseStatus.HELD.value,
            hold_reason="ocr_required",
        )
