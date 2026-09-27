# Self-Maintaining APIs

A five-stage pipeline that watches for breaking changes in a third-party API (Stripe) and auto-patches customer codebases, then opens a GitHub PR for human review.

## Quick start

```bash
cd api-manager-web
pip install -r requirements.txt
cp .env.example .env          # fill in GITHUB_TOKEN and OPENROUTER_API_KEY
uvicorn backend.main:app --reload --port 8000
```

Open `http://localhost:8000`.

## Run the tests

```bash
cd api-manager-web
python -m pytest -q
```

Expected: **33 passed**.

## Architecture

```
backend/
  main.py              FastAPI app, startup, static file serving
  db.py                SQLite persistence (repos + runs tables)
  orchestrator.py      Ties the 5 stages together per run
  spec_watcher.py      Background Stripe spec poller (opt-in)
  pipeline/
    schema.py          Shared dataclasses (FieldChange, ScanResult, …)
    phase1_change_detection.py   Diff two OpenAPI specs → StructuredDiff
    phase2_codebase_scan.py      AST-scan repo for affected call sites
    phase3_patch_generation.py   Mechanical + LLM patching on isolated copy
    phase4_test_gate.py          Baseline + patched pytest runs
    phase5_pr_automation.py      blob→tree→commit→ref→PR via GitHub API
    llm_patch.py                 OpenRouter/Nemotron fallback for hard cases
  routers/
    repos.py           POST/GET /api/repos, POST /api/repos/{id}/trigger
    runs.py            GET /api/runs, GET /api/runs/{id}
frontend/
  index.html           Single-page dashboard
  app.js               Vanilla JS: register, trigger, poll, drawer
  style.css            No framework
tests/
  test_pipeline.py     33 unit + 1 end-to-end fixture tests
sample_data/
  spec_old.json        Stripe-flavoured old spec (2023-08-16 shape)
  spec_new.json        Stripe-flavoured new spec (2024-06-20 shape)
  sample_target_repo/  Toy billing service that exercises every change category
docs/
  demo-script.md       Step-by-step demo walkthrough
  data-sources.md      What is real, what is simulated
bob_sessions/
  README.md            Where to store Bob task-session screenshots
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GITHUB_TOKEN` | Yes (for PRs) | — | GitHub PAT with Contents + PRs write |
| `OPENROUTER_API_KEY` | No | — | Enables LLM fallback for newly-required fields |
| `ENABLE_SPEC_WATCHER` | No | `false` | Set `true` to auto-poll Stripe spec |
| `WATCH_INTERVAL` | No | `3600` | Seconds between spec polls |
| `STRIPE_SPEC_URL` | No | Stripe public URL | Override spec source |

## Merge policy

This system **never merges PRs automatically**. Every opened PR contains:

```
MERGE POLICY: moderator_only — A human reviewer must inspect, approve, and merge it.
```

The `PRResult.merge_policy` field is always `"moderator_only"` in code.

## Demo mode vs live mode

See [`docs/demo-script.md`](docs/demo-script.md) for the fixture demo.  
See [`docs/data-sources.md`](docs/data-sources.md) for what is real vs simulated.
