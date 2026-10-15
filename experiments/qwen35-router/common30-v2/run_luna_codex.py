"""One-pass Luna replay using the installed Codex CLI; no file or tool actions by the model."""
from __future__ import annotations
import hashlib,json,subprocess,time,statistics,math
from pathlib import Path
ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[2]
CASES=ROOT/'common30-v2.jsonl'; SYS=ROOT/'system-prompt-v2.txt'; SCHEMA=ROOT/'luna-output-schema.json'
CLI=Path('/opt/homebrew/bin/codex'); WORK=Path('/private/tmp/qwen35-router/luna-eval')

def pct(xs,p):
 x=sorted(xs); return round(x[max(0,min(len(x)-1,math.ceil(p*len(x))-1))],2) if x else None

def main():
 WORK.mkdir(parents=True,exist_ok=True)
 cases=[json.loads(x) for x in CASES.read_text().splitlines() if x.strip()]
 system=SYS.read_text(); system_hash=hashlib.sha256(system.encode()).hexdigest(); rows=[]; times=[]
 for case in cases:
  context=f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}\n[사용자 메시지]\n{case['message']}"
  request={'candidates':{'lead_ids':[],'model_ids':[],'tool_ids':[],'permission_ids':[]},'request':context}
  prompt=(system+"\n\n분류할 입력은 다음 JSON 하나다. 도구·파일·네트워크를 사용하지 말고 라우팅만 판정해라. 출력은 지정된 JSON Schema에 맞춰라.\n"+
          json.dumps(request,ensure_ascii=False,separators=(',',':')))
  prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(); out=WORK/f"{case['case_id']}.json"
  argv=[str(CLI),'exec','--ignore-user-config','--ephemeral','--sandbox','read-only','--skip-git-repo-check',
        '--model','gpt-6-luna','-c','model_reasoning_effort="low"','--output-schema',str(SCHEMA),
        '--output-last-message',str(out),'-C',str(WORK),'-']
  start=time.perf_counter(); proc=subprocess.run(argv,input=prompt,text=True,capture_output=True,timeout=180,check=False); elapsed=(time.perf_counter()-start)*1000; times.append(elapsed)
  raw=out.read_text() if out.exists() else proc.stdout
  try:
   decoded=json.loads(raw); valid=True; error=None
   valid=decoded.get('action') in {'answer','delegate','abstain'} and decoded.get('lead_id') is None and decoded.get('model_id') is None and decoded.get('tool_ids')==[] and decoded.get('permission_ids')==[] and isinstance(decoded.get('reply'),str) and bool(decoded['reply'].strip()) and (decoded.get('abstain_reason') is None if decoded['action']!='abstain' else decoded.get('abstain_reason') in {'ambiguous','insufficient_context','unsafe'})
   if not valid: error='schema_or_membership_validation_failed'
  except Exception as exc: decoded=None; valid=False; error=type(exc).__name__+': '+str(exc)[:300]
  row={'case_id':case['case_id'],'expected_status':case['gold_status'],'allowed_actions':case['allowed_actions'],
       'input_sha256':hashlib.sha256(context.encode()).hexdigest(),'prompt_sha256':prompt_hash,'system_sha256':system_hash,
       'model':'gpt-6-luna','reasoning_effort':'low','cli_version':'codex-cli 0.162.1','elapsed_ms':round(elapsed,2),
       'exit_code':proc.returncode,'valid':valid,'exact_correct':(decoded.get('action')==case['gold_status'] if case['gold_status']!='hold' and valid else (None if case['gold_status']=='hold' else False)),
       'acceptable_set_correct':bool(valid and decoded.get('action') in case['allowed_actions']),'output':decoded,'raw_output':raw,
       'stdout':proc.stdout,'stderr':proc.stderr,'failure':error}
  rows.append(row); print(json.dumps({'case_id':row['case_id'],'valid':valid,'action':decoded.get('action') if decoded else None,'elapsed_ms':row['elapsed_ms'],'exit_code':proc.returncode},ensure_ascii=False),flush=True)
 assert len(rows)==30
 valid=sum(x['valid'] for x in rows)
 summary={'n':30,'valid':valid,'invalid':30-valid,'dataset_sha256':hashlib.sha256(CASES.read_bytes()).hexdigest(),'system_sha256':system_hash,
          'hard_scored_n':sum(x['expected_status']!='hold' for x in rows),'exact_correct':sum(x['exact_correct'] is True for x in rows),
          'acceptable_set_correct':sum(x['acceptable_set_correct'] for x in rows),'coverage_non_abstain_valid':sum(x['valid'] and x['output']['action']!='abstain' for x in rows),
          'latency_p50_ms':pct(times,.5),'latency_p95_ms':pct(times,.95),'total_wall_seconds':round(sum(times)/1000,2),
          'execution_path':'Codex CLI, each item isolated in a separate ephemeral read-only invocation; system policy supplied as user-task instructions, so wrapper context differs from direct llama.cpp system-role prompt.',
          'observations':rows}
 (ROOT/'results-luna-codex.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print('SUMMARY '+json.dumps({k:v for k,v in summary.items() if k!='observations'},ensure_ascii=False),flush=True)
if __name__=='__main__': main()
