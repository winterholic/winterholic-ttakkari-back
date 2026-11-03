"""Run-owned PG/API for current UI acceptance. Public test credentials, L2 disabled, no production settings."""
import asyncio
import importlib.util
import os
from pathlib import Path

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
os.environ['KB_REPO_ROOT']=str(REPO)
os.environ['TTAKKARI_BACK']=str(REPO)
spec=importlib.util.spec_from_file_location('own',REPO.parent/'back-kb-v3-api/review/kb-v3/original-e2e-20261026/start_owned_backend.py')
own=importlib.util.module_from_spec(spec);spec.loader.exec_module(own)
url=own.owned_database_url()
spec=importlib.util.spec_from_file_location('live',REPO.parent/'front-kb-v3/scripts/kb-live/serve_backend.py')
live=importlib.util.module_from_spec(spec);spec.loader.exec_module(live)
live.configure_env(url,'http://127.0.0.1:5287',HERE/'.runtime')
os.environ['TTAKKARI_PUBLIC_SHARE_ORIGIN']='http://127.0.0.1:5287'
from ttakkari.config import get_settings

get_settings.cache_clear()
async def main():
 from alembic.config import Config

 from alembic import command
 await asyncio.to_thread(command.upgrade,Config(str(REPO/'alembic.ini')),'head')
 from argon2 import PasswordHasher

 from ttakkari.db import sessionmaker
 from ttakkari.models import Credential
 from ttakkari.security import passwords
 passwords._hasher=PasswordHasher(time_cost=1,memory_cost=8,parallelism=1)
 async with sessionmaker()() as db:
  if await db.get(Credential,1) is None:
   db.add(Credential(id=1,password_hash=passwords.hash_password(live.TEST_PASSWORD)))
   await db.commit()
 import uvicorn

 from ttakkari.main import app
 server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8897,log_level='warning'))
 stop=HERE/'server.stop';stop.unlink(missing_ok=True)
 async def control():
  while not stop.exists():await asyncio.sleep(0.2)
  server.should_exit=True
 task=asyncio.create_task(control())
 print('UI_ONLY_API port8897 L2disabled embedderhash ownRunDB',flush=True)
 try:await server.serve()
 finally:
  task.cancel();await asyncio.gather(task,return_exceptions=True)
asyncio.run(main())
