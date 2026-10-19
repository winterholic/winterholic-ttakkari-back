"""게임 프로젝트 빌드 격리 러너 (TASK-G01, docs/design/game-studio.md §6.2, ADR 0046).

신뢰할 수 없는 package.json 스크립트와 빌드 도구를 호스트가 아니라 OrbStack(docker) 컨테이너 안에서만 실행한다.

- 컨테이너에는 소스 **복사본**(스테이징)만 마운트한다. `.git`·`.env*`·심볼릭 링크·node_modules 는 복사하지 않는다.
  원본 저장소를 마운트하면 빌드 스크립트가 `.git/hooks` 를 심어 다음 호스트 git 작업에서 실행될 수 있다.
- 홈·따까리 데이터·비밀·docker 소켓은 마운트하지 않고, 호스트 환경변수는 `-e` 로 명시한 것만 전달한다.
- 비root(호스트 uid:gid), 읽기 전용 루트, 전 capability drop, no-new-privileges, 메모리·CPU·pid·파일 크기·시간 제한.
- 설치 단계: `npm ci --ignore-scripts`(프로젝트 코드는 실행하지 않는다). 컨테이너는 internal 네트워크에만 붙고
  허용 호스트 CONNECT 만 중계하는 프록시를 통해서만 나간다. 호스트(host.docker.internal)·LAN·임의 인터넷은 막힌다.
- 검사 단계: tsconfig.json 이 있으면 `tsc --noEmit` 을 `--network none` 에서 돈다(ADR 0058). 빌드 스크립트 내용과 무관하게 돌아서
  worker 가 호스트에서 못 하는 타입 검사를 대신하고, 실패는 vite 빌드 오류와 구분되는 오류 목록으로 돌려준다.
- 빌드 단계: `--network none`. 프로젝트 코드는 이 단계에서만 실행된다.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ttakkari.config import get_settings

BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
PROXY_SCRIPT = Path(__file__).with_name("egress_proxy.js")
PROXY_ALIAS = "registry-proxy"
# 복사하지 않는 이름. .git 은 훅 심기, .env* 는 비밀, node_modules/dist 는 컨테이너 안에서 새로 만들고,
# .ttakkari-feedback 은 worker 가 열어 보는 스크린샷 사본이라 빌드에 필요 없다.
SKIP_DIRS = frozenset({".git", "node_modules", "dist", ".ttakkari-feedback"})
ENV_SAFE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})
LOG_TAIL_BYTES = 200 * 1024
# docker CLI 자체가 쓰는 환경만 넘긴다. TTAKKARI_* 와 비밀 변수는 CLI 프로세스에도 주지 않는다.
CLI_ENV_KEYS = ("PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "TMPDIR")

Network = Literal["none", "egress"]

# 타입 검사 실패 오류 문구의 머리말. autobuild 가 worker 에게 되돌릴 실패를 가려내는 표식으로 쓴다.
TYPECHECK_FAILED = "타입 검사 실패"
MAX_DIAGNOSTICS = 20
_TSC_DIAGNOSTIC_RE = re.compile(r"error TS\d+:")
# typescript 가 설치돼 있지 않으면 검사할 수 없으니 건너뛰고 로그에 남긴다(빌드 자체는 계속한다).
_TYPECHECK_SCRIPT = ("if [ -x node_modules/.bin/tsc ]; then node_modules/.bin/tsc --noEmit --pretty false; "
                     "else echo '[check] typescript 가 설치되어 있지 않아 타입 검사를 건너뜁니다.'; fi")
TYPECHECK_COMMAND = ["sh", "-c", _TYPECHECK_SCRIPT]


def tsc_diagnostics(log: str) -> list[str]:
    """tsc --pretty false 출력에서 오류 줄만 뽑는다(src/a.ts(3,5): error TS2322: ...). 이어지는 들여쓴 설명 줄은 버린다."""
    return [ln.strip() for ln in log.splitlines() if _TSC_DIAGNOSTIC_RE.search(ln)]


def typecheck_error(exit_code: int | None, timed_out: bool, log: str) -> str:
    if timed_out:
        return f"{TYPECHECK_FAILED}: 시간 초과"
    diags = tsc_diagnostics(log)
    head = f"{TYPECHECK_FAILED}(tsc exit {exit_code}): 오류 {len(diags)}건" if diags else f"{TYPECHECK_FAILED}(tsc exit {exit_code})"
    shown = diags[:MAX_DIAGNOSTICS]
    more = f"\n... 외 {len(diags) - len(shown)}건" if len(diags) > len(shown) else ""
    return head + ("\n" + "\n".join(shown) + more if shown else "\n" + log.strip()[-1500:])


class IsolationError(Exception):
    def __init__(self, message: str, code: str = "isolation_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeStatus:
    available: bool
    reason: str


def probe_runtime(runtime: str | None = None, image: str | None = None) -> RuntimeStatus:
    """컨테이너 런타임이 실제로 컨테이너를 돌릴 수 있는지 확인한다. 테스트가 skip 사유로도 쓴다."""
    s = get_settings()
    runtime = runtime or s.game_build_runtime
    image = image or s.game_build_image
    exe = shutil.which(runtime)
    if not exe:
        return RuntimeStatus(False, f"컨테이너 런타임 '{runtime}' 이(가) PATH 에 없다")
    try:
        info = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return RuntimeStatus(False, f"'{runtime} info' 실패: {e}")
    if info.returncode != 0:
        return RuntimeStatus(False, f"컨테이너 데몬에 연결할 수 없다: {(info.stderr or info.stdout).strip()[:200]}")
    try:
        img = subprocess.run([exe, "image", "inspect", image, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return RuntimeStatus(False, f"이미지 확인 실패: {e}")
    if img.returncode != 0:
        # 이미지가 없어도 pull 이 가능하면 러너가 받는다. 사용 가능 여부에는 영향을 주지 않는다.
        return RuntimeStatus(True, f"{runtime} {info.stdout.strip()} (이미지 {image} 는 첫 실행에서 받는다)")
    return RuntimeStatus(True, f"{runtime} {info.stdout.strip()} / {image}")


@dataclass(frozen=True)
class IsolationLimits:
    memory_mb: int = 2048
    cpus: float = 2.0
    pids: int = 512
    tmp_mb: int = 512
    file_size_mb: int = 512
    install_timeout: int = 300
    build_timeout: int = 300
    max_source_mb: int = 200
    max_output_mb: int = 200

    @classmethod
    def from_settings(cls) -> IsolationLimits:
        s = get_settings()
        return cls(
            memory_mb=s.game_build_memory_mb, cpus=s.game_build_cpus, pids=s.game_build_pids,
            install_timeout=s.game_build_install_timeout_seconds, build_timeout=s.game_build_timeout_seconds,
            max_source_mb=s.game_build_max_source_mb, max_output_mb=s.game_build_max_output_mb,
        )


@dataclass
class StageResult:
    stage: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    log: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class IsolatedBuildResult:
    success: bool
    build_id: str
    stages: list[StageResult] = field(default_factory=list)
    output_dir: Path | None = None
    bundle_files: list[str] = field(default_factory=list)
    error: str | None = None
    skipped_entries: list[str] = field(default_factory=list)

    @property
    def log(self) -> str:
        return "\n".join(f"== {st.stage} (exit={st.exit_code}, {st.duration_s:.1f}s{', timeout' if st.timed_out else ''}) ==\n{st.log}" for st in self.stages)


class _TailBuffer:
    """출력이 아무리 많아도 마지막 limit 바이트만 보관한다."""

    def __init__(self, limit: int):
        self.limit = limit
        self.data = b""
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > self.limit * 2:
            self.data = self.data[-self.limit :]
            self.truncated = True

    def text(self) -> str:
        data = self.data[-self.limit :]
        if len(self.data) > self.limit:
            self.truncated = True
        out = data.decode("utf-8", "replace")
        return ("[...앞부분 생략...]\n" + out) if self.truncated else out


def _cli_env() -> dict[str, str]:
    return {k: os.environ[k] for k in CLI_ENV_KEYS if k in os.environ}


def _check_build_id(build_id: str) -> None:
    if not BUILD_ID_RE.fullmatch(build_id):
        raise IsolationError("빌드 ID 형식이 올바르지 않습니다.", "bad_build_id")


def _check_mount_path(path: Path) -> str:
    text = str(path)
    if any(c in text for c in (",", ":", "\x00", "\n")):
        raise IsolationError("마운트 경로에 쓸 수 없는 문자가 있습니다.", "bad_path")
    return text


def stage_source(source_dir: Path, dest: Path, *, max_bytes: int) -> list[str]:
    """원본 프로젝트를 안전한 복사본으로 옮긴다. 일반 파일과 디렉터리만 복사하고 건너뛴 항목을 돌려준다."""
    source_dir = source_dir.resolve(strict=True)
    if not source_dir.is_dir():
        raise IsolationError("프로젝트 경로가 디렉터리가 아닙니다.", "not_a_dir")
    dest.mkdir(parents=True, exist_ok=False)
    skipped: list[str] = []
    total = 0
    for root, dirs, files in os.walk(source_dir, followlinks=False):
        rel_root = Path(root).relative_to(source_dir)
        keep: list[str] = []
        for d in dirs:
            rel = (rel_root / d).as_posix()
            full = Path(root, d)
            if full.is_symlink():
                skipped.append(f"{rel} (symlink)")
            elif d in SKIP_DIRS:
                skipped.append(f"{rel}/")
            else:
                keep.append(d)
                (dest / rel_root / d).mkdir(parents=True, exist_ok=True)
        dirs[:] = keep
        for f in files:
            rel = (rel_root / f).as_posix()
            full = Path(root, f)
            if full.is_symlink() or not full.is_file():
                skipped.append(f"{rel} ({'symlink' if full.is_symlink() else 'non-regular'})")
                continue
            if f.startswith(".env") and f not in ENV_SAFE_NAMES:
                skipped.append(f"{rel} (env)")
                continue
            total += full.stat().st_size
            if total > max_bytes:
                raise IsolationError(f"소스가 제한({max_bytes // (1024 * 1024)}MB)을 넘었습니다.", "source_too_large")
            target = dest / rel_root / f
            shutil.copyfile(full, target)
            target.chmod(0o755 if os.access(full, os.X_OK) else 0o644)
    return skipped


def collect_output(dist: Path, out: Path, *, max_bytes: int) -> list[str]:
    """빌드 산출물을 검증하며 복사한다. 심볼릭 링크·특수 파일이 있으면 통째로 거부한다."""
    if dist.is_symlink() or not dist.is_dir():
        raise IsolationError("dist 디렉터리가 없습니다.", "no_output")
    if not (dist / "index.html").is_file() or (dist / "index.html").is_symlink():
        raise IsolationError("dist/index.html 이 없습니다.", "no_index")
    files: list[str] = []
    total = 0
    for root, dirs, names in os.walk(dist, followlinks=False):
        rel_root = Path(root).relative_to(dist)
        for d in dirs:
            if Path(root, d).is_symlink():
                raise IsolationError(f"산출물에 심볼릭 링크가 있습니다: {(rel_root / d).as_posix()}", "output_symlink")
        for n in names:
            full = Path(root, n)
            if full.is_symlink():
                raise IsolationError(f"산출물에 심볼릭 링크가 있습니다: {(rel_root / n).as_posix()}", "output_symlink")
            if not full.is_file():
                raise IsolationError(f"산출물에 일반 파일이 아닌 항목이 있습니다: {(rel_root / n).as_posix()}", "output_special")
            total += full.stat().st_size
            if total > max_bytes:
                raise IsolationError(f"산출물이 제한({max_bytes // (1024 * 1024)}MB)을 넘었습니다.", "output_too_large")
            files.append((rel_root / n).as_posix())
    out.mkdir(parents=True, exist_ok=False)
    for rel in files:
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dist / rel, target)
        target.chmod(0o644)
    return sorted(files)


class IsolatedBuildRunner:
    def __init__(self, *, runtime: str | None = None, image: str | None = None, limits: IsolationLimits | None = None,
                 registry_hosts: list[str] | None = None):
        s = get_settings()
        self.runtime = shutil.which(runtime or s.game_build_runtime) or (runtime or s.game_build_runtime)
        self.image = image or s.game_build_image
        self.limits = limits or IsolationLimits.from_settings()
        self.registry_hosts = list(registry_hosts or s.game_registry_hosts)
        uid, gid = os.getuid(), os.getgid()
        if uid == 0:
            raise IsolationError("root 로 실행 중이라 비root 컨테이너 사용자를 정할 수 없습니다.", "root_user")
        self.user = f"{uid}:{gid}"

    # ---- docker 호출 기반
    async def _docker(self, *args: str, timeout: float = 30) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self.runtime, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=_cli_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "timeout"
        return proc.returncode or 0, out.decode("utf-8", "replace")

    async def _rm(self, name: str) -> None:
        await self._docker("rm", "-f", name, timeout=30)

    async def ensure_image(self) -> None:
        code, _ = await self._docker("image", "inspect", self.image, timeout=20)
        if code == 0:
            return
        code, out = await self._docker("pull", self.image, timeout=600)
        if code != 0:
            raise IsolationError(f"이미지 {self.image} 를 받지 못했습니다: {out[-300:]}", "image_unavailable")

    def _run_argv(self, *, name: str, build_id: str, network: str, workdir: Path | None, command: list[str],
                  env: dict[str, str], detach: bool = False, writable_tmp: bool = True) -> list[str]:
        lim = self.limits
        argv = [
            self.runtime, "run", "--rm", "--pull", "never", "--name", name, "--label", f"ttakkari.game-build={build_id}",
            "--user", self.user, "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m", "--cpus", str(lim.cpus),
            "--pids-limit", str(lim.pids), "--ulimit", "nofile=4096:4096",
            "--ulimit", f"fsize={lim.file_size_mb * 1024 * 1024}", "--network", network,
        ]
        if detach:
            argv.append("-d")
        if writable_tmp:
            argv += ["--tmpfs", f"/tmp:rw,nosuid,nodev,size={lim.tmp_mb}m"]
        if workdir is not None:
            argv += ["-v", f"{_check_mount_path(workdir)}:/work:rw", "-w", "/work"]
        for k, v in env.items():
            argv += ["-e", f"{k}={v}"]
        return [*argv, self.image, *command]

    async def _exec(self, argv: list[str], name: str, timeout: float) -> tuple[int | None, bool, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_cli_env(),
        )
        buf = _TailBuffer(LOG_TAIL_BYTES)

        async def pump() -> None:
            assert proc.stdout is not None
            while chunk := await proc.stdout.read(65536):
                buf.add(chunk)

        pump_task = asyncio.create_task(pump())
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:
                # 시간 초과·취소: 컨테이너를 먼저 지워야 안의 프로세스가 남지 않는다.
                await asyncio.shield(self._rm(name))
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(pump_task, 5)
        pump_task.cancel()
        return (None if timed_out else proc.returncode), timed_out, buf.text()

    # ---- 설치 단계 egress 프록시
    @contextlib.asynccontextmanager
    async def _egress(self, build_id: str) -> AsyncIterator[dict[str, str]]:
        net, proxy = f"ttk-game-{build_id}", f"ttk-game-{build_id}-proxy"
        code, out = await self._docker("network", "create", "--internal", "--label", f"ttakkari.game-build={build_id}", net)
        if code != 0:
            raise IsolationError(f"격리 네트워크를 만들지 못했습니다: {out[-200:]}", "network_failed")
        try:
            argv = self._run_argv(
                name=proxy, build_id=build_id, network="bridge", workdir=None, detach=True, writable_tmp=False,
                env={"ALLOW_HOSTS": ",".join(self.registry_hosts)}, command=["node", "/proxy.js"],
            )
            # 프록시 스크립트만 읽기 전용으로 마운트한다.
            argv[argv.index(self.image):argv.index(self.image)] = ["-v", f"{_check_mount_path(PROXY_SCRIPT)}:/proxy.js:ro"]
            code, out = await self._docker(*argv[1:], timeout=60)
            if code != 0:
                raise IsolationError(f"egress 프록시를 시작하지 못했습니다: {out[-200:]}", "proxy_failed")
            code, out = await self._docker("network", "connect", "--alias", PROXY_ALIAS, net, proxy)
            if code != 0:
                raise IsolationError(f"egress 프록시를 연결하지 못했습니다: {out[-200:]}", "proxy_failed")
            for _ in range(50):
                _, logs = await self._docker("logs", proxy)
                if "READY" in logs:
                    break
                await asyncio.sleep(0.2)
            else:
                raise IsolationError("egress 프록시가 준비되지 않았습니다.", "proxy_failed")
            url = f"http://{PROXY_ALIAS}:3128"
            yield {"network": net, "HTTPS_PROXY": url, "HTTP_PROXY": url, "npm_config_https_proxy": url, "npm_config_proxy": url}
        finally:
            await self._rm(proxy)
            await self._docker("network", "rm", net)

    # ---- 단계 실행
    async def run_stage(self, workdir: Path, command: list[str], *, stage: str, build_id: str, network: Network,
                        timeout: float, env: dict[str, str] | None = None) -> StageResult:
        _check_build_id(build_id)
        base_env = {"HOME": "/tmp", "CI": "true", "npm_config_update_notifier": "false", "npm_config_cache": "/tmp/npm-cache"}
        base_env.update(env or {})
        name = f"ttk-game-{build_id}-{stage}"
        started = time.monotonic()
        if network == "egress":
            async with self._egress(build_id) as eg:
                net = eg.pop("network")
                argv = self._run_argv(name=name, build_id=build_id, network=net, workdir=workdir, command=command, env={**base_env, **eg})
                code, timed_out, log = await self._exec(argv, name, timeout)
        else:
            argv = self._run_argv(name=name, build_id=build_id, network="none", workdir=workdir, command=command, env=base_env)
            code, timed_out, log = await self._exec(argv, name, timeout)
        return StageResult(stage, code, timed_out, time.monotonic() - started, log)

    async def build_project(self, source_dir: Path, work_dir: Path, build_id: str, *, build_script: str = "build",
                            keep_stage: bool = False, typecheck: bool | None = None) -> IsolatedBuildResult:
        """source_dir 를 스테이징 복사 → 설치(egress·스크립트 없음) → 타입 검사(네트워크 없음) → 빌드(네트워크 없음) → dist 검증·복사.

        work_dir/<build_id>/out 에 산출물이 남는다. 이미 있는 build_id 는 거부해 이전 산출물을 덮어쓰지 않는다.
        """
        _check_build_id(build_id)
        if typecheck is None:
            typecheck = get_settings().game_build_typecheck
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,40}", build_script):
            raise IsolationError("빌드 스크립트 이름이 올바르지 않습니다.", "bad_script")
        result = IsolatedBuildResult(success=False, build_id=build_id)
        build_root = work_dir / build_id
        if build_root.exists():
            raise IsolationError(f"빌드 {build_id} 가 이미 있습니다.", "build_exists")
        build_root.mkdir(parents=True, mode=0o700)
        stage_dir = build_root / "src"
        try:
            await ensure_image_once(self)
            result.skipped_entries = await asyncio.to_thread(
                stage_source, source_dir, stage_dir, max_bytes=self.limits.max_source_mb * 1024 * 1024)
            if not (stage_dir / "package.json").is_file():
                raise IsolationError("package.json 이 없습니다.", "no_package_json")
            install_cmd = ["npm", "ci" if (stage_dir / "package-lock.json").is_file() else "install",
                           "--ignore-scripts", "--no-audit", "--no-fund", "--no-progress"]
            inst = await self.run_stage(stage_dir, install_cmd, stage="install", build_id=build_id, network="egress",
                                        timeout=self.limits.install_timeout)
            result.stages.append(inst)
            if not inst.ok:
                result.error = "의존성 설치 시간 초과" if inst.timed_out else f"의존성 설치 실패(exit {inst.exit_code})"
                return result
            if typecheck and (stage_dir / "tsconfig.json").is_file():
                checked = await self.run_stage(stage_dir, TYPECHECK_COMMAND, stage="check", build_id=build_id, network="none",
                                               timeout=self.limits.build_timeout)
                result.stages.append(checked)
                if not checked.ok:
                    result.error = typecheck_error(checked.exit_code, checked.timed_out, checked.log)
                    return result
            built = await self.run_stage(stage_dir, ["npm", "run", build_script], stage="build", build_id=build_id,
                                         network="none", timeout=self.limits.build_timeout)
            result.stages.append(built)
            if not built.ok:
                result.error = "빌드 시간 초과" if built.timed_out else f"빌드 실패(exit {built.exit_code})"
                return result
            out = build_root / "out"
            result.bundle_files = await asyncio.to_thread(
                collect_output, stage_dir / "dist", out, max_bytes=self.limits.max_output_mb * 1024 * 1024)
            result.output_dir = out
            result.success = True
            return result
        except IsolationError as e:
            result.error = str(e)
            result.stages.append(StageResult("guard", None, False, 0.0, f"{e.code}: {e}"))
            return result
        finally:
            if not keep_stage:
                shutil.rmtree(stage_dir, ignore_errors=True)


_image_ready: set[tuple[str, str]] = set()


async def ensure_image_once(runner: IsolatedBuildRunner) -> None:
    key = (runner.runtime, runner.image)
    if key not in _image_ready:
        await runner.ensure_image()
        _image_ready.add(key)
