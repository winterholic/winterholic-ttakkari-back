"""common30-v2 최종 측정(Qwen 4B + 하네스, 고정 30문항 x 3회 = 90건). 개발 중 실행 금지, 확정 구성으로 한 번만 실행한다.
행 형식은 기존 run_v2.py 와 같게(case_id, repeat, expected_status, allowed_actions, total_ms, output, exact_correct, acceptable_set_correct) 두고
하네스 trace(단계별 호출/토큰/지연)를 덧붙인다."""
import asyncio, csv, hashlib, json, os, sys, time
from collections import Counter, defaultdict
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H

OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
CASES = H.BACK / 'experiments/qwen35-router/common30-v2/common30-v2.jsonl'
CFG = H.Config(name='harness-v1', mode='staged', fewshot=2, prompt_v=2, ground=True, overlap=True)
EXPECT = {'dataset': '37b607c9679bba193f18578256a33f8477a3f36cf30e05283ae3ce32ac95b2b0',
          'base_system': '850eb694411d25fe0d51c3960fe778a63503a74c022becd8ef14701816b5c1bd',
          'schema_file': 'd451347a2f19835d61a27fb62667f2dd92164ff181a490481f70d7d2ff7d8e7d'}
sha = lambda b: hashlib.sha256(b).hexdigest()

async def main():
    ep = os.environ['QWEN_ENDPOINT']
    got = {'dataset': sha(CASES.read_bytes()), 'base_system': sha((CASES.parent / 'system-prompt-v2.txt').read_bytes()),
           'schema_file': sha((CASES.parent / 'luna-output-schema.json').read_bytes())}
    assert got == EXPECT, f'fixed-input SHA mismatch: {got}'
    schema_sha = sha(json.dumps(H.build_response_schema(H.EMPTY), sort_keys=True).encode())
    # 하네스 프롬프트(새 system prompt SHA 기록): kind 판정 system+few-shot, 응답 생성 system
    kind_prompt = {'system': H.SYSTEM_STAGED_V2, 'fewshot': [list(x) for x in H.FEWSHOT_V2], 'schema': H.STAGED_SCHEMA}
    reply_prompt = {'system': H.SYSTEM_REPLY, 'schema': H.REPLY_SCHEMA, 'delegate_reply': H.DELEGATE_REPLY, 'abstain_reply': H.ABSTAIN_REPLY}
    (OUT / 'harness-prompts.json').write_text(json.dumps({'kind': kind_prompt, 'reply': reply_prompt, 'config': vars(CFG)}, ensure_ascii=False, indent=2))
    prompt_sha = {'kind_system': sha(H.SYSTEM_STAGED_V2.encode()), 'kind_system_plus_fewshot': sha(json.dumps(kind_prompt, ensure_ascii=False, sort_keys=True).encode()),
                  'reply_system': sha(H.SYSTEM_REPLY.encode()), 'harness_prompts_file': sha((OUT / 'harness-prompts.json').read_bytes())}
    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    assert len(cases) == 30 and len({c['case_id'] for c in cases}) == 30
    rows = []; t_start = time.time(); load_start = os.getloadavg()
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        for rep in range(1, 4):
            for c in cases:
                prompt = f"[작업판]\n{c['context']['board']}\n[최근 대화]\n{c['context']['history']}\n[사용자 메시지]\n{c['message']}"
                row = {'case_id': c['case_id'], 'repeat': rep, 'expected_status': c['gold_status'], 'allowed_actions': c['allowed_actions'],
                       'input_sha256': sha(prompt.encode()), 'started_at_epoch': time.time()}
                try:
                    dec, raw, trace, ms = await H.route(client, ep, CFG, {'context': c['context'], 'message': c['message']})
                    row.update(valid=True, total_ms=round(ms, 2), raw_json=raw, output=dec, trace=trace,
                               exact_correct=(dec['action'] == c['gold_status']) if c['gold_status'] != 'hold' else None,
                               acceptable_set_correct=dec['action'] in c['allowed_actions'])
                except Exception as e:
                    row.update(valid=False, total_ms=None, failure_type=type(e).__name__, failure_detail=str(e)[:500],
                               exact_correct=False if c['gold_status'] != 'hold' else None, acceptable_set_correct=False)
                rows.append(row)
                print(json.dumps({k: row.get(k) for k in ('case_id', 'repeat', 'valid', 'total_ms')}), flush=True)
    keys = [(r['case_id'], r['repeat']) for r in rows]
    assert len(rows) == 90 and len(set(keys)) == 90, 'missing/duplicate rows'
    (OUT / 'qwen-harness-common30-v2-raw90.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
    with (OUT / 'qwen-harness-common30-v2-rows.csv').open('w', newline='') as f:
        w = csv.writer(f); w.writerow(['case_id', 'repeat', 'expected', 'allowed', 'action', 'valid', 'exact', 'acceptable', 'total_ms', 'kind', 'evidence', 'grounded', 'calls', 'prompt_tokens', 'completion_tokens', 'reply'])
        for r in rows:
            t = r.get('trace') or {}; calls = t.get('calls', [])
            w.writerow([r['case_id'], r['repeat'], r['expected_status'], '|'.join(r['allowed_actions']), (r.get('output') or {}).get('action'), r['valid'], r['exact_correct'],
                        r['acceptable_set_correct'], r['total_ms'], t.get('kind'), t.get('evidence'), t.get('grounded'), len(calls),
                        sum(c['tokens'].get('prompt_tokens', 0) for c in calls), sum(c['tokens'].get('completion_tokens', 0) for c in calls), (r.get('output') or {}).get('reply')])
    lat = [r['total_ms'] for r in rows if r['total_ms'] is not None]
    hard = [r for r in rows if r['expected_status'] != 'hold']
    conf = Counter((r['expected_status'], (r.get('output') or {}).get('action')) for r in rows)
    by_case = defaultdict(list)
    for r in rows: by_case[r['case_id']].append((r.get('output') or {}).get('action'))
    unstable = {k: v for k, v in by_case.items() if len(set(v)) > 1}
    per_round = {rep: {'exact': sum(r['exact_correct'] is True for r in rows if r['repeat'] == rep), 'hard_n': sum(r['expected_status'] != 'hold' for r in rows if r['repeat'] == rep),
                       'acceptable': sum(r['acceptable_set_correct'] for r in rows if r['repeat'] == rep)} for rep in (1, 2, 3)}
    mis = {f'{g}->{a}': n for (g, a), n in sorted(conf.items(), key=str) if g != 'hold' and g != a}
    tokens = {'prompt_tokens_total': sum(c['tokens'].get('prompt_tokens', 0) for r in rows for c in (r.get('trace') or {}).get('calls', [])),
              'completion_tokens_total': sum(c['tokens'].get('completion_tokens', 0) for r in rows for c in (r.get('trace') or {}).get('calls', [])),
              'cached_prompt_tokens_total': sum((c['tokens'].get('prompt_tokens_details') or {}).get('cached_tokens', 0) for r in rows for c in (r.get('trace') or {}).get('calls', [])),
              'model_calls': sum(len((r.get('trace') or {}).get('calls', [])) for r in rows)}
    summary = {'n': len(rows), 'unique_case_repeat': len(set(keys)), 'valid': sum(r['valid'] for r in rows), 'errors': sum(not r['valid'] for r in rows),
               'hard_n': len(hard), 'exact': sum(r['exact_correct'] is True for r in rows), 'acceptable': sum(r['acceptable_set_correct'] for r in rows),
               'exact_pct': round(100 * sum(r['exact_correct'] is True for r in rows) / len(hard), 1),
               'p50_ms': H.pct(lat, .5), 'p95_ms': H.pct(lat, .95), 'mean_ms': round(sum(lat) / len(lat), 1) if lat else None,
               'confusion': {f'{g}->{a}': n for (g, a), n in sorted(conf.items(), key=str)}, 'misroute_by_type': mis,
               'per_round': per_round, 'unstable_cases': unstable, 'tokens': tokens, 'fixed_input_sha256': got, 'response_schema_sha256_json_sorted': schema_sha,
               'harness_prompt_sha256': prompt_sha, 'config': vars(CFG), 'loadavg_start': load_start, 'loadavg_end': os.getloadavg(),
               'ready_seconds': float(os.environ.get('QWEN_READY_SECONDS', 0)), 'wall_seconds': round(time.time() - t_start, 1)}
    (OUT / 'qwen-harness-common30-v2-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print('SUMMARY', json.dumps(summary, ensure_ascii=False))

asyncio.run(main())
