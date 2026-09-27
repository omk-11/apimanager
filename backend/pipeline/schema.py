"""
Shared dataclasses used across all 5 pipeline stages.

Import from here — never redefine these structures in individual stage modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Stage 1 output: individual change records
# ---------------------------------------------------------------------------

class ChangeKind(str, Enum):
    RENAMED           = "renamed"
    REMOVED           = "removed"
    REMOVED_ENDPOINT  = "removed_endpoint"
    NEWLY_REQUIRED    = "newly_required"
    DEPRECATED        = "deprecated"
    TYPE_CHANGED      = "type_changed"


@dataclass
class FieldChange:
    """One discrete change detected between old and new spec versions."""
    kind: ChangeKind

    # Dotted path to the field within its schema object, e.g. "charge.amount"
    path: str

    # For RENAMED: the new name after renaming
    new_name: str | None = None

    # For TYPE_CHANGED: old and new type strings
    old_type: str | None = None
    new_type: str | None = None

    # For DEPRECATED: the replacement hint from the spec, if present
    replacement_hint: str | None = None

    # The OpenAPI endpoint tag this change belongs to (e.g. "Charges")
    endpoint_tag: str | None = None


@dataclass
class SpecDiff:
    """Complete output of Stage 1 — the structured diff of two spec versions."""
    renames:         list[FieldChange] = field(default_factory=list)
    removals:        list[FieldChange] = field(default_factory=list)
    newly_required:  list[FieldChange] = field(default_factory=list)
    deprecated:      list[FieldChange] = field(default_factory=list)
    type_changes:    list[FieldChange] = field(default_factory=list)

    def all_changes(self) -> list[FieldChange]:
        return (
            self.renames
            + self.removals
            + self.newly_required
            + self.deprecated
            + self.type_changes
        )

    def is_empty(self) -> bool:
        return len(self.all_changes()) == 0


# ---------------------------------------------------------------------------
# Stage 2 output: affected call sites inside the target repo
# ---------------------------------------------------------------------------

@dataclass
class CallSite:
    """One location in the target repo that references a changed field/endpoint."""
    file: str          # relative path within the repo
    line: int          # 1-based line number
    col: int           # 0-based column offset

    # The source text of the attribute access / keyword argument as written
    source_text: str

    # The FieldChange this call site is linked to
    change: FieldChange

    # True when this is a *response* attribute read (field must not be deleted)
    is_response_read: bool = False


@dataclass
class ScanResult:
    """Complete output of Stage 2 — all affected call sites found in the repo."""
    affected: list[CallSite] = field(default_factory=list)

    def by_file(self) -> dict[str, list[CallSite]]:
        """Group affected sites by file path."""
        out: dict[str, list[CallSite]] = {}
        for cs in self.affected:
            out.setdefault(cs.file, []).append(cs)
        return out


# ---------------------------------------------------------------------------
# Stage 3 output: patch decisions and results
# ---------------------------------------------------------------------------

class PatchStatus(str, Enum):
    MECHANICAL  = "mechanical"   # safe deterministic patch applied
    LLM         = "llm"          # watsonx.ai suggestion applied
    HUMAN       = "human"        # could not patch; left a HUMAN REVIEW comment
    SKIPPED     = "skipped"      # response-side removal — intentionally left alone


@dataclass
class PatchRecord:
    """The patch decision made for one CallSite."""
    call_site: CallSite
    status: PatchStatus
    description: str             # human-readable summary of what was done/why

    # The diff applied as a unified-diff string (empty for SKIPPED / HUMAN)
    unified_diff: str = ""


@dataclass
class PatchResult:
    """Complete output of Stage 3 — patches applied across all files."""
    records: list[PatchRecord] = field(default_factory=list)

    # Map of relative file path → full patched source text
    patched_files: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {s.value: 0 for s in PatchStatus}
        for r in self.records:
            out[r.status.value] += 1
        return out


# ---------------------------------------------------------------------------
# Stage 4 output: test gate result
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """Output of Stage 4 — pytest run outcome."""
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    error_count: int
    stdout: str
    stderr: str
    returncode: int
    # "passed" | "failed" | "no_tests_found"
    status: str = "passed"
    # When run as the patched copy: did the baseline also pass?
    baseline_passed: bool | None = None
    baseline_regression: bool = False   # True → patch broke something that was passing


# ---------------------------------------------------------------------------
# Stage 5 output: PR automation result
# ---------------------------------------------------------------------------

@dataclass
class PRResult:
    """Output of Stage 5 — the opened GitHub PR (or the reason one wasn't opened)."""
    opened: bool
    pr_url: str | None = None
    pr_number: int | None = None
    branch: str | None = None
    reason: str = ""                          # populated when opened=False
    merge_policy: str = "moderator_only"      # NEVER changed to anything else
    human_review_count: int = 0               # how many HUMAN_REVIEW sites remain


# ---------------------------------------------------------------------------
# Top-level run envelope passed through the whole pipeline
# ---------------------------------------------------------------------------

@dataclass
class PipelineRun:
    """
    Carries all stage outputs for a single pipeline execution.
    Constructed by the orchestrator before any stage runs and mutated in place.
    """
    run_id: int
    repo_id: int
    repo_path: str               # local filesystem path to cloned/checked-out repo
    spec_old_path: str           # path to old OpenAPI spec JSON
    spec_new_path: str           # path to new OpenAPI spec JSON
    github_token: str
    github_repo: str             # "owner/repo" string

    # Stage outputs — None until the stage completes
    spec_diff:    SpecDiff    | None = None
    scan_result:  ScanResult  | None = None
    patch_result: PatchResult | None = None
    test_result:  TestResult  | None = None
    pr_result:    PRResult    | None = None

    # Extra metadata the orchestrator can stash for DB persistence
    meta: dict[str, Any] = field(default_factory=dict)
