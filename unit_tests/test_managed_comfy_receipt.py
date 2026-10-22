"""Comfy 실행 receipt 매핑 검사. G0 director.py 는 가져오지 않고, 같은 이름·정규화 규칙만 맞췄는지 본다."""
from __future__ import annotations

import hashlib
import json
import os
import struct
from types import SimpleNamespace

import pytest

from ttakkari.managed_comfy_receipt import (
    ReceiptError,
    build_receipt,
    canonical_sha256,
    comfy_runtime,
    model_entry,
    probe_mp4,
)

PROFILE = SimpleNamespace(profile_id="wan22-5b-draft", workflow="t2v-wan22-5b", revision="a603a63d6f3a",
                          models=("unet.safetensors", "clip.safetensors", "missing.safetensors"))
GRAPH = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 20}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "A fox"}},
    "55": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"width": 832, "height": 480, "length": 48}},
    "57": {"class_type": "CreateVideo", "inputs": {"fps": 24.0}},
}


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4(*, width=832, height=480, frames=48, fps=24, duration_s=2) -> bytes:
    mvhd = _box(b"mvhd", struct.pack(">BBBBIIII", 0, 0, 0, 0, 0, 0, 1000, duration_s * 1000) + b"\0" * 80)
    tkhd = _box(b"tkhd", struct.pack(">BBBBIIIII", 0, 0, 0, 3, 0, 0, 1, 0, 0) + b"\0" * 8 + b"\0" * 8
                + b"\0" * 36 + struct.pack(">II", width << 16, height << 16))
    mdhd = _box(b"mdhd", struct.pack(">BBBBIIII", 0, 0, 0, 0, 0, 0, fps * 100, duration_s * fps * 100) + b"\0" * 4)
    hdlr = _box(b"hdlr", struct.pack(">BBBBI4s", 0, 0, 0, 0, 0, b"vide") + b"\0" * 12)
    stsz = _box(b"stsz", struct.pack(">BBBBII", 0, 0, 0, 0, 0, frames))
    mdia = _box(b"mdia", mdhd + hdlr + _box(b"minf", _box(b"stbl", stsz)))
    moov = _box(b"moov", mvhd + _box(b"trak", tkhd + mdia))
    return _box(b"ftyp", b"isom\0\0\0\0isom") + _box(b"mdat", b"x" * 64) + moov


@pytest.fixture
def tree(tmp_path):
    comfy = tmp_path / "comfyui"
    models = comfy / "ComfyUI" / "models"
    (models / "diffusion_models").mkdir(parents=True)
    (models / "text_encoders").mkdir(parents=True)
    (models / "diffusion_models" / "unet.safetensors").write_bytes(b"u" * 100)
    (models / "text_encoders" / "clip.safetensors").write_bytes(b"c" * 5000)
    (comfy / "ComfyUI" / "comfyui_version.py").write_text('# generated\n__version__ = "0.42.0"\n')
    git = comfy / "ComfyUI" / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/master\n")
    (git / "refs" / "heads" / "master").write_text("a" * 40 + "\n")
    (comfy / "venv" / "lib" / "python3.13" / "site-packages" / "torch-2.14.1.dist-info").mkdir(parents=True)
    (comfy / "venv" / "lib" / "python3.13" / "site-packages" / "torchaudio-2.11.0.dist-info").mkdir(parents=True)

    jobs = tmp_path / "data" / "managed-comfy" / "jobs"
    job_dir = jobs / "adapter-1"
    (job_dir / "attempts").mkdir(parents=True)
    (job_dir / "outputs" / "58").mkdir(parents=True)
    (job_dir / "attempts" / "attempt-0001.resolved.api.json").write_text(json.dumps(GRAPH))
    video = _mp4()
    (job_dir / "outputs" / "58" / "wan.mp4").write_bytes(video)
    adapter_job = {
        "id": "adapter-1", "status": "succeeded", "dir": str(job_dir), "prompt_id": "prompt-9",
        "resolved_hash": canonical_sha256(GRAPH)[:12], "seconds": 91.5, "node_seconds": {"3": 80.0},
        "peak_rss_gb": 31.2, "started_at": "2026-10-22T10:00:00", "finished_at": "2026-10-22T10:01:31",
        "attempts": [{"attempt_id": "att", "external_prompt_id": "prompt-9",
                      "resolved_graph": "attempts/attempt-0001.resolved.api.json",
                      "seed": {"3": {"seed": 7}}}],
        "files": [{"node": "58", "key": "images", "filename": "wan.mp4", "subfolder": "ttk", "type": "output",
                   "path": str(job_dir / "outputs" / "58" / "wan.mp4"), "intermediate": False}],
    }
    job = {"task_id": "a" * 32, "owner_attempt": "b" * 32, "runtime_instance": "c" * 32, "lease_id": "d" * 32,
           "prompt": "A fox", "seed": 7}
    return SimpleNamespace(comfy=comfy, jobs=jobs, job_dir=job_dir, adapter=adapter_job, job=job, video=video)


def _build(tree, **kwargs):
    return build_receipt(job=tree.job, adapter_job=tree.adapter, profile=PROFILE, comfy_root=tree.comfy,
                         jobs_root=tree.jobs, **kwargs)


def test_receipt_carries_every_required_field_with_g0_names_where_they_exist(tree):
    receipt = _build(tree, hash_limit=1000, rss_at_finish_gb=12.5)

    # G0 AttemptReceipt / WorkflowRevision 이름·의미
    assert receipt["external_prompt_id"] == "prompt-9"
    assert receipt["resolved_graph_sha256"] == hashlib.sha256(json.dumps(
        GRAPH, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert receipt["seed"] == 7
    assert receipt["model_hashes"] == {"unet.safetensors": hashlib.sha256(b"u" * 100).hexdigest()}
    assert receipt["runtime"] == {"engine": "comfyui", "comfyui_version": "0.42.0",
                                  "comfyui_commit": "a" * 40, "torch_version": "2.14.1"}
    out = receipt["outputs"][0]
    sha = hashlib.sha256(tree.video).hexdigest()
    assert (out["name"], out["sha256"], out["bytes"], out["path"], out["media_type"]) == (
        "wan.mp4", sha, len(tree.video), f"artifacts/{sha}", "video/mp4")
    assert out["source_path"].endswith("outputs/58/wan.mp4")
    assert (out["width"], out["height"], out["fps"], out["duration"]) == (832, 480, 24.0, 2.0)
    assert out["metadata_source"] == "mp4-container"
    # Comfy 확장
    assert (receipt["profile_id"], receipt["workflow_id"], receipt["workflow_revision_hash"]) == (
        "wan22-5b-draft", "t2v-wan22-5b", "a603a63d6f3a")
    # G0 의 workflow_revision 은 정수 순번이다. Comfy receipt 는 같은 이름의 키를 다른 타입으로 쓰지 않는다.
    assert "workflow_revision" not in receipt
    assert receipt["prompt_sha256"] == hashlib.sha256(b"A fox").hexdigest()
    assert receipt["resolved_graph_hash"] == receipt["resolved_graph_sha256"][:12]
    assert receipt["graph_hash_consistent"] is True and receipt["seed_consistent"] is True
    assert receipt["lease"] == {"owner_task": "a" * 32, "owner_attempt": "b" * 32,
                                "runtime_instance": "c" * 32, "lease_id": "d" * 32}
    assert "generation_token" not in json.dumps(receipt)
    assert receipt["timing"]["observed_seconds"] == 91.5 and receipt["timing"]["queue_seconds"] is None
    assert receipt["peak_rss_gb"] == 31.2 and receipt["rss_at_finish_gb"] == 12.5


def test_models_use_sha256_when_cheap_size_mtime_when_large_and_missing_files_are_reported(tree):
    receipt = _build(tree, hash_limit=1000)
    unet, clip, missing = receipt["models"]
    assert unet["hash_method"] == "sha256" and unet["sha256"] and unet["size"] == 100
    assert clip["hash_method"] == "size+mtime" and clip["sha256"] is None and clip["size"] == 5000
    assert isinstance(clip["mtime_ns"], int)
    assert missing == {"name": "missing.safetensors", "found": False}
    assert "clip.safetensors" not in receipt["model_hashes"]


def test_model_lookup_rejects_path_tricks_and_symlinks(tree, tmp_path):
    models = tree.comfy / "ComfyUI" / "models"
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"o")
    (models / "vae").mkdir()
    (models / "vae" / "link.safetensors").symlink_to(outside)
    assert model_entry("../../outside.safetensors", models)["found"] is False
    assert model_entry("link.safetensors", models)["found"] is False


def test_unknown_values_are_null_not_invented(tmp_path):
    runtime = comfy_runtime(tmp_path)
    assert runtime == {"engine": "comfyui", "comfyui_version": None, "comfyui_commit": None, "torch_version": None}


def test_packed_ref_and_detached_head_commit(tree):
    git = tree.comfy / "ComfyUI" / ".git"
    (git / "refs" / "heads" / "master").unlink()
    (git / "packed-refs").write_text("# pack\n" + "b" * 40 + " refs/heads/master\n")
    assert comfy_runtime(tree.comfy)["comfyui_commit"] == "b" * 40
    (git / "HEAD").write_text("c" * 40 + "\n")
    assert comfy_runtime(tree.comfy)["comfyui_commit"] == "c" * 40
    (git / "HEAD").write_text("garbage\n")
    assert comfy_runtime(tree.comfy)["comfyui_commit"] is None


def test_non_video_output_falls_back_to_graph_declared_metadata_and_says_so(tree):
    path = tree.job_dir / "outputs" / "58" / "clip.webp"
    path.write_bytes(b"RIFF-fake")
    tree.adapter["files"] = [{**tree.adapter["files"][0], "filename": "clip.webp", "path": str(path)}]
    out = _build(tree)["outputs"][0]
    assert out["metadata_source"] == "graph-declared"
    assert (out["width"], out["height"], out["fps"], out["duration"]) == (832, 480, 24.0, 2.0)
    assert out["media_type"] == "image/webp"


def test_truncated_or_corrupt_mp4_is_not_probed(tree):
    path = tree.job_dir / "outputs" / "58" / "wan.mp4"
    path.write_bytes(tree.video[:30])
    assert probe_mp4(path) is None
    path.write_bytes(b"not an mp4 at all")
    assert probe_mp4(path) is None
    assert probe_mp4(tree.job_dir / "nope.mp4") is None
    assert _build(tree)["outputs"][0]["metadata_source"] == "graph-declared"  # 추정이 아니라 선언값임을 표시


def test_failed_or_unfinished_job_has_no_receipt(tree):
    for status in ("failed", "needs_reconcile", "cancelled", "running"):
        tree.adapter["status"] = status
        with pytest.raises(ReceiptError, match="succeeded"):
            _build(tree)


def test_output_outside_the_managed_job_directory_is_rejected(tree, tmp_path):
    stray = tmp_path / "stray.mp4"
    stray.write_bytes(tree.video)
    tree.adapter["files"][0]["path"] = str(stray)
    with pytest.raises(ReceiptError, match="managed job directory"):
        _build(tree)


def test_symlinked_output_is_rejected(tree, tmp_path):
    target = tmp_path / "secret.mp4"
    target.write_bytes(tree.video)
    link = tree.job_dir / "outputs" / "58" / "link.mp4"
    link.symlink_to(target)
    tree.adapter["files"][0]["path"] = str(link)
    with pytest.raises(ReceiptError, match="regular file"):
        _build(tree)


def test_adapter_dir_outside_jobs_root_or_missing_graph_or_no_outputs_is_rejected(tree, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(ReceiptError, match="outside the managed jobs root"):
        build_receipt(job=tree.job, adapter_job={**tree.adapter, "dir": str(other)}, profile=PROFILE,
                      comfy_root=tree.comfy, jobs_root=tree.jobs)
    (tree.job_dir / "attempts" / "attempt-0001.resolved.api.json").unlink()
    with pytest.raises(ReceiptError, match="resolved graph"):
        _build(tree)
    (tree.job_dir / "attempts" / "attempt-0001.resolved.api.json").write_text(json.dumps(GRAPH))
    tree.adapter["files"] = []
    with pytest.raises(ReceiptError, match="no collected output"):
        _build(tree)


def test_tampered_graph_or_seed_is_flagged_not_hidden(tree):
    tampered = {**GRAPH, "3": {"class_type": "KSampler", "inputs": {"seed": 8, "steps": 20}}}
    (tree.job_dir / "attempts" / "attempt-0001.resolved.api.json").write_text(json.dumps(tampered))
    receipt = _build(tree)
    assert receipt["graph_hash_consistent"] is False
    assert receipt["resolved_graph_sha256"] == canonical_sha256(tampered)
    tree.adapter["attempts"][0]["seed"] = {"3": {"seed": 99}}
    assert _build(tree)["seed_consistent"] is False


def test_mp4_probe_handles_64bit_box_sizes_and_unreadable_files(tree, tmp_path):
    video = tmp_path / "big.mp4"
    data = tree.video
    ftyp_end = struct.unpack(">I", data[:4])[0]
    # ftyp 박스를 largesize(64-bit) 형태로 다시 쓴다.
    ftyp = data[8:ftyp_end]
    rewritten = struct.pack(">I4sQ", 1, b"ftyp", 16 + len(ftyp)) + ftyp + data[ftyp_end:]
    video.write_bytes(rewritten)
    assert probe_mp4(video)["width"] == 832
    video.chmod(0)
    try:
        if os.geteuid() != 0:
            assert probe_mp4(video) is None
    finally:
        video.chmod(0o600)
