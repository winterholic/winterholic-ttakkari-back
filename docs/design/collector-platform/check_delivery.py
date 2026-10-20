"""Verify the design package and captured PMS receipt, not the product implementation."""
import csv,json,re
from pathlib import Path
P=Path(__file__).resolve().parent

def verify(*,strict=False):
    required=['architecture.md','evaluation.md','corrections.md','first-slice.md','integration-map.md','summary.html','wbs.csv','contracts/ingestion-envelope.schema.json','contracts/budget-policy.schema.json','contracts/verify_contracts.py','intake-binding.md','contracts/intake-binding.json','contracts/fixtures/intake/expectations.json','evidence/contract-results.json','evidence/input-manifest.json','evidence/repository.txt','evidence/pms-registration.json','evidence/pms-readback.json']
    assert all((P/f).is_file() for f in required),'missing required package file'
    rows=list(csv.DictReader((P/'wbs.csv').open(encoding='utf-8-sig')))
    read=json.loads((P/'evidence/pms-readback.json').read_text())['tasks']
    assert len(rows)==len(read)==25,'WBS/readback row count mismatch'
    by={r['id']:r for r in rows};actual={r['id']:r for r in read}
    assert len(by)==len(rows) and set(by)==set(actual),'duplicate/missing IDs'
    ext={'87f921eb-7797-43cc-95e7-45139a4dc2f2'};visited=set();visiting=set()
    def visit(tid):
        if tid in ext:return
        assert tid in by,'unresolved predecessor '+tid
        assert tid not in visiting,'dependency cycle at '+tid
        if tid in visited:return
        visiting.add(tid)
        for parent in filter(None,by[tid]['predecessor_uuids'].split('|')):visit(parent)
        visiting.remove(tid);visited.add(tid)
    for tid in by:visit(tid)
    for row in rows:
        a=actual[row['id']]
        assert a['status']==row['status'],row['code']+' stale status'
        assert a['assignee']==row['owner_role'],row['code']+' stale owner'
        assert a['predecessors']==list(filter(None,row['predecessor_uuids'].split('|'))),row['code']+' stale deps'
        assert row['completion_criterion'] and row['verification_command_or_procedure'] and row['artifact_paths'],'missing execution specification'
        assert a['description'],'missing persisted PMS description'
        assert int(a['progress'])==int(row['progress']),row['code']+' stale progress'
        if row['stage']!='S1':
            assert row['status']!='done' and int(row['progress'])<100,'unexecuted implementation marked complete'
            # Only S2-00 (min gate recorded) and S2-01 (reworked, awaiting independent verification) may leave todo, and only as review, never done.
            if row['status']!='todo':assert row['code'] in ('CP-S2-00','CP-S2-01') and row['status']=='review','unexpected in-progress row '+row['code']
    if strict:assert sum(r['status']=='done' for r in rows)==3,'design close status missing'
    assert len([r for r in rows if r['stage']=='S1'])==3
    assert len([r for r in rows if r['stage']=='S2'])==11
    assert len([r for r in rows if r['stage']=='S3'])==11
    assertions=json.loads((P/'evidence/contract-results.json').read_text())
    env=[r for r in assertions if not r['file'].startswith('intake/')];intake=[r for r in assertions if r['file'].startswith('intake/')]
    assert len(env)==18 and all(r['actual']==r['expected'] for r in assertions),'fixture mismatch'
    assert sum(r['actual']=='reject' for r in env)==14,'negative controls missing'
    assert len(intake)==10 and sum(r['negative'] for r in intake)==7,'intake binding/idempotency controls missing'
    for name in ('tenant-tamper','principal-tamper','principal-escalation','scope-tamper'):
        assert any(name in r['file'] and r['actual']=='reject:scope_binding_mismatch' for r in intake),'scope tamper not rejected: '+name
    s01=by['48d0a2b4-09f5-47ab-a6ac-d86a92c249a5'];assert 'fd87eb4c-29df-47d1-ad3a-346a8c6260ba' in s01['predecessor_uuids'],'S2-00 gate missing'
    assert 'CP-S2-00' in actual[s01['id']]['description'] and 'JCS' in actual[s01['id']]['description'],'S2-01 PMS gate/contract missing'
    s02=by['7a90615c-2871-47ed-b0cb-75f74c73a132']
    assert 'CP-S2-02' in s01['completion_criterion'] and 'HTTP' in s02['completion_criterion'] and 'tests/test_collector_api.py' in s02['verification_command_or_procedure'],'HTTP route/403/409 tests not consistently moved S2-01 -> S2-02'
    assert 'CP-S2-02' in actual[s01['id']]['description'] and 'HTTP' in actual['7a90615c-2871-47ed-b0cb-75f74c73a132']['description'],'PMS HTTP boundary move missing'
    inv=json.loads((P.parent.parent.parent/'reports/collector/s2-00/inventory.json').read_text())
    assert inv['inventory_version']=='2.1.0' and re.fullmatch(r'[0-9a-f]{40}',inv['git']['head']) and inv['alembic']['output'].startswith('0059_collector_intake'),'S2-00 inventory is not the re-recorded v2'
    for f in list(P.glob('*.md'))+[P.parent.parent/'adr/collector-platform-20261020.md']:
        assert f.exists(),str(f)
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)',f.read_text()):
            if target.startswith(('https://','http://','#','/')):continue
            assert (f.parent/target.split('#')[0]).exists(),f'{f.name}: missing link {target}'
    assert '{{' not in (P/'summary.html').read_text(),'HTML template placeholder'
    print(f'design package: PASS; WBS/readback=25; stages=3/11/11; dependency DAG=PASS; external P0=1')
    print(f'fixture evidence=18 (4 positive/14 negative) + intake=10 (7 negative, tamper 4 rejected); S2-00 live=review {by["fd87eb4c-29df-47d1-ad3a-346a8c6260ba"]["progress"]}%, S2-01 live=review {by["48d0a2b4-09f5-47ab-a6ac-d86a92c249a5"]["progress"]}% (HTTP route moved to S2-02); local document links=PASS; S2-01 implementation=reworked, awaiting independent verification; ML/deploy=NOT EXECUTED')

if __name__=='__main__':
    import sys
    verify(strict='--closed' in sys.argv)
