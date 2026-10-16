"""공용 원자 lock 아래에서 llama.cpp 서버 하나를 소유 PID 로만 띄우고, 그 동안 명령을 실행한다.
서버 인자는 기존 supervise_server.py(Qwen 4B 측정)와 같다: CPU-only, 6 threads, ctx 4096, parallel 1, jinja."""
import argparse, hashlib, json, os, subprocess, sys, time, urllib.request, uuid
from pathlib import Path
LOCK = Path('/private/tmp/ttakkari-apple-inference-bench.lock')
BIN = Path('/private/tmp/qwen35-router/llama/llama-b11655/llama-server')
MODEL = Path('/private/tmp/qwen35-router/model/Qwen3.5-4B-Q4_K_M.gguf')
def main():
    p = argparse.ArgumentParser(); p.add_argument('--port', type=int, default=18793); p.add_argument('--log', required=True)
    p.add_argument('--meta', required=True); p.add_argument('cmd', nargs=argparse.REMAINDER)
    a = p.parse_args(); cmd = a.cmd[1:] if a.cmd and a.cmd[0] == '--' else a.cmd
    try: LOCK.mkdir()
    except FileExistsError:
        prior = LOCK / 'owner.json'; print('LOCK_BUSY', prior.read_text() if prior.exists() else '?', flush=True); return 75
    owner = f'qwen-harness-{uuid.uuid4()}'; started = time.time(); child = None
    argv = [str(BIN), '--model', str(MODEL), '--device', 'none', '--gpu-layers', '0', '--threads', '6', '--threads-batch', '6',
            '--ctx-size', '4096', '--parallel', '1', '--host', '127.0.0.1', '--port', str(a.port), '--jinja', '--no-webui']
    meta = {'owner_id': owner, 'started_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(started)), 'parent_pid': os.getpid(),
            'argv': argv, 'loadavg_at_start': os.getloadavg()}
    try:
        with open(a.log, 'wb') as err:
            child = subprocess.Popen(argv, stdout=err, stderr=subprocess.STDOUT); meta['child_pid'] = child.pid
            (LOCK / 'owner.json').write_text(json.dumps(meta))
        ep = f'http://127.0.0.1:{a.port}'; dl = time.monotonic() + 120
        while time.monotonic() < dl:
            if child.poll() is not None: raise RuntimeError('server exited early')
            try:
                with urllib.request.urlopen(ep + '/health', timeout=2) as r:
                    if r.status == 200: break
            except Exception: time.sleep(1)
        else: raise TimeoutError('ready timeout')
        meta['ready_seconds'] = round(time.time() - started, 2)
        env = dict(os.environ, QWEN_ENDPOINT=ep, QWEN_READY_SECONDS=str(meta['ready_seconds']), QWEN_STARTED_EPOCH=str(started))
        rc = subprocess.run(cmd, env=env).returncode
        meta['cmd_exit'] = rc; return rc
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: child.kill(); child.wait()
        try: cur = json.loads((LOCK / 'owner.json').read_text())
        except Exception: cur = {}
        if cur.get('owner_id') == owner:
            (LOCK / 'owner.json').unlink(); LOCK.rmdir(); print('LOCK_RELEASED', owner, flush=True)
        meta['loadavg_at_end'] = os.getloadavg(); meta['child_exit_code'] = child.returncode if child else None
        Path(a.meta).write_text(json.dumps(meta, indent=2))
if __name__ == '__main__': sys.exit(main())
