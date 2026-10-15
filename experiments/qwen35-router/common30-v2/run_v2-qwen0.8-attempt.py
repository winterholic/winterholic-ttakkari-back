"""Isolated streaming replay for the frozen common-30 v2 router corpus."""
from __future__ import annotations
import argparse, asyncio, hashlib, json, statistics, time
from pathlib import Path
import httpx
from ttakkari.experiments.qwen35_shadow import CandidateSnapshot, build_response_schema, validate_shadow_output

ROOT=Path(__file__).resolve().parent
CASES=ROOT/'common30-v2.jsonl'
SYSTEM=ROOT/'system-prompt-v2.txt'

async def infer_stream(client, endpoint, model, prompt, candidates):
    payload={
      'model':model,
      'messages':[{'role':'system','content':SYSTEM.read_text()},
                  {'role':'user','content':json.dumps({'candidates':candidates.as_prompt_data(),'request':prompt},ensure_ascii=False)}],
      'temperature':0,'seed':20261015,'max_tokens':1024,
      'chat_template_kwargs':{'enable_thinking':False},
      'response_format':{'type':'json_schema','json_schema':{'name':'desk_shadow_decision','strict':True,'schema':build_response_schema(candidates)}},
      'stream':True,
    }
    started=time.perf_counter(); first=None; parts=[]; events=0
    async with client.stream('POST',endpoint+'/v1/chat/completions',json=payload) as response:
      response.raise_for_status()
      async for line in response.aiter_lines():
        if not line.startswith('data:'): continue
        data=line[5:].strip()
        if data=='[DONE]': break
        item=json.loads(data); events+=1
        for choice in item.get('choices',[]):
          delta=choice.get('delta') or {}
          content=delta.get('content')
          if content is not None:
            if first is None and content: first=(time.perf_counter()-started)*1000
            parts.append(content)
    total=(time.perf_counter()-started)*1000
    raw=''.join(parts)
    decision=validate_shadow_output(raw,candidates)
    return {'total_ms':round(total,2),'ttft_ms':round(first,2) if first is not None else None,
            'sse_events':events,'raw_json':raw,'output':decision.model_dump(mode='json')}

async def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--base-url',required=True); ap.add_argument('--model',required=True); ap.add_argument('--out',required=True); ap.add_argument('--started-epoch',type=float,required=True)
    args=ap.parse_args(); endpoint=args.base_url.rstrip('/')
    cases=[json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    assert len(cases)==30 and len({x['case_id'] for x in cases})==30
    candidates=CandidateSnapshot(lead_ids=(),model_ids=(),tool_ids=(),permission_ids=())
    system=SYSTEM.read_text(); sh=hashlib.sha256(system.encode()).hexdigest()
    observations=[]; times=[]
    async with httpx.AsyncClient(timeout=120,trust_env=False) as client:
      for repeat in range(1,4):
       for case in cases:
        prompt=f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}\n[사용자 메시지]\n{case['message']}"
        row={'case_id':case['case_id'],'repeat':repeat,'expected_status':case['gold_status'],'allowed_actions':case['allowed_actions'],'input_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'system_sha256':sh,'started_at_epoch':args.started_epoch}
        try:
          result=await infer_stream(client,endpoint,args.model,prompt,candidates)
          action=result['output']['action']; row.update(result)
          row['valid']=True; row['exact_correct']=action==case['gold_status'] if case['gold_status']!='hold' else None
          row['acceptable_set_correct']=action in case['allowed_actions']
          row['candidate_membership_valid']=True
          times.append(result['total_ms'])
        except Exception as exc:
          row.update({'valid':False,'exact_correct':False if case['gold_status']!='hold' else None,'acceptable_set_correct':False,
                      'candidate_membership_valid':False if type(exc).__name__=='ValueError' else None,
                      'failure_type':type(exc).__name__,'failure_detail':str(exc)[:240]})
        observations.append(row); print(json.dumps(row,ensure_ascii=False),flush=True)
    assert len(observations)==90, f'incomplete run: {len(observations)}/90'
    ordered=sorted(times)
    def percentile(p): return round(ordered[max(0,min(len(ordered)-1,__import__('math').ceil(p*len(ordered))-1))],2) if ordered else None
    summary={'dataset_sha256':hashlib.sha256(CASES.read_bytes()).hexdigest(),'system_sha256':sh,'n':len(observations),
      'valid':sum(x['valid'] for x in observations),'exact_scored_n':sum(x['expected_status']!='hold' for x in observations),
      'exact_correct':sum(x['exact_correct'] is True for x in observations),'acceptable_set_correct':sum(x['acceptable_set_correct'] for x in observations),
      'abstain':sum(x.get('output',{}).get('action')=='abstain' for x in observations),'coverage_non_abstain':sum(x.get('output',{}).get('action')!='abstain' for x in observations),
      'latency_p50_ms':round(statistics.median(times),2) if times else None,'latency_p95_ms':percentile(.95),
      'cold_request_total_ms':observations[0].get('total_ms'),'cold_request_ttft_ms':observations[0].get('ttft_ms'),
      'model_load_seconds_to_ready':round((time.time()-args.started_epoch),2),
      'observations':observations}
    Path(args.out).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print('SUMMARY '+json.dumps({k:v for k,v in summary.items() if k!='observations'},ensure_ascii=False),flush=True)

if __name__=='__main__': asyncio.run(main())
