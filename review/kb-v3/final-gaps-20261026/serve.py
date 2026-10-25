import asyncio
import os
from environment import HERE, setup
setup()

async def main():
    from argon2 import PasswordHasher
    from sqlalchemy import select
    from ttakkari.db import sessionmaker
    from ttakkari.models import Credential
    from ttakkari.security import passwords
    passwords._hasher=PasswordHasher(time_cost=1,memory_cost=8,parallelism=1)
    async with sessionmaker()() as db:
        credential=await db.scalar(select(Credential).where(Credential.id==1))
        if credential is None:
            db.add(Credential(id=1,password_hash=passwords.hash_password('correct-horse-battery')))
            await db.commit()
    from ttakkari.main import app
    import uvicorn
    print('REAL_E5_TEST_SERVER port=8819 l2=disabled',flush=True)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8819,log_level='warning'))
    stop=HERE/'server.stop'
    stop.unlink(missing_ok=True)
    (HERE/'server.pid').write_text(str(os.getpid())+'\n')
    async def shutdown_control():
        while not stop.exists():
            await asyncio.sleep(0.2)
        server.should_exit=True
    watcher=asyncio.create_task(shutdown_control())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)

asyncio.run(main())
