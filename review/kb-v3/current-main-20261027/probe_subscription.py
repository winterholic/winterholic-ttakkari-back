"""Synthetic, bounded real L2 call with this engine's inherited account; no auth file inspection."""
import asyncio, json, os, re, tempfile, uuid
from pathlib import Path
root=Path(__file__).resolve().parent
base=root/'.probe-runtime';base.mkdir(exist_ok=True)
os.environ.update(TTAKKARI_NO_ENV_FILE='1',TTAKKARI_ENV='dev',TTAKKARI_HOME=str(base.resolve()),TTAKKARI_DATABASE_URL='postgresql+asyncpg://unused@127.0.0.1:1/none')
from ttakkari.kb_pipeline import subscription_transport as transport
from ttakkari.kb_pipeline.extract_l2 import L2_SCHEMA
from ttakkari.runs import desk
from ttakkari.runs.profiles import Profile
original=desk._ask
async def observed(*args,**kwargs):
    result=await original(*args,**kwargs)
    error=result.error or ''
    error=re.sub(r'(?:postgres(?:ql)?[^\s]*|(?:sk-|Bearer )[A-Za-z0-9_.-]+)','[REDACTED]',error)
    (root/'l2-attempt.json').write_text(json.dumps({'elapsed_ms':result.elapsed_ms,'text_present':result.text is not None,'error':error},ensure_ascii=False,indent=2))
    return result
desk._ask=observed
async def main():
    profile=Profile(uuid.uuid4(),'inherited-personal-codex','codex',Path(os.environ['CODEX_HOME']),pool='personal')
    prompt='[제목 / 헤딩]: Redis\n[패시지 본문]:\nRedis stores key-value data in memory. Redis supports disk persistence using AOF logs.'
    try:
        result=await transport.execute_subscription_l2_call(profile,prompt,L2_SCHEMA,transport.SubscriptionCallConfig('codex','gpt-6-luna','high',120))
    except transport.L2TransportError as e:
        result={'state':'unavailable','diagnosis':e.diagnosis,'consumed':e.consumed}
    else: result={'state':'available','extraction':result}
    (root/'l2-real-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())
