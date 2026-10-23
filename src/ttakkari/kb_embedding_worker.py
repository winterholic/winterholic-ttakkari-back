"""Offline, bounded, full-text token-aware KB embedding. No network or model downloads."""
import json
import math
import os
import sys
import threading
from pathlib import Path

from ttakkari.model_process import watch_parent


def complete_chunks(text, tokenizer, prefix):
    if len(text) > 50000:
        raise ValueError("passage_too_long_for_semantic_projection")
    pending = [text]
    result = []
    while pending:
        part = pending.pop(0)
        if len(tokenizer.encode(prefix + part).ids) <= 500:
            result.append(part)
        else:
            middle = len(part) // 2
            if middle == 0:
                raise ValueError("unrepresentable_token_window")
            pending[0:0] = [part[:middle], part[middle:]]
        if len(result) + len(pending) > 128:
            raise ValueError("passage_window_budget_exceeded")
    return result


def compute(request):
    from fastembed import TextEmbedding
    model = TextEmbedding(request["model"], cache_dir=request["cache_dir"],
                          threads=request["threads"], local_files_only=True)
    expected = request.get("snapshot")
    if expected and expected not in Path(model.model._model_dir).resolve().parts:
        raise ValueError("kb_embedding_snapshot_mismatch")
    model.model._ensure_tokenizer()
    tokenizer = model.model.tokenizer
    tokenizer.no_truncation()
    prefix = "query: " if request["query"] else "passage: "
    chunks = [complete_chunks(text, tokenizer, prefix) for text in request["texts"]]
    flattened = [prefix + chunk for parts in chunks for chunk in parts]
    raw = iter(model.embed(flattened, batch_size=16))
    vectors = []
    for parts in chunks:
        pooled = None
        for part in parts:
            value = next(raw)
            weight = max(1, len(part))
            if pooled is None:
                pooled = value * weight
            else:
                pooled += value * weight
        norm = math.sqrt(sum(float(x) ** 2 for x in pooled)) or 1.0
        vectors.append([float(x) / norm for x in pooled])
    return {"vectors": vectors, "coverage": "full", "windows": [len(c) for c in chunks]}


def main():
    stop = threading.Event()
    threading.Thread(target=watch_parent, args=(os.getppid(), stop), daemon=True).start()
    try:
        result = compute(json.load(sys.stdin))
        Path(sys.argv[1]).write_text(json.dumps(result, allow_nan=False))
        return 0
    except Exception:  # noqa: BLE001 -- subprocess boundary returns a fixed error, never credential-bearing tracebacks
        return 2
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
