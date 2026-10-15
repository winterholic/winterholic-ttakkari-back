"""Same-input baseline vs improved benchmark; never claims meeting accuracy from read speech.

Manifest: [{"id": "sample", "audio_path": "/abs/file.wav", "reference": "..."}]
Reference may be null. Private transcript text is excluded from the output report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from ttakkari.meetings.local_stt.core import error_rates
from ttakkari.meetings.local_stt.runner import run_worker as _run_worker


async def run_worker(audio_path: Path, params: dict, settings) -> dict:
    # Historical profiles are allowed only from this standalone measurement entry.
    return await _run_worker(audio_path, params, settings, production=False)


def aggregate(rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("Benchmark has no samples")
    measured = [
        error_rates(row["reference"], row["hypothesis"])
        for row in rows
        if row.get("reference")
    ]
    characters = sum(m["reference_characters"] for m in measured)
    words = sum(m["reference_words"] for m in measured)
    return {
        "cer": sum(m["character_edits"] for m in measured) / characters
        if characters
        else None,
        "wer": sum(m["word_edits"] for m in measured) / words if words else None,
        "labelled_samples": len(measured),
        "reference_characters": characters,
        "reference_words": words,
        "der": None,
        "overlap_performance": None,
        "omission_rate": None,
        "hallucination_rate": None,
    }


async def run(args) -> dict:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not manifest:
        raise ValueError("Benchmark has no samples")
    report = {
        "schema_version": 1,
        "normalization": "NFC; CER excludes whitespace; WER whitespace tokens; punctuation kept",
        "profiles": {},
        "unmeasured": {
            "der": "No speaker reference annotations",
            "overlap_performance": "No overlap annotations or separation model",
            "omission_rate": "No timestamped human reference",
            "hallucination_rate": "No human segment judgements",
            "one_hour_wall_time": "No one-hour input processed; short clips must not be extrapolated",
        },
    }
    with tempfile.TemporaryDirectory(prefix="ttk-bench-") as home:
        settings = SimpleNamespace(
            home=Path(home),
            local_stt_python=args.python,
            local_stt_model_path=args.model,
            local_stt_timeout_seconds=7200,
            local_stt_max_audio_seconds=7200,
            local_stt_cpu_threads=args.threads,
        )
        for profile in ("baseline", "improved"):
            results, score_rows = [], []
            for entry in manifest:
                params = {k: entry[k] for k in ("start", "end", "terms") if k in entry}
                result = await run_worker(
                    Path(entry["audio_path"]), {**params, "profile": profile}, settings
                )
                score_rows.append(
                    {"reference": entry.get("reference"), "hypothesis": result["text"]}
                )
                results.append(
                    {
                        "id": entry["id"],
                        "audio_sha256": result["audio_sha256"],
                        "start": result["start"],
                        "end": result["end"],
                        "metrics": result["metrics"],
                        "engine": result["engine"],
                        "engine_version": result["engine_version"],
                        "runtime_version": result["runtime_version"],
                        "model": result["model"],
                        "model_version": result["model_version"],
                        "options": result["options"],
                        "duration_after_vad": result["duration_after_vad"],
                        "segments": len(result["segments"]),
                        "text_characters": len(result["text"]),
                        "flagged_segments": sum(
                            bool(s["flags"]) for s in result["segments"]
                        ),
                        "error_rates": error_rates(entry["reference"], result["text"])
                        if entry.get("reference")
                        else None,
                    }
                )
                print(
                    profile,
                    entry["id"],
                    "seconds",
                    round(result["metrics"]["wall_seconds"], 3),
                    flush=True,
                )
            report["profiles"][profile] = {
                "samples": results,
                "aggregate": aggregate(score_rows),
            }
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True)
    parser.add_argument("--threads", type=int, default=4)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
