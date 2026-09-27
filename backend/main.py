"""
FastAPI app entrypoint.

Mounts:
  /api/repos  →  routers/repos.py
  /api/runs   →  routers/runs.py
  /           →  frontend/ static files (index.html fallback)

Startup:
  - Calls db.init_db() to create tables if they don't exist.
  - Loads environment variables from a .env file if python-dotenv is installed.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import backend.db as db
from backend.routers import repos as repos_router
from backend.routers import runs as runs_router
from backend.spec_watcher import check_for_update, WATCH_INTERVAL
from backend.orchestrator import run_pipeline

# ---------------------------------------------------------------------------
# Optional .env loading (python-dotenv; silently skipped if not installed)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Self-Maintaining APIs",
    description="Watches for breaking Stripe API changes and auto-patches customer codebases.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup() -> None:
    db.init_db()
    asyncio.create_task(_watch_loop())


async def _watch_loop() -> None:
    """
    Background task that wakes every WATCH_INTERVAL seconds, checks every
    watched repo for a spec update, and auto-triggers the pipeline if the
    spec has changed.
    """
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        repos = db.get_watched_repos()
        for repo in repos:
            try:
                changed = await asyncio.to_thread(check_for_update, repo)
                if changed:
                    # Re-fetch repo so paths + SHA are current
                    fresh = db.get_repo(repo["id"])
                    if fresh:
                        run = db.create_run(fresh["id"])
                        await asyncio.to_thread(run_pipeline, run["id"], fresh)
            except Exception as exc:
                print(f"[watcher] unhandled error for repo {repo['id']}: {exc}")


# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------

app.include_router(repos_router.router)
app.include_router(runs_router.router)

# ---------------------------------------------------------------------------
# Frontend static files
# ---------------------------------------------------------------------------

_FRONTEND = Path(__file__).parent.parent / "frontend"

# Mount /static so JS/CSS can be fetched by exact path
app.mount("/static", StaticFiles(directory=str(_FRONTEND)), name="static")


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_frontend(full_path: str = ""):
    """
    Serve index.html for any non-API path (SPA fallback).
    Allows direct URL navigation in the dashboard.
    """
    # Let actual static file requests through
    candidate = _FRONTEND / full_path
    if candidate.is_file():
        return FileResponse(str(candidate))
    index = _FRONTEND / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"detail": "Frontend not found — run Session 7 to build it."}
