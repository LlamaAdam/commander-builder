"""Post-recommendation filters for the deck improvement advisor.

Two filters that run AFTER a source (heuristic / bracket_peers /
claude) produces its recommendations:

- ``_filter_for_saturation``: drops add candidates whose role bucket
  is already saturated in the user's deck. Real failure mode (Ur-
  Dragon B4 audit, 2026-05-13): the heuristic and bracket-peers
  sources both rank "what other decks have a lot of" without
  checking what the user's deck already has. A deck running 13
  ramp pieces doesn't need a 14th suggested.

- ``_validate_card_names``: cross-checks each rec's card name
  against the Scryfall cache. Catches Claude hallucinations
  (plausible-sounding fake cards) before the audit pipeline passes
  them to Forge, which would silently reject the deck.

Extracted from ``improvement_advisor.py`` as part of the per-source
module split. External code keeps importing from
``commander_builder.improvement_advisor``.
"""

from __future__ import annotations

from ._advisor_models import SwapRecommendation
from .staples import ROLE_SATURATION_THRESHOLDS, is_role_saturated


def _filter_for_saturation(
    recs: list[SwapRecommendation],
    role_counts: dict,
) -> tuple[list[SwapRecommendation], list[dict]]:
    """Drop add candidates whose role bucket is already saturated in
    the user's deck.

    Real failure mode this addresses (Ur-Dragon B4 audit,
    2026-05-13): the EDHREC heuristic and bracket-peers source both
    rank "what other decks have a lot of" without checking what the
    user's deck already has. A deck running 13 ramp pieces doesn't
    need a 14th suggested; recommending one would either get
    applied (replacing a stronger non-ramp card) or get balanced
    out by ``_apply_swaps_to_dck``'s adds==cuts rule, wasting a
    slot.

    Returns ``(kept_recs, skipped_records)``. Each skipped record:
    ``{card, role, deck_count, threshold}``. Cuts are never
    filtered (they're already in the deck — removing a 13th ramp
    piece IS the user's decision). Recs without ``evidence.role``
    bucket as ``"other"`` which never saturates, so legacy stubs
    pass through untouched.
    """
    kept: list[SwapRecommendation] = []
    skipped: list[dict] = []
    for rec in recs:
        if rec.action != "add":
            kept.append(rec)
            continue
        role = (rec.evidence or {}).get("role", "other") or "other"
        deck_count = int(role_counts.get(role, 0))
        if is_role_saturated(role, deck_count):
            threshold = ROLE_SATURATION_THRESHOLDS.get(role, 0)
            skipped.append({
                "card": rec.card,
                "role": role,
                "deck_count": deck_count,
                "threshold": threshold,
            })
            continue
        kept.append(rec)
    return kept, skipped


def _validate_card_names(recs: list[SwapRecommendation]) -> None:
    """Mutate each rec's ``name_known`` flag based on Scryfall lookup.

    Defense against Claude analyst hallucinations: when the LLM
    invents a plausible-sounding card name (e.g. "Accursed
    Marauder"), the audit pipeline would otherwise pass it down to
    Forge, which then rejects the deck silently. Cross-checking
    against the Scryfall cache catches it early so the UI can mark
    the recommendation with a warning pill.

    Three terminal states for each rec:

    - ``True``  — Scryfall returned a card dict; the name is real.
    - ``False`` — Scryfall returned ``None`` (HTTP 404); the name
      is fake.
    - ``None``  — lookup raised (network, cache corruption); we
      couldn't check. **Never** flag a legitimate card as fake on
      transient failure.

    Heuristic recs come from EDHREC and should always resolve;
    running them through the validator is cheap (cache hit) and
    uniform so callers don't need to special-case the source.

    ``lookup_card`` is imported lazily from the orchestrator so
    test monkeypatches at
    ``commander_builder.improvement_advisor.lookup_card`` still
    intercept calls made from this module.
    """
    from .improvement_advisor import lookup_card
    for rec in recs:
        try:
            card = lookup_card(rec.card)
        except Exception:
            rec.name_known = None
            continue
        rec.name_known = card is not None
