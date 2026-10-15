import os
from pathlib import Path
ROOT=Path('/Users/winterholic/testing/ttakkari-stt')
os.environ.update(HF_HOME=str(ROOT/'isolated-phase2-hf'),HF_HUB_OFFLINE='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1',PYANNOTE_METRICS_ENABLED='0',MPLCONFIGDIR=str(ROOT/'mpl'),XDG_CACHE_HOME=str(ROOT/'cache'))
import dataclasses,gc,hashlib,json,resource,time
import numpy as np
import torch
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from ttakkari.meetings.local_stt.separation import load_separator
from ttakkari.meetings.local_stt.core import compare_overlap_candidate,normalize_segments
from ttakkari.meetings.local_stt.enhance import reprocess_overlaps
from ttakkari.meetings.local_stt.worker import decode_options
torch.set_num_threads(4)
manifest=json.loads((ROOT/'manifest.json').read_text())
req=json.loads((ROOT/'full-request.json').read_text())
a,b=[decode_audio(manifest[i]['audio_path'],sampling_rate=16000) for i in [0,1]]
# Simultaneous source onset; zero-pad to 12.48 s. Each known source is from FLEURS.
length=max(len(a),len(b));a=np.pad(a,(0,length-len(a)));b=np.pad(b,(0,length-len(b)))
a=a/(np.sqrt(np.mean(a*a))+1e-9)*.05;b=b/(np.sqrt(np.mean(b*b))+1e-9)*.05
mixture=(a+b).astype(np.float32)
refs=[manifest[i]['reference'] for i in [0,1]]
report={'pid':os.getpid(),'kind':'synthetic_fleurs_overlap','source_ids':[manifest[i]['id'] for i in [0,1]],'duration':length/16000,
        'mixture_pcm_sha256':hashlib.sha256(mixture.tobytes()).hexdigest(),
        'assumptions':'Known source mixing interval supplied; not model-detected. Real meeting performance and overlap detection metrics unmeasured.',
        'separator_revision':'9950fd8055b38679d2af5bef2366b5dba600ec66','speaker_attribution_measured':False}
t=time.perf_counter();separator=load_separator(ROOT/'sepformer-whamr');report['separation_load_seconds']=time.perf_counter()-t
t=time.perf_counter();sources=separator(mixture);report['separation_inference_seconds']=time.perf_counter()-t
# Release separator before ASR; process exit remains lifetime guarantee.
del separator;gc.collect()
t=time.perf_counter();model=WhisperModel(req['model_path'],device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True);report['stt_load_seconds']=time.perf_counter()-t
opts=decode_options('improved',[])
def recognize(samples,offset):
    it,_=model.transcribe(samples,**opts)
    raw=[dataclasses.asdict(s) for s in it]
    return {'text':''.join(s['text'] for s in raw).strip(),'raw_segments':raw,'segments':normalize_segments(raw,duration=len(samples)/16000,offset=offset)}
t=time.perf_counter();baseline=recognize(mixture,0);report['baseline_stt_seconds']=time.perf_counter()-t
t=time.perf_counter();tracks=[recognize(s,0) for s in sources];report['separated_stt_seconds']=time.perf_counter()-t
report['decision']=compare_overlap_candidate(baseline['text'],[r['text'] for r in tracks],references=refs)
report['peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
report['original_preserved']=True
report['weights_sha256']={p.name:hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in (ROOT/'sepformer-whamr').glob('*.ckpt')}
(ROOT/'phase2-overlap-private.json').write_text(json.dumps({'reference':refs,'baseline':baseline,'tracks':tracks},ensure_ascii=False))
# Public FLEURS raw comparison for review, no personal recording or transcript.
(ROOT/'phase2-overlap-public-text.json').write_text(json.dumps({'reference':refs,'baseline':baseline['text'],'tracks':[r['text'] for r in tracks]},ensure_ascii=False,indent=2))
Path('/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/stt/evidence/phase2/synthetic-overlap.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
