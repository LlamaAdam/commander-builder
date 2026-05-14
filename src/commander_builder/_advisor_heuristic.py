"""EDHREC-heuristic recommender for the deck improvement advisor.

The default (no-LLM, no Moxfield-peer) recommender. Pulls
high-synergy + top-cards lists from a fetched EDHREC commander
page, filters universal staples, applies role-tag re-ranking from
the deck's diagnosis (if present), and emits SwapRecommendations
for adds + cuts.

Extracted from ``improvement_advisor.py`` as part of the per-source
module split. External code keeps importing from
``commander_builder.improvement_advisor``.
"""

from __future__ import annotations

from typing import Optional

from ._advisor_models import DeckDiagnosis, SwapRecommendation
from ._advisor_role_helpers import _role_for_card
from .edhrec_client import CardEntry, CommanderPage
from .staples import is_land, is_universal_staple


# How many candidate adds + cuts to recommend. Roughly matches the audit
# prompt's expected swap-list size for a single-iteration pass. Kept in
# sync with the orchestrator's DEFAULT_ADD_LIMIT / DEFAULT_CUT_LIMIT.
_DEFAULT_ADD_LIMIT = 8
_DEFAULT_CUT_LIMIT = 8

# Inclusion% threshold below which a card is unlikely to be a top add.
MIN_INCLUSION_PCT_FOR_ADD = 30.0

# Synergy% threshold for the "high synergy" buckets — these get
# prioritized even if their inclusion is moderate.
MIN_SYNERGY_PCT = 25.0

# Minimum combined size of EDHREC top_cards + high_synergy_cards
# below which we don't emit cut recommendations. Live two-deck
# comparison (2026-05-14) revealed EDHREC's page-scrape returns
# only 10+10=20 cards per commander, which means ~80% of a 99-card
# deck would be flagged as "not in EDHREC top lists" → wrong cuts
# of obvious staples (Muxus from Krenko, Path to Exile from
# any deck, etc.).
#
# 30 is a deliberate floor: if EDHREC's next-data blob ever
# expands to a reasonable list size (50+), cuts re-engage. If it
# stays at 20 we emit zero cuts from the heuristic source —
# better than wrong cuts. Users who want richer cuts should use
# ``source=bracket_peers`` which sources from 5 tuned same-bracket
# decks and reliably returns much more signal.
MIN_EDHREC_SIGNAL_FOR_CUTS = 30


# Map diagnosis weakness keywords to the role buckets that address them.
# Order in each tuple matters — leftmost role is the strongest match
# for that weakness, used to break ties in priority ranking.
#
# Both ``finisher`` and ``win_condition`` are listed together for
# closer-related diagnoses because they're synonyms from the user's
# perspective ("this deck can't close games"). Before the
# 2026-05-13 role-classifier consolidation only the base
# ``finisher`` taxonomy was in play; after consolidation cards
# like Coalition Victory / Insurrection / Triumph of the Hordes
# now tag as ``win_condition`` and would have been ignored by the
# rerank if we listed only ``finisher`` here.
_SIGNAL_TO_ROLES: list[tuple[str, tuple[str, ...]]] = [
    # "no closer / finisher" → bring in finishers/wincons, then wipes
    ("closer", ("finisher", "win_condition", "wipe")),
    ("finisher", ("finisher", "win_condition", "wipe")),
    # "low win rate" → assume offense problem; finishers + draw to dig
    ("low win rate", ("finisher", "win_condition", "draw", "tutor")),
    # "offense, not defense" → finisher + draw (survives, just doesn't close)
    ("offense, not defense", ("finisher", "win_condition", "tutor", "draw")),
    # "defense / sustain is weak" → wipe (clear board) + protection
    ("defense", ("wipe", "protection", "removal")),
    # "early aggression / no T1-T3 interaction" → cheap removal + ramp + protection
    ("early aggression", ("removal", "ramp", "protection")),
    ("T1-T3", ("removal", "ramp", "protection")),
    # "high draw rate" (signal text) → finisher / closer
    ("high draw rate", ("finisher", "win_condition", "wipe", "tutor")),
]


def _signals_to_priority_roles(signals: list[str]) -> list[str]:
    """Translate weakness-signal phrases into a deduplicated,
    priority-ordered role list. The earliest match in each signal
    contributes the strongest role, with later signals adding
    progressively lower-priority roles.

    Returns at most 4 unique roles. Empty signals → empty list (no
    re-ranking, fall back to default ordering).
    """
    out: list[str] = []
    for signal in signals:
        lc = signal.lower()
        for keyword, roles in _SIGNAL_TO_ROLES:
            if keyword in lc:
                for r in roles:
                    if r not in out:
                        out.append(r)
                break
    return out[:4]


def _heuristic_swap_recommendations(
    deck_cards: set[str],
    edhrec_page: CommanderPage,
    add_limit: int = _DEFAULT_ADD_LIMIT,
    cut_limit: int = _DEFAULT_CUT_LIMIT,
    diagnosis: Optional[DeckDiagnosis] = None,
) -> list[SwapRecommendation]:
    """Pure-data swap proposals from EDHREC inclusion-% deltas.

    Adds: cards EDHREC ranks high (top_cards or high_synergy) that
    are NOT already in the deck. Cuts: cards in the deck that
    AREN'T in EDHREC's top-cards list (likely off-archetype). No
    LLM, no card-text reasoning — just statistical co-inclusion.

    If ``edhrec_page`` is ``None`` (commander missing from EDHREC,
    network blip, slug mismatch), returns an empty list rather than
    crashing the audit. The caller still produces a valid
    AdviceReport with zero swaps, which the UI surfaces as "no
    audit suggestions available."
    """
    if edhrec_page is None:
        return []
    recs: list[SwapRecommendation] = []
    deck_cards_lc = {c.lower() for c in deck_cards}

    # Adds — pull from high-synergy first (commander-specific signal),
    # then top cards (color staples).
    candidates_for_add: list[CardEntry] = []
    seen: set[str] = set()
    for c in edhrec_page.high_synergy_cards:
        if c.synergy_pct >= MIN_SYNERGY_PCT and c.name.lower() not in seen:
            candidates_for_add.append(c)
            seen.add(c.name.lower())
    for c in edhrec_page.top_cards:
        if c.inclusion_pct >= MIN_INCLUSION_PCT_FOR_ADD and c.name.lower() not in seen:
            candidates_for_add.append(c)
            seen.add(c.name.lower())

    # Build the full add-recommendation list first, then re-rank.
    add_recs: list[SwapRecommendation] = []
    for c in candidates_for_add:
        if c.name.lower() in deck_cards_lc:
            continue
        # Skip universal staples — they're noise in the must-add
        # list. Every deck already has Sol Ring; if it doesn't,
        # that's an intentional choice.
        if is_universal_staple(c.name):
            continue
        bucket = "high_synergy" if c.synergy_pct >= MIN_SYNERGY_PCT else "top_cards"
        # Categorize the recommendation by role so the advice
        # surface can group adds by ramp/draw/removal/finisher
        # rather than show a flat list.
        role = _role_for_card(c.name)
        # `inclusion_pct` from EDHREC is actually a raw deck count
        # (e.g. 30627 — "this card appears in 30627 decks"), not a
        # percentage. Render it as a count so the rationale doesn't
        # read "in 30627% of decks". If the value is small (≤100)
        # we treat it as a real percentage; otherwise format as a
        # deck count.
        inclusion_phrase = (
            f"{c.inclusion_pct:.0f}% of decks"
            if 0 < c.inclusion_pct <= 100
            else f"{int(c.inclusion_pct):,} decks"
        )
        add_recs.append(SwapRecommendation(
            card=c.name,
            action="add",
            reason=(
                f"EDHREC {bucket}: in {inclusion_phrase}"
                + (f", synergy {c.synergy_pct:.0f}%" if c.synergy_pct else "")
            ),
            evidence={
                "inclusion_pct": c.inclusion_pct,
                "synergy_pct": c.synergy_pct,
                "source": f"edhrec.{bucket}",
                "role": role,
            },
        ))

    # Re-rank by diagnosis priority roles, when present. Adds in the
    # priority-role list float to the top in their listed order;
    # everything else keeps its original (synergy-then-top) ordering.
    # Stable sort preserves intra-bucket order.
    if diagnosis and diagnosis.priority_roles:
        priority_index = {r: i for i, r in enumerate(diagnosis.priority_roles)}
        def _rank(r: SwapRecommendation) -> int:
            role = r.evidence.get("role", "unknown")
            return priority_index.get(role, len(priority_index) + 1)
        add_recs.sort(key=_rank)

    # Apply the add_limit after re-ranking so the surfaced top-N
    # reflects the re-ordered list, not the pre-ranked one.
    recs.extend(add_recs[:add_limit])

    # Cuts — cards in deck not in EDHREC's top-cards or high-synergy
    # lists. Inverse of the adds path: if the rest of the meta isn't
    # running this, it's probably off-archetype. Conservative — top
    # cards by EDHREC are color staples that not all decks need.
    edhrec_known = {c.name.lower() for c in edhrec_page.top_cards} \
                 | {c.name.lower() for c in edhrec_page.high_synergy_cards}

    # Sparse-data guard. When EDHREC's next-data blob returns only
    # 10+10=20 cards (the current 2026-05-14 baseline), the
    # "not in edhrec_known" predicate flags ~80% of a 99-card deck
    # — including obvious staples like Muxus, Path to Exile, etc.
    # Live two-deck comparison surfaced this: both Krenko's Muxus
    # and the Sliver deck's Path to Exile were recommended for
    # cutting. Better to emit ZERO cuts than wrong cuts; the user
    # can switch to source=bracket_peers for high-signal cuts.
    if len(edhrec_known) < MIN_EDHREC_SIGNAL_FOR_CUTS:
        return recs

    for card in deck_cards:
        # Don't recommend cutting any land (basic, dual, fetch,
        # shock, MDFC, utility) or universal staples. The manabase
        # is a deliberate construction; a missing reference doesn't
        # mean the user should pull a $200 ABU dual.
        if is_land(card) or is_universal_staple(card):
            continue
        if card.lower() not in edhrec_known:
            recs.append(SwapRecommendation(
                card=card,
                action="cut",
                reason="not in EDHREC's top-cards or high-synergy lists for this commander",
                evidence={"source": "edhrec.absence"},
            ))
            if sum(1 for r in recs if r.action == "cut") >= cut_limit:
                break

    return recs
