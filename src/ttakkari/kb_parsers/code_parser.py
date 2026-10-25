"""Code and structured configuration parser tracking AST and symbol boundaries."""

from __future__ import annotations

import ast
import re
import unicodedata
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus


class CodeParser(BaseParser):
    name = "code_symbol_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".sql",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".sh",
        ".zsh",
    }
    supported_mime_types: ClassVar[set[str]] = {
        "text/x-python",
        "application/javascript",
        "application/typescript",
        "text/x-go",
        "text/x-rust",
        "application/sql",
        "application/json",
        "application/yaml",
        "application/toml",
    }

    def _parse_python_ast(self, text: str) -> list[LocatorSpan]:
        """Extract top-level and nested classes/functions using Python AST."""
        spans: list[LocatorSpan] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return self._parse_regex_symbols(text)

        lines = text.splitlines(keepends=True)
        # Compute 0-based character offsets for line starts
        line_offsets: list[int] = [0]
        for l in lines:
            line_offsets.append(line_offsets[-1] + len(l))

        def get_span(node: ast.AST, symbol_path: str) -> LocatorSpan | None:
            if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                # lineno is 1-based
                s_line = node.lineno - 1
                e_line = node.end_lineno
                start_offset = line_offsets[s_line] if s_line < len(line_offsets) else 0
                end_offset = (
                    line_offsets[e_line] if e_line < len(line_offsets) else len(text)
                )
                return LocatorSpan(
                    start_offset=start_offset,
                    end_offset=end_offset,
                    section_path=[symbol_path],
                    chunk_kind="code",
                    extra={
                        "symbol_path": symbol_path,
                        "line_start": node.lineno,
                        "line_end": node.end_lineno,
                    },
                )
            return None

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                cls_span = get_span(node, node.name)
                if cls_span:
                    spans.append(cls_span)
                # Nested methods
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_path = f"{node.name}.{sub.name}"
                        m_span = get_span(sub, method_path)
                        if m_span:
                            spans.append(m_span)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_span = get_span(node, node.name)
                if fn_span:
                    spans.append(fn_span)

        return (
            sorted(spans, key=lambda s: s.start_offset)
            if spans
            else self._parse_regex_symbols(text)
        )

    def _parse_regex_symbols(self, text: str) -> list[LocatorSpan]:
        """Fallback declaration regex parser for TS/JS/Go/Rust/SQL/etc."""
        spans: list[LocatorSpan] = []
        pattern = re.compile(
            r"(?m)^(?:export\s+)?(?:pub\s+)?(?:async\s+)?(class|interface|type|enum|fn|function|def|func|struct)\s+([A-Za-z0-9_]+)"
        )
        matches = list(pattern.finditer(text))
        if not matches:
            # Whole file as single span
            return [
                LocatorSpan(
                    start_offset=0,
                    end_offset=len(text),
                    section_path=["Module"],
                    chunk_kind="code",
                )
            ]

        for idx, m in enumerate(matches):
            symbol_kind = m.group(1)
            symbol_name = m.group(2)
            start = m.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            spans.append(
                LocatorSpan(
                    start_offset=start,
                    end_offset=end,
                    section_path=[symbol_name],
                    chunk_kind="code",
                    extra={"symbol_path": symbol_name, "symbol_kind": symbol_kind},
                )
            )
        return spans

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
            text = data.decode("utf-8", errors="replace")

        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        text = unicodedata.normalize("NFC", text)

        is_python = filename and filename.lower().endswith(".py")
        if is_python:
            spans = self._parse_python_ast(text)
        else:
            spans = self._parse_regex_symbols(text)

        return ParsedDocument(
            normalized_body=text,
            metadata={
                "parser": self.name,
                "filename": filename,
                "content_type": content_type,
            },
            locator_spans=spans,
            status=ParseStatus.SUCCEEDED.value,
        )
