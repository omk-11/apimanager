"""
SQLite persistence layer: repos + runs tables.

Design decisions:
- Two tables: `repos` (registered GitHub repositories) and `runs` (pipeline
  executions).  Both are append-only from the application's perspective — rows
  are never deleted through the API.
- Pipeline stage outputs are stored as JSON blobs rather than normalised columns.
  This keeps the schema stable across iterations and lets each stage store
  whatever the schema.py dataclasses currently describe.
- Thread safety: every public function creates its own connection with
  check_same_thread=False so the FastAPI async workers can call them freely.
  SQLite's default serialised write mode is enough for our write rate.

Usage:
    from backend.db import init_db, create_repo, get_repos, create_run, ...
    init_db()           # call once at startup
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Location of the SQLite file
# ---------------------------------------------------------------------------

_DB_PATH = Path(__file__).parent.parent / "pipeline_data.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS repos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,          -- human label, e.g. "my-service"
    github_repo    TEXT    NOT NULL,          -- "owner/repo"
    token_env_var  TEXT,                      -- env var name holding the token (e.g. GITHUB_TOKEN)
    repo_path      TEXT    NOT NULL,          -- local FS path to the checked-out repo
    spec_old_path  TEXT    NOT NULL,          -- path to old OpenAPI spec JSON
    spec_new_path  TEXT    NOT NULL,          -- path to new OpenAPI spec JSON
    spec_sha       TEXT,                      -- SHA-256 of last-seen spec (for change detection)
    auto_watch     INTEGER NOT NULL DEFAULT 1, -- 1 = watcher should poll this repo
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id        INTEGER NOT NULL REFERENCES repos(id),
    status         TEXT    NOT NULL DEFAULT 'pending',
    -- status values: pending | running | success | failed | error
    current_stage  INTEGER,                   -- 1-5, NULL when not yet started
    stage1_out     TEXT,                      -- JSON blob: SpecDiff (serialised)
    stage2_out     TEXT,                      -- JSON blob: ScanResult summary
    stage3_out     TEXT,                      -- JSON blob: PatchResult summary
    stage4_out     TEXT,                      -- JSON blob: TestResult
    stage5_out     TEXT,                      -- JSON blob: PRResult
    error_message  TEXT,                      -- populated on status='error'
    started_at     TEXT,
    finished_at    TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.executescript(_DDL)
        # Additive migrations: add new columns to existing tables if absent.
        _add_column_if_missing(conn, "repos", "spec_sha",     "TEXT")
        _add_column_if_missing(conn, "repos", "auto_watch",   "INTEGER NOT NULL DEFAULT 1")
        _add_column_if_missing(conn, "repos", "token_env_var","TEXT")


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, typedef: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
        conn.commit()


# ---------------------------------------------------------------------------
# Repos CRUD
# ---------------------------------------------------------------------------

def resolve_github_token(repo: dict[str, Any]) -> str:
    """
    Resolve the GitHub token for a repo at call time from the environment.
    The raw token is NEVER stored in SQLite.

    Resolution order:
      1. If repo["token_env_var"] is set, read os.environ[token_env_var].
      2. Fall back to os.environ["GITHUB_TOKEN"].
      3. Return "" if nothing is set (caller should treat as missing).
    """
    env_var = repo.get("token_env_var") or "GITHUB_TOKEN"
    return os.environ.get(env_var, "")


def create_repo(
    *,
    name: str,
    github_repo: str,
    token_env_var: str = "GITHUB_TOKEN",
    repo_path: str,
    spec_old_path: str,
    spec_new_path: str,
) -> dict[str, Any]:
    """Insert a new repo row and return it as a dict."""
    sql = """
        INSERT INTO repos (name, github_repo, token_env_var,
                           repo_path, spec_old_path, spec_new_path)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with _connect() as conn:
        cur = conn.execute(
            sql,
            (name, github_repo, token_env_var, repo_path, spec_old_path, spec_new_path),
        )
        conn.commit()
        return get_repo(cur.lastrowid)  # type: ignore[arg-type]


def get_repo(repo_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return dict(row) if row else None


def get_repos() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM repos ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Runs CRUD
# ---------------------------------------------------------------------------

def create_run(repo_id: int) -> dict[str, Any]:
    """Insert a new run row in 'pending' status and return it."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (repo_id, status) VALUES (?, 'pending')",
            (repo_id,),
        )
        conn.commit()
        return get_run(cur.lastrowid)  # type: ignore[arg-type]


def get_run(run_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_runs(repo_id: int | None = None) -> list[dict[str, Any]]:
    """Return all runs, optionally filtered by repo_id, newest first."""
    with _connect() as conn:
        if repo_id is not None:
            rows = conn.execute(
                "SELECT * FROM runs WHERE repo_id = ? ORDER BY id DESC",
                (repo_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def update_run_started(run_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status='running', started_at=datetime('now') WHERE id=?",
            (run_id,),
        )
        conn.commit()


def update_run_stage(run_id: int, stage: int, stage_blob: Any) -> None:
    """
    Mark the run as being in `stage` and persist that stage's output blob.
    `stage_blob` is any JSON-serialisable object (typically a dict built from
    the relevant schema.py dataclass).
    """
    col = f"stage{stage}_out"
    blob_json = json.dumps(stage_blob)
    with _connect() as conn:
        conn.execute(
            f"UPDATE runs SET current_stage=?, {col}=? WHERE id=?",  # noqa: S608
            (stage, blob_json, run_id),
        )
        conn.commit()


def update_run_finished(
    run_id: int,
    *,
    success: bool,
    error_message: str | None = None,
) -> None:
    status = "success" if success else ("error" if error_message else "failed")
    with _connect() as conn:
        conn.execute(
            """UPDATE runs
               SET status=?, error_message=?, finished_at=datetime('now')
               WHERE id=?""",
            (status, error_message, run_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Spec watcher helpers
# ---------------------------------------------------------------------------

def get_repo_spec_sha(repo_id: int) -> str | None:
    """Return the last-seen spec SHA for a repo, or None if never set."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT spec_sha FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
    return row["spec_sha"] if row else None


def update_repo_spec_paths(
    repo_id: int,
    *,
    spec_old_path: str,
    spec_new_path: str,
    spec_sha: str,
) -> None:
    """Update the spec file paths and SHA after a new spec is downloaded."""
    with _connect() as conn:
        conn.execute(
            """UPDATE repos
               SET spec_old_path=?, spec_new_path=?, spec_sha=?
               WHERE id=?""",
            (spec_old_path, spec_new_path, spec_sha, repo_id),
        )
        conn.commit()


def get_watched_repos() -> list[dict[str, Any]]:
    """Return all repos with auto_watch=1."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM repos WHERE auto_watch = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]
