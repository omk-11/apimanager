"""
POST /api/repos  — register a new repo
GET  /api/repos  — list all registered repos
POST /api/repos/{id}/trigger — start a pipeline run for a repo
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

import backend.db as db
from backend.orchestrator import run_pipeline

router = APIRouter(prefix="/api/repos", tags=["repos"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RepoCreate(BaseModel):
    name: str
    github_repo: str        # "owner/repo"
    github_token: str
    repo_path: str          # local FS path to the checked-out repo
    spec_old_path: str      # path to old OpenAPI spec JSON
    spec_new_path: str      # path to new OpenAPI spec JSON


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_repos():
    """Return all registered repos, newest first."""
    repos = db.get_repos()
    # Never expose the token to the client
    for r in repos:
        r.pop("github_token", None)
    return repos


@router.post("", status_code=201)
def create_repo(body: RepoCreate):
    """Register a new repo and return it (token redacted)."""
    repo = db.create_repo(
        name=body.name,
        github_repo=body.github_repo,
        github_token=body.github_token,
        repo_path=body.repo_path,
        spec_old_path=body.spec_old_path,
        spec_new_path=body.spec_new_path,
    )
    repo.pop("github_token", None)
    return repo


@router.post("/{repo_id}/trigger", status_code=202)
def trigger_run(repo_id: int, background_tasks: BackgroundTasks):
    """
    Create a run record and kick off the pipeline in a background thread.
    Returns the new run ID immediately so the client can poll for status.
    """
    repo = db.get_repo(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repo not found")

    run = db.create_run(repo_id)
    background_tasks.add_task(run_pipeline, run["id"], repo)
    return {"run_id": run["id"], "status": run["status"]}
