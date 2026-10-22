"""Comfy 실행 receipt 매핑.

G0 main(`creative/director.py`)의 `AttemptReceipt`/`WorkflowRevision`/출력 CAS 레코드가 이미 가진 이름은 그대로 따른다
(`external_prompt_id`, `resolved_graph_sha256`, `seed`, `model_hashes`, `runtime`, 출력의 `name/sha256/bytes/media_type/path`).
G0 파일은 가져오거나 수정하지 않는다. G0에 없는 필드(profile/workflow/prompt/lease/시간/RSS)는 Comfy 쪽 확장이다.
입력은 Runner 의 job.json 과 managed controller 의 job 기록이며, 값을 꾸며 내지 않고 못 구한 값은 null 과 사유로 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import struct
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
HASH_LIMIT_BYTES = 256 * 1024 * 1024  # 이보다 큰 모델 파일은 sha256 대신 size+mtime 으로 식별한다.
_MOOV_LIMIT = 64 * 1024 * 1024


class ReceiptError(ValueError):
    pass


def canonical_sha256(value: object) -> str:
    """G0 director._hash 와 같은 정규화(sort_keys, 공백 없음, ensure_ascii=False) 뒤 sha256."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


# ---- 모델·런타임 식별

def model_entry(name: str, models_root: Path, *, hash_limit: int = HASH_LIMIT_BYTES) -> dict[str, Any]:
    """`models/<하위 디렉터리>/<name>` 에서 파일을 찾아 식별한다. 큰 파일은 size+mtime 으로 식별한다."""
    matches = sorted(p for p in models_root.glob(f"*/{name}") if p.is_file() and not p.is_symlink()) \
        if re.fullmatch(r"[\w.\-]+", name) else []
    if not matches:
        return {"name": name, "found": False}
    path = matches[0]
    stat = path.stat()
    entry: dict[str, Any] = {"name": name, "found": True, "path": str(path), "size": stat.st_size,
                             "mtime_ns": stat.st_mtime_ns}
    if len(matches) > 1:
        entry["alternate_paths"] = [str(p) for p in matches[1:]]
    if stat.st_size <= hash_limit:
        entry.update(sha256=sha256_file(path), hash_method="sha256")
    else:
        entry.update(sha256=None, hash_method="size+mtime")
    return entry


def _read_assignment(path: Path, key: str) -> str | None:
    try:
        match = re.search(rf"^{key}\s*=\s*['\"]([^'\"]+)['\"]", path.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        return None
    return match.group(1) if match else None


def _git_head(repo: Path) -> str | None:
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head if re.fullmatch(r"[0-9a-f]{40}", head) else None
        ref = head[5:]
        ref_file = git / ref
        if ref_file.is_file():
            value = ref_file.read_text(encoding="utf-8").strip()
        else:
            packed = (git / "packed-refs").read_text(encoding="utf-8").splitlines()
            value = next((line.split()[0] for line in packed if line.endswith(" " + ref)), "")
    except OSError:
        return None
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def comfy_runtime(comfy_root: Path) -> dict[str, Any]:
    """프로세스를 띄우지 않고 파일에서만 읽는다. 못 읽은 값은 None."""
    torch = sorted((comfy_root / "venv").glob("lib/python3*/site-packages/torch-*.dist-info"))
    torch_version = None
    if len(torch) == 1:
        torch_version = torch[0].name.removeprefix("torch-").removesuffix(".dist-info")
    return {
        "engine": "comfyui",
        "comfyui_version": _read_assignment(comfy_root / "ComfyUI" / "comfyui_version.py", "__version__"),
        "comfyui_commit": _git_head(comfy_root / "ComfyUI"),
        "torch_version": torch_version,
    }


# ---- 출력 메타데이터

def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind.decode("latin-1"), pos + header, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, kind: str):
    return next(((s, e) for k, s, e in _boxes(data, start, end) if k == kind), None)


def probe_mp4(path: Path) -> dict[str, Any] | None:
    """MP4 moov 박스에서 길이·해상도·프레임율을 읽는다. 읽을 수 없으면 None(추정하지 않는다)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            offset = 0
            moov = None
            while offset + 8 <= size:
                stream.seek(offset)
                header = stream.read(16)
                box_size, kind = struct.unpack(">I4s", header[:8])
                if box_size == 1:
                    box_size = struct.unpack(">Q", header[8:16])[0]
                elif box_size == 0:
                    box_size = size - offset
                if box_size < 8:
                    return None
                if kind == b"moov":
                    if box_size > _MOOV_LIMIT:
                        return None
                    stream.seek(offset)
                    moov = stream.read(box_size)
                    break
                offset += box_size
        if moov is None:
            return None
        body = _find(moov, 0, len(moov), "moov")
        if body is None:
            return None
        duration = None
        mvhd = _find(moov, *body, "mvhd")
        if mvhd:
            base = mvhd[0]
            if moov[base] == 1:
                timescale, ticks = struct.unpack(">IQ", moov[base + 20:base + 32])
            else:
                timescale, ticks = struct.unpack(">II", moov[base + 12:base + 20])
            duration = ticks / timescale if timescale else None
        for kind, start, end in _boxes(moov, *body):
            if kind != "trak":
                continue
            mdia = _find(moov, start, end, "mdia")
            hdlr = _find(moov, *mdia, "hdlr") if mdia else None
            if not hdlr or moov[hdlr[0] + 8:hdlr[0] + 12] != b"vide":
                continue
            tkhd = _find(moov, start, end, "tkhd")
            width = height = None
            if tkhd:
                base = tkhd[0]
                tail = base + (88 if moov[base] == 1 else 76)
                width, height = (v >> 16 for v in struct.unpack(">II", moov[tail:tail + 8]))
            fps = None
            mdhd = _find(moov, *mdia, "mdhd")
            minf = _find(moov, *mdia, "minf")
            stbl = _find(moov, *minf, "stbl") if minf else None
            stsz = _find(moov, *stbl, "stsz") if stbl else None
            if mdhd and stsz:
                base = mdhd[0]
                if moov[base] == 1:
                    scale, ticks = struct.unpack(">IQ", moov[base + 20:base + 32])
                else:
                    scale, ticks = struct.unpack(">II", moov[base + 12:base + 20])
                count = struct.unpack(">I", moov[stsz[0] + 8:stsz[0] + 12])[0]
                if scale and ticks:
                    fps = round(count / (ticks / scale), 3)
            return {"duration": None if duration is None else round(duration, 3),
                    "width": width, "height": height, "fps": fps, "metadata_source": "mp4-container"}
    except (OSError, struct.error, IndexError, ZeroDivisionError):
        return None
    return None


def _graph_declared(graph: dict) -> dict[str, Any]:
    """파일을 해석할 수 없을 때, 실행한 resolved graph 가 선언한 값만 쓴다(실제 파일 측정이 아님)."""
    width = height = length = fps = None
    for node in graph.values():
        inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
        if isinstance(inputs.get("width"), int) and isinstance(inputs.get("height"), int):
            width, height = inputs["width"], inputs["height"]
            length = inputs.get("length") if isinstance(inputs.get("length"), int) else length
        if isinstance(inputs.get("fps"), (int, float)) and not isinstance(inputs.get("fps"), bool):
            fps = float(inputs["fps"])
    duration = round(length / fps, 3) if length and fps else None
    return {"duration": duration, "width": width, "height": height, "fps": fps,
            "metadata_source": "graph-declared"}


def _output_entry(file: dict, root: Path, graph: dict) -> dict[str, Any]:
    path = Path(str(file.get("path", "")))
    if not path.is_file() or path.is_symlink() or not _under(path, root):
        raise ReceiptError(f"output {file.get('filename')!r} is not a regular file under the managed job directory")
    sha = sha256_file(path)
    meta = (probe_mp4(path) if path.suffix.lower() in {".mp4", ".m4v", ".mov"} else None) or _graph_declared(graph)
    return {
        "name": file.get("filename"), "sha256": sha, "bytes": path.stat().st_size,
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "path": f"artifacts/{sha}",  # G0 CAS 상대 경로 규칙. 실제 파일 위치는 source_path.
        "source_path": str(path), "node": file.get("node"), "intermediate": bool(file.get("intermediate")),
        **meta,
    }


# ---- receipt

def build_receipt(*, job: dict[str, Any], adapter_job: dict[str, Any], profile: Any, comfy_root: Path,
                  jobs_root: Path, rss_at_finish_gb: float | None = None,
                  hash_limit: int = HASH_LIMIT_BYTES) -> dict[str, Any]:
    """성공한 managed 실행 하나의 receipt. 근거 파일이 없으면 ReceiptError 로 거부한다."""
    if adapter_job.get("status") != "succeeded":
        raise ReceiptError("receipt requires a succeeded adapter job")
    attempts = adapter_job.get("attempts") or []
    attempt = attempts[-1] if attempts else {}
    job_dir = Path(str(adapter_job.get("dir", "")))
    if not job_dir.is_dir() or not _under(job_dir, jobs_root):
        raise ReceiptError("adapter job directory is missing or outside the managed jobs root")
    graph_path = job_dir / str(attempt.get("resolved_graph", ""))
    if not graph_path.is_file() or not _under(graph_path, job_dir):
        raise ReceiptError("resolved graph of the executed attempt is missing")
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph_sha = canonical_sha256(graph)
    prompt = job["prompt"]
    outputs = [_output_entry(f, job_dir, graph) for f in adapter_job.get("files") or []]
    if not outputs:
        raise ReceiptError("succeeded job has no collected output files")
    models_root = comfy_root / "ComfyUI" / "models"
    models = [model_entry(name, models_root, hash_limit=hash_limit) for name in profile.models]
    seeds = {value for per_node in (attempt.get("seed") or {}).values() for value in per_node.values()}
    peak = adapter_job.get("peak_rss_gb")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "comfy-execution-receipt",
        # Comfy 확장
        "profile_id": profile.profile_id,
        "workflow_id": profile.workflow,
        # registry 내용 해시. G0 의 workflow_revision(int 순번)과 이름이 겹치지 않게 일부러 다른 키를 쓴다.
        # workflow_revision 키는 G0 통합 때 G0 가 채우므로 여기서는 넣지 않는다.
        "workflow_revision_hash": profile.revision,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "models": models,
        "timing": {
            "observed_seconds": adapter_job.get("seconds"),  # submit→terminal, 대기열 시간 포함
            "queue_seconds": None,  # adapter 가 대기 구간을 따로 기록하지 않는다(WS/히스토리 타임스탬프 미보존).
            "node_seconds": adapter_job.get("node_seconds") or {},
            "started_at": adapter_job.get("started_at"), "finished_at": adapter_job.get("finished_at"),
        },
        "peak_rss_gb": peak if peak else None,  # progress 이벤트(WS)가 없으면 adapter 가 측정하지 못한다.
        "rss_at_finish_gb": rss_at_finish_gb,
        "lease": {
            "owner_task": job["task_id"], "owner_attempt": job["owner_attempt"],
            "runtime_instance": job["runtime_instance"], "lease_id": job["lease_id"],
        },
        "adapter_job_id": adapter_job.get("id"),
        "resolved_graph_hash": adapter_job.get("resolved_hash"),
        "graph_hash_consistent": graph_sha[:12] == adapter_job.get("resolved_hash"),
        "seed_consistent": seeds == {job["seed"]},
        # G0 AttemptReceipt / WorkflowRevision 과 같은 이름·의미
        "external_prompt_id": attempt.get("external_prompt_id") or adapter_job.get("prompt_id"),
        "resolved_graph_sha256": graph_sha,
        "seed": job["seed"],
        "model_hashes": {m["name"]: m["sha256"] for m in models if m.get("sha256")},
        "runtime": comfy_runtime(comfy_root),
        "outputs": outputs,
    }


def safe_rss_gb(probe: Any) -> float | None:
    try:
        value = probe() if callable(probe) else None
    except (OSError, ValueError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


__all__ = ["HASH_LIMIT_BYTES", "ReceiptError", "build_receipt", "canonical_sha256", "comfy_runtime", "model_entry",
           "probe_mp4", "safe_rss_gb", "sha256_file"]
