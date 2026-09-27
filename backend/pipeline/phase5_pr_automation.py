"""
Phase 5: decide whether it's safe to open a PR, then create it correctly.

Entry point:
    open_pr(
        patch_result: PatchResult,
        test_result:  TestResult,
        repo_path:    str | Path,
        github_repo:  str,          # "owner/repo"
        github_token: str,
        run_id:       int,
    ) -> PRResult

Safety gate
-----------
A PR is opened ONLY when test_result.passed is True.
If tests failed the function returns immediately with opened=False and a reason.

Commit strategy — the only correct order
-----------------------------------------
A PR opened against an unmodified branch is a failure state, not an edge case.
The GitHub Contents/Git Data API requires this exact sequence before opening a PR:

  1. GET  /repos/{owner}/{repo}/git/ref/heads/{base}
         → resolve the SHA of the base branch tip (usually "main"/"master")

  2. POST /repos/{owner}/{repo}/git/blobs
         → create one blob per changed file, receive a blob SHA each

  3. POST /repos/{owner}/{repo}/git/trees
         → create a tree that maps each path to its blob SHA,
           base_tree = the commit SHA from step 1

  4. POST /repos/{owner}/{repo}/git/commits
         → create a commit pointing at the new tree,
           parents = [base_commit_sha from step 1]

  5. POST /repos/{owner}/{repo}/git/refs
         → create the new branch ref pointing at the commit SHA from step 4

  6. POST /repos/{owner}/{repo}/pulls
         → open the PR from the new branch into base

Steps 2-4 MUST complete before step 5.
Step 5 MUST complete before step 6.
Skipping any of steps 1-5 will open a PR against an empty or unmodified branch.

Branch naming
-------------
    auto-patch/run-{run_id}-{timestamp}

so parallel runs never collide.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from backend.pipeline.schema import PatchResult, PRResult, TestResult


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

_GH_API = "https://api.github.com"


def _gh(
    method: str,
    path: str,
    token: str,
    **kwargs,
) -> requests.Response:
    """Make an authenticated GitHub API call. Raises on 4xx/5xx."""
    url = f"{_GH_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def open_pr(
    patch_result: PatchResult,
    test_result: TestResult,
    repo_path: str | Path,
    github_repo: str,
    github_token: str,
    run_id: int,
) -> PRResult:
    """
    Gate on test passage, commit all patched files to a new branch via the
    GitHub Git Data API, then open a pull request.

    Returns PRResult with opened=False (and a reason) if:
      - Tests did not pass
      - No files were patched (nothing to commit)
      - Any GitHub API call fails
    """
    # -----------------------------------------------------------------------
    # Gate 1: tests must pass
    # -----------------------------------------------------------------------
    if not test_result.passed:
        return PRResult(
            opened=False,
            reason=(
                f"Tests did not pass "
                f"({test_result.failed_count} failed, "
                f"{test_result.error_count} errors) — PR not opened."
            ),
        )

    # -----------------------------------------------------------------------
    # Gate 2: there must be files to commit
    # -----------------------------------------------------------------------
    if not patch_result.patched_files:
        return PRResult(
            opened=False,
            reason="No files were patched — nothing to commit.",
        )

    repo_path = Path(repo_path)
    owner, repo_name = github_repo.split("/", 1)
    branch_name = f"auto-patch/run-{run_id}-{int(time.time())}"

    try:
        return _create_branch_and_pr(
            owner=owner,
            repo_name=repo_name,
            token=github_token,
            branch_name=branch_name,
            patch_result=patch_result,
            repo_path=repo_path,
            run_id=run_id,
        )
    except requests.HTTPError as exc:
        return PRResult(
            opened=False,
            reason=f"GitHub API error: {exc.response.status_code} {exc.response.text[:200]}",
        )
    except Exception as exc:
        return PRResult(
            opened=False,
            reason=f"Unexpected error during PR creation: {exc}",
        )


# ---------------------------------------------------------------------------
# Core: blob → tree → commit → ref → PR
# ---------------------------------------------------------------------------

def _create_branch_and_pr(
    *,
    owner: str,
    repo_name: str,
    token: str,
    branch_name: str,
    patch_result: PatchResult,
    repo_path: Path,
    run_id: int,
) -> PRResult:
    repo_slug = f"{owner}/{repo_name}"

    # ------------------------------------------------------------------
    # Step 1: resolve base branch tip SHA
    # ------------------------------------------------------------------
    base_branch = _resolve_default_branch(owner, repo_name, token)
    ref_data = _gh("GET", f"/repos/{repo_slug}/git/ref/heads/{base_branch}", token).json()
    base_commit_sha: str = ref_data["object"]["sha"]

    # Resolve the tree SHA that the base commit points to (needed for base_tree)
    commit_data = _gh("GET", f"/repos/{repo_slug}/git/commits/{base_commit_sha}", token).json()
    base_tree_sha: str = commit_data["tree"]["sha"]

    # ------------------------------------------------------------------
    # Step 2: create one blob per patched file
    # ------------------------------------------------------------------
    tree_entries = []
    for rel_path, content in patch_result.patched_files.items():
        blob = _gh(
            "POST",
            f"/repos/{repo_slug}/git/blobs",
            token,
            json={
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            },
        ).json()
        tree_entries.append({
            "path": rel_path,
            "mode": "100644",   # regular file
            "type": "blob",
            "sha":  blob["sha"],
        })

    # ------------------------------------------------------------------
    # Step 3: create a new tree on top of the base tree
    # ------------------------------------------------------------------
    new_tree = _gh(
        "POST",
        f"/repos/{repo_slug}/git/trees",
        token,
        json={
            "base_tree": base_tree_sha,
            "tree": tree_entries,
        },
    ).json()
    new_tree_sha: str = new_tree["sha"]

    # ------------------------------------------------------------------
    # Step 4: create a commit pointing at the new tree
    # ------------------------------------------------------------------
    counts = patch_result.counts()
    commit_message = _build_commit_message(counts, run_id)
    new_commit = _gh(
        "POST",
        f"/repos/{repo_slug}/git/commits",
        token,
        json={
            "message": commit_message,
            "tree": new_tree_sha,
            "parents": [base_commit_sha],
        },
    ).json()
    new_commit_sha: str = new_commit["sha"]

    # ------------------------------------------------------------------
    # Step 5: create the branch ref pointing at the new commit
    # ------------------------------------------------------------------
    _gh(
        "POST",
        f"/repos/{repo_slug}/git/refs",
        token,
        json={
            "ref": f"refs/heads/{branch_name}",
            "sha": new_commit_sha,
        },
    )

    # ------------------------------------------------------------------
    # Step 6: open the pull request
    # ------------------------------------------------------------------
    pr_body = _build_pr_body(patch_result, run_id)
    pr = _gh(
        "POST",
        f"/repos/{repo_slug}/pulls",
        token,
        json={
            "title": f"[auto-patch] API migration for run #{run_id}",
            "body":  pr_body,
            "head":  branch_name,
            "base":  base_branch,
        },
    ).json()

    return PRResult(
        opened=True,
        pr_url=pr["html_url"],
        pr_number=pr["number"],
        branch=branch_name,
        reason="",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_default_branch(owner: str, repo_name: str, token: str) -> str:
    """Return the default branch name ("main", "master", etc.)."""
    data = _gh("GET", f"/repos/{owner}/{repo_name}", token).json()
    return data.get("default_branch", "main")


def _build_commit_message(counts: dict[str, int], run_id: int) -> str:
    parts = []
    if counts.get("mechanical", 0):
        parts.append(f"{counts['mechanical']} mechanical")
    if counts.get("llm", 0):
        parts.append(f"{counts['llm']} LLM-assisted")
    if counts.get("human", 0):
        parts.append(f"{counts['human']} needing human review")
    summary = ", ".join(parts) if parts else "no changes"
    return (
        f"auto-patch: API migration run #{run_id}\n\n"
        f"Applied patches: {summary}.\n"
        f"Generated by Self-Maintaining APIs pipeline."
    )


def _build_pr_body(patch_result: PatchResult, run_id: int) -> str:
    counts = patch_result.counts()
    lines = [
        f"## Auto-patch: API migration (run #{run_id})",
        "",
        "This PR was opened automatically by the Self-Maintaining APIs pipeline.",
        "",
        "### Patch summary",
        "",
        f"| Status | Count |",
        f"|--------|-------|",
        f"| ✅ Mechanical | {counts.get('mechanical', 0)} |",
        f"| 🤖 LLM-assisted | {counts.get('llm', 0)} |",
        f"| ⚠️ Needs human review | {counts.get('human', 0)} |",
        f"| ⏭️ Skipped (response reads) | {counts.get('skipped', 0)} |",
        "",
        "### Files changed",
        "",
    ]
    for rel_path in sorted(patch_result.patched_files):
        lines.append(f"- `{rel_path}`")

    if counts.get("human", 0):
        lines += [
            "",
            "> **Action required:** "
            f"{counts['human']} site(s) could not be patched automatically. "
            "Search for `# HUMAN REVIEW` in the changed files.",
        ]

    return "\n".join(lines)
