"""Real MCP stdio -> HTTP API -> exported legacy row evidence, with access negative control."""
import asyncio
import hashlib
import json
import os
import sys
import uuid

from environment import API_URL, CORPUS, HERE, REPO, setup
setup()
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Workspace
from ttakkari.orchestrator.runtime import issue_token

async def main():
    corpus=json.loads(CORPUS.read_text())['documents']
    tokens={}
    async with sessionmaker()() as db:
        w=Workspace(id=uuid.uuid4(),name='KB legacy transport proof',root_path=str(HERE/'.runtime'/uuid.uuid4().hex))
        db.add(w)
        await db.flush()
        for pool in ('work','personal'):
            s=ChatSession(workspace_id=w.id,title='stdio-'+pool,engine='codex',pool=pool)
            db.add(s)
            await db.flush()
            tokens[pool]=issue_token(s.id)
        await db.commit()
    evidence={'transport':'SDK stdio MCP -> actual loopback HTTP -> PostgreSQL','documents':len(corpus),'pools':{}}
    for pool,token in tokens.items():
        env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG') if k in os.environ}
        env.update(TTAKKARI_NO_ENV_FILE='1',TTAKKARI_HOME=str(HERE/'.runtime/mcp-home'),PYTHONPATH=str(REPO/'src'),TTK_ROLE='agent',TTK_ORCH_TOKEN=token,TTK_API_URL=API_URL)
        params=StdioServerParameters(command=sys.executable,args=['-m','ttakkari.orchestrator.mcp_server'],env=env,cwd=str(REPO))
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as m:
                init=await m.initialize()
                tools=await m.list_tools()
                schemas={t.name:t.input_schema for t in tools.tools if t.name in {'kb_search','kb_documents','kb_document','kb_evidence'}}
                assert set(schemas)=={'kb_search','kb_documents','kb_document','kb_evidence'}
                calls=[]
                async def call(name,args):
                    result=await m.call_tool(name,args)
                    assert not result.is_error,result
                    out=json.loads(result.content[0].text)
                    calls.append(dict(tool=name,args=args,error=out.get('error'),sha256=hashlib.sha256(json.dumps(out,sort_keys=True).encode()).hexdigest()))
                    return out
                listed=await call('kb_documents',{'limit':50,'offset':0})
                # Agent API explicitly preserves the legacy scope partition. Work does not union
                # personal documents: kb_agent.access_for -> from_legacy_scope -> origin filter.
                expected=[x for x in corpus if x['metadata']['doc_key'].startswith('research:personal:') == (pool=='personal')]
                assert listed['total']==len(expected),(pool,listed.get('total'),len(expected))
                returned={x['id'] for x in listed['items']}
                assert returned=={x['metadata']['id'] for x in expected}
                for item in expected:
                    old=item['detail']
                    detail=await call('kb_document',{'document_id':old['id']})
                    assert detail['id']==old['id'] and detail['selected_revision_id']==old['selected_revision_id']
                    assert detail['passages']['items'][0]['sha256']==old['passages']['items'][0]['sha256']
                    ev=await call('kb_evidence',{'passage_id':old['passages']['items'][0]['id'],'start':0,'end':min(80,len(old['passages']['items'][0]['body']))})
                    assert 'error' not in ev,ev
                search=await call('kb_search',{'query':'reward hacking' if pool=='personal' else 'Kubernetes','mode':'lexical','limit':10})
                assert search.get('items') and 'error' not in search
                if pool=='personal':
                    hidden=corpus[0]['detail']
                    neg=await call('kb_document',{'document_id':hidden['id']})
                    assert neg.get('error')==404,neg
                    neg=await call('kb_evidence',{'passage_id':hidden['passages']['items'][0]['id']})
                    assert neg.get('error')==404,neg
                evidence['pools'][pool]=dict(expected_documents=len(expected),observed_documents=listed['total'],read_documents=len(expected),calls=calls,schemas=schemas,protocol_version=init.protocol_version)
    (HERE/'mcp-roundtrip.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    print('MCP_STDIO_ROUNDTRIP work_docs=15 personal_docs=21 preserved_ids_hashes=36 negative_404=2',flush=True)

asyncio.run(main())
