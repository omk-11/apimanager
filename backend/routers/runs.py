"""
GET /api/runs           — list all runs (optionally filtered by ?repo_id=)
GET /api/runs/{id}      — get a single run by ID
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

import backend.db as db

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(repo_id: int | None = Query(default=None)):
    """
    Return runs newest-first.  Pass ?repo_id=N to filter to one repo.
    Stage output blobs are included in the response as parsed JSON.
    """
    runs = db.get_runs(repo_id=repo_id)
    return [_deserialise_blobs(r) for r in runs]


@router.get("/{run_id}")
def get_run(run_id: int):
    """Return a single run by ID, with stage output blobs parsed."""
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _deserialise_blobs(run)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _deserialise_blobs(run: dict) -> dict:
    """
    The stage*_out columns are stored as JSON strings in SQLite.
    Parse them into dicts before returning so the frontend gets real objects,
    not escaped strings.
    """
    import json
    out = dict(run)
    for col in ("stage1_out", "stage2_out", "stage3_out", "stage4_out", "stage5_out"):
        if out.get(col) and isinstance(out[col], str):
            try:
                out[col] = json.loads(out[col])
            except (ValueError, TypeError):
                pass  # leave as-is if it can't be parsed
    return out
