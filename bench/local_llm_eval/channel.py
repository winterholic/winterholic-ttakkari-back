"""Dedicated, bounded binary transport. Framing is validation, not authentication."""

import json
import os
import selectors
import signal
import struct
import subprocess
import time
from dataclasses import dataclass

MAGIC = b"LLO1"
HEADER = struct.Struct("!4sI")
MAX_FRAME = 200000
MAX_OUTPUT = 4000000


class ChannelError(ValueError):
    pass


def identity(fd):
    st = os.fstat(fd)
    return [st.st_dev, st.st_ino, st.st_mode]


def encode_frame(value):
    body = json.dumps(value, allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ChannelError("frame too large")
    return HEADER.pack(MAGIC, len(body)) + body


def decode_frame(data):
    if len(data) < HEADER.size:
        raise ChannelError("missing or truncated header")
    magic, size = HEADER.unpack(data[: HEADER.size])
    if magic != MAGIC or size > MAX_FRAME:
        raise ChannelError("invalid header")
    if len(data) != HEADER.size + size:
        raise ChannelError("expected exactly one complete frame")
    try:
        value = json.loads(data[HEADER.size :])
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChannelError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise ChannelError("frame is not an object")
    return value


def writer(fd, guard_fd, expected):
    """Capture transport functions before candidate import; emit only on original pipe.

    This detects descriptor faults in the trusted writer. It is not a secret or
    a proof against arbitrary memory corruption in the same interpreter.
    """
    stat, write, terminate = os.fstat, os.write, os._exit
    encode = encode_frame

    def send(observations):
        try:
            for descriptor in (fd, guard_fd):
                st = stat(descriptor)
                if [st.st_dev, st.st_ino, st.st_mode] != expected:
                    terminate(73)
            raw = encode({"observations": observations, "fd_identity": expected})
            while raw:
                raw = raw[write(fd, raw) :]
        except BaseException:  # noqa: BLE001 - any transport fault invalidates the run
            terminate(73)

    return send


@dataclass
class TransportResult:
    status: str
    obs: dict
    returncode: int
    detail: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""


def collect(command, cwd, env, spec, timeout):
    """Drain stdin, stdout, stderr and the observation pipe without unbounded buffers."""
    read_fd, write_fd = os.pipe()
    guard_fd = os.dup(write_fd)
    expected = identity(write_fd)
    proc = None
    streams = {"stdout": bytearray(), "stderr": bytearray(), "channel": bytearray()}
    sizes = dict.fromkeys(streams, 0)
    failure = ""
    try:
        proc = subprocess.Popen(
            command + [str(write_fd), str(guard_fd), json.dumps(expected)],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(write_fd, guard_fd),
            start_new_session=True,
        )
        os.close(write_fd)
        os.close(guard_fd)
        write_fd = guard_fd = -1
        pending = memoryview(json.dumps(spec).encode())
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream, name in [
                (proc.stdout, "stdout"),
                (proc.stderr, "stderr"),
                (read_fd, "channel"),
            ]:
                os.set_blocking(
                    stream if isinstance(stream, int) else stream.fileno(), False
                )
                selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            while selector.get_map():
                if time.monotonic() >= deadline:
                    failure = "timeout"
                    break
                for key, _ in selector.select(
                    min(0.1, max(0, deadline - time.monotonic()))
                ):
                    if key.data == "stdin":
                        try:
                            written = os.write(key.fd, pending[:65536])
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            proc.stdin.close()
                        continue
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    sizes[name] += len(block)
                    streams[name].extend(block)
                    if name != "channel":
                        del streams[name][:-400]
                    if sizes[name] > (
                        MAX_FRAME + HEADER.size if name == "channel" else MAX_OUTPUT
                    ):
                        failure = "output_limit"
                        break
                if failure:
                    break
        if failure:
            _kill_group(proc.pid)
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = "timeout"
            _kill_group(proc.pid)
            proc.wait()
        # No descendant may keep mutating parent-observed files after collection.
        _kill_group(proc.pid)
        out = streams["stdout"].decode("utf-8", "replace")
        err = streams["stderr"].decode("utf-8", "replace")
        base = {"returncode": proc.returncode, "stdout_tail": out, "stderr_tail": err}
        if failure:
            return TransportResult(failure, {}, **base)
        if proc.returncode != 0:
            return TransportResult(
                "crash", {}, detail=f"nonzero exit {proc.returncode}", **base
            )
        if not streams["channel"]:
            return TransportResult(
                "stub_exit0", {}, detail="no observation frame", **base
            )
        try:
            envelope = decode_frame(streams["channel"])
            if envelope.get("fd_identity") != expected or not isinstance(
                envelope.get("observations"), dict
            ):
                raise ChannelError("invalid channel identity/envelope")
        except ChannelError as exc:
            return TransportResult("bad_obs", {}, detail=str(exc), **base)
        return TransportResult("OBS", envelope["observations"], **base)
    finally:
        for fd in (read_fd, write_fd, guard_fd):
            if fd >= 0:
                os.close(fd)
        if proc:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream and not stream.closed:
                    stream.close()


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
