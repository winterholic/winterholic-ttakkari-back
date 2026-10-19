"""게임 빌드 격리 러너 검사(TASK-G01). 실제 컨테이너(OrbStack docker)에서 탈출·비밀·네트워크 음성 사례를 돌린다.

컨테이너 런타임이 없는 환경(CI 등)에서는 런타임 필요 검사만 사유와 함께 skip 한다.
`TTAKKARI_REQUIRE_GAME_RUNTIME=1` 이면 skip 대신 실패시켜 이 Mac 에서 몰래 건너뛰는 일을 막는다.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ttakkari.games import isolation
from ttakkari.games.isolation import (
    IsolatedBuildRunner,
    IsolationError,
    IsolationLimits,
    collect_output,
    probe_runtime,
    stage_source,
)

RUNTIME = probe_runtime()
if not RUNTIME.available and os.environ.get("TTAKKARI_REQUIRE_GAME_RUNTIME") == "1":
    pytest.fail(f"TTAKKARI_REQUIRE_GAME_RUNTIME=1 인데 컨테이너 런타임을 쓸 수 없다: {RUNTIME.reason}", pytrace=False)
requires_runtime = pytest.mark.skipif(not RUNTIME.available, reason=f"컨테이너 런타임 필요(격리 빌드 불가): {RUNTIME.reason}")

SYNTHETIC = "ttk-synthetic-secret-7f3a9c"
BUILD_HTML = "<!doctype html><title>g</title>"


def write_project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def package_json(build: str = "node build.js", **extra) -> str:
    return json.dumps({"name": "g", "version": "0.0.0", "private": True, "scripts": {"build": build}, **extra})


def containers(build_id: str) -> str:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"label=ttakkari.game-build={build_id}", "--format", "{{.Names}}"],
                         capture_output=True, text=True, timeout=30, check=False)
    return out.stdout.strip()


@pytest.fixture
def runner() -> IsolatedBuildRunner:
    return IsolatedBuildRunner(limits=IsolationLimits(install_timeout=120, build_timeout=60))


@pytest.fixture
def work(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


# ---- 런타임이 필요 없는 순수 검사 -------------------------------------------------------------

def test_stage_source_copies_only_safe_regular_files(tmp_path: Path):
    src = tmp_path / "src"
    write_project(src, {
        "package.json": "{}", "src/main.ts": "x", ".env": "SECRET=1", ".env.local": "SECRET=2", ".env.example": "KEY=",
        ".git/hooks/pre-commit": "#!/bin/sh\nevil", "node_modules/a/index.js": "x", "dist/old.js": "x",
    })
    (src / "link").symlink_to("/etc/hostname")
    (src / "dirlink").symlink_to(tmp_path)
    skipped = stage_source(src, tmp_path / "stage", max_bytes=1024 * 1024)
    copied = sorted(p.relative_to(tmp_path / "stage").as_posix() for p in (tmp_path / "stage").rglob("*") if p.is_file())
    assert copied == [".env.example", "package.json", "src/main.ts"]
    joined = " ".join(skipped)
    for needle in (".env (env)", ".env.local (env)", ".git/", "node_modules/", "link (symlink)", "dirlink (symlink)"):
        assert needle in joined


def test_stage_source_enforces_size_cap(tmp_path: Path):
    src = tmp_path / "src"
    write_project(src, {"a.bin": "x" * 2048})
    with pytest.raises(IsolationError) as e:
        stage_source(src, tmp_path / "stage", max_bytes=1024)
    assert e.value.code == "source_too_large"


def test_collect_output_rejects_symlink_and_missing_index(tmp_path: Path):
    dist = tmp_path / "dist"
    write_project(dist, {"index.html": BUILD_HTML, "a.js": "1"})
    assert collect_output(dist, tmp_path / "out", max_bytes=1024 * 1024) == ["a.js", "index.html"]
    (dist / "leak").symlink_to("/etc/hostname")
    with pytest.raises(IsolationError) as e:
        collect_output(dist, tmp_path / "out2", max_bytes=1024 * 1024)
    assert e.value.code == "output_symlink"
    (dist / "leak").unlink()
    (dist / "index.html").unlink()
    with pytest.raises(IsolationError) as e:
        collect_output(dist, tmp_path / "out3", max_bytes=1024 * 1024)
    assert e.value.code == "no_index"


def test_collect_output_enforces_size_cap(tmp_path: Path):
    dist = tmp_path / "dist"
    write_project(dist, {"index.html": BUILD_HTML, "big.bin": "x" * 4096})
    with pytest.raises(IsolationError) as e:
        collect_output(dist, tmp_path / "out", max_bytes=1024)
    assert e.value.code == "output_too_large"


def test_probe_runtime_reports_missing_runtime():
    status = probe_runtime("definitely-not-a-container-runtime")
    assert not status.available and "PATH" in status.reason


async def test_ids_and_paths_are_validated(work: Path, tmp_path: Path):
    r = IsolatedBuildRunner()
    for bad in ("../x", "a b", "", "a" * 41, "-x"):
        with pytest.raises(IsolationError) as e:
            await r.build_project(tmp_path, work, bad)
        assert e.value.code == "bad_build_id"
    with pytest.raises(IsolationError) as e:
        isolation._check_mount_path(Path("/tmp/a,b"))
    assert e.value.code == "bad_path"


# ---- 컨테이너 격리 --------------------------------------------------------------------------

PROBE_JS = r"""
const fs = require('fs'), os = require('os'), net = require('net'), dns = require('dns');
const cfg = JSON.parse(fs.readFileSync('/work/probe-config.json', 'utf8'));
const report = {};
const tryRead = (p) => { try { fs.statSync(p); return true; } catch (e) { return false; } };
report.visible_host_paths = cfg.host_paths.filter(tryRead);
report.uid = process.getuid();
report.gid = process.getgid();
const write = (p) => { try { fs.writeFileSync(p, 'x'); return true; } catch (e) { return false; } };
report.writable = { etc: write('/etc/ttk'), usr: write('/usr/ttk'), root: write('/ttk'), tmp: write('/tmp/ttk'), work: write('/work/ttk-ok') };
report.cap_eff = (fs.readFileSync('/proc/self/status', 'utf8').match(/CapEff:\s*(\w+)/) || [])[1];
report.no_new_privs = (fs.readFileSync('/proc/self/status', 'utf8').match(/NoNewPrivs:\s*(\d)/) || [])[1];
report.sockets = ['/var/run/docker.sock', '/run/docker.sock'].filter(tryRead);
const envBlob = Object.entries(process.env).map(([k, v]) => k + '=' + v).join('\n')
  + (() => { try { return fs.readFileSync('/proc/1/environ', 'utf8'); } catch (e) { return ''; } })();
report.env_has_host_secret = envBlob.includes(cfg.token);
report.env_keys = Object.keys(process.env).sort();
report.control_file_found = tryRead('/work/tokens/control.txt') && fs.readFileSync('/work/tokens/control.txt', 'utf8').includes(cfg.token);
report.dotenv_visible = tryRead('/work/.env');
report.git_visible = tryRead('/work/.git');
report.symlink_visible = tryRead('/work/leak');
report.interfaces = Object.keys(os.networkInterfaces());
report.mounts = fs.readFileSync('/proc/self/mountinfo', 'utf8').split('\n').filter(Boolean).map((l) => l.split(' ')[4]);
const tcp = (host, port) => new Promise((res) => {
  const s = net.connect({ host, port, timeout: 3000 });
  s.on('connect', () => { s.destroy(); res(true); });
  s.on('error', () => res(false));
  s.on('timeout', () => { s.destroy(); res(false); });
});
const lookup = (h) => new Promise((res) => dns.lookup(h, (e) => res(!e)));
(async () => {
  report.net = {
    registry_dns: await lookup('registry.npmjs.org'),
    registry_tcp: await tcp('104.16.0.35', 443),
    host_internal_tcp: await tcp('host.docker.internal', 5432),
    host_ip_tcp: await tcp('0.250.250.254', 8787),
    cloudflare_tcp: await tcp('1.1.1.1', 443),
  };
  fs.mkdirSync('dist', { recursive: true });
  fs.writeFileSync('dist/index.html', '<!doctype html><title>probe</title>');
  fs.writeFileSync('dist/report.json', JSON.stringify(report));
})();
"""


def probe_project(root: Path, host_secret_dir: Path) -> Path:
    host_secret_dir.mkdir()
    (host_secret_dir / "secret.txt").write_text(SYNTHETIC)
    write_project(root, {
        "package.json": package_json("node build.js"),
        "build.js": PROBE_JS,
        "tokens/control.txt": SYNTHETIC,
        ".env": f"TOKEN={SYNTHETIC}",
        ".git/config": "[core]",
        "probe-config.json": json.dumps({
            "token": SYNTHETIC,
            "host_paths": [str(host_secret_dir), str(host_secret_dir / "secret.txt"), str(Path.home() / ".ssh"),
                           str(Path.home() / ".ttakkari"), str(Path.home() / "development"), str(root.parent), "/Users", "/private"],
        }),
    })
    (root / "leak").symlink_to(host_secret_dir / "secret.txt")
    return root


@requires_runtime
async def test_hostile_build_cannot_escape_read_secrets_or_use_network(runner, work, tmp_path, monkeypatch):
    monkeypatch.setenv("TTK_SYNTHETIC_SECRET", SYNTHETIC)
    monkeypatch.setenv("TTAKKARI_JWT_SECRET", SYNTHETIC)
    project = probe_project(tmp_path / "proj", tmp_path / "host-secret")
    res = await runner.build_project(project, work, "probe1")
    assert res.success, res.log
    report = json.loads((res.output_dir / "report.json").read_text())

    # 파일: 호스트 경로·비밀 디렉터리·홈은 컨테이너에서 보이지 않는다.
    assert report["visible_host_paths"] == []
    assert not any(m.startswith(("/Users", "/private", "/Volumes", "/mnt/mac")) for m in report["mounts"])
    # 환경: 호스트 환경변수(합성 토큰 포함)는 전달되지 않는다.
    assert report["env_has_host_secret"] is False
    assert not any(k.startswith("TTAKKARI_") or k == "TTK_SYNTHETIC_SECRET" for k in report["env_keys"])
    # 대조: 프로젝트 안에 둔 같은 토큰은 읽힌다(탐침이 실제로 읽을 수 있는 코드임을 보인다).
    assert report["control_file_found"] is True
    # 스테이징 복사본에는 .env·.git·심볼릭 링크가 없다.
    assert (report["dotenv_visible"], report["git_visible"], report["symlink_visible"]) == (False, False, False)
    assert {".env (env)", ".git/", "leak (symlink)"} <= set(res.skipped_entries)
    # 권한: 비root, 읽기 전용 루트, capability 없음, docker 소켓 없음.
    assert report["uid"] == os.getuid() != 0
    assert report["writable"] == {"etc": False, "usr": False, "root": False, "tmp": True, "work": True}
    assert int(report["cap_eff"], 16) == 0 and report["no_new_privs"] == "1"
    assert report["sockets"] == []
    # 네트워크: 빌드 단계는 loopback 외 인터페이스가 없고 호스트·인터넷·DNS 모두 막힌다.
    assert report["interfaces"] == ["lo"]
    assert report["net"] == {"registry_dns": False, "registry_tcp": False, "host_internal_tcp": False, "host_ip_tcp": False,
                              "cloudflare_tcp": False}


@requires_runtime
async def test_build_does_not_modify_original_project_or_leave_containers(runner, work, tmp_path):
    project = write_project(tmp_path / "proj", {
        "package.json": package_json("node build.js"),
        "build.js": "const fs=require('fs');fs.writeFileSync('package.json','tampered');fs.writeFileSync('planted.txt','x');"
                    "fs.mkdirSync('dist');fs.writeFileSync('dist/index.html','<title>x</title>');",
        ".git/HEAD": "ref: refs/heads/main",
    })
    before = (project / "package.json").read_text()
    res = await runner.build_project(project, work, "orig1")
    assert res.success, res.log
    assert (project / "package.json").read_text() == before
    assert not (project / "planted.txt").exists() and not (project / "node_modules").exists() and not (project / "dist").exists()
    assert not (work / "orig1" / "src").exists()  # 스테이징 정리
    assert containers("orig1") == ""
    with pytest.raises(IsolationError) as e:  # 같은 빌드 ID 로 이전 산출물을 덮어쓰지 못한다.
        await runner.build_project(project, work, "orig1")
    assert e.value.code == "build_exists"


@requires_runtime
async def test_install_scripts_never_run_and_failed_build_is_reported(runner, work, tmp_path):
    postinstall = "node -e \"require('fs').writeFileSync('/work/POSTINSTALL_RAN','1')\""
    project = write_project(tmp_path / "proj", {
        "package.json": json.dumps({"name": "g", "version": "0.0.0", "private": True, "dependencies": {"evil": "file:./evil-pkg"},
                                    "scripts": {"build": "node build.js"}}),
        "evil-pkg/package.json": json.dumps({"name": "evil", "version": "1.0.0", "scripts": {"postinstall": postinstall}}),
        "evil-pkg/index.js": "",
        "build.js": "const fs=require('fs');fs.mkdirSync('dist');"
                    "fs.writeFileSync('dist/index.html',fs.existsSync('POSTINSTALL_RAN')?'RAN':'CLEAN');",
    })
    res = await runner.build_project(project, work, "scripts1")
    assert res.success, res.log
    assert (res.output_dir / "index.html").read_text() == "CLEAN"

    failing = write_project(tmp_path / "bad", {"package.json": package_json("node -e \"process.exit(3)\"")})
    res = await runner.build_project(failing, work, "failing1")
    assert not res.success and res.output_dir is None
    assert "빌드 실패(exit 3)" in res.error
    assert not (work / "failing1" / "out").exists()

    linky = write_project(tmp_path / "linky", {
        "package.json": package_json("node build.js"),
        "build.js": "const fs=require('fs');fs.mkdirSync('dist');fs.writeFileSync('dist/index.html','x');fs.symlinkSync('/etc/hostname','dist/leak');",
    })
    res = await runner.build_project(linky, work, "linky1")
    assert not res.success and "심볼릭 링크" in res.error


@requires_runtime
async def test_install_egress_only_reaches_allowlisted_registry(runner, work, tmp_path):
    stage = write_project(tmp_path / "stage", {"probe.js": r"""
const net = require('net');
const tcp = (host, port) => new Promise((res) => { const s = net.connect({host, port, timeout: 3000});
  s.on('connect', () => { s.destroy(); res(true); }); s.on('error', () => res(false)); s.on('timeout', () => { s.destroy(); res(false); }); });
const connect = (target) => new Promise((res) => { const s = net.connect({host: 'registry-proxy', port: 3128, timeout: 8000});
  let buf = ''; s.on('connect', () => s.write('CONNECT ' + target + ' HTTP/1.1\r\nHost: ' + target + '\r\n\r\n'));
  s.on('data', (d) => { buf += d; if (buf.includes('\r\n')) { s.destroy(); res(buf.split('\r\n')[0]); } });
  s.on('error', (e) => res('error')); s.on('timeout', () => { s.destroy(); res('timeout'); }); });
(async () => {
  console.log('RESULT ' + JSON.stringify({
    direct_host: await tcp('host.docker.internal', 5432), direct_internet: await tcp('1.1.1.1', 443),
    proxy_host: await connect('host.docker.internal:5432'), proxy_ip: await connect('1.1.1.1:443'),
    proxy_lan: await connect('192.168.1.1:443'), proxy_loopback: await connect('127.0.0.1:8787'),
    proxy_wrong_port: await connect('registry.npmjs.org:80'), proxy_other_host: await connect('example.com:443'),
    proxy_registry: await connect('registry.npmjs.org:443'),
  }));
})();
"""})
    res = await runner.run_stage(stage, ["node", "probe.js"], stage="probe", build_id="egress1", network="egress", timeout=90)
    assert res.ok, res.log
    got = json.loads(res.log.split("RESULT ", 1)[1].strip().splitlines()[0])
    assert got["direct_host"] is False and got["direct_internet"] is False
    for k in ("proxy_host", "proxy_ip", "proxy_lan", "proxy_loopback", "proxy_wrong_port", "proxy_other_host"):
        assert " 403 " in got[k], (k, got[k])
    if " 502 " in got["proxy_registry"]:
        pytest.skip("npm 레지스트리에 연결할 수 없는 환경(허용 경로 양성 대조 불가)")
    assert " 200 " in got["proxy_registry"], got["proxy_registry"]
    assert containers("egress1") == ""  # 프록시 컨테이너와 네트워크 정리
    _, nets = await runner._docker("network", "ls", "--filter", "name=ttk-game-egress1", "--format", "{{.Name}}")
    assert nets.strip() == ""


@requires_runtime
async def test_timeout_memory_pids_and_file_size_limits(work, tmp_path):
    stage = write_project(tmp_path / "stage", {"x": "1"})
    small = IsolatedBuildRunner(limits=IsolationLimits(memory_mb=128, pids=32, file_size_mb=8, tmp_mb=32))

    res = await small.run_stage(stage, ["sleep", "60"], stage="hang", build_id="limits1", network="none", timeout=3)
    assert res.timed_out and not res.ok
    assert containers("limits1") == ""  # 시간 초과 컨테이너는 제거된다

    mem = await small.run_stage(stage, ["node", "-e", "const a=[];for(;;)a.push(Buffer.alloc(16*1024*1024,1))"],
                                stage="mem", build_id="limits2", network="none", timeout=60)
    assert mem.exit_code not in (0, None), mem.log  # OOM kill(137) 또는 V8 힙 오류

    pids = await small.run_stage(stage, ["node", "-e", """
const cp = require('child_process'); let failed = 0, started = 0;
for (let i = 0; i < 100; i++) { const c = cp.spawn('sleep', ['5']); c.on('error', () => {}); if (c.pid) started++; else failed++; }
console.log('PIDS ' + started + ' ' + failed); setTimeout(() => process.exit(0), 200);"""],
                             stage="pids", build_id="limits3", network="none", timeout=60)
    started, failed = map(int, pids.log.split("PIDS ", 1)[1].split()[:2])
    assert failed > 0 and started < 100, pids.log

    await small.run_stage(stage, ["sh", "-c", "dd if=/dev/zero of=/work/big bs=1M count=64 2>&1; echo EXIT=$?"],
                                stage="fsize", build_id="limits4", network="none", timeout=60)
    assert (stage / "big").stat().st_size <= 8 * 1024 * 1024
