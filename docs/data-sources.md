# Data Sources and Compliance

## What is real

| Component | Status | Notes |
|---|---|---|
| OpenAPI diffing logic | Real | Pure Python, difflib SequenceMatcher |
| AST codebase scanning | Real | Python `ast` module, no regex |
| Mechanical patching | Real | Regex line substitution on isolated temp copy |
| pytest subprocess runner | Real | Calls system pytest |
| GitHub blob/tree/commit/ref/PR API | Real | Standard GitHub REST API v2022-11-28 |
| Stripe OpenAPI spec URL | Real | `https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json` |
| LLM (OpenRouter/Nemotron) | Real | Optional; falls back to HUMAN_REVIEW if key absent |

## What is simulated / synthetic

| Component | Status | Notes |
|---|---|---|
| `sample_data/spec_old.json` | Synthetic | Participant-authored Stripe-flavoured spec (2023-08-16 shape). Not a real Stripe release. |
| `sample_data/spec_new.json` | Synthetic | Participant-authored new spec with deliberate breaking changes for demo purposes. |
| `sample_data/sample_target_repo/app.py` | Synthetic | Toy billing service written to exercise every change category. Not a real customer codebase. |
| `sample_data/sample_target_repo/test_app.py` | Synthetic | Mock-based tests that pass without Stripe credentials. |

## What is disabled by default

| Feature | Default | Enable with |
|---|---|---|
| Live Stripe spec polling | **Disabled** | `ENABLE_SPEC_WATCHER=true` |
| LLM patch suggestions | **Disabled** (returns HUMAN_REVIEW) | `OPENROUTER_API_KEY=sk-or-v1-…` |
| GitHub PR creation | Requires token | `GITHUB_TOKEN=ghp_…` in environment |

## Merge policy

This system **never merges pull requests automatically**. The `merge_policy` field
in every `PRResult` is always `"moderator_only"`. No code path in the repository
calls the GitHub merge endpoint. The generated PR body states this explicitly.

## Token security

GitHub tokens are **not stored in SQLite**. The database stores only the
environment variable name (e.g. `"GITHUB_TOKEN"`). The token is read from
`os.environ` at trigger time and passed in memory only.

## Fixture compliance

The sample specs and target repo were authored by the project participants
for this hackathon submission. They do not contain proprietary third-party data.
The Stripe API structure used as inspiration is publicly documented at
https://stripe.com/docs/api.
