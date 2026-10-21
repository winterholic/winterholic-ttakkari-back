from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import ClassVar

import psutil
import pytest

from ttakkari.host_resource import LeaseBusy, LeaseError, ResourceLeaseManager
from ttakkari.managed_comfy import PROFILES, ComfyManagedController, ManagedClient


class FakeServer:
    def __init__(self, *, owned=True):
        self.owned = owned
        self.started = False
        self.stopped = False
        self.proc = type("Proc", (), {"pid": 2317, "returncode": 0,
                                      "poll": lambda _self: 0 if self.stopped else None})()
        self.client = type("Client", (), {
            "alive": lambda _self: self.started and not self.stopped,
            "queue": lambda _self: {"queue_running": [], "queue_pending": []},
        })()

    def start(self, **_kwargs):
        self.started = True
        return self

    def stop(self):
        self.stopped = True
        return {"stopped": True, "pid": self.proc.pid}


class FakeRunner:
    instances: ClassVar[list] = []
    result_status = "succeeded"
    result_failure = None
    cancel_status = "cancelled"

    def __init__(self, server, _profile, jobs_dir, _controller, lease):
        self.server = server
        self.jobs_dir = jobs_dir
        self.lease = lease
        self.runs = []
        self.cancel_calls = []
        self.reconcile_calls = []
        self.result_status = self.__class__.result_status
        self.__class__.instances.append(self)

    def check(self, _workflow, version):
        assert version == PROFILES["wan22-5b-draft"].revision
        return {"ok": True}

    def run(self, **kwargs):
        self.runs.append(kwargs)
        job_dir = self.jobs_dir / "adapter-job-1"
        job_dir.mkdir(parents=True)
        job = {"id": "adapter-job-1", "tag": kwargs["tag"], "status": self.result_status,
               "attempts": [{"attempt_id": "attempt-1", "external_prompt_id": "prompt-1"}],
               "files": [], "failure": self.__class__.result_failure}
        (job_dir / "job.json").write_text(json.dumps(job))
        return job

    def reconcile(self, job_id):
        self.reconcile_calls.append(job_id)
        return {"id": job_id, "status": "succeeded", "files": [], "failure": None}

    def cancel(self, job_id):
        self.cancel_calls.append(job_id)
        return {"id": job_id, "status": self.__class__.cancel_status, "files": [], "failure": None,
                "attempts": [{"attempt_id": "attempt-1", "external_prompt_id": "prompt-1",
                              "idempotency_key": "key-1"}]}


@pytest.fixture
def controller(tmp_path):
    FakeRunner.instances.clear()
    FakeRunner.result_status = "succeeded"
    FakeRunner.result_failure = None
    FakeRunner.cancel_status = "cancelled"
    servers = []

    def runtime_factory(profile, runtime_instance, data_dir, _lease, _controller):
        server = FakeServer()
        servers.append(server)
        return server, FakeRunner(server, profile, data_dir / "managed-comfy" / "jobs", None, _lease)

    service = ComfyManagedController(data_dir=tmp_path, runtime_factory=runtime_factory,
                                     spawn=lambda fn, *args: fn(*args))
    return service, servers


def test_registered_profile_is_fixed_and_rejects_unknown_workflow(controller):
    service, _servers = controller
    assert service.profiles() == [{"id": "wan22-5b-draft", "workflow": "t2v-wan22-5b",
                                  "revision": PROFILES["wan22-5b-draft"].revision,
                                  "preset": "draft"}]
    with pytest.raises(ValueError, match="registered profile"):
        service.prepare("arbitrary-shell-or-workflow", prompt="x", seed=1)


def test_prepare_rejects_extra_parameters_before_acquiring_lease(controller):
    service, _servers = controller
    with pytest.raises(ValueError, match="parameters"):
        service.prepare("wan22-5b-draft", prompt="fox", seed=1, extra_graph={"x": 1})
    assert ResourceLeaseManager(data_dir=service.data_dir).current() is None


def test_run_uses_registered_runner_under_shared_lease_and_releases_after_owned_stop(controller):
    service, servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    task_id = record["task_id"]

    service.execute(task_id)

    assert servers[0].started and servers[0].stopped
    runner = FakeRunner.instances[-1]
    assert len(runner.runs) == 1
    args = runner.runs[0]
    assert args["name"] == "t2v-wan22-5b"
    assert args["version"] == PROFILES["wan22-5b-draft"].revision
    assert args["preset"] == "draft" and args["params"] == {"prompt": "A fox", "seed": 7}
    saved = service.get(task_id)
    assert saved["status"] == "succeeded"
    lease = ResourceLeaseManager(data_dir=service.data_dir).current()
    assert lease is None
    record_path = service._path(task_id)
    assert json.loads(Path(record_path).read_text())["runner_job_id"] == "adapter-job-1"


def test_uncertain_runner_result_keeps_runtime_and_lease_until_reconcile(controller):
    service, servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    FakeRunner.result_status = "needs_reconcile"
    service.execute(record["task_id"])

    assert not servers[0].stopped
    lease = ResourceLeaseManager(data_dir=service.data_dir).current()
    assert lease and lease["owner_task"] == record["task_id"]

    result = service.reconcile(record["task_id"])

    assert result["status"] == "succeeded"
    assert servers[0].stopped
    assert ResourceLeaseManager(data_dir=service.data_dir).current() is None
    assert FakeRunner.instances[-1].reconcile_calls == ["adapter-job-1"]


def test_download_failure_keeps_lease_and_reconcile_reuses_adapter_collection(controller):
    service, servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    FakeRunner.result_status = "failed"
    FakeRunner.result_failure = {"stage": "download", "error": "connection lost"}
    service.execute(record["task_id"])

    assert service.get(record["task_id"])["status"] == "needs_reconcile"
    assert not servers[0].stopped
    assert ResourceLeaseManager(data_dir=service.data_dir).current()

    result = service.reconcile(record["task_id"])

    assert result["status"] == "succeeded"
    assert FakeRunner.instances[-1].reconcile_calls == ["adapter-job-1"]
    assert servers[0].stopped
    assert ResourceLeaseManager(data_dir=service.data_dir).current() is None


def test_unowned_preexisting_runtime_does_not_submit_or_release_claim(controller):
    service, servers = controller
    servers.clear()

    def runtime_factory(profile, runtime_instance, data_dir, _lease, _controller):
        server = FakeServer(owned=False)
        servers.append(server)
        return server, FakeRunner(server, profile, data_dir / "managed-comfy" / "jobs", None, _lease)

    service.runtime_factory = runtime_factory
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    service.execute(record["task_id"])

    assert FakeRunner.instances[-1].runs == []
    assert not servers[0].stopped
    lease = ResourceLeaseManager(data_dir=service.data_dir).current()
    assert lease and lease["owner_task"] == record["task_id"]
    assert service.get(record["task_id"])["status"] == "needs_reconcile"


def test_other_task_cannot_cancel_or_reconcile_owner_job(controller):
    service, _servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    with pytest.raises(PermissionError, match="owner"):
        service.cancel(record["task_id"], owner_task="foreign-task")
    with pytest.raises(PermissionError, match="owner"):
        service.reconcile(record["task_id"], owner_task="foreign-task")


def test_concurrent_submit_is_rejected_by_the_same_host_resource_lease(controller):
    service, _servers = controller
    first = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    with pytest.raises(LeaseBusy):
        service.prepare("wan22-5b-draft", prompt="A bird", seed=8)
    assert ResourceLeaseManager(data_dir=service.data_dir).current()["owner_task"] == first["task_id"]


def test_stale_owner_reconcile_releases_only_after_exact_runtime_stop_proof(controller, monkeypatch):
    service, _servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    active = service._active.pop(record["task_id"])
    active["heartbeat_stop"].set()
    active["heartbeat_thread"].join(timeout=1)
    active["lease"]._lock_fd.close()  # simulate backend exit; preserve the on-disk active lease
    saved = service.get(record["task_id"])
    saved.update({"runtime_pid": 711, "runtime_created_at": 100.0})
    service._save(saved)
    monkeypatch.setattr(service, "_owned_runtime_absent", lambda _job: True)

    result = service.reconcile(record["task_id"])

    assert result["status"] == "needs_reconcile"
    assert "still needs collection reconciliation" in result["error"]
    assert ResourceLeaseManager(data_dir=service.data_dir).current() is None


def test_unknown_runtime_identity_does_not_release_stale_lease(controller, monkeypatch):
    service, _servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    active = service._active.pop(record["task_id"])
    active["heartbeat_stop"].set()
    active["heartbeat_thread"].join(timeout=1)
    active["lease"]._lock_fd.close()
    monkeypatch.setattr(service, "_owned_runtime_absent", lambda _job: None)

    result = service.reconcile(record["task_id"])

    assert result["status"] == "needs_reconcile"
    assert "indeterminate" in result["error"]
    assert ResourceLeaseManager(data_dir=service.data_dir).current()


def test_cancel_before_submission_stops_only_new_owned_server(controller):
    service, servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    service._active[record["task_id"]]["lease"].request_cancel()

    result = service.execute(record["task_id"])

    assert result["status"] == "cancelled"
    assert servers[0].stopped
    assert FakeRunner.instances[-1].runs == []
    assert ResourceLeaseManager(data_dir=service.data_dir).current() is None


def test_interrupt_requires_single_owned_running_prompt_and_live_generation_token(tmp_path):
    manager = ResourceLeaseManager(data_dir=tmp_path)
    lease = manager.acquire(owner_task="task", owner_attempt="attempt", runtime_instance="runtime",
                            resource_family="comfy", claim={"profile": "wan22-5b-draft"})
    lease.mark_running()
    lease.request_cancel()

    class Client:
        def __init__(self, queue):
            self.queue_value = queue
            self.requests = []

        def queue(self):
            return self.queue_value

        def _req(self, method, path, body):
            self.requests.append((method, path, body))

    class Server:
        owned = True
        proc = type("Proc", (), {"poll": lambda _self: None})()

    job = {"id": "job"}
    attempt = {"external_prompt_id": "prompt", "attempt_id": "a", "idempotency_key": "k"}
    entry = [None, "prompt", None, {"ttk_job": "job", "ttk_attempt_id": "a",
                                    "ttk_idempotency_key": "k"}]
    client = Client({"queue_running": [entry], "queue_pending": []})
    owned = lambda e, j, a: (e[1] == a["external_prompt_id"] and e[3]["ttk_job"] == j["id"]
                             and e[3]["ttk_attempt_id"] == a["attempt_id"]
                             and e[3]["ttk_idempotency_key"] == a["idempotency_key"])
    managed = ManagedClient(client, owned, manager, lease, Server())

    managed.interrupt_owned(job, attempt)
    assert client.requests == [("POST", "/interrupt", {"prompt_id": "prompt"})]
    client.queue_value = {"queue_running": [entry, entry], "queue_pending": []}
    with pytest.raises(LeaseError, match="sole prompt"):
        managed.interrupt_owned(job, attempt)
    lease._lock_fd.close()


def test_cross_generation_cancel_is_interrupted_only_by_current_lease_owner(controller):
    service, _servers = controller
    record = service.prepare("wan22-5b-draft", prompt="A fox", seed=7)
    active = service._active[record["task_id"]]
    server = FakeServer()
    server.started = True
    runner = FakeRunner(server, PROFILES["wan22-5b-draft"], service.data_dir / "managed-comfy" / "jobs",
                        None, active["lease"])
    runner_job_dir = runner.jobs_dir / "adapter-job-1"
    runner_job_dir.mkdir(parents=True)
    (runner_job_dir / "job.json").write_text(json.dumps({
        "id": "adapter-job-1", "tag": record["task_id"], "status": "running",
        "attempts": [{"attempt_id": "attempt-1", "external_prompt_id": "prompt-1",
                      "idempotency_key": "key-1"}],
    }))

    class InterruptClient:
        def __init__(self):
            self.interrupted = []

        def interrupt_owned(self, job, attempt):
            assert job["id"] == "adapter-job-1"
            self.interrupted.append(attempt["external_prompt_id"])

    client = InterruptClient()
    active.update({"server": server, "runner": runner, "client": client})
    active["lease"].mark_running()
    foreign_controller = ComfyManagedController(data_dir=service.data_dir, runtime_factory=lambda *_: None)
    FakeRunner.cancel_status = "cancel_requested"

    request = foreign_controller.cancel(record["task_id"], owner_task=record["task_id"])
    assert request["status"] == "cancel_requested"
    result = service._maybe_cancel(record["task_id"], active)

    assert result["status"] == "cancel_requested"
    assert client.interrupted == ["prompt-1"]
    assert active["cancel_interrupt_sent"] is True
    active["heartbeat_stop"].set()
    active["heartbeat_thread"].join(timeout=1)
    active["lease"]._lock_fd.close()


def _stale_job(pid, created):
    return {"runtime_pid": pid, "runtime_created_at": created, "runtime_instance": "runtime-x",
            "profile": "wan22-5b-draft", "task_id": "t", }


@pytest.mark.parametrize(("error", "port_alive", "expected"), [
    (psutil.AccessDenied(4242), False, None),      # 권한 거부는 부재가 아니다
    (OSError("lookup blocked"), False, None),
    (psutil.NoSuchProcess(4242), False, True),     # 프로세스 없음 + 포트 죽음 = 확인된 부재
    (psutil.NoSuchProcess(4242), None, None),      # 포트 상태를 모르면 부재로 보지 않는다
    (psutil.NoSuchProcess(4242), True, None),      # 포트가 응답하면 부재가 아니다
])
def test_owned_runtime_absence_requires_confirmed_process_and_port_evidence(
        controller, monkeypatch, error, port_alive, expected):
    service, _servers = controller
    real = psutil.Process

    def lookup(pid):
        if pid == 4242:
            raise error
        return real(pid)

    monkeypatch.setattr(psutil, "Process", lookup)
    monkeypatch.setattr(service, "_is_comfy_port_alive", lambda: port_alive)

    assert service._owned_runtime_absent(_stale_job(4242, 100.0)) is expected


def test_owned_runtime_absence_ignores_missing_identity_and_reused_pid_rules(controller, monkeypatch):
    service, _servers = controller
    monkeypatch.setattr(service, "_is_comfy_port_alive", lambda: False)
    child = subprocess.Popen(["sleep", "60"])
    try:
        created = psutil.Process(child.pid).create_time()
        assert service._owned_runtime_absent(_stale_job(None, created)) is None  # pid 근거 누락
        assert service._owned_runtime_absent(_stale_job(child.pid, None)) is None  # 시작 시각 근거 누락
        # 살아 있고 시작 시각이 같지만 runtime instance 환경변수·cwd 로 소유를 확인할 수 없다 → unknown
        assert service._owned_runtime_absent(_stale_job(child.pid, created)) is None
        # 같은 pid 를 다른 프로세스가 쓴다(시작 시각 불일치) + 포트 죽음 → 기록된 runtime 은 확인된 부재
        assert service._owned_runtime_absent(_stale_job(child.pid, created - 3600)) is True
        assert child.poll() is None  # 다른 프로세스는 건드리지 않는다
    finally:
        child.kill()
        child.wait()
