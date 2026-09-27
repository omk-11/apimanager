"""
Orchestrator: ties the 5 pipeline stages together for one run.

Entry point (called by the trigger endpoint in a background thread):
    run_pipeline(run_id: int, repo: dict) -> None

Execution order:
    Stage 1  detect_changes     → SpecDiff
    Stage 2  scan_repo          → ScanResult
    Stage 3  generate_patches   → PatchResult
    Stage 4  run_tests          → TestResult
    Stage 5  open_pr            → PRResult

Each stage result is:
  1. Attached to the PipelineRun dataclass
  2. Serialised to a JSON-compatible dict and persisted to the DB via
     db.update_run_stage() before the next stage starts

If any stage raises an unhandled exception the run is marked 'error' and
execution stops.  Stage failures that produce a structured result (e.g.
tests failing, PR not opened) do NOT stop the pipeline — they are recorded
and the next stage decides what to do.
"""
from __future__ import annotations

import dataclasses
import json
import traceback
from pathlib import Path
from typing import Any

import backend.db as db
from backend.pipeline.schema import PipelineRun
from backend.pipeline.phase1_change_detection import detect_changes, load_spec
from backend.pipeline.phase2_codebase_scan import scan_repo
from backend.pipeline.phase3_patch_generation import generate_patches
from backend.pipeline.phase4_test_gate import run_tests
from backend.pipeline.phase5_pr_automation import open_pr


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_pipeline(run_id: int, repo: dict) -> None:
    """
    Execute the full 5-stage pipeline for *run_id* / *repo*.
    All exceptions are caught; the run is marked 'error' on failure.
    """
    db.update_run_started(run_id)

    pipeline = PipelineRun(
        run_id=run_id,
        repo_id=repo["id"],
        repo_path=repo["repo_path"],
        spec_old_path=repo["spec_old_path"],
        spec_new_path=repo["spec_new_path"],
        github_token=repo["github_token"],
        github_repo=repo["github_repo"],
    )

    try:
        _stage1(pipeline)
        _stage2(pipeline)
        _stage3(pipeline)
        _stage4(pipeline)
        _stage5(pipeline)
    except Exception:
        db.update_run_finished(
            run_id,
            success=False,
            error_message=traceback.format_exc(),
        )
        return

    db.update_run_finished(run_id, success=True)


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def _stage1(p: PipelineRun) -> None:
    old_spec = load_spec(p.spec_old_path)
    new_spec = load_spec(p.spec_new_path)
    p.spec_diff = detect_changes(old_spec, new_spec)
    db.update_run_stage(p.run_id, 1, _serialise_spec_diff(p.spec_diff))


def _stage2(p: PipelineRun) -> None:
    assert p.spec_diff is not None
    p.scan_result = scan_repo(p.repo_path, p.spec_diff)
    db.update_run_stage(p.run_id, 2, _serialise_scan_result(p.scan_result))


def _stage3(p: PipelineRun) -> None:
    assert p.scan_result is not None and p.spec_diff is not None
    p.patch_result = generate_patches(p.repo_path, p.scan_result, p.spec_diff)
    db.update_run_stage(p.run_id, 3, _serialise_patch_result(p.patch_result))


def _stage4(p: PipelineRun) -> None:
    p.test_result = run_tests(p.repo_path)
    db.update_run_stage(p.run_id, 4, _serialise_test_result(p.test_result))


def _stage5(p: PipelineRun) -> None:
    assert p.patch_result is not None and p.test_result is not None
    p.pr_result = open_pr(
        patch_result=p.patch_result,
        test_result=p.test_result,
        repo_path=p.repo_path,
        github_repo=p.github_repo,
        github_token=p.github_token,
        run_id=p.run_id,
    )
    db.update_run_stage(p.run_id, 5, _serialise_pr_result(p.pr_result))


# ---------------------------------------------------------------------------
# Serialisers — convert dataclasses to JSON-safe dicts for DB storage
# ---------------------------------------------------------------------------

def _serialise_spec_diff(diff) -> dict:
    return {
        "renames":        [_fc(c) for c in diff.renames],
        "removals":       [_fc(c) for c in diff.removals],
        "newly_required": [_fc(c) for c in diff.newly_required],
        "deprecated":     [_fc(c) for c in diff.deprecated],
        "type_changes":   [_fc(c) for c in diff.type_changes],
        "total":          len(diff.all_changes()),
    }


def _serialise_scan_result(scan) -> dict:
    by_file = {}
    for rel_path, sites in scan.by_file().items():
        by_file[rel_path] = [
            {
                "line": cs.line,
                "col": cs.col,
                "source_text": cs.source_text,
                "is_response_read": cs.is_response_read,
                "change_kind": cs.change.kind.value,
                "change_path": cs.change.path,
            }
            for cs in sites
        ]
    return {"total_affected": len(scan.affected), "by_file": by_file}


def _serialise_patch_result(patch) -> dict:
    return {
        "counts": patch.counts(),
        "patched_files": list(patch.patched_files.keys()),
        "records": [
            {
                "file": r.call_site.file,
                "line": r.call_site.line,
                "status": r.status.value,
                "description": r.description,
            }
            for r in patch.records
        ],
    }


def _serialise_test_result(test) -> dict:
    return {
        "passed": test.passed,
        "total": test.total,
        "passed_count": test.passed_count,
        "failed_count": test.failed_count,
        "error_count": test.error_count,
        "returncode": test.returncode,
        # Truncate stdout/stderr to keep the DB row size bounded
        "stdout": test.stdout[-4000:] if test.stdout else "",
        "stderr": test.stderr[-2000:] if test.stderr else "",
    }


def _serialise_pr_result(pr) -> dict:
    return {
        "opened": pr.opened,
        "pr_url": pr.pr_url,
        "pr_number": pr.pr_number,
        "branch": pr.branch,
        "reason": pr.reason,
    }


def _fc(change) -> dict:
    """Serialise a FieldChange to a plain dict."""
    return {
        "kind": change.kind.value,
        "path": change.path,
        "new_name": change.new_name,
        "old_type": change.old_type,
        "new_type": change.new_type,
        "replacement_hint": change.replacement_hint,
        "endpoint_tag": change.endpoint_tag,
    }
