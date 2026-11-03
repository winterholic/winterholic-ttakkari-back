"""Actual API -> automatic worker burst, 36 existing-source texts, after identity correction."""
import asyncio
import json
import time
import uuid
from collections import Counter

import httpx
from environment import API_URL, CORPUS, HERE, setup

setup()

async def main():
    corpus=json.loads(CORPUS.read_text())['documents']
    tag=uuid.uuid4().hex
    output={'purpose':'automatic concurrent batch after identity fix; L2 disabled, real E5','tag':tag,'inputs':[]}
    async with httpx.AsyncClient(base_url=API_URL,timeout=60) as c:
        version=await c.get('/api/version')
        version.raise_for_status()
        output['runtime']=version.json()
        login=await c.post('/api/auth/login',json={'password':'correct-horse-battery'})
        login.raise_for_status()
        headers={'Authorization':'Bearer '+login.json()['access_token']}
        async def register(i,item):
            d=item['detail']
            response=await c.post('/api/assistant/kb/documents',headers=headers,json={'title':d['title'],
                 'text':d['passages']['items'][0]['body'],'source_key':f'replay-{tag}-{i}',
                 'scope':'work' if i<15 else 'personal','lang':d['lang']})
            response.raise_for_status()
            return {'original_id':d['id'],'response':response.json()}
        # Simultaneous user registrations, bounded batches of four; automated server stages run independently.
        for start in range(0,len(corpus),4):
            output['inputs'].extend(await asyncio.gather(*(register(i,corpus[i]) for i in range(start,min(start+4,len(corpus))))))
        watched={row['response']['document_id'] for row in output['inputs']}
        deadline=time.monotonic()+240
        terminal={'ready','degraded','failed','held'}
        while True:
            all_jobs=[]
            for scope in ('personal','work'):
                r=await c.get('/api/assistant/kb/jobs',headers=headers,params={'scope':scope,'limit':100})
                r.raise_for_status()
                payload=r.json()
                assert payload['total']<=100,'pagination required'
                all_jobs.extend(payload['items'])
            jobs=[j for j in all_jobs if j['document_id'] in watched]
            if len(jobs)==36 and all(j['status'] in terminal for j in jobs):
                break
            if time.monotonic()>deadline:
                output['incomplete_jobs']=jobs
                (HERE/'replay-batch.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
                raise TimeoutError('automatic batch did not settle')
            await asyncio.sleep(1)
        output['jobs']=jobs
        details=[]
        for j in jobs:
            r=await c.get('/api/assistant/kb/jobs/'+j['id'],headers=headers,params={'scope':'work' if j.get('origin_scope')=='work' else 'personal'})
            if r.status_code==404:
                r=await c.get('/api/assistant/kb/jobs/'+j['id'],headers=headers,params={'scope':'work'})
            r.raise_for_status()
            details.append(r.json())
        output['details']=details
        output['statuses']=dict(Counter(j['status'] for j in jobs))
        (HERE/'replay-batch.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,default=str)+'\n')
        attempts=[j.get('attempt_count',0) for j in jobs]
        print('AUTO_BATCH',json.dumps({'count':len(jobs),'statuses':output['statuses'],'attempts':attempts}),flush=True)
        assert all(j['status']=='degraded' for j in jobs),output['statuses']
        assert all(n==1 for n in attempts),attempts
        print('AUTO_BATCH_NO_IDENTITY_RETRY 36/36 attempt=1; L2-disabled degraded disclosed',flush=True)

asyncio.run(main())
