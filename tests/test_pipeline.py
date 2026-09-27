"""
Unit + end-to-end tests for the Self-Maintaining APIs pipeline.

Run with:
    cd api-manager-web
    python -m pytest -q
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIR = Path(__file__).parent.parent / "sample_data"
REPO_DIR   = SAMPLE_DIR / "sample_target_repo"

def load_specs():
    from backend.pipeline.phase1_change_detection import load_spec
    old = load_spec(SAMPLE_DIR / "spec_old.json")
    new = load_spec(SAMPLE_DIR / "spec_new.json")
    return old, new


# ===========================================================================
# Phase 1 — Change Detection
# ===========================================================================

class TestPhase1:

    def test_detects_removal(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        diff = detect_changes(old, new)
        paths = [c.path for c in diff.removals]
        assert "CreateChargeRequest.source" in paths, \
            "source field removal not detected"

    def test_detects_newly_required(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        diff = detect_changes(old, new)
        paths = [c.path for c in diff.newly_required]
        assert "CreateChargeRequest.payment_method" in paths
        assert "CreatePaymentIntentRequest.payment_method" in paths

    def test_detects_deprecated_endpoint(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        diff = detect_changes(old, new)
        paths = [c.path for c in diff.deprecated]
        assert any("charges" in p for p in paths), \
            "POST /v1/charges deprecation not detected"

    def test_detects_type_change(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        diff = detect_changes(old, new)
        tc = diff.type_changes
        assert any(c.path == "Customer.balance" for c in tc)
        balance = next(c for c in tc if c.path == "Customer.balance")
        assert balance.old_type == "integer"
        assert balance.new_type == "number"

    def test_source_to_payment_method_is_removal_not_rename(self):
        """
        source -> payment_method are lexically dissimilar (ratio ~0.10),
        so they must NOT be classified as a rename. They surface as
        REMOVED (source) + NEWLY_REQUIRED (payment_method).
        """
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        diff = detect_changes(old, new)
        rename_paths = [c.path for c in diff.renames]
        assert "CreateChargeRequest.source" not in rename_paths, \
            "source->payment_method must not be classified as a rename (names are unrelated)"

    def test_removed_endpoint_detected(self):
        """An operation absent from new spec is classified as REMOVED_ENDPOINT."""
        from backend.pipeline.phase1_change_detection import detect_changes
        from backend.pipeline.schema import ChangeKind
        old = {
            "paths": {"/v1/old_endpoint": {"get": {"tags": ["Test"], "operationId": "GetOld"}}},
            "components": {"schemas": {}}
        }
        new = {
            "paths": {},
            "components": {"schemas": {}}
        }
        diff = detect_changes(old, new)
        removed_ep = [c for c in diff.removals if c.kind == ChangeKind.REMOVED_ENDPOINT]
        assert any("old_endpoint" in c.path for c in removed_ep), \
            "removed endpoint not detected"

    def test_empty_diff_on_identical_specs(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, _ = load_specs()
        diff = detect_changes(old, old)
        assert diff.is_empty()

    def test_rename_detection_for_similar_names(self):
        """A genuinely similar rename (user_id -> userId) is detected."""
        from backend.pipeline.phase1_change_detection import detect_changes
        old_spec = {
            "paths": {},
            "components": {"schemas": {"MySchema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}}
            }}}
        }
        new_spec = {
            "paths": {},
            "components": {"schemas": {"MySchema": {
                "type": "object",
                "properties": {"userId": {"type": "string"}}
            }}}
        }
        diff = detect_changes(old_spec, new_spec)
        assert any(c.path == "MySchema.user_id" and c.new_name == "userId"
                   for c in diff.renames), "similar rename not detected"


# ===========================================================================
# Phase 2 — Codebase Scan
# ===========================================================================

class TestPhase2:

    def setup_method(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        old, new = load_specs()
        self.diff = detect_changes(old, new)

    def test_finds_affected_sites(self):
        from backend.pipeline.phase2_codebase_scan import scan_repo
        result = scan_repo(REPO_DIR, self.diff)
        assert len(result.affected) > 0

    def test_response_reads_flagged(self):
        from backend.pipeline.phase2_codebase_scan import scan_repo
        result = scan_repo(REPO_DIR, self.diff)
        response_reads = [cs for cs in result.affected if cs.is_response_read]
        assert len(response_reads) > 0, "no response-side reads detected"

    def test_retrieve_not_flagged_for_newly_required(self):
        """stripe.Charge.retrieve must NOT be flagged for payment_method (create-only)."""
        from backend.pipeline.phase2_codebase_scan import scan_repo
        from backend.pipeline.schema import ChangeKind
        result = scan_repo(REPO_DIR, self.diff)
        retrieve_newly_required = [
            cs for cs in result.affected
            if cs.change.kind == ChangeKind.NEWLY_REQUIRED
            and "retrieve" in cs.source_text.lower()
        ]
        assert len(retrieve_newly_required) == 0, \
            "retrieve wrongly flagged for newly_required"

    def test_by_file_groups_correctly(self):
        from backend.pipeline.phase2_codebase_scan import scan_repo
        result = scan_repo(REPO_DIR, self.diff)
        by_file = result.by_file()
        assert "app.py" in by_file


# ===========================================================================
# Phase 3 — Patch Generation
# ===========================================================================

class TestPhase3:

    def setup_method(self):
        from backend.pipeline.phase1_change_detection import detect_changes
        from backend.pipeline.phase2_codebase_scan import scan_repo
        old, new = load_specs()
        self.diff = detect_changes(old, new)
        self.scan = scan_repo(REPO_DIR, self.diff)

    def test_patches_isolated_copy_not_original(self):
        """Primary working tree must not be modified."""
        from backend.pipeline.phase3_patch_generation import generate_patches
        original_mtime = (REPO_DIR / "app.py").stat().st_mtime
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        new_mtime = (REPO_DIR / "app.py").stat().st_mtime
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        assert original_mtime == new_mtime, \
            "Phase 3 modified the original repo — it must only write to the temp copy"

    def test_source_renamed_to_payment_method(self):
        from backend.pipeline.phase3_patch_generation import generate_patches
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        patched = (patched_repo / "app.py").read_text()
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        assert "payment_method=source_token" in patched

    def test_response_reads_not_deleted(self):
        from backend.pipeline.phase3_patch_generation import generate_patches
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        patched = (patched_repo / "app.py").read_text()
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        assert "charge.source" in patched, \
            "response-side charge.source read was wrongly deleted"

    def test_human_review_comments_for_newly_required(self):
        from backend.pipeline.phase3_patch_generation import generate_patches
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        patched = (patched_repo / "app.py").read_text()
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        assert "# HUMAN REVIEW" in patched

    def test_deprecated_comment_inserted(self):
        from backend.pipeline.phase3_patch_generation import generate_patches
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        patched = (patched_repo / "app.py").read_text()
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        assert "# DEPRECATED" in patched

    def test_patch_counts(self):
        from backend.pipeline.phase3_patch_generation import generate_patches
        patch_result, patched_repo = generate_patches(REPO_DIR, self.scan, self.diff)
        shutil.rmtree(patched_repo.parent, ignore_errors=True)
        counts = patch_result.counts()
        assert counts["mechanical"] > 0
        assert counts["human"] > 0
        assert counts["skipped"] > 0


# ===========================================================================
# Phase 4 — Test Gate
# ===========================================================================

class TestPhase4:

    def test_passes_on_sample_repo(self):
        from backend.pipeline.phase4_test_gate import run_tests
        result = run_tests(REPO_DIR)
        assert result.passed
        assert result.status == "passed"
        assert result.total > 0

    def test_no_tests_found_not_passed(self):
        """Exit code 5 must produce status=no_tests_found, passed=False."""
        from backend.pipeline.phase4_test_gate import _run_pytest
        from unittest.mock import patch, MagicMock
        mock_proc = MagicMock()
        mock_proc.returncode = 5
        mock_proc.stdout = "no tests ran"
        mock_proc.stderr = ""
        with patch("subprocess.run", return_value=mock_proc):
            result = _run_pytest(Path("/fake"))
        assert result.passed is False
        assert result.status == "no_tests_found"

    def test_baseline_regression_detected(self):
        """When baseline passes but patched fails, baseline_regression=True."""
        from backend.pipeline.phase4_test_gate import run_tests
        from backend.pipeline.schema import TestResult

        passing = TestResult(passed=True, status="passed", total=1,
                             passed_count=1, failed_count=0, error_count=0,
                             stdout="1 passed", stderr="", returncode=0)
        failing = TestResult(passed=False, status="failed", total=1,
                             passed_count=0, failed_count=1, error_count=0,
                             stdout="1 failed", stderr="", returncode=1)

        with patch("backend.pipeline.phase4_test_gate._run_pytest",
                   side_effect=[passing, failing]):
            result = run_tests("/fake/original", original_path="/fake/original")
        assert result.baseline_regression is True

    def test_baseline_regression_false_when_both_fail(self):
        from backend.pipeline.phase4_test_gate import run_tests
        from backend.pipeline.schema import TestResult

        failing = TestResult(passed=False, status="failed", total=1,
                             passed_count=0, failed_count=1, error_count=0,
                             stdout="", stderr="", returncode=1)
        with patch("backend.pipeline.phase4_test_gate._run_pytest",
                   side_effect=[failing, failing]):
            result = run_tests("/fake/p", original_path="/fake/o")
        assert result.baseline_regression is False


# ===========================================================================
# Phase 5 — PR Automation
# ===========================================================================

class TestPhase5:

    def _make_test_result(self, passed=True, status="passed",
                          baseline_regression=False, no_tests=False):
        from backend.pipeline.schema import TestResult
        if no_tests:
            return TestResult(passed=False, status="no_tests_found", total=0,
                              passed_count=0, failed_count=0, error_count=0,
                              stdout="", stderr="", returncode=5)
        return TestResult(passed=passed,
                          status=status if passed else "failed",
                          total=7, passed_count=7 if passed else 5,
                          failed_count=0 if passed else 2, error_count=0,
                          stdout="", stderr="", returncode=0 if passed else 1,
                          baseline_regression=baseline_regression)

    def _make_patch_result(self, has_files=True):
        from backend.pipeline.schema import PatchResult, PatchRecord, PatchStatus, CallSite, FieldChange, ChangeKind
        pr = PatchResult()
        ch = FieldChange(kind=ChangeKind.REMOVED, path="CreateChargeRequest.source")
        cs = CallSite(file="app.py", line=33, col=8, source_text="source=x", change=ch)
        pr.records = [PatchRecord(call_site=cs, status=PatchStatus.MECHANICAL, description="test")]
        if has_files:
            pr.patched_files = {"app.py": "# patched\n"}
        return pr

    def test_no_tests_found_blocks_pr(self):
        from backend.pipeline.phase5_pr_automation import open_pr
        result = open_pr(self._make_patch_result(), self._make_test_result(no_tests=True),
                         "/tmp", "o/r", "tok", 1)
        assert not result.opened
        assert "no test suite" in result.reason.lower()

    def test_baseline_regression_blocks_pr(self):
        from backend.pipeline.phase5_pr_automation import open_pr
        result = open_pr(self._make_patch_result(),
                         self._make_test_result(passed=False, baseline_regression=True),
                         "/tmp", "o/r", "tok", 1)
        assert not result.opened
        assert "regression" in result.reason.lower()

    def test_failed_tests_block_pr(self):
        from backend.pipeline.phase5_pr_automation import open_pr
        result = open_pr(self._make_patch_result(), self._make_test_result(passed=False),
                         "/tmp", "o/r", "tok", 1)
        assert not result.opened

    def test_no_files_blocks_pr(self):
        from backend.pipeline.phase5_pr_automation import open_pr
        result = open_pr(self._make_patch_result(has_files=False),
                         self._make_test_result(), "/tmp", "o/r", "tok", 1)
        assert not result.opened

    def test_merge_policy_is_always_moderator_only(self):
        """merge_policy must be moderator_only even when PR is successfully opened."""
        from backend.pipeline.phase5_pr_automation import open_pr
        responses = [
            {"default_branch": "main"},
            {"object": {"sha": "abc"}},
            {"tree": {"sha": "tree_abc"}},
            {"sha": "blob1"},
            {"sha": "new_tree"},
            {"sha": "new_commit"},
            {},
            {"html_url": "https://github.com/o/r/pull/1", "number": 1},
        ]
        resp_iter = iter(responses)
        def side_effect(method, url, **kwargs):
            m = MagicMock()
            m.json.return_value = next(resp_iter)
            m.raise_for_status = MagicMock()
            return m
        with patch("requests.request", side_effect=side_effect):
            result = open_pr(self._make_patch_result(), self._make_test_result(),
                             "/tmp", "o/r", "ghp_test", 42)
        assert result.merge_policy == "moderator_only"
        assert result.opened


# ===========================================================================
# LLM patch validation
# ===========================================================================

class TestLLMValidation:

    def test_rejects_multiline_output(self):
        from backend.pipeline.llm_patch import _clean
        assert _clean("line1\nline2") is None

    def test_rejects_syntax_error(self):
        from backend.pipeline.llm_patch import _clean
        assert _clean("def (broken:") is None

    def test_accepts_valid_single_line(self):
        from backend.pipeline.llm_patch import _clean
        result = _clean("    payment_method=pm_card_visa,")
        assert result is not None

    def test_strips_markdown_fences(self):
        from backend.pipeline.llm_patch import _clean
        result = _clean("```python\n    payment_method=pm_card_visa,\n```")
        assert result is not None
        assert "```" not in result

    def test_returns_none_when_no_api_key(self):
        from backend.pipeline.schema import CallSite, FieldChange, ChangeKind
        import importlib, backend.pipeline.llm_patch as lp
        os.environ.pop("OPENROUTER_API_KEY", None)
        importlib.reload(lp)
        ch = FieldChange(kind=ChangeKind.NEWLY_REQUIRED, path="X.y")
        cs = CallSite(file="f.py", line=1, col=0, source_text="x", change=ch)
        assert lp.llm_suggest_patch(cs, ["x"]) is None


# ===========================================================================
# End-to-end fixture test (stages 1-4, no GitHub)
# ===========================================================================

class TestEndToEnd:

    def test_full_pipeline_stages_1_to_4(self):
        """Run stages 1-4 against the committed sample fixtures."""
        from backend.pipeline.phase1_change_detection import detect_changes, load_spec
        from backend.pipeline.phase2_codebase_scan import scan_repo
        from backend.pipeline.phase3_patch_generation import generate_patches
        from backend.pipeline.phase4_test_gate import run_tests

        old = load_spec(SAMPLE_DIR / "spec_old.json")
        new = load_spec(SAMPLE_DIR / "spec_new.json")

        # Stage 1
        diff = detect_changes(old, new)
        assert not diff.is_empty()
        assert len(diff.all_changes()) == 6

        # Stage 2
        scan = scan_repo(REPO_DIR, diff)
        assert len(scan.affected) == 10

        # Stage 3 — must not touch REPO_DIR
        orig_mtime = (REPO_DIR / "app.py").stat().st_mtime
        patch_result, patched_repo = generate_patches(REPO_DIR, scan, diff)
        assert (REPO_DIR / "app.py").stat().st_mtime == orig_mtime, \
            "Stage 3 modified the original repo"
        assert "app.py" in patch_result.patched_files
        counts = patch_result.counts()
        assert counts["mechanical"] == 5
        assert counts["human"] == 2
        assert counts["skipped"] == 1

        # Stage 4 — run on both original (baseline) and patched copy
        result = run_tests(patched_repo, original_path=REPO_DIR)
        shutil.rmtree(patched_repo.parent, ignore_errors=True)

        assert result.passed, f"Tests failed after patch: {result.stdout}"
        assert result.status == "passed"
        assert result.baseline_passed is True
        assert result.baseline_regression is False
        assert result.total == 7
