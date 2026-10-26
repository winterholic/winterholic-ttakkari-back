"""Run the committed real-PG regressions without pytest's table-reset fixtures."""
import asyncio
import runpy
from environment import REPO, setup
setup()

async def main():
    ns=runpy.run_path(str(REPO/'tests/test_kb_v3_identity_concurrency.py'))
    failed=0
    for name,fn in ns.items():
        if name.startswith('test_'):
            try:
                await fn()
            except Exception as exc:
                print('FAIL',name,type(exc).__name__,str(exc)[:1200],flush=True)
                failed+=1
            else:
                print('PASS',name,flush=True)
    print('IDENTITY_REGRESSIONS',f'failed={failed}',flush=True)
    return failed

raise SystemExit(asyncio.run(main()))
