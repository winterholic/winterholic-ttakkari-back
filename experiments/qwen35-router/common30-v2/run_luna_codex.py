"""One-pass Luna replay using the installed Codex CLI; no file or tool actions by the model."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[2]
CASES=ROOT/'common30-v2.jsonl'; SYS=ROOT/'system-prompt-v2.txt'; SCHEMA=ROOT/'luna-output-schema.json'
CLI=Path('/opt/homebrew/bin/codex'); WORK=Path('/private/tmp/qwen35-router/luna-eval')
REPETITIONS=3
MODEL_MAX_OUTPUT_TOKENS=1024

def pct(xs,p):
 x=sorted(xs); return round(x[max(0,min(len(x)-1,math.ceil(p*len(x))-1))],2) if x else None

def expand_cases(cases, repetitions=REPETITIONS):
 return [{**case,'repetition':repetition} for repetition in range(1,repetitions+1) for case in cases]

def summarize_instability(rows):
 grouped={}
 for row in rows:
  if row.get('scored',True) and row.get('valid') and row.get('output'):
   action=row['output'].get('action')
   if action is not None: grouped.setdefault(row['case_id'],[]).append(action)
 analyzed=sorted(case_id for case_id,actions in grouped.items() if len(actions)==REPETITIONS)
 unstable=sorted(case_id for case_id in analyzed if len(set(grouped[case_id]))>1)
 return {'analyzed_case_count':len(analyzed),'unstable_case_count':len(unstable),'unstable_case_ids':unstable}

def is_app_server_init_eperm(row):
 stderr=row.get('stderr') or ''
 return ('failed to initialize in-process app-server client' in stderr and
         'Operation not permitted (os error 1)' in stderr)

def run_cases(cases, attempt, environ=None):
 env=os.environ if environ is None else environ
 if env.get('CODEX_SANDBOX') == 'seatbelt':
  return {'status':'blocked_preflight','failure_category':'nested_codex_sandbox','rows':[]}
 rows=[]
 for case in cases:
  row=attempt(case)
  if is_app_server_init_eperm(row):
   row.update({'execution_status':'blocked_initialization','failure_category':'codex_app_server_init_eperm',
               'scored':False,'valid':False,'exact_correct':None,'acceptable_set_correct':None})
   rows.append(row)
   return {'status':'blocked_initialization','failure_category':'codex_app_server_init_eperm','rows':rows}
  row.setdefault('execution_status','completed')
  row.setdefault('scored',True)
  rows.append(row)
 return {'status':'completed','failure_category':None,'rows':rows}

def main(output_path=None):
 cases=[json.loads(x) for x in CASES.read_text().splitlines() if x.strip()]
 benchmark_cases=expand_cases(cases)
 system=SYS.read_text(); system_hash=hashlib.sha256(system.encode()).hexdigest(); times=[]
 def attempt(case):
  WORK.mkdir(parents=True,exist_ok=True)
  context=f"[작업판]\n{case['context']['board']}\n[최근 대화]\n{case['context']['history']}\n[사용자 메시지]\n{case['message']}"
  request={'candidates':{'lead_ids':[],'model_ids':[],'tool_ids':[],'permission_ids':[]},'request':context}
  prompt=(system+"\n\n분류할 입력은 다음 JSON 하나다. 도구·파일·네트워크를 사용하지 말고 라우팅만 판정해라. 출력은 지정된 JSON Schema에 맞춰라.\n"+
          json.dumps(request,ensure_ascii=False,separators=(',',':')))
  prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(); out=WORK/f"{case['case_id']}-r{case['repetition']}.json"
  argv=[str(CLI),'exec','--ignore-user-config','--ephemeral','--sandbox','read-only','--skip-git-repo-check',
        '--model','gpt-6-luna','-c','model_reasoning_effort="high"','-c',f'model_max_output_tokens={MODEL_MAX_OUTPUT_TOKENS}','--output-schema',str(SCHEMA),
        '--output-last-message',str(out),'-C',str(WORK),'-']
  out.unlink(missing_ok=True)
  start=time.perf_counter(); proc=subprocess.run(argv,input=prompt,text=True,capture_output=True,timeout=180,check=False); elapsed=(time.perf_counter()-start)*1000
  if proc.returncode == 0: times.append(elapsed)
  raw=out.read_text() if out.exists() else proc.stdout
  try:
   decoded=json.loads(raw); valid=True; error=None
   valid=decoded.get('action') in {'answer','delegate','abstain'} and decoded.get('lead_id') is None and decoded.get('model_id') is None and decoded.get('tool_ids')==[] and decoded.get('permission_ids')==[] and isinstance(decoded.get('reply'),str) and bool(decoded['reply'].strip()) and (decoded.get('abstain_reason') is None if decoded['action']!='abstain' else decoded.get('abstain_reason') in {'ambiguous','insufficient_context','unsafe'})
   if not valid: error='schema_or_membership_validation_failed'
  except json.JSONDecodeError as exc: decoded=None; valid=False; error=type(exc).__name__+': '+str(exc)[:300]
  row={'case_id':case['case_id'],'repetition':case['repetition'],'expected_status':case['gold_status'],'allowed_actions':case['allowed_actions'],
       'input_sha256':hashlib.sha256(context.encode()).hexdigest(),'prompt_sha256':prompt_hash,'system_sha256':system_hash,
       'model':'gpt-6-luna','reasoning_effort':'high','max_output_tokens':MODEL_MAX_OUTPUT_TOKENS,'cli_version':'codex-cli 0.162.1','elapsed_ms':round(elapsed,2),
       'exit_code':proc.returncode,'valid':valid,'exact_correct':(decoded.get('action')==case['gold_status'] if case['gold_status']!='hold' and valid else (None if case['gold_status']=='hold' else False)),
       'acceptable_set_correct':bool(valid and decoded.get('action') in case['allowed_actions']),'output':decoded,'raw_output':raw,
       'stdout':proc.stdout,'stderr':proc.stderr,'failure':error,
       'execution_status':'completed','scored':True}
  print(json.dumps({'case_id':row['case_id'],'valid':valid,'action':decoded.get('action') if decoded else None,'elapsed_ms':row['elapsed_ms'],'exit_code':proc.returncode},ensure_ascii=False),flush=True)
  return row
 result=run_cases(benchmark_cases,attempt)
 rows=result['rows']; scored=[x for x in rows if x.get('scored')]
 valid=sum(x['valid'] for x in scored)
 summary={'status':result['status'],'failure_category':result['failure_category'],'model':'gpt-6-luna','reasoning_effort':'high',
          'max_output_tokens':MODEL_MAX_OUTPUT_TOKENS,'dataset_cases_n':len(cases),'repetitions':REPETITIONS,
          'requested_n':len(benchmark_cases),'attempted_n':len(rows),
          'model_response_n':sum(x.get('exit_code')==0 for x in rows),'n':len(scored),'valid':valid,'invalid':len(scored)-valid,'dataset_sha256':hashlib.sha256(CASES.read_bytes()).hexdigest(),'system_sha256':system_hash,
          'hard_scored_n':sum(x['expected_status']!='hold' for x in scored),'exact_correct':sum(x['exact_correct'] is True for x in scored),
          'misclassified':[{'case_id':x['case_id'],'repetition':x['repetition'],'expected':x['expected_status'],'actual':x['output'].get('action') if x.get('output') else None} for x in scored if x['expected_status']!='hold' and x['exact_correct'] is not True],
          'acceptable_set_correct':sum(x['acceptable_set_correct'] is True for x in scored),'coverage_non_abstain_valid':sum(x['valid'] and x['output'] and x['output']['action']!='abstain' for x in scored),
          'latency_p50_ms':pct(times,.5),'latency_p95_ms':pct(times,.95),'total_wall_seconds':round(sum(times)/1000,2),
          'round_instability':summarize_instability(rows) if result['status']=='completed' else None,
          'execution_path':'Codex CLI, each repetition isolated in a separate ephemeral read-only invocation; system policy supplied as user-task instructions, so wrapper context differs from direct llama.cpp system-role prompt.',
          'observations':rows}
 target=Path(output_path) if output_path else ROOT/'results-luna-codex.json'
 target.parent.mkdir(parents=True,exist_ok=True)
 target.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
 print('SUMMARY '+json.dumps({k:v for k,v in summary.items() if k!='observations'},ensure_ascii=False),flush=True)
if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser()
 parser.add_argument('--output',type=Path)
 main(parser.parse_args().output)
