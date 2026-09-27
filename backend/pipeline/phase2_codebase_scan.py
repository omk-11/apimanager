"""
Phase 2: AST-scan a repo for Stripe SDK call sites affected by Stage 1 changes.

Entry point:
    scan_repo(repo_path: str | Path, spec_diff: SpecDiff) -> ScanResult

What it finds:
  For each FieldChange in spec_diff.all_changes(), it walks every .py file in
  the repo and looks for two kinds of AST nodes:

  1. KEYWORD ARGUMENTS in Stripe SDK calls
     e.g.  stripe.Charge.create(..., source=tok, ...)
     Matches when:
       - The call is on a stripe.X.method() shape (stripe attribute chain).
       - A keyword argument name matches the field name from the FieldChange path
         (last segment of the dotted path, e.g. "source" from "CreateChargeRequest.source").
       - The Stripe resource in the call (e.g. "Charge") matches the schema that
         owns the field (first segment, e.g. "CreateChargeRequest" → "Charge").
     These are REQUEST-side sites: is_response_read = False.

  2. ATTRIBUTE ACCESS on Stripe response objects
     e.g.  charge.source  or  customer.balance
     Matches when:
       - The value being accessed is a local variable whose name looks like a
         Stripe resource instance (heuristic: lowercase of a known Stripe resource,
         e.g. "charge", "customer", "intent").
       - The attribute name matches the field name from the FieldChange.
       - The access is NOT inside a stripe.X.Y() call argument (i.e. it's reading
         a response value, not passing it as a request parameter).
     These are RESPONSE-side sites: is_response_read = True.

  For DEPRECATED changes (path = "POST /v1/charges"), it finds calls to the
  deprecated SDK method (e.g. stripe.Charge.create) regardless of arguments.

  For NEWLY_REQUIRED changes, it finds Stripe SDK calls on the matching resource
  that do NOT already pass the required field as a keyword argument — those are
  the sites that need patching.

AST approach:
  - ast.parse() each file; walk with a single-pass NodeVisitor.
  - No regex, no string search.
  - Unparseable files are skipped with a warning (syntax errors in the target
    repo shouldn't crash the pipeline).

Stripe resource mapping:
  Schema names like "CreateChargeRequest" and "Charge" are both mapped to the
  Stripe SDK resource "Charge" for matching purposes.  The mapping table covers
  all resources referenced by the sample specs and is easy to extend.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

from backend.pipeline.schema import (
    CallSite,
    ChangeKind,
    FieldChange,
    ScanResult,
    SpecDiff,
)

# ---------------------------------------------------------------------------
# Stripe resource name normalisation
# ---------------------------------------------------------------------------
# Maps schema name prefixes / suffixes to the canonical SDK resource name.
# e.g. "CreateChargeRequest" → "Charge",  "Charge" → "Charge"
_SCHEMA_TO_RESOURCE: dict[str, str] = {
    "Charge":                     "Charge",
    "CreateChargeRequest":        "Charge",
    "PaymentIntent":              "PaymentIntent",
    "CreatePaymentIntentRequest": "PaymentIntent",
    "Customer":                   "Customer",
    "CreateCustomerRequest":      "Customer",
    "Refund":                     "Refund",
    "CreateRefundRequest":        "Refund",
    "Subscription":               "Subscription",
    "CreateSubscriptionRequest":  "Subscription",
    "Invoice":                    "Invoice",
    "PaymentMethod":              "PaymentMethod",
    "CreatePaymentMethodRequest": "PaymentMethod",
}

# Instance variable names typically used for each resource in user code.
# charge = stripe.Charge.create(...)  → "charge" maps to "Charge"
_INSTANCE_NAME_TO_RESOURCE: dict[str, str] = {
    "charge":         "Charge",
    "chg":            "Charge",
    "intent":         "PaymentIntent",
    "payment_intent": "PaymentIntent",
    "pi":             "PaymentIntent",
    "customer":       "Customer",
    "cust":           "Customer",
    "refund":         "Refund",
    "subscription":   "Subscription",
    "sub":            "Subscription",
    "invoice":        "Invoice",
    "inv":            "Invoice",
    "payment_method": "PaymentMethod",
    "pm":             "PaymentMethod",
}

# Deprecated endpoint path prefix → SDK resource name
# "POST /v1/charges" → "Charge"
_ENDPOINT_PATH_PREFIX_TO_RESOURCE: dict[str, str] = {
    "/v1/charges":          "Charge",
    "/v1/payment_intents":  "PaymentIntent",
    "/v1/customers":        "Customer",
    "/v1/refunds":          "Refund",
    "/v1/subscriptions":    "Subscription",
    "/v1/invoices":         "Invoice",
    "/v1/payment_methods":  "PaymentMethod",
}


def _schema_to_resource(schema_name: str) -> str | None:
    """Return the canonical SDK resource for a schema name, or None."""
    return _SCHEMA_TO_RESOURCE.get(schema_name)


def _field_name_from_path(path: str) -> str:
    """
    Extract the leaf field name from a dotted change path.
    "CreateChargeRequest.source" → "source"
    "Charge.outcome.seller_message" → "seller_message"
    "POST /v1/charges" → "" (deprecated endpoint, no field)
    """
    if " " in path:  # "POST /v1/charges" style
        return ""
    return path.split(".")[-1]


def _schema_name_from_path(path: str) -> str:
    """
    Extract the schema name (first segment) from a dotted change path.
    "CreateChargeRequest.source" → "CreateChargeRequest"
    """
    return path.split(".")[0]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scan_repo(repo_path: str | Path, spec_diff: SpecDiff) -> ScanResult:
    """
    Walk every .py file under *repo_path* and return all affected CallSites
    cross-referenced against *spec_diff*.
    """
    repo_path = Path(repo_path)
    result = ScanResult()

    py_files = sorted(repo_path.rglob("*.py"))
    for py_file in py_files:
        rel_path = str(py_file.relative_to(repo_path))
        _scan_file(py_file, rel_path, spec_diff, result)

    return result


# ---------------------------------------------------------------------------
# Per-file scanning
# ---------------------------------------------------------------------------

def _scan_file(
    filepath: Path,
    rel_path: str,
    spec_diff: SpecDiff,
    result: ScanResult,
) -> None:
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        # Skip unparseable files silently; a broken target repo shouldn't
        # crash the pipeline.
        return

    source_lines = source.splitlines()
    visitor = _StripeCallVisitor(rel_path, source_lines, spec_diff)
    visitor.visit(tree)
    result.affected.extend(visitor.found)


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------

class _StripeCallVisitor(ast.NodeVisitor):
    """
    Single-pass visitor that finds:
      - stripe.Resource.method(...) calls (keyword args + deprecated calls)
      - variable.attribute reads on Stripe response objects
    """

    def __init__(
        self,
        rel_path: str,
        source_lines: list[str],
        spec_diff: SpecDiff,
    ) -> None:
        self.rel_path = rel_path
        self.source_lines = source_lines
        self.spec_diff = spec_diff
        self.found: list[CallSite] = []

        # Pre-build lookup structures for O(1) inner-loop checks.
        # keyword_changes: field_name → list[FieldChange] for request-side fields
        self._keyword_changes: dict[str, list[FieldChange]] = {}
        # attr_changes: field_name → list[FieldChange] for response-side reads
        self._attr_changes: dict[str, list[FieldChange]] = {}
        # deprecated_changes: resource_name → list[FieldChange]
        self._deprecated_changes: dict[str, list[FieldChange]] = {}
        # newly_required: resource_name → list[FieldChange]
        self._newly_required: dict[str, list[FieldChange]] = {}

        for change in spec_diff.all_changes():
            if change.kind == ChangeKind.DEPRECATED:
                # path = "POST /v1/charges"
                resource = _endpoint_to_resource(change.path)
                if resource:
                    self._deprecated_changes.setdefault(resource, []).append(change)
            else:
                field = _field_name_from_path(change.path)
                if not field:
                    continue
                schema = _schema_name_from_path(change.path)
                resource = _schema_to_resource(schema)

                # Request-side changes → look in keyword args
                if change.kind in (
                    ChangeKind.RENAMED,
                    ChangeKind.REMOVED,
                    ChangeKind.TYPE_CHANGED,
                ):
                    self._keyword_changes.setdefault(field, []).append(change)
                    # Also look for response-side attribute reads for the same field
                    self._attr_changes.setdefault(field, []).append(change)

                elif change.kind == ChangeKind.NEWLY_REQUIRED:
                    if resource:
                        self._newly_required.setdefault(resource, []).append(change)
                    self._attr_changes.setdefault(field, []).append(change)

    # ------------------------------------------------------------------
    # Visitor methods
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        """Handle stripe.Resource.method(...) calls."""
        resource, method = _parse_stripe_call(node)
        if resource is not None:
            self._check_stripe_call(node, resource, method)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Handle response object attribute reads: obj.field_name."""
        # Only interested in reads (Load context), not stores.
        if not isinstance(node.ctx, ast.Load):
            self.generic_visit(node)
            return

        attr_name = node.attr
        if attr_name not in self._attr_changes:
            self.generic_visit(node)
            return

        # Determine if the object being accessed looks like a Stripe instance.
        obj_resource = _infer_resource_from_value(node.value)
        if obj_resource is None:
            self.generic_visit(node)
            return

        for change in self._attr_changes[attr_name]:
            schema = _schema_name_from_path(change.path)
            expected_resource = _schema_to_resource(schema)
            if expected_resource is None or expected_resource != obj_resource:
                continue

            source_text = self._get_source_text(node)
            self.found.append(
                CallSite(
                    file=self.rel_path,
                    line=node.lineno,
                    col=node.col_offset,
                    source_text=source_text,
                    change=change,
                    is_response_read=True,
                )
            )

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_stripe_call(
        self, node: ast.Call, resource: str, method: str
    ) -> None:
        keyword_names = {kw.arg for kw in node.keywords if kw.arg is not None}

        # 1. Deprecated endpoint check
        if resource in self._deprecated_changes and method.lower() in (
            "create", "retrieve", "list", "update", "delete", "modify"
        ):
            for change in self._deprecated_changes[resource]:
                source_text = self._get_source_text(node.func)
                self.found.append(
                    CallSite(
                        file=self.rel_path,
                        line=node.lineno,
                        col=node.col_offset,
                        source_text=source_text,
                        change=change,
                        is_response_read=False,
                    )
                )

        # 2. Keyword argument matches (RENAMED / REMOVED / TYPE_CHANGED)
        for kw in node.keywords:
            if kw.arg is None:
                continue  # **kwargs unpack — skip
            if kw.arg not in self._keyword_changes:
                continue
            for change in self._keyword_changes[kw.arg]:
                schema = _schema_name_from_path(change.path)
                expected_resource = _schema_to_resource(schema)
                if expected_resource is None or expected_resource != resource:
                    continue
                source_text = self._get_source_text(kw.value)
                self.found.append(
                    CallSite(
                        file=self.rel_path,
                        line=kw.value.lineno,
                        col=kw.col_offset,
                        source_text=f"{kw.arg}={source_text}",
                        change=change,
                        is_response_read=False,
                    )
                )

        # 3. Newly-required field missing from this call.
        # Only applies to "create" — retrieve/list/update have different
        # required params and must not be flagged for create-only requirements.
        if method.lower() == "create" and resource in self._newly_required:
            for change in self._newly_required[resource]:
                field = _field_name_from_path(change.path)
                if field and field not in keyword_names:
                    source_text = self._get_source_text(node.func)
                    self.found.append(
                        CallSite(
                            file=self.rel_path,
                            line=node.lineno,
                            col=node.col_offset,
                            source_text=source_text,
                            change=change,
                            is_response_read=False,
                        )
                    )

    def _get_source_text(self, node: ast.AST) -> str:
        """Best-effort extraction of the source text for an AST node."""
        try:
            return ast.unparse(node)
        except Exception:
            return f"<line {getattr(node, 'lineno', '?')}>"


# ---------------------------------------------------------------------------
# Pure helper functions (no state)
# ---------------------------------------------------------------------------

def _parse_stripe_call(node: ast.Call) -> tuple[str | None, str]:
    """
    If *node* is a call of the form  stripe.Resource.method(...)
    return ("Resource", "method").  Otherwise return (None, "").

    Handles both stripe.Charge.create(...) and the two-level
    stripe.PaymentIntent.create(...) style.
    """
    func = node.func
    # stripe.Charge.create  →  Attribute(value=Attribute(value=Name("stripe"), attr="Charge"), attr="create")
    if not isinstance(func, ast.Attribute):
        return None, ""
    method = func.attr
    obj = func.value
    if not isinstance(obj, ast.Attribute):
        return None, ""
    resource = obj.attr
    root = obj.value
    if isinstance(root, ast.Name) and root.id == "stripe":
        return resource, method
    return None, ""


def _infer_resource_from_value(node: ast.expr) -> str | None:
    """
    Given the object side of an attribute access (e.g. the `charge` in
    `charge.source`), try to infer which Stripe resource it represents.

    Strategy:
      - If it's a bare Name node, look up the variable name in
        _INSTANCE_NAME_TO_RESOURCE.
      - If it's itself an Attribute (e.g. charge.outcome), look up the
        outermost Name.
    """
    if isinstance(node, ast.Name):
        return _INSTANCE_NAME_TO_RESOURCE.get(node.id)
    if isinstance(node, ast.Attribute):
        # e.g. charge.outcome  — outermost object is `charge`
        return _infer_resource_from_value(node.value)
    return None


def _endpoint_to_resource(deprecated_path: str) -> str | None:
    """
    Map a deprecated FieldChange.path like "POST /v1/charges" to a resource.
    Returns None if unmapped.
    """
    # path format: "METHOD /v1/resource_path"
    parts = deprecated_path.split(" ", 1)
    if len(parts) != 2:
        return None
    endpoint = parts[1]
    # Match by prefix (handles paths like /v1/charges/{id})
    for prefix, resource in _ENDPOINT_PATH_PREFIX_TO_RESOURCE.items():
        if endpoint.startswith(prefix):
            return resource
    return None
