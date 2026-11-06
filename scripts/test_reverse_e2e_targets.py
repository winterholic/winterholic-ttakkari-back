#!/usr/bin/env python3
"""Process-level checks for the independent local reverse E2E targets."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT = Path(__file__).with_name("reverse_e2e_targets.py")
PYTHON = Path(__file__).resolve().parents[1] / "../back/.venv/bin/python"


class ReverseE2ETargetProcessTests(unittest.TestCase):
    def start_target(self, kind: str, directory: Path):
        completed = subprocess.run(
            [str(PYTHON), str(SCRIPT), "start", kind, "--port", "0", "--state-dir", str(directory)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        metadata = json.loads(completed.stdout)
        self.assertTrue(metadata["ready"])
        self.assertEqual(metadata["environment"], "local-loopback")
        self.assertEqual(urlsplit(metadata["url"]).hostname, "127.0.0.1")
        return metadata

    def stop_target(self, metadata):
        result = subprocess.run(
            [str(PYTHON), str(SCRIPT), "stop", "--pid-file", metadata["pid_file"]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["stopped"])

    def request(self, port: int, method: str, path: str, body: bytes | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_api_target_http_contract_and_clean_state(self):
        with tempfile.TemporaryDirectory(prefix="reverse-api-target-") as temp:
            metadata = self.start_target("api", Path(temp))
            try:
                port = metadata["port"]
                status, body = self.request(port, "GET", "/state")
                self.assertEqual((status, json.loads(body)), (200, {"count": 0}))

                status, body = self.request(port, "GET", "/items")
                self.assertEqual((status, json.loads(body)), (200, []))

                status, body = self.request(port, "POST", "/items", b'{"value":"alpha"}')
                self.assertEqual(status, 201)
                self.assertEqual(json.loads(body), {"accepted": True, "value": "alpha", "count": 1})

                status, body = self.request(port, "GET", "/state")
                self.assertEqual((status, json.loads(body)), (200, {"count": 1}))

                status, body = self.request(port, "GET", "/large")
                self.assertEqual(status, 200)
                self.assertEqual(len(json.loads(body)["payload"]), 8192)

                status, body = self.request(port, "GET", "/missing")
                self.assertEqual((status, json.loads(body)), (404, {"error": "not_found"}))

                status, body = self.request(port, "POST", "/items", b'{bad')
                self.assertEqual((status, json.loads(body)), (400, {"error": "invalid_json"}))

                status, body = self.request(port, "POST", "/items", b'{}')
                self.assertEqual((status, json.loads(body)), (422, {"error": "value_required"}))

                status, body = self.request(port, "POST", "/items", b'[]')
                self.assertEqual((status, json.loads(body)), (422, {"error": "value_required"}))

                status, body = self.request(port, "GET", "/items")
                self.assertEqual((status, json.loads(body)), (200, [{"value": "alpha"}]))
            finally:
                self.stop_target(metadata)

    def test_web_target_is_standalone_loopback_page(self):
        with tempfile.TemporaryDirectory(prefix="reverse-web-target-") as temp:
            metadata = self.start_target("web", Path(temp))
            try:
                status, body = self.request(metadata["port"], "GET", "/")
                page = body.decode("utf-8")
                self.assertEqual(status, 200)
                for feature in ('type="number"', "required", "aria-live", "@media", "390px", "addEventListener"):
                    self.assertIn(feature, page)
            finally:
                self.stop_target(metadata)

    def test_stop_refuses_unowned_pid_metadata(self):
        with tempfile.TemporaryDirectory(prefix="reverse-stop-owner-") as temp:
            pid_file = Path(temp) / "not-owned.json"
            pid_file.write_text(json.dumps({"pid": os.getpid(), "owner": "not-this-target"}), encoding="utf-8")
            result = subprocess.run(
                [str(PYTHON), str(SCRIPT), "stop", "--pid-file", str(pid_file)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ownership", result.stderr.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
