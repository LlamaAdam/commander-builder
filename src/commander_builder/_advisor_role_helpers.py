"""Shared role-classification helpers for the advisor's per-source modules.

Thin wrappers around ``scryfall_client.lookup_card`` +
``staples.classify_role`` that the heuristic, bracket-peers, and
manabase recommenders all consume. Extracted to its own module so
those recommender modules can import from here without circular
references through the orchestrator.

External callers should keep using
``commander_builder.improvement_advisor`` re-exports — this module
is an internal layout detail.
"""

from __future__ import annotations

from .staples import classify_role


def _role_for_card(card_name: str) -> str:
    """Look up ``card_name`` via Scryfall (cached) and classify its role.

    Returns ``"unknown"`` on Scryfall miss or offline. The role tag
    is advisory — it groups recommendations on the advice surface
    but doesn't drive program logic, so a soft failure is fine.

    ``lookup_card`` is imported lazily from the orchestrator
    (``improvement_advisor``) so test monkeypatches at the
    orchestrator path still intercept calls made from this module.
    Otherwise tests that patch
    ``commander_builder.improvement_advisor.lookup_card`` wouldn't
    affect lookups happening inside this helper.
    """
    from .improvement_advisor import lookup_card
    try:
        card = lookup_card(card_name)
    except Exception:
        return "unknown"
    if not card:
        return "unknown"
    return classify_role(card.get("oracle_text", ""), card.get("type_line", ""))
