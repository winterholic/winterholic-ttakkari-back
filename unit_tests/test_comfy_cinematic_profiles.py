"""감성 시네마틱 영상용 관리형 프로필(키프레임·이미지→영상·텍스트→영상 장시간·음악)의 입력 계약과 ACE-Step 실행 경로."""
from __future__ import annotations

import json
import struct
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from ttakkari import managed_comfy
from ttakkari.host_resource import ResourceLeaseManager
from ttakkari.managed_comfy import PROFILES, ComfyManagedController
from ttakkari.managed_comfy_receipt import probe_wav

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Server:
    owned = True
    started = False
    stopped = False

    def __init__(self):
        self.proc = type("Proc", (), {"pid": 4242, "returncode": 0, "poll": lambda _s: 0 if self.stopped else None})()
        self.client = type("Client", (), {
            "alive": lambda _s: self.started and not self.stopped,
            "queue": lambda _s: {"queue_running": [], "queue_pending": []},
        })()

    def start(self, **_kwargs):
        self.started = True
        return self

    def stop(self):
        self.stopped = True
        return {"stopped": True, "pid": 4242}


class _Runner:
    runs: ClassVar[list] = []

    def __init__(self, data_dir):
        self.jobs_dir = data_dir / "managed-comfy" / "jobs"

    def check(self, _workflow, version):
        self.checked = version
        return {"ok": True}

    def run(self, **kwargs):
        type(self).runs.append(kwargs)
        return {"id": "adapter-1", "tag": kwargs["tag"], "status": "succeeded", "attempts": [], "files": [],
                "failure": None}


@pytest.fixture
def controller(tmp_path):
    _Runner.runs = []

    def factory(_profile, _instance, data_dir, _lease, _controller):
        return _Server(), _Runner(data_dir)

    return ComfyManagedController(data_dir=tmp_path, runtime_factory=factory, spawn=lambda fn, *a: fn(*a))


def _lease_free(service):
    return ResourceLeaseManager(data_dir=service.data_dir).current() is None


# ---- 프로필 표

def test_profile_table_lists_the_four_cinematic_profiles_with_pinned_revisions():
    assert {"flux2-klein-t2i-16x9", "wan22-5b-i2v-16x9", "wan22-5b-t2v-long-16x9", "acestep15-music"} <= set(PROFILES)
    assert PROFILES["flux2-klein-t2i-16x9"].revision == "4a0d0ecfab25"
    assert PROFILES["wan22-5b-i2v-16x9"].revision == "6c9413e014d5"
    assert PROFILES["wan22-5b-t2v-long-16x9"].revision == PROFILES["wan22-5b-draft"].revision
    assert PROFILES["wan22-5b-draft"].parametric is False
    for profile in PROFILES.values():
        assert all(w * 9 == h * 16 or (w, h) in {(832, 480), (640, 352)} for w, h in profile.sizes)
        assert all(w % 16 == 0 and h % 16 == 0 for w, h in profile.sizes)
    for pid in ("wan22-5b-i2v-16x9", "wan22-5b-t2v-long-16x9"):
        assert all(w % 32 == 0 and h % 32 == 0 for w, h in PROFILES[pid].sizes)  # Wan 5B VAE 패치 단위
        assert PROFILES[pid].frames and all((n - 1) % 4 == 0 for n in PROFILES[pid].frames)


def test_profiles_endpoint_reports_limits_and_defaults(controller):
    by_id = {p["id"]: p for p in controller.profiles()}
    i2v = by_id["wan22-5b-i2v-16x9"]
    assert i2v["capabilities"] == ["gen.video_i2v"] and i2v["kind"] == "video" and i2v["engine"] == "comfy"
    assert i2v["limits"]["max_seconds"] == pytest.approx(3.375) and i2v["limits"]["needs_image"] is True
    assert i2v["limits"]["default_size"] == [832, 480] and i2v["limits"]["fps"] == 24
    assert by_id["wan22-5b-t2v-long-16x9"]["limits"]["max_seconds"] == pytest.approx(5.042, abs=1e-3)
    assert by_id["flux2-klein-t2i-16x9"]["limits"]["default_size"] == [1280, 720]
    music = by_id["acestep15-music"]
    assert music["engine"] == "acestep" and music["capabilities"] == ["audio.music_generate"]
    assert (music["limits"]["min_seconds"], music["limits"]["max_seconds"]) == (10.0, 120.0)
    assert by_id["wan22-5b-draft"]["limits"] == {"measured": None}


# ---- 길이·해상도 해석

@pytest.mark.parametrize(("seconds", "length"), [(None, 73), (3.0, 73), (3.375, 81), (1.05, 25), (2.0, 49)])
def test_i2v_duration_maps_to_a_valid_wan_frame_count(seconds, length):
    params, spec = PROFILES["wan22-5b-i2v-16x9"].resolve(None, None, seconds)
    assert params == {"width": 832, "height": 480, "length": length}
    assert spec["fps"] == 24 and spec["length"] == length and (length - 1) % 4 == 0
    assert spec["duration_seconds"] == pytest.approx(length / 24, abs=1e-3)


def test_t2v_long_defaults_to_five_seconds_and_rejects_out_of_range():
    profile = PROFILES["wan22-5b-t2v-long-16x9"]
    assert profile.resolve(None, None, None)[0]["length"] == 121
    assert profile.resolve(1024, 576, 4.0)[0] == {"width": 1024, "height": 576, "length": 97}
    for bad in (0.5, 5.1, 60):
        with pytest.raises(ValueError, match="duration_seconds must be"):
            profile.resolve(None, None, bad)


def test_size_must_be_one_of_the_listed_pairs_and_given_together():
    profile = PROFILES["wan22-5b-i2v-16x9"]
    with pytest.raises(ValueError, match="not allowed"):
        profile.resolve(1920, 1080, None)
    with pytest.raises(ValueError, match="together"):
        profile.resolve(832, None, None)
    assert PROFILES["flux2-klein-t2i-16x9"].resolve(1024, 576, None)[0] == {"width": 1024, "height": 576}
    with pytest.raises(ValueError, match="does not take duration_seconds"):
        PROFILES["flux2-klein-t2i-16x9"].resolve(None, None, 3)


def test_music_duration_range_and_no_size():
    profile = PROFILES["acestep15-music"]
    assert profile.resolve(None, None, None) == ({}, {"duration_seconds": 30.0})
    assert profile.resolve(None, None, 40)[1] == {"duration_seconds": 40.0}
    with pytest.raises(ValueError, match="duration_seconds must be"):
        profile.resolve(None, None, 200)
    with pytest.raises(ValueError, match="does not take width/height"):
        profile.resolve(1280, 720, 30)


def test_draft_profile_keeps_fixed_size_and_length():
    with pytest.raises(ValueError, match="fixed size and length"):
        PROFILES["wan22-5b-draft"].resolve(832, 480, None)
    assert PROFILES["wan22-5b-draft"].resolve(None, None, None) == ({}, {})


# ---- prepare 검증과 실행

def test_prepare_rejects_bad_options_before_taking_the_lease(controller):
    with pytest.raises(ValueError, match="not allowed"):
        controller.prepare("wan22-5b-t2v-long-16x9", prompt="rain", seed=1, width=999, height=555)
    with pytest.raises(ValueError, match="must be an integer"):
        controller.prepare("wan22-5b-t2v-long-16x9", prompt="rain", seed=1, width="832", height=480)
    with pytest.raises(ValueError, match="must be a number"):
        controller.prepare("wan22-5b-t2v-long-16x9", prompt="rain", seed=1, duration_seconds=float("nan"))
    with pytest.raises(ValueError, match="does not take a start image"):
        controller.prepare("wan22-5b-t2v-long-16x9", prompt="rain", seed=1, image_path="/tmp/x.png")
    with pytest.raises(ValueError, match="exactly one of image_path or image_task_id"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="rain", seed=1)
    with pytest.raises(ValueError, match="negative_prompt is not accepted"):
        controller.prepare("acestep15-music", prompt="calm", seed=1, negative_prompt="vocals")
    with pytest.raises(ValueError, match="parameters"):
        controller.prepare("wan22-5b-t2v-long-16x9", prompt="rain", seed=1, workflow="x")
    assert _lease_free(controller)


def test_start_image_must_be_a_real_image_file(controller, tmp_path):
    text = tmp_path / "x.png"
    text.write_text("not an image")
    with pytest.raises(ValueError, match="PNG, JPEG or WebP"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="rain", seed=1, image_path=text)
    link = tmp_path / "link.png"
    link.symlink_to(text)
    with pytest.raises(ValueError, match="absolute regular file"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="rain", seed=1, image_path=link)
    with pytest.raises(ValueError, match="absolute regular file"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="rain", seed=1, image_path="relative.png")
    assert _lease_free(controller)


def _image_task(controller, tmp_path, *, owner="owner", status="succeeded"):
    jobs = tmp_path / "managed-comfy" / "jobs" / "adapter-img"
    jobs.mkdir(parents=True, exist_ok=True)
    image = jobs / "key.png"
    image.write_bytes(PNG)
    sha = managed_comfy._sha256_file(image)
    task_id = "a" * 32
    controller._save({"task_id": task_id, "owner": owner, "status": status,
                      "receipt": {"outputs": [{"sha256": sha, "source_path": str(image), "intermediate": False,
                                               "media_type": "image/png"}]}})
    return task_id, image


def test_image_task_id_resolves_only_same_owner_succeeded_image_with_matching_hash(controller, tmp_path):
    task_id, image = _image_task(controller, tmp_path)
    job = controller.prepare("wan22-5b-i2v-16x9", prompt="slow push in", seed=3, image_task_id=task_id)
    assert job["image"]["task_id"] == task_id and job["image"]["path"] == str(image)
    assert job["spec"] == {"width": 832, "height": 480, "fps": 24, "length": 73, "duration_seconds": 3.042}
    assert job["graph_params"] == {"width": 832, "height": 480, "length": 73}
    controller.cancel(job["task_id"], owner_task=job["task_id"])  # 잡은 lease 정리용
    _cleanup(controller, job)

    with pytest.raises(ValueError, match="same owner"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="x", seed=3, image_task_id=task_id, owner="someone-else")
    image.write_bytes(PNG + b"tampered")
    with pytest.raises(ValueError, match="receipt hash"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="x", seed=3, image_task_id=task_id)
    with pytest.raises(ValueError, match="does not name"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="x", seed=3, image_task_id="b" * 32)


def _cleanup(controller, job):
    """prepare 가 잡은 lease 를 정리한다(실행하지 않은 task)."""
    with controller._lock:
        active = controller._active.pop(job["task_id"], None)
    if active:
        active["heartbeat_stop"].set()
        active["lease"].release(proof={"runtime_status": "stopped", "evidence": {
            "runtime_instance": job["runtime_instance"], "runtime_spawned": False}})


def test_image_task_must_not_be_another_kind_of_output(controller, tmp_path):
    task_id, _image = _image_task(controller, tmp_path)
    saved = controller._load(task_id)
    saved["receipt"]["outputs"][0]["media_type"] = "video/mp4"
    controller._save(saved)
    with pytest.raises(ValueError, match="exactly one image output"):
        controller.prepare("wan22-5b-i2v-16x9", prompt="x", seed=1, image_task_id=task_id)


def test_idempotency_key_covers_size_duration_and_image(controller, tmp_path):
    kwargs = {"prompt": "rain street", "seed": 5, "idempotency_key": "stable-key-0001", "owner": "o"}
    first = controller.prepare("wan22-5b-t2v-long-16x9", width=832, height=480, duration_seconds=4.0, **kwargs)
    again = controller.prepare("wan22-5b-t2v-long-16x9", width=832, height=480, duration_seconds=4.0, **kwargs)
    assert again["task_id"] == first["task_id"]
    with pytest.raises(ValueError, match="different task arguments"):
        controller.prepare("wan22-5b-t2v-long-16x9", width=1024, height=576, duration_seconds=4.0, **kwargs)
    with pytest.raises(ValueError, match="different task arguments"):
        controller.prepare("wan22-5b-t2v-long-16x9", width=832, height=480, duration_seconds=5.0, **kwargs)
    _cleanup(controller, first)


def test_execute_passes_size_length_and_image_to_the_pinned_workflow(controller, tmp_path):
    task_id, image = _image_task(controller, tmp_path)
    job = controller.prepare("wan22-5b-i2v-16x9", prompt="slow push in", seed=3, image_task_id=task_id,
                             width=1024, height=576, duration_seconds=3.375)
    controller.execute(job["task_id"])

    run = _Runner.runs[-1]
    assert run["name"] == "i2v-wan22-5b" and run["version"] == "6c9413e014d5" and run["preset"] == "standard"
    assert run["params"] == {"prompt": "slow push in", "seed": 3, "width": 1024, "height": 576, "length": 81,
                             "image": str(image)}
    assert _lease_free(controller)


def test_execute_refuses_to_start_the_runtime_when_the_start_image_changed(controller, tmp_path):
    task_id, image = _image_task(controller, tmp_path)
    job = controller.prepare("wan22-5b-i2v-16x9", prompt="x", seed=3, image_task_id=task_id)
    image.write_bytes(PNG + b"changed after prepare")
    result = controller.execute(job["task_id"])
    assert result["status"] == "failed" and "start image changed" in result["error"]
    assert _Runner.runs == [] and _lease_free(controller)


def test_t2i_profile_passes_only_size_and_seed(controller):
    job = controller.prepare("flux2-klein-t2i-16x9", prompt="rainy neon alley", seed=11)
    controller.execute(job["task_id"])
    assert _Runner.runs[-1]["params"] == {"prompt": "rainy neon alley", "seed": 11, "width": 1280, "height": 720}
    assert _Runner.runs[-1]["name"] == "t2i-flux2-klein"


# ---- ACE-Step

_STUB = textwrap.dedent(
    """
    import json, math, struct, sys, time, wave
    from pathlib import Path
    job = json.load(open(sys.argv[1]))
    mode = job["caption"]
    if mode == "FAIL":
        sys.exit(3)
    if mode == "HANG":
        time.sleep(120)
    out = Path(job["out"]); out.mkdir(parents=True, exist_ok=True)
    seconds = float(job["duration"]) * (0.5 if mode == "SHORT" else 1.0)
    rate = 8000
    audio = out / "music.wav"
    with wave.open(str(audio), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<hh", int(5000 * math.sin(n / 9)), 0) for n in range(int(rate * seconds))))
    (out / "metrics.json").write_text(json.dumps({"audio_path": str(audio), "stages": {"generate": {"seconds": 1.5}}}))
    """
)


@pytest.fixture
def acestep(tmp_path, monkeypatch):
    runner = tmp_path / "run_acestep_job.py"
    runner.write_text(_STUB)
    checkpoint = tmp_path / "model.safetensors"
    checkpoint.write_bytes(b"x" * 8)
    root = tmp_path / "ace-root"
    root.mkdir()
    monkeypatch.setattr(managed_comfy, "ACESTEP_ROOT", root)
    monkeypatch.setattr(managed_comfy, "ACESTEP_RUNNER", runner)
    monkeypatch.setattr(managed_comfy, "ACESTEP_PYTHON", Path(sys.executable))
    monkeypatch.setattr(managed_comfy, "ACESTEP_TURBO_CHECKPOINT", checkpoint)
    return ComfyManagedController(data_dir=tmp_path / "data", spawn=lambda fn, *a: fn(*a))


def test_music_profile_runs_acestep_under_the_shared_lease_and_records_a_receipt(acestep):
    job = acestep.prepare("acestep15-music", prompt="calm rain piano", seed=21, duration_seconds=20)
    lease = ResourceLeaseManager(data_dir=acestep.data_dir).current()
    assert lease["resource_family"] == "gpu" and lease["claim"]["profile"] == "acestep15-music"

    done = acestep.execute(job["task_id"])

    assert done["status"] == "succeeded" and done["error"] is None
    output = done["receipt"]["outputs"][0]
    assert output["media_type"] == "audio/wav" and output["sample_rate"] == 8000 and output["channels"] == 2
    assert output["duration"] == pytest.approx(20.0, abs=0.01)
    assert done["receipt"]["requested_spec"] == {"duration_seconds": 20.0} and done["receipt"]["seed"] == 21
    assert done["receipt"]["timing"]["stage_seconds"] == {"generate": 1.5}
    assert done["lease_release_proof"]["evidence"]["process_exit_code"] == 0
    assert done["result"]["files"][0]["node"] == "acestep" and done["runtime_pid"]
    assert _lease_free(acestep)


def test_music_failure_releases_the_lease_after_the_process_is_gone(acestep):
    job = acestep.prepare("acestep15-music", prompt="FAIL", seed=1, duration_seconds=10)
    done = acestep.execute(job["task_id"])
    assert done["status"] == "failed" and "ACE-Step exit=3" in done["error"]
    assert _lease_free(acestep)


def test_music_shorter_than_requested_is_not_a_success(acestep):
    job = acestep.prepare("acestep15-music", prompt="SHORT", seed=1, duration_seconds=20)
    done = acestep.execute(job["task_id"])
    assert done["status"] == "failed" and "shorter than requested" in done["error"]
    assert _lease_free(acestep)


def test_music_missing_files_fail_without_spawning(acestep, monkeypatch, tmp_path):
    monkeypatch.setattr(managed_comfy, "ACESTEP_PYTHON", tmp_path / "missing-python")
    job = acestep.prepare("acestep15-music", prompt="calm", seed=1, duration_seconds=10)
    done = acestep.execute(job["task_id"])
    assert done["status"] == "failed" and "missing" in done["error"] and "runtime_pid" not in done
    assert _lease_free(acestep)


def test_music_cancel_stops_the_process_group_and_releases(acestep):
    job = acestep.prepare("acestep15-music", prompt="HANG", seed=1, duration_seconds=10)
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(acestep.execute(job["task_id"])))
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not acestep._load(job["task_id"]).get("runtime_pid"):
        time.sleep(0.1)
    acestep.cancel(job["task_id"], owner_task=job["task_id"])
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert result["status"] == "cancelled" and result["lease_release_proof"]["evidence"]["process_exit_code"] != 0
    assert _lease_free(acestep)


def test_acestep_job_runtime_absence_uses_pid_identity_only(acestep, monkeypatch):
    job = {"profile": "acestep15-music", "runtime_pid": 999_999_999, "runtime_created_at": 1.0}
    monkeypatch.setattr(acestep, "_is_comfy_port_alive", lambda: pytest.fail("Comfy 포트는 이 소비자와 무관하다"))
    assert acestep._owned_runtime_absent(job) is True


# ---- 영수증 보조

def test_probe_wav_reads_pcm_and_float_headers(tmp_path):
    pcm = tmp_path / "pcm.wav"
    data = b"\x00\x00" * 16000
    pcm.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                    + b"data" + struct.pack("<I", len(data)) + data)
    assert probe_wav(pcm) == {"duration": 1.0, "sample_rate": 16000, "channels": 1, "metadata_source": "wav-header"}
    streamed = tmp_path / "streamed.wav"  # 스트리밍 기록: data 크기 0xFFFFFFFF
    streamed.write_bytes(b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 3, 2, 48000, 384000, 8, 32)
                         + b"data" + struct.pack("<I", 0xFFFFFFFF) + b"\x00" * 384000)
    assert probe_wav(streamed)["duration"] == 1.0
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav at all")
    assert probe_wav(bad) is None


def test_public_job_json_stays_serializable(acestep):
    job = acestep.prepare("acestep15-music", prompt="calm", seed=2, duration_seconds=10)
    done = acestep.execute(job["task_id"])
    json.dumps(done)
