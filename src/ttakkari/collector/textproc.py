"""Collector Platform: normalization, language identification and duplicate detection (CP-S2-06 / L0). Pure functions.

Normalization keeps the author's text: NFC only (NFKC would rewrite compatibility characters such as full-width
forms), no case folding, no punctuation removal. Every change is counted so the raw -> extracted -> normalized chain
can be audited from hashes plus the step list. Duplicate detection normalizes harder on a separate copy that is never
stored as the document.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

NORMALIZER_VERSION = "norm-1"
LANGID_VERSION = "py3langid-0.4+script-1"
DEDUPE_VERSION = "dedupe-1:exact-sha256+minhash128-char5-lsh16x8@0.80"

_ZERO_WIDTH = dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD))  # ZW space/non-joiner/joiner, word joiner, BOM, soft hyphen
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[^\S\n]+")
_BLANKS = re.compile(r"\n{3,}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NormalizedText:
    text: str
    steps: tuple[tuple[str, int], ...]
    truncated: bool


def normalize_text(text: str, *, max_chars: int = 1_000_000) -> NormalizedText:
    steps: list[tuple[str, int]] = []

    def record(op: str, before: str, after: str) -> str:
        if before != after:
            changed = abs(len(before) - len(after)) or sum(x != y for x, y in zip(before, after, strict=False))
            steps.append((op, changed))
        return after

    t = record("nfc", text, unicodedata.normalize("NFC", text))
    t = record("line_endings", t, t.replace("\r\n", "\n").replace("\r", "\n"))
    t = record("strip_zero_width", t, t.translate(_ZERO_WIDTH))
    t = record("strip_control", t, _CONTROL.sub("", t))
    t = record("collapse_spaces", t, "\n".join(_SPACES.sub(" ", line).strip() for line in t.split("\n")))
    t = record("collapse_blank_lines", t, _BLANKS.sub("\n\n", t))
    t = record("trim", t, t.strip())
    truncated = len(t) > max_chars
    if truncated:
        t = t[:max_chars]
        steps.append(("truncate", len(text) - max_chars))
    return NormalizedText(t, tuple(steps), truncated)


# ---- language identification -------------------------------------------------------------------------------

@dataclass(frozen=True)
class LanguageResult:
    language: str  # BCP 47 primary subtag, or "und" when it cannot be told
    confidence: float
    method: str


def _script_counts(text: str) -> dict[str, int]:
    counts = {"hangul": 0, "kana": 0, "han": 0, "latin": 0, "other": 0}
    for ch in text:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            counts["hangul"] += 1
        elif 0x3040 <= o <= 0x30FF:
            counts["kana"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            counts["han"] += 1
        elif o < 0x250:
            counts["latin"] += 1
        else:
            counts["other"] += 1
    return counts


@lru_cache(maxsize=1)
def _identifier():
    from py3langid.langid import MODEL_DIR, MODEL_FILE, LanguageIdentifier

    return LanguageIdentifier.from_modelpath(MODEL_DIR / MODEL_FILE, norm_probs=True)


def identify_language(text: str, *, min_letters: int = 20) -> LanguageResult:
    """Script rules settle Korean and Japanese (the n-gram model is weakest on short CJK); the rest uses langid."""
    sample = text[:5000]
    counts = _script_counts(sample)
    letters = sum(counts.values())
    if letters < min_letters:
        return LanguageResult("und", 0.0, "too_short")
    if counts["hangul"] / letters >= 0.2:
        return LanguageResult("ko", round(min(0.99, 0.6 + counts["hangul"] / letters / 2), 3), "script")
    if counts["kana"] / letters >= 0.1:
        return LanguageResult("ja", 0.95, "script")
    lang, prob = _identifier().classify(sample)
    return LanguageResult(lang, round(float(prob), 3), "langid")


# ---- duplicate detection -----------------------------------------------------------------------------------

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
NUM_PERM = 128
BANDS = 16
ROWS = NUM_PERM // BANDS
SHINGLE = 5
NEAR_THRESHOLD = 0.80
MIN_NEAR_CHARS = 100
MAX_SHINGLE_CHARS = 100_000
_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)


@lru_cache(maxsize=1)
def _perms() -> tuple[np.ndarray, np.ndarray]:
    seed = hashlib.shake_256(b"ttakkari-collector-minhash-v1").digest(8 * 2 * NUM_PERM)
    values = np.frombuffer(seed, dtype=np.uint64)
    return values[:NUM_PERM] | np.uint64(1), values[NUM_PERM:]  # odd multipliers make x -> a*x+b a permutation mod 2^64


def dedupe_key_text(text: str) -> str:
    """NFKC + casefold, everything but letters and digits removed. Used only to compare, never stored as content."""
    return _NON_WORD.sub("", unicodedata.normalize("NFKC", text).casefold())


def minhash_signature(text: str) -> bytes | None:
    """128 x uint64 MinHash over character 5-grams of the dedupe key, or None when the text is too short to compare."""
    key = dedupe_key_text(text)[:MAX_SHINGLE_CHARS]
    if len(key) < MIN_NEAR_CHARS:
        return None
    shingles = {key[i : i + SHINGLE] for i in range(len(key) - SHINGLE + 1)}
    hashes = np.fromiter(
        (int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big") for s in shingles),
        dtype=np.uint64,
        count=len(shingles),
    )
    a, b = _perms()
    sig = np.full(NUM_PERM, np.iinfo(np.uint64).max, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for start in range(0, len(hashes), 4096):  # bounded memory: 128 x 4096 x 8 bytes per block
            block = hashes[start : start + 4096]
            sig = np.minimum(sig, np.min((block[None, :] * a[:, None] + b[:, None]) & _MASK, axis=1))
    return sig.astype("<u8").tobytes()


def band_hashes(signature: bytes) -> list[str]:
    return [
        hashlib.blake2b(signature[i * ROWS * 8 : (i + 1) * ROWS * 8], digest_size=8).hexdigest() for i in range(BANDS)
    ]


def estimated_jaccard(a: bytes, b: bytes) -> float:
    x = np.frombuffer(a, dtype="<u8")
    y = np.frombuffer(b, dtype="<u8")
    return float(np.mean(x == y))
