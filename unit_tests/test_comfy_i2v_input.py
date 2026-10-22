"""E1 준비: managed Comfy I2V 기준 이미지 입력·profile 계약(CPU, GPU·실제 Comfy 서버 없음).

이 파일이 검증하는 것: 입력 검증(소유 자산 id+sha256, 형식, 변조, 심볼릭 링크, 경로 이탈), lease 획득 전 거부,
업로드 실패의 영속 기록과 소유 lease 정리, T2V 호환, ASGI 인증 경계.
이 파일이 검증하지 않는 것: 실제 I2V 생성, 실제 ComfyUI의 LoadImage 동작, GPU/메모리 사용량(E1 실행 담당 소관).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from creative_support import png_bytes
from fastapi import FastAPI
from PIL import Image

from ttakkari.api import comfy_managed as api
from ttakkari.creative.comfy_inputs import load_input_image
from ttakkari.creative.director import DirectorStore
from ttakkari.host_resource import LeaseBusy, ResourceLeaseManager
from ttakkari.managed_comfy import PROFILES, ComfyManagedController
from ttakkari.managed_comfy_input import (
    InputImage,
    InputImageError,
    validate_input_image,
)
from ttakkari.managed_comfy_receipt import ReceiptError, _input_image_section
from ttakkari.security.auth import require_user

I2V = "wan22-5b-i2v-draft"
NAME = "keyframe_flux_cafe.png"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image(data: bytes | None = None, name: str = NAME) -> InputImage:
    data = data or png_bytes()
    return validate_input_image(asset_id=name, sha256=_sha(data), data=data)


# ---------- 검증기


@pytest.mark.parametrize(
    "name,fmt",
    [("a.png", "PNG"), ("b.jpg", "JPEG"), ("c.jpeg", "JPEG"), ("d.webp", "WEBP")],
)
def test_validator_accepts_png_jpeg_webp_and_seals_metadata(name, fmt):
    data = png_bytes(fmt=fmt)
    image = validate_input_image(asset_id=name, sha256=_sha(data), data=data)
    assert (image.format, image.width, image.height, image.upload_name) == (
        fmt,
        128,
        96,
        name,
    )
    assert image.public()["sha256"] == _sha(data) and "data" not in image.public()


def test_validator_rejects_tampered_empty_nonimage_truncated_and_oversized_inputs():
    good = png_bytes()
    with pytest.raises(InputImageError, match="sha256이 선언된 값과 다릅니다"):
        validate_input_image(asset_id=NAME, sha256="0" * 64, data=good)
    with pytest.raises(InputImageError, match="비어"):
        validate_input_image(asset_id=NAME, sha256=_sha(b""), data=b"")
    junk = b"this is not an image" * 50
    with pytest.raises(InputImageError, match="읽을 수 없거나"):
        validate_input_image(asset_id=NAME, sha256=_sha(junk), data=junk)
    truncated = good[: len(good) // 2]
    with pytest.raises(InputImageError, match="읽을 수 없거나"):
        validate_input_image(asset_id=NAME, sha256=_sha(truncated), data=truncated)
    big = png_bytes(5000, 64)
    with pytest.raises(InputImageError, match="해상도"):
        validate_input_image(asset_id=NAME, sha256=_sha(big), data=big)
    tiny = png_bytes(8, 8)
    with pytest.raises(InputImageError, match="해상도"):
        validate_input_image(asset_id=NAME, sha256=_sha(tiny), data=tiny)
    with pytest.raises(InputImageError, match="25MiB"):
        validate_input_image(
            asset_id=NAME, sha256=_sha(b"x"), data=b"x" * (25 * 1024 * 1024 + 1)
        )


def test_validator_rejects_animation_other_formats_and_extension_lies():
    frames = [Image.new("RGB", (128, 96), c) for c in ((255, 0, 0), (0, 255, 0))]
    animated = io.BytesIO()
    frames[0].save(
        animated, "WEBP", save_all=True, append_images=frames[1:], duration=50, loop=0
    )
    data = animated.getvalue()
    with pytest.raises(InputImageError, match="애니메이션"):
        validate_input_image(asset_id="anim.webp", sha256=_sha(data), data=data)
    gif = io.BytesIO()
    frames[0].save(gif, "GIF")
    with pytest.raises(InputImageError, match="PNG, JPEG, WebP만"):
        validate_input_image(
            asset_id="x.png", sha256=_sha(gif.getvalue()), data=gif.getvalue()
        )
    png = png_bytes()
    with pytest.raises(InputImageError, match="확장자가 실제 형식과 다릅니다"):
        validate_input_image(asset_id="liar.jpg", sha256=_sha(png), data=png)


@pytest.mark.parametrize(
    "asset_id",
    [
        "../escape.png",
        "dir/x.png",
        "/abs/x.png",
        ".hidden.png",
        "x.PNG",
        "x.png/",
        "x.png\n",
        "",
        "a" * 90 + ".png",
        "x.exe",
        "x",
    ],
)
def test_validator_rejects_path_like_or_malformed_asset_ids(asset_id):
    data = png_bytes()
    with pytest.raises(InputImageError, match="id는"):
        validate_input_image(asset_id=asset_id, sha256=_sha(data), data=data)


# ---------- 소유 자산 resolver (G0 CAS 재사용)


def _store_with_asset(tmp_path, *, name=NAME, data=None):
    store = DirectorStore(tmp_path / "productions")
    pid = store.create_production("i2v")["manifest"]["production_id"]
    data = data or png_bytes()
    record = store.put_artifact(pid, name, data, media_type="image/png")
    return store, pid, record["sha256"], data


def test_resolver_returns_the_owned_asset_only_for_a_matching_id_and_sha(tmp_path):
    store, pid, sha, data = _store_with_asset(tmp_path)
    image = load_input_image(store, pid, NAME, sha)
    assert image.data == data and image.sha256 == sha and image.asset_id == NAME
    other = png_bytes(color=(1, 2, 3))
    store.put_artifact(pid, "other.png", other, media_type="image/png")
    with pytest.raises(InputImageError, match="일치하지 않습니다"):
        load_input_image(
            store, pid, NAME, _sha(other)
        )  # id와 sha가 서로 다른 자산을 가리킴
    with pytest.raises(InputImageError, match="일치하지 않습니다"):
        load_input_image(store, pid, "missing.png", sha)
    with pytest.raises(InputImageError):
        load_input_image(store, pid, "../x.png", sha)
    with pytest.raises(FileNotFoundError):
        load_input_image(store, "00000000-0000-4000-8000-000000000000", NAME, sha)


def test_resolver_does_not_cross_productions_and_gives_the_same_answer_as_for_unknown_ids(
    tmp_path,
):
    store, pid_a, sha, _data = _store_with_asset(tmp_path)
    pid_b = store.create_production("다른 제작")["manifest"]["production_id"]
    with pytest.raises(InputImageError, match="일치하지 않습니다") as cross:
        load_input_image(store, pid_b, NAME, sha)
    with pytest.raises(InputImageError, match="일치하지 않습니다") as unknown:
        load_input_image(store, pid_b, "never-uploaded.png", "f" * 64)
    assert str(cross.value) == str(
        unknown.value
    )  # 다른 제작의 자산 존재 여부를 알려 주지 않는다
    # 같은 내용을 B에도 올리면 B가 소유하므로 통과한다.
    store.put_artifact(pid_b, NAME, png_bytes(), media_type="image/png")
    assert load_input_image(store, pid_b, NAME, sha).sha256 == sha
    assert pid_a != pid_b


def test_resolver_rejects_symlink_missing_file_tampered_cas_and_non_images(tmp_path):
    store, pid, sha, _data = _store_with_asset(tmp_path)
    cas = store.production_path(pid) / "artifacts" / sha
    decoy = tmp_path / "decoy.png"
    decoy.write_bytes(png_bytes(color=(9, 9, 9)))
    cas.unlink()
    cas.symlink_to(decoy)  # CAS 파일을 외부 파일 심볼릭 링크로 바꿔치기
    with pytest.raises(InputImageError, match="안전하게 읽을 수 없습니다"):
        load_input_image(store, pid, NAME, sha)
    cas.unlink()
    with pytest.raises(InputImageError, match="안전하게 읽을 수 없습니다"):
        load_input_image(store, pid, NAME, sha)  # 파일 없음
    cas.write_bytes(png_bytes(color=(5, 5, 5)))  # 내용 변조: 해시 불일치
    with pytest.raises(InputImageError, match="안전하게 읽을 수 없습니다"):
        load_input_image(store, pid, NAME, sha)
    junk = b"not an image at all" * 40
    store.put_artifact(pid, "junk.png", junk, media_type="image/png")
    with pytest.raises(InputImageError, match="읽을 수 없거나"):
        load_input_image(store, pid, "junk.png", _sha(junk))


# ---------- 컨트롤러: lease 이전 거부, 업로드, 실패 정리


class _Client:
    def __init__(self, server):
        self.server = server

    def alive(self):
        return self.server.started and not self.server.stopped

    def queue(self):
        return {"queue_running": [], "queue_pending": []}

    def upload_image(self, path):
        self.server.uploads.append(
            {
                "path": path,
                "bytes_sha256": _sha(Path(path).read_bytes()),
                "mode": os.stat(path).st_mode & 0o777,
            }
        )
        if self.server.upload_error:
            raise self.server.upload_error
        return self.server.upload_result or f"ttk/{Path(path).name}"


class _Server:
    def __init__(self, *, upload_error=None, upload_result=None):
        self.owned, self.started, self.stopped = True, False, False
        self.uploads: list[dict] = []
        self.upload_error, self.upload_result = upload_error, upload_result
        self.proc = type(
            "Proc",
            (),
            {
                "pid": 4141,
                "returncode": 0,
                "poll": lambda _s: 0 if self.stopped else None,
            },
        )()
        self.client = _Client(self)
        self.start_kwargs: dict = {}

    def start(self, **kwargs):
        self.started = True
        self.start_kwargs = kwargs
        return self

    def stop(self):
        self.stopped = True
        return {"stopped": True, "pid": self.proc.pid}


class _Runner:
    instances: ClassVar[list] = []

    def __init__(self, jobs_dir):
        self.jobs_dir, self.runs = jobs_dir, []
        self.__class__.instances.append(self)

    def check(self, _workflow, version):
        assert version == PROFILES[I2V].revision
        return {"ok": True}

    def run(self, **kwargs):
        self.runs.append(kwargs)
        job = {
            "id": "adapter-job-1",
            "tag": kwargs["tag"],
            "status": "succeeded",
            "files": [],
            "failure": None,
            "attempts": [{"attempt_id": "a1", "external_prompt_id": "p1"}],
        }
        (self.jobs_dir / "adapter-job-1").mkdir(parents=True)
        (self.jobs_dir / "adapter-job-1" / "job.json").write_text(json.dumps(job))
        return job

    def reconcile(self, job_id):
        return {"id": job_id, "status": "succeeded", "files": [], "failure": None}


def _controller(tmp_path, **server_kwargs):
    _Runner.instances.clear()
    servers: list[_Server] = []

    def factory(profile, runtime_instance, data_dir, lease, _controller_):
        server = _Server(**server_kwargs)
        servers.append(server)
        return server, _Runner(data_dir / "managed-comfy" / "jobs")

    service = ComfyManagedController(
        data_dir=tmp_path, runtime_factory=factory, spawn=lambda fn, *a: fn(*a)
    )
    return service, servers


def _lease(service):
    return ResourceLeaseManager(data_dir=service.data_dir).current()


def _staged_dir(service, task_id):
    return service._inputs_root() / task_id


def test_i2v_without_a_reference_image_is_rejected_before_any_lease_or_runtime(
    tmp_path,
):
    service, servers = _controller(tmp_path)
    with pytest.raises(ValueError, match="requires an owned reference image"):
        service.prepare(I2V, prompt="x", seed=2101)
    assert _lease(service) is None and servers == []
    assert not service._inputs_root().exists() or not any(
        service._inputs_root().iterdir()
    )


def test_t2v_profile_rejects_an_image_and_stays_compatible_without_one(tmp_path):
    service, servers = _controller(tmp_path)
    with pytest.raises(ValueError, match="does not accept an input image"):
        service.prepare("wan22-5b-draft", prompt="x", seed=1, image=_image())
    assert _lease(service) is None
    record = service.prepare(
        "wan22-5b-draft", prompt="fox", seed=1
    )  # 기존 호출 형태 그대로
    assert "input_image" not in record and record["status"] == "starting"
    assert not _staged_dir(service, record["task_id"]).exists()
    assert servers == []


def test_only_a_validated_input_image_object_is_accepted(tmp_path):
    service, _servers = _controller(tmp_path)
    forged = InputImage(
        asset_id=NAME,
        sha256="0" * 64,
        data=png_bytes(),
        format="PNG",
        width=128,
        height=96,
    )
    for bad in (forged, {"asset_id": NAME}, png_bytes(), "/etc/passwd", None):
        with pytest.raises(ValueError, match="reference image|requires an owned"):
            service.prepare(I2V, prompt="x", seed=1, image=bad)
    assert _lease(service) is None
    with pytest.raises(ValueError, match="parameters"):
        service.prepare(I2V, prompt="x", seed=1, image=_image(), path="/etc/passwd")
    assert _lease(service) is None


def test_profile_pins_immutable_registry_revision_and_does_not_claim_the_unmeasured_16gb(
    tmp_path,
):
    profile = PROFILES[I2V]
    assert (profile.workflow, profile.revision, profile.preset) == (
        "i2v-wan22-5b",
        "6c9413e014d5",
        "draft",
    )
    assert (
        profile.verification == "prepared-unverified" and profile.input_kind == "image"
    )
    assert profile.required_gb == 24 and profile.required_gb != 16
    assert "미측정" in profile.required_gb_basis
    assert PROFILES["wan22-5b-draft"].required_gb == 16  # T2V 기존 값은 바꾸지 않았다


def test_i2v_stages_verified_bytes_privately_uploads_once_and_passes_the_uploaded_name_to_the_runner(
    tmp_path,
):
    service, servers = _controller(tmp_path)
    image = _image()
    record = service.prepare(I2V, prompt="cafe", seed=2101, image=image)
    task_id = record["task_id"]
    staged = _staged_dir(service, task_id) / NAME
    assert (
        staged.is_file()
        and not staged.is_symlink()
        and (staged.stat().st_mode & 0o777) == 0o600
    )
    assert (_staged_dir(service, task_id).stat().st_mode & 0o777) == 0o700
    saved = service.get(task_id)
    assert (
        saved["input_image"]["sha256"] == image.sha256
        and saved["input_image"]["asset_id"] == NAME
    )

    job = service.execute(task_id)

    assert job["status"] == "succeeded"
    server = servers[0]
    assert server.start_kwargs == {
        "required_gb": 24
    }  # 기존 memory guard 경로에 profile 값이 그대로 전달된다
    assert (
        len(server.uploads) == 1 and server.uploads[0]["bytes_sha256"] == image.sha256
    )
    runner = _Runner.instances[-1]
    assert runner.runs[0]["params"] == {
        "prompt": "cafe",
        "seed": 2101,
        "image": f"ttk/{NAME}",
    }
    assert (
        runner.runs[0]["retries"] == 0 and runner.runs[0]["version"] == "6c9413e014d5"
    )
    assert server.stopped and _lease(service) is None
    assert not _staged_dir(
        service, task_id
    ).exists()  # 사본은 실행 뒤 지운다(원본은 제작 CAS)


@pytest.mark.parametrize(
    "server_kwargs,reason",
    [
        (
            {"upload_error": ConnectionResetError("connection lost")},
            "ConnectionResetError",
        ),
        ({"upload_error": TimeoutError("upload timed out")}, "TimeoutError"),
        ({"upload_result": "other/name.png"}, "unexpected uploaded name"),
    ],
)
def test_upload_failure_is_persisted_without_submit_and_releases_the_owned_runtime_and_lease(
    tmp_path, server_kwargs, reason
):
    service, servers = _controller(tmp_path, **server_kwargs)
    record = service.prepare(I2V, prompt="cafe", seed=2101, image=_image())
    job = service.execute(record["task_id"])

    saved = service.get(record["task_id"])
    assert job["status"] == saved["status"] == "failed"
    assert saved["error"].startswith("input_upload_failed") and reason in saved["error"]
    assert (
        saved["input_upload"]["prompt_submitted"] is False
        and saved["input_upload"]["stage"] == "upload"
    )
    assert (
        _Runner.instances[-1].runs == []
    )  # runner.run(제출·GPU 작업)에 도달하지 않았고 재제출도 없다
    assert servers[0].stopped and _lease(service) is None
    proof = saved["lease_release_proof"]["evidence"]
    assert (
        proof["server_health_after_stop"] is False
        and proof["stop_result"]["stopped"] is True
    )
    assert not _staged_dir(service, record["task_id"]).exists()


def test_tampered_or_symlinked_staged_input_is_never_uploaded(tmp_path):
    service, servers = _controller(tmp_path)
    record = service.prepare(I2V, prompt="x", seed=1, image=_image())
    staged = _staged_dir(service, record["task_id"]) / NAME
    staged.write_bytes(png_bytes(color=(1, 1, 1)))  # prepare 이후 변조
    service.execute(record["task_id"])
    saved = service.get(record["task_id"])
    assert saved["status"] == "failed" and "no longer match" in saved["error"]
    assert (
        servers[0].uploads == []
        and _Runner.instances[-1].runs == []
        and _lease(service) is None
    )

    service2, servers2 = _controller(tmp_path / "second")
    record2 = service2.prepare(I2V, prompt="x", seed=1, image=_image())
    staged2 = _staged_dir(service2, record2["task_id"]) / NAME
    outside = tmp_path / "outside.png"
    outside.write_bytes(_image().data)
    staged2.unlink()
    staged2.symlink_to(outside)
    service2.execute(record2["task_id"])
    saved2 = service2.get(record2["task_id"])
    assert saved2["status"] == "failed" and "missing or outside" in saved2["error"]
    assert servers2[0].uploads == [] and _lease(service2) is None


def test_second_submission_is_rejected_by_the_shared_lease_and_leaves_no_staged_copy(
    tmp_path,
):
    service, _servers = _controller(tmp_path)
    first = service.prepare(I2V, prompt="a", seed=1, image=_image())
    with pytest.raises(LeaseBusy):
        service.prepare(I2V, prompt="b", seed=2, image=_image())
    assert [p.name for p in service._inputs_root().iterdir()] == [first["task_id"]]
    assert _lease(service)["owner_task"] == first["task_id"]


# ---------- 영수증의 입력 항목


def test_receipt_input_section_binds_hash_and_the_executed_graph_load_image():
    profile = PROFILES[I2V]
    info = {**_image().public()}
    graph = {"56": {"class_type": "LoadImage", "inputs": {"image": f"ttk/{NAME}"}}}
    section = _input_image_section(graph, profile, info)
    assert section["sha256"] == info["sha256"] and section["graph_consistent"] is True
    assert section["graph_load_image"] == f"ttk/{NAME}"
    wrong = {"56": {"class_type": "LoadImage", "inputs": {"image": "ttk/other.png"}}}
    with pytest.raises(ReceiptError, match="do not match the staged upload"):
        _input_image_section(wrong, profile, info)
    two = {
        **graph,
        "57": {"class_type": "LoadImage", "inputs": {"image": f"ttk/{NAME}"}},
    }
    with pytest.raises(ReceiptError):
        _input_image_section(two, profile, info)
    with pytest.raises(ReceiptError, match="requires the recorded input image"):
        _input_image_section(graph, profile, None)
    assert (
        _input_image_section(graph, PROFILES["wan22-5b-draft"], None) is None
    )  # T2V receipt에는 항목이 없다


# ---------- ASGI: 인증 경계와 요청 계약


class _Service:
    def __init__(self):
        self.prepared: list[dict] = []
        self.started: list[str] = []

    def profiles(self):
        return [{"id": I2V}]

    def prepare(self, profile, *, prompt, seed, image=None):
        self.prepared.append(
            {"profile": profile, "prompt": prompt, "seed": seed, "image": image}
        )
        record = {
            "task_id": "b" * 32,
            "profile": profile,
            "status": "starting",
            "owner_attempt": "internal",
            "generation_token": "internal",
        }
        if image is not None:
            record["input_image"] = {
                **image.public(),
                "staged_path": "/private/staged/x.png",
            }
        return record

    def start(self, task_id):
        self.started.append(task_id)


@pytest.fixture
def web(tmp_path, monkeypatch):
    store, pid, sha, _data = _store_with_asset(tmp_path)
    service = _Service()

    class Settings:
        data_dir = None

    monkeypatch.setattr(api, "get_settings", lambda: Settings())
    monkeypatch.setattr(api, "default_controller", lambda _d: service)
    monkeypatch.setattr(api, "default_director_store", lambda: store)
    app = FastAPI()
    app.include_router(api.router)
    return app, service, store, pid, sha


def _client(app, *, authed=True):
    if authed:
        app.dependency_overrides[require_user] = lambda: "owner"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _body(pid, sha, **image):
    return {
        "profile_id": I2V,
        "prompt": "cafe",
        "seed": 2101,
        "image": {"production_id": pid, "asset_id": NAME, "sha256": sha, **image},
    }


async def test_submit_resolves_the_owned_asset_and_never_exposes_the_staged_path(web):
    app, service, _store, pid, sha = web
    async with _client(app) as client:
        response = await client.post("/api/comfy/jobs", json=_body(pid, sha))
    assert response.status_code == 202, response.text
    body = response.json()
    assert (
        body["input_image"]["sha256"] == sha
        and "staged_path" not in body["input_image"]
    )
    sent = service.prepared[0]
    assert (
        isinstance(sent["image"], InputImage)
        and sent["image"].sha256 == sha
        and service.started == ["b" * 32]
    )


async def test_t2v_submit_keeps_the_original_prepare_call_shape(web):
    app, service, _store, _pid, _sha = web
    async with _client(app) as client:
        response = await client.post(
            "/api/comfy/jobs",
            json={"profile_id": "wan22-5b-draft", "prompt": "fox", "seed": 1},
        )
    assert response.status_code == 202 and service.prepared[0]["image"] is None
    assert "input_image" not in response.json()


async def test_request_cannot_smuggle_paths_urls_or_graphs(web):
    app, service, _store, pid, sha = web
    async with _client(app) as client:
        for extra in (
            {"path": "/etc/passwd"},
            {"url": "http://evil/x.png"},
            {"file": "../../x.png"},
        ):
            assert (
                await client.post("/api/comfy/jobs", json=_body(pid, sha, **extra))
            ).status_code == 422
        top = {**_body(pid, sha), "image_path": "/etc/passwd"}
        assert (await client.post("/api/comfy/jobs", json=top)).status_code == 422
        assert (
            await client.post(
                "/api/comfy/jobs", json={**_body(pid, sha), "image": "/etc/passwd"}
            )
        ).status_code == 422
        bad_sha = _body(pid, "ZZ" * 32)
        assert (await client.post("/api/comfy/jobs", json=bad_sha)).status_code == 422
    assert service.prepared == []


async def test_cross_production_missing_production_and_wrong_id_are_rejected_before_prepare(
    web,
):
    app, service, store, pid, sha = web
    other = store.create_production("다른 제작")["manifest"]["production_id"]
    async with _client(app) as client:
        cross = await client.post("/api/comfy/jobs", json=_body(other, sha))
        missing = await client.post(
            "/api/comfy/jobs", json=_body("00000000-0000-4000-8000-000000000000", sha)
        )
        wrong_id = await client.post(
            "/api/comfy/jobs",
            json={
                **_body(pid, sha),
                "image": {**_body(pid, sha)["image"], "asset_id": "nope.png"},
            },
        )
    assert cross.status_code == 422 and "일치하지 않습니다" in cross.json()["detail"]
    assert missing.status_code == 404
    assert wrong_id.status_code == 422
    assert service.prepared == []  # lease를 잡는 prepare까지 가지 않았다


async def test_unauthenticated_requests_cannot_reach_the_controller_or_the_asset_store(
    web,
):
    app, service, _store, pid, sha = web
    async with _client(app, authed=False) as client:
        post = await client.post("/api/comfy/jobs", json=_body(pid, sha))
        profiles = await client.get("/api/comfy/profiles")
    assert post.status_code == 401 and profiles.status_code == 401
    assert service.prepared == []


async def test_controller_refusals_map_to_422_and_409_without_starting_anything(web):
    app, service, _store, pid, sha = web

    def refuse(profile, **kwargs):
        raise ValueError(
            "this profile requires an owned reference image (asset id and sha256)"
        )

    service.prepare = refuse
    async with _client(app) as client:
        no_image = await client.post(
            "/api/comfy/jobs", json={"profile_id": I2V, "prompt": "x", "seed": 1}
        )
        assert (
            no_image.status_code == 422
            and "requires an owned reference image" in no_image.json()["detail"]
        )

        def busy(profile, **kwargs):
            raise LeaseBusy("host resource lease is busy")

        service.prepare = busy
        locked = await client.post("/api/comfy/jobs", json=_body(pid, sha))
    assert locked.status_code == 409 and service.started == []
