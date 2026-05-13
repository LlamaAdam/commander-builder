"""Manabase-essentials safety net for the improvement advisor.

Curated recommender that surfaces any color-identity-appropriate
ABU dual / fetch / shock / bond land the deck doesn't already own,
plus tribal-utility lands (Cavern of Souls etc.) when the commander
is tribal. Runs alongside the source-specific recommenders
(heuristic / bracket_peers / claude) so manabase upgrades always
get surfaced, regardless of which references the source happens to
include.

Extracted from ``improvement_advisor.py`` as part of the per-source
module split (chunk 2/N). External code should keep importing from
``improvement_advisor`` — this module is an internal layout detail.
"""

from __future__ import annotations

from typing import Optional

from ._advisor_models import SwapRecommendation
from .staples import (
    essential_manabase_for_colors,
    tribal_essential_lands,
)


def _missing_manabase_recommendations(
    deck_cards,
    color_identity,
    tribe: Optional[str] = None,
    budget: bool = False,
) -> list[SwapRecommendation]:
    """Curated manabase-essentials safety net.

    User feedback (2026-05-13): "tribal decks should have cavern of
    souls. All decks should have dual lands and bond lands and fetch
    lands." The heuristic + bracket_peers paths only surface lands
    when they happen to appear in references/EDHREC. This helper
    runs alongside those paths to deterministically recommend any
    color-identity-appropriate ABU dual / fetch / shock / bond land
    that the deck doesn't already own.

    ``deck_cards`` is the set of card names currently in the deck.
    ``color_identity`` is a set/iterable of WUBRG letters (case-
    insensitive). Empty/colorless identity → no color-gated lands
    (but tribal essentials still surface if ``tribe`` is set).

    ``tribe`` (optional) is the deck's primary creature type as
    detected from the commander's oracle text. When set, appends
    Cavern of Souls + Path of Ancestry + Secluded Courtyard +
    Unclaimed Territory — colorless lands every tribal deck wants
    regardless of color identity.

    ``budget=True`` strips the $200+ ABU duals and $25-60 fetch
    lands from the color-gated tier — shocks, bond lands, utility
    fixers, and tribal lands stay.

    Each rec carries ``evidence.role="land"`` so it groups cleanly
    in the UI. Source identifies which arm produced it:
    ``manabase_essentials`` for color-gated, ``tribal_essentials``
    for the tribe-utility set.
    """
    essentials = essential_manabase_for_colors(color_identity, budget=budget)
    tribal = tribal_essential_lands(tribe)
    if not essentials and not tribal:
        return []
    deck_lc = {c.lower() for c in deck_cards}
    recs: list[SwapRecommendation] = []
    for name in essentials:
        if name.lower() in deck_lc:
            continue
        recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                "manabase essential — high-impact land for this "
                "color identity (dual / fetch / shock / bond)"
            ),
            evidence={
                "source": "manabase_essentials",
                "role": "land",
            },
        ))
    for name in tribal:
        if name.lower() in deck_lc:
            continue
        recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                f"tribal essential — colorless utility land for "
                f"{tribe} decks (uncounterable / mana of any color)"
            ),
            evidence={
                "source": "tribal_essentials",
                "role": "land",
                "tribe": tribe,
            },
        ))
    return recs
