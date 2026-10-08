from __future__ import annotations

import mimetypes
from pathlib import Path

CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh", ".zsh", ".bash", ".ps1", ".sql", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".xml", ".css", ".scss", ".vue", ".svelte", ".dart", ".lua", ".r", ".txt", ".log", ".cfg",
    ".conf", ".gradle", ".dockerfile", ".graphql", ".proto", ".env.example", ".lock",
}
EXTRA_MIME = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".jsx": "text/javascript",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".toml": "text/x-toml",
    ".webp": "image/webp",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# kind 는 프론트 뷰어 선택 키다. 값이 바뀌면 프론트 계약도 같이 바뀐다.
KINDS = ("markdown", "html", "pdf", "pptx", "docx", "sheet", "image", "code", "other")


def detect(path: Path) -> tuple[str, str]:
    """(mime_type, kind)."""
    ext = path.suffix.lower()
    name = path.name.lower()
    mime = EXTRA_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ext in (".md", ".markdown"):
        kind = "markdown"
    elif ext in (".html", ".htm"):
        kind = "html"
    elif ext == ".pdf":
        kind = "pdf"
    elif ext in (".pptx", ".ppt"):
        kind = "pptx"
    elif ext in (".docx", ".doc"):
        kind = "docx"
    elif ext in (".xlsx", ".xls", ".csv", ".tsv"):
        kind = "sheet"
    elif mime.startswith("image/"):
        kind = "image"
    elif ext in CODE_EXTS or name in ("dockerfile", "makefile") or mime.startswith("text/"):
        kind = "code"
    else:
        kind = "other"
    return mime, kind


def needs_server_preview(kind: str) -> bool:
    # 나머지 형식은 프론트가 원본을 직접 렌더한다(react-markdown, PDF.js, SheetJS, Monaco 등).
    return kind in ("pptx", "docx")
