"""Offline embedding worker; never imports app settings or opens the database."""

from __future__ import annotations

import json
import math
import os
import resource
import sys
import threading
import time
from pathlib import Path

from ttakkari.model_process import watch_parent


def compute(request: dict) -> dict:
    from fastembed import TextEmbedding

    started = time.perf_counter()
    model = TextEmbedding(
        request["model"],
        cache_dir=request["cache_dir"],
        threads=request["threads"],
        local_files_only=True,
    )
    loaded = time.perf_counter()
    default_prefix = "query: " if request["query"] else "passage: "
    # Per-text prefixes let one child embed a query and its passages together (collector triage).
    prefixes = request.get("prefixes") or [default_prefix] * len(request["texts"])
    if len(prefixes) != len(request["texts"]):
        raise ValueError("prefix_count_mismatch")
    vectors = []
    for raw in model.embed([p + t for p, t in zip(prefixes, request["texts"], strict=True)], batch_size=16):
        values = list(map(float, raw))
        norm = math.sqrt(sum(x * x for x in values)) or 1.0
        vectors.append([x / norm for x in values])
    return {
        "vectors": vectors,
        "snapshot": Path(model.model._model_dir).resolve().name,
        "metrics": {
            "load_seconds": loaded - started,
            "inference_seconds": time.perf_counter() - loaded,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
        },
    }


def main() -> int:
    stop = threading.Event()
    threading.Thread(
        target=watch_parent, args=(os.getppid(), stop), daemon=True
    ).start()
    try:
        result = compute(json.load(sys.stdin))
        Path(sys.argv[1]).write_text(
            json.dumps(result, allow_nan=False), encoding="utf-8"
        )
        return 0
    except Exception:  # noqa: BLE001 -- process boundary returns a fixed failure code
        return 2
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
