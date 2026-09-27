# Demo Script

A deterministic, offline demo using only the committed sample fixtures.
No live Stripe API call, no GitHub write, no LLM call required.

## Prerequisites

```bash
cd api-manager-web
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

Open `http://localhost:8000`.

## Step 1 — Register the sample repo

Fill in the registration form:

| Field | Value |
|---|---|
| Name | `sample-billing-service` |
| GitHub repo | `your-username/your-test-repo` |
| Token env var | `GITHUB_TOKEN` (or leave default) |
| Local repo path | `/absolute/path/to/api-manager-web/sample_data/sample_target_repo` |
| Old spec path | `/absolute/path/to/api-manager-web/sample_data/spec_old.json` |
| New spec path | `/absolute/path/to/api-manager-web/sample_data/spec_new.json` |

Click **Register**.

## Step 2 — Trigger the pipeline

Click **▶ Trigger** on the repo card.

The dashboard shows a new run card. Five stage-dots light up in sequence:

| Stage | What happens | Expected result |
|---|---|---|
| 1 | Diffs old vs new Stripe spec | 6 changes: 2 removals, 2 newly-required, 1 deprecated, 1 type-change |
| 2 | AST-scans `app.py` | 10 affected call sites |
| 3 | Patches isolated temp copy | 5 mechanical, 2 human-review, 1 skipped |
| 4 | Runs pytest (baseline + patched) | 7/7 pass, no regression |
| 5 | Opens GitHub PR (if token set) | PR created with `merge_policy: moderator_only` |

## Step 3 — Inspect the details drawer

Click **Details** on the run. The side drawer shows:

- **Stage 1**: change breakdown table
- **Stage 2**: affected file + site count
- **Stage 3**: patch counts and status
- **Stage 4**: baseline passed / patched passed / regression=false
- **Stage 5**: PR link (or gate reason if token not set)

## Step 4 — Review the PR (if GitHub token is set)

The PR body shows:

```
MERGE POLICY: moderator_only — A human reviewer must inspect, approve, and merge it.
This system never merges pull requests automatically.
```

And a callout for the 2 `# HUMAN REVIEW` sites that need manual attention.

## Running without a GitHub token

Stage 5 will gate with reason:  
`"GitHub API error: 401 Bad credentials — PR not opened."`

The run still completes as `success`. All stage 1-4 outputs are visible in the drawer.

## Running the tests offline

```bash
cd api-manager-web
python -m pytest -q
# Expected: 33 passed in ~2s
```
