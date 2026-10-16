import json, sys
from collections import Counter, defaultdict
rows=[json.loads(l) for l in open(sys.argv[1])]
by=defaultdict(lambda:[0,0])
for r in rows:
    by[r['category'].split('_')[0] if not r['category'].startswith('state') else r['category']][0]+= (r['exact'] is True)
    by[r['category'].split('_')[0] if not r['category'].startswith('state') else r['category']][1]+=1
print({k:f'{a}/{b}' for k,(a,b) in sorted(by.items())})
for sp in 'AB':
    sub=[r for r in rows if r['split']==sp]
    print(sp, sum(r['exact'] is True for r in sub), len(sub))
print(Counter((r['gold'],r.get('action')) for r in rows))
if len(sys.argv)>2:
    for r in rows:
        if r['exact'] is not True: print(r['case_id'],r['category'],r['gold'],'->',r.get('action'), (r.get('trace') or {}).get('kind'), repr((r.get('trace') or {}).get('evidence'))[:60])
