"""Bounded recovery and explicit presentation folding. Acoustic originals stay immutable."""

from __future__ import annotations

import re
from copy import deepcopy

from .core import flag_repetitions

REPETITION_FLAGS = {"repetition", "phrase_repetition", "consecutive_duplicate"}
RETRY_FLAGS = REPETITION_FLAGS | {"high_compression"}


def key(text):
    return re.sub(r"[\W_]+", "", text)


def fold_text(text: str) -> str:
    # Keep the first literal phrase, including spaces/punctuation; never invent speech.
    pattern = re.compile(r"(.{4,160}?)(?:\s*\1){2,}", re.DOTALL)
    previous = None
    while previous != text:
        previous, text = text, pattern.sub(r"\1", text)
    return text.strip()


def repetition_metrics(segments: list[dict]) -> dict:
    flagged = flag_repetitions(
        [
            {**s, "flags": [f for f in s["flags"] if f not in REPETITION_FLAGS]}
            for s in segments
        ]
    )
    repeated = [s for s in flagged if REPETITION_FLAGS.intersection(s["flags"])]
    intervals = []
    for s in sorted(repeated, key=lambda x: x["start"]):
        if intervals and s["start"] <= intervals[-1][1]:
            intervals[-1][1] = max(intervals[-1][1], s["end"])
        else:
            intervals.append([s["start"], s["end"]])
    runs = []
    for row in repeated:
        if (
            runs
            and key(row["original_text"]) == runs[-1]["key"]
            and row["start"] - runs[-1]["end"] <= 5
        ):
            runs[-1]["end"] = max(runs[-1]["end"], row["end"])
            runs[-1]["segments"] += 1
        else:
            runs.append(
                {
                    "key": key(row["original_text"]),
                    "start": row["start"],
                    "end": row["end"],
                    "segments": 1,
                }
            )
    return {
        "repetition_runs": len(runs),
        "longest_run_segments": max((r["segments"] for r in runs), default=0),
        "longest_run_span_seconds": max(
            (r["end"] - r["start"] for r in runs), default=0
        ),
        "repeated_segments": len(repeated),
        "repeated_characters": sum(len(s["original_text"]) for s in repeated),
        "repeated_seconds": sum(b - a for a, b in intervals),
        "repeated_regions": len(intervals),
    }


def recover_quality(segments: list[dict], *, audio, offset: float, recognize) -> dict:
    originals = flag_repetitions(deepcopy(segments))
    presentation, retries = [], []
    i, used = 0, 0.0
    # Recovery budget is explicit; exhausted regions are still folded/flagged.
    max_retry_seconds = 120
    while i < len(originals):
        s = originals[i]
        if not RETRY_FLAGS.intersection(s["flags"]):
            presentation.append(deepcopy(s))
            i += 1
            continue
        j = i + 1
        while (
            j < len(originals)
            and RETRY_FLAGS.intersection(originals[j]["flags"])
            and originals[j]["start"] - originals[j - 1]["end"] <= 1
            and originals[j]["end"] - s["start"] <= 25
        ):
            j += 1
        a, b = s["start"], max(x["end"] for x in originals[i:j])
        chosen = []
        cursor = a
        while recognize is not None and cursor < b and used < max_retry_seconds:
            stop = min(cursor + 8, b, cursor + max_retry_seconds - used)
            samples = audio[
                round((cursor - offset) * 16000) : round((stop - offset) * 16000)
            ]
            used += stop - cursor
            failure = None
            try:
                candidate = recognize(samples, cursor)
                cs = flag_repetitions(candidate["segments"])
            except Exception as exc:  # noqa: BLE001 -- optional retry must retain source evidence
                candidate, cs = {"raw_segments": []}, []
                failure = type(exc).__name__
            retries.append(
                {
                    "start": cursor,
                    "end": stop,
                    "temperature": [0.4, 0.6, 0.8, 1.0],
                    "raw_segments": candidate["raw_segments"],
                    "status": "decoded" if cs else "empty",
                }
            )
            if not cs:
                # A silent retry is not evidence that the original speech was
                # absent. Preserve this window even when later retries succeed.
                for original in originals[i:j]:
                    if original["start"] < stop and original["end"] > cursor:
                        row = deepcopy(original)
                        row["start"] = max(row["start"], cursor)
                        row["end"] = min(row["end"], stop)
                        row["words"] = []
                        cs.append(row)
                retries[-1]["status"] = (
                    "failed_preserved" if failure else "empty_preserved"
                )
                if failure:
                    retries[-1]["failure_type"] = failure
            chosen.extend(cs)
            cursor = stop
        # Incomplete or empty retries never silently erase speech.
        if cursor < b or not chosen:
            chosen = deepcopy(originals[i:j])
            retries.append(
                {
                    "start": a,
                    "end": b,
                    "status": "budget_exceeded" if cursor < b else "empty_preserved",
                }
            )
        for row in chosen:
            row["verification_status"] = "needs_review"
            row["flags"] = list(dict.fromkeys(row["flags"] + ["quality_retry"]))
        presentation.extend(chosen)
        for row in originals[i:j]:
            row["verification_status"] = "needs_review"
        i = j
    before_fold = sum(len(s["original_text"]) for s in presentation)
    presentation = flag_repetitions(presentation)
    folded = []
    for s in presentation:
        row = deepcopy(s)
        if REPETITION_FLAGS.intersection(row["flags"]):
            row["original_text"] = fold_text(row["original_text"])
            row["verification_status"] = "needs_review"
            row["flags"] = list(dict.fromkeys(row["flags"] + ["repetition_folded"]))
            row["words"] = []  # source word offsets no longer describe the folded text
            if (
                folded
                and "repetition_folded" in folded[-1]["flags"]
                and key(folded[-1]["original_text"]) == key(row["original_text"])
                and row["start"] - folded[-1]["end"] <= 5
            ):
                folded[-1]["end"] = max(folded[-1]["end"], row["end"])
                continue
        folded.append(row)
    return {
        "segments": originals,
        "presentation_segments": folded,
        "quality_retries": retries,
        "quality": {
            "before": repetition_metrics(originals),
            "after": repetition_metrics(folded),
            "retry_audio_seconds": used,
            "retry_budget_seconds": max_retry_seconds,
            "folded_characters": before_fold
            - sum(len(s["original_text"]) for s in folded),
        },
    }
