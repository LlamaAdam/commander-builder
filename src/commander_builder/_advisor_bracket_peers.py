"""Bracket-peers recommender + peer-summary helper for the advisor.

The "bracket-peers" source pulls the top-N highest-liked Moxfield
decks for the user's commander at the user's bracket and emits
add/cut recommendations from the frequency analysis. Same fetcher
powers ``_collect_bracket_peer_summary_for_prompt`` which compacts
the data for inclusion in Claude's prompt.

Both functions share ``_peer_card_frequency`` (each deck contributes
at most once per card; basics don't inflate the signal) and
``_extract_main_cards_from_moxfield_json`` (Moxfield JSON shape →
list of card names).

Extracted from ``improvement_advisor.py`` as part of the per-source
module split. External code keeps importing from
``commander_builder.improvement_advisor`` — the orchestrator
re-exports.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Optional

from ._advisor_models import DeckDiagnosis, SwapRecommendation
from ._advisor_role_helpers import _role_for_card
from .staples import (
    is_basic_land,
    is_land,
    is_universal_staple,
    render_frequency_label,
)

# ``find_top_liked_decks_for_commander`` is imported lazily inside the
# two functions below (via ``from .improvement_advisor import ...``)
# so test monkeypatches of
# ``commander_builder.improvement_advisor.find_top_liked_decks_for_commander``
# still intercept the call. The lazy form avoids the circular
# import that would happen if we tried ``from .improvement_advisor
# import find_top_liked_decks_for_commander`` at module top
# (the orchestrator imports from THIS module during its own load).


# How many reference decks to pull. Five is the sweet spot: enough
# that frequency math has signal ("in 5/5 references" reads as
# 'unanimous'), not so many that one cluster of similar builds
# dominates. Configurable via the public function signature.
DEFAULT_BRACKET_PEERS_N = 5

# Match the orchestrator's add/cut caps so bracket_peers recommends
# the same surface-size as heuristic / claude.
_DEFAULT_ADD_LIMIT = 8
_DEFAULT_CUT_LIMIT = 8


def _extract_main_cards_from_moxfield_json(deck_json: dict) -> list[str]:
    """Pull the mainboard card names out of a Moxfield deck JSON.

    Moxfield's response shape is ``boards.mainboard.cards`` keyed by
    internal card UUIDs, each value an object whose ``card.name``
    field is the canonical card name. We don't care about quantity
    here — each name counts once (multi-copies aren't a thing in
    singleton Commander anyway, except for basics, which the
    staples filter drops).
    """
    boards = deck_json.get("boards") or {}
    mainboard = (boards.get("mainboard") or {}).get("cards") or {}
    out: list[str] = []
    for entry in mainboard.values():
        if not isinstance(entry, dict):
            continue
        card = entry.get("card") or {}
        name = (card.get("name") or "").strip()
        if name:
            out.append(name)
    return out


def _peer_card_frequency(decks):
    """Build per-card reference-frequency across a list of Moxfield
    deck JSONs.

    Returns ``(frequency_counter, case_map)`` where:
      - ``frequency_counter`` maps lowercased card name → number of
        references that contain it. **Each deck contributes at most
        once per card** (so basic lands present 30× in a deck don't
        inflate the count — what we want is the "shows up in N of
        the M references" signal).
      - ``case_map`` maps lowercased name → display-cased name
        (first-seen wins, mirrors how source decks render the card).
    """
    freq: Counter = Counter()
    case_map: dict[str, str] = {}
    for deck in decks:
        cards = _extract_main_cards_from_moxfield_json(deck)
        seen_this_deck: set[str] = set()
        for c in cards:
            lc = c.lower()
            if lc not in case_map:
                case_map[lc] = c
            if lc in seen_this_deck:
                continue
            seen_this_deck.add(lc)
            freq[lc] += 1
    return freq, case_map


def _default_min_refs(total_refs: int) -> int:
    """Default frequency floor for bracket-peers adds.

    Singletons (cards in 1 of N references) are noise — they reflect
    one builder's idiosyncrasy, not a tuned-archetype consensus.
    Default to "at least majority of references, but never less
    than 2" so a small reference set (N=2 or 3) still produces some
    recommendations.
    """
    return max(2, math.ceil(total_refs / 2))


def _bracket_peers_recommendations(
    commander_name: str,
    bracket: int,
    deck_cards: set[str],
    n: int = DEFAULT_BRACKET_PEERS_N,
    add_limit: int = _DEFAULT_ADD_LIMIT,
    cut_limit: int = _DEFAULT_CUT_LIMIT,
    min_refs: Optional[int] = None,
    diagnosis: Optional[DeckDiagnosis] = None,
) -> tuple[list[SwapRecommendation], int]:
    """Source swap recommendations from the top-N highest-liked Moxfield
    decks for ``commander_name`` at ``bracket``.

    The Ur-Dragon B4 audit (2026-05-13) surfaced why this exists:
    EDHREC's commander page averages inclusion% across all brackets
    and includes precons, so it recommended generic ramp for a deck
    that was already swimming in ramp and cut archetype-specific
    tools (Moat in a flying-tribal deck, Last March of the Ents as
    the deck's card draw). Sourcing from other tuned builds at the
    same bracket produces archetype-appropriate suggestions by
    construction — what 5 other people who've tuned this commander
    at this bracket consider essential.

    Returns ``(recommendations, ref_count)``. Empty list + ``0``
    when no references could be fetched (caller falls back to a
    sparser source).

    Frequency thresholds:
      - **Adds** include cards present in any reference but missing
        from the user's deck. Each rec carries ``in_n_references``
        so callers rank by confidence; the reason string already
        names the ratio.
      - **Cuts** are user-deck cards absent from every reference.
        Universal staples and basic lands are excluded from both
        directions (they're noise in either).
    """
    from .improvement_advisor import find_top_liked_decks_for_commander
    decks = find_top_liked_decks_for_commander(
        commander_name, bracket=bracket, n=n,
    )
    if not decks:
        return [], 0

    # Shared frequency helper — counts each deck at most once per
    # card, so basics don't inflate the signal. case_map preserves
    # first-seen capitalization.
    freq, case_map = _peer_card_frequency(decks)
    total_refs = len(decks)
    deck_cards_lc = {c.lower() for c in deck_cards}
    # Extend case_map with the user's own cards so cuts can render
    # the user's casing (the peer-cardlists may not include cards
    # that are in the user's deck and absent from every reference).
    for c in deck_cards:
        lc = c.lower()
        if lc not in case_map:
            case_map[lc] = c

    # Drop singletons (cards in only 1 of N references) — they're
    # one builder's quirk, not a tuned-archetype consensus. Default
    # floor is majority-of-references (never below 2). Phase A gap #3.
    effective_min_refs = (
        min_refs if min_refs is not None else _default_min_refs(total_refs)
    )

    # Adds: any card appearing in ≥ effective_min_refs references,
    # missing from user, not a universal staple. Sort by frequency
    # desc, then alphabetical.
    add_candidates_lc = [
        lc for lc in freq
        if lc not in deck_cards_lc
        and freq[lc] >= effective_min_refs
        and not is_universal_staple(case_map[lc])
        and not is_basic_land(case_map[lc])
    ]
    add_candidates_lc.sort(
        key=lambda lc: (-freq[lc], case_map[lc].lower()),
    )

    add_recs: list[SwapRecommendation] = []
    for lc in add_candidates_lc:
        name = case_map[lc]
        n_refs = freq[lc]
        role = _role_for_card(name)
        label = render_frequency_label(n_refs, total_refs)
        add_recs.append(SwapRecommendation(
            card=name,
            action="add",
            reason=(
                f"in {n_refs}/{total_refs} reference decks "
                f"({label}) for {commander_name} at B{bracket}"
            ),
            evidence={
                "in_n_references": n_refs,
                "total_references": total_refs,
                "frequency_label": label,
                "role": role,
                "source": "bracket_peers",
            },
        ))

    # Phase A gap #5: re-rank by diagnosis priority roles when
    # available, matching the heuristic path's behavior. If the
    # deck's weakness signals say "no closer / high draw rate",
    # finisher-tagged adds float to the top regardless of which
    # reference they came from. Stable sort preserves
    # frequency-desc within each priority bucket.
    if diagnosis and getattr(diagnosis, "priority_roles", None):
        priority_index = {r: i for i, r in enumerate(diagnosis.priority_roles)}
        def _rank(rec: SwapRecommendation) -> int:
            role_str = (rec.evidence or {}).get("role", "unknown")
            return priority_index.get(role_str, len(priority_index) + 1)
        add_recs.sort(key=_rank)

    recs: list[SwapRecommendation] = list(add_recs[:add_limit])

    # Cuts: user cards absent from every reference, with the
    # universal-staples filter applied so we don't recommend cutting
    # Sol Ring.
    any_ref_lc = set(freq.keys())
    cut_candidates = [
        case_map[lc] for lc in (deck_cards_lc - any_ref_lc)
        if lc in case_map
        and not is_universal_staple(case_map[lc])
        # Skip ALL lands (basic + nonbasic + fetch + shock + MDFC).
        # Regression caught 2026-05-13: bracket_peers recommended
        # cutting Savannah from a 5-color Ur-Dragon deck because
        # the top-5 references happened to use different specific
        # duals. Manabase decisions are deliberate, not
        # auto-recommended.
        and not is_land(case_map[lc])
    ]
    cut_candidates.sort(key=str.lower)
    for name in cut_candidates[:cut_limit]:
        recs.append(SwapRecommendation(
            card=name,
            action="cut",
            reason=(
                f"absent from all {total_refs} reference decks for "
                f"{commander_name} at B{bracket}"
            ),
            evidence={
                "in_n_references": 0,
                "total_references": total_refs,
                "source": "bracket_peers",
            },
        ))

    return recs, total_refs


def _collect_bracket_peer_summary_for_prompt(
    commander_name: str,
    bracket: int,
    n: int = DEFAULT_BRACKET_PEERS_N,
) -> Optional[dict]:
    """Pull top-N bracket-matched references and produce a compact
    frequency summary suitable for inclusion in the Claude prompt.

    Sourced from ``find_top_liked_decks_for_commander`` (same fetcher
    powering the standalone bracket_peers source). The summary
    shape is designed for LLM consumption — minimal metadata + a
    sorted frequency table — so Claude can reason about "this card
    is in 5/5 references but missing from this deck" at a glance,
    without parsing full decklists.

    Returns ``None`` when no references could be fetched (caller's
    Claude prompt falls back to EDHREC-only context).
    """
    from .improvement_advisor import find_top_liked_decks_for_commander
    decks = find_top_liked_decks_for_commander(
        commander_name, bracket=bracket, n=n,
    )
    if not decks:
        return None

    # Shared frequency helper — same semantics as the standalone
    # bracket_peers source. Each deck counts at most once per card,
    # which keeps the in_n_refs count meaningful: "Moat in 5/5
    # references" rather than "Forest in 150 occurrences."
    freq, case_map = _peer_card_frequency(decks)

    # Top-100 most frequent cards is a generous cap — keeps the
    # prompt compact while covering virtually every relevant card
    # across 5 references.
    sorted_lc = sorted(freq, key=lambda lc: (-freq[lc], case_map[lc].lower()))
    cards_by_frequency = [
        {"name": case_map[lc], "in_n_refs": freq[lc]}
        for lc in sorted_lc[:100]
    ]
    ref_metadata = [
        {"public_id": d.get("publicId"), "name": d.get("name", "")}
        for d in decks
    ]
    return {
        "ref_count": len(decks),
        "ref_metadata": ref_metadata,
        "cards_by_frequency": cards_by_frequency,
    }
