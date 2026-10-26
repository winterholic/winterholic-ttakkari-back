"""Measured product HTTP retrieval over a legacy production corpus copy and frozen positive qrels.

No predictions, vectors, graph rows or assertions are mocked/inserted. Clone scopes preserve
API legacy partitions. Report failed requests and unjudged results rather than dropping them.
"""
import asyncio
import hashlib
import json
import math
import time
from collections import Counter
from statistics import median

import httpx
from environment import API_URL, CORPUS, HERE, setup
setup()
from sqlalchemy import text
from ttakkari.db import sessionmaker
from ttakkari.kb_eval.metrics import metrics
from ttakkari.kb_retrieval import model_key

async def inventory():
    async with sessionmaker()() as db:
        counts={}
        for table in ('kb_documents','kb_revisions','kb_passages','kb_entities','kb_relations','kb_assertions','kb_conflicts'):
            counts[table]=await db.scalar(text(f'SELECT count(*) FROM {table}'))
        counts['vectors']=await db.scalar(text('SELECT count(*) FROM kb_passage_vectors WHERE model_key=:key'),{'key':model_key()})
        jobs=await db.execute(text('SELECT status,count(*) FROM kb_pipeline_jobs GROUP BY status'))
        counts['jobs']=dict(jobs.all())
        counts['model_key']=model_key()
        return counts

async def main():
    qrels=json.loads((HERE/'qrels.json').read_text())
    cases=qrels['cases']
    original=json.loads(CORPUS.read_text())['documents']
    bodies={d['metadata']['id']:d['detail']['passages']['items'][0]['body'] for d in original}
    for c in cases:
        for anchor in c['anchors']:
            assert anchor['quote'] in bodies[anchor['document_id']]
    before=await inventory()
    deadline=time.monotonic()+240
    active={'registered','parsing','chunking','extracting','linking','indexing','canonicalizing'}
    while any(s in active for s in before['jobs']):
        if time.monotonic()>deadline:
            raise TimeoutError('automatic backfill jobs did not settle')
        await asyncio.sleep(1)
        before=await inventory()
    assert before['vectors']>0 and before['model_key'].startswith('intfloat/multilingual-e5-large:')
    result={'backend_sha':'6d158ba4f9c36a029f5099cf7d6dda05dc6de7f8','runtime_sha':'3a6d69a524d0f86399cf496d34e77676d50d9d9f',
            'corpus_sha256':hashlib.sha256(CORPUS.read_bytes()).hexdigest(),
            'qrels_sha256':hashlib.sha256((HERE/'qrels.json').read_bytes()).hexdigest(),
            'inventory':before,'qrels_method':qrels['method'],'runs':{},'limitations':[
                '35 agent-authored diagnostic questions, positive-only source-reviewed qrels; no user approval or user-query holdout.',
                'All 36 current personal-MCP-visible production documents copied; actual source raw blobs/access rows/observations not exported.',
                'Clone-only legacy scopes partition 15 Kubernetes and 21 research records. Queries run with actual matching HTTP scopes.',
                'L2 disabled in this test; graph coverage reflects L0/L1/backfill only. No synthetic assertion/graph rows inserted.',
                'Latency includes actual request-level E5 child load; one observation/query/mode, no throughput or significance claim.'
            ]}
    async with httpx.AsyncClient(base_url=API_URL,timeout=300) as client:
        auth=await client.post('/api/auth/login',json={'password':'correct-horse-battery'})
        auth.raise_for_status()
        headers={'Authorization':'Bearer '+auth.json()['access_token']}
        for mode in ('lexical','vector','hybrid'):
            predictions={}
            rows=[]
            for c in cases:
                params={'q':c['query'],'mode':mode,'limit':30,'current_only':'true'}
                if c['pool']=='work':
                    params['scope']='work'
                t=time.perf_counter()
                response=await client.get('/api/assistant/kb/search',params=params,headers=headers)
                wall=time.perf_counter()-t
                body=response.json()
                if response.status_code!=200:
                    predictions[c['id']]={'failure':f'http_{response.status_code}','ranked':[],'coverage':'unknown'}
                else:
                    ranked=list(dict.fromkeys(hit['document']['id'] for hit in body['items']))
                    paths=Counter(path for hit in body['items'] for path in hit.get('match_paths',[]))
                    predictions[c['id']]={'ranked':ranked,'candidates':ranked,'coverage':body.get('coverage','unknown')}
                rows.append({'id':c['id'],'query':c['query'],'scope':c['pool'],'http_status':response.status_code,
                             'latency_seconds':wall,'response':body})
                print('QUERY',mode,c['id'],response.status_code,round(wall,3),flush=True)
                (HERE/'search-progress.json').write_text(json.dumps({'mode':mode,'completed':len(rows),'total':len(cases)},indent=2)+'\n')
            at={str(k):metrics(cases,predictions,k=k) for k in (1,5,10)}
            times=sorted(r['latency_seconds'] for r in rows)
            result['runs'][mode]={'metrics':at,'requests':rows,'latency':{'median_seconds':median(times),'p95_seconds':times[math.ceil(.95*len(times))-1],'n':len(times)}}
            (HERE/'product-search-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
            print('MODE_SUMMARY',mode,json.dumps({k:{m:v[m] for m in ('recall','mrr','failed')} for k,v in at.items()}),flush=True)
    result['final_inventory']=await inventory()
    (HERE/'product-search-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print('PRODUCT_E5_COMPARISON',len(cases),'queries x 3 modes; output=product-search-results.json',flush=True)

asyncio.run(main())
