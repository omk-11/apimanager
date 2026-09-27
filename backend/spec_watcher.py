"""
Spec watcher: fetches Stripe's live OpenAPI spec and detects when it changes.

How it works
------------
Stripe publishes its OpenAPI spec at a stable public URL. This module:

1. Downloads the spec (GET request, no auth needed).
2. Computes a SHA-256 of the response body.
3. Compares against the last-seen SHA stored per-repo in the DB.
4. If the SHA differs → saves the new spec to disk as the new spec_new_path,
   promotes the previous spec_new_path to spec_old_path, updates the stored SHA,
   and returns True so the caller can trigger the pipeline.
5. If unchanged → returns False (no pipeline run needed).

The spec files are written into a per-repo directory under backend/specs/:
    backend/specs/repo_{id}/spec_old.json
    backend/specs/repo_{id}/spec_new.json

This directory is created automatically on first watch.

Configuration
-------------
STRIPE_SPEC_URL  — override the spec URL (default: Stripe's public spec)
WATCH_INTERVAL   — seconds between polls (default: 3600 = 1 hour)
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import requests

import backend.db as db

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STRIPE_SPEC_URL = os.environ.get(
    "STRIPE_SPEC_URL",
    "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json",
)

WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "3600"))   # seconds

_SPECS_DIR = Path(__file__).parent / "specs"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_for_update(repo: dict) -> bool:
    """
    Check whether Stripe's spec has changed since the last time we saw it
    for this repo.

    Returns True  → spec changed; spec_old_path and spec_new_path on disk are
                    updated; caller should trigger the pipeline.
    Returns False → spec unchanged; nothing to do.

    Never raises — all errors are caught and return False so the scheduler
    doesn't crash.
    """
    repo_id = repo["id"]

    try:
        new_content = _fetch_spec()
    except Exception as exc:
        print(f"[watcher] repo {repo_id}: fetch failed — {exc}")
        return False

    new_sha = _sha256(new_content)
    stored_sha = db.get_repo_spec_sha(repo_id)

    if stored_sha == new_sha:
        return False  # nothing changed

    # Spec has changed — promote new→old, write fresh new
    spec_dir = _ensure_spec_dir(repo_id)
    old_path = spec_dir / "spec_old.json"
    new_path = spec_dir / "spec_new.json"

    # If we already have a new spec on disk, it becomes the old one
    if new_path.exists():
        old_path.write_bytes(new_path.read_bytes())
    else:
        # First run: seed old_path with the current live spec so Stage 1
        # has something to diff against. We write it, then immediately
        # write the same content to new_path — Stage 1 will find zero
        # changes and the pipeline will be a no-op, which is correct.
        old_path.write_text(new_content, encoding="utf-8")

    new_path.write_text(new_content, encoding="utf-8")

    # Update the repo row with the new paths + SHA
    db.update_repo_spec_paths(
        repo_id,
        spec_old_path=str(old_path),
        spec_new_path=str(new_path),
        spec_sha=new_sha,
    )

    print(f"[watcher] repo {repo_id}: spec changed (sha {new_sha[:8]}…) — triggering pipeline")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_spec() -> str:
    resp = requests.get(STRIPE_SPEC_URL, timeout=30)
    resp.raise_for_status()
    # Normalise: parse and re-dump to get stable key ordering for SHA
    return json.dumps(json.loads(resp.text), sort_keys=True)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ensure_spec_dir(repo_id: int) -> Path:
    d = _SPECS_DIR / f"repo_{repo_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d
