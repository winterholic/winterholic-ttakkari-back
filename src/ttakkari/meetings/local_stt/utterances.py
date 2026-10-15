"""Speaker aligned, reviewable consumer contract. Never infer human identities."""

from __future__ import annotations

from .core import align_speakers


def build_utterances(
    segments: list[dict], turns: list[dict], *, overlap_known: bool
) -> list[dict]:
    labels = {}
    for t in sorted(turns, key=lambda t: (t["start"], t["end"])):
        labels.setdefault(t["speaker_id"], f"화자 {len(labels) + 1}")
    out = []
    for position, segment in enumerate(segments):
        # Words give real boundaries; without them preserve ambiguity instead of duplicating text.
        units = [
            {**segment, **w, "original_text": w["word"]}
            for w in segment.get("words", [])
        ] or [segment]
        for unit in align_speakers(units, turns):
            speaker = unit["speaker_id"]
            overlap = unit["overlap"] if overlap_known else None
            flags = list(unit.get("flags", []))
            review = (
                unit.get("verification_status") == "needs_review"
                or speaker is None
                or overlap is True
            )
            row = {
                "start": unit["start"],
                "end": unit["end"],
                "speaker_id": speaker,
                "speaker_label": labels.get(speaker),
                "text": unit["original_text"],
                "verification_status": "needs_review" if review else "unverified",
                "overlap": overlap,
                "flags": flags,
                "segment_positions": [position],
            }
            if (
                out
                and (
                    out[-1]["speaker_id"],
                    out[-1]["overlap"],
                    out[-1]["verification_status"],
                )
                == (speaker, overlap, row["verification_status"])
                and row["start"] - out[-1]["end"] <= 0.8
                and row["end"] - out[-1]["start"] <= 25
            ):
                out[-1]["end"] = max(out[-1]["end"], row["end"])
                out[-1]["text"] += row["text"]
                out[-1]["flags"] = list(dict.fromkeys(out[-1]["flags"] + flags))
                out[-1]["segment_positions"] = list(
                    dict.fromkeys(out[-1]["segment_positions"] + [position])
                )
            else:
                out.append(row)
    for i, row in enumerate(out):
        row["id"] = str(i)
        row["text"] = row["text"].strip()
    return out


def render_transcript(utterances: list[dict]) -> str:
    return "\n".join(
        f"[{u['start']:.2f}–{u['end']:.2f}] {u['speaker_label'] or '화자 미상'}"
        f"{' [검증 필요]' if u['verification_status'] == 'needs_review' else ''}: {u['text']}"
        for u in utterances
        if u["text"]
    )
