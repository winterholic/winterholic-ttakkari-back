"""Local-only UI fixture. Real studio routes, isolated files, public DEV token.

Run with TTAKKARI_NO_ENV_FILE=1 and a separate TTAKKARI_HOME. Never production.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ttakkari.api.creative import router
from ttakkari.config import get_settings
from ttakkari.creative.render import legacy_loop
from ttakkari.security.auth import issue_access_token

assert not get_settings().is_prod
assert "creative-test-home" in str(get_settings().home)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(legacy_loop())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5187"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.include_router(router)


@app.get("/test-token")
async def token():
    return {"access_token": issue_access_token()[0]}
