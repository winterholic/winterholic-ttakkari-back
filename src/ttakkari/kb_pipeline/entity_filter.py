"""L0 엔터티 노이즈 필터. 결정적 규칙만 쓴다(모델·사전 없음).

L0 가 만든 엔터티 후보의 대부분은 개념이 아니라 문서 구조의 부산물이었다: 번호가 붙은 일반 제목('1. 개요 및 배경'),
평범한 단어의 인라인 코드(`files`, `agent`), 파일 경로, 명령행, 옵션, 설정 대입문 전체 등. 아래 기준으로 걸러낸다.

기준
1. 제목: 번호 접두를 떼고, 일반 제목 목록·문장형(마침표/물음표, 너무 김)·너무 짧은 것은 개념으로 보지 않는다.
2. 인라인 코드: 공백 포함(명령행/문구), '-' 옵션, 경로(/, \\), 확장자 붙은 파일명, URL 은 엔터티가 아니다.
   대입문(a=b)은 키만 본다. 한 단어짜리는 식별자 모양(밑줄·점·콜론·카멜케이스·숫자 혼합·전부 대문자 3자 이상·함수 호출형)일 때만 남긴다.
3. 빈도: 식별자 모양이 약한 한 단어 코드는 문서 안에서 4번 이상 나올 때만 남긴다(WEAK_SYMBOL_MIN_MENTIONS). 명시 정의로 나온 후보는 항상 남긴다.
4. 상한: 한 문서(리비전)에서 L0 엔터티는 점수 순 최대 MAX_L0_ENTITIES 개.
"""

from __future__ import annotations

import re

MAX_L0_ENTITIES = 60
MAX_HEADING_CHARS = 60
MAX_HEADING_WORDS = 8

_NUMBERING = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*|[IVXivx]+|[A-Za-z]|[가-힣])[.)]|\(\d+\)|\d+(?:\.\d+)+)\s+"
)
_MD_DECORATION = re.compile(r"^[#>*\-\s]+|[*_`]+|[:：]+\s*$")

GENERIC_HEADINGS = {
    # Korean
    "개요", "소개", "배경", "목적", "목표", "비목표", "범위", "요약", "결론", "참고", "참고 자료", "참고자료", "목차",
    "동작", "설정", "기본 설정", "구현", "테스트", "검증", "결정", "근거", "한계", "예시", "예제", "사용법", "사용 방법",
    "문제", "해결", "정책", "원칙", "요구사항", "요구 사항", "현재 상태", "다음 단계", "변경 이력", "부록", "기타", "비고",
    "주의", "주의사항", "참고사항", "세부 설정", "세부사항", "상세", "설명", "정의", "용어", "개념", "구조", "흐름", "절차",
    "운영", "배포", "롤백", "이행", "평가", "비용", "위험", "리스크", "미해결", "남은 일", "api", "오류", "에러",
    "개요 및 배경", "개요와 배경", "배경과 목적", "목적과 범위", "범위와 한계", "결정과 근거", "근거와 기록",
    # English
    "overview", "introduction", "background", "summary", "conclusion", "references", "appendix", "notes", "note",
    "usage", "example", "examples", "limitations", "goals", "non-goals", "non goals", "scope", "purpose", "context",
    "details", "description", "definition", "terminology", "design", "implementation", "testing", "verification",
    "decision", "decisions", "rationale", "status", "todo", "faq", "changelog", "history", "contents", "toc",
    "configuration", "settings", "behavior", "operations", "deployment", "rollback", "risks", "errors",
}

# 문장형 제목: 마침표/물음표 또는 서술 어미. '개요'처럼 '요'로 끝나는 명사는 문장이 아니다.
_SENTENCE_END = re.compile(r"[.!?。]$|(?:입니다|합니다|이다|한다|된다|있다|없다|해요|이에요|예요)$")
_PATHLIKE = re.compile(r"[/\\]|^\.{1,2}[\w-]|^~")
_FILE_EXT = re.compile(
    r"\.(?:json|ya?ml|toml|md|txt|py|ts|tsx|js|jsx|sh|sql|lock|cfg|ini|env|log|csv|html|css|gguf|plist|xml|png|jpe?g|pdf)$",
    re.IGNORECASE,
)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]*$")


def clean_heading(text: str) -> str:
    """제목에서 번호 접두와 마크다운 장식을 떼고 공백을 정리한다."""
    value = (text or "").strip()
    value = _MD_DECORATION.sub("", value).strip()
    prev = None
    while prev != value:
        prev = value
        value = _NUMBERING.sub("", value, count=1).strip()
    return re.sub(r"\s+", " ", value)


def is_concept_heading(text: str) -> bool:
    """정리된 제목이 개념 이름으로 쓸 만한가. 일반 제목·문장형·너무 길거나 짧은 제목은 아니다."""
    name = clean_heading(text)
    if len(name) < 2 or len(name) > MAX_HEADING_CHARS:
        return False
    if len(name.split()) > MAX_HEADING_WORDS:
        return False
    if name.lower() in GENERIC_HEADINGS:
        return False
    if _SENTENCE_END.search(name) or "?" in name:
        return False
    return not name.isdigit()


def is_definable_term(text: str) -> bool:
    """명시 정의('X 는 Y 이다')의 정의 대상이 이름으로 쓸 만한가. 일반 제목 목록은 적용하지 않는다(정의가 곧 근거다)."""
    name = clean_heading(text)
    if len(name) < 2 or len(name) > MAX_HEADING_CHARS or len(name.split()) > MAX_HEADING_WORDS:
        return False
    return not (_SENTENCE_END.search(name) or "?" in name or name.isdigit())


# 명령행 인라인 코드는 통째로 엔터티가 아니지만, 첫 토큰이 알려진 CLI 도구면 그 도구가 언급된 것이다.
KNOWN_CLI = {"kubectl", "docker", "git", "helm", "cargo", "npm", "uv", "pip", "python", "pnpm", "make", "alembic", "pytest", "ruff"}


def strong_identifier_shape(symbol: str) -> bool:
    """한 단어 식별자가 우연한 일반 단어가 아니라 코드 식별자 모양인가."""
    if "(" in symbol and symbol.endswith(")") and len(symbol.split("(")[0]) >= 3:
        return True
    if "_" in symbol or "." in symbol or "::" in symbol:
        return True
    if "-" in symbol and re.search(r"[A-Za-z]", symbol):
        return True
    if _CAMEL.search(symbol):
        return True
    if any(c.isdigit() for c in symbol) and any(c.isalpha() for c in symbol):
        return True
    return symbol.isupper() and len(symbol) >= 3 and symbol.isalpha()


def classify_inline_symbol(raw: str) -> tuple[str, str, bool] | None:
    """인라인 코드 조각을 (이름, kind, 강한 식별자 모양 여부)로 분류한다. 엔터티가 아니면 None.

    kind 는 'api'(호출형) 또는 'symbol'.
    """
    sym = (raw or "").strip()
    if len(sym) < 2 or sym.isdigit():
        return None
    if any(c.isspace() for c in sym):
        first = sym.split()[0]
        return (first, "symbol", True) if first in KNOWN_CLI else None
    if _URL.match(sym) or _PATHLIKE.search(sym) or _FILE_EXT.search(sym):
        return None
    if sym.startswith("-"):
        return None
    if "=" in sym:
        key = sym.split("=", 1)[0].strip().lstrip("-")
        if not key or " " in key:
            return None
        sym = key
    if not _IDENT.match(sym.rstrip("()")):
        return None

    is_call = sym.endswith("()") or ("(" in sym and sym.endswith(")"))
    name = sym.split("(", 1)[0] if is_call else sym
    if len(name) < 3:
        return None
    strong = strong_identifier_shape(sym)
    if not strong and name.lower() in GENERIC_HEADINGS:
        return None
    return name, ("api" if is_call else "symbol"), strong
