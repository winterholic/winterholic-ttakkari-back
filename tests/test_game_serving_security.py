"""별도 게임 origin 서빙: 자격증명 없는 정적 제공, CSP/CORS/쿠키, 경로 이탈, 토큰 범위, Host 분리 (TASK-G07)."""

import os
import socket
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from test_api_games import fake_adapter, make_repo, register, wait_build  # noqa: F401
from test_game_build_snapshots import (  # noqa: F401
    _cleanup_snapshots,
    manifest,
    requires_runtime,
)

from ttakkari.config import get_settings
from ttakkari.games import builds, origin
from ttakkari.games.adapter import PhaserAdapter
from ttakkari.games.builds import bundle_path
from ttakkari.games.contract import game_dir
from ttakkari.main import create_app
from ttakkari.models import GameBuild
from ttakkari.security.auth import issue_access_token

pytestmark = [pytest.mark.security]


GAME_HOST = "play.games.test"
PWA = "http://localhost:5173"


@pytest.fixture
def game_origin(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "game_origin_host", GAME_HOST)
    monkeypatch.setattr(s, "game_origin_url", f"http://{GAME_HOST}")
    return s


@pytest.fixture
async def games_client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=origin.GameOriginApp()), base_url=f"http://{GAME_HOST}") as c:
        yield c


async def built_game(client, auth, slug: str) -> tuple[str, dict]:
    make_repo(slug)
    game = (await register(client, auth, slug)).json()
    assert (await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)).status_code == 200
    b = (await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)).json()
    done = await wait_build(client, auth, game["id"], b["id"])
    assert done["status"] == "succeeded", done
    return game["id"], done


async def play(client, auth, game_id: str, **body):
    return await client.post(f"/api/games/{game_id}/play", json=body, headers={**auth, "Origin": PWA})


def token_of(url: str) -> str:
    return url.split("/p/")[1].split("/")[0]


# ---- 토큰 발급 API ---------------------------------------------------------------------------------


async def test_play_requires_auth_and_configured_origin(client, auth, fake_adapter):  # noqa: F811
    gid = uuid.uuid4()
    assert (await client.post(f"/api/games/{gid}/play", json={})).status_code == 401
    assert (await client.post(f"/api/games/{gid}/play", json={}, headers={"Authorization": "Bearer nope"})).status_code == 401
    game_id, _ = await built_game(client, auth, "serve-cfg")
    r = await play(client, auth, game_id)
    assert r.status_code == 503 and r.json()["code"] == "origin_unconfigured"  # 설정이 없으면 어떤 origin 으로도 열지 않는다


async def test_origin_must_differ_from_api_and_pwa(game_origin, monkeypatch):
    assert origin.origin_problem() is None
    monkeypatch.setattr(game_origin, "game_origin_host", "localhost:5173")
    monkeypatch.setattr(game_origin, "game_origin_url", PWA)
    assert "같습니다" in origin.origin_problem()
    monkeypatch.setattr(game_origin, "game_origin_host", GAME_HOST)
    monkeypatch.setattr(game_origin, "game_origin_url", "http://other.test")
    assert "다릅니다" in origin.origin_problem()
    monkeypatch.setattr(game_origin, "game_origin_url", f"http://{GAME_HOST}/sub")
    assert "경로 없는" in origin.origin_problem()


async def test_play_issues_scoped_url_and_rejects_bad_builds(client, auth, game_origin, fake_adapter):  # noqa: F811
    game_id, build = await built_game(client, auth, "serve-play")
    other_id, other_build = await built_game(client, auth, "serve-other")
    r = await play(client, auth, game_id)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["origin"] == f"http://{GAME_HOST}" and out["build_id"] == build["id"] and out["bundle_sha256"] == build["bundle_sha256"]
    assert out["url"].startswith(f"http://{GAME_HOST}/p/") and f"#ttk-parent={PWA.replace(':', '%3A').replace('/', '%2F')}" in out["url"]
    assert "allow-same-origin" not in out["sandbox"] and "allow-scripts" in out["sandbox"]
    assert out["frame_ancestors"] == [PWA]
    claims = jwt.decode(token_of(out["url"]), get_settings().jwt_secret, algorithms=["HS256"], audience="game-play", issuer="ttakkari")
    assert claims["sub"] == build["id"] and claims["bp"].endswith("/bundle") and "cookie" not in claims
    assert (await play(client, auth, game_id, build_id=other_build["id"])).status_code == 404  # 다른 게임의 빌드
    assert (await play(client, auth, game_id, build_id=str(uuid.uuid4()))).status_code == 404
    assert (await client.post(f"/api/games/{game_id}/play", json={"x": 1}, headers=auth)).status_code == 422
    # 호출자 Origin 이 허용 목록 밖이면 부모 origin 힌트를 넣지 않는다
    r = await client.post(f"/api/games/{game_id}/play", json={}, headers={**auth, "Origin": "https://evil.test"})
    assert "ttk-parent" not in r.json()["url"]
    # 변조된 스냅샷은 열어 주지 않는다
    from ttakkari.db import sessionmaker
    async with sessionmaker()() as db:
        b = await db.get(GameBuild, uuid.UUID(build["id"]))
        bundle = bundle_path(b)
        builds._thaw(bundle)
        (bundle / "index.html").write_text("hacked")
    r = await play(client, auth, game_id)
    assert r.status_code == 409 and r.json()["code"] == "snapshot_tampered"
    assert other_id != game_id


# ---- 게임 origin 응답 ------------------------------------------------------------------------------


async def test_serves_bundle_without_credentials_and_with_hardened_headers(client, auth, game_origin, fake_adapter, games_client):  # noqa: F811
    game_id, _ = await built_game(client, auth, "serve-ok")
    url = (await play(client, auth, game_id)).json()["url"].split("#")[0]
    path = url.removeprefix(f"http://{GAME_HOST}")
    for hdrs in ({}, {"Authorization": auth["Authorization"], "Cookie": "ttk_refresh=SECRET-REFRESH; sid=abc"}):
        r = await games_client.get(path, headers=hdrs)
        assert r.status_code == 200 and "<title>fake</title>" in r.text
        assert r.headers["content-type"].startswith("text/html")
        assert "set-cookie" not in r.headers and "access-control-allow-credentials" not in r.headers
        assert "SECRET-REFRESH" not in r.text and auth["Authorization"] not in r.text
        assert r.headers["access-control-allow-origin"] == "*" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer" and "no-store" not in r.headers["cache-control"]
        csp = r.headers["content-security-policy"]
        for needle in ("default-src 'none'", "connect-src 'self' data: blob:", "form-action 'none'", "base-uri 'none'", "object-src 'none'",
                       f"frame-ancestors {PWA}", "sandbox allow-scripts allow-pointer-lock"):
            assert needle in csp, needle
        assert "allow-same-origin" not in csp and "script-src 'self' 'wasm-unsafe-eval';" in csp and "'unsafe-eval'" not in csp
    js = await games_client.get(path.rsplit("/", 1)[0] + "/assets/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript") and js.text == "console.log(1)"
    assert (await games_client.get(path.rsplit("/", 1)[0] + "/")).status_code == 200  # 디렉터리 요청은 index.html
    head = await games_client.head(path)
    assert head.status_code == 200 and head.content == b""
    rng = await games_client.get(path.rsplit("/", 1)[0] + "/assets/app.js", headers={"Range": "bytes=0-6"})
    assert rng.status_code == 206 and rng.text == "console"
    assert (await games_client.post(path, content=b"x")).status_code == 405
    pre = await games_client.options(path, headers={"Origin": "null", "Access-Control-Request-Method": "GET"})
    assert pre.status_code == 204 and "access-control-allow-credentials" not in pre.headers


async def test_token_scope_expiry_and_audience(client, auth, game_origin, fake_adapter, games_client):  # noqa: F811
    game_id, build = await built_game(client, auth, "serve-tok")
    url = (await play(client, auth, game_id)).json()["url"].split("#")[0]
    good = token_of(url)
    secret = get_settings().jwt_secret
    api_token = auth["Authorization"].split()[1]
    assert (await games_client.get(f"/p/{api_token}/index.html")).status_code == 403  # API bearer 는 게임 토큰이 아니다
    base = {"iss": "ttakkari", "aud": "game-play", "sub": build["id"], "gid": game_id, "bp": jwt.decode(good, options={"verify_signature": False})["bp"]}
    expired = jwt.encode({**base, "exp": datetime.now(UTC) - timedelta(seconds=5)}, secret, algorithm="HS256")
    wrong_aud = jwt.encode({**base, "aud": "download", "exp": datetime.now(UTC) + timedelta(minutes=5)}, secret, algorithm="HS256")
    wrong_key = jwt.encode({**base, "exp": datetime.now(UTC) + timedelta(minutes=5)}, "x" * 40, algorithm="HS256")
    for bad in (expired, wrong_aud, wrong_key, "garbage", good[:-3] + "AAA", ""):
        r = await games_client.get(f"/p/{bad}/index.html")
        assert r.status_code in (403, 404), bad
        assert r.status_code == 403 or bad == ""
        assert "fake" not in r.text
    # 토큰의 번들 경로는 game_builds_dir 안이어야 한다(서명 키 유출을 가정해도 다른 곳을 가리킬 수 없다)
    for bp in ("../../etc", "/etc", ".."):
        forged = jwt.encode({**base, "bp": bp, "exp": datetime.now(UTC) + timedelta(minutes=5)}, secret, algorithm="HS256")
        assert (await games_client.get(f"/p/{forged}/index.html")).status_code == 404
    # 반대로 게임 토큰으로 따까리 API 를 부를 수 없다
    r = await client.get("/api/games", headers={"Authorization": f"Bearer {good}"})
    assert r.status_code == 401
    r = await client.get("/api/artifacts", headers={"Authorization": f"Bearer {good}"})
    assert r.status_code == 401
    assert issue_access_token()[0] != good


async def test_path_traversal_hidden_files_and_symlinks_are_rejected(client, auth, game_origin, fake_adapter, games_client, tmp_path: Path):  # noqa: F811
    game_id, build = await built_game(client, auth, "serve-path")
    url = (await play(client, auth, game_id)).json()["url"].split("#")[0]
    base = url.removeprefix(f"http://{GAME_HOST}").rsplit("/", 1)[0]
    secret_file = get_settings().game_builds_dir / "secret.txt"
    secret_file.write_text("TOP-SECRET")
    try:
        for rel in ("../../../secret.txt", "..%2f..%2f..%2fsecret.txt", "%2e%2e/%2e%2e/%2e%2e/secret.txt", "assets/../../../../secret.txt",
                    "..\\..\\secret.txt", "%00index.html", "index.html%00", "//etc/passwd", "assets//app.js", ".git/config", "assets/.env"):
            r = await games_client.get(f"{base}/{rel}")
            assert r.status_code in (400, 403, 404), (rel, r.status_code)
            assert "TOP-SECRET" not in r.text
    finally:
        secret_file.unlink()
    assert (await games_client.get(f"{base}/missing.js")).status_code == 404
    assert (await games_client.get("/")).status_code == 404 and (await games_client.get("/api/games")).status_code == 404
    assert (await games_client.get("/p/")).status_code in (403, 404)
    # 심볼릭 링크 거부(번들 밖 파일, 번들 안 디렉터리 링크 모두)
    bundle = tmp_path / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("ok")
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    (bundle / "link.txt").symlink_to(outside)
    (bundle / "linkdir").symlink_to(bundle / "assets")
    (bundle / "assets" / "a.js").write_text("1")
    assert origin.resolve_file(bundle, "index.html") == (bundle / "index.html").resolve()
    for rel in ("link.txt", "linkdir/a.js", "../outside.txt", "assets", "nope", ".hidden", "a/../index.html", "./index.html", "assets/./a.js"):
        assert origin.resolve_file(bundle, rel) is None, rel
    assert build["bundle_sha256"]


# ---- Host 분리 --------------------------------------------------------------------------------------


async def test_host_router_isolates_game_host_from_api_routes(client, auth, game_origin, fake_adapter):  # noqa: F811
    game_id, _ = await built_game(client, auth, "serve-host")
    url = (await play(client, auth, game_id)).json()["url"].split("#")[0]
    path = url.removeprefix(f"http://{GAME_HOST}")
    routed = origin.GameOriginMiddleware(create_app(), GAME_HOST)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=routed), base_url="http://api.test") as c:
        # 게임 host 로는 게임 앱만 닿는다. API 라우트는 인증 헤더가 있어도 존재하지 않는다.
        for p in ("/api/games", "/api/sessions", "/api/auth/refresh", "/api/health", "/docs", "/api/artifacts"):
            for method in ("GET", "POST"):
                r = await c.request(method, p, headers={"Host": GAME_HOST, **auth, "Cookie": "ttk_refresh=SECRET"})
                assert r.status_code in (404, 405) and r.text in ("not found", "method not allowed"), (method, p, r.status_code, r.text[:60])
                assert "set-cookie" not in r.headers and "access-control-allow-credentials" not in r.headers
        assert (await c.get(path, headers={"Host": GAME_HOST})).status_code == 200
        assert (await c.get(path, headers={"Host": f"{GAME_HOST}:8080"})).status_code == 200  # 포트가 설정에 없으면 포트는 무시
        # API host 로는 게임 경로가 없다(같은 origin 으로 게임을 서빙할 수 없다)
        r = await c.get(path, headers={"Host": "api.test"})
        assert r.status_code == 404 and "fake" not in r.text
        assert (await c.get("/api/games", headers={"Host": "api.test", **auth})).status_code == 200
        assert (await c.get("/api/games", headers={"Host": "api.test"})).status_code == 401
        assert (await c.get("/api/games", headers={"Host": f"x{GAME_HOST}", **auth})).status_code == 200  # 접미사만 같은 host 는 API 로 간다


async def test_create_app_installs_host_router_only_when_configured(game_origin, monkeypatch):
    assert any(m.cls is origin.GameOriginMiddleware for m in create_app().user_middleware)
    monkeypatch.setattr(game_origin, "game_origin_host", "")
    assert not any(m.cls is origin.GameOriginMiddleware for m in create_app().user_middleware)


def test_access_log_filter_redacts_play_tokens():
    import logging

    f = origin.PlayTokenAccessFilter()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/p/SECRET.TOKEN.PART/assets/a.js", "1.1", 200), None)
    assert f.filter(rec) and "SECRET" not in rec.getMessage() and "/p/-/assets/a.js" in rec.getMessage()


def test_origin_module_never_touches_db_or_user_credentials():
    src = Path(origin.__file__).read_text()
    for forbidden in ("sessionmaker", "get_session", "require_user", "set_cookie", "request.cookies", "authorization", "Authorization"):
        assert forbidden not in src, forbidden
    assert os.path.exists(origin.__file__)


# ---- 실제 Phaser 빌드를 별도 프로세스·포트의 게임 origin 으로 ------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@requires_runtime
async def test_real_phaser_build_served_by_separate_game_origin_process(client, auth, game_origin):
    root = game_dir("serve-real")
    await PhaserAdapter().scaffold_project(root, manifest("serve-real"))
    game = (await register(client, auth, "serve-real")).json()
    await client.post(f"/api/games/{game['id']}/lock/approve", headers=auth)
    started = await client.post(f"/api/games/{game['id']}/builds", json={}, headers=auth)
    done = await wait_build(client, auth, game["id"], started.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    url = (await play(client, auth, game["id"])).json()["url"].split("#")[0]
    port = _free_port()
    # 별도 프로세스는 DB 설정 없이 번들 디렉터리와 서명 키만 받는다. 따까리 refresh 쿠키·bearer 는 환경에 없다.
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "TTAKKARI_HOME", "TTAKKARI_JWT_SECRET", "TTAKKARI_NO_ENV_FILE",
                                                         "TTAKKARI_ENV", "TTAKKARI_CORS_ORIGINS", "TTAKKARI_ALLOWED_ROOTS"}}
    env["TTAKKARI_DATABASE_URL"] = "postgresql+asyncpg://localhost/does_not_exist"  # 게임 origin 은 DB 를 쓰지 않는다
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "ttakkari.games.origin:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        base = f"http://127.0.0.1:{port}"
        path = url.removeprefix(f"http://{GAME_HOST}")
        async with httpx.AsyncClient(base_url=base, timeout=10) as c:
            for _ in range(100):
                try:
                    r = await c.get(path, headers={"Cookie": "ttk_refresh=SECRET", **auth})
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise AssertionError(proc.stderr.read().decode())
            assert r.status_code == 200 and "<div id=\"game\">" in r.text and "set-cookie" not in r.headers
            assert "script-src 'self'" in r.headers["content-security-policy"]
            import re
            js = re.search(r'src="(\./)?(assets/[^"]+\.js)"', r.text)
            assert js, r.text
            bundle_js = await c.get(path.rsplit("/", 1)[0] + "/" + js.group(2))
            assert bundle_js.status_code == 200 and bundle_js.headers["content-type"].startswith("text/javascript") and len(bundle_js.content) > 500_000
            assert "set-cookie" not in bundle_js.headers and "access-control-allow-credentials" not in bundle_js.headers
            assert (await c.get("/api/games", headers=auth)).status_code == 404  # 이 origin 에는 따까리 API 가 없다
            assert (await c.get(path.replace("/p/", "/p/x"))).status_code == 403
    finally:
        proc.terminate()
        proc.wait(10)
