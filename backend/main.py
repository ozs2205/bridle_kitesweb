"""
Bridle - a lightweight FastAPI REST wrapper around the KITES RISC-V simulator's
interactive REPL (`kites-cli --repl`).

Architecture
------------
    HTTP client
        |
    routes.py      FastAPI endpoints  (+ handlers.py error envelope, models.py validation)
        |
    session.py     SessionRegistry / Session: session manager (timeouts, teardown)
        |          Session.command(): CLI wrapper (async stdin/stdout/stderr)
        |
    parsers.py     parse_regs / parse_info / summarize_*: parser layer (text -> JSON)
        |
    kites-cli --repl   (one subprocess + one private temp workspace per session)

    config.py      environment variables and constants
    errors.py      BridleError / ReplCrashed / OutputLimitExceeded

Run from this folder:
    python main.py            or        uvicorn main:app --port 8000
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import CORS_ORIGINS, FRONTEND_DIR, _env_int
from handlers import register_exception_handlers
from routes import router
from session import registry


@asynccontextmanager
async def lifespan(_: FastAPI):
    reaper_task = asyncio.create_task(registry.reaper())
    try:
        yield
    finally:
        reaper_task.cancel()
        try:
            await reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        await registry.shutdown()


app = FastAPI(
    title="Bridle",
    version="1.0.0",
    description="REST API around the KITES RISC-V simulator REPL.",
    lifespan=lifespan,
)

# The browser test page (index.html) calls this API from another origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(router)

# Mounted last so the API routes above take precedence over the static catch-all "/".
app.mount(
    "/",
    StaticFiles(directory=FRONTEND_DIR, html=True),
    name="frontend"
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BRIDLE_HOST", "127.0.0.1"),
        port=_env_int("BRIDLE_PORT", 8000),
    )
