"""Import supported-API exports into an EMPTY own-Run 0071 database, upgrade to 0073.

Exact IDs, text, hashes and source metadata are preserved. This is an exported-row compatibility
test, not an operational full backup: raw blobs, access rows and observations weren't exported.
Clone-only access rows deliberately exercise personal and work migration boundaries.
"""
import asyncio
import hashlib
import json
from datetime import datetime

from environment import CORPUS, HERE, REPO, setup

URL = setup()
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def date(value):
    return datetime.fromisoformat(value)

async def main():
    corpus = json.loads(CORPUS.read_text())
    assert len(corpus['documents']) == corpus['expected_documents'] == 36
    e = create_async_engine(URL)
    async with e.connect() as c:
        assert await c.scalar(text("select to_regclass('kb_documents')")) is None, 'requires empty own-Run database'
    cfg = Config(str(REPO / 'alembic.ini'))
    await asyncio.to_thread(command.upgrade, cfg, '0071_library_platform_v2_merge')
    proof = []
    async with e.begin() as c:
        for item in corpus['documents']:
            d = item['detail']
            r = next(r for r in d['revisions'] if r['id'] == d['selected_revision_id'])
            s = r['source']
            ps = d['passages']['items']
            assert len(ps) == d['passages']['total'] == 1
            body = ps[0]['body']
            assert len(body) == r['char_count']
            assert digest(body) == r['text_sha256'] == ps[0]['sha256']
            await c.execute(text('INSERT INTO kb_documents(id,doc_key,title,lang,created_at,updated_at) VALUES(:id,:key,:title,:lang,:created,:updated)'),
                            dict(id=d['id'],key=d['doc_key'],title=d['title'],lang=d['lang'],created=date(d['created_at']),updated=date(d['updated_at'])))
            await c.execute(text('INSERT INTO kb_sources(id,source_key,kind,locator,sha256,byte_size,media_type,store_path,collected_at,origin_meta,created_at) VALUES(:id,:key,:kind,:locator,:sha,:bytes,:media,:store,:collected,\'{}\',:created)'),
                            dict(id=s['id'],key=s['source_key'],kind=s['kind'],locator=s['locator'],sha=s['sha256'],bytes=s['byte_size'],media=s['media_type'],store='not-exported/'+s['sha256'],collected=date(s['collected_at']),created=date(r['created_at'])))
            await c.execute(text('INSERT INTO kb_revisions(id,document_id,source_id,rev_no,parser_version,body,text_sha256,char_count,created_at) VALUES(:id,:doc,:src,:no,:parser,:body,:sha,:chars,:created)'),
                            dict(id=r['id'],doc=d['id'],src=s['id'],no=r['rev_no'],parser=r['parser_version'],body=body,sha=r['text_sha256'],chars=r['char_count'],created=date(r['created_at'])))
            for p in ps:
                await c.execute(text('INSERT INTO kb_passages(id,revision_id,ord,heading,body,char_start,char_end,sha256,created_at) VALUES(:id,:rev,:ord,:heading,:body,:start,:end,:sha,:created)'),
                                dict(id=p['id'],rev=r['id'],ord=p['ord'],heading=p['heading'],body=p['body'],start=p['char_start'],end=p['char_end'],sha=p['sha256'],created=date(r['created_at'])))
            scope = 'personal' if d['doc_key'].startswith('research:personal:') else 'work'
            await c.execute(text("INSERT INTO kb_document_access(document_id,tenant_id,scope,hidden) VALUES(:doc,'owner',:scope,false)"),dict(doc=d['id'],scope=scope))
            proof.append(dict(document_id=d['id'],revision_id=r['id'],passage_id=ps[0]['id'],body_sha256=digest(body),clone_scope=scope))
    await asyncio.to_thread(command.upgrade, cfg, 'head')
    async with e.connect() as c:
        for row in proof:
            actual=(await c.execute(text('SELECT r.body,r.text_sha256,p.body as passage,a.origin_scope,a.visibility FROM kb_documents d JOIN kb_revisions r ON r.document_id=d.id JOIN kb_passages p ON p.revision_id=r.id JOIN kb_document_access a ON a.document_id=d.id WHERE d.id=:id AND r.id=:rev AND p.id=:pid'),dict(id=row['document_id'],rev=row['revision_id'],pid=row['passage_id']))).mappings().one()
            assert digest(actual['body'])==digest(actual['passage'])==actual['text_sha256']==row['body_sha256']
            assert actual['origin_scope']==row['clone_scope']
            assert actual['visibility']==('global' if row['clone_scope']=='personal' else 'work_only')
        head=await c.scalar(text('SELECT version_num FROM alembic_version'))
    await e.dispose()
    result=dict(state='passed',backend_sha='6d158ba4f9c36a029f5099cf7d6dda05dc6de7f8',runtime_sha='3a6d69a524d0f86399cf496d34e77676d50d9d9f',schema=head,documents=len(proof),unchanged_rows=proof,corpus_sha256=hashlib.sha256(CORPUS.read_bytes()).hexdigest(),scope='supported-API exported text/IDs/source metadata; clone-only scopes; raw blobs and original access/observations not exported')
    (HERE/'legacy-migration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('LEGACY_0071_TO_HEAD',head,'documents=36 exact_text_ids_hashes=36 access_boundary=36',flush=True)

if __name__=='__main__':
    asyncio.run(main())
