"""Own one local llama.cpp server under the shared atomic bench lock."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

LOCK=Path('/private/tmp/ttakkari-apple-inference-bench.lock')
ROOT=Path(__file__).resolve().parents[3]
BIN=Path('/private/tmp/qwen35-router/llama/llama-b11655/llama-server')

def digest(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()

def main() -> int:
    p=argparse.ArgumentParser()
    p.add_argument('--model',required=True,type=Path); p.add_argument('--model-label',required=True)
    p.add_argument('--port',required=True,type=int); p.add_argument('--output',required=True,type=Path)
    p.add_argument('--owner-out',required=True,type=Path)
    a=p.parse_args(); model=a.model.resolve(); out=a.output.resolve(); owner_out=a.owner_out.resolve()
    if not model.is_file(): raise SystemExit('model file missing')
    try: LOCK.mkdir()
    except FileExistsError:
        prior=LOCK/'owner.json'
        print('LOCK_BUSY '+(prior.read_text() if prior.exists() else 'owner metadata unavailable'),flush=True)
        return 75
    owner_id=f'qwen35-common30-{a.model_label}-{uuid.uuid4()}'
    started=time.time()
    argv=[str(BIN),'--model',str(model),'--device','none','--gpu-layers','0','--threads','6','--threads-batch','6','--ctx-size','4096','--parallel','1','--host','127.0.0.1','--port',str(a.port),'--jinja','--no-webui']
    raw_log=out.with_suffix('.stderr.log'); runner_log=out.with_suffix('.stdout.txt')
    meta={'owner_id':owner_id,'started_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(started)),
      'parent_pid':os.getpid(),'child_pid':None,'run_id':owner_id,'model':a.model_label,'model_path':str(model),
      'model_sha256':digest(model),'server_path':str(BIN),'server_sha256':digest(BIN),
      'server_version':'llama.cpp b11655 commit 47f5a64ae','argv':argv,'raw_log':str(raw_log),
      'host':'127.0.0.1','port':a.port,'gpu_layers':0,'candidate_snapshot':{'lead_ids':[],'model_ids':[],'tool_ids':[],'permission_ids':[]}}
    child=None
    try:
        with raw_log.open('wb') as stderr:
            child=subprocess.Popen(argv,stdout=stderr,stderr=subprocess.STDOUT,cwd=ROOT)
            meta['child_pid']=child.pid
            LOCK.joinpath('owner.json').write_text(json.dumps(meta,indent=2)+'\n')
        print('LOCK_ACQUIRED '+json.dumps(meta),flush=True)
        endpoint=f'http://127.0.0.1:{a.port}'
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if child.poll() is not None:
                raise RuntimeError(f'server exited before ready: {child.returncode}; see {raw_log}')
            try:
                with urllib.request.urlopen(endpoint+'/health',timeout=2) as r:
                    if r.status==200: break
            except Exception: time.sleep(1)
        else: raise TimeoutError(f'server readiness timeout; see {raw_log}')
        ready=time.time(); meta['ready_at_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(ready)); meta['startup_seconds_to_ready']=round(ready-started,3); print(f'READY owner_id={owner_id} child_pid={child.pid} load_seconds={ready-started:.2f}',flush=True)
        env=os.environ.copy(); env['PYTHONPATH']=str(ROOT/'src')
        cmd=[str(ROOT/'.venv/bin/python'),str(Path(__file__).resolve().parent/'run_v2.py'),
          '--base-url',endpoint,'--model',f'qwen35-{a.model_label}-q4','--out',str(out),'--started-epoch',str(started),'--ready-seconds',str(ready-started)]
        with runner_log.open('wb') as stdout:
            proc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=stdout,stderr=subprocess.STDOUT,check=False)
        print(f'RUNNER_EXIT owner_id={owner_id} code={proc.returncode} raw={out} runner_log={runner_log}',flush=True)
        return proc.returncode
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                # Exact PID created above; terminate only this owned child.
                child.kill(); child.wait()
        try:
            current=json.loads(LOCK.joinpath('owner.json').read_text())
        except Exception:
            current={}
        if current.get('owner_id')==owner_id and current.get('child_pid')==(child.pid if child else None) and (child is None or child.poll() is not None):
            LOCK.joinpath('owner.json').unlink()
            LOCK.rmdir()
            print(f'LOCK_RELEASED owner_id={owner_id}',flush=True)
        else:
            print(f'LOCK_PRESERVED owner_id={owner_id} reason=ownership_or_exit_state_mismatch',flush=True)
        owner_out.parent.mkdir(parents=True,exist_ok=True)
        meta['ended_at_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        meta['child_exit_code']=child.returncode if child is not None else None
        owner_out.write_text(json.dumps(meta,indent=2)+'\n')

if __name__=='__main__': raise SystemExit(main())
