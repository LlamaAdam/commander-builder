"""Shared robust JSON extraction for LLM responses.

Every LLM-backed path in this project (analyst verdicts, proposer
manifests, the auto-curate curator, the Claude advisor) asks the model
for "JSON ONLY" — and every one of them still occasionally receives:

  - Markdown code fences (```json ... ```), sometimes mid-response
  - Prose preamble ("Looking at this deck, I think...") before the JSON
  - Trailing prose after the object
  - Truncated JSON when the response hit max_tokens
  - Multiple ``{...}`` blocks when the model quotes an example

Before this module existed each call site hand-rolled its own partial
defense (a startswith-``` fence strip here, a greedy ``\\{.*\\}`` regex
there), and the *failure* behavior diverged too: proposer.claude_propose
let a bare JSONDecodeError escape into a broad except that silently
fell through to manual_propose — which then raised a misleading
``FileNotFoundError: No manifest at ...`` masking the real problem.

This module is the single source of truth for both halves:

  1. RECOVERY — ``try_extract_json_object`` implements the layered
     strategy (whole-text parse, fence strip, brace-counting scan) that
     used to live only in ``proposer._extract_curator_json``.
  2. FAILURE — ``extract_json_object`` raises ``LLMJsonError`` (a
     ValueError subclass) with the caller's context plus head/tail
     snippets of the raw response, so a garbage or truncated reply is a
     LOUD, diagnosable error instead of a silent fallback.

Callers whose contract genuinely wants an Optional (the curator's
``_extract_curator_json`` back-compat surface, pinned by tests) use the
``try_`` variant; everything else should use the raising variant.
"""

from __future__ import annotations

import json
from typing import Optional


class LLMJsonError(ValueError):
    """No JSON object could be recovered from an LLM response.

    Subclasses ValueError so generic ``except ValueError`` handling
    still works, but stays distinct from JSONDecodeError on purpose:
    routers must be able to tell "the backend returned garbage" (raise
    loudly / degrade with a warning) apart from "the backend is
    unavailable" (NotImplementedError — quiet fall-through). See
    ``proposer.propose()`` and ``analyst.analyze()``.
    """


# How much of the raw response to quote in an LLMJsonError. Enough to
# recognize what the model was doing (prose? half a JSON object?)
# without dumping a multi-KB deck audit into every log line.
_SNIPPET_CHARS = 200


def try_extract_json_object(text: str) -> Optional[dict]:
    """Best-effort recovery of a JSON *object* from LLM output.

    Returns the parsed dict, or None when no parseable object exists
    (prefer ``extract_json_object`` unless your contract needs None).

    Strategy, in order:

      1. Parse the whole (stripped) response. Covers the happy path
         where the model obeyed "JSON ONLY".
      2. If the text contains markdown fences, parse the first fenced
         block. Covers "```json\\n{...}\\n```" — including when prose
         precedes the fence, which the old startswith-``` checks in
         claude_propose / _advisor_claude missed entirely.
      3. Scan for the first balanced ``{...}`` block by counting
         braces, then retry from the next ``{`` if that block doesn't
         parse. Covers prose before/after the object and "prose with
         { in it } then the real JSON".

    The brace counter respects JSON string context (skips braces inside
    double-quoted runs, honors backslash escapes) because rationale
    strings legally contain ``{``/``}`` and a naive find-last-``}``
    mis-parses them.

    Only dicts are returned: every LLM contract in this project is an
    object schema, and passing a top-level list/scalar through would
    just move the crash to the caller's ``.get()`` (a real failure mode
    — the old analyst path did exactly that via AttributeError).
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None

    # Path 1: the whole response is the JSON object.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        # Top-level list/scalar: not what any caller wants — fall
        # through to the scanners, which may find an object inside.
    except json.JSONDecodeError:
        pass

    # Path 2: first markdown-fenced block, wherever it appears.
    if "```" in cleaned:
        fenced = cleaned.split("```", 2)
        if len(fenced) >= 2:
            inner = fenced[1]
            # Strip optional language tag like ```json
            inner = inner.split("\n", 1)[1] if "\n" in inner else inner
            inner = inner.rsplit("```", 1)[0].strip()
            try:
                parsed = json.loads(inner)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    # Path 3: first balanced ``{...}`` block in the text.
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidate = cleaned[start:end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        # That block didn't parse (or never closed — truncated
        # max_tokens output lands here) — try from the next ``{``.
        start = cleaned.find("{", start + 1)

    return None


def extract_json_object(text: str, *, context: str = "LLM response") -> dict:
    """Extract a JSON object from LLM output or raise ``LLMJsonError``.

    ``context`` names the call site (and ideally the model) so the
    error is actionable from a log line alone, e.g.
    ``claude_propose (model=claude-sonnet-4-5)``.

    The error message quotes the head AND tail of the response: the
    head shows whether the model wrote prose instead of JSON; the tail
    is the tell for max_tokens truncation (an object that just stops
    mid-string). Callers must NOT swallow this into a generic fallback
    — a garbage response is a real failure the operator needs to see.
    """
    result = try_extract_json_object(text)
    if result is not None:
        return result

    raw = text or ""
    if len(raw) <= 2 * _SNIPPET_CHARS:
        snippet = f"full response: {raw!r}"
    else:
        snippet = (
            f"head: {raw[:_SNIPPET_CHARS]!r} ... "
            f"tail: {raw[-_SNIPPET_CHARS:]!r}"
        )
    raise LLMJsonError(
        f"{context}: could not extract a JSON object from the model "
        f"response ({len(raw)} chars — possibly prose-only or truncated "
        f"by max_tokens). {snippet}"
    )
