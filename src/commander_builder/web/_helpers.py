"""Pure helper functions shared across the web layer's route handlers.

Extracted from ``web/app.py`` as part of the blueprint refactor
(tier-3 issue #3.1). Every function here is independent of Flask
state — no ``current_app``, no ``request``, no closure over
``create_app``'s arguments — so the route blueprints can import
them freely.

2026-06-12 split: this module outgrew its format-helpers charter, so
cohesive groups were extracted verbatim into sibling modules (and are
re-exported below for backward compatibility):

- ``deck_pricing``     Scryfall USD pricing over .dck blobs
- ``deck_text_ops``    .dck text transformations (paste normalize,
                       constructed conversion, padding, swap splicing)
- ``deck_insights``    analysis / projection helpers (suggested adds,
                       average-deck preview, salt warning, cross-deck
                       library search)

Functions still defined here:

- ``_resolve_deck_path``        deck id / explicit path → real file
- ``_bracket_from_filename``    parse [B<n>] suffix from a deck name
- ``_match_pct_from_evidence``  evidence dict → 0..100 or None
- ``_iteration_to_dict``        Iteration row → JSON-friendly dict
- ``read_protected_cards``      [metadata] Protect= entries → list

External callers should NOT import from this module — it's an
internal layout detail. The web layer's public surface stays
``commander_builder.web.app:create_app``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Backward-compatibility re-exports (2026-06-12 split).
#
# These names used to be defined in this module and are imported from
# here by the route blueprints, web/app.py, scripts/, and tests. They
# now live in the sibling modules below — keep importing them from
# their new homes; this shim exists so existing importers keep working
# unchanged.
# ---------------------------------------------------------------------------
from .deck_pricing import _total_price_for_deck_text  # noqa: F401
from .deck_text_ops import (  # noqa: F401
    _BASIC_LANDS,
    _apply_swaps_to_dck,
    _format_added_line,
    _normalize_pasted_deck,
    _pad_main_to_99,
    _to_constructed_format,
)
from .deck_insights import (  # noqa: F401
    _SALT_WARN_BRACKET_MAX,
    _SALT_WARN_THRESHOLD,
    _build_suggested_adds,
    _user_deck_card_names,
    decks_containing_card,
    project_average_deck_preview,
    project_salt_warning,
)


def _resolve_deck_path(
    deck_dir: Path, deck_id: Optional[str], explicit_path: Optional[str],
) -> Optional[Path]:
    """Resolve a deck identifier or explicit path to a real file.

    Both forms are validated against ``deck_dir`` — explicit paths must
    be inside ``deck_dir`` after resolution AND carry a ``.dck`` suffix,
    otherwise None is returned. The ``.dck`` lock matters because this
    resolver also backs the DELETE/PUT deck routes: without it a crafted
    ``?path=`` could target a non-deck file inside the dir (pool JSON,
    soak summary, a staged file) and the write/delete would clobber it.

    Moved here from ``web/app.py`` in the 2026-05-14 Tier-1 #0a
    cleanup so the 5 blueprint factories can import it directly
    instead of receiving it as a constructor parameter.
    """
    if deck_id:
        candidate = (deck_dir / f"{deck_id}.dck").resolve()
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    if explicit_path:
        candidate = Path(explicit_path).resolve()
        # Only ever resolve to actual deck files — never arbitrary files
        # that merely happen to live inside deck_dir.
        if candidate.suffix.lower() != ".dck":
            return None
        try:
            candidate.relative_to(deck_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.exists() else None
    return None


def _bracket_from_filename(deck_id: str | None) -> int | None:
    """Parse the ``[B<n>]`` suffix the user encodes in deck filenames.

    Returns the bracket integer (1..5) or None if the suffix is missing
    or unparseable. The filename is the user's declared/intended
    bracket; it should override the heuristic guess unless the request
    explicitly passes a different bracket.
    """
    if not deck_id:
        return None
    import re as _re
    m = _re.search(r"\[B(\d)\](?:\.dck)?\s*$", deck_id)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 5 else None


def _match_pct_from_evidence(evidence: dict | None) -> int | None:
    """Mirror deck_dashboard.match_score combination so audit output
    uses the same 0..100 scale the suggestion panel renders.

    Bracket-peers recs (source="bracket_peers") only set
    ``in_n_references`` / ``total_references`` rather than the EDHREC
    inclusion%/synergy% pair. When those are present we compute
    ``100 * in_n_references / total_references`` — a card in 5/5
    references shows 100, 3/5 shows 60. This keeps the UI's match-pct
    pill meaningful across all sources without bracket_peers needing
    to fabricate EDHREC-shaped fields.

    Returns ``None`` (JSON ``null``) when evidence carries no usable
    scoring signal — manabase essentials, vanilla Claude recs with
    no peer-ref data, etc. The UI branches on null and shows a
    source-specific badge (Manabase / Claude analyst) instead of a
    misleading "0%" pill. Before 2026-05-13 we returned 0 here and
    the UI rendered it as "0%", which looked identical to "this
    card is a bad match" rather than "this card has no inclusion%
    to score against."
    """
    if not evidence:
        return None
    # Prefer reference-frequency math when bracket_peers fields are set.
    total = evidence.get("total_references")
    in_n = evidence.get("in_n_references")
    if isinstance(total, int) and total > 0 and isinstance(in_n, int):
        return max(0, min(100, round(100 * in_n / total)))
    inclusion = evidence.get("inclusion_pct")
    synergy = evidence.get("synergy_pct")
    # If neither inclusion nor synergy was provided, the rec carries
    # no scoring signal — return None so the UI renders a
    # source-tag badge instead of a confusing "0%" pill.
    if inclusion is None and synergy is None:
        return None
    inclusion = float(inclusion or 0)
    synergy = min(float(synergy or 0), 20.0)
    raw = inclusion + synergy
    if raw <= 0:
        return None
    return max(1, min(100, round(raw)))


def _iteration_to_dict(it) -> dict:
    """JSON-friendly projection of an Iteration. Drops the deck_snapshot
    blob to keep payloads small — callers can re-request the full row
    via /api/iteration/<id> if we add it later."""
    return {
        "id": it.id,
        "deck_id": it.deck_id,
        "deck_name": it.deck_name,
        "bracket": it.bracket,
        "parent_id": it.parent_id,
        "audit_version": it.audit_version,
        "audit_manifest": it.audit_manifest,
        "verdict": it.verdict,
        "verdict_notes": it.verdict_notes,
        "win_rate_old": it.win_rate_old,
        "win_rate_new": it.win_rate_new,
        "margin": it.margin,
        "created_at": it.created_at,
        # Milestone (schema v2 / #012) — None when unset. Powers the
        # iteration-graph flag glyph + "reference baseline" filter
        # on /api/iterations.
        "milestone": getattr(it, "milestone", None),
    }


# ---------------------------------------------------------------------------
# Protected-cards list — pet cards the curator must not propose for cut
# ---------------------------------------------------------------------------
#
# Per-deck protection lives in the .dck file's ``[metadata]`` section as
# ``Protect=`` entries. Forge ignores unknown metadata keys, so the
# list travels with the deck file across imports, Moxfield round-trips,
# and version bumps without polluting anything Forge cares about.
#
# Format (either / both work, unioned):
#
#     [metadata]
#     Name=Goblin
#     Moxfield=abc123
#     Protect=Krenko, Mob Boss
#     Protect=Goblin Lackey, Skirk Prospector
#
# Comma-separated on a single line OR multiple Protect= lines; both
# accepted. Card names preserve casing on read so the UI can render
# them faithfully — comparison against cut suggestions folds case at
# the call site.


def read_protected_cards(deck_text: str) -> list[str]:
    """Parse ``[metadata] Protect=`` entries out of a .dck blob.

    Convention: **one card per Protect= line**, comma is literal.
    This way the most common case — protecting your commander —
    works naturally without quoting:

        Protect=Krenko, Mob Boss
        Protect=Jaya Ballard, Task Mage
        Protect=Sol Ring

    All three are single cards. To protect multiple cards, use
    multiple Protect= lines (not comma-separated).

    Compact comma-separated form is supported only when entries are
    wrapped in double quotes, so users who want a one-line list of
    no-comma names can still do:

        Protect="Sol Ring", "Lightning Bolt", "Counterspell"

    Quoted form mixes safely with bare lines on the same .dck.

    Returns a list preserving input order with duplicates collapsed
    case-insensitively. Empty list when no Protect= entries exist —
    the caller treats this as "no protection list configured."

    Whitespace trimmed per entry; empty entries silently dropped.
    """
    import re as _re
    # Quoted-entry pattern: matches `"..."` sequences. We pull each
    # quoted chunk out separately and treat unquoted leftover as a
    # single bare entry (preserves the comma-in-name case).
    _quoted_re = _re.compile(r'"([^"]*)"')

    protected: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = name.strip()
        if not n:
            return
        key = n.lower()
        if key in seen:
            return
        seen.add(key)
        protected.append(n)

    in_metadata = False
    for raw in deck_text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            in_metadata = s.lower() == "[metadata]"
            continue
        if not in_metadata or "=" not in s:
            continue
        key, _, value = s.partition("=")
        if key.strip().lower() != "protect":
            continue
        value = value.strip()
        if not value:
            continue
        if '"' in value:
            # Quoted form: pull each "..." chunk out; ignore any
            # commas / whitespace between chunks.
            for m in _quoted_re.finditer(value):
                _add(m.group(1))
        else:
            # Bare form: the entire value is ONE card name. Commas
            # inside the name (e.g. "Krenko, Mob Boss") stay literal
            # rather than splitting the name in half — which is the
            # whole point of this rule, since commanders are almost
            # always comma-named.
            _add(value)
    return protected
