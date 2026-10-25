"""Multi-format document parser registry and content-addressed blob store for Knowledge Library v3."""

from __future__ import annotations

from ttakkari.kb_parsers.base import (
    BaseParser,
    ParsedDocument,
    ParserRegistry,
    ParseStatus,
)
from ttakkari.kb_parsers.blob_store import BlobStore
from ttakkari.kb_parsers.code_parser import CodeParser
from ttakkari.kb_parsers.markdown_parser import MarkdownParser
from ttakkari.kb_parsers.media_parser import (
    ImageParser,
    MediaAudioParser,
    SubtitleParser,
)
from ttakkari.kb_parsers.office_parser import (
    CsvParser,
    DocxParser,
    LegacyOfficeParser,
    PptxParser,
    XlsxParser,
)
from ttakkari.kb_parsers.pdf_parser import PdfParser


def create_default_registry() -> ParserRegistry:
    """Create a ParserRegistry populated with all standard v3 format parsers."""
    registry = ParserRegistry()
    registry.register(MarkdownParser())
    registry.register(PdfParser())
    registry.register(DocxParser())
    registry.register(PptxParser())
    registry.register(XlsxParser())
    registry.register(CsvParser())
    registry.register(LegacyOfficeParser())
    registry.register(CodeParser())
    registry.register(SubtitleParser())
    registry.register(MediaAudioParser())
    registry.register(ImageParser())
    return registry


__all__ = [
    "BaseParser",
    "BlobStore",
    "CodeParser",
    "CsvParser",
    "DocxParser",
    "ImageParser",
    "LegacyOfficeParser",
    "MarkdownParser",
    "MediaAudioParser",
    "ParseStatus",
    "ParsedDocument",
    "ParserRegistry",
    "PdfParser",
    "PptxParser",
    "SubtitleParser",
    "XlsxParser",
    "create_default_registry",
]
