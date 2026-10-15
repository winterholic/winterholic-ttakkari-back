"""Deterministic, model-independent evidence handling (ADR 0025).

Flags are review hints, not confidence estimates or permission to remove speech.
Speaker labels describe acoustic clusters, never the identity of a participant.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Any


def term_prompt(terms: list[str]) -> str:
    clean = dict.fromkeys(
        re.sub(r"\s+", " ", t).strip()[:80] for t in terms if t.strip()
    )
    return ", ".join(clean)[:400]


def _number(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite model output")
    return number


def normalize_segments(
    raw: list[dict], *, duration: float, offset: float = 0
) -> list[dict]:
    if (
        not math.isfinite(duration)
        or duration < 0
        or not math.isfinite(offset)
        or offset < 0
    ):
        raise ValueError("Invalid audio interval")
    out = []
    for source in raw:
        start, end = _number(source["start"]), _number(source["end"])
        if (
            start is None
            or end is None
            or start < 0
            or end < start
            or end > duration + 0.02
        ):
            raise ValueError("Segment outside source audio interval")
        text = source["text"]
        if not isinstance(text, str):
            raise TypeError("Invalid segment text")
        metrics = {
            k: _number(source.get(k))
            for k in ("avg_logprob", "no_speech_prob", "compression_ratio")
        }
        flags = []
        if metrics["avg_logprob"] is not None and metrics["avg_logprob"] < -1:
            flags.append("low_logprob")
        if metrics["no_speech_prob"] is not None and metrics["no_speech_prob"] > 0.6:
            flags.append("possible_silence")
        if (
            metrics["compression_ratio"] is not None
            and metrics["compression_ratio"] > 2.4
        ):
            flags.append("high_compression")
        tokens = text.split()
        if len(tokens) >= 6 and max(Counter(tokens).values()) / len(tokens) >= 0.6:
            flags.append("repetition")
        words = []
        for word in source.get("words") or []:
            ws, we = _number(word["start"]), _number(word["end"])
            if ws is None or we is None or ws < 0 or we < ws or we > duration + 0.02:
                raise ValueError("Word outside source audio interval")
            words.append(
                {**word, "start": ws + offset, "end": min(we, duration) + offset}
            )
        out.append(
            {
                "start": start + offset,
                "end": min(end, duration) + offset,
                "original_text": text,
                "corrected_text": None,
                "speaker_id": None,
                "overlap": None,
                "verification_status": "unverified",
                "flags": flags,
                "words": words,
                **metrics,
            }
        )
    return out


def align_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Conservative interval alignment. Multiple sequential speakers are not overlap.

    Require a strict majority of segment duration for a single label. A tie or a
    speaker change remains ambiguous; word timestamps allow a caller to use finer units.
    No turns means diarization was unavailable, not that nobody overlapped.
    """
    checked = []
    for t in turns:
        a, b = _number(t["start"]), _number(t["end"])
        if a is None or b is None or a < 0 or b <= a or not t["speaker_id"]:
            raise ValueError("Invalid diarization turn")
        checked.append((a, b, t["speaker_id"]))
    out = []
    for s in segments:
        active = [
            (max(a, s["start"]), min(b, s["end"]), label)
            for a, b, label in checked
            if a < s["end"] and b > s["start"]
        ]
        # Union per speaker avoids double counting repeated annotations.
        spans = {}
        for a, b, label in sorted(active):
            merged = spans.setdefault(label, [])
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        durations = sorted(
            (
                (sum(b - a for a, b in intervals), label)
                for label, intervals in spans.items()
            ),
            reverse=True,
        )
        overlap = any(
            la != lb and max(a, c) < min(b, d)
            for i, (a, b, la) in enumerate(active)
            for c, d, lb in active[i + 1 :]
        )
        winner = None
        if (
            durations
            and durations[0][0] > (s["end"] - s["start"]) / 2
            and (len(durations) == 1 or durations[0][0] > durations[1][0])
        ):
            winner = durations[0][1]
        flags = list(s["flags"])
        if active and winner is None:
            flags.append("speaker_ambiguous")
        out.append(
            {
                **s,
                "speaker_id": winner,
                "overlap": overlap if turns else None,
                "flags": flags,
            }
        )
    return out


def _distance(a, b) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = row
    return previous[-1]


def error_rates(reference: str, hypothesis: str) -> dict:
    """NFC; CER excludes whitespace; WER uses whitespace tokens; keep punctuation.

    Empty reference has an undefined denominator, reported as None (including silence).
    Insertions can legitimately produce rates greater than one.
    """
    r, h = (
        unicodedata.normalize("NFC", reference),
        unicodedata.normalize("NFC", hypothesis),
    )
    rc, hc = "".join(r.split()), "".join(h.split())
    rw, hw = r.split(), h.split()
    ce, we = _distance(rc, hc), _distance(rw, hw)
    return {
        "cer": ce / len(rc) if rc else None,
        "wer": we / len(rw) if rw else None,
        "character_edits": ce,
        "reference_characters": len(rc),
        "word_edits": we,
        "reference_words": len(rw),
    }


def compare_candidate(original: str, candidate: str, *, reference: str | None) -> dict:
    if reference is None or not reference.strip():
        return {
            "adopt": False,
            "reason": "no_reference",
            "original": None,
            "candidate": None,
        }
    before, after = error_rates(reference, original), error_rates(reference, candidate)
    return {
        "adopt": after["cer"] < before["cer"] and after["wer"] <= before["wer"],
        "reason": "reference_comparison",
        "original": before,
        "candidate": after,
    }


def overlap_regions(turns: list[dict], *, duration: float) -> list[dict]:
    """Exact sweep of distinct active acoustic labels, without exclusive diarization."""
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid duration")
    events: dict[float, Counter] = {}
    for t in turns:
        a, b = _number(t["start"]), _number(t["end"])
        label = t["speaker_id"]
        if (
            a is None
            or b is None
            or not 0 <= a < b <= duration + 0.02
            or not isinstance(label, str)
            or not label
        ):
            raise ValueError("Invalid diarization turn")
        events.setdefault(a, Counter())[label] += 1
        events.setdefault(min(b, duration), Counter())[label] -= 1
    active, out, previous = Counter(), [], 0.0
    for point in sorted(events):
        labels = sorted(k for k, count in active.items() if count > 0)
        if len(labels) > 1 and point > previous:
            if out and out[-1]["end"] == previous and out[-1]["speaker_ids"] == labels:
                out[-1]["end"] = point
            else:
                out.append({"start": previous, "end": point, "speaker_ids": labels})
        active.update(events[point])
        previous = point
    return out


def annotate_diarization(
    segments: list[dict], turns: list[dict], *, duration: float
) -> dict:
    regions = overlap_regions(turns, duration=duration)
    aligned = align_speakers(segments, turns)
    for s in aligned:
        s["words"] = align_speakers([{**w, "flags": []} for w in s["words"]], turns)
        labels = {
            t["speaker_id"]
            for t in turns
            if t["start"] < s["end"] and t["end"] > s["start"]
        }
        if len(labels) > 1 and not s["overlap"]:
            s["flags"].append("speaker_change")
        if s["overlap"]:
            s["flags"].append("overlapping_speech")
        if not any(t["start"] < s["end"] and t["end"] > s["start"] for t in turns):
            s["flags"].append("speaker_uncovered")
        # Successful empty annotation is distinct from a diarizer that never ran.
        if not turns:
            s["overlap"] = False
    return {
        "segments": aligned,
        "turns": turns,
        "overlap_regions": regions,
        "speaker_count": len({t["speaker_id"] for t in turns}),
        "identity_status": "not_run",
        "speaker_identities": {},
    }


def compare_overlap_candidate(
    original: str, tracks: list[str], *, references: list[str] | None
) -> dict:
    """Permutation-invariant two-source transcript comparison, not an acoustic score.

    References must be independently human verified for this exact region. Concatenated
    references are a content metric; they do not measure speaker attribution or timing.
    """
    from itertools import permutations

    if not references or any(not r.strip() for r in references):
        return {"adopt": False, "reason": "no_reference"}
    if len(references) != 2 or len(tracks) != len(references):
        return {"adopt": False, "reason": "source_count_mismatch"}
    if any(not track.strip() for track in tracks):
        return {"adopt": False, "reason": "empty_source"}
    reference = " ".join(references)
    before = error_rates(reference, original)
    choices = []
    for order in permutations(range(len(tracks))):
        text = " ".join(tracks[i] for i in order)
        after = error_rates(reference, text)
        choices.append(
            (after["character_edits"], after["word_edits"], list(order), after)
        )
    _, _, order, after = min(choices)
    return {
        "adopt": after["cer"] < before["cer"] and after["wer"] <= before["wer"],
        "reason": "human_reference_comparison",
        "permutation": order,
        "original": before,
        "candidate": after,
    }


def correct_terms(segments: list[dict], replacements: dict[str, str]) -> list[dict]:
    """Explicit literal substitutions in a separate candidate field, never cascading."""
    if (
        not replacements
        or len(replacements) > 100
        or any(
            not a or not b or len(a) > 80 or len(b) > 80
            for a, b in replacements.items()
        )
    ):
        raise ValueError("Invalid correction dictionary")
    pattern = re.compile(
        "|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True))
    )
    out = []
    for s in segments:
        corrected = pattern.sub(lambda m: replacements[m[0]], s["original_text"])
        changed = corrected != s["original_text"]
        out.append(
            {
                **s,
                "corrected_text": corrected if changed else None,
                "flags": list(s["flags"])
                + (["term_correction_candidate"] if changed else []),
            }
        )
    return out


def flag_repetitions(segments: list[dict]) -> list[dict]:
    """Review hints only. Repeated valid speech can trigger these rules."""
    out = [{**s, "flags": list(s["flags"])} for s in segments]
    normalized = [re.sub(r"[\W_]+", "", s["original_text"]) for s in segments]
    for i, text in enumerate(normalized):
        # Minimum 3 chars/unit and four repetitions avoid short natural stutters.
        if (
            re.search(r"(.{3,80}?)\1{3,}", text)
            and "phrase_repetition" not in out[i]["flags"]
        ):
            out[i]["flags"].append("phrase_repetition")
    i = 0
    while i < len(out):
        j = i + 1
        while (
            j < len(out)
            and normalized[j] == normalized[i]
            and len(normalized[i]) >= 4
            and out[j]["start"] - out[j - 1]["end"] <= 5
        ):
            j += 1
        if j - i >= 3:
            for s in out[i:j]:
                s["flags"].append("consecutive_duplicate")
        i = j
    return out
