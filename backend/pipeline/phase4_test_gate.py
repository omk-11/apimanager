"""
Phase 4: run the patched repo's test suite and report pass/fail.

Entry point:
    run_tests(repo_path: str | Path) -> TestResult

Runs:
    pytest --tb=short -q <repo_path>

in a subprocess with the repo itself on PYTHONPATH so that local imports
like `from app import ...` resolve correctly (as the sample test_app.py uses).

The subprocess captures stdout+stderr separately.  The TestResult is populated
by parsing pytest's summary line ("X passed", "X failed", "X error") from
stdout — no pytest plugins or JSON output required.

Exit codes:
    0   → all tests passed           → TestResult.passed = True
    1   → some tests failed/errored  → TestResult.passed = False
    2   → pytest usage/collection error → TestResult.passed = False
    5   → no tests found             → treated as passed (empty suite)
    other → TestResult.passed = False
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from backend.pipeline.schema import TestResult


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_tests(repo_path: str | Path) -> TestResult:
    """
    Run pytest inside *repo_path* and return a structured TestResult.
    Never raises — all failures are captured in the returned object.
    """
    repo_path = Path(repo_path).resolve()

    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short",
        "-q",
        str(repo_path),
    ]

    # Put the repo on PYTHONPATH so `from app import ...` works in tests.
    env = _build_env(repo_path)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return TestResult(
            passed=False,
            total=0,
            passed_count=0,
            failed_count=0,
            error_count=0,
            stdout="",
            stderr="Test run timed out after 120 seconds.",
            returncode=-1,
        )
    except Exception as exc:
        return TestResult(
            passed=False,
            total=0,
            passed_count=0,
            failed_count=0,
            error_count=0,
            stdout="",
            stderr=f"Failed to launch pytest: {exc}",
            returncode=-1,
        )

    counts = _parse_summary(proc.stdout + proc.stderr)

    # Exit code 5 = no tests collected — treat as passing (nothing to fail).
    if proc.returncode == 5:
        return TestResult(
            passed=True,
            total=0,
            passed_count=0,
            failed_count=0,
            error_count=0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
        )

    passed = proc.returncode == 0
    return TestResult(
        passed=passed,
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
    """
    Return a copy of os.environ with repo_path prepended to PYTHONPATH.
    This lets test files do `from app import ...` without an installed package.
    """
    import os
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo_path) + (":" + existing if existing else "")
    return env


_SUMMARY_RE = re.compile(
    r"(?:(\d+) passed)?[,\s]*(?:(\d+) failed)?[,\s]*(?:(\d+) error(?:s)?)?"
)


def _parse_summary(output: str) -> dict[str, int]:
    """
    Extract passed/failed/error counts from pytest's short summary line.
    e.g. "7 passed, 1 failed, 2 errors in 0.42s"
    Returns {"passed": N, "failed": N, "error": N} defaulting to 0.
    """
    counts = {"passed": 0, "failed": 0, "error": 0}

    # Look for the final summary line (last line containing "passed" or "failed" or "error")
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not any(tok in line for tok in ("passed", "failed", "error")):
            continue

        # Extract individual numbers
        p = re.search(r"(\d+) passed", line)
        f = re.search(r"(\d+) failed", line)
        e = re.search(r"(\d+) error", line)
        if p:
            counts["passed"] = int(p.group(1))
        if f:
            counts["failed"] = int(f.group(1))
        if e:
            counts["error"] = int(e.group(1))
        break

    return counts
