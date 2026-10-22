# ruff: noqa
import random, hashlib, csv, sys
random.seed(7)
N_REV=int(sys.argv[1]); PER=10
syl=[chr(c) for c in range(0xAC00,0xAC00+2000)]
ko=[''.join(random.choice(syl) for _ in range(random.randint(2,4))) for _ in range(60000)]
en=[''.join(random.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(random.randint(3,10))) for _ in range(60000)]
vocab=ko+en
w=[1/(i+1)**1.05 for i in range(len(vocab))]
random.shuffle(vocab)
import itertools
cum=list(itertools.accumulate(w))
def words(n): return random.choices(vocab,cum_weights=cum,k=n)
rf=open('/tmp/kbv2/rev.csv','w',newline=''); pf=open('/tmp/kbv2/pas.csv','w',newline='')
rw=csv.writer(rf); pw=csv.writer(pf)
import uuid
for r in range(N_REV):
    paras=[' '.join(words(random.randint(80,160))) for _ in range(PER)]
    body='\n\n'.join(paras)
    rid=str(uuid.uuid4())
    rw.writerow([rid, body, hashlib.sha256(body.encode()).hexdigest(), len(body)])
    pos=0
    for i,p in enumerate(paras):
        s=body.index(p,pos); e=s+len(p); pos=e
        an=' '.join(t for t in p.split() )
        pw.writerow([str(uuid.uuid4()), rid, i, p, s, e, hashlib.sha256(p.encode()).hexdigest(), an])
