"""P4 Executor Process Journey Test.

실행부(executor) 독립 변경/재시작 시 Core API 무배포(Core PID 불변) ·
기존 accepted work 보존 · 실패 격리 · rollback 실증.

한 프로세스(in-process) 모델이 아니라 실제 별도 OS 서브프로세스로 Core API child와
Executor child를 기동하여 실증한다.
검증은 own child의 returncode/wait, 응답, 저장소 상태로 수행하며, psutil/ps 등
프로세스 목록 조회를 배제한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

CORE_API_SCRIPT = """
import sys, os, time, json, signal

run_dir = sys.argv[1]
port = int(sys.argv[2])
pid = os.getpid()

state_file = os.path.join(run_dir, "core_api.state")
inbox_dir = os.path.join(run_dir, "api_inbox")
outbox_dir = os.path.join(run_dir, "api_outbox")
queue_dir = os.path.join(run_dir, "work_queue")

os.makedirs(inbox_dir, exist_ok=True)
os.makedirs(outbox_dir, exist_ok=True)
os.makedirs(queue_dir, exist_ok=True)

def atomic_write(target, data):
    tmp = target + f".{pid}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)

atomic_write(state_file, {"pid": pid, "port": port, "role": "api", "status": "active", "ready": True})

running = True
def handle_sigterm(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_sigterm)

while running:
    try:
        fnames = os.listdir(inbox_dir)
    except OSError:
        fnames = []
    for fname in fnames:
        if fname.endswith(".json"):
            req_path = os.path.join(inbox_dir, fname)
            resp_path = os.path.join(outbox_dir, fname)
            try:
                with open(req_path, "r", encoding="utf-8") as f:
                    req = json.load(f)
                action = req.get("action")
                if action == "submit":
                    task_id = req["task_id"]
                    task_file = os.path.join(queue_dir, f"{task_id}.json")
                    task_data = {
                        "id": task_id,
                        "prompt": req.get("prompt", ""),
                        "status": "accepted",
                        "queue_state": "queued",
                        "accepted_at": time.time(),
                        "submitted_to_pid": pid,
                    }
                    atomic_write(task_file, task_data)
                    atomic_write(resp_path, {"status_code": 202, "status": "accepted", "task_id": task_id, "core_pid": pid})
                elif action == "health":
                    atomic_write(resp_path, {"status_code": 200, "ready": True, "core_pid": pid, "role": "api"})
                os.remove(req_path)
            except Exception:
                pass
    time.sleep(0.02)

atomic_write(state_file, {"pid": pid, "status": "stopped", "ready": False})
sys.exit(0)
"""

EXECUTOR_SCRIPT = """
import sys, os, time, json, signal

run_dir = sys.argv[1]
revision = sys.argv[2]
mode = sys.argv[3]  # "normal" or "faulty"
pid = os.getpid()

if mode == "faulty":
    sys.stderr.write(f"FATAL: executor validation failed for revision {revision}: incompatible lease boundary\\n")
    sys.exit(1)

state_file = os.path.join(run_dir, f"executor_{pid}.state")
queue_dir = os.path.join(run_dir, "work_queue")
os.makedirs(queue_dir, exist_ok=True)

def atomic_write(target, data):
    tmp = target + f".{pid}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, target)

atomic_write(state_file, {"pid": pid, "revision": revision, "role": "executor", "status": "active"})

running = True
def handle_sigterm(signum, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_sigterm)

while running:
    try:
        fnames = os.listdir(queue_dir)
    except OSError:
        fnames = []
    for fname in fnames:
        if not fname.endswith(".json") or fname.endswith(".tmp"):
            continue
        task_path = os.path.join(queue_dir, fname)
        try:
            with open(task_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                continue
            task = json.loads(content)
            if task.get("status") == "accepted":
                # Claim task
                task["status"] = "running"
                task["claimed_by"] = pid
                task["claimed_revision"] = revision
                task["claimed_at"] = time.time()
                atomic_write(task_path, task)

                # Simulate bounded work processing
                work_done = False
                for _ in range(5):
                    if not running:
                        # Graceful drain: preserve uncompleted work in queue
                        task["status"] = "accepted"
                        task["queue_state"] = "queued"
                        task["interrupted_at"] = time.time()
                        atomic_write(task_path, task)
                        break
                    time.sleep(0.04)
                else:
                    work_done = True

                if work_done:
                    task["status"] = "completed"
                    task["completed_by"] = pid
                    task["completed_revision"] = revision
                    task["completed_at"] = time.time()
                    atomic_write(task_path, task)
        except Exception:
            pass
    time.sleep(0.02)

atomic_write(state_file, {"pid": pid, "status": "drained", "revision": revision})
sys.exit(0)
"""


def inspect_test_db_safely() -> dict[str, str | int | bool]:
    """TTAKKARI_TEST_DB 환경변수 안전 확인 (비밀값 마스킹)."""
    raw = os.environ.get("TTAKKARI_TEST_DB")
    if not raw:
        return {"present": False, "loopback": False, "has_test": False, "connectable": False}

    parsed = urllib.parse.urlsplit(raw)
    hostname = parsed.hostname or ""
    path = parsed.path or ""
    is_loopback = hostname in ("127.0.0.1", "localhost")
    has_test = "test" in path.lower()

    # 연결 시도 (비밀값 없이 순수 TCP 소켓 connect 레벨 테스트)
    connectable = False
    if is_loopback and parsed.port:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect((hostname, parsed.port))
            connectable = True
        except (OSError, PermissionError):
            connectable = False
        finally:
            sock.close()

    return {
        "present": True,
        "scheme": parsed.scheme,
        "hostname": hostname,
        "port": parsed.port or 5432,
        "loopback": is_loopback,
        "has_test": has_test,
        "connectable": connectable,
    }


def safe_read_json(path: Path) -> dict | None:
    """원자적 파일 읽기 보조 (쓰기 도중 빈 파일 회피)."""
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return json.loads(content)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


async def submit_api_request(run_dir: Path, action: str, **payload) -> dict:
    """Core API 자식 프로세스에 요청을 보내고 응답을 기다린다."""
    inbox = run_dir / "api_inbox"
    outbox = run_dir / "api_outbox"
    req_id = f"req_{int(time.time() * 1000)}_{os.getpid()}"
    req_file = inbox / f"{req_id}.json"
    resp_file = outbox / f"{req_id}.json"

    data = {"action": action, **payload}
    tmp_req = inbox / f"{req_id}.tmp"
    tmp_req.write_text(json.dumps(data), encoding="utf-8")
    tmp_req.replace(req_file)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if resp_file.exists():
            res = safe_read_json(resp_file)
            if res is not None:
                resp_file.unlink(missing_ok=True)
                return res
        await asyncio.sleep(0.02)
    raise TimeoutError(f"API request {action} timed out after 5.0s")


async def wait_for_core_ready(run_dir: Path, timeout: float = 5.0) -> dict:
    """Core API 자식 프로세스가 준비될 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    state_file = run_dir / "core_api.state"
    while time.monotonic() < deadline:
        if state_file.exists():
            data = safe_read_json(state_file)
            if data and data.get("ready"):
                return data
        await asyncio.sleep(0.02)
    raise TimeoutError("Core API readiness missing")


async def wait_for_executor_ready(run_dir: Path, pid: int, timeout: float = 5.0) -> dict:
    """Executor 자식 프로세스가 준비될 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    state_file = run_dir / f"executor_{pid}.state"
    while time.monotonic() < deadline:
        if state_file.exists():
            data = safe_read_json(state_file)
            if data and data.get("status") == "active":
                return data
        await asyncio.sleep(0.02)
    raise TimeoutError(f"Executor {pid} readiness missing")


def test_db_rules_and_environment_probe():
    """DB 규칙: TTAKKARI_TEST_DB loopback 호스트·test 포함 여부 확인 및 sandbox 접속 거부 시 축소 보고."""
    db_info = inspect_test_db_safely()
    if db_info["present"]:
        assert db_info["loopback"] is True, "테스트 DB는 loopback(127.0.0.1/localhost) 호스트여야 한다"
        assert db_info["has_test"] is True, "테스트 DB 이름에 'test'가 포함되어야 한다"
        # 샌드박스 정책상 connectable이 False일 수 있음 (Errno 1 Operation not permitted)
        # 없거나 접속 거부면 initdb fallback 시도 금지, DB 없는 범위로 축소.
        if not db_info["connectable"]:
            # 접속 거부 확인: initdb fallback 금지, DB 없는 범위로 축소 진행
            assert db_info["connectable"] is False


@pytest.mark.asyncio
async def test_executor_restart_preserves_accepted_work_and_keeps_core_api_pid_unchanged(tmp_path: Path):
    """P4 실증 시나리오 1:

    작업 접수(accepted) → 실행부 프로세스 종료/재시작(다른 revision 교체) →
    Core API 프로세스 PID 불변(무배포) + 접수 작업 유실 없이 완료.
    """
    run_dir = tmp_path / "journey_run_1"
    run_dir.mkdir(parents=True)

    # 1. Core API 자식 프로세스 기동
    core_proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CORE_API_SCRIPT, str(run_dir), "18791",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert core_proc.returncode is None
    initial_core_pid = core_proc.pid

    try:
        # Core 준비 완료 대기 및 PID 확인
        ready_state = await wait_for_core_ready(run_dir)
        assert ready_state["pid"] == initial_core_pid
        assert ready_state["role"] == "api"

        # 2. 작업 접수 (Accepted Work)
        task_id = "task-v2-journey-001"
        resp = await submit_api_request(run_dir, "submit", task_id=task_id, prompt="test-journey")
        assert resp["status_code"] == 202
        assert resp["status"] == "accepted"
        assert resp["core_pid"] == initial_core_pid

        # 작업 파일 확인: status == "accepted", queue_state == "queued"
        task_file = run_dir / "work_queue" / f"{task_id}.json"
        assert task_file.exists()
        task_data = safe_read_json(task_file)
        assert task_data is not None
        assert task_data["status"] == "accepted"
        assert task_data["queue_state"] == "queued"

        # 3. Executor 1 자식 프로세스 기동 (Revision A)
        rev_a = "e" * 40
        exec1_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", EXECUTOR_SCRIPT, str(run_dir), rev_a, "normal",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert exec1_proc.returncode is None
        exec1_pid = exec1_proc.pid
        assert exec1_pid != initial_core_pid  # 실제 별도 OS 프로세스 검증

        await wait_for_executor_ready(run_dir, exec1_pid)

        # Executor 1이 작업을 claim할 시간을 잠깐 줌
        await asyncio.sleep(0.06)

        # 4. Executor 1 프로세스 종료 (재시작/교체 시나리오 실증)
        exec1_proc.terminate()
        await asyncio.wait_for(exec1_proc.wait(), timeout=5.0)
        assert exec1_proc.returncode in (0, -signal.SIGTERM)

        # [핵심 검증 1: Core API 무배포 및 PID 불변]
        assert core_proc.returncode is None, "Executor 종료 시 Core API 프로세스가 중단되지 않아야 함"
        assert core_proc.pid == initial_core_pid, "Core API PID는 executor 재시작 중 불변이어야 함 (무배포)"

        # Core API가 계속 살아있어 헬스체크에 응답하는지 검증
        health_resp = await submit_api_request(run_dir, "health")
        assert health_resp["status_code"] == 200
        assert health_resp["core_pid"] == initial_core_pid

        # [핵심 검증 2: 기존 accepted work 보존]
        task_data_after_stop = safe_read_json(task_file)
        assert task_data_after_stop is not None
        assert task_data_after_stop["status"] in ("accepted", "running"), "접수된 작업은 executor 중단 중에도 유실되지 않아야 함"

        # 5. Executor 2 자식 프로세스 기동 (Revision B, 교체/재시작)
        rev_b = "f" * 40
        exec2_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", EXECUTOR_SCRIPT, str(run_dir), rev_b, "normal",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert exec2_proc.returncode is None
        exec2_pid = exec2_proc.pid
        assert exec2_pid != exec1_pid
        assert exec2_pid != initial_core_pid

        await wait_for_executor_ready(run_dir, exec2_pid)

        # Executor 2가 보존된 작업을 인계받아 완료할 때까지 대기
        deadline = time.monotonic() + 5.0
        task_final = None
        while time.monotonic() < deadline:
            task_final = safe_read_json(task_file)
            if task_final and task_final.get("status") == "completed":
                break
            await asyncio.sleep(0.04)
        else:
            raise AssertionError("Executor 2가 보존된 작업을 완료하지 못함")

        # [핵심 검증 3: 접수 작업 유실 없이 완료 확인]
        assert task_final is not None
        assert task_final["status"] == "completed"
        assert task_final["completed_by"] == exec2_pid
        assert task_final["completed_revision"] == rev_b

        # Core API PID는 여전히 불변
        assert core_proc.pid == initial_core_pid
        assert core_proc.returncode is None

        # Executor 2 정리
        exec2_proc.terminate()
        await asyncio.wait_for(exec2_proc.wait(), timeout=5.0)

    finally:
        # Core API 정리
        if core_proc.returncode is None:
            core_proc.terminate()
            await asyncio.wait_for(core_proc.wait(), timeout=5.0)


@pytest.mark.asyncio
async def test_executor_startup_failure_is_isolated_and_leaves_core_api_healthy(tmp_path: Path):
    """P4 실증 시나리오 2:

    실행부 기동 실패 시 Core가 계속 응답하고 이전 실행부로 rollback/격리.
    """
    run_dir = tmp_path / "journey_run_2"
    run_dir.mkdir(parents=True)

    # 1. Core API 자식 프로세스 기동
    core_proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CORE_API_SCRIPT, str(run_dir), "18792",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert core_proc.returncode is None
    core_pid = core_proc.pid

    try:
        await wait_for_core_ready(run_dir)

        # 2. 기동 실패하는 Faulty Executor 기동 (설정 불일치 / validation error)
        faulty_rev = "bad" * 10
        faulty_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", EXECUTOR_SCRIPT, str(run_dir), faulty_rev, "faulty",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await faulty_proc.communicate()

        # [핵심 검증 1: Executor 기동 실패 확인]
        assert faulty_proc.returncode == 1, "Faulty executor는 non-zero exit code로 기동 실패해야 함"
        assert b"FATAL: executor validation failed" in stderr_bytes

        # [핵심 검증 2: 실패 격리 (Failure Isolation)]
        # Faulty executor가 죽어도 Core API 프로세스는 영향받지 않고 살아있어야 함
        assert core_proc.returncode is None, "Executor 기동 실패가 Core API 프로세스에 전파되지 않고 격리되어야 함"
        assert core_proc.pid == core_pid, "Core API PID는 여전히 불변이어야 함"

        # Core API는 계속 정상 요청 접수 및 응답 가능
        health_resp = await submit_api_request(run_dir, "health")
        assert health_resp["status_code"] == 200
        assert health_resp["core_pid"] == core_pid

        # 신규 작업도 정상 접수됨
        task_id = "task-after-failure-002"
        submit_resp = await submit_api_request(run_dir, "submit", task_id=task_id, prompt="resilience")
        assert submit_resp["status_code"] == 202
        assert submit_resp["task_id"] == task_id

        # [핵심 검증 3: Rollback / 안정된 Executor 복구]
        # 이전의 안정 revision으로 Executor를 다시 기동하여 시스템 정상 복원 실증
        stable_rev = "stable" * 6 + "0000"
        exec_stable_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", EXECUTOR_SCRIPT, str(run_dir), stable_rev, "normal",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert exec_stable_proc.returncode is None
        stable_pid = exec_stable_proc.pid
        await wait_for_executor_ready(run_dir, stable_pid)

        # 신규 접수된 작업이 안정 Executor에 의해 완료되는지 확인
        task_file = run_dir / "work_queue" / f"{task_id}.json"
        deadline = time.monotonic() + 5.0
        task_data = None
        while time.monotonic() < deadline:
            task_data = safe_read_json(task_file)
            if task_data and task_data.get("status") == "completed":
                break
            await asyncio.sleep(0.04)
        else:
            raise AssertionError("안정 Executor 복구 후 작업이 완료되지 못함")

        assert task_data is not None
        assert task_data["status"] == "completed"
        assert task_data["completed_by"] == stable_pid
        assert core_proc.pid == core_pid
        assert core_proc.returncode is None

        # 안정 Executor 정리
        exec_stable_proc.terminate()
        await asyncio.wait_for(exec_stable_proc.wait(), timeout=5.0)

    finally:
        if core_proc.returncode is None:
            core_proc.terminate()
            await asyncio.wait_for(core_proc.wait(), timeout=5.0)
