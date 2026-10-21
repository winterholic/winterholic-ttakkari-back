"""Supertonic 3 워커. 요청 1건당 1프로세스. 앱 설정·DB·토큰·네트워크를 쓰지 않는다.

Usage: python -m ttakkari.tts.worker_supertonic OUTPUT_JSON < REQUEST_JSON
감독기(model_process.run_json_worker)는 성공·타임아웃·취소·오류 모두에서 종료를 기다려야 한다.

런타임 venv(Python 3.11, numpy, onnxruntime)에서 실행되므로 3.11 문법만 쓴다.
추론 구현은 Supertone 의 공개 예제(helper.py, MIT License, https://github.com/supertone-inc/supertonic)를
필요한 부분만 옮겼다. 가중치는 OpenRAIL-M 이다. 옮긴 이유: 예제 helper 는 런타임 디렉터리에 놓인 파일이라
위치·버전에 기대지 않으려는 것과, 스레드 수·난수 시드를 우리가 정하려는 것이다.

결과 JSON 에는 원문을 싣지 않는다. 오디오는 요청이 준 out_dir 에 청크별 WAV 로 쓴다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import sys
import threading
import time
import unicodedata
import wave
from pathlib import Path

from ttakkari.model_process import watch_parent

TAIL_SILENCE_SECONDS = 0.2
_SPECIALS = {
    "–": "-", "‑": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "´": "'", "`": "'",
    "[": " ", "]": " ", "|": " ", "/": " ", "#": " ", "→": " ", "←": " ", "_": " ",
}  # fmt: skip
_END_PUNCT = re.compile(r"[.!?;:,'\")\]}…。」』】〉》›»]$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _chunk_seed(text: str, voice: str, speed: float) -> int:
    digest = hashlib.blake2b(f"{voice}\0{speed:.3f}\0{text}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % (2**32)


class _Engine:
    def __init__(self, model_dir: Path, *, threads: int):
        import numpy as np
        import onnxruntime as ort

        self.np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        onnx = model_dir / "onnx"
        self.cfg = json.loads((onnx / "tts.json").read_text(encoding="utf-8"))
        self.dp = ort.InferenceSession(str(onnx / "duration_predictor.onnx"), opts, providers=providers)
        self.enc = ort.InferenceSession(str(onnx / "text_encoder.onnx"), opts, providers=providers)
        self.est = ort.InferenceSession(str(onnx / "vector_estimator.onnx"), opts, providers=providers)
        self.voc = ort.InferenceSession(str(onnx / "vocoder.onnx"), opts, providers=providers)
        self.indexer = json.loads((onnx / "unicode_indexer.json").read_text(encoding="utf-8"))
        self.sample_rate = int(self.cfg["ae"]["sample_rate"])
        self.base_chunk = int(self.cfg["ae"]["base_chunk_size"])
        self.compress = int(self.cfg["ttl"]["chunk_compress_factor"])
        self.latent_dim = int(self.cfg["ttl"]["latent_dim"])
        self.ort_version = ort.__version__

    def style(self, path: Path):
        np = self.np
        data = json.loads(path.read_text(encoding="utf-8"))
        ttl = np.array(data["style_ttl"]["data"], dtype=np.float32).reshape(1, *data["style_ttl"]["dims"][1:])
        dp = np.array(data["style_dp"]["data"], dtype=np.float32).reshape(1, *data["style_dp"]["dims"][1:])
        return ttl, dp

    def _prepare(self, text: str, lang: str) -> str:
        text = unicodedata.normalize("NFKD", _CONTROL.sub(" ", text))
        for src, dst in _SPECIALS.items():
            text = text.replace(src, dst)
        # 어휘표에 없는 글자(-1)는 임베딩 범위를 벗어나므로 미리 버린다.
        limit = len(self.indexer)
        text = "".join(ch for ch in text if (ord(ch) < limit and self.indexer[ord(ch)] != -1) or ch.isspace())
        text = re.sub(r"\s+", " ", text).strip()
        if not _END_PUNCT.search(text):
            text += "."
        return f"<{lang}>{text}</{lang}>"

    def synthesize(self, text: str, lang: str, style, *, steps: int, speed: float, seed: int):
        np = self.np
        ttl, dp = style
        prepared = self._prepare(text, lang)
        ids = np.array([[self.indexer[ord(ch)] for ch in prepared]], dtype=np.int64)
        mask = np.ones((1, 1, ids.shape[1]), dtype=np.float32)
        duration, *_ = self.dp.run(None, {"text_ids": ids, "style_dp": dp, "text_mask": mask})
        duration = duration / speed
        emb, *_ = self.enc.run(None, {"text_ids": ids, "style_ttl": ttl, "text_mask": mask})
        wav_len = int(duration.max() * self.sample_rate)
        chunk = self.base_chunk * self.compress
        latent_len = (wav_len + chunk - 1) // chunk
        latent_mask = np.ones((1, 1, latent_len), dtype=np.float32)
        rng = np.random.default_rng(seed)
        xt = rng.standard_normal((1, self.latent_dim * self.compress, latent_len)).astype(np.float32) * latent_mask
        total = np.array([steps], dtype=np.float32)
        for step in range(steps):
            xt, *_ = self.est.run(
                None,
                {
                    "noisy_latent": xt, "text_emb": emb, "style_ttl": ttl, "text_mask": mask,
                    "latent_mask": latent_mask, "current_step": np.array([step], dtype=np.float32),
                    "total_step": total,
                },
            )  # fmt: skip
        wav, *_ = self.voc.run(None, {"latent": xt})
        seconds = float(duration[0])
        return wav[0, : int(self.sample_rate * seconds)], seconds


def _write_wav(path: Path, samples, sample_rate: int, np) -> int:
    pcm = np.clip(samples, -1.0, 1.0)
    silence = np.zeros(int(sample_rate * TAIL_SILENCE_SECONDS), dtype=np.float32)
    pcm = (np.concatenate([pcm, silence]) * 32767.0).astype("<i2")
    tmp = path.with_suffix(".part")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    os.replace(tmp, path)
    return len(pcm)


def synthesize(request: dict) -> dict:
    started_epoch = time.time()
    started = time.perf_counter()
    model_dir = Path(request["model_dir"])
    out_dir = Path(request["out_dir"])
    voice = str(request["voice"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", voice):
        raise ValueError("invalid_voice")
    style_path = model_dir / "voice_styles" / f"{voice}.json"
    if not style_path.is_file() or not (model_dir / "onnx" / "vocoder.onnx").is_file():
        raise ValueError("model_missing")
    out_dir.mkdir(parents=True, exist_ok=True)
    speed, steps, lang = float(request["speed"]), int(request["steps"]), str(request["lang"])
    engine = _Engine(model_dir, threads=int(request.get("cpu_threads", 4)))
    style = engine.style(style_path)
    loaded = time.perf_counter()
    chunks, synth_total, audio_total = [], 0.0, 0.0
    first_chunk_epoch = None
    per_chunk_synth = []
    for index, text in enumerate(request["chunks"]):
        t0 = time.perf_counter()
        samples, _seconds = engine.synthesize(
            text, lang, style, steps=steps, speed=speed, seed=_chunk_seed(text, voice, speed)
        )
        path = out_dir / f"chunk-{index:04d}.wav"
        frames = _write_wav(path, samples, engine.sample_rate, engine.np)
        took = time.perf_counter() - t0
        if first_chunk_epoch is None:
            first_chunk_epoch = time.time()
        duration = frames / engine.sample_rate
        synth_total += took
        audio_total += duration
        per_chunk_synth.append(round(took, 4))
        chunks.append(
            {
                "index": index, "file": path.name, "duration_seconds": duration,
                "sample_rate": engine.sample_rate, "bytes": path.stat().st_size,
            }
        )  # fmt: skip
    return {
        "chunks": chunks,
        "metrics": {
            "worker_started_epoch": started_epoch,
            "first_chunk_epoch": first_chunk_epoch,
            "finished_epoch": time.time(),
            "load_seconds": loaded - started,
            "synth_seconds": synth_total,
            "audio_seconds": audio_total,
            "chunk_synth_seconds": per_chunk_synth,
            "rtf": synth_total / audio_total if audio_total else None,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "cpu_threads": int(request.get("cpu_threads", 4)),
            "onnxruntime": engine.ort_version,
        },
    }


def main() -> int:
    stop = threading.Event()
    guard = threading.Thread(target=watch_parent, args=(os.getppid(), stop), daemon=True)
    guard.start()
    try:
        request = json.load(sys.stdin)
        result = synthesize(request)
        Path(sys.argv[1]).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001 -- process boundary: nonzero exit is consumed by supervisor
        # 원문이 섞일 수 있는 예외 메시지는 내보내지 않는다. 종류만 남긴다.
        print(f"tts_worker_failed:{type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
