"""Decision C2 — the politics guard, across every cut path.

WHY THIS FILE EXISTS. Forge's AI cannot play politics: it doesn't
negotiate, doesn't pick an archenemy, doesn't model an opponent's
incentive to pay a tax. So goad / the monarch / votes / tempting offers /
Rhystic taxes / pillow-fort deterrents all read to the sim as no-ops, and
the A/B loop that measures margin against that AI will "empirically"
recommend cutting exactly the cards that define multiplayer Commander.
The guard shields those cards from margin-driven cuts. It never promotes
them — an ADD recommendation for a politics card is untouched.

Three cut paths exist and each is pinned here:

1. ``_advisor_heuristic``'s EDHREC-absence cut loop (in-loop skip, so the
   shield doesn't consume one of the ``cut_limit`` slots),
2. ``_advisor_filters._filter_for_politics`` (the source-agnostic
   post-advice filter — the Claude and bracket-peers backends propose
   cuts the heuristic loop never sees),
3. ``card_score``'s cut ranking (``_cut_block_reason`` rail, reusing the
   existing refuse-don't-down-rank guard-rail mechanism).

Plus the per-deck opt-out (``[metadata] PoliticsGuard=off``) disabling all
three.

ORACLE-TEXT PROVENANCE: oracle bodies here are SYNTHETIC, written to the
printed rules template rather than copied from one card, and marked as
such. Scryfall is unreachable from the sandbox this landed in, and
``tests/fixtures/real_oracles.py`` (the repo's byte-exact fixture
discipline) carries no politics card yet — the false-positive guards that
DO need real text live in ``test_staples.py`` against existing fixture
cards. See that file's note for the follow-up.
"""
from __future__ import annotations

import pytest

from commander_builder import _advisor_heuristic as ah
from commander_builder import card_score as cs
from commander_builder._advisor_filters import _filter_for_politics
from commander_builder._advisor_models import SwapRecommendation
from commander_builder.edhrec_client import CardEntry, CommanderPage
from commander_builder.staples import POLITICS_SHIELD_REASON


# SYNTHETIC oracle bodies, one per politics mechanic plus non-politics
# controls. ``goad``/``monarch``/``tax``/``deterrent`` shapes only — the
# per-pattern coverage lives in test_staples.py.
_UNIVERSE: dict[str, dict] = {
    "Disrupt Decorum": {
        "type_line": "Sorcery",
        "oracle_text": "Goad all creatures you don't control.",
    },
    "Palace Sentinels": {
        "type_line": "Creature — Human Soldier",
        "oracle_text": "When this creature enters, you become the monarch.",
    },
    "Rhystic Study": {
        "type_line": "Enchantment",
        "oracle_text": (
            "Whenever an opponent casts a spell, you may draw a card "
            "unless that player pays {1}."
        ),
    },
    "Propaganda": {
        "type_line": "Enchantment",
        "oracle_text": (
            "Creatures can't attack you unless their controller pays {2} "
            "for each creature they control that's attacking you."
        ),
    },
    # Controls: no politics mechanic, must stay cuttable.
    "Solitude": {
        "type_line": "Creature — Elemental Incarnation",
        "oracle_text": "When this creature enters, exile up to one target "
                       "creature.",
    },
    "Divination": {
        "type_line": "Sorcery",
        "oracle_text": "Draw two cards.",
    },
}

# Interchangeable draw filler with DISTINCT names (Commander is singleton,
# and ``DeckContext.without`` removes every copy of a repeated name). Their
# only job is to lift the ``draw`` role clear of its target so the
# role-floor rail isn't what blocks the cards under test — the politics
# rail has to be the one firing.
_UNIVERSE.update({
    f"Filler Draw {i}": {"type_line": "Sorcery",
                         "oracle_text": "Draw two cards."}
    for i in range(1, 12)
})

_POLITICS_NAMES = ("Disrupt Decorum", "Palace Sentinels", "Rhystic Study",
                   "Propaganda")


def _lookup(name):
    """Case-insensitive offline ``name -> card`` stub."""
    return _UNIVERSE.get((name or "").strip())


@pytest.fixture
def offline_lookup(monkeypatch):
    """Pin every name-resolution seam the cut paths can reach.

    ``_advisor_heuristic`` reads its own cache-only ``_cached_scryfall``;
    ``staples``' name-keyed wrapper reads ``staples.lookup_card``;
    ``card_score`` takes an injected ``lookup``. All three are separate
    seams, and a test that misses one reaches for the network.
    """
    monkeypatch.setattr(ah, "_cached_scryfall", _lookup)
    monkeypatch.setattr("commander_builder.staples.lookup_card", _lookup)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", _lookup,
    )
    return _lookup


# ---------------------------------------------------------------------------
# Path 1 — _advisor_heuristic's cut loop
# ---------------------------------------------------------------------------

def _page(commander="Marchesa, the Black Rose"):
    """An EDHREC page with enough known cards to release the cut safety
    net (MIN_EDHREC_SIGNAL_FOR_CUTS), and containing NONE of the deck's
    cards — so absence makes every deck card a cut candidate."""
    return CommanderPage(
        commander_name=commander,
        slug="marchesa-the-black-rose",
        fetched_at="2026-08-17T00:00:00Z",
        top_cards=[CardEntry(name=f"Filler Card {i}", inclusion_pct=80,
                             num_decks=1000) for i in range(60)],
    )


@pytest.mark.parametrize("card", _POLITICS_NAMES)
def test_heuristic_cut_loop_shields_politics_cards(offline_lookup, card):
    """EDHREC absence is a POPULARITY signal and the sim that would
    adjudicate the resulting swap is blind to these cards, so the loop
    must not propose the cut at all."""
    recs = ah._heuristic_swap_recommendations(
        deck_cards={card, "Solitude"}, edhrec_page=_page(),
    )
    cut_names = {r.card for r in recs if r.action == "cut"}
    assert card not in cut_names
    # Control: the non-politics card in the same deck IS still cuttable,
    # so the test can't pass by the cut path being dead.
    assert "Solitude" in cut_names


def test_heuristic_cut_loop_shield_does_not_eat_a_cut_limit_slot(
    offline_lookup,
):
    """Shielded in-loop (a ``continue``), not filtered afterwards: with
    ``cut_limit=1`` and a politics card sorting first alphabetically, the
    loop must still return its one real cut."""
    recs = ah._heuristic_swap_recommendations(
        # "Disrupt Decorum" < "Solitude" alphabetically, so it is visited
        # first and a post-hoc filter would leave zero cuts.
        deck_cards={"Disrupt Decorum", "Solitude"},
        edhrec_page=_page(), cut_limit=1,
    )
    cuts = [r.card for r in recs if r.action == "cut"]
    assert cuts == ["Solitude"]


def test_heuristic_cut_loop_opt_out_restores_the_cut(offline_lookup):
    """``PoliticsGuard=off`` in the deck's metadata disables the shield
    for that one deck — no global flag involved."""
    deck_text = (
        "[metadata]\nName=Marchesa\nPoliticsGuard=off\n"
        "[Main]\n1 Disrupt Decorum\n1 Solitude\n"
    )
    recs = ah._heuristic_swap_recommendations(
        deck_cards={"Disrupt Decorum", "Solitude"}, edhrec_page=_page(),
        deck_text=deck_text,
    )
    cut_names = {r.card for r in recs if r.action == "cut"}
    assert "Disrupt Decorum" in cut_names


def test_heuristic_cut_loop_guard_is_on_without_deck_text(offline_lookup):
    """A caller that holds only a name set (``deck_text=None``) gets the
    guard ON — the default must not depend on how much context the caller
    happened to pass."""
    recs = ah._heuristic_swap_recommendations(
        deck_cards={"Rhystic Study"}, edhrec_page=_page(),
    )
    assert [r.card for r in recs if r.action == "cut"] == []


def test_heuristic_adds_are_untouched_by_the_guard(offline_lookup):
    """The guard shields, it never promotes. A politics card EDHREC ranks
    highly is still a normal add candidate with no special treatment."""
    page = CommanderPage(
        commander_name="Marchesa, the Black Rose",
        slug="marchesa-the-black-rose",
        fetched_at="2026-08-17T00:00:00Z",
        top_cards=[CardEntry(name="Rhystic Study", inclusion_pct=70,
                             num_decks=1000)]
        + [CardEntry(name=f"Filler Card {i}", inclusion_pct=80,
                     num_decks=1000) for i in range(60)],
    )
    recs = ah._heuristic_swap_recommendations(
        deck_cards={"Divination"}, edhrec_page=page,
    )
    adds = [r for r in recs if r.action == "add" and r.card == "Rhystic Study"]
    assert adds, "a politics card must remain a normal add candidate"
    assert POLITICS_SHIELD_REASON not in adds[0].reason


# ---------------------------------------------------------------------------
# Path 2 — _advisor_filters._filter_for_politics
# ---------------------------------------------------------------------------

def _recs():
    return [
        SwapRecommendation(card="Rhystic Study", action="cut", reason="x"),
        SwapRecommendation(card="Solitude", action="cut", reason="x"),
        SwapRecommendation(card="Propaganda", action="add", reason="x"),
    ]


def test_filter_drops_politics_cuts_and_keeps_the_rest():
    kept, skipped = _filter_for_politics(_recs(), lookup=_lookup)
    assert [(r.card, r.action) for r in kept] == [
        ("Solitude", "cut"), ("Propaganda", "add"),
    ]
    assert len(skipped) == 1
    assert skipped[0]["card"] == "Rhystic Study"
    assert skipped[0]["politics_tags"] == ["tax"]


def test_filter_skip_record_carries_the_project_voice_reason():
    """Disclosure contract, same as ``_filter_for_saturation``: the report
    says WHY the cut list is shorter, in the shared wording."""
    _, skipped = _filter_for_politics(_recs(), lookup=_lookup)
    assert skipped[0]["reason"] == POLITICS_SHIELD_REASON
    assert "sim-invisible" in skipped[0]["reason"]


def test_filter_never_touches_adds():
    """An ADD for a politics card survives — the guard is a shield, not a
    promotion, and filtering adds would silently ban the whole category."""
    adds = [SwapRecommendation(card=n, action="add", reason="x")
            for n in _POLITICS_NAMES]
    kept, skipped = _filter_for_politics(adds, lookup=_lookup)
    assert [r.card for r in kept] == list(_POLITICS_NAMES)
    assert skipped == []


def test_filter_opt_out_is_pass_through():
    deck_text = "[metadata]\nPoliticsGuard=off\n[Main]\n1 Rhystic Study\n"
    kept, skipped = _filter_for_politics(_recs(), deck_text, lookup=_lookup)
    assert [r.card for r in kept] == ["Rhystic Study", "Solitude",
                                      "Propaganda"]
    assert skipped == []


def test_filter_guard_defaults_on_without_deck_text():
    kept, skipped = _filter_for_politics(_recs(), lookup=_lookup)
    assert len(skipped) == 1


def test_filter_unresolvable_name_is_kept():
    """A Scryfall miss must not shield: the guard is earned. Otherwise an
    outage would freeze every cut the advisor can propose."""
    recs = [SwapRecommendation(card="Who Knows", action="cut", reason="x")]
    kept, skipped = _filter_for_politics(recs, lookup=lambda n: None)
    assert [r.card for r in kept] == ["Who Knows"]
    assert skipped == []


# ---------------------------------------------------------------------------
# Path 3 — card_score's cut ranking
# ---------------------------------------------------------------------------

def _cs_ctx(deck_cards, **kwargs):
    """A ``DeckContext`` with every external source injected (offline).

    35 Islands keep the effective-land rail clear and 11 draw fillers keep
    the role-floor rail clear, so the politics rail is what fires for the
    cards under test rather than one of the pre-existing rails.
    """
    kwargs.setdefault("commander_names", ["Marchesa, the Black Rose"])
    kwargs.setdefault("lookup", _lookup)
    kwargs.setdefault("combos", [])
    kwargs.setdefault("salt_scores", {})
    kwargs.setdefault("game_changers", [])
    filler = [f"Filler Draw {i}" for i in range(1, 12)]
    return cs.deck_context(
        deck_cards=["Island"] * 35 + filler + list(deck_cards), **kwargs,
    )


@pytest.mark.parametrize("card", _POLITICS_NAMES)
def test_card_score_cut_rail_blocks_politics_cards(offline_lookup, card):
    ctx = _cs_ctx([card, "Divination"])
    scored = cs.cut_score(card, ctx)
    assert scored.blocked
    # Assert on the REASON, not just the flag: several rails can block a
    # card, and this test is only meaningful if the politics one did.
    assert POLITICS_SHIELD_REASON in scored.block_reason
    assert card not in cs.cut_order(ctx.deck_cards, ctx)


def test_card_score_block_reason_names_the_mechanic_and_the_sim(
    offline_lookup,
):
    """A guard rail's reason string is user-visible, so it has to say
    which mechanic fired AND why the margin isn't evidence."""
    ctx = _cs_ctx(["Disrupt Decorum", "Divination"])
    reason = cs.cut_score("Disrupt Decorum", ctx).block_reason
    assert "goad" in reason
    assert POLITICS_SHIELD_REASON in reason


def test_card_score_non_politics_card_still_cuttable(offline_lookup):
    """Control: the rail is mechanic-specific, not a blanket refusal."""
    ctx = _cs_ctx(["Disrupt Decorum", "Divination"])
    assert not cs.cut_score("Divination", ctx).blocked
    assert "Divination" in cs.cut_order(ctx.deck_cards, ctx)


def test_card_score_opt_out_unblocks_the_cut(offline_lookup):
    """``PoliticsGuard=off`` read straight off the deck text, exactly as
    ``Protect=`` is."""
    deck_text = (
        "[metadata]\nName=Marchesa\nPoliticsGuard=off\n"
        "[Commander]\n1 Marchesa, the Black Rose\n"
        "[Main]\n35 Island\n1 Disrupt Decorum\n1 Divination\n"
    )
    ctx = cs.deck_context(deck_text, lookup=_lookup, combos=[],
                          salt_scores={}, game_changers=[])
    assert ctx.politics_guard is False
    assert not cs.cut_score("Disrupt Decorum", ctx).blocked


def test_card_score_guard_flag_survives_the_without_child(offline_lookup):
    """``cut_score`` scores a card against ``ctx.without(card)``. If the
    child re-derived the flag from its own (synthesized) text the opt-out
    would silently come back on mid-ranking."""
    deck_text = (
        "[metadata]\nPoliticsGuard=off\n[Commander]\n"
        "1 Marchesa, the Black Rose\n[Main]\n35 Island\n1 Disrupt Decorum\n"
    )
    ctx = cs.deck_context(deck_text, lookup=_lookup, combos=[],
                          salt_scores={}, game_changers=[])
    assert ctx.without("Disrupt Decorum").politics_guard is False
    on = _cs_ctx(["Disrupt Decorum"])
    assert on.without("Divination").politics_guard is True


def test_card_score_politics_tags_of_uses_the_injected_lookup():
    """``politics_tags_of`` must route through ``DeckContext.card`` — the
    memoized, never-raising, injected seam — not staples' own resolver."""
    ctx = _cs_ctx(["Rhystic Study"])
    assert ctx.politics_tags_of("Rhystic Study") == ("tax",)
    assert ctx.politics_tags_of("Unknown Card") == ()


def test_card_score_protect_rail_still_wins_the_reason(offline_lookup):
    """Rail ORDER: an explicit user lock is reported before the politics
    rail. A user who wrote ``Protect=`` should be told that's why."""
    ctx = _cs_ctx(["Disrupt Decorum"], protected_cards=["Disrupt Decorum"])
    assert "Protect=" in cs.cut_score("Disrupt Decorum", ctx).block_reason
