"""Deck analysis / projection helpers for the web layer.

Functions that project advisor output and EDHREC data into
JSON-friendly shapes for the dashboard and audit panels, plus the
cross-deck library search. Extracted verbatim from ``web/_helpers.py``
(2026-06-12 split); ``_helpers`` re-exports every name here for
backward compatibility.
"""

from __future__ import annotations

from pathlib import Path

from ..dck_utils import CARD_LINE_RE


def _build_suggested_adds(deck_path: Path, bracket: int) -> list[dict]:
    """Project ``improvement_advisor.advise()`` recommendations into the
    shape ``deck_dashboard.build_dashboard`` expects for
    ``suggested``::

        [{"card": str, "inclusion_pct": float, "synergy_pct": float,
          "rationale": str, "price_usd": Optional[float]}, ...]

    Only `add` actions are forwarded — the dashboard's "suggested
    adds" panel is for cards to consider including, not cuts.
    Pulled out as a helper so both `/api/dashboard?advise=1` and
    `/api/advise` reuse the same projection.
    """
    from ..improvement_advisor import advise
    report = advise(deck_path, bracket=bracket)
    out: list[dict] = []
    for rec in report.recommendations:
        if rec.action != "add":
            continue
        ev = rec.evidence or {}
        out.append({
            "card": rec.card,
            "inclusion_pct": float(ev.get("inclusion_pct") or 0),
            "synergy_pct": float(ev.get("synergy_pct") or 0),
            "rationale": rec.reason or "",
            "price_usd": ev.get("price_usd"),
        })
    return out


# ---------------------------------------------------------------------------
# EDHREC average-deck preview projection
# ---------------------------------------------------------------------------
#
# AdviceReport carries an Optional[AverageDeck] (commit 4ee8a0e) and a
# lowercase-name → category map sourced from the commander page. This
# helper turns those into a JSON-friendly dict the audit-panel UI
# renders inside a collapsible <details> section. The UI doesn't open
# the section by default — most users only need it when they want to
# compare their list to the bracket archetype.


def _user_deck_card_names(deck_text: str) -> set[str]:
    """Extract the lowercase set of card names from a Forge .dck blob.

    Handles both bare ``1 Sol Ring`` and ``1 Sol Ring|CLB|871`` lines
    by stripping the optional edition tail. Section headers, metadata,
    and blank lines are ignored — only quantity-prefixed cards count.
    """
    out: set[str] = set()
    for raw in deck_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("["):
            continue
        m = CARD_LINE_RE.match(stripped)
        if m:
            out.add(m.group(2).strip().lower())
    return out


def project_average_deck_preview(
    average_deck,        # Optional[AverageDeck] — forward-typed to avoid cycle
    edhrec_categories,   # dict[str, str] — lowercase name → category
    user_deck_text: str,
):
    """Project an AverageDeck → JSON-friendly preview dict.

    Returns ``None`` when there's nothing to show:
      - ``average_deck`` is None (EDHREC unreachable, no published
        average deck for this commander+bracket)
      - ``average_deck.cards`` is empty (page parsed but produced
        zero entries)

    Otherwise returns::

        {
          "bracket_slug": str | None,
          "card_count": int,
          "cards": [
            {"name": str, "inclusion_pct": float,
             "category": str | None, "in_user_deck": bool},
            ...
          ]
        }

    Card ordering preserves the input list — EDHREC's average-deck page
    ranks by typical-build prominence and that ordering is meaningful.

    in_user_deck folds case both ways: the average-deck card name and
    the .dck-extracted name set are both lowercased before comparison.

    category match is also case-insensitive against ``edhrec_categories``;
    cards missing from the map surface ``category=None`` so the UI can
    group them under an 'Other' bucket without the helper inventing
    a label.
    """
    if average_deck is None:
        return None
    cards = list(getattr(average_deck, "cards", []) or [])
    if not cards:
        return None

    user_names = _user_deck_card_names(user_deck_text)
    # Categories map is keyed lowercase already (the advisor builds it
    # that way); fold the average-deck card name for the lookup.
    projected = []
    for entry in cards:
        key = (entry.name or "").lower()
        projected.append({
            "name": entry.name,
            "inclusion_pct": float(getattr(entry, "inclusion_pct", 0.0) or 0.0),
            "category": edhrec_categories.get(key),
            "in_user_deck": key in user_names,
        })

    return {
        "bracket_slug": getattr(average_deck, "bracket_slug", None),
        "card_count": len(projected),
        "cards": projected,
    }


# ---------------------------------------------------------------------------
# Salt-list warning aggregator
# ---------------------------------------------------------------------------
#
# Per-recommendation salt annotations already land on each add/cut
# entry (see routes_audit.py's salt_map lookups). The banner that the
# audit panel renders ABOVE the recommendations needs an aggregate
# view of the user's CURRENT deck — every salty card, sorted by score,
# regardless of whether the advisor flagged it for cut. That's what
# this helper produces.


# Default threshold for "salty" — EDHREC's salt scores run 0..5.
# Anything ≥ 1.5 is "noticeable salt" in their UI's color scale; we
# use the same cut-off so the banner reflects what a casual reader
# of EDHREC would already consider problematic.
_SALT_WARN_THRESHOLD = 1.5

# Brackets at which the warning shows. WotC's bracket guidance:
# B1 (Exhibition) + B2 (Core) are unconditionally casual; B3 (Upgraded)
# is "focused but still social". Salt is unwelcome at all three. B4+
# tables expect cEDH-grade picks and the banner just becomes noise.
_SALT_WARN_BRACKET_MAX = 3


def project_salt_warning(
    user_deck_text: str,
    salt_map: dict,
    bracket: int,
    *,
    threshold: float = _SALT_WARN_THRESHOLD,
    bracket_max: int = _SALT_WARN_BRACKET_MAX,
):
    """Aggregate salty cards in the user's deck into a banner payload.

    Returns ``None`` when there's no warning to show:
      - bracket > bracket_max (B4/B5 expect salty picks; banner = noise)
      - no salt_map (EDHREC unreachable)
      - no cards in the deck meet the threshold

    Otherwise returns::

        {
          "bracket": int,
          "count": int,
          "threshold": float,
          "cards": [
            {"name": str, "salt": float},
            ...    # sorted by salt desc, then name asc
          ]
        }

    The UI uses ``count`` for the headline ("3 salty cards at B2 —
    consider cutting"), iterates ``cards`` for the inline list, and
    ``threshold`` shows what cut-off we used (so the banner stays
    truthful if we ever tune the threshold).
    """
    if bracket > bracket_max:
        return None
    if not salt_map:
        return None

    # Preserve the canonical casing from the user's .dck for display
    # — the salt-list is keyed lowercase but the banner reads better
    # as "Smothering Tithe" than "smothering tithe".
    canonical_by_lower: dict[str, str] = {}
    for raw in user_deck_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("["):
            continue
        m = CARD_LINE_RE.match(stripped)
        if m:
            name = m.group(2).strip()
            canonical_by_lower.setdefault(name.lower(), name)

    hits: list[dict] = []
    for name_lower, canonical in canonical_by_lower.items():
        score = salt_map.get(name_lower)
        if score is None:
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            continue
        if score_f >= threshold:
            hits.append({"name": canonical, "salt": round(score_f, 2)})

    if not hits:
        return None

    hits.sort(key=lambda h: (-h["salt"], h["name"]))
    return {
        "bracket": bracket,
        "count": len(hits),
        "threshold": threshold,
        "cards": hits,
    }


# ---------------------------------------------------------------------------
# Cross-deck library search — which decks run a given card
# ---------------------------------------------------------------------------
#
# Backs the unified app's "which of my decks run this card?" lookup
# (FP-007 next slice). Pure file read over the .dck set in a directory.


def decks_containing_card(deck_dir: Path, card_name: str) -> list[str]:
    """Return the SORTED deck IDs whose [Commander] or [Main] section
    runs ``card_name``.

    Each ``.dck`` file in ``deck_dir`` is scanned. A deck matches when
    its ``[Commander]`` or ``[Main]`` section contains a line for
    ``card_name``, matched case-insensitively and ignoring the leading
    quantity and any ``|SET|CN`` edition tail (so ``1 Sol Ring|CLB|871``
    matches ``"sol ring"``). The deck ID returned is the filename stem
    (e.g. ``"Alpha [B3]"`` for ``Alpha [B3].dck``). Empty list when no
    deck runs the card.

    Only ``[Commander]`` and ``[Main]`` count — sideboard / considering
    / metadata sections are ignored, mirroring the card-section scope
    used elsewhere in this module.
    """
    target = card_name.strip().lower()
    matches: list[str] = []
    for path in deck_dir.glob("*.dck"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        in_card_section = False
        found = False
        for raw in text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("[") and s.endswith("]"):
                in_card_section = s.lower() in ("[commander]", "[main]")
                continue
            if not in_card_section:
                continue
            m = CARD_LINE_RE.match(s)
            if not m:
                continue
            if m.group(2).strip().lower() == target:
                found = True
                break
        if found:
            matches.append(path.stem)
    return sorted(matches)
