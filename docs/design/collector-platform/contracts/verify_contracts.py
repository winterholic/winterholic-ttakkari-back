"""Design artifact checker, not product runtime code. jsonschema Draft202012 required."""
import hashlib,json
from pathlib import Path
from datetime import datetime
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
P=Path(__file__).resolve().parent

def parse_time(t):
    d=datetime.fromisoformat(t.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timezone missing')
    return d

def semantic(x):
    if x['normalized'] is not None:
        body=x['normalized']['text']
        if hashlib.sha256(body.encode()).hexdigest()!=x['normalized']['sha256']:raise ValueError('normalized.sha256 text mismatch')
    if 'fixture:raw_text' in x['extensions']:
        b=x['extensions']['fixture:raw_text'].encode()
        if hashlib.sha256(b).hexdigest()!=x['raw']['sha256'] or len(b)!=x['raw']['byte_size']:raise ValueError('raw.sha256/byte_size mismatch')
    start,end=x['source']['valid_from'],x['source']['valid_to']
    if start and end and parse_time(start)>=parse_time(end):raise ValueError('source.valid_to must be later')
    if parse_time(x['source']['collected_at'])>parse_time(x['recorded_at']):raise ValueError('recorded_at before collected_at')
    for key,a in x['quality'].items():
        if a['status']=='measured' and (a['scale']=='none' or (key=='relevance' and not a['query_id'])):raise ValueError(f'quality.{key} measured scale/query missing')
        if a['calibrated_probability'] is not None and a['status']!='measured':raise ValueError(f'quality.{key} probability without measurement')
    if x['fact_check_status'] in ('supported','refuted','conflicting') and not x['quality']['factuality']['evidence']:raise ValueError('factuality evidence missing')
    if x['decision']['state']=='accept' and x['rights']['robots'] in ('disallowed','unavailable'):raise ValueError('robots blocks acceptance')

def _num(v):
    # RFC 8785 3.2.2.3: ECMAScript Number.prototype.toString on the shortest round-trip digits.
    import math
    from decimal import Decimal
    if isinstance(v,bool) or not isinstance(v,(int,float)):raise TypeError(v)
    if isinstance(v,int):
        if abs(v)>2**53:raise ValueError('integer outside IEEE-754 exact range')
        return str(v)
    if not math.isfinite(v):raise ValueError('non-finite number')
    if v==0:return '0'
    sign='-' if v<0 else '';t=Decimal(repr(abs(v))).normalize().as_tuple()
    d=''.join(map(str,t.digits));k=len(d);n=t.exponent+k
    if k<=n<=21:out=d+'0'*(n-k)
    elif 0<n<=21:out=d[:n]+'.'+d[n:]
    elif -6<n<=0:out='0.'+'0'*(-n)+d
    else:
        e=n-1;out=d[0]+('.'+d[1:] if k>1 else '')+'e'+('+' if e>0 else '-')+str(abs(e))
    return sign+out

def jcs(o):
    if o is None or isinstance(o,bool):return json.dumps(o)
    if isinstance(o,(int,float)):return _num(o)
    if isinstance(o,str):return json.dumps(o,ensure_ascii=False)
    if isinstance(o,list):return '['+','.join(jcs(v) for v in o)+']'
    if isinstance(o,dict):
        keys=sorted(o,key=lambda k:k.encode('utf-16-be'))
        return '{'+','.join(json.dumps(k,ensure_ascii=False)+':'+jcs(o[k]) for k in keys)+'}'
    raise TypeError(type(o))

JCS_VECTORS=[(0.0,'0'),(-0.0,'0'),(1.0,'1'),(1e-7,'1e-7'),(1e21,'1e+21'),(1e20,'100000000000000000000'),(0.000001,'0.000001'),(333333333.3333333,'333333333.3333333'),(-1.5e-10,'-1.5e-10'),(4.5,'4.5'),(2e-3,'0.002')]

def payload_hash(x,spec):
    y=json.loads(json.dumps(x))
    for path in spec['idempotency']['excluded_paths']:
        keys=path.strip('/').split('/');cur=y
        for k in keys[:-1]:cur=cur[k]
        cur.pop(keys[-1],None)
    return hashlib.sha256(jcs(y).encode('utf-8')).hexdigest()

def derive_scope(auth):
    # Envelope access is never trusted; scope comes from the authenticated session/agent principal.
    return {'tenant_id':auth['tenant_id'],'scope':auth['session_pool'],'principal_ids':{auth['principal_id']}}

def run_intake(v,spec):
    for raw,want in JCS_VECTORS:assert _num(raw)==want,f'JCS number {raw!r}->{_num(raw)} != {want}'
    assert jcs({'b':1,'a':[True,None,'\u20ac'],'\u00e9':0.5})=='{"a":[true,null,"\u20ac"],"b":1,"\u00e9":0.5}'
    d=P/'fixtures/intake';expectations=json.loads((d/'expectations.json').read_text())
    assert set(f.name for f in d.glob('intake-*.json'))==set(expectations),'intake manifest mismatch'
    results=[]
    for name,want in expectations.items():
        case=json.loads((d/name).read_text());store={};got=[];job_seq=0
        for x in case['submissions']:
            try:v.validate(x);semantic(x)
            except (ValidationError,ValueError) as e:got.append('reject:contract');continue
            b=derive_scope(case['auth']);a=x['access']
            if a['tenant_id']!=b['tenant_id'] or a['scope']!=b['scope'] or set(a['principal_ids'])!=b['principal_ids']:
                got.append('reject:scope_binding_mismatch');continue
            key=(b['tenant_id'],b['scope'],x['source']['key'],case['request_key']);h=payload_hash(x,spec)
            if key not in store:job_seq+=1;store[key]=(h,job_seq);got.append('created')
            elif store[key][0]==h:got.append('replay')
            else:got.append('conflict:409')
        assert got==want,f'{name}: expected {want}, got {got}'
        assert job_seq==(1 if 'created' in want else 0),f'{name}: job rows {job_seq}'
        results.append({'file':'intake/'+name,'expected':'|'.join(want),'actual':'|'.join(got),'reason':case['description'],'negative':any(g.startswith(('reject','conflict')) for g in got)})
    return results

def main():
    schema=json.loads((P/'ingestion-envelope.schema.json').read_text());Draft202012Validator.check_schema(schema)
    v=Draft202012Validator(schema,format_checker=FormatChecker())
    expectations=json.loads((P/'fixtures/expectations.json').read_text());results=[]
    files=list((P/'fixtures').glob('valid-*.json'))+list((P/'fixtures').glob('invalid-*.json'))
    assert set(f.name for f in files)==set(expectations),'fixture manifest mismatch'
    for name,want in expectations.items():
        value=json.loads((P/'fixtures'/name).read_text());reason=None
        try:v.validate(value);semantic(value)
        except (ValidationError,ValueError) as e:reason=str(e).split('\n')[0]
        got='reject' if reason else 'accept'
        assert got==want,f'{name}: expected {want}, got {got}: {reason}'
        results.append({'file':name,'expected':want,'actual':got,'reason':reason})
    bs=json.loads((P/'budget-policy.schema.json').read_text());Draft202012Validator.check_schema(bs);bv=Draft202012Validator(bs,format_checker=FormatChecker())
    for f in sorted((P/'fixtures').glob('budget-*.json')):
        x=json.loads(f.read_text());reason=None
        try:
            bv.validate(x)
            for k in ('tokens','calls','usd'):
                if x['job'][k]>x['day'][k]:raise ValueError('job.'+k+' exceeds day')
            if sum(x['request'].values())>x['job']['tokens']:raise ValueError('request exceeds job.tokens')
        except (ValidationError,ValueError) as e:reason=str(e).split('\n')[0]
        want='reject' if f.name.startswith('budget-invalid') else 'accept';got='reject' if reason else 'accept'
        assert want==got,f'{f.name}: expected {want}, got {got}'
        results.append({'file':f.name,'expected':want,'actual':got,'reason':reason})
    spec=json.loads((P/'intake-binding.json').read_text());intake=run_intake(v,spec)
    (P.parent/'evidence/contract-results.json').write_text(json.dumps(results+intake,ensure_ascii=False,indent=2)+'\n')
    print(f'schema meta-validation: 2 PASS; fixture expectations: {len(results)} PASS')
    print(f'positive={sum(r["actual"]=="accept" for r in results)} negative={sum(r["actual"]=="reject" for r in results)}; hash/calibration/query/time/scope/unknown-setting controls checked')
    print(f'intake binding/idempotency: {len(intake)} PASS (negative={sum(r["negative"] for r in intake)}: tenant/principal/scope tamper, cross-scope replay, 409 conflicts; replay=key-order+recorded_at); JCS vectors={len(JCS_VECTORS)} PASS')
    print('runtime budget/SSRF/DB/lease enforcement: NOT EXECUTED (design only)')

if __name__=='__main__':main()
