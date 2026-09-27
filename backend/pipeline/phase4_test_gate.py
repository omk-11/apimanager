"""
Phase 4: run the patched repo's test suite and report pass/fail.

Entry point:
    run_tests(repo_path: str | Path) -> TestResult

Runs pytest twice:
  1. Baseline run  — against an unmodified copy of the repo (created from the
                     original source before Stage 3 patches were applied).
                     Stored in TestResult.baseline_passed.
  2. Patched run   — against the copy Stage 3 already modified.

baseline_regression is True when the baseline passed but the patched run
failed — meaning the patch itself introduced a regression.

If no tests are found (pytest exit code 5), the run is recorded as
status="no_tests_found" and passed=False. A missing test suite is NOT safe
to treat as passing, because Stage 5 uses TestResult.passed to gate the PR.

Exit codes:
    0   → all tests passed  → passed=True,  status="passed"
    1   → some tests failed → passed=False, status="failed"
    2   → collection error  → passed=False, status="failed"
    5   → no tests found    → passed=False, status="no_tests_found"
    other                   → passed=False, status="failed"
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.pipeline.schema import TestResult


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_tests(repo_path: str | Path, original_path: str | Path | None = None) -> TestResult:
    """
    Run pytest on the patched *repo_path*.

    If *original_path* is supplied (the unmodified source before patching),
    a baseline run is performed first and the result is stored in
    TestResult.baseline_passed / TestResult.baseline_regression.

    Never raises — all failures are captured in the returned object.
    """
    repo_path = Path(repo_path).resolve()

    baseline_passed: bool | None = None
    baseline_regression = False

    if original_path is not None:
        original_path = Path(original_path).resolve()
        baseline_result = _run_pytest(original_path)
        baseline_passed = baseline_result.passed

    patched_result = _run_pytest(repo_path)

    if baseline_passed is not None and baseline_passed and not patched_result.passed:
        baseline_regression = True

    return TestResult(
        passed=patched_result.passed,
        status=patched_result.status,
        total=patched_result.total,
        passed_count=patched_result.passed_count,
        failed_count=patched_result.failed_count,
        error_count=patched_result.error_count,
        stdout=patched_result.stdout,
        stderr=patched_result.stderr,
        returncode=patched_result.returncode,
        baseline_passed=baseline_passed,
        baseline_regression=baseline_regression,
    )


# ---------------------------------------------------------------------------
# Single pytest invocation
# ---------------------------------------------------------------------------

def _run_pytest(path: Path) -> TestResult:
    """Run pytest on *path* and return a structured result. Never raises."""
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q", str(path)]
    env = _build_env(path)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            passed=False, status="failed", total=0,
            passed_count=0, failed_count=0, error_count=0,
            stdout="", stderr="Test run timed out after 120 seconds.",
            returncode=-1,
        )
    except Exception as exc:
        return TestResult(
            passed=False, status="failed", total=0,
            passed_count=0, failed_count=0, error_count=0,
            stdout="", stderr=f"Failed to launch pytest: {exc}",
            returncode=-1,
        )

    # Exit code 5 = no tests collected — explicitly NOT passed
    if proc.returncode == 5:
        return TestResult(
            passed=False, status="no_tests_found", total=0,
            passed_count=0, failed_count=0, error_count=0,
            stdout=proc.stdout, stderr=proc.stderr,
            returncode=proc.returncode,
        )

    counts = _parse_summary(proc.stdout + proc.stderr)
    passed = proc.returncode == 0
    return TestResult(
        passed=passed,
        status="passed" if passed else "failed",
        total=counts["passed"] + counts["failed"] + counts["error"],
        passed_count=counts["passed"],
        failed_count=counts["failed"],
        error_count=counts["error"],
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_env(repo_path: Path) -> dict[str, str]:
    import os
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo_path) + (":" + existing if existing else "")
    return env


def _parse_summary(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "error": 0}
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not any(tok in line for tok in ("passed", "failed", "error")):
            continue
        p = re.search(r"(\d+) passed", line)
        f = re.search(r"(\d+) failed", line)
        e = re.search(r"(\d+) error", line)
        if p: counts["passed"] = int(p.group(1))
        if f: counts["failed"] = int(f.group(1))
        if e: counts["error"]  = int(e.group(1))
        break
    return counts
