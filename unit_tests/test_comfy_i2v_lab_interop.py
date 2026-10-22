"""Optional interoperability check for the separately installed Comfy lab client."""

from __future__ import annotations

import email
import email.policy
import hashlib
import json

import pytest
from test_comfy_i2v_input import I2V, NAME, _controller, _image, _staged_dir


def test_lab_comfy_client_multipart_uses_the_controller_validated_bytes_during_restore_race(
    tmp_path, monkeypatch
):
    client_module = pytest.importorskip("ttk_comfy.client")
    import ttakkari.managed_comfy as managed_module

    service, _servers = _controller(tmp_path)
    image = _image()
    record = service.prepare(I2V, prompt="cafe", seed=2101, image=image)
    job = service._load(record["task_id"])
    snapshot = _staged_dir(service, record["task_id"]) / ".upload" / NAME
    captured = {}

    def stub_urlopen(request, timeout=120):
        content_type = request.get_header("Content-type")
        message = email.message_from_bytes(
            b"Content-Type: "
            + content_type.encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + request.data,
            policy=email.policy.default,
        )
        image_part = next(
            part
            for part in message.iter_parts()
            if part.get_param("name", header="content-disposition") == "image"
        )
        sent = image_part.get_payload(decode=True)
        captured["sha256"] = hashlib.sha256(sent).hexdigest()
        captured["filename"] = image_part.get_filename()

        class Response:
            def read(self):
                return json.dumps({"name": NAME, "subfolder": "ttk"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return Response()

    monkeypatch.setattr(client_module.urllib.request, "urlopen", stub_urlopen)
    monkeypatch.setattr(managed_module, "urlopen", stub_urlopen)

    class RestoreAfterRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self.stream.__enter__()

        def __exit__(self, *args):
            result = self.stream.__exit__(*args)
            snapshot.write_bytes(image.data)
            return result

    import builtins
    from pathlib import Path

    real_open = builtins.open

    def racing_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file) == snapshot and mode == "rb":
            snapshot.write_bytes(b"transient same-UID modification")
            return RestoreAfterRead(real_open(file, mode, *args, **kwargs))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", racing_open)
    uploaded = service._upload_input(
        job, {"client": client_module.ComfyClient("http://stub.invalid")}
    )

    assert captured == {"sha256": image.sha256, "filename": NAME}
    assert uploaded == f"ttk/{NAME}"
