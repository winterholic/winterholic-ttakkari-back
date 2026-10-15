import asyncio, json, time
from pathlib import Path
from types import SimpleNamespace
from ttakkari.meetings.local_stt.runner import run_worker
from ttakkari.meetings.local_stt.core import error_rates
ROOT=Path('/Users/winterholic/testing/ttakkari-stt')
OUT=Path('/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/stt/evidence/phase2')
async def main():
    base=json.loads((ROOT/'full-request.json').read_text())
    manifest=json.loads((ROOT/'manifest.json').read_text())
    rows=[]
    for run_index, threads in enumerate([4,6,6,4]):
        settings=SimpleNamespace(home=ROOT/'benchmark-home',local_stt_python=ROOT/'venv/bin/python',
            local_stt_model_path=Path(base['model_path']),local_stt_timeout_seconds=1800,
            local_stt_max_audio_seconds=7200,local_stt_cpu_threads=threads)
        for entry in manifest[:3]+[{'id':'private-first-180s','audio_path':base['audio_path'],'start':0,'end':180}]:
            params={'profile':'improved', **{k:entry[k] for k in ('start','end') if k in entry}}
            result=await run_worker(Path(entry['audio_path']),params,settings)
            # Private hypothesis remains outside Git; public report contains only scores and timing.
            (ROOT/f"phase2-run{run_index}-{threads}-{entry['id']}.json").write_text(json.dumps(result,ensure_ascii=False))
            row={'run_index':run_index,'id':entry['id'],'threads':threads,'audio_sha256':result['audio_sha256'],
                 'start':result['start'],'end':result['end'],'options':result['options'],
                 'metrics':result['metrics'],'segments':len(result['segments']),
                 'characters':len(result['text']),
                 'error_rates':error_rates(entry['reference'],result['text']) if entry.get('reference') else None}
            rows.append(row)
            (OUT/'cpu-threads.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False))
            print(threads,entry['id'],round(result['metrics']['wall_seconds'],3),row['error_rates'],flush=True)
asyncio.run(main())
