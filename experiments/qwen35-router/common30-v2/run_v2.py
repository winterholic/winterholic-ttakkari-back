"""Streaming replay; every attempted row retains raw output and latency, including failures."""
from __future__ import annotations
import argparse, asyncio, hashlib, json, math, statistics, time
from pathlib import Path
import httpx
from ttakkari.experiments.qwen35_shadow import CandidateSnapshot, build_response_schema, validate_shadow_output

ROOT=Path(__file__).resolve().parent
CASES=ROOT/'common30-v2.jsonl'
SYSTEM=ROOT/'system-prompt-v2.txt'

class InvalidStructuredOutput(Exception):
    def __init__(self, raw, total_ms, ttft_ms, sse_events, cause):
        super().__init__(str(cause)); self.raw=raw; self.total_ms=total_ms; self.ttft_ms=ttft_ms; self.sse_events=sse_events; self.cause=cause

async def infer_stream(client, endpoint, model, prompt, candidates):
    payload={'model':model,'messages':[{'role':'system','content':SYSTEM.read_text()},
      {'role':'user','content':json.dumps({'candidates':candidates.as_prompt_data(),'request':prompt},ensure_ascii=False)}],
      'temperature':0,'seed':20261015,'max_tokens':1024,'chat_template_kwargs':{'enable_thinking':False},
      'response_format':{'type':'json_schema','json_schema':{'name':'desk_shadow_decision','strict':True,'schema':build_response_schema(candidates)}},'stream':True}
    started=time.perf_counter(); first=None; parts=[]; events=0
    async with client.stream('POST',endpoint+'/v1/chat/completions',json=payload) as response:
      response.raise_for_status()
      async for line in response.aiter_lines():
        if not line.startswith('data:'): continue
        data=line[5:].strip()
        if data=='[DONE]': break
        item=json.loads(data); events+=1
        for choice in item.get('choices',[]):
          content=(choice.get('delta') or {}).get('content')
          if content is not None:
            if first is None and content: first=(time.perf_counter()-started)*1000
            parts.append(content)
    total=(time.perf_counter()-started)*1000; raw=''.join(parts)
    try: decision=validate_shadow_output(raw,candidates)
    except Exception as exc: raise InvalidStructuredOutput(raw,total,first,events,exc) from exc
    return {'total_ms':round(total,2),'ttft_ms':round(first,2) if first is not None else None,
      'sse_events':events,'raw_json':raw,'output':decision.model_dump(mode='json')}

def percentile(values,p):
    ordered=sorted(values)
    return round(ordered[max(0,min(len(ordered)-1,math.ceil(p*len(ordered))-1))],2) if ordered else None

async def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--base-url',required=True); ap.add_argument('--model',required=True); ap.add_argument('--out',required=True); ap.add_argument('--started-epoch',type=float,required=True); ap.add_argument('--ready-seconds',type=float,required=True)
    args=ap.parse_args(); endpoint=args.base_url.rstrip('/'); run_started=time.time()
    cases=[json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    assert len(cases)==30 and len({x['case_id'] for x in cases})==30
    candidates=CandidateSnapshot(lead_ids=(),model_ids=(),tool_ids=(),permission_ids=())
    sh=hashlib.sha256(SYSTEM.read_bytes()).hexdigest(); observations=[]; attempt_times=[]; valid_times=[]; ttfts=[]
    async with httpx.AsyncClient(timeout=120,trust_env=False) as client:
      for repeat in range(1,4):
       for case in cases:
        prompt=f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}\n[사용자 메시지]\n{case['message']}"
        row={'case_id':case['case_id'],'repeat':repeat,'expected_status':case['gold_status'],'allowed_actions':case['allowed_actions'],
          'input_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'system_sha256':sh,'started_at_epoch':time.time()}
        attempt_started=time.perf_counter()
        try:
          result=await infer_stream(client,endpoint,args.model,prompt,candidates)
          row.update(result); row['valid']=True; row['exact_correct']=result['output']['action']==case['gold_status'] if case['gold_status']!='hold' else None
          row['acceptable_set_correct']=result['output']['action'] in case['allowed_actions']; row['candidate_membership_valid']=True
          valid_times.append(result['total_ms']);
          if result['ttft_ms'] is not None: ttfts.append(result['ttft_ms'])
        except InvalidStructuredOutput as exc:
          row.update({'valid':False,'raw_json':exc.raw,'total_ms':round(exc.total_ms,2),'ttft_ms':round(exc.ttft_ms,2) if exc.ttft_ms is not None else None,
            'sse_events':exc.sse_events,'failure_type':type(exc.cause).__name__,'failure_detail':str(exc.cause)[:500],
            'exact_correct':False if case['gold_status']!='hold' else None,'acceptable_set_correct':False,'candidate_membership_valid':False})
          valid_times.append(exc.total_ms)
          if exc.ttft_ms is not None: ttfts.append(exc.ttft_ms)
        except Exception as exc:
          row.update({'valid':False,'total_ms':round((time.perf_counter()-attempt_started)*1000,2),'ttft_ms':None,
            'failure_type':type(exc).__name__,'failure_detail':str(exc)[:500],
            'exact_correct':False if case['gold_status']!='hold' else None,'acceptable_set_correct':False,'candidate_membership_valid':None})
        attempt_times.append(row['total_ms']); observations.append(row); print(json.dumps(row,ensure_ascii=False),flush=True)
    assert len(observations)==90, f'incomplete run: {len(observations)}/90'
    valid=sum(x['valid'] for x in observations); valid_nonabstain=sum(x['valid'] and x.get('output',{}).get('action') not in (None,'abstain') for x in observations)
    summary={'dataset_sha256':hashlib.sha256(CASES.read_bytes()).hexdigest(),'system_sha256':sh,'n':len(observations),'valid':valid,'invalid':len(observations)-valid,
      'hard_scored_n':sum(x['expected_status']!='hold' for x in observations),'exact_correct':sum(x['exact_correct'] is True for x in observations),
      'acceptable_set_correct':sum(x['acceptable_set_correct'] for x in observations),'abstain_valid':sum(x['valid'] and x['output']['action']=='abstain' for x in observations),
      'coverage_non_abstain_valid':valid_nonabstain,'invalid_by_type':{},'attempt_latency_p50_ms':percentile(attempt_times,.5),'attempt_latency_p95_ms':percentile(attempt_times,.95),
      'valid_latency_p50_ms':percentile(valid_times,.5),'valid_latency_p95_ms':percentile(valid_times,.95),'ttft_p50_ms':percentile(ttfts,.5),'ttft_p95_ms':percentile(ttfts,.95),
      'first_attempt_total_ms':observations[0].get('total_ms'),'first_attempt_ttft_ms':observations[0].get('ttft_ms'),
      'server_startup_seconds_to_ready':args.ready_seconds,'total_run_seconds_from_server_start':round(time.time()-args.started_epoch,2),
      'runner_wall_seconds':round(time.time()-run_started,2),'observations':observations}
    from collections import Counter
    summary['invalid_by_type']=dict(Counter(x.get('failure_type','unknown') for x in observations if not x['valid']))
    Path(args.out).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print('SUMMARY '+json.dumps({k:v for k,v in summary.items() if k!='observations'},ensure_ascii=False),flush=True)

if __name__=='__main__': asyncio.run(main())
