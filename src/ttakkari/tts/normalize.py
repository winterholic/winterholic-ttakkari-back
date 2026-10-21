"""한국어 TTS 텍스트 정규화와 문장 청크 분할. 순수 함수만 두고 ML·I/O 의존을 두지 않는다.

실측(docs/tts)에서 숫자·날짜·금액·기호 표기가 발음 오류의 주된 원인이었다. 엔진이 알아서 읽게 두지 않고
읽는 말(한글)로 바꿔서 넘긴다. 같은 입력은 항상 같은 출력이어야 캐시 키가 안정된다.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["chunk_text", "normalize_text", "plain_text", "split_sentences"]

# ---------------------------------------------------------------- 수 읽기

_SINO = "영일이삼사오육칠팔구"
_SMALL_UNITS = ("", "십", "백", "천")
_BIG_UNITS = ("", "만", "억", "조", "경")
_NATIVE_ONES = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")
_NATIVE_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
_NATIVE_STANDALONE = ("", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉")


def sino(n: int) -> str:
    """정수를 한자어 수로 읽는다. 1만은 '만', 1억은 '일억'처럼 관용을 따른다."""
    if n < 0:
        return "마이너스 " + sino(-n)
    if n == 0:
        return "영"
    groups: list[tuple[int, int]] = []
    idx = 0
    while n:
        n, part = divmod(n, 10000)
        if part:
            groups.append((idx, part))
        idx += 1
    if idx > len(_BIG_UNITS):
        return " ".join(_SINO[int(d)] for d in str(n))
    words = []
    for idx, part in reversed(groups):
        words.append(_below_10000(part, drop_one=(idx == 1 and part == 1)) + _BIG_UNITS[idx])
    return " ".join(words) if len(words) > 1 else words[0]


def _below_10000(n: int, *, drop_one: bool = False) -> str:
    if drop_one:
        return ""
    out = []
    for pos in range(3, -1, -1):
        digit = (n // 10**pos) % 10
        if not digit:
            continue
        unit = _SMALL_UNITS[pos]
        # 십·백·천 앞의 '일'은 읽지 않는다. 일의 자리는 그대로 읽는다.
        out.append(unit if digit == 1 and pos else _SINO[digit] + unit)
    return "".join(out)


def native_counter(n: int) -> str | None:
    """단위 앞에서 쓰는 고유어 수(한·두·세…). 99를 넘으면 None(한자어로 읽는다)."""
    if not 1 <= n <= 99:
        return None
    tens, ones = divmod(n, 10)
    if n == 20:
        return "스무"
    return _NATIVE_TENS[tens] + _NATIVE_ONES[ones]


def digits(text: str) -> str:
    """전화번호·긴 식별자처럼 자릿수대로 읽는 수. 0은 '공'. 묶음 안은 붙여 읽는다."""
    return "".join("공" if ch == "0" else _SINO[int(ch)] for ch in text if ch.isdigit())


def _decimal(int_part: str, frac: str) -> str:
    whole = digits(int_part) if len(int_part) > 1 and int_part[0] == "0" and int_part != "0" else sino(int(int_part))
    return f"{whole}점{''.join('영' if c == '0' else _SINO[int(c)] for c in frac)}"


def _number_word(raw: str, *, native: bool = False) -> str:
    """콤마가 제거된 숫자 문자열 하나를 읽는다. 소수·긴 수·선행 0을 처리한다."""
    if "." in raw:
        int_part, frac = raw.split(".", 1)
        return _decimal(int_part or "0", frac)
    if len(raw) > 1 and raw[0] == "0":
        return digits(raw)
    if len(raw) > 15:
        return digits(raw)
    value = int(raw)
    if native:
        word = native_counter(value)
        if word:
            return word
    return sino(value)


# ---------------------------------------------------------------- 단위

# 단위 앞 수를 고유어(한·두·세)로 읽는 단위. 분(分)·초·일·월·년·건·회·층·호·차·위·세·배는 한자어다.
_NATIVE_UNITS = (
    "시간", "시", "개월", "개", "명", "마리", "번째", "번", "살", "권", "잔", "병", "장", "대", "곳",
    "가지", "채", "켤레", "송이", "그루", "벌", "통", "접시", "봉지", "자루", "쪽", "줄", "달",
)  # fmt: skip
_SINO_UNITS = (
    "분", "초", "일", "주", "년", "건", "회", "층", "호", "차", "위", "세", "배", "점", "원", "달러",
    "엔", "위안", "퍼센트", "프로", "도", "번지", "등", "급", "단계", "페이지", "쪽수",
)  # fmt: skip
# 개월은 한자어, 달은 고유어. '개월'이 '개'보다 먼저 맞도록 긴 단위부터 둔다.
_NATIVE_UNITS_SORTED = sorted(set(_NATIVE_UNITS) - {"개월"}, key=len, reverse=True)
_SINO_UNITS_SORTED = sorted({*_SINO_UNITS, "개월"}, key=len, reverse=True)

_LATIN_UNITS = {
    "km": "킬로미터", "cm": "센티미터", "mm": "밀리미터", "m": "미터", "kg": "킬로그램", "mg": "밀리그램",
    "g": "그램", "ml": "밀리리터", "l": "리터", "gb": "기가바이트", "mb": "메가바이트", "kb": "킬로바이트",
    "tb": "테라바이트", "ghz": "기가헤르츠", "mhz": "메가헤르츠", "khz": "킬로헤르츠", "hz": "헤르츠",
    "ms": "밀리초", "px": "픽셀", "fps": "에프피에스", "kbps": "킬로비피에스", "mbps": "메가비피에스",
    "gbps": "기가비피에스", "kw": "킬로와트", "w": "와트", "v": "볼트", "s": "초", "h": "시간",
    "min": "분", "sec": "초", "kwh": "킬로와트시", "ea": "개",
}  # fmt: skip
_SYMBOL_UNITS = {"℃": "도", "°C": "도", "℉": "화씨 도", "°F": "화씨 도", "°": "도", "㎏": "킬로그램", "㎞": "킬로미터"}

_NUM = r"\d+(?:\.\d+)?"
_NUM_COMMA = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"
_NUM_ANY = rf"(?:{_NUM_COMMA}|{_NUM})"


def _clean_num(raw: str) -> str:
    return raw.replace(",", "")


# ---------------------------------------------------------------- 영문 약어

_LETTER_NAMES = {
    "A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지", "H": "에이치",
    "I": "아이", "J": "제이", "K": "케이", "L": "엘", "M": "엠", "N": "엔", "O": "오", "P": "피",
    "Q": "큐", "R": "알", "S": "에스", "T": "티", "U": "유", "V": "브이", "W": "더블유", "X": "엑스",
    "Y": "와이", "Z": "제트",
}  # fmt: skip

# 글자대로 읽으면 어색하거나 단어처럼 읽는 약어·고유명. 키는 소문자.
_WORD_READINGS = {
    "json": "제이슨", "sql": "에스큐엘", "gui": "지유아이", "yaml": "야믈", "rest": "레스트",
    "oauth": "오어스", "nasa": "나사", "asap": "에이샙", "wifi": "와이파이", "wi-fi": "와이파이",
    "ios": "아이오에스", "macos": "맥오에스", "iphone": "아이폰", "ipad": "아이패드",
    "github": "깃허브", "gitlab": "깃랩", "docker": "도커", "kubernetes": "쿠버네티스",
    "python": "파이썬", "javascript": "자바스크립트", "typescript": "타입스크립트",
    "postgresql": "포스트그레스큐엘", "postgres": "포스트그레스", "redis": "레디스",
    "linux": "리눅스", "ubuntu": "우분투", "chatgpt": "챗지피티", "openai": "오픈에이아이",
    "r&d": "알앤디", "q&a": "큐앤에이", "b2b": "비투비", "b2c": "비투씨", "ci/cd": "씨아이 씨디",
    "ai": "에이아이", "pr": "피알", "ui": "유아이", "ux": "유엑스", "api": "에이피아이",
    "cpu": "씨피유", "gpu": "지피유", "ram": "램", "ssd": "에스에스디", "usb": "유에스비",
    "url": "유알엘", "http": "에이치티티피", "https": "에이치티티피에스", "html": "에이치티엠엘",
    "css": "씨에스에스", "sdk": "에스디케이", "cli": "씨엘아이", "ci": "씨아이", "cd": "씨디",
    "pdf": "피디에프", "faq": "에프에이큐", "ceo": "씨이오", "cto": "씨티오", "kpi": "케이피아이",
    "stt": "에스티티", "tts": "티티에스", "llm": "엘엘엠", "mps": "엠피에스", "onnx": "오닉스",
    "c++": "씨플러스플러스", "wav": "웨이브", "mp3": "엠피쓰리", "mp4": "엠피포", "ok": "오케이", "vs": "브이에스",
    "pm": "피엠", "am": "에이엠", "qa": "큐에이", "db": "디비", "os": "오에스", "pc": "피씨",
    "tv": "티브이", "gps": "지피에스", "nfc": "엔에프씨", "vpn": "브이피엔", "dns": "디엔에스",
    "ip": "아이피", "id": "아이디", "pin": "핀", "otp": "오티피", "sso": "에스에스오",
}  # fmt: skip

_ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9])")
_LATIN_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+&/\-]*[A-Za-z0-9+]|(?<![A-Za-z0-9])[A-Za-z]")


def _spell(token: str) -> str:
    parts = []
    for ch in token:
        if ch.isdigit():
            parts.append(_SINO[int(ch)])
        else:
            parts.append(_LETTER_NAMES.get(ch.upper(), ch))
    return "".join(parts)


def _read_token(token: str) -> str:
    known = _WORD_READINGS.get(token.lower())
    if known:
        return known
    if token.isalpha() and token.isupper() and len(token) <= 6:
        return _spell(token)  # 한 글자 대문자도 글자 이름으로 읽는다(A/B 의 A).
    if re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", token):
        return _spell(token)
    return token


def _read_latin_token(match: re.Match[str]) -> str:
    token = match.group(0)
    if len(token) == 1:
        return token  # 문장 속 외톨이 글자("I", "a")는 엔진에 맡긴다.
    if "/" in token:
        return " ".join(_read_token(p) for p in token.split("/") if p)
    return _read_token(token)


def read_latin(text: str) -> str:
    return _LATIN_WORD.sub(_read_latin_token, text)


# ---------------------------------------------------------------- 평문화

_FENCE = re.compile(r"(^|\n)[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(\n[ \t]*\2[ \t]*(?=\n|$)|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]]+", re.IGNORECASE)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U0001f1e6-\U0001f1ff☀-➿⬀-⯿️‍⃣←-⇿"
    "⌀-⏿■-◿✀-➿]+"
)
_ZERO_WIDTH = re.compile("[\u200b\u200c\u2060\ufeff\u00ad]")


def plain_text(text: str) -> str:
    """마크다운·HTML·URL·표를 걷어 낸 평문. 코드 블록은 통째로 읽지 않는다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HTML_COMMENT.sub(" ", text)
    text = _FENCE.sub("\n", text)
    text = _IMAGE.sub(lambda m: m.group(1), text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _URL.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    text = _INLINE_CODE.sub(lambda m: m.group(1), text)
    lines = []
    for line in text.split("\n"):
        if _TABLE_SEP.match(line) and "-" in line:
            continue
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
            line = ", ".join(cells) + "."
        elif re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>+\s?", "", line)
        line = re.sub(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+", "", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    return text


# ---------------------------------------------------------------- 숫자 규칙 (순서가 중요하다)

_DATE_FULL = re.compile(r"(?<!\d)(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)")
_DATE_KO = re.compile(r"(\d{4})\s*년(?:\s*(\d{1,2})\s*월)?(?:\s*(\d{1,2})\s*일)?")
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?")
_DAY_ONLY = re.compile(r"(?<![\d.])(\d{1,2})\s*일(?![가-힣])")
_TIME_COLON = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
_TIME_KO = re.compile(r"(?<!\d)(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?(?:\s*(\d{1,2})\s*초)?")
_PHONE = re.compile(r"(?<![\d-])(0\d{1,2})[-. ](\d{3,4})[-. ](\d{4})(?![\d-])")
_VERSION = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+){2,})(?![\w.])")
_CURRENCY_PREFIX = re.compile(rf"(₩|\$|€|£|¥|￦)\s*({_NUM_ANY})(?:\s*(만|억|조|천|백|k|K|M|B))?")
_CURRENCY_SUFFIX = re.compile(rf"({_NUM_ANY})\s*(원|달러|유로|엔|위안)")
_PERCENT = re.compile(rf"({_NUM_ANY})\s*(?:%|퍼센트|％)")
_PERCENT_P = re.compile(rf"({_NUM_ANY})\s*(?:%p|%P|퍼센트포인트)")
_RANGE_UNIT = (
    r"(?:℃|°C|°F|°|%|퍼센트|[A-Za-z]{1,4}(?![A-Za-z])|" + "|".join(_NATIVE_UNITS_SORTED + _SINO_UNITS_SORTED) + r")"
)
_RANGE = re.compile(rf"({_NUM_ANY})\s*({_RANGE_UNIT})?\s*[~～∼]\s*({_NUM_ANY})\s*({_RANGE_UNIT})?")
_SYMBOL_UNIT = re.compile(rf"({_NUM_ANY})\s*(℃|°C|℉|°F|°|㎏|㎞)")
_LATIN_UNIT = re.compile(rf"(?<![\w.])({_NUM_ANY})\s?([A-Za-z]{{1,4}})(?![A-Za-z0-9])")
# 단위 뒤에는 조사·어미만 올 수 있다. "2시스템"의 '시'를 단위로 읽는 오류를 막는다.
_AFTER_UNIT = (
    r"(?=$|[^가-힣]|이|가|은|는|을|를|의|에|도|로|으로|와|과|만|씩|째|쯤|간|짜리|정도|이상|이하|이나|나|부터|까지"
    r"|마다|당|여|넘|동안|입니다|이에요|예요|인|였|이고|이며|이라|이다|밖에|보다|처럼|정|내|전|후|뒤|앞)"
)
_COUNTER = re.compile(
    rf"(?<![\d.])({_NUM_ANY})\s*(" + "|".join(_NATIVE_UNITS_SORTED + _SINO_UNITS_SORTED) + rf"){_AFTER_UNIT}"
)
_FIRST_ORDER = re.compile(r"(?<![\d.])1\s*번째")
_BARE_NUMBER = re.compile(rf"(?<![\d.])({_NUM_ANY})(?![\d])")
_SIGNED = re.compile(r"(?<![\w.)])([+\-−])(?=\d)")
_HASH_NUMBER = re.compile(r"#\s?(\d+)")


_MONTH_READING = {6: "유월", 10: "시월"}


def _month(m: int) -> str:
    return _MONTH_READING.get(m, sino(m) + "월")


def _date_full(m: re.Match[str]) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"{sino(y)}년 {_month(mo)} {sino(d)}일"


def _date_ko(m: re.Match[str]) -> str:
    out = f"{sino(int(m.group(1)))}년"
    if m.group(2):
        out += f" {_month(int(m.group(2)))}"
        if m.group(3):
            out += f" {sino(int(m.group(3)))}일"
    elif m.group(3):
        out += f" {sino(int(m.group(3)))}일"
    return out


def _month_day(m: re.Match[str]) -> str:
    out = _month(int(m.group(1)))
    if m.group(2):
        out += f" {sino(int(m.group(2)))}일"
    return out


def _hour(h: int) -> str:
    # 13시 이상은 24시간제 표기라 한자어로 읽는다(십오 시). 12시 이하는 고유어(세 시).
    word = native_counter(h) if h <= 12 else None
    return f"{word} 시" if word else f"{sino(h)} 시"


def _time(h: int, minute: str | None, second: str | None) -> str:
    out = _hour(h)
    if minute and int(minute):
        out += f" {sino(int(minute))} 분"
    if second and int(second):
        out += f" {sino(int(second))} 초"
    return out


def _time_colon(m: re.Match[str]) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (m.group(3) and int(m.group(3)) > 59):
        return m.group(0)
    return _time(h, m.group(2), m.group(3))


def _time_ko(m: re.Match[str]) -> str:
    return _time(int(m.group(1)), m.group(2), m.group(3))


def _phone(m: re.Match[str]) -> str:
    return " ".join(digits(g) for g in m.groups())


def _version(m: re.Match[str]) -> str:
    return "점".join(sino(int(p)) for p in m.group(1).split("."))


_CURRENCY_NAMES = {"₩": "원", "￦": "원", "$": "달러", "€": "유로", "£": "파운드", "¥": "엔"}
_SCALE = {"만": 10_000, "억": 100_000_000, "조": 10**12, "천": 1000, "백": 100, "k": 1000, "K": 1000,
          "M": 1_000_000, "B": 1_000_000_000}  # fmt: skip


def _money_value(raw: str, scale: str | None) -> str:
    raw = _clean_num(raw)
    if scale and "." not in raw:
        return sino(int(raw) * _SCALE[scale])
    if scale:
        whole = _decimal(*raw.split(".", 1))
        return whole + {"만": "만", "억": "억", "조": "조", "천": "천", "백": "백"}.get(scale, "")
    return _number_word(raw)


def _currency_prefix(m: re.Match[str]) -> str:
    return f"{_money_value(m.group(2), m.group(3))} {_CURRENCY_NAMES[m.group(1)]}"


def _currency_suffix(m: re.Match[str]) -> str:
    return f"{_number_word(_clean_num(m.group(1)))} {m.group(2)}"


def _percent(m: re.Match[str]) -> str:
    return f"{_number_word(_clean_num(m.group(1)))} 퍼센트"


def _percent_point(m: re.Match[str]) -> str:
    return f"{_number_word(_clean_num(m.group(1)))} 퍼센트포인트"


def _counter_text(num: str, unit: str) -> str:
    raw = _clean_num(num)
    native = unit in _NATIVE_UNITS_SORTED
    word = _number_word(raw, native=native)
    return f"{word} {unit}"


def _unit_reading(unit: str | None) -> tuple[str, bool] | None:
    """범위 표기 뒤 단위를 (읽는 말, 고유어 수 여부)로 돌려준다. 모르는 단위는 None."""
    if not unit:
        return ("", False)
    if unit in ("%", "퍼센트"):
        return ("퍼센트", False)
    if unit in _SYMBOL_UNITS:
        return (_SYMBOL_UNITS[unit], False)
    low = unit.lower()
    if unit.isascii() and low in _LATIN_UNITS:
        return (_LATIN_UNITS[low], False)
    if unit in _NATIVE_UNITS_SORTED:
        return (unit, True)
    if unit in _SINO_UNITS_SORTED:
        return (unit, False)
    return None


def _range(m: re.Match[str]) -> str:
    left_unit, right_unit = m.group(2), m.group(4)
    # 앞 단위가 다음 단어의 첫머리일 수 있다("10~20명"처럼 앞 수에는 단위가 없는 경우가 보통이다).
    left = _unit_reading(left_unit)
    right = _unit_reading(right_unit)
    if left is None or right is None:
        return m.group(0)
    native = right[1] or left[1]
    a = _number_word(_clean_num(m.group(1)), native=native)
    b = _number_word(_clean_num(m.group(3)), native=native)
    left_part = f"{a} {left[0]}".strip()
    right_part = f"{b} {right[0]}".strip()
    return f"{left_part}에서 {right_part}"


def _symbol_unit(m: re.Match[str]) -> str:
    return f"{_number_word(_clean_num(m.group(1)))} {_SYMBOL_UNITS[m.group(2)]}"


def _latin_unit(m: re.Match[str]) -> str:
    unit = m.group(2).lower()
    if unit not in _LATIN_UNITS:
        return m.group(0)
    return f"{_number_word(_clean_num(m.group(1)))} {_LATIN_UNITS[unit]}"


def _counter(m: re.Match[str]) -> str:
    return _counter_text(m.group(1), m.group(2))


def _bare(m: re.Match[str]) -> str:
    return _number_word(_clean_num(m.group(1)))


def _signed(m: re.Match[str]) -> str:
    return "마이너스 " if m.group(1) in "-−" else "플러스 "


def convert_numbers(text: str) -> str:
    text = _PHONE.sub(_phone, text)
    text = _DATE_FULL.sub(_date_full, text)
    text = _VERSION.sub(_version, text)
    text = _DATE_KO.sub(_date_ko, text)
    text = _TIME_COLON.sub(_time_colon, text)
    text = _TIME_KO.sub(_time_ko, text)
    text = _MONTH_DAY.sub(_month_day, text)
    text = _DAY_ONLY.sub(lambda m: f"{sino(int(m.group(1)))}일", text)
    text = _CURRENCY_PREFIX.sub(_currency_prefix, text)
    text = _RANGE.sub(_range, text)
    text = _CURRENCY_SUFFIX.sub(_currency_suffix, text)
    text = _PERCENT_P.sub(_percent_point, text)
    text = _PERCENT.sub(_percent, text)
    text = _SYMBOL_UNIT.sub(_symbol_unit, text)
    text = _LATIN_UNIT.sub(_latin_unit, text)
    text = _HASH_NUMBER.sub(lambda m: f"{sino(int(m.group(1)))} 번", text)
    text = _SIGNED.sub(_signed, text)
    text = _FIRST_ORDER.sub("첫 번째", text)
    text = _COUNTER.sub(_counter, text)
    text = _BARE_NUMBER.sub(_bare, text)
    return text


# ---------------------------------------------------------------- 기호·공백

_SYMBOL_WORDS = (
    ("→", ", "), ("←", ", "), ("⇒", ", "), ("↔", " 와 "), ("×", " 곱하기 "), ("÷", " 나누기 "),
    ("±", " 플러스마이너스 "), ("≥", " 이상 "), ("≤", " 이하 "), ("≈", " 약 "), ("≠", " 같지 않음 "),
    ("™", ""), ("®", ""), ("©", ""), ("§", ""), ("※", ""), ("•", ", "), ("·", " "),
)  # fmt: skip
_CORP = re.compile(r"\(\s*주\s*\)")
_LANG_SHARP = re.compile(r"\b([CF])#")
_AMP_ACRONYM = re.compile(r"(?<![A-Za-z])([A-Za-z]{1,2})\s?&\s?([A-Za-z]{1,2})(?![A-Za-z])")
_AMP = re.compile(r"\s*&\s*")
_PLUS = re.compile(r"(?<=[가-힣A-Za-z0-9])\s*\+\s*(?=[가-힣A-Za-z0-9])")
_EQUALS = re.compile(r"\s*=\s*")
_SLASH_ACRONYM = re.compile(r"(?<![\w])([A-Za-z]{1,6})/([A-Za-z]{1,6})(?![\w])")
_TILDE_TRAILING = re.compile(r"(?<=[^\s~～∼])[~～∼]+(?=\s|$|[.,!?])")
_TILDE_BETWEEN = re.compile(r"\s*[~～∼]+\s*")
_REPEAT_PUNCT = re.compile(r"([.!?])\1{1,}")
_DASH = re.compile(r"\s+[-–—]+\s+|[–—]+")


def _slash_acronym(m: re.Match[str]) -> str:
    a, b = m.group(1), m.group(2)
    if a.isupper() and b.isupper():
        return f"{_read_token(a)} {_read_token(b)}"
    return f"{a} {b}"


def _symbols(text: str) -> str:
    text = _CORP.sub("주식회사", text)
    text = _LANG_SHARP.sub(lambda m: _LETTER_NAMES[m.group(1)] + "샵", text)
    text = text.replace("...", "…").replace("‥", "…")
    for src, dst in _SYMBOL_WORDS:
        text = text.replace(src, dst)
    text = _SLASH_ACRONYM.sub(_slash_acronym, text)
    text = _AMP_ACRONYM.sub(lambda m: f"{_spell(m.group(1))}앤{_spell(m.group(2))}", text)
    text = _AMP.sub(" 앤 ", text)
    text = _PLUS.sub(" 플러스 ", text)
    text = _EQUALS.sub(" 이퀄 ", text)
    text = text.replace("@", " 앳 ")
    text = _TILDE_TRAILING.sub("", text)
    text = _TILDE_BETWEEN.sub("에서 ", text)
    text = _DASH.sub(", ", text)
    for ch in "#*^|\\_<>{}[]":
        text = text.replace(ch, " ")
    return text


def _final_cleanup(text: str) -> str:
    text = re.sub(r"[ \t 　]+", " ", text)
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)
    text = re.sub(r"([,;:])\1+", r"\1", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = _REPEAT_PUNCT.sub(r"\1", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \n,;")


def normalize_text(text: str) -> str:
    """평문화 → 이모지·특수문자 → 숫자·날짜·금액·단위 → 영문 약어 → 공백 정리. NFC 로 끝낸다."""
    text = unicodedata.normalize("NFC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = plain_text(text)
    text = _EMOJI.sub(" ", text)
    text = convert_numbers(text)
    text = _symbols(text)
    text = read_latin(text)
    text = _final_cleanup(text)
    return unicodedata.normalize("NFC", text)


# ---------------------------------------------------------------- 문장·청크 분할

_SENTENCE_END = re.compile(r"(?<=[.!?…。？！])(?:[\"')\]」』”’]*)\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """정규화된 텍스트를 문장 단위로 나눈다. 종결 부호와 줄바꿈이 경계다."""
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    return [p for p in parts if re.search(r"[\w가-힣]", p)]


def _split_long(sentence: str, limit: int) -> list[str]:
    """한 문장이 limit 를 넘으면 쉼표 등 절 경계에서, 없으면 공백에서 자른다."""
    if len(sentence) <= limit:
        return [sentence]
    pieces = [p for p in re.split(r"(?<=[,;:])\s+", sentence) if p]
    out: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(_split_by_space(piece, limit))
            continue
        if current and len(current) + 1 + len(piece) > limit:
            out.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        out.append(current)
    return out


def _split_by_space(text: str, limit: int) -> list[str]:
    words = text.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        while len(word) > limit:  # 공백 없는 긴 토큰은 글자 수로 자른다.
            if current:
                out.append(current)
                current = ""
            out.append(word[:limit])
            word = word[limit:]
        if current and len(current) + 1 + len(word) > limit:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out


def chunk_text(text: str, max_chars: int = 120) -> list[str]:
    """원문 → 정규화 → 문장 → 상한 이내 청크. 청크는 캐시의 최소 단위이므로 문장 단위로 유지한다."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    chunks: list[str] = []
    for sentence in split_sentences(normalize_text(text)):
        chunks.extend(_split_long(sentence, max_chars))
    return chunks
