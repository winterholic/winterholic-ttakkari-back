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
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".ogg": "audio/ogg",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    # mimetypes 가 image/* 로 추측하지만 브라우저가 그리지 못하는 형식은 다운로드 안내(other)로 보낸다.
    ".dxf": "application/dxf",
    ".psd": "application/vnd.adobe.photoshop",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# kind 는 프론트 뷰어 선택 키다. 값이 바뀌면 프론트 계약도 같이 바뀐다.
KINDS = ("markdown", "html", "pdf", "pptx", "docx", "sheet", "image", "audio", "video", "code", "other")

# category 는 보관함 탐색·필터용 분류다.
CATEGORIES = (
    "document",
    "spreadsheet",
    "presentation",
    "image",
    "video",
    "audio",
    "code",
    "archive",
    "other",
)

DOCUMENT_EXTS = {
    ".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".rtf", ".odt", ".pages", ".hwp",
}

SPREADSHEET_EXTS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".ods", ".parquet", ".numbers",
}

PRESENTATION_EXTS = {
    ".pptx", ".ppt", ".key", ".odp",
}

ARCHIVE_EXTS = {
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz", ".txz", ".7z", ".rar",
}


def detect_category(path: Path, mime: str, kind: str) -> str:
    """탐색용 카테고리를 판별한다.

    애매한 형식 규칙:
    - .txt: 뷰어는 code(Monaco)이나 카테고리는 document(텍스트 문서).
    - .html: 뷰어는 html(인라인)이나 카테고리는 code(웹 코드).
    - .json: 뷰어는 code이나 카테고리는 code(설정·구조화 데이터).
    - .log: 뷰어는 code이나 카테고리는 code(로그 파일).
    - .csv, .tsv: 뷰어는 sheet이나 카테고리는 spreadsheet.
    - .zip, .tar 등: 뷰어는 other이나 카테고리는 archive.
    """
    ext = path.suffix.lower()
    name = path.name.lower()
    if ext in DOCUMENT_EXTS:
        return "document"
    if ext in SPREADSHEET_EXTS or kind == "sheet":
        return "spreadsheet"
    if ext in PRESENTATION_EXTS or kind == "pptx":
        return "presentation"
    if kind == "image" or mime.startswith("image/"):
        return "image"
    if kind == "video" or mime.startswith("video/") or ext in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv"):
        return "video"
    if kind == "audio" or mime.startswith("audio/") or ext in (
        ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aif", ".aiff", ".aac"
    ):
        return "audio"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if kind in ("code", "html") or ext in CODE_EXTS or name in ("dockerfile", "makefile"):
        return "code"
    return "other"



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
    elif mime.startswith("audio/"):
        kind = "audio"
    elif mime.startswith("video/"):
        kind = "video"
    elif ext in CODE_EXTS or name in ("dockerfile", "makefile") or mime.startswith("text/"):
        kind = "code"
    else:
        kind = "other"
    return mime, kind


def needs_server_preview(kind: str) -> bool:
    # 나머지 형식은 프론트가 원본을 직접 렌더한다(react-markdown, PDF.js, SheetJS, Monaco 등).
    return kind in ("pptx", "docx")
