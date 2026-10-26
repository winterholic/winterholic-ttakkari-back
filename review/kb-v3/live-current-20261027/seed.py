import asyncio,json
from pathlib import Path
import httpx
HERE=Path(__file__).resolve().parent
async def main():
 async with httpx.AsyncClient(base_url='http://127.0.0.1:8897',timeout=30) as c:
  r=await c.post('/api/auth/login',json={'password':'correct-horse-battery'});r.raise_for_status();h={'Authorization':'Bearer '+r.json()['access_token']}
  data=[]
  bodies=[('Kubernetes networking','Kubernetes schedules containers on cluster nodes. Kubernetes manages Pods through Deployments. A Service routes requests to Pods. PostgreSQL stores relational data.'),('Kubernetes 한국어 안내','쿠버네티스(Kubernetes)는 컨테이너를 관리합니다. Deployment는 ReplicaSet을 관리합니다. Service는 Pod의 네트워크 접근을 제공합니다.')]
  for i,(title,body) in enumerate(bodies):
   r=await c.post('/api/assistant/kb/documents',headers=h,json={'title':title,'text':body,'source_key':f'ui-only-20261027-{i}','lang':'en' if i==0 else 'ko'})
   r.raise_for_status();data.append(r.json())
  for _ in range(50):
   r=await c.get('/api/assistant/kb/jobs',headers=h);r.raise_for_status();j=r.json()
   jobs=j.get('items',j.get('jobs',[]))
   if jobs and all(x.get('status') in ['ready','degraded','failed','held','cancelled'] for x in jobs):break
   await asyncio.sleep(.3)
  report={'ingest':data,'jobs':j,'purpose':'UI acceptance; L2 disabled/hash embedder, no quality approval'}
  (HERE/'seed.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
  print('UI_ONLY_SEED',len(data),'documents','jobs',[(x.get('status'),x.get('hold_reason')) for x in jobs])
asyncio.run(main())
