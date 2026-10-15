"""Optional Community-1 adapter. Only local snapshots; identities are a separate layer."""

from __future__ import annotations

import gc
import time
from pathlib import Path

from .core import overlap_regions


def serialize_output(output, *, offset: float, duration: float) -> dict:
    def turns(annotation):
        result = [
            {"start": float(t.start), "end": float(t.end), "speaker_id": str(label)}
            for t, _, label in annotation.itertracks(yield_label=True)
        ]
        overlap_regions(result, duration=duration)  # fail closed on corrupt timestamps
        return [
            {**t, "start": t["start"] + offset, "end": min(t["end"], duration) + offset}
            for t in result
        ]

    return {
        "turns": turns(output.speaker_diarization),
        "exclusive_turns": turns(output.exclusive_speaker_diarization),
    }


def diarize(
    audio, *, model_path: Path, offset: float, device: str = "cpu", cpu_threads: int = 4
) -> dict:
    model_path = Path(model_path)
    if not model_path.is_dir() or not (model_path / "config.yaml").is_file():
        raise ValueError("Community-1 local snapshot missing")
    if device not in ("cpu", "mps", "cuda"):
        raise ValueError("Unsupported diarization device")
    import torch
    from pyannote.audio import Pipeline

    torch.set_num_threads(cpu_threads)
    started = time.perf_counter()
    pipeline = Pipeline.from_pretrained(str(model_path.resolve()), token=False)
    if pipeline is None:
        raise ValueError("Community-1 local snapshot incomplete")
    pipeline.to(torch.device(device))
    loaded = time.perf_counter()
    with torch.inference_mode():
        output = pipeline(
            {
                "waveform": torch.from_numpy(audio.copy()).unsqueeze(0),
                "sample_rate": 16000,
            }
        )
    result = serialize_output(output, offset=offset, duration=len(audio) / 16000)
    result.update(
        {
            "engine": "pyannote-community-1",
            "model_version": model_path.name,
            "device": device,
            "load_seconds": loaded - started,
            "inference_seconds": time.perf_counter() - loaded,
        }
    )
    del pipeline, output
    gc.collect()
    if device == "mps":
        torch.mps.synchronize()
        torch.mps.empty_cache()
    return result
