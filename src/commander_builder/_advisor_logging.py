"""Per-recommendation decision logging for the advisor pipeline.

When the audit produces a misclassification ("Cyclonic Rift was tagged
``other`` instead of ``wipe``"), the only way to surface it today is
to either:

  (a) eyeball the dashboard's Categories panel, or
  (b) write a synthetic test (which can lie — synthetic oracle text
      happens to match overly-permissive regexes; see the 9 bugs
      caught only by live-browser audit in 2026-05-14)

This module adds a third option: write one structured line per
recommendation to a per-process log file, opt-in via the
``COMMANDER_BUILDER_LOG_DECISIONS`` environment variable. Pattern
drift then surfaces in seconds via ``grep`` rather than via Chrome
screenshot.

Wire format (one line per rec)::

    2026-05-14T12:34:56Z deck=[USER] Wyrm Sovereign [B4] commander=The Ur-Dragon \\
        source=heuristic action=add card=Cyclonic Rift role=wipe \\
        match_pct=None evidence_source=edhrec.high_synergy name_known=True

Each field is ``key=value`` (no quotes, no escapes — values are
already free of whitespace because we render card names from
Scryfall's canonical form). Lines run-to-completion fit in a single
``grep`` filter; multi-line entries would break that.

Opt-in:

  Set ``COMMANDER_BUILDER_LOG_DECISIONS=1`` (or ``true`` / ``yes``).
  The log lands at ``<deck_dir>.parent.parent/_audit_decisions.log``
  alongside the existing ``_js_errors.log`` and
  ``_forge_py_correlation.csv``. Off by default — production audits
  don't pay the disk-write cost unless someone's diagnosing.

Append-only; never read from this module. Rotation is the operator's
problem (it's a debug aid, not a long-term sink). Best-effort:
file-open failures are swallowed so logging never breaks the audit
itself.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _log_path_for(deck_path: Path) -> Path:
    """Resolve the decisions log location relative to the deck dir.

    Matches where ``_js_errors.log`` and ``_forge_py_correlation.csv``
    already land (``deck_dir.parent.parent``), so all operator-level
    audit artifacts cluster in one folder.
    """
    return deck_path.parent.parent.parent / "_audit_decisions.log"


def is_enabled() -> bool:
    """True when the operator has opted in via the env var.

    Truthy values mirror the rest of the codebase: ``1``, ``true``,
    ``yes`` (case-insensitive). Anything else = off.
    """
    return os.environ.get(
        "COMMANDER_BUILDER_LOG_DECISIONS", "",
    ).strip().lower() in ("1", "true", "yes")


def log_decisions(
    deck_path: Path,
    commander_names: list[str],
    effective_source: str,
    recommendations,
    fallback_reason: Optional[str] = None,
) -> None:
    """Emit one structured log line per recommendation.

    ``recommendations`` is a list of ``SwapRecommendation``-like
    objects (we duck-type on ``.card``, ``.action``, ``.evidence``,
    ``.name_known``). No-op when the operator hasn't opted in via
    the env var — keeps production audits free of disk I/O.

    Best-effort: file write errors are caught and discarded. Don't
    let logging break the audit.
    """
    if not is_enabled():
        return
    if not recommendations:
        return

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    commander = commander_names[0] if commander_names else "<unknown>"
    deck_label = deck_path.name
    lines: list[str] = []
    for rec in recommendations:
        ev = getattr(rec, "evidence", None) or {}
        role = ev.get("role", "?")
        evidence_source = ev.get("source", "?")
        # match-pct calculation mirrors the web layer's
        # ``_match_pct_from_evidence`` but in-line here so the
        # advisor module doesn't have to depend on web/.
        in_n = ev.get("in_n_references")
        total = ev.get("total_references")
        if isinstance(total, int) and total > 0 and isinstance(in_n, int):
            match_pct: object = round(100 * in_n / total)
        else:
            inc = ev.get("inclusion_pct")
            syn = ev.get("synergy_pct")
            if inc is None and syn is None:
                match_pct = None
            else:
                inc_v = float(inc or 0)
                syn_v = min(float(syn or 0), 20.0)
                raw = inc_v + syn_v
                match_pct = round(raw) if raw > 0 else None
        name_known = getattr(rec, "name_known", None)
        # Card names can contain spaces ("Sol Ring") and apostrophes
        # ("Yavimaya, Cradle of Growth"). Use plain key=value; field
        # split on first '=' per token. No quoting needed because
        # the format is one rec per line and 'card=' is the last
        # multi-word field — every prior field is single-token.
        lines.append(
            f"{ts} deck={deck_label} commander={commander} "
            f"source={effective_source} "
            f"action={rec.action} role={role} "
            f"evidence_source={evidence_source} "
            f"match_pct={match_pct} name_known={name_known} "
            f"card={rec.card}"
        )

    if fallback_reason:
        lines.append(
            f"{ts} deck={deck_label} commander={commander} "
            f"event=fallback reason={fallback_reason}"
        )

    try:
        log_path = _log_path_for(deck_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
    except OSError:
        # Best-effort. A failed log write should never break the
        # audit response. Operators who care about the log will
        # notice it missing.
        return
