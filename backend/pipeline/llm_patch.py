"""
Phase 3b: LLM fallback for changes with no safe mechanical fix.

Uses OpenRouter (https://openrouter.ai) with the model:
    nvidia/nemotron-3-ultra-550b-a55b:free

Entry point:
    llm_suggest_patch(call_site: CallSite, source_lines: list[str]) -> str | None

Returns a suggested replacement for the single line identified by call_site,
or None if the call fails / returns something unusable.  The caller decides
whether to apply it or fall back to a # HUMAN REVIEW comment.

Configuration (environment variables):
    OPENROUTER_API_KEY   — OpenRouter API key (required)
                           Get one free at https://openrouter.ai/keys

The module only uses `requests` (already in requirements.txt) — no extra SDK needed.
"""
from __future__ import annotations

import os
import textwrap

import requests

from backend.pipeline.schema import CallSite

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL    = "nvidia/nemotron-ultra-253b-v1:free"

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(call_site: CallSite, source_lines: list[str]) -> str:
    line_idx = call_site.line - 1          # 0-based
    context_start = max(0, line_idx - 4)
    context_end   = min(len(source_lines), line_idx + 5)
    context = "\n".join(
        f"{'>>>' if i == line_idx else '   '} {source_lines[i]}"
        for i in range(context_start, context_end)
    )
    change = call_site.change
    return textwrap.dedent(f"""\
        You are a Python code migration assistant.

        A third-party API has changed. One line of Python code needs to be updated.

        Change description:
          kind      : {change.kind.value}
          field path: {change.path}
          new name  : {change.new_name or 'n/a'}
          old type  : {change.old_type or 'n/a'}
          new type  : {change.new_type or 'n/a'}
          hint      : {change.replacement_hint or 'n/a'}

        Source context (>>> marks the line to fix):
        {context}

        Return ONLY the corrected replacement for the marked line (>>>), preserving
        indentation exactly. Do not add explanation. Do not add markdown fences.
        If you are not confident, return the original line unchanged.
    """)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def llm_suggest_patch(call_site: CallSite, source_lines: list[str]) -> str | None:
    """
    Ask the LLM to suggest a replacement for the line at call_site.line.
    Returns the suggested line string (with original indentation) or None on
    any failure.
    """
    if not _API_KEY:
        return None

    prompt = _build_prompt(call_site, source_lines)

    try:
        return _call_openrouter(prompt)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# OpenRouter REST call
# ---------------------------------------------------------------------------

def _call_openrouter(prompt: str) -> str | None:
    resp = requests.post(
        _BASE_URL,
        headers={
            "Authorization": f"Bearer {_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/self-maintaining-apis",  # optional but polite
        },
        json={
            "model": _MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise Python code migration assistant. "
                        "When asked to fix a line, return only the fixed line — "
                        "no markdown, no explanation, no fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 128,
            "temperature": 0.0,
        },
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    return _clean(text)


def _clean(text: str) -> str | None:
    """Strip markdown fences and excess whitespace from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = [l for l in lines if not l.startswith("```")]
        text = "\n".join(inner).strip()
    return text if text else None
