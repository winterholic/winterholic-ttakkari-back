"""Ungated SpeechBrain ECAPA + VAD + cosine clustering, entirely local.

Apache-2.0 weights: speechbrain/spkrec-ecapa-voxceleb. This fallback cannot
measure overlapping speech. Cluster count is an estimate, never a verified headcount.
"""

from __future__ import annotations

import gc
import time
from pathlib import Path


def cluster_embeddings(vectors) -> list[int]:
    """Pruned cosine graph, eigengap count (1..10), deterministic spectral clustering.

    Follows SpeechBrain's AMI spectral-clustering recipe; no meeting-specific
    headcount or participant names enter the estimate.
    """
    import numpy as np
    from scipy.cluster.vq import kmeans2
    from scipy.linalg import eigh

    x = np.asarray(vectors, dtype=float)
    if not len(x):
        return []
    if not np.isfinite(x).all() or (np.linalg.norm(x, axis=1) == 0).any():
        raise ValueError("Invalid speaker embedding")
    if len(x) == 1:
        return [0]
    x = x / np.linalg.norm(x, axis=1, keepdims=True)
    affinity = np.maximum(x @ x.T, 0)
    if len(x) == 2:
        return [0, 0] if affinity[0, 1] >= 0.5 else [0, 1]
    keep = max(2, int(np.ceil(len(x) * 0.3)))
    cutoff = np.sort(affinity, axis=1)[:, -keep]
    affinity[affinity < cutoff[:, None]] = 0
    affinity = (affinity + affinity.T) / 2
    np.fill_diagonal(affinity, 0)
    degree = affinity.sum(axis=1)
    inv = 1 / np.sqrt(np.maximum(degree, 1e-12))
    laplacian = (
        np.diag((degree > 0).astype(float)) - affinity * inv[:, None] * inv[None, :]
    )
    eigenvalues, eigenvectors = eigh(
        laplacian, subset_by_index=[0, min(10, len(x) - 1)]
    )
    gaps = np.diff(eigenvalues)
    count = int(np.flatnonzero(np.isclose(gaps, gaps.max(), rtol=1e-6))[0] + 1)
    if count == 1:
        return [0] * len(x)
    _, labels = kmeans2(eigenvectors[:, :count], count, minit="++", seed=0)
    mapping = {}
    return [mapping.setdefault(int(label), len(mapping)) for label in labels]


def diarize_embeddings(
    audio, *, model_path: Path, offset: float, cpu_threads: int = 4
) -> dict:
    path = Path(model_path) / "embedding_model.ckpt"
    if not path.is_file():
        raise ValueError("Speaker embedding model missing")
    import numpy as np
    import torch
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    from speechbrain.lobes.features import Fbank
    from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
    from speechbrain.processing.features import InputNormalization

    torch.set_num_threads(cpu_threads)
    started = time.perf_counter()
    features = Fbank(n_mels=80).eval()
    norm = InputNormalization(norm_type="sentence", std_norm=False).eval()
    model = ECAPA_TDNN(
        input_size=80,
        channels=[1024, 1024, 1024, 1024, 3072],
        kernel_sizes=[5, 3, 3, 3, 1],
        dilations=[1, 2, 3, 4, 1],
        attention_channels=128,
        lin_neurons=192,
    ).eval()
    model.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
    loaded = time.perf_counter()
    spans = get_speech_timestamps(
        audio,
        VadOptions(
            min_silence_duration_ms=300,
            speech_pad_ms=300,
            max_speech_duration_s=3,
            min_speech_duration_ms=250,
        ),
    )
    windows = []
    for span in spans:
        # Very short speech remains uncovered/unknown rather than forced into a cluster.
        if span["end"] - span["start"] >= 8000:
            windows.append(span)
    vectors = []
    with torch.inference_mode():
        for span in windows:
            samples = torch.from_numpy(
                np.asarray(audio[span["start"] : span["end"]]).copy()
            ).unsqueeze(0)
            feats = norm(features(samples), torch.ones(1))
            vectors.append(model(feats).reshape(-1).cpu().numpy())
    labels = cluster_embeddings(vectors)
    turns = [
        {
            "start": s["start"] / 16000 + offset,
            "end": s["end"] / 16000 + offset,
            "speaker_id": f"SPEAKER_{label:02d}",
        }
        for s, label in zip(windows, labels, strict=True)
    ]
    version = Path(model_path) / "revision.txt"
    result = {
        "engine": "speechbrain-ecapa-clustering",
        "model_version": version.read_text().strip()
        if version.is_file()
        else Path(model_path).name,
        "device": "cpu",
        "turns": turns,
        "exclusive_turns": turns,
        "overlap_supported": False,
        "speaker_count_method": "spectral_cosine_keep30pct_eigengap_1to10",
        "window_max_seconds": 3,
        "minimum_window_seconds": 0.5,
        "load_seconds": loaded - started,
        "inference_seconds": time.perf_counter() - loaded,
    }
    del model, features, norm
    gc.collect()
    return result
