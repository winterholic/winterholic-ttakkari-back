"""CDP wire-level controls; no browser, login profile or external requests."""

import asyncio
import json
import subprocess
from unittest.mock import Mock

import pytest

from ttakkari.stocks import tossinvest as ad


class Wire:
    def __init__(self):
        self.sent = []
        self.inbox = asyncio.Queue()

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        method = msg["method"]
        result = {}
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {"targetId": "page-1", "type": "page", "url": "about:blank"}
                ]
            }
        if method == "Target.attachToTarget":
            result = {"sessionId": "page-session"}
        if method == "Page.navigate":
            for ident, verb, url in [
                ("write", "POST", "https://api.tossinvest.com/v1/summary"),
                ("order", "GET", "https://api.tossinvest.com/v1/order"),
                ("foreign", "GET", "https://evil.example.com/"),
                ("read", "GET", "https://api.tossinvest.com/v1/summary?secret=hidden"),
            ]:
                await self.inbox.put(
                    json.dumps(
                        {
                            "method": "Fetch.requestPaused",
                            "sessionId": "page-session",
                            "params": {
                                "requestId": ident,
                                "request": {"method": verb, "url": url},
                            },
                        }
                    )
                )
            # Events may precede the navigate acknowledgement.
            await self.inbox.put(
                json.dumps(
                    {"method": "Page.loadEventFired", "sessionId": "page-session"}
                )
            )
        if method == "Runtime.evaluate":
            result = {"result": {"value": json.dumps({"hasLogin": True})}}
        await self.inbox.put(json.dumps({"id": msg["id"], "result": result}))

    async def recv(self):
        return await self.inbox.get()

    async def close(self):
        pass


async def test_intercepts_requests_before_navigation_and_during_command_ack(
    monkeypatch,
):
    import websockets

    wire = Wire()

    async def connect(*args, **kwargs):
        return wire

    monkeypatch.setattr(websockets, "connect", connect)
    cdp = ad._Cdp("ws://127.0.0.1/browser/test")
    await cdp.open()
    try:
        sniff, seen = await cdp.navigate_and_sniff(ad.TOSS_HOME, 0.1)
        assert sniff == {"hasLogin": True}
        methods = [m["method"] for m in wire.sent]
        assert "Fetch.enable" in methods
        assert methods.index("Fetch.enable") < methods.index("Page.navigate")
        actions = {
            m["params"]["requestId"]: m["method"]
            for m in wire.sent
            if m["method"] in ("Fetch.failRequest", "Fetch.continueRequest")
        }
        assert actions == {
            "write": "Fetch.failRequest",
            "order": "Fetch.failRequest",
            "foreign": "Fetch.failRequest",
            "read": "Fetch.continueRequest",
        }
        assert seen == [{"host": "api.tossinvest.com", "path": "/v1/summary"}]
        assert all(
            m.get("sessionId") == "page-session"
            for m in wire.sent
            if m["method"].startswith(("Page.", "Runtime.", "Fetch.", "Network."))
        )
    finally:
        await cdp.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.tossinvest.com/v1/summary",
        "https://api.tossinvest.com/v1/%6frder/new",
        "https://api.tossinvest.com/v1/%256frder/new",
        "https://api.tossinvest.com:444/v1/summary",
        "https://user:password@www.tossinvest.com/",
    ],
)
def test_read_only_rejects_insecure_or_encoded_writes(url):
    with pytest.raises(ad.TossError):
        ad.assert_read_only("GET", url)


def test_interval_cannot_be_disabled():
    from pydantic import ValidationError

    from ttakkari.config import Settings

    for value in (0, 1, 299):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, tossinvest_check_interval_seconds=value)


async def test_stop_reaps_process_after_forced_kill():
    process = Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired("fake-chrome", 10), 0]
    await ad._stop(process)
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    process.stderr.close.assert_called_once()


async def test_public_static_scripts_allowed_but_static_xhr_and_post_denied():
    wire = Wire()
    cdp = ad._Cdp("unused")
    cdp._ws = wire
    cdp._reader = asyncio.create_task(cdp._read_loop())
    try:
        for ident, resource, method in [
            ("script", "Script", "GET"),
            ("xhr", "XHR", "GET"),
            ("post", "Script", "POST"),
        ]:
            await cdp._intercept(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": "page-session",
                    "params": {
                        "requestId": ident,
                        "resourceType": resource,
                        "request": {
                            "method": method,
                            "url": "https://static.toss.im/assets/app.js",
                        },
                    },
                }
            )
        actions = {m["params"]["requestId"]: m["method"] for m in wire.sent}
        assert actions == {
            "script": "Fetch.continueRequest",
            "xhr": "Fetch.failRequest",
            "post": "Fetch.failRequest",
        }
        assert cdp._seen == []  # 정적 파일은 API 관측 목록에 넣지 않는다.
    finally:
        await cdp.close()


async def test_child_target_is_only_resumed_after_interception_is_enabled():
    wire = Wire()
    cdp = ad._Cdp("unused")
    cdp._ws = wire
    cdp._reader = asyncio.create_task(cdp._read_loop())
    try:
        await cdp._configure_child("worker-session")
        methods = [m["method"] for m in wire.sent]
        assert methods.index("Fetch.enable") < methods.index(
            "Runtime.runIfWaitingForDebugger"
        )
        assert all(m["sessionId"] == "worker-session" for m in wire.sent)
        assert cdp._error is None
    finally:
        await cdp.close()


async def test_interception_command_failure_stops_further_navigation():
    class BrokenWire(Wire):
        async def send(self, raw):
            msg = json.loads(raw)
            if msg["method"] == "Fetch.failRequest":
                self.sent.append(msg)
                await self.inbox.put(
                    json.dumps(
                        {"id": msg["id"], "error": {"message": "private detail"}}
                    )
                )
            else:
                await super().send(raw)

    wire = BrokenWire()
    cdp = ad._Cdp("unused")
    cdp._ws = wire
    cdp._reader = asyncio.create_task(cdp._read_loop())
    try:
        await cdp._intercept(
            {
                "sessionId": "page-session",
                "params": {
                    "requestId": "write",
                    "request": {"method": "POST", "url": ad.TOSS_HOME},
                },
            }
        )
        with pytest.raises(ad.TossError, match="읽기 전용 통제"):
            await cdp.send(
                "Page.navigate", {"url": ad.TOSS_HOME}, session="page-session"
            )
        assert "private detail" not in str(cdp._error)
        assert not any(m["method"] == "Page.navigate" for m in wire.sent)
    finally:
        await cdp.close()
