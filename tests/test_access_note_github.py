"""샌드박스에서 실패하는 `gh auth switch` 대신 명령별 계정 지정 방식을 지침에 싣고, 그 명령이 실제로 동작하는지 확인한다."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from ttakkari.runs import instructions


def _fake_gh(tmp_path: Path) -> Path:
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\n'
                  'if [ "$1 $2" = "auth token" ]; then\n'
                  '  while [ $# -gt 0 ]; do [ "$1" = "--user" ] && { echo "tok-$2"; exit 0; }; shift; done\n'
                  'fi\n'
                  'echo "gh used token=$GH_TOKEN args=$*"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return gh


def test_access_note_warns_against_gh_auth_switch_and_shows_per_command_form(tmp_path):
    note = instructions.access_note(tmp_path)
    assert "never run `gh auth switch`" in note and "exit 161" in note
    assert instructions.GH_AS_GH in note and instructions.GH_AS_GIT in note
    assert "github_switch_account" in note and "github_accounts" in note


def test_per_command_gh_form_selects_the_account_without_switching(tmp_path):
    _fake_gh(tmp_path)
    cmd = instructions.GH_AS_GH.replace("LOGIN", "daeseon-dev").replace("ARGS", "repo view")
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=True,
                         env={"PATH": f"{tmp_path}:/usr/bin:/bin"}).stdout
    assert out.strip() == "gh used token=tok-daeseon-dev args=repo view"


def test_git_credential_helper_hands_git_the_selected_accounts_token(tmp_path):
    helper = instructions.GH_AS_GIT_HELPER.removeprefix("!")
    out = subprocess.run(["sh", "-c", helper], capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], "GH_AS_USER": "daeseon-dev", "GH_AS_TOKEN": "tok-x"}).stdout
    assert out.splitlines() == ["username=daeseon-dev", "password=tok-x"]


def test_real_git_uses_the_helper_for_an_http_remote(tmp_path):
    """git 이 이 helper 를 실제로 호출해 자격을 얻는지: 로컬 HTTP 서버가 받은 Authorization 을 확인한다."""
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            auth = self.headers.get("Authorization")
            if auth is None:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
                self.end_headers()
                return
            seen.append(base64.b64decode(auth.split()[1]).decode())
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _fake_gh(tmp_path)
        cmd = (instructions.GH_AS_GIT.replace("LOGIN", "daeseon-dev").replace("pull|fetch|push ARGS",
                                                                          f"ls-remote http://127.0.0.1:{server.server_port}/o/r.git"))
        subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False, timeout=30,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0",
                            "GIT_CONFIG_NOSYSTEM": "1"})
    finally:
        server.shutdown()
    assert seen == ["daeseon-dev:tok-daeseon-dev"]
