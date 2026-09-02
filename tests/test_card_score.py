"""Tests for ``commander_builder.card_score`` (FP-015).

FULLY OFFLINE. Every test injects a dict-backed ``lookup`` through
``deck_context(lookup=...)`` and injects the combo list, salt map and
Game-Changer list too, so nothing here reads the network, the Scryfall
snapshot cache, the EDHREC salt cache or ``data/combos.json``. Same
hermetic policy as tests/test_deck_legality.py, which prefers the
injection seam over monkeypatching.

The stubs return the SHAPE Scryfall returns — ``type_line``,
``oracle_text``, ``color_identity``, ``cmc``, ``produced_mana``,
``edhrec_rank``, ``prices.usd``, ``legalities.commander`` — so a
projection change upstream fails here rather than silently changing
rankings in production.

The centerpiece is the FP-015 **ordinal sanity suite** (the plan's
tier-1 validation): assert known orderings on fixed decks rather than
regress a score against sim margin, which FP-002 already established
would be uninformative at our sample size.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from commander_builder import card_score as cs
from commander_builder._advisor_heuristic import (
    _heuristic_swap_recommendations,
)
from commander_builder.edhrec_client import CardEntry, CommanderPage


# ---------------------------------------------------------------------------
# Card / deck builders
# ---------------------------------------------------------------------------

def _card(
    name: str,
    *,
    type_line: str = "Artifact",
    oracle: str = "",
    ci: str = "",
    cmc: float = 0.0,
    mana_cost: str = "",
    produced: str = "",
    rank: int | None = None,
    usd: str | None = None,
    legality: str = "legal",
) -> dict:
    """A Scryfall-shaped card dict. ``ci`` / ``produced`` are WUBRG(C)
    letter strings."""
    card: dict = {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "color_identity": list(ci),
        "cmc": float(cmc),
        # ``mana_cost`` is what deck_builder_manabase's pip_stats reads to
        # build the Karsten per-color demand; without it every deck reports
        # "no_demand" and mana_fit measures nothing.
        "mana_cost": mana_cost,
        "legalities": {"commander": legality},
        "prices": {"usd": usd},
    }
    if produced:
        card["produced_mana"] = list(produced)
    if rank is not None:
        card["edhrec_rank"] = rank
    return card


# A small, stable card universe shared by most tests.
_UNIVERSE: list[dict] = [
    _card("Sol Ring", oracle="{T}: Add {C}{C}.", cmc=1, mana_cost="{1}",
          produced="C", rank=1),
    _card("Worn Powerstone",
          oracle="Worn Powerstone enters tapped. {T}: Add {C}{C}.",
          cmc=3, mana_cost="{3}", produced="C", rank=2000),
    _card("Arcane Signet", oracle="{T}: Add one mana of any color in "
                                  "your commander's identity.",
          cmc=2, mana_cost="{2}", produced="WUBRG", rank=3),
    _card("Rhystic Study", type_line="Enchantment", ci="U", cmc=3,
          mana_cost="{2}{U}",
          oracle="Whenever an opponent casts a spell, that player may "
                 "pay {1}. If the player does not, you draw a card."),
    _card("Divination", type_line="Sorcery", ci="U", cmc=3,
          mana_cost="{2}{U}", oracle="Draw two cards."),
    _card("Counterspell", type_line="Instant", ci="U", cmc=2,
          mana_cost="{U}{U}", oracle="Counter target spell."),
    _card("Talrand, Sky Summoner", ci="U", cmc=3, mana_cost="{2}{U}",
          type_line="Legendary Creature — Merfolk Wizard",
          oracle="Whenever you cast an instant or sorcery spell, create "
                 "a 2/2 blue Drake creature token with flying."),
    _card("Island", type_line="Basic Land — Island", ci="U", cmc=0,
          produced="U", oracle="{T}: Add {U}."),
    _card("Command Tower", type_line="Land", ci="", cmc=0, produced="WUBRG",
          oracle="{T}: Add one mana of any color in your commander's "
                 "identity."),
    # Off-identity, banned and expensive probes for the gates/modifiers.
    _card("Lightning Bolt", type_line="Instant", ci="R", cmc=1,
          oracle="Lightning Bolt deals 3 damage to any target."),
    _card("Black Lotus", cmc=0, oracle="{T}, Sacrifice Black Lotus: Add "
                                       "three mana of any one color.",
          legality="banned"),
    _card("Mox Emerald", cmc=0, oracle="{T}: Add {G}.", legality="banned"),
    _card("Timmy's Custom Card", cmc=2, oracle="Do a thing.",
          legality="not_legal"),
    _card("Expensive Rock", oracle="{T}: Add {U}.", cmc=2, produced="U",
          ci="U", mana_cost="{2}", usd="90.00"),
    # Combo pieces.
    _card("Thassa's Oracle", type_line="Creature — Merfolk Wizard",
          ci="U", cmc=2, mana_cost="{U}{U}",
          oracle="When Thassa's Oracle enters the battlefield, look at "
                 "the top X cards of your library."),
    _card("Demonic Consultation", type_line="Instant", ci="B", cmc=1,
          oracle="Name a card. Exile the top six cards of your library."),
    _card("Underworld Breach", type_line="Enchantment", ci="R", cmc=2,
          oracle="Each nonland card in your graveyard has escape."),
    _card("Brain Freeze", type_line="Instant", ci="U", cmc=2,
          oracle="Target player mills three cards. Storm."),
    _card("Lion's Eye Diamond", cmc=0,
          oracle="{T}, Sacrifice Lion's Eye Diamond: Add three mana of "
                 "any one color."),
    # Bracket-pressure probes (names must match bracket_estimator's lists).
    _card("Demonic Tutor", type_line="Sorcery", ci="B", cmc=2,
          oracle="Search your library for a card, put that card into "
                 "your hand, then shuffle."),
    _card("Vampiric Tutor", type_line="Instant", ci="B", cmc=1,
          oracle="Search your library for a card, then shuffle and put "
                 "that card on top."),
    _card("Diabolic Tutor", type_line="Sorcery", ci="B", cmc=4,
          oracle="Search your library for a card, put that card into "
                 "your hand, then shuffle."),
    _card("Grim Tutor", type_line="Sorcery", ci="B", cmc=3,
          oracle="Search your library for a card, put that card into "
                 "your hand, then shuffle. You lose 3 life."),
    _card("Armageddon", type_line="Sorcery", ci="W", cmc=4,
          oracle="Destroy all lands."),
    _card("Time Warp", type_line="Sorcery", ci="U", cmc=5,
          oracle="Target player takes an extra turn after this one."),
    # Role probes for cut guard rails.
    _card("Cultivate", type_line="Sorcery", ci="G", cmc=3,
          oracle="Search your library for up to two basic land cards, "
                 "reveal them, put one onto the battlefield tapped and "
                 "the other into your hand, then shuffle."),
    _card("Swords to Plowshares", type_line="Instant", ci="W", cmc=1,
          oracle="Exile target creature. Its controller gains life "
                 "equal to its power."),
    _card("Wrath of God", type_line="Sorcery", ci="W", cmc=4,
          oracle="Destroy all creatures. They can't be regenerated."),
    _card("Craterhoof Behemoth", type_line="Creature — Beast", ci="G",
          cmc=8,
          oracle="When Craterhoof Behemoth enters the battlefield, "
                 "creatures you control gain trample and get +X/+X until "
                 "end of turn. You win the game."),
    _card("Sea Gate Restoration", type_line="Sorcery // Land", ci="U",
          cmc=5, mana_cost="{4}{U}",
          oracle="Draw cards equal to the number of cards in "
                 "your hand."),
    _card("Agadeem's Awakening", type_line="Sorcery // Land", ci="B",
          cmc=6, mana_cost="{X}{B}{B}{B}",
          oracle="Return from your graveyard to the battlefield any "
                 "number of target creature cards."),
]

# Interchangeable filler so a test can saturate a role with DISTINCT
# names — Commander is singleton, and a fixture that repeats one name
# would make ``DeckContext.without`` remove all the copies at once.
_UNIVERSE += [
    _card(f"Filler Draw {i}", type_line="Sorcery", ci="U", cmc=3,
          mana_cost="{2}{U}", oracle="Draw two cards.")
    for i in range(1, 10)
]

_INDEX = {c["name"].lower(): c for c in _UNIVERSE}


def _lookup(name):
    """Case-insensitive ``name -> card`` stub over ``_UNIVERSE``."""
    return _INDEX.get((name or "").strip().lower())


@pytest.fixture(autouse=True)
def _offline_staples(monkeypatch):
    """Pin ``staples``' own lookup seam as well as card_score's.

    ``staples.count_deck_roles`` / ``commander_role_credits`` — which
    ``role_target_report`` (and therefore ``role_fit``) route through —
    resolve names via ``commander_builder.staples.lookup_card``, whose
    module-level import exists specifically so tests can monkeypatch it
    (see that import's comment). It is a SEPARATE seam from
    ``deck_context(lookup=...)``, so without this the suite would reach
    for Scryfall. Same for the advisor's ``_role_for_card``, which reads
    ``improvement_advisor.lookup_card``.
    """
    monkeypatch.setattr("commander_builder.staples.lookup_card", _lookup)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", _lookup,
    )


def _ctx(deck_cards, **kwargs) -> cs.DeckContext:
    """A DeckContext with every external source injected (offline)."""
    kwargs.setdefault("commander_names", ["Talrand, Sky Summoner"])
    kwargs.setdefault("lookup", _lookup)
    kwargs.setdefault("combos", [])
    kwargs.setdefault("salt_scores", {})
    kwargs.setdefault("game_changers", [])
    return cs.deck_context(deck_cards=list(deck_cards), **kwargs)


def _blue_deck(extra=()) -> list[str]:
    """A plausible mono-U shell: 35 Islands plus whatever ``extra`` adds."""
    return ["Island"] * 35 + list(extra)


# ---------------------------------------------------------------------------
# The tuning surface
# ---------------------------------------------------------------------------

def test_weights_sum_to_one():
    """``100 * Σ w_k f_k`` only spans 0..100 when the weights sum to 1.0.

    Pinned because CARD_SCORE_WEIGHTS is the documented tuning surface —
    a retune that forgets to rebalance would silently rescale the axis
    every other consumer reads.
    """
    assert sum(cs.CARD_SCORE_WEIGHTS.values()) == pytest.approx(1.0)
    assert set(cs.CARD_SCORE_WEIGHTS) == {
        "consensus", "synergy", "role_fit", "curve_fit", "mana_fit",
    }


def test_modifier_magnitudes_match_the_spec_table():
    assert cs.CARD_SCORE_MODIFIERS == {
        "combo_completion": 15.0,
        "combo_partial": 6.0,
        "redundancy_relief": 5.0,
        "owned": 6.0,
        "price_penalty": -12.0,
        "salt_penalty": -10.0,
        "bracket_pressure": -20.0,
        "mdfc_bonus": 3.0,
        # FP-019.4 primer-derived penalties (heuristics §3/§5)
        "commander_dependence": -8.0,
        "tempo_fail": -6.0,
        "capped_engine": -4.0,
        "tutor_top_delta": -5.0,
    }


def test_score_is_bounded_to_the_documented_scale():
    ctx = _ctx(_blue_deck(), bracket=3)
    for name in ("Sol Ring", "Divination", "Counterspell", "Command Tower"):
        result = cs.score_card(name, ctx)
        assert 0.0 <= result.total <= 100.0


# ---------------------------------------------------------------------------
# ORDINAL SANITY SUITE — the FP-015 tier-1 validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deck,bracket,archetype", [
    (_blue_deck(), None, None),                       # bare shell
    (_blue_deck(["Divination", "Counterspell"]), 2, "control"),
    (_blue_deck(["Sol Ring"] and ["Arcane Signet"] * 1), 4, "combo"),
    (["Island"] * 20 + ["Divination"] * 8, 3, "midrange"),
    (["Island"] * 38, 5, "aggro"),
])
def test_sol_ring_outranks_worn_powerstone_in_every_deck(
    deck, bracket, archetype,
):
    """Both are colorless ramp rocks; Sol Ring is cheaper and universally
    played. If any weight ever gets a sign error, this is the first
    assertion to fall over."""
    ctx = _ctx(deck, bracket=bracket, archetype=archetype)
    sol = cs.score_card("Sol Ring", ctx, inclusion_pct=71.0)
    powerstone = cs.score_card("Worn Powerstone", ctx, inclusion_pct=5.0)
    assert sol.total > powerstone.total, (
        f"{sol.total} !> {powerstone.total} for deck of {len(deck)}"
    )


def test_rhystic_study_outranks_divination_and_the_gap_narrows_at_b2():
    """Both are 3-mana blue card draw, so curve_fit and role_fit are
    identical by construction and only the deck-relative terms move.

    At B4 the gap is the raw consensus/synergy difference. At B2 the
    salt penalty (Rhystic Study is a notorious table-unfriendly card,
    Divination is not) charges Rhystic Study and the gap NARROWS — the
    bracket is doing real work, which is the property this test exists
    to pin. Salt is scored at B <= 3 only, matching
    ``_SALT_WARN_THRESHOLD``'s bracket scope.
    """
    salt = {"rhystic study": 2.4, "divination": 0.0}

    def gap(bracket: int) -> float:
        ctx = _ctx(_blue_deck(), bracket=bracket, salt_scores=salt)
        rhystic = cs.score_card("Rhystic Study", ctx, inclusion_pct=45.0)
        divination = cs.score_card("Divination", ctx, inclusion_pct=12.0)
        assert rhystic.total > divination.total, f"at B{bracket}"
        return rhystic.total - divination.total

    gap_b4 = gap(4)
    gap_b2 = gap(2)
    assert gap_b2 < gap_b4, f"B2 gap {gap_b2} should be under B4 gap {gap_b4}"


def test_salt_is_the_reason_the_b2_gap_narrows():
    """Guards the mechanism, not just the inequality: a bracket-2 score
    for a salty card must actually carry the salt_penalty modifier with
    an explanation string."""
    ctx = _ctx(_blue_deck(), bracket=2, salt_scores={"rhystic study": 2.4})
    result = cs.score_card("Rhystic Study", ctx, inclusion_pct=45.0)
    salt = next(m for m in result.modifiers if m.name == "salt_penalty")
    assert -10.0 <= salt.points < 0.0
    assert "salt" in salt.explanation.lower()
    # ...and at B4 the table has opted in, so it must not fire.
    b4 = _ctx(_blue_deck(), bracket=4, salt_scores={"rhystic study": 2.4})
    assert not [m for m in cs.score_card("Rhystic Study", b4).modifiers
                if m.name == "salt_penalty"]


def test_role_fit_is_deck_relative_not_a_global_ranking():
    """The same card scores lower into a deck that is already saturated
    on its role. This is what separates CardScore from a power rating."""
    hungry = _ctx(_blue_deck())
    saturated = _ctx(["Island"] * 25 + ["Divination"] * 12)
    thin = cs.score_card("Rhystic Study", hungry, inclusion_pct=45.0)
    full = cs.score_card("Rhystic Study", saturated, inclusion_pct=45.0)
    assert thin.components["role_fit"].value == 1.0
    assert full.components["role_fit"].value == 0.0
    assert thin.total > full.total


# ---------------------------------------------------------------------------
# Gates — each zeroes, each reports its reason
# ---------------------------------------------------------------------------

def test_legal_gate_zeroes_a_banned_card_with_its_reason():
    """Scryfall-backed via ``deck_legality.scan_banned`` — no hardcoded
    ban set anywhere in this module."""
    ctx = _ctx(_blue_deck(), bracket=3)
    result = cs.score_card("Black Lotus", ctx, inclusion_pct=90.0)
    assert result.total == 0.0
    assert result.gated
    gate = next(g for g in result.gates if g.name == "legal")
    assert gate.passed is False
    assert "banned" in gate.reason.lower()
    assert gate.reason in result.explanations


def test_legal_gate_zeroes_a_not_in_format_card():
    ctx = _ctx(_blue_deck())
    result = cs.score_card("Timmy's Custom Card", ctx)
    assert result.total == 0.0
    assert "not legal" in next(
        g.reason for g in result.gates if g.name == "legal"
    ).lower()


def test_legal_gate_passes_with_a_note_when_scryfall_cannot_verify():
    """Unavailable != illegal. A dead lookup must not make every card
    unrankable — it makes the legality *unverified*, and says so."""
    ctx = cs.deck_context(deck_cards=_blue_deck(),
                          commander_names=["Talrand, Sky Summoner"],
                          lookup=lambda name: None, combos=[],
                          salt_scores={}, game_changers=[])
    result = cs.score_card("Some Uncached Card", ctx, inclusion_pct=50.0)
    gate = next(g for g in result.gates if g.name == "legal")
    assert gate.passed is True
    assert "unverified" in gate.reason
    assert result.total > 0.0


def test_color_identity_gate_zeroes_an_off_color_card():
    ctx = _ctx(_blue_deck())  # commander is mono-U Talrand
    result = cs.score_card("Lightning Bolt", ctx, inclusion_pct=50.0)
    assert result.total == 0.0
    gate = next(g for g in result.gates if g.name == "color_identity")
    assert gate.passed is False
    assert "color identity" in gate.reason.lower()
    assert "R" in gate.reason


def test_singleton_gate_zeroes_a_card_already_in_the_deck():
    ctx = _ctx(_blue_deck(["Divination"]))
    result = cs.score_card("Divination", ctx, inclusion_pct=50.0)
    assert result.total == 0.0
    gate = next(g for g in result.gates if g.name == "singleton")
    assert gate.passed is False
    assert "already in the deck" in gate.reason


def test_singleton_gate_ignores_basic_lands():
    """A 36th Island is a legal add; the singleton rule exempts basics."""
    ctx = _ctx(_blue_deck())
    result = cs.score_card("Island", ctx)
    gate = next(g for g in result.gates if g.name == "singleton")
    assert gate.passed is True


def test_bracket_cap_gate_zeroes_a_game_changer_over_the_cap():
    """B1/B2 allow zero Game Changers, B3 allows three."""
    deck = _blue_deck(["Rhystic Study"])
    b2 = _ctx(deck, bracket=2, game_changers=["Sol Ring"])
    result = cs.score_card("Sol Ring", b2, inclusion_pct=71.0)
    assert result.total == 0.0
    gate = next(g for g in result.gates if g.name == "bracket_cap")
    assert gate.passed is False
    assert "Game Changer" in gate.reason and "bracket 2" in gate.reason
    # B4 is unrestricted, so the same card sails through.
    b4 = _ctx(deck, bracket=4, game_changers=["Sol Ring"])
    assert cs.score_card("Sol Ring", b4, inclusion_pct=71.0).total > 0.0


def test_bracket_cap_gate_counts_the_decks_existing_game_changers():
    """At B3 the cap is 3: a deck holding 3 Game Changers can't take a
    fourth, a deck holding 2 can."""
    gc = ["Rhystic Study", "Demonic Tutor", "Vampiric Tutor", "Sol Ring"]
    full = _ctx(_blue_deck(["Rhystic Study", "Demonic Tutor",
                            "Vampiric Tutor"]),
                bracket=3, game_changers=gc)
    assert cs.score_card("Sol Ring", full, inclusion_pct=71.0).total == 0.0
    room = _ctx(_blue_deck(["Rhystic Study", "Demonic Tutor"]),
                bracket=3, game_changers=gc)
    assert cs.score_card("Sol Ring", room, inclusion_pct=71.0).total > 0.0


def test_every_gate_is_reported_even_when_it_passes():
    ctx = _ctx(_blue_deck(), bracket=3)
    result = cs.score_card("Counterspell", ctx, inclusion_pct=50.0)
    assert {g.name for g in result.gates} == {
        "legal", "color_identity", "singleton", "bracket_cap",
    }
    assert not result.gated


# ---------------------------------------------------------------------------
# "Unavailable != bad" — renormalization, never a zero
# ---------------------------------------------------------------------------

def test_unavailable_component_renormalizes_rather_than_zeroing():
    """A card with no synergy figure and no corpus must not be scored as
    if its synergy were measured at zero — the surviving weights are
    renormalized and the drop is reported."""
    ctx = _ctx(_blue_deck())
    result = cs.score_card("Divination", ctx, inclusion_pct=30.0)
    assert "synergy" in result.unavailable
    synergy = result.components["synergy"]
    assert synergy.value is None
    assert synergy.effective_weight == 0.0
    assert synergy.points == 0.0
    # Surviving weights renormalize to 1.0.
    live = sum(c.effective_weight for c in result.components.values()
               if c.available)
    assert live == pytest.approx(1.0)
    assert "unavailable" in synergy.explanation


def test_zero_fill_would_have_scored_lower_than_renormalization():
    """The contract, stated as an inequality: dropping a component must
    leave the score where the measured components put it, not drag it
    toward zero."""
    ctx = _ctx(_blue_deck())
    partial = cs.score_card("Divination", ctx, inclusion_pct=30.0)
    zero_filled = sum(
        100.0 * cs.CARD_SCORE_WEIGHTS[name] * (comp.value or 0.0)
        for name, comp in partial.components.items()
    )
    assert partial.base > zero_filled


def test_synergy_renormalizes_to_edhrec_alone_under_min_corpus():
    """Under ``MIN_CORPUS_DECKS`` harvested decks the lift term is
    dropped from the blend and the EDHREC term carries it alone — the
    mode is surfaced in the explanation, not hidden."""
    small = _ctx(_blue_deck(), corpus_decks=cs.MIN_CORPUS_DECKS - 1,
                 lift_scores={"divination": 3.0})
    result = cs.score_card("Divination", small, inclusion_pct=30.0,
                           synergy_pct=20.0)
    assert result.components["synergy"].value == pytest.approx(0.5)
    assert "corpus" in result.components["synergy"].explanation

    big = _ctx(_blue_deck(), corpus_decks=cs.MIN_CORPUS_DECKS,
               lift_scores={"divination": 3.0})
    blended = cs.score_card("Divination", big, inclusion_pct=30.0,
                            synergy_pct=20.0)
    # 0.55 * 0.5 + 0.45 * clamp((3.0 - 1.0) / 2.0) = 0.725
    assert blended.components["synergy"].value == pytest.approx(0.725)
    assert blended.total > result.total


def test_consensus_falls_back_to_scryfall_edhrec_rank():
    """``edhrec_rank`` ships in every cached snapshot and was read by
    zero code before FP-015. With no inclusion% it carries consensus."""
    ctx = _ctx(_blue_deck())
    top = cs.score_card("Sol Ring", ctx)               # edhrec_rank 1
    tail = cs.score_card("Worn Powerstone", ctx)       # edhrec_rank 2000
    assert top.components["consensus"].value == pytest.approx(1.0)
    assert 0.0 < tail.components["consensus"].value < 1.0
    assert "rank" in tail.components["consensus"].explanation


def test_measured_zero_synergy_is_scored_not_renormalized_away():
    """EDHREC synergy is a signed delta from the format baseline, so
    0.0 is a REAL measurement and must stay a scored component. The
    pre-fix truthiness test (``if synergy_pct``) renormalized a
    measured 0.0 away — which let a 0%-synergy card OUTRANK a
    0.1%-synergy card: the zero card's synergy weight shifted onto its
    stronger components while the 0.1% card was charged a near-zero."""
    ctx = _ctx(_blue_deck())
    zero = cs.score_card("Divination", ctx, inclusion_pct=30.0,
                         synergy_pct=0.0)
    assert "synergy" not in zero.unavailable
    assert zero.components["synergy"].available
    assert zero.components["synergy"].value == pytest.approx(0.0)
    tiny = cs.score_card("Divination", ctx, inclusion_pct=30.0,
                         synergy_pct=0.1)
    assert (zero.components["synergy"].value
            < tiny.components["synergy"].value)
    # Same card, same deck, every other input identical — the measured
    # 0.0 must not outrank the measured 0.1 overall either.
    assert zero.total < tiny.total


def test_negative_synergy_is_measured_bad_not_unavailable():
    """EDHREC reports negative synergy for cards played BELOW their
    format baseline. That clamps to a scored 0.0 — measured bad —
    exactly like the literal 0.0, never to 'unavailable'."""
    ctx = _ctx(_blue_deck())
    neg = cs.score_card("Divination", ctx, inclusion_pct=30.0,
                        synergy_pct=-5.0)
    assert "synergy" not in neg.unavailable
    assert neg.components["synergy"].value == pytest.approx(0.0)


def test_zero_inclusion_defers_to_edhrec_rank_not_a_hard_zero():
    """Pin the deliberate asymmetry with the synergy fix: a literal
    0.0 ``inclusion_pct`` is overwhelmingly the EDHREC client's
    missing-data sentinel (``CardEntry`` defaults the field to 0.0 and
    its parsers coerce absent values with ``or 0``), so ``_f_consensus``
    falls through to ``edhrec_rank`` — a real per-card measurement —
    instead of scoring a hard 0. See the comment in ``_f_consensus``."""
    ctx = _ctx(_blue_deck())
    zero = cs.score_card("Sol Ring", ctx, inclusion_pct=0.0)  # rank 1
    assert zero.components["consensus"].value == pytest.approx(1.0)
    assert "rank" in zero.components["consensus"].explanation


def test_consensus_treats_a_raw_deck_count_as_not_a_percentage():
    """EDHREC sometimes reports a raw deck count in ``inclusion_pct``
    (30627 == 'in 30627 decks'), a quirk the advisor's rationale strings
    already special-case. Treating it as a percentage would hand every
    such card a free 1.0, so we fall through to ``edhrec_rank``."""
    ctx = _ctx(_blue_deck())
    counted = cs.score_card("Worn Powerstone", ctx, inclusion_pct=30627.0)
    ranked = cs.score_card("Worn Powerstone", ctx)
    assert counted.components["consensus"].value == pytest.approx(
        ranked.components["consensus"].value)
    assert counted.components["consensus"].value < 1.0


def test_manabase_outage_renormalizes_mana_fit():
    """``manabase_report`` returns None on a majority-lookup failure;
    that must renormalize, not score every card's mana fit at zero."""
    ctx = cs.deck_context(deck_cards=["Nope A", "Nope B", "Nope C"],
                          commander_names=["Talrand, Sky Summoner"],
                          lookup=lambda n: (_lookup(n)
                                            if n == "Talrand, Sky Summoner"
                                            else None),
                          combos=[], salt_scores={}, game_changers=[])
    result = cs.score_card("Divination", ctx, inclusion_pct=30.0)
    assert result.components["mana_fit"].value is None
    assert "unavailable" in result.components["mana_fit"].explanation


def test_all_components_unavailable_degrades_to_a_zero_base_not_a_crash():
    ctx = cs.deck_context(deck_cards=[], commander_names=[],
                          lookup=lambda n: None, combos=[],
                          salt_scores={}, game_changers=[])
    result = cs.score_card("Unknown Card", ctx)
    assert result.base == 0.0
    assert len(result.unavailable) == len(cs.CARD_SCORE_WEIGHTS)


# ---------------------------------------------------------------------------
# mana_fit — Karsten targets doing evaluative work
# ---------------------------------------------------------------------------

def test_mana_fit_rewards_a_source_for_an_under_served_color():
    """A land producing the deck's colors when sources are short scores
    high; a card producing nothing in-identity scores 0.0."""
    deck = ["Island"] * 4 + ["Rhystic Study", "Counterspell", "Divination"]
    ctx = _ctx(deck)
    tower = cs.score_card("Command Tower", ctx)
    rock = cs.score_card("Sol Ring", ctx)
    assert tower.components["mana_fit"].value > 0.0
    assert rock.components["mana_fit"].value == 0.0
    assert "no mana in the deck's colors" in (
        rock.components["mana_fit"].explanation
    )


def test_mana_fit_falls_off_once_sources_hit_target():
    thin = _ctx(["Island"] * 4 + ["Counterspell", "Rhystic Study"])
    fat = _ctx(["Island"] * 38 + ["Counterspell", "Rhystic Study"])
    assert (cs.score_card("Command Tower", thin).components["mana_fit"].value
            > cs.score_card("Command Tower", fat).components["mana_fit"].value)


# ---------------------------------------------------------------------------
# curve_fit
# ---------------------------------------------------------------------------

def test_archetype_curve_tilts_down_for_aggro_and_up_for_control():
    aggro = cs.archetype_curve("aggro")
    control = cs.archetype_curve("control")
    assert aggro[1] > control[1]
    assert aggro[6] < control[6]
    # Tilting changes the SHAPE, not the number of slots asked for.
    assert sum(aggro.values()) == pytest.approx(cs.CURVE_NONLAND_SLOTS)
    assert sum(control.values()) == pytest.approx(cs.CURVE_NONLAND_SLOTS)


def test_curve_fit_is_not_applicable_to_lands():
    """The target curve is a NONLAND curve — scoring a land in its
    bucket-0 slot would measure something the model does not describe."""
    ctx = _ctx(_blue_deck())
    result = cs.score_card("Command Tower", ctx)
    assert result.components["curve_fit"].value is None
    assert "land" in result.components["curve_fit"].explanation


# ---------------------------------------------------------------------------
# Modifiers
# ---------------------------------------------------------------------------

_ORACLE_COMBO = {
    "cards": ["Thassa's Oracle", "Demonic Consultation"],
    "produces": "Win the game", "popularity": 314670,
}
_BREACH_COMBO = {
    "cards": ["Underworld Breach", "Brain Freeze", "Lion's Eye Diamond"],
    "produces": "Infinite storm", "popularity": 5000,
}


def test_combo_completion_fires_when_the_other_piece_is_in_deck():
    ctx = _ctx(_blue_deck(["Thassa's Oracle"]), combos=[_ORACLE_COMBO],
               commander_names=[])
    result = cs.score_card("Demonic Consultation", ctx, inclusion_pct=20.0)
    mod = next(m for m in result.modifiers if m.name == "combo_completion")
    assert mod.points == 15.0
    assert "Thassa's Oracle" in mod.explanation


def test_combo_completion_does_not_fire_without_the_other_piece():
    ctx = _ctx(_blue_deck(), combos=[_ORACLE_COMBO], commander_names=[])
    result = cs.score_card("Demonic Consultation", ctx, inclusion_pct=20.0)
    assert not [m for m in result.modifiers
                if m.name in ("combo_completion", "combo_partial")]


def test_combo_partial_fires_for_two_of_a_three_card_line():
    """One piece present + this card = 2 of 3, worth less than a
    completion but more than nothing."""
    ctx = _ctx(_blue_deck(["Underworld Breach"]), combos=[_BREACH_COMBO],
               commander_names=[])
    result = cs.score_card("Brain Freeze", ctx, inclusion_pct=10.0)
    mod = next(m for m in result.modifiers if m.name == "combo_partial")
    assert mod.points == 6.0
    assert "Underworld Breach" in mod.explanation


def test_combo_completion_outranks_combo_partial():
    partial_ctx = _ctx(_blue_deck(["Underworld Breach"]),
                       combos=[_BREACH_COMBO], commander_names=[])
    complete_ctx = _ctx(_blue_deck(["Underworld Breach",
                                    "Lion's Eye Diamond"]),
                        combos=[_BREACH_COMBO], commander_names=[])
    partial = cs.score_card("Brain Freeze", partial_ctx, inclusion_pct=10.0)
    complete = cs.score_card("Brain Freeze", complete_ctx,
                             inclusion_pct=10.0)
    assert complete.total > partial.total


def test_bracket_pressure_penalizes_a_game_changer_tutor_over_budget():
    """Demonic / Vampiric Tutor are Game Changers AND tutors. Under the
    Game-Changer cap they pass the gate, but at a bracket whose tutor
    budget is already spent they take bracket_pressure — 'tutors should
    be sparse' at the low brackets."""
    gc = ["Demonic Tutor"]
    deck = ["Island"] * 35 + ["Vampiric Tutor", "Grim Tutor",
                              "Diabolic Tutor"]
    ctx = _ctx(deck, bracket=3, game_changers=gc,
               commander_names=[])
    result = cs.score_card("Demonic Tutor", ctx, inclusion_pct=20.0)
    assert not result.gated                      # under the B3 GC cap of 3
    mod = next(m for m in result.modifiers if m.name == "bracket_pressure")
    assert mod.points < 0.0
    assert "tutor" in mod.explanation.lower()
    assert "bracket 3" in mod.explanation
    # A bracket with no tutor budget doesn't charge it.
    loose = _ctx(deck, bracket=5, game_changers=gc, commander_names=[])
    assert not [m for m in cs.score_card("Demonic Tutor", loose).modifiers
                if m.name == "bracket_pressure"]


def test_bracket_pressure_is_clamped_to_its_documented_floor():
    """Armageddon at B2 stacks the MLD charge; the total must never
    exceed the -20 the modifier table publishes."""
    ctx = _ctx(["Island"] * 35 + ["Armageddon"], bracket=2,
               commander_names=[])
    result = cs.score_card("Armageddon", ctx, apply_gates=False)
    mod = next(m for m in result.modifiers if m.name == "bracket_pressure")
    assert mod.points == cs.CARD_SCORE_MODIFIERS["bracket_pressure"]


def test_bracket_pressure_charges_a_second_extra_turn_spell_at_b3():
    ctx = _ctx(["Island"] * 35 + ["Temporal Manipulation"], bracket=3,
               commander_names=[])
    result = cs.score_card("Time Warp", ctx, apply_gates=False)
    mod = next(m for m in result.modifiers if m.name == "bracket_pressure")
    assert "chaining" in mod.explanation


def test_price_penalty_scales_with_the_overage():
    cheap = _ctx(_blue_deck(), price_soft_cap=200.0)
    strict = _ctx(_blue_deck(), price_soft_cap=10.0)
    assert not [m for m in cs.score_card("Expensive Rock", cheap).modifiers
                if m.name == "price_penalty"]
    mod = next(m for m in cs.score_card("Expensive Rock", strict).modifiers
               if m.name == "price_penalty")
    assert mod.points == pytest.approx(-12.0)     # 90 vs a 10 cap = clamped
    assert "$90.00" in mod.explanation


def test_owned_bonus_requires_collection_bias_to_be_active():
    from commander_builder.collection import name_key
    keys = frozenset({name_key("Sol Ring")})
    off = _ctx(_blue_deck(), collection_keys=keys, collection_bias=False)
    on = _ctx(_blue_deck(), collection_keys=keys, collection_bias=True)
    assert not [m for m in cs.score_card("Sol Ring", off).modifiers
                if m.name == "owned"]
    mod = next(m for m in cs.score_card("Sol Ring", on).modifiers
               if m.name == "owned")
    assert mod.points == 6.0


def test_mdfc_bonus_fires_for_a_modal_land():
    ctx = _ctx(_blue_deck())
    mod = next(m for m in cs.score_card("Sea Gate Restoration", ctx).modifiers
               if m.name == "mdfc_bonus")
    assert mod.points == 3.0
    assert not [m for m in cs.score_card("Divination", ctx).modifiers
                if m.name == "mdfc_bonus"]


def test_redundancy_relief_fires_when_the_deck_is_thin_on_an_effect():
    thin = _ctx(["Island"] * 35 + ["Counterspell"], commander_names=[])
    mod = [m for m in cs.score_card("Counterspell", thin,
                                    apply_gates=False).modifiers
           if m.name == "redundancy_relief"]
    assert mod and mod[0].points == 5.0
    assert "cover" in mod[0].explanation


def test_every_modifier_carries_an_explanation_string():
    ctx = _ctx(_blue_deck(["Thassa's Oracle"]), bracket=2,
               combos=[_ORACLE_COMBO], commander_names=[],
               price_soft_cap=10.0, salt_scores={"rhystic study": 3.0},
               collection_keys=frozenset({"sol ring"}),
               collection_bias=True)
    for name in ("Sol Ring", "Rhystic Study", "Expensive Rock",
                 "Demonic Consultation", "Sea Gate Restoration"):
        for mod in cs.score_card(name, ctx, apply_gates=False).modifiers:
            assert mod.explanation.strip(), f"{name}/{mod.name}"


# ---------------------------------------------------------------------------
# Explanations / evidence payload
# ---------------------------------------------------------------------------

def test_evidence_payload_carries_the_full_breakdown():
    """``SwapRecommendation.evidence`` already flows to the UI, so the
    breakdown surfaces with no schema change."""
    ctx = _ctx(_blue_deck(), bracket=3)
    payload = cs.score_card("Divination", ctx, inclusion_pct=30.0
                            ).as_evidence()
    assert set(payload) >= {"total", "base", "gated", "gates", "components",
                            "modifiers", "unavailable", "explanations"}
    assert set(payload["components"]) == set(cs.CARD_SCORE_WEIGHTS)
    assert payload["explanations"]
    # The payload must self-identify as a prior, never a power rating.
    assert payload["kind"] == "ranking_prior"


def test_a_gated_card_explains_itself_with_the_gate_reason_only():
    ctx = _ctx(_blue_deck())
    result = cs.score_card("Lightning Bolt", ctx, inclusion_pct=50.0)
    assert result.explanations == result.gate_reasons


def test_no_explanation_string_presents_the_score_as_a_power_rating():
    """FP-015's framing constraint, enforced mechanically: the UI must
    never be handed a phrase that reads as a card-quality verdict."""
    banned_words = ("power level", "power rating", "card quality",
                    "best card", "strongest", "tier")
    ctx = _ctx(_blue_deck(["Thassa's Oracle"]), bracket=2,
               combos=[_ORACLE_COMBO], commander_names=[],
               price_soft_cap=10.0, salt_scores={"rhystic study": 3.0})
    for name in ("Sol Ring", "Rhystic Study", "Divination", "Command Tower",
                 "Demonic Consultation", "Expensive Rock", "Black Lotus"):
        for line in cs.score_card(name, ctx, inclusion_pct=40.0
                                  ).explanations:
            low = line.lower()
            for word in banned_words:
                assert word not in low, f"{name}: {line}"


# ---------------------------------------------------------------------------
# Cut scoring + guard rails
# ---------------------------------------------------------------------------

def test_cut_score_is_the_complement_scored_against_the_deck_minus_the_card():
    """``cut_score(card) = 100 - CardScore(card | deck_without_card)``.

    Scoring against the FULL deck would charge every member of a
    saturated role for its own presence and flatten them all; removing
    the card first is what lets the weakest member surface.
    """
    deck = ["Island"] * 30 + ["Divination"] * 6 + ["Rhystic Study"]
    ctx = _ctx(deck)
    result = cs.cut_score("Divination", ctx)
    inner = cs.score_card("Divination", ctx.without("Divination"),
                          apply_gates=False)
    assert result.score == pytest.approx(100.0 - inner.total)
    # The card is absent from the context the score was computed against.
    assert "divination" not in ctx.without("Divination").deck_keys or (
        deck.count("Divination") > 1)


def test_derived_role_report_matches_a_full_recount():
    """``without()`` subtracts one card from its parent's role report
    instead of re-walking the deck (cut ordering builds one child per
    card, and a fresh walk each time would be quadratic). Exactness is
    the whole premise, so pin it against the real thing."""
    from commander_builder.staples import role_target_report
    deck = (["Island"] * 33 + [f"Filler Draw {i}" for i in range(1, 6)]
            + ["Cultivate", "Swords to Plowshares", "Wrath of God",
               "Craterhoof Behemoth", "Divination"])
    ctx = _ctx(deck)
    for removed in ("Divination", "Cultivate", "Craterhoof Behemoth",
                    "Island", "Wrath of God"):
        derived = ctx.without(removed).role_report
        expected = role_target_report(
            [n for n in deck if n.lower() != removed.lower()],
            list(ctx.commander_names),
        )
        assert derived == expected, removed


def test_cut_ordering_is_by_score_then_name_for_determinism():
    deck = ["Island"] * 33 + ["Divination", "Worn Powerstone",
                              "Counterspell"]
    ctx = _ctx(deck)
    ordered = cs.cut_candidates(ctx)
    scores = [(-c.score, c.card) for c in ordered]
    assert scores == sorted(scores)


def test_cut_guard_rail_never_cuts_a_protect_line():
    deck = ["Island"] * 33 + ["Divination", "Counterspell", "Cultivate"]
    ctx = _ctx(deck, protected_cards=["Counterspell"])
    assert "Counterspell" not in cs.cut_order(deck, ctx)
    blocked = cs.cut_score("Counterspell", ctx)
    assert blocked.blocked and "Protect=" in blocked.block_reason


def test_protect_lines_are_read_off_the_deck_text():
    deck_text = (
        "[metadata]\nProtect=Counterspell\n[Commander]\n"
        "1 Talrand, Sky Summoner\n[Main]\n1 Counterspell\n1 Divination\n"
    )
    assert cs.protected_from_deck_text(deck_text) == ["Counterspell"]
    ctx = cs.deck_context(deck_text, lookup=_lookup, combos=[],
                          salt_scores={}, game_changers=[])
    assert "counterspell" in ctx.protected_keys
    assert "Counterspell" not in cs.cut_order(ctx.deck_cards, ctx)


def test_cut_guard_rail_never_cuts_below_a_role_target():
    """One removal spell in the deck against a target of 8 — cutting it
    would deepen a deficit the advisor is supposed to close."""
    deck = ["Island"] * 33 + ["Swords to Plowshares", "Divination"]
    ctx = _ctx(deck)
    blocked = cs.cut_score("Swords to Plowshares", ctx)
    assert blocked.blocked
    assert "below its target" in blocked.block_reason
    assert "Swords to Plowshares" not in cs.cut_order(deck, ctx)


def test_cut_guard_rail_never_drops_effective_lands_below_33():
    at_floor = _ctx(["Island"] * 33 + ["Divination"])
    assert cs.cut_score("Island", at_floor).blocked
    assert "effective lands" in cs.cut_score("Island", at_floor).block_reason
    spare = _ctx(["Island"] * 38 + ["Divination"])
    assert not cs.cut_score("Island", spare).blocked


def test_effective_land_floor_counts_mdfcs_as_half_a_land():
    """Matches ``deck_health``'s counting: a spell-front MDFC is worth
    0.5 lands, which is why the band bottoms out at 33 rather than 36."""
    ctx = _ctx(["Island"] * 32 + ["Sea Gate Restoration",
                                  "Agadeem's Awakening", "Divination"])
    assert ctx.effective_lands == pytest.approx(33.0)
    blocked = cs.cut_score("Sea Gate Restoration", ctx)
    assert blocked.blocked and "effective lands" in blocked.block_reason


def test_cut_guard_rail_never_cuts_a_piece_of_a_detected_combo():
    deck = (["Island"] * 33 + ["Thassa's Oracle", "Demonic Consultation",
                               "Divination"])
    ctx = _ctx(deck, combos=[_ORACLE_COMBO], commander_names=[])
    assert "thassa's oracle" in ctx.combo_pieces
    blocked = cs.cut_score("Thassa's Oracle", ctx)
    assert blocked.blocked and "combo" in blocked.block_reason
    assert "Thassa's Oracle" not in cs.cut_order(deck, ctx)


def test_cut_respects_the_like_for_like_role_constraint():
    """Mirrors ``deck_builder_personalize.lift_swaps`` lines 217-224: a
    swap with no same-role slot is skipped rather than allowed to
    distort the deck's role counts."""
    assert cs.like_for_like("draw", "draw") is True
    assert cs.like_for_like("draw", "ramp") is False
    assert cs.like_for_like("win_condition", "finisher") is True
    assert cs.like_for_like("unknown", "unknown") is False
    deck = (["Island"] * 34 + [f"Filler Draw {i}" for i in range(1, 10)]
            + ["Divination", "Rhystic Study", "Cultivate"])
    ctx = _ctx(deck)
    draw_only = cs.cut_order(deck, ctx, for_role="draw")
    assert draw_only
    assert all(ctx.role_of(n) == "draw" for n in draw_only)


def test_cut_score_surfaces_the_weakest_member_of_a_saturated_role():
    """The whole point of cut scoring: with draw saturated, the cheapest
    signal separates the weakest draw spell from the strongest."""
    deck = (["Island"] * 33 + [f"Filler Draw {i}" for i in range(1, 10)]
            + ["Divination", "Rhystic Study"])
    ctx = _ctx(deck, lift_scores={"rhystic study": 3.0, "divination": 1.0},
               corpus_decks=50)
    scored = {c.card: c.score for c in cs.cut_candidates(ctx)}
    assert scored["Divination"] > scored["Rhystic Study"]


# ---------------------------------------------------------------------------
# The flag — default OFF, and OFF must be byte-identical
# ---------------------------------------------------------------------------

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(cs.CARD_SCORE_ENV_VAR, raising=False)
    assert cs.is_enabled() is False
    assert cs.CARD_SCORE_ENV_VAR == "COMMANDER_BUILDER_CARD_SCORE"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("0", False), ("", False), ("no", False), ("maybe", False),
])
def test_flag_truthy_values_match_the_rest_of_the_codebase(
    monkeypatch, value, expected,
):
    monkeypatch.setenv(cs.CARD_SCORE_ENV_VAR, value)
    assert cs.is_enabled() is expected


def test_conftest_isolates_the_card_score_flag():
    """No ``delenv`` here on purpose: the conftest autouse fixture
    (``_isolate_card_score_flag``) must already have stripped the flag
    before this test body runs. This test FAILS if an operator shell's
    ``COMMANDER_BUILDER_CARD_SCORE=1`` export leaks into the suite —
    the exact leak the fixture exists to stop (the tier-3 workflow has
    the operator export the flag, and ``is_enabled`` reads the
    environment at call time). Driven end to end by
    ``test_flag_isolation_survives_an_exported_operator_shell``."""
    assert cs.CARD_SCORE_ENV_VAR not in os.environ
    assert cs.is_enabled() is False


def test_setenv_in_a_test_still_beats_the_autouse_delenv(monkeypatch):
    """Referenced by the conftest fixture's docstring: opt-in tests
    ``setenv`` inside the test body (or a non-autouse fixture), which
    runs AFTER the autouse ``delenv`` at fixture setup — so the opt-in
    always wins and the flag-on tests keep working."""
    monkeypatch.setenv(cs.CARD_SCORE_ENV_VAR, "1")
    assert cs.is_enabled() is True


@pytest.mark.slow
def test_flag_isolation_survives_an_exported_operator_shell():
    """End-to-end proof of the conftest fixture: run the probe test
    (``test_conftest_isolates_the_card_score_flag``) in a child pytest
    whose environment carries the operator export. Without the autouse
    ``delenv`` the probe fails, so this run passing IS the isolation.
    Slow lane: one child pytest startup (~5-10s)."""
    env = dict(os.environ)
    env["COMMANDER_BUILDER_CARD_SCORE"] = "1"
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_card_score.py::"
         "test_conftest_isolates_the_card_score_flag"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"probe failed under an exported flag — the conftest autouse "
        f"delenv is not isolating the suite\n{proc.stdout}\n{proc.stderr}"
    )


def _advisor_page() -> CommanderPage:
    return CommanderPage(
        commander_name="Talrand, Sky Summoner",
        slug="talrand-sky-summoner",
        fetched_at="2026-07-24T00:00:00",
        top_cards=[
            CardEntry(name="Worn Powerstone", inclusion_pct=35.0),
            CardEntry(name="Sol Ring", inclusion_pct=71.0),
            CardEntry(name="Divination", inclusion_pct=40.0),
        ],
        high_synergy_cards=[
            CardEntry(name="Rhystic Study", inclusion_pct=45.0,
                      synergy_pct=30.0),
        ],
    )


# The ordering the advisor produced BEFORE FP-015: high-synergy bucket
# first, then top_cards in page order. Recorded literally so this test
# fails if the flag-off path ever drifts, not just if it disagrees with
# whatever the current code happens to do.
_PRE_FP015_ADD_ORDER = [
    "Rhystic Study", "Worn Powerstone", "Divination",
]


def test_flag_off_ordering_is_identical_to_the_pre_change_ordering(
    monkeypatch,
):
    """The default path must be the bucket-order ranking, unchanged.

    FP-014 is explicitly skeptical of static power heuristics and
    FP-002 found no pre-sim feature predicts curation margin, so
    CardScore must not become the default until the tier-3 A/B sim
    reads positive.
    """
    monkeypatch.delenv(cs.CARD_SCORE_ENV_VAR, raising=False)
    deck = {"Island", "Counterspell"}
    recs = _heuristic_swap_recommendations(deck, _advisor_page())
    adds = [r.card for r in recs if r.action == "add"]
    assert adds == _PRE_FP015_ADD_ORDER
    # ...and no card_score payload is attached when the flag is off.
    assert all("card_score" not in r.evidence for r in recs)


def test_flag_off_ordering_is_identical_with_a_diagnosis_rerank(
    monkeypatch,
):
    """The role-priority + trending rerank must also be untouched."""
    monkeypatch.delenv(cs.CARD_SCORE_ENV_VAR, raising=False)
    deck = {"Island"}
    page = _advisor_page()
    baseline = [r.card for r in
                _heuristic_swap_recommendations(deck, page,
                                                trending={"divination"})
                if r.action == "add"]
    assert baseline[0] == "Divination"    # trending floats up, as before


def test_flag_on_reranks_adds_by_score_and_attaches_the_breakdown(
    monkeypatch,
):
    """Wiring check: with the flag on the score becomes the primary sort
    key and the breakdown lands in ``evidence`` (no schema change)."""
    monkeypatch.setenv(cs.CARD_SCORE_ENV_VAR, "1")
    monkeypatch.setattr(
        "commander_builder._advisor_heuristic._cached_scryfall", _lookup,
    )
    deck = {"Island", "Counterspell"}
    recs = _heuristic_swap_recommendations(deck, _advisor_page())
    adds = [r for r in recs if r.action == "add"]
    names = [r.card for r in adds]
    assert names != _PRE_FP015_ADD_ORDER
    # Sol Ring is filtered as a universal staple; among what remains the
    # ordering is monotone in the attached score.
    totals = [r.evidence["card_score"]["total"] for r in adds]
    assert totals == sorted(totals, reverse=True)
    assert adds[0].evidence["card_score"]["kind"] == "ranking_prior"


def test_flag_on_cut_ordering_is_scored_not_alphabetical(monkeypatch):
    """The second seam: the alphabetical cut walk becomes a cut-score
    walk, still deterministic, still guard-railed."""
    monkeypatch.setenv(cs.CARD_SCORE_ENV_VAR, "1")
    monkeypatch.setattr(
        "commander_builder._advisor_heuristic._cached_scryfall", _lookup,
    )
    page = _advisor_page()
    # 60+ known cards so the MIN_EDHREC_SIGNAL_FOR_CUTS floor is cleared.
    page.category_cards = {"Creatures": [f"Known {i}" for i in range(60)]}
    deck = {"Divination", "Worn Powerstone", "Counterspell",
            "Craterhoof Behemoth"} | {"Island"}
    recs = _heuristic_swap_recommendations(deck, page)
    cuts = [r.card for r in recs if r.action == "cut"]
    if cuts:                      # only asserts ordering when cuts exist
        assert cuts != sorted(cuts) or len(cuts) <= 1


def test_flag_on_cut_ordering_emits_nothing_when_every_card_is_guarded(
    monkeypatch,
):
    """An empty guard-railed order is a real answer, not a failure.

    Falling back to the alphabetical walk when ``cut_order`` comes back
    empty would hand that loop exactly the cards the guard rails just
    refused to cut, which is the one outcome FP-015 promises can't
    happen.
    """
    monkeypatch.setenv(cs.CARD_SCORE_ENV_VAR, "1")
    monkeypatch.setattr(
        "commander_builder._advisor_heuristic._cached_scryfall", _lookup,
    )
    from commander_builder import _advisor_heuristic as ah
    ordered = ah._card_score_cut_order(
        {"Divination"}, _advisor_page(), None, ["Divination"],
    )
    # Divination is the deck's only draw spell, so the role floor guards
    # it and the scored order is legitimately empty.
    assert ordered == []


def test_scoring_never_raises_on_a_degenerate_deck():
    """A ranking loop that throws on 1 of 200 candidates is worse than a
    coarse ordering — the module's never-raise contract."""
    for deck in ([], ["???"], ["Island"] * 200):
        ctx = _ctx(deck)
        assert 0.0 <= cs.score_card("Sol Ring", ctx).total <= 100.0
        assert cs.cut_candidates(ctx) is not None


def test_env_var_is_not_read_at_import_time():
    """``is_enabled`` must consult the environment per-call so a test (or
    a long-lived web process) can toggle it without a reimport."""
    os.environ.pop(cs.CARD_SCORE_ENV_VAR, None)
    assert cs.is_enabled() is False
    os.environ[cs.CARD_SCORE_ENV_VAR] = "yes"
    try:
        assert cs.is_enabled() is True
    finally:
        os.environ.pop(cs.CARD_SCORE_ENV_VAR, None)


# --- P3 (OPTIMIZATION_AUDIT 2026-07-25): offline-contract regression -------
# Cut scoring must NEVER reach staples' module-level lookup_card — the
# context's injected lookup is the only sanctioned card source. Before
# this fix, _role_report_minus and _role_target_for's tutor fallback
# both routed through staples.count_deck_roles -> staples.lookup_card,
# costing live HTTP per cut candidate on a cold Scryfall cache.

def _offline_ctx():
    cards = {
        "llanowar elves": {"name": "Llanowar Elves",
                           "type_line": "Creature — Elf Druid",
                           "oracle_text": "{T}: Add {G}.",
                           "cmc": 1.0, "color_identity": ["G"],
                           "legalities": {"commander": "legal"}},
        "demonic tutor": {"name": "Demonic Tutor",
                          "type_line": "Sorcery",
                          "oracle_text": "Search your library for a card, "
                                         "put that card into your hand, "
                                         "then shuffle.",
                          "cmc": 2.0, "color_identity": ["B"],
                          "legalities": {"commander": "legal"}},
        "test cmdr": {"name": "Test Cmdr",
                      "type_line": "Legendary Creature — Human",
                      "oracle_text": "", "cmc": 4.0,
                      "color_identity": ["B", "G"],
                      "legalities": {"commander": "legal"}},
    }
    return cs.deck_context(
        commander_names=["Test Cmdr"],
        deck_cards=["Llanowar Elves", "Demonic Tutor"],
        bracket=3,
        lookup=lambda n: cards.get(n.strip().lower()),
        salt_scores={},
        combos=[],
        game_changers=[],
    )


def test_cut_scoring_never_touches_module_level_lookup(monkeypatch):
    import commander_builder.staples as staples

    def _forbidden(name):  # pragma: no cover - the assertion IS the test
        raise AssertionError(
            f"staples.lookup_card({name!r}) called from the scoring path "
            "— the offline contract requires the ctx-injected lookup")

    monkeypatch.setattr(staples, "lookup_card", _forbidden)
    ctx = _offline_ctx()
    # Both cut candidates exercise _role_report_minus; Demonic Tutor's
    # role has a saturation ceiling but no target, which exercises the
    # _role_target_for fallback recount.
    for name in ("Llanowar Elves", "Demonic Tutor"):
        cs.cut_score(name, ctx)
    cs.score_card("Llanowar Elves", ctx)


def test_deck_role_counts_matches_count_deck_roles():
    import commander_builder.staples as staples
    ctx = _offline_ctx()
    expected = staples.count_deck_roles.__wrapped__(  # type: ignore[attr-defined]
        list(ctx.deck_cards)) if hasattr(
        staples.count_deck_roles, "__wrapped__") else None
    # Equivalence via the shared pure bucket function instead: each
    # deck card's bucket must match staples.role_bucket on the same
    # oracle/type text the ctx serves.
    for name in ctx.deck_cards:
        card = ctx.card(name)
        assert ctx.deck_role_counts[staples.role_bucket(
            card.get("oracle_text") or "", card.get("type_line") or "",
        )] >= 1


# --- Quantity-collapse regression (2026-07-26) -----------------------------
# The advisor seam built its scoring context from the NAME SET only, so
# DeckContext synthesized ``1 <name>`` per name and every text-derived
# derivation saw a stacked ``37 Island`` line as 1 Island: the Karsten
# source counts read ~8 lands, ``mana_fit`` reported every color
# massively short (inflating every mana producer by up to the full
# component weight, with false "N source(s) short" evidence), and
# ``effective_lands`` fired the 33-land cut floor on EVERY land. These
# tests pin the plumbed-through quantities at each seam.

_STACKED_DECK_TEXT = (
    "[metadata]\n"
    "[Commander]\n"
    "1 Talrand, Sky Summoner\n"
    "[Main]\n"
    "37 Island\n"
    "1 Rhystic Study\n"
    "1 Divination\n"
    "1 Counterspell\n"
)
_STACKED_DECK_CARDS = {"Island", "Rhystic Study", "Divination",
                       "Counterspell"}


def _stacked_ctx() -> cs.DeckContext:
    """A context built the way bubble_analysis builds one: real text."""
    return cs.deck_context(deck_text=_STACKED_DECK_TEXT, lookup=_lookup,
                           combos=[], salt_scores={}, game_changers=[])


def test_advisor_context_manabase_sees_stacked_basic_quantities(
    monkeypatch,
):
    """The advisor seam with the real ``.dck`` text plumbed through must
    count all 37 Islands as U sources, not the 1 the synthesized 1x
    blob credited."""
    from commander_builder import _advisor_heuristic as ah
    monkeypatch.setattr(
        "commander_builder._advisor_heuristic._cached_scryfall", _lookup,
    )
    ctx = ah._card_score_context(
        _STACKED_DECK_CARDS, _advisor_page(), None,
        deck_text=_STACKED_DECK_TEXT,
    )
    report = ctx.manabase
    assert report is not None
    assert report["per_color"]["U"]["sources"] == 37


def test_mana_fit_reports_no_deficit_for_a_properly_built_manabase(
    monkeypatch,
):
    """37 Islands in a mono-U deck meets every Karsten target, so a mana
    producer must score 0.0 with "already at target" — never a false
    "U is N source(s) short" evidence string."""
    from commander_builder import _advisor_heuristic as ah
    monkeypatch.setattr(
        "commander_builder._advisor_heuristic._cached_scryfall", _lookup,
    )
    ctx = ah._card_score_context(
        _STACKED_DECK_CARDS, _advisor_page(), None,
        deck_text=_STACKED_DECK_TEXT,
    )
    f, detail = cs._f_mana_fit("Island", ctx)
    assert f == 0.0
    assert "short" not in detail
    assert "already at target" in detail


def test_effective_lands_counts_stacked_quantities():
    """``37 Island`` is 37 effective lands, and the 33-land cut floor
    must NOT fire on a deck with headroom (the collapsed count blocked
    every land cut with false "drops the deck to N" evidence)."""
    ctx = _stacked_ctx()
    assert ctx.effective_lands == pytest.approx(37.0)
    assert not cs.cut_score("Island", ctx).blocked


def test_without_preserves_stacked_quantities():
    """A ``without()`` child inherits the parent's real text minus the
    card's line(s) — re-synthesizing 1x-per-name would poison every cut
    score's manabase math the same way the advisor seam was."""
    ctx = _stacked_ctx()
    sub = ctx.without("Divination")
    assert "37 Island" in sub.deck_text
    assert "Divination" not in sub.deck_text
    assert sub.effective_lands == pytest.approx(37.0)
    assert sub.manabase is not None
    assert sub.manabase["per_color"]["U"]["sources"] == 37


def test_flag_off_path_is_untouched_by_deck_text(monkeypatch):
    """Flag off, ``deck_text`` supplied: ordering stays byte-identical to
    the pre-FP-015 bucket order and no score payload appears."""
    monkeypatch.delenv(cs.CARD_SCORE_ENV_VAR, raising=False)
    recs = _heuristic_swap_recommendations(
        {"Island", "Counterspell"}, _advisor_page(),
        deck_text=_STACKED_DECK_TEXT,
    )
    adds = [r.card for r in recs if r.action == "add"]
    assert adds == _PRE_FP015_ADD_ORDER
    assert all("card_score" not in r.evidence for r in recs)


def test_real_deck_text_context_matches_direct_manabase_report():
    """The bubble_analysis path (``deck_context(deck_text=...)``) must
    keep producing exactly what ``manabase_report`` itself says — the
    fix adds quantity plumbing for name-set callers, it must not perturb
    callers that already passed real text."""
    from commander_builder.deck_builder_manabase import manabase_report
    ctx = _stacked_ctx()
    assert ctx.manabase == manabase_report(_STACKED_DECK_TEXT,
                                           lookup=ctx.card)


# ---------------------------------------------------------------------------
# FP-019.4 -- primer-derived modifiers (heuristics §3/§5)
# ---------------------------------------------------------------------------

_PRIMER_MOD_CARDS = {
    "banner of the general": {
        "name": "Banner of the General", "type_line": "Enchantment",
        "oracle_text": "As long as you control your commander, creatures "
                       "you control get +2/+2.",
        "color_identity": ["W"], "cmc": 3.0,
        "legalities": {"commander": "legal"},
    },
    "slow banner": {
        "name": "Slow Banner", "type_line": "Enchantment",
        "oracle_text": "At the beginning of your upkeep, create a 1/1 "
                       "white Soldier creature token.",
        "color_identity": ["W"], "cmc": 4.0,
        "legalities": {"commander": "legal"},
    },
    "patient scribe": {
        "name": "Patient Scribe", "type_line": "Creature — Human",
        "oracle_text": "Whenever another creature dies, draw a card. This "
                       "ability triggers only once each turn.",
        "color_identity": ["U"], "cmc": 3.0,
        "legalities": {"commander": "legal"},
    },
    "grim tutor's map": {
        "name": "Grim Tutor's Map", "type_line": "Instant",
        "oracle_text": "Search your library for a card, then shuffle and "
                       "put that card on top of your library.",
        "color_identity": ["B"], "cmc": 1.0,
        "legalities": {"commander": "legal"},
    },
    "plain sorcery": {
        "name": "Plain Sorcery", "type_line": "Sorcery",
        "oracle_text": "Draw two cards.",
        "color_identity": ["U"], "cmc": 3.0,
        "legalities": {"commander": "legal"},
    },
}


def _primer_lookup(name):
    return _PRIMER_MOD_CARDS.get(name.lower()) or _lookup(name)


def _primer_ctx(**kwargs):
    kwargs.setdefault("lookup", _primer_lookup)
    return _ctx(_blue_deck(), **kwargs)


def test_mod_commander_dependence_penalizes_commander_gated_card():
    ctx = _primer_ctx()
    mod = cs._mod_commander_dependence("Banner of the General", ctx)
    assert mod is not None
    assert mod.points == cs.CARD_SCORE_MODIFIERS["commander_dependence"]
    assert mod.points < 0


def test_mod_commander_dependence_ignores_standalone_card():
    ctx = _primer_ctx()
    assert cs._mod_commander_dependence("Plain Sorcery", ctx) is None


def test_mod_tempo_fail_fires_only_for_aggro_context():
    aggro = _primer_ctx(archetype="aggro")
    other = _primer_ctx()
    assert cs._mod_tempo_fail("Slow Banner", aggro) is not None
    assert cs._mod_tempo_fail("Slow Banner", other) is None
    # immediate-value cards are safe even in aggro
    assert cs._mod_tempo_fail("Plain Sorcery", aggro) is None


def test_mod_capped_engine_penalizes_once_each_turn_draw():
    ctx = _primer_ctx()
    mod = cs._mod_capped_engine("Patient Scribe", ctx)
    assert mod is not None and mod.points < 0
    assert cs._mod_capped_engine("Plain Sorcery", ctx) is None


def test_mod_tutor_top_delta_spares_combo_decks():
    plain = _primer_ctx()
    combo = _primer_ctx(archetype="combo")
    assert cs._mod_tutor_top_delta("Grim Tutor's Map", plain) is not None
    assert cs._mod_tutor_top_delta("Grim Tutor's Map", combo) is None
    # a to-hand tutor keeps its card parity: never penalized
    assert cs._mod_tutor_top_delta("Plain Sorcery", plain) is None


def test_primer_modifiers_are_registered():
    for fn in (cs._mod_commander_dependence, cs._mod_tempo_fail,
               cs._mod_capped_engine, cs._mod_tutor_top_delta):
        assert fn in cs._MODIFIER_FNS
    for key in ("commander_dependence", "tempo_fail", "capped_engine",
                "tutor_top_delta"):
        assert cs.CARD_SCORE_MODIFIERS[key] < 0


def test_primer_modifiers_flow_through_score_card():
    ctx = _primer_ctx()
    scored = cs.score_card("Banner of the General", ctx)
    assert any(m.name == "commander_dependence" for m in scored.modifiers)
