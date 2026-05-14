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
from .edhrec_client import AverageDeck, CardEntry, CommanderPage
from .staples import is_land, is_universal_staple


# How many candidate adds + cuts to recommend. Bumped from 8 → 12
# on 2026-05-14 when the heuristic gained two additional add
# buckets (new_cards from EDHREC's "New Cards" section + average-
# deck cards from EDHREC's bracket-specific sample build). With
# 4 buckets feeding candidates_for_add the old 8-card cap was
# trimming most of the average-deck pool — and average_deck is
# the highest-confidence signal we have for "cards a tuned deck
# of this commander runs."
_DEFAULT_ADD_LIMIT = 12
_DEFAULT_CUT_LIMIT = 8

# Inclusion% threshold below which a card is unlikely to be a top add.
MIN_INCLUSION_PCT_FOR_ADD = 30.0

# Synergy% threshold for the "high synergy" buckets — these get
# prioritized even if their inclusion is moderate.
MIN_SYNERGY_PCT = 25.0

# Minimum combined size of EDHREC's recognized cards below which
# we don't emit cut recommendations. Originally introduced
# 2026-05-14 when the parser only captured 25 cards/commander
# (top + high_synergy + new); follow-up investigation revealed
# EDHREC actually ships 200+ cards per commander across 14
# sections, and the parser was discarding 90%. The expanded
# parser (commit f8e9b7f) now feeds the full ~200-card pool
# into ``edhrec_page.all_known_cards()``, so this threshold
# returns to its original safety-net role: catch genuinely-
# broken pages (commander too obscure for EDHREC to have data),
# not papering over a parser bug.
#
# 50 is a conservative floor — the typical commander has 200+
# cards, so anything below 50 indicates a fundamentally degraded
# page response and we shouldn't emit cuts.
MIN_EDHREC_SIGNAL_FOR_CUTS = 50


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
    average_deck: Optional[AverageDeck] = None,
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

    ``average_deck`` (optional) is EDHREC's bracket-specific sample
    build for this commander — a coherent 73-98 card reference
    deck. When provided:
      - Cards in the average deck join ``edhrec_known`` for the
        cut decision (high-confidence "this DOES belong here"
        signal — EDHREC built a deck containing it).
      - Cards in the average deck but NOT in the user's deck
        surface as a new add bucket (``source: edhrec.average_deck``).
    """
    if edhrec_page is None:
        return []
    recs: list[SwapRecommendation] = []
    deck_cards_lc = {c.lower() for c in deck_cards}

    # Adds — pull from high-synergy first (commander-specific signal),
    # then top cards (color staples), then new cards (recent
    # printings that EDHREC's "New Cards" section surfaces for this
    # commander). The 2026-05-14 audit revealed ``new_cards`` was
    # parsed but never used in recommendations — surfacing it
    # closes the gap for recently-printed archetype additions.
    #
    # Track which candidate came from which bucket so we can label
    # the rationale string honestly ("new printing" vs "high
    # synergy") and so the audit log can disambiguate sources.
    candidates_for_add: list[CardEntry] = []
    candidate_bucket: dict[str, str] = {}  # card_name_lc → bucket label
    seen: set[str] = set()
    for c in edhrec_page.high_synergy_cards:
        if c.synergy_pct >= MIN_SYNERGY_PCT and c.name.lower() not in seen:
            candidates_for_add.append(c)
            candidate_bucket[c.name.lower()] = "high_synergy"
            seen.add(c.name.lower())
    for c in edhrec_page.top_cards:
        if c.inclusion_pct >= MIN_INCLUSION_PCT_FOR_ADD and c.name.lower() not in seen:
            candidates_for_add.append(c)
            candidate_bucket[c.name.lower()] = "top_cards"
            seen.add(c.name.lower())
    # Average-deck candidates BEFORE new_cards: average_deck is a
    # coherent sample build for this commander at this bracket —
    # every card was deliberately included, the strongest possible
    # "tuned decks run this" signal. New cards are interesting
    # but lower-confidence (early-adopter inclusion data).
    if average_deck:
        for c in average_deck.cards:
            if c.name.lower() not in seen:
                candidates_for_add.append(c)
                candidate_bucket[c.name.lower()] = "average_deck"
                seen.add(c.name.lower())
    # New-card candidates: EDHREC's "New Cards" section ships the
    # 5 most-recently-printed cards that have already accumulated
    # inclusion data for this commander. They're often interesting
    # adds because the meta hasn't fully absorbed them — early-
    # adopter signal. No inclusion floor (these are new, the
    # numbers haven't matured); just dedupe against earlier
    # buckets.
    for c in edhrec_page.new_cards:
        if c.name.lower() not in seen:
            candidates_for_add.append(c)
            candidate_bucket[c.name.lower()] = "new_cards"
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
        # Pull the bucket we recorded when this candidate was
        # added — preserves the new_cards distinction (which the
        # heuristic-after-the-fact ``high_synergy if synergy_pct``
        # check would have mislabeled as top_cards for any
        # zero-synergy new card).
        bucket = candidate_bucket.get(c.name.lower(), "top_cards")
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
        # New-card rationale gives the user a reason to consider
        # an unfamiliar pick: "recently printed for this commander."
        # EDHREC ranks new_cards by early-inclusion signal, so the
        # cards here aren't random — they're cards the meta is
        # actively trying.
        if bucket == "new_cards":
            reason_text = (
                f"EDHREC new printing: in {inclusion_phrase}"
                + (f", synergy {c.synergy_pct:.0f}%" if c.synergy_pct else "")
                + " — recently added to the meta for this commander"
            )
        elif bucket == "average_deck":
            # Average-deck cards don't carry inclusion%/synergy
            # numbers (the endpoint returns just names), so the
            # rationale leans on the bracket context instead.
            bracket_label = (
                (average_deck.bracket_slug or "average").replace("-", " ")
                if average_deck else "average"
            )
            reason_text = (
                f"in EDHREC's {bracket_label} sample deck for this "
                f"commander"
            )
        else:
            reason_text = (
                f"EDHREC {bucket}: in {inclusion_phrase}"
                + (f", synergy {c.synergy_pct:.0f}%" if c.synergy_pct else "")
            )
        add_recs.append(SwapRecommendation(
            card=c.name,
            action="add",
            reason=reason_text,
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

    # Cuts — cards in deck not present anywhere on EDHREC's page
    # for this commander. Uses ``all_known_cards()`` which folds
    # together every section (top_cards + high_synergy + new_cards
    # + per-category lists Creatures/Instants/Sorceries/Lands/Mana
    # Artifacts/Game Changers/...). Typical commander has 200+
    # cards in this pool — comparable to the deck size, so
    # absence is a real signal rather than the 20-card spotlight
    # the original parser produced.
    #
    # Average-deck cards (when available) join the known set as
    # the strongest possible signal: EDHREC built a coherent
    # sample build containing them. A card in the average deck
    # is by construction archetype-appropriate; never recommend
    # cutting one even if it doesn't appear in the page's flat
    # category lists.
    edhrec_known = edhrec_page.all_known_cards()
    if average_deck:
        for c in average_deck.cards:
            edhrec_known.add(c.name.lower())

    # Safety net: if EDHREC's page is genuinely degraded (very
    # obscure commander, schema regression), still refuse to emit
    # cuts rather than recommend cutting on weak signal.
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
