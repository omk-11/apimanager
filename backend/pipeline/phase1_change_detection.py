"""
Phase 1: diff two OpenAPI spec versions into structured changes.

Entry point:
    detect_changes(old_spec: dict, new_spec: dict) -> SpecDiff

What it detects (one FieldChange per item):
  RENAMED           — field present in old, absent in new, but a same-type field
                      with similarity >= 0.4 exists in new (SequenceMatcher).
                      NOTE: lexically dissimilar renames (e.g. source ->
                      payment_method) are NOT detected here; they surface as
                      REMOVED + NEWLY_REQUIRED, which is the safer classification.
  REMOVED           — field present in old, absent in new, no rename candidate.
  REMOVED_ENDPOINT  — HTTP operation present in old spec, absent from new spec.
  NEWLY_REQUIRED    — field present in new's required[] but not old's required[].
  DEPRECATED        — operation marked deprecated:true in new but not in old.
  TYPE_CHANGED      — field present in both old and new with a different "type".

Spec traversal:
  - Walks components/schemas/* for field-level changes.
  - Walks paths/*/[method] for deprecated + removed endpoint changes.
  - Resolves $ref one level deep (only within #/components/schemas/).
  - Does not recurse into nested object properties beyond one level.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from backend.pipeline.schema import ChangeKind, FieldChange, SpecDiff

# Minimum similarity ratio to treat a field as a rename candidate.
_RENAME_THRESHOLD = 0.4


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_changes(old_spec: dict, new_spec: dict) -> SpecDiff:
    """
    Diff *old_spec* against *new_spec* and return a populated SpecDiff.

    Both arguments must be dicts already parsed from OpenAPI 3.x JSON.
    Use load_spec() below if you need to load from a file path first.
    """
    diff = SpecDiff()

    old_schemas = _resolve_schemas(old_spec)
    new_schemas = _resolve_schemas(new_spec)

    # 1. Schema-level field changes (renamed, removed, newly_required, type_changed)
    all_schema_names = set(old_schemas) | set(new_schemas)
    for schema_name in sorted(all_schema_names):
        old_schema = old_schemas.get(schema_name, {})
        new_schema = new_schemas.get(schema_name, {})
        _diff_schema(
            schema_name=schema_name,
            old_schema=old_schema,
            new_schema=new_schema,
            diff=diff,
            endpoint_tag=_tag_for_schema(schema_name, old_spec, new_spec),
        )

    # 2. Operation-level changes (deprecated + removed endpoints)
    _diff_deprecated_operations(old_spec, new_spec, diff)
    _diff_removed_operations(old_spec, new_spec, diff)

    return diff


def load_spec(path: str | Path) -> dict:
    """Load an OpenAPI spec from a JSON file path."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Internal: spec traversal helpers
# ---------------------------------------------------------------------------

def _resolve_ref(ref: str, spec: dict) -> dict:
    """
    Resolve a $ref string of the form '#/components/schemas/Foo'.
    Returns an empty dict if the ref cannot be resolved.
    """
    if not ref.startswith("#/"):
        return {}
    parts = ref.lstrip("#/").split("/")
    node: Any = spec
    for part in parts:
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _resolve_schemas(spec: dict) -> dict[str, dict]:
    """
    Return a flat mapping of schema_name → resolved schema object for all
    schemas under components/schemas/.
    """
    raw: dict = spec.get("components", {}).get("schemas", {})
    resolved: dict[str, dict] = {}
    for name, schema in raw.items():
        if "$ref" in schema:
            resolved[name] = _resolve_ref(schema["$ref"], spec)
        else:
            resolved[name] = schema
    return resolved


def _get_properties(schema: dict) -> dict[str, dict]:
    """Return the properties dict from a schema, or {} if absent."""
    return schema.get("properties", {})


def _get_required(schema: dict) -> set[str]:
    """Return the set of required field names from a schema."""
    return set(schema.get("required", []))


def _tag_for_schema(schema_name: str, old_spec: dict, new_spec: dict) -> str | None:
    """
    Heuristically find an OpenAPI tag associated with a schema by looking at
    which operations reference it in their request/response bodies.
    Returns the first tag found, or None.
    """
    for spec in (new_spec, old_spec):
        for _path, path_item in spec.get("paths", {}).items():
            for _method, op in path_item.items():
                if not isinstance(op, dict):
                    continue
                op_str = json.dumps(op)
                if schema_name in op_str:
                    tags = op.get("tags", [])
                    if tags:
                        return tags[0]
    return None


# ---------------------------------------------------------------------------
# Internal: rename detection via SequenceMatcher
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _find_rename_candidate(
    old_field: str,
    old_type: str | None,
    new_fields: dict[str, dict],
    already_claimed: set[str],
) -> str | None:
    """
    Among *new_fields* (excluding already-claimed ones), find the best rename
    candidate for *old_field*:
      - Same type as old_field (if type info is available).
      - Similarity ≥ _RENAME_THRESHOLD.
      - No tie — if two candidates share the highest ratio, return None
        (ambiguous; falls through to REMOVED).
    """
    best_name: str | None = None
    best_ratio: float = 0.0
    tie: bool = False

    for new_name, new_props in new_fields.items():
        if new_name in already_claimed:
            continue
        # Type guard: only match same-type fields when type info is present
        new_type = new_props.get("type")
        if old_type is not None and new_type is not None and old_type != new_type:
            continue
        ratio = _similarity(old_field, new_name)
        if ratio < _RENAME_THRESHOLD:
            continue
        if ratio > best_ratio:
            best_ratio = ratio
            best_name = new_name
            tie = False
        elif ratio == best_ratio:
            tie = True

    return None if tie else best_name


# ---------------------------------------------------------------------------
# Internal: per-schema diffing
# ---------------------------------------------------------------------------

def _diff_schema(
    *,
    schema_name: str,
    old_schema: dict,
    new_schema: dict,
    diff: SpecDiff,
    endpoint_tag: str | None,
    parent_path: str = "",
) -> None:
    """
    Diff one schema object (one level deep; recurse into nested objects).
    Populates *diff* in place.
    """
    old_props = _get_properties(old_schema)
    new_props = _get_properties(new_schema)
    old_required = _get_required(old_schema)
    new_required = _get_required(new_schema)

    # Track which new fields have been matched to a rename so we don't
    # double-count them as newly-required / additions later.
    claimed_as_rename_target: set[str] = set()

    # --- Pass 1: type changes on fields present in both ---
    for field_name in sorted(set(old_props) & set(new_props)):
        path = f"{parent_path}{field_name}" if parent_path else f"{schema_name}.{field_name}"
        old_type = old_props[field_name].get("type")
        new_type = new_props[field_name].get("type")
        if old_type and new_type and old_type != new_type:
            diff.type_changes.append(
                FieldChange(
                    kind=ChangeKind.TYPE_CHANGED,
                    path=path,
                    old_type=old_type,
                    new_type=new_type,
                    endpoint_tag=endpoint_tag,
                )
            )

        # Recurse into nested object schemas (one extra level)
        if (
            not parent_path  # only recurse one level beyond schema root
            and old_props[field_name].get("type") == "object"
            and new_props[field_name].get("type") == "object"
        ):
            _diff_schema(
                schema_name=schema_name,
                old_schema=old_props[field_name],
                new_schema=new_props[field_name],
                diff=diff,
                endpoint_tag=endpoint_tag,
                parent_path=f"{schema_name}.{field_name}.",
            )

    # --- Pass 2: fields in old but not in new → rename or removal ---
    removed_fields = sorted(set(old_props) - set(new_props))
    # new fields not in old — potential rename targets
    added_fields = {n: p for n, p in new_props.items() if n not in old_props}

    for field_name in removed_fields:
        path = f"{parent_path}{field_name}" if parent_path else f"{schema_name}.{field_name}"
        old_type = old_props[field_name].get("type")
        rename_target = _find_rename_candidate(
            field_name, old_type, added_fields, claimed_as_rename_target
        )
        if rename_target is not None:
            claimed_as_rename_target.add(rename_target)
            diff.renames.append(
                FieldChange(
                    kind=ChangeKind.RENAMED,
                    path=path,
                    new_name=rename_target,
                    old_type=old_type,
                    endpoint_tag=endpoint_tag,
                )
            )
        else:
            diff.removals.append(
                FieldChange(
                    kind=ChangeKind.REMOVED,
                    path=path,
                    old_type=old_type,
                    endpoint_tag=endpoint_tag,
                )
            )

    # --- Pass 3: newly required fields ---
    for field_name in sorted(new_required - old_required):
        # Only flag if the field actually exists in the new schema
        if field_name not in new_props:
            continue
        path = f"{parent_path}{field_name}" if parent_path else f"{schema_name}.{field_name}"
        diff.newly_required.append(
            FieldChange(
                kind=ChangeKind.NEWLY_REQUIRED,
                path=path,
                endpoint_tag=endpoint_tag,
            )
        )


# ---------------------------------------------------------------------------
# Internal: deprecated operation detection
# ---------------------------------------------------------------------------

def _diff_deprecated_operations(
    old_spec: dict, new_spec: dict, diff: SpecDiff
) -> None:
    """
    Detect operations that became deprecated between old and new spec.
    An operation is considered newly deprecated when:
      - It exists in both specs at the same path+method.
      - new_op.get("deprecated") is True and old_op.get("deprecated") is not True.
    """
    old_paths: dict = old_spec.get("paths", {})
    new_paths: dict = new_spec.get("paths", {})

    for path_str, new_path_item in new_paths.items():
        old_path_item = old_paths.get(path_str, {})
        for method, new_op in new_path_item.items():
            if not isinstance(new_op, dict):
                continue
            old_op = old_path_item.get(method, {})
            if not isinstance(old_op, dict):
                old_op = {}

            if new_op.get("deprecated") and not old_op.get("deprecated"):
                tags = new_op.get("tags", [])
                endpoint_tag = tags[0] if tags else None
                operation_id = new_op.get("operationId", f"{method.upper()} {path_str}")
                hint = new_op.get("description", "")
                diff.deprecated.append(
                    FieldChange(
                        kind=ChangeKind.DEPRECATED,
                        path=f"{method.upper()} {path_str}",
                        replacement_hint=hint or None,
                        endpoint_tag=endpoint_tag,
                    )
                )


def _diff_removed_operations(
    old_spec: dict, new_spec: dict, diff: SpecDiff
) -> None:
    """
    Detect HTTP operations present in old spec but absent from new spec.
    Distinct from deprecated: these endpoints are fully gone.
    """
    old_paths: dict = old_spec.get("paths", {})
    new_paths: dict = new_spec.get("paths", {})

    for path_str, old_path_item in old_paths.items():
        new_path_item = new_paths.get(path_str, {})
        for method, old_op in old_path_item.items():
            if not isinstance(old_op, dict):
                continue
            if method not in new_path_item:
                tags = old_op.get("tags", [])
                endpoint_tag = tags[0] if tags else None
                diff.removals.append(
                    FieldChange(
                        kind=ChangeKind.REMOVED_ENDPOINT,
                        path=f"{method.upper()} {path_str}",
                        endpoint_tag=endpoint_tag,
                    )
                )
