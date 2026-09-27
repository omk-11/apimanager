"""
Phase 3: mechanical patchers for renamed/removed/deprecated/type-changed call sites.

Entry point:
    generate_patches(
        repo_path: str | Path,
        scan_result: ScanResult,
        spec_diff: SpecDiff,
    ) -> PatchResult

Decision matrix — one PatchRecord per unique (file, line, change) triple:

  RENAMED       request-side kwarg   → rename keyword argument in-place (MECHANICAL)
  REMOVED       request-side kwarg   → remove keyword argument (MECHANICAL)
                                       Exception: if a NEWLY_REQUIRED field exists
                                       on the same resource and the removed field
                                       is the only arg on that line, swap it
  REMOVED       response-side read   → SKIPPED (must never delete a response read)
  NEWLY_REQUIRED missing from call   → fall back to llm_patch; # HUMAN REVIEW on failure
  DEPRECATED    endpoint call        → insert a # DEPRECATED comment above the call (MECHANICAL)
  TYPE_CHANGED  request-side kwarg   → insert a # TYPE CHANGE comment above (MECHANICAL)
  TYPE_CHANGED  response-side read   → insert a # TYPE CHANGE comment above (MECHANICAL)

Patching is done on in-memory line lists so multiple changes to the same file
accumulate correctly.  After all sites in a file are processed the final source
is written back to disk and stored in PatchResult.patched_files.

Deduplication: (file, line, change.path) triples are processed at most once.
When Stage 2 produces two REMOVED sites for the same line (one for
CreateChargeRequest.source, one for Charge.source), only the first is acted on;
the second is silently skipped because the keyword is already gone.
"""
from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path
from typing import Any

from backend.pipeline.schema import (
    CallSite,
    ChangeKind,
    FieldChange,
    PatchRecord,
    PatchResult,
    PatchStatus,
    ScanResult,
    SpecDiff,
)
from backend.pipeline.llm_patch import llm_suggest_patch


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_patches(
    repo_path: str | Path,
    scan_result: ScanResult,
    spec_diff: SpecDiff,
) -> tuple["PatchResult", Path]:
    """
    Apply all safe mechanical patches to an ISOLATED TEMPORARY COPY of the
    repo.  The caller's primary working tree (repo_path) is never modified.

    Returns (PatchResult, patched_repo_path) where patched_repo_path is
    the temporary directory containing the modified files.  The caller
    (orchestrator) is responsible for cleaning it up after Stage 4/5.

    patched_files in PatchResult maps relative path -> full patched source,
    and unified_diff in each PatchRecord shows what changed.
    """
    import shutil, tempfile
    repo_path = Path(repo_path)

    # Create an isolated copy — this is what gets patched, never repo_path.
    tmp_dir = Path(tempfile.mkdtemp(prefix="api_patch_"))
    patched_repo = tmp_dir / "repo"
    shutil.copytree(str(repo_path), str(patched_repo))

    result = PatchResult()
    by_file = scan_result.by_file()

    for rel_path, sites in by_file.items():
        abs_path = patched_repo / rel_path
        try:
            original_source = abs_path.read_text(encoding="utf-8")
        except OSError:
            continue

        original_lines = original_source.splitlines(keepends=True)
        patched_lines = list(original_lines)

        records = _patch_file(
            rel_path=rel_path,
            sites=sites,
            lines=patched_lines,
            spec_diff=spec_diff,
        )

        patched_source = "".join(patched_lines)

        if patched_source != original_source:
            abs_path.write_text(patched_source, encoding="utf-8")
            result.patched_files[rel_path] = patched_source

        result.records.extend(records)

    return result, patched_repo


# ---------------------------------------------------------------------------
# Per-file patching
# ---------------------------------------------------------------------------

def _patch_file(
    rel_path: str,
    sites: list[CallSite],
    lines: list[str],
    spec_diff: SpecDiff,
) -> list[PatchRecord]:
    """
    Apply patches for all sites in one file.  *lines* is mutated in place.
    Returns PatchRecords for all sites (including SKIPPED ones).
    """
    records: list[PatchRecord] = []

    # Deduplication key: (line_number, leaf_field_name) — process each unique
    # (line, field) pair once.  When Stage 2 emits two REMOVED sites for the
    # same keyword on the same line (one from CreateChargeRequest.source, one
    # from Charge.source), only the first is acted on.
    seen: set[tuple[int, str]] = set()

    # Build a lookup: field_name → newly_required FieldChange for same resource,
    # used when a REMOVED site needs to know if there's a matching new requirement.
    newly_required_by_field = _build_newly_required_index(spec_diff)

    # Sort by line descending so that inserting lines above a site doesn't shift
    # subsequent sites' line numbers.
    sorted_sites = sorted(sites, key=lambda cs: cs.line, reverse=True)

    for cs in sorted_sites:
        key = (cs.line, cs.change.path.split(".")[-1])
        if key in seen:
            continue
        seen.add(key)

        record = _apply_one(cs, lines, newly_required_by_field)
        records.append(record)

    return records


def _apply_one(
    cs: CallSite,
    lines: list[str],
    newly_required_by_field: dict[str, FieldChange],
) -> PatchRecord:
    """Dispatch to the right patcher for one call site."""
    kind = cs.change.kind

    if kind == ChangeKind.DEPRECATED:
        return _patch_deprecated(cs, lines)

    if kind == ChangeKind.REMOVED:
        if cs.is_response_read:
            return PatchRecord(
                call_site=cs,
                status=PatchStatus.SKIPPED,
                description="Response-side field read — left unchanged to preserve runtime behaviour.",
            )
        return _patch_removed_kwarg(cs, lines, newly_required_by_field)

    if kind == ChangeKind.RENAMED:
        if cs.is_response_read:
            # Rename on a response read — insert a comment; don't touch the attribute
            return _insert_comment(
                cs, lines,
                f"# TYPE CHANGE: attribute '{cs.change.path.split('.')[-1]}' "
                f"renamed to '{cs.change.new_name}' in new API version",
            )
        return _patch_renamed_kwarg(cs, lines)

    if kind == ChangeKind.TYPE_CHANGED:
        field = cs.change.path.split(".")[-1]
        comment = (
            f"# TYPE CHANGE: '{field}' is now "
            f"{cs.change.new_type} (was {cs.change.old_type}) — verify cast if needed"
        )
        return _insert_comment(cs, lines, comment)

    if kind == ChangeKind.NEWLY_REQUIRED:
        return _patch_newly_required(cs, lines)

    # Fallthrough — should not happen with well-formed SpecDiff
    return PatchRecord(
        call_site=cs,
        status=PatchStatus.SKIPPED,
        description=f"Unhandled change kind: {kind}",
    )


# ---------------------------------------------------------------------------
# Individual patchers
# ---------------------------------------------------------------------------

def _patch_deprecated(cs: CallSite, lines: list[str]) -> PatchRecord:
    """Insert a # DEPRECATED comment on the line above the call site."""
    line_idx = cs.line - 1  # 0-based
    indent = _leading_whitespace(lines[line_idx])
    hint = cs.change.replacement_hint or "use the updated API"
    comment = f"{indent}# DEPRECATED: {cs.change.path} — {hint}\n"
    # Don't insert if we already added this comment (idempotency)
    if line_idx > 0 and "# DEPRECATED:" in lines[line_idx - 1]:
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.MECHANICAL,
            description="Deprecated comment already present — skipped duplicate insertion.",
        )
    lines.insert(line_idx, comment)
    return PatchRecord(
        call_site=cs,
        status=PatchStatus.MECHANICAL,
        description=f"Inserted deprecation notice above {cs.change.path}.",
    )


def _patch_renamed_kwarg(cs: CallSite, lines: list[str]) -> PatchRecord:
    """
    Rename a keyword argument in the call on cs.line.
    source_text is "old_name=value_expr" — we replace just the keyword name.
    """
    line_idx = cs.line - 1
    old_field = cs.change.path.split(".")[-1]
    new_field = cs.change.new_name
    if not new_field:
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.SKIPPED,
            description=f"Rename target missing for {cs.change.path}.",
        )
    original_line = lines[line_idx]
    # Replace the keyword name — use word-boundary regex to be safe
    patched_line = re.sub(
        rf'\b{re.escape(old_field)}\s*=',
        f"{new_field}=",
        original_line,
        count=1,
    )
    if patched_line == original_line:
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.SKIPPED,
            description=f"Keyword '{old_field}' not found verbatim on line {cs.line}.",
        )
    lines[line_idx] = patched_line
    diff = _make_diff(cs.file, [original_line], [patched_line], cs.line)
    return PatchRecord(
        call_site=cs,
        status=PatchStatus.MECHANICAL,
        description=f"Renamed keyword argument '{old_field}' → '{new_field}'.",
        unified_diff=diff,
    )


def _patch_removed_kwarg(
    cs: CallSite,
    lines: list[str],
    newly_required_by_field: dict[str, FieldChange],
) -> PatchRecord:
    """
    Remove a keyword argument from the call on cs.line.

    If the REMOVED field has a corresponding NEWLY_REQUIRED field on the same
    resource (e.g. source → payment_method), and the old field is the only
    kwarg on that line, swap the name rather than delete the line.
    """
    line_idx = cs.line - 1
    old_field = cs.change.path.split(".")[-1]
    original_line = lines[line_idx]

    # Check whether there is a NEWLY_REQUIRED counterpart on the same resource
    nr_change = newly_required_by_field.get(old_field)
    if nr_change is not None:
        new_field = nr_change.path.split(".")[-1]
        patched_line = re.sub(
            rf'\b{re.escape(old_field)}\s*=',
            f"{new_field}=",
            original_line,
            count=1,
        )
        if patched_line != original_line:
            lines[line_idx] = patched_line
            diff = _make_diff(cs.file, [original_line], [patched_line], cs.line)
            return PatchRecord(
                call_site=cs,
                status=PatchStatus.MECHANICAL,
                description=(
                    f"Replaced removed kwarg '{old_field}' with newly-required "
                    f"'{new_field}' (same resource, same position)."
                ),
                unified_diff=diff,
            )

    # No counterpart — remove the entire kwarg line.
    # Only remove if the kwarg appears alone on this line (safe heuristic).
    stripped = original_line.strip().rstrip(",")
    if re.match(rf'^{re.escape(old_field)}\s*=', stripped):
        lines.pop(line_idx)
        diff = _make_diff(cs.file, [original_line], [], cs.line)
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.MECHANICAL,
            description=f"Removed keyword argument '{old_field}' (field no longer exists in new spec).",
            unified_diff=diff,
        )

    # kwarg shares a line with other code — insert a removal comment instead
    return _insert_comment(
        cs, lines,
        f"# REMOVED: kwarg '{old_field}' no longer exists in new spec — remove it",
    )


def _patch_newly_required(cs: CallSite, lines: list[str]) -> PatchRecord:
    """
    A required field is missing from a create() call.
    Try LLM; fall back to # HUMAN REVIEW comment.
    """
    source_lines_raw = [l.rstrip("\n") for l in lines]
    suggestion = llm_suggest_patch(cs, source_lines_raw)
    field = cs.change.path.split(".")[-1]

    if suggestion and field in suggestion:
        # LLM returned something that at least mentions the field name.
        line_idx = cs.line - 1
        original_line = lines[line_idx]
        # The LLM is asked for the one marked line; apply it directly.
        indent = _leading_whitespace(original_line)
        new_line = suggestion if suggestion.endswith("\n") else suggestion + "\n"
        lines[line_idx] = new_line
        diff = _make_diff(cs.file, [original_line], [new_line], cs.line)
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.LLM,
            description=f"watsonx.ai added missing required kwarg '{field}'.",
            unified_diff=diff,
        )

    # LLM unavailable or unhelpful — leave a conspicuous comment
    return _insert_comment(
        cs, lines,
        f"# HUMAN REVIEW: '{field}' is now required — add it to this call",
    )


def _insert_comment(cs: CallSite, lines: list[str], comment_text: str) -> PatchRecord:
    """Insert a comment line immediately above cs.line."""
    line_idx = cs.line - 1
    indent = _leading_whitespace(lines[line_idx])
    comment_line = f"{indent}{comment_text}\n"
    # Idempotency: skip if an identical comment already precedes this line
    if line_idx > 0 and lines[line_idx - 1].strip() == comment_text.strip():
        return PatchRecord(
            call_site=cs,
            status=PatchStatus.MECHANICAL,
            description=f"Comment already present above line {cs.line} — skipped.",
        )
    lines.insert(line_idx, comment_line)
    status = (
        PatchStatus.HUMAN
        if "HUMAN REVIEW" in comment_text
        else PatchStatus.MECHANICAL
    )
    return PatchRecord(
        call_site=cs,
        status=status,
        description=comment_text.lstrip("# "),
        unified_diff=_make_diff(cs.file, [], [comment_line], cs.line),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _make_diff(
    filename: str,
    old_lines: list[str],
    new_lines: list[str],
    start_line: int,
) -> str:
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=0,
        )
    )


def _build_newly_required_index(spec_diff: SpecDiff) -> dict[str, FieldChange]:
    """
    Build a mapping from a *removed* field name to the NEWLY_REQUIRED change
    that should replace it, keyed by schema resource.

    Used so _patch_removed_kwarg can do a swap instead of a plain delete when
    the old field has a direct newly-required successor (e.g. source → payment_method
    on Charge).
    """
    from backend.pipeline.phase2_codebase_scan import (
        _schema_to_resource,
        _schema_name_from_path,
        _field_name_from_path,
    )

    # Build: resource → list[newly_required field names]
    nr_by_resource: dict[str, list[str]] = {}
    nr_change_by_field: dict[str, FieldChange] = {}
    for ch in spec_diff.newly_required:
        resource = _schema_to_resource(_schema_name_from_path(ch.path))
        if resource:
            field = _field_name_from_path(ch.path)
            nr_by_resource.setdefault(resource, []).append(field)
            nr_change_by_field[field] = ch

    # Build: removed field name → newly_required FieldChange on same resource,
    # only when there is exactly ONE newly_required field for that resource
    # (unambiguous swap).
    result: dict[str, FieldChange] = {}
    for ch in spec_diff.removals:
        resource = _schema_to_resource(_schema_name_from_path(ch.path))
        if not resource:
            continue
        candidates = nr_by_resource.get(resource, [])
        if len(candidates) == 1:
            nr_field = candidates[0]
            result[_field_name_from_path(ch.path)] = nr_change_by_field[nr_field]

    return result
