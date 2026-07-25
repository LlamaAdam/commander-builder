"""Tests for bubble_analysis — reference corpus, deck score, bubble cards.

Fully offline: fetchers, scorer, and cutter are injected; the deck
context is a hand-built fake so no Scryfall/EDHREC/Moxfield traffic and
no dependence on the staples regexes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import pytest

from commander_builder import bubble_analysis as ba
from commander_builder.bubble_analysis import (
    BubbleCard,
    ReferenceCorpus,
    build_reference_corpus,
    find_bubble_cards,
    score_deck,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_corpus(n_decks=12, with_card="Rhystic Study",
                without_card="Storm Crow", average=("Sol Ring",),
                salt=None, bracket=None) -> ReferenceCorpus:
    """``with_card`` appears in every reference deck, ``without_card``
    in none of them; filler cards make each deck distinct."""
    decks = []
    for i in range(n_decks):
        decks.append(frozenset({with_card.lower(), f"filler {i}"}))
    display = {with_card.lower(): with_card}
    for name in average:
        display[name.lower()] = name
    return ReferenceCorpus(
        commander="Test Commander",
        bracket=bracket,
        deck_card_keys=tuple(decks),
        display_names=display,
        average_deck_keys=frozenset(n.lower() for n in average),
        salt_map=salt or {},
    )


@dataclass
class FakeCtx:
    """Just the DeckContext surface score_deck/find_bubble_cards read."""

    deck_cards: tuple = ()
    commander_names: tuple = ()
    bracket: Optional[int] = None
    role_report: Optional[dict] = None
    manabase: Optional[dict] = None
    salt_scores: Optional[dict] = None
    protected_keys: frozenset = frozenset()
    cards: dict = field(default_factory=dict)

    def __post_init__(self):
        self.deck_keys = frozenset(c.strip().lower()
                                   for c in self.deck_cards)

    def card(self, name):
        return self.cards.get(name.strip().lower())


@dataclass
class FakeCut:
    card: str
    score: float
    blocked: bool = False
    block_reason: str = ""


@dataclass
class FakeScore:
    total: float


def cutter_from(scores: dict, blocked=()):
    def _cut(name, ctx):
        return FakeCut(card=name, score=scores.get(name, 0.0),
                       blocked=name in blocked)
    return _cut


def scorer_from(scores: dict):
    def _score(name, ctx):
        return FakeScore(total=scores.get(name, 0.0))
    return _score


def moxfield_deck_json(*names: str) -> dict:
    return {"boards": {"mainboard": {"cards": {
        f"id{i}": {"card": {"name": n}} for i, n in enumerate(names)
    }}}}


# ---------------------------------------------------------------------------
# ReferenceCorpus
# ---------------------------------------------------------------------------

def test_support_is_fraction_of_reference_decks():
    corpus = make_corpus(n_decks=12)
    assert corpus.support("Rhystic Study") == 1.0
    assert corpus.support("Storm Crow") == 0.0


def test_support_is_none_below_min_reference_decks():
    corpus = make_corpus(n_decks=ba.MIN_REFERENCE_DECKS - 1)
    assert corpus.support("Rhystic Study") is None


def test_support_is_case_insensitive():
    corpus = make_corpus(n_decks=12)
    assert corpus.support("RHYSTIC STUDY") == 1.0


def test_in_average_deck():
    corpus = make_corpus()
    assert corpus.in_average_deck("Sol Ring")
    assert not corpus.in_average_deck("Storm Crow")


def test_replacement_pool_excludes_deck_and_sorts_by_support():
    decks = [frozenset({"a", "b"}) for _ in range(10)]
    decks += [frozenset({"a", "c"}) for _ in range(2)]
    corpus = ReferenceCorpus(
        commander="X", bracket=None, deck_card_keys=tuple(decks),
        display_names={"a": "A", "b": "B", "c": "C"})
    pool = corpus.replacement_pool(exclude_keys=frozenset({"a"}),
                                   min_support=0.1)
    assert pool == ["B", "C"]  # b support 10/12 > c support 2/12


def test_replacement_pool_falls_back_to_average_when_thin():
    corpus = make_corpus(n_decks=3, average=("Sol Ring", "Arcane Signet"))
    pool = corpus.replacement_pool(exclude_keys=frozenset({"sol ring"}))
    assert pool == ["Arcane Signet"]


def test_corpus_dict_round_trip():
    corpus = make_corpus(n_decks=12, salt={"rhystic study": 3.1})
    clone = ReferenceCorpus.from_dict(corpus.to_dict())
    assert clone.n_decks == corpus.n_decks
    assert clone.support("Rhystic Study") == 1.0
    assert clone.salt_map["rhystic study"] == pytest.approx(3.1)
    assert clone.in_average_deck("Sol Ring")


# ---------------------------------------------------------------------------
# build_reference_corpus
# ---------------------------------------------------------------------------

@pytest.fixture
def ref_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "_ref_cache_dir", lambda: tmp_path)
    return tmp_path


def test_build_corpus_from_injected_fetchers(ref_cache):
    corpus = build_reference_corpus(
        "Krenko, Mob Boss",
        fetch_decks=lambda c, b, n: [
            moxfield_deck_json("Goblin King", "Sol Ring"),
            moxfield_deck_json("Goblin King", "Skirk Prospector"),
        ],
        fetch_average=lambda c: ["Impact Tremors"],
        fetch_salt=lambda: {"Rhystic Study": 3.2},
        fetch_extra_lists=lambda c, b, n: [],
    )
    assert corpus is not None
    assert corpus.n_decks == 2
    assert corpus.display_names["goblin king"] == "Goblin King"
    assert corpus.in_average_deck("Impact Tremors")
    assert corpus.salt_map["rhystic study"] == pytest.approx(3.2)


def test_build_corpus_returns_none_when_all_sources_empty(ref_cache):
    corpus = build_reference_corpus(
        "Nobody",
        fetch_decks=lambda c, b, n: [],
        fetch_average=lambda c: [],
        fetch_salt=lambda: {},
                  fetch_extra_lists=lambda c, b, n: [],
    )
    assert corpus is None


def test_build_corpus_caches_and_reuses(ref_cache):
    calls = {"n": 0}

    def fetch(c, b, n):
        calls["n"] += 1
        return [moxfield_deck_json("Sol Ring")]

    kwargs = dict(fetch_decks=fetch, fetch_average=lambda c: [],
                  fetch_salt=lambda: {},
                  fetch_extra_lists=lambda c, b, n: [])
    first = build_reference_corpus("Krenko, Mob Boss", **kwargs)
    second = build_reference_corpus("Krenko, Mob Boss", **kwargs)
    assert calls["n"] == 1
    assert second is not None
    assert second.n_decks == first.n_decks


def test_build_corpus_refetches_on_corrupt_cache(ref_cache):
    kwargs = dict(fetch_decks=lambda c, b, n: [moxfield_deck_json("A")],
                  fetch_average=lambda c: [], fetch_salt=lambda: {},
                  fetch_extra_lists=lambda c, b, n: [])
    build_reference_corpus("Krenko, Mob Boss", **kwargs)
    for f in ref_cache.iterdir():
        f.write_text("{not json", encoding="utf-8")
    corpus = build_reference_corpus("Krenko, Mob Boss", **kwargs)
    assert corpus is not None
    assert corpus.n_decks == 1


def test_build_corpus_skips_cache_when_disabled(ref_cache):
    calls = {"n": 0}

    def fetch(c, b, n):
        calls["n"] += 1
        return [moxfield_deck_json("A")]

    kwargs = dict(cache=False, fetch_decks=fetch,
                  fetch_average=lambda c: [], fetch_salt=lambda: {},
                  fetch_extra_lists=lambda c, b, n: [])
    build_reference_corpus("Krenko, Mob Boss", **kwargs)
    build_reference_corpus("Krenko, Mob Boss", **kwargs)
    assert calls["n"] == 2
    assert not list(ref_cache.iterdir())


# ---------------------------------------------------------------------------
# score_deck
# ---------------------------------------------------------------------------

def full_ctx(**overrides) -> FakeCtx:
    """A ctx where every component has healthy inputs."""
    base = dict(
        deck_cards=("Rhystic Study", "Filler 0"),
        bracket=3,
        role_report={"roles": {"ramp": {"target": 10, "deficit": 0},
                               "draw": {"target": 10, "deficit": 0}},
                     "under_built": []},
        manabase={"total_target": 20, "total_deficit": 0,
                  "under_served": []},
        salt_scores={},
    )
    base.update(overrides)
    return FakeCtx(**base)


def test_score_deck_perfect_inputs_verdict_keep():
    corpus = make_corpus(n_decks=12)
    report = score_deck(corpus=corpus, ctx=full_ctx())
    # reference_alignment: Rhystic 1.0 + Filler 0 in 1/12 decks -> high
    assert report.total >= ba.VERDICT_KEEP
    assert report.verdict == "keep"
    assert report.change_budget == (0, 2)
    assert report.n_reference_decks == 12


def test_score_deck_renormalizes_when_component_unavailable():
    corpus = make_corpus(n_decks=12)
    ctx = full_ctx(manabase=None)
    report = score_deck(corpus=corpus, ctx=ctx)
    assert report.components["mana_fit"]["value"] is None
    weights = [c["weight"] for c in report.components.values()
               if c["value"] is not None]
    # components store weights rounded to 3 decimals — allow that slack
    assert sum(weights) == pytest.approx(1.0, abs=0.005)


def test_score_deck_role_deficits_lower_role_fit():
    ctx = full_ctx(role_report={"roles": {"ramp": {"target": 10,
                                                   "deficit": 5}},
                                "under_built": ["ramp"]})
    report = score_deck(corpus=make_corpus(n_decks=12), ctx=ctx)
    assert report.components["role_fit"]["value"] == pytest.approx(0.5)
    assert "ramp" in report.components["role_fit"]["detail"]


def test_score_deck_salt_component_skipped_above_b3():
    ctx = full_ctx(bracket=4)
    report = score_deck(corpus=make_corpus(n_decks=12), ctx=ctx)
    assert report.components["salt_fit"]["value"] is None


def test_score_deck_salty_cards_dock_salt_fit():
    ctx = full_ctx(salt_scores={"rhystic study": 3.0})
    report = score_deck(corpus=make_corpus(n_decks=12), ctx=ctx)
    assert report.components["salt_fit"]["value"] == pytest.approx(
        1.0 - ba._SALT_STEP)
    assert "Rhystic Study" in report.components["salt_fit"]["detail"]


def test_score_deck_structural_deficits_mean_overhaul():
    ctx = full_ctx(
        role_report={"roles": {"ramp": {"target": 10, "deficit": 10}},
                     "under_built": ["ramp"]},
        manabase={"total_target": 20, "total_deficit": 18,
                  "under_served": ["B"]},
    )
    report = score_deck(corpus=None, ctx=ctx)
    assert report.verdict == "overhaul"
    assert report.change_budget == (0, 0)


def test_score_deck_no_measurable_inputs_is_mid_band_not_overhaul():
    ctx = FakeCtx(deck_cards=("A",), bracket=None)
    report = score_deck(corpus=None, ctx=ctx)
    assert report.total == pytest.approx(50.0)
    assert report.verdict == "polish"


def test_score_deck_lands_excluded_from_alignment():
    corpus = make_corpus(n_decks=12)
    ctx = full_ctx(
        deck_cards=("Rhystic Study", "Wastes"),
        cards={"wastes": {"name": "Wastes", "type_line": "Basic Land"}},
    )
    report = score_deck(corpus=corpus, ctx=ctx)
    detail = report.components["reference_alignment"]["detail"]
    assert "1 nonland" in detail


# ---------------------------------------------------------------------------
# find_bubble_cards
# ---------------------------------------------------------------------------

def test_bubble_requires_weak_cut_and_low_support():
    corpus = make_corpus(n_decks=12)
    ctx = FakeCtx(deck_cards=("Rhystic Study", "Storm Crow"))
    cut = cutter_from({"Rhystic Study": 90.0, "Storm Crow": 90.0})
    out = find_bubble_cards(corpus=corpus, ctx=ctx, cutter=cut,
                            scorer=scorer_from({}))
    # Rhystic has 100% support -> community vouches -> not a bubble.
    assert [b.card for b in out] == ["Storm Crow"]
    assert "reference decks run it" in " ".join(out[0].reasons)


def test_bubble_requires_cut_floor():
    corpus = make_corpus(n_decks=12)
    ctx = FakeCtx(deck_cards=("Storm Crow",))
    cut = cutter_from({"Storm Crow": ba.BUBBLE_CUT_FLOOR - 1})
    out = find_bubble_cards(corpus=corpus, ctx=ctx, cutter=cut,
                            scorer=scorer_from({}))
    assert out == []


def test_bubble_skips_blocked_and_protected():
    corpus = make_corpus(n_decks=12)
    ctx = FakeCtx(deck_cards=("Storm Crow", "Craterhoof Behemoth"),
                  protected_keys=frozenset({"craterhoof behemoth"}))
    cut = cutter_from({"Storm Crow": 90.0, "Craterhoof Behemoth": 90.0},
                      blocked={"Storm Crow"})
    out = find_bubble_cards(corpus=corpus, ctx=ctx, cutter=cut,
                            scorer=scorer_from({}))
    assert out == []


def test_bubble_without_corpus_uses_cut_only():
    ctx = FakeCtx(deck_cards=("Storm Crow",))
    out = find_bubble_cards(corpus=None, ctx=ctx,
                            cutter=cutter_from({"Storm Crow": 80.0}),
                            scorer=scorer_from({}))
    assert len(out) == 1
    assert out[0].support is None
    assert "no reference corpus" in " ".join(out[0].reasons)


def test_replacement_needs_margin_over_in_deck_score():
    ctx = FakeCtx(deck_cards=("Storm Crow",))
    # in-deck score = 100 - 80 = 20; margin 10 -> needs >= 30.
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Meh Card", "Good Card"],
        cutter=cutter_from({"Storm Crow": 80.0}),
        scorer=scorer_from({"Meh Card": 25.0, "Good Card": 45.0}))
    assert out[0].replacement["card"] == "Good Card"


def test_replacements_consumed_greedily_not_shared():
    ctx = FakeCtx(deck_cards=("Storm Crow", "Mudhole"))
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Best Card"],
        cutter=cutter_from({"Storm Crow": 90.0, "Mudhole": 85.0}),
        scorer=scorer_from({"Best Card": 95.0}))
    replacements = [b.replacement for b in out]
    assert sum(1 for r in replacements if r) == 1
    assert out[0].replacement["card"] == "Best Card"  # worst card first


def test_gated_candidates_never_offered():
    ctx = FakeCtx(deck_cards=("Storm Crow",))
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Illegal Card"],
        cutter=cutter_from({"Storm Crow": 90.0}),
        scorer=scorer_from({"Illegal Card": 0.0}))
    assert out[0].replacement is None


def test_candidates_already_in_deck_are_excluded():
    ctx = FakeCtx(deck_cards=("Storm Crow", "Sol Ring"))
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Sol Ring"],
        cutter=cutter_from({"Storm Crow": 90.0, "Sol Ring": 0.0}),
        scorer=scorer_from({"Sol Ring": 99.0}))
    assert out[0].replacement is None


def test_max_results_caps_output():
    cards = tuple(f"Bulk {i}" for i in range(15))
    ctx = FakeCtx(deck_cards=cards)
    out = find_bubble_cards(
        corpus=None, ctx=ctx, max_results=4,
        cutter=cutter_from({c: 90.0 for c in cards}),
        scorer=scorer_from({}))
    assert len(out) == 4


def test_ease_orders_replaceable_bubbles_first():
    ctx = FakeCtx(deck_cards=("Storm Crow", "Mudhole"))
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Best Card", "Ok Card"],
        cutter=cutter_from({"Storm Crow": 70.0, "Mudhole": 90.0}),
        scorer=scorer_from({"Best Card": 95.0, "Ok Card": 45.0}))
    # Mudhole (weaker, bigger gap) should rank first with the best add.
    assert out[0].card == "Mudhole"
    assert out[0].ease > out[1].ease


def test_salty_bubble_notes_salt():
    corpus = make_corpus(n_decks=12, salt={"storm crow": 2.5})
    ctx = FakeCtx(deck_cards=("Storm Crow",),
                  salt_scores={"storm crow": 2.5})
    out = find_bubble_cards(corpus=corpus, ctx=ctx,
                            cutter=cutter_from({"Storm Crow": 90.0}),
                            scorer=scorer_from({}))
    assert out[0].salt == pytest.approx(2.5)
    assert any("salt" in r for r in out[0].reasons)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_main_missing_file_exits_2(tmp_path, capsys):
    rc = ba.main([str(tmp_path / "nope.dck")])
    assert rc == 2
    assert "no such deck" in capsys.readouterr().err


def test_main_json_happy_path(tmp_path, monkeypatch, capsys):
    deck = tmp_path / "test.dck"
    deck.write_text("[Commander]\n1 Krenko, Mob Boss\n[Main]\n"
                    "1 Storm Crow\n", encoding="utf-8")
    ctx = FakeCtx(deck_cards=("Storm Crow",), commander_names=())
    monkeypatch.setattr(ba, "deck_context", lambda **kw: ctx)
    monkeypatch.setattr(
        ba, "find_bubble_cards",
        lambda **kw: [BubbleCard(card="Storm Crow", cut_score=90.0,
                                 support=None, salt=0.0, replacement=None,
                                 ease=0.0, reasons=("weak",))])
    rc = ba.main([str(deck), "--no-network", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deck_score"]["verdict"] in {"keep", "polish",
                                                "overhaul"}
    assert payload["bubble_cards"][0]["card"] == "Storm Crow"


# ---------------------------------------------------------------------------
# apply_verdict_to_report (advisor integration)
# ---------------------------------------------------------------------------

from commander_builder._advisor_models import (  # noqa: E402
    AdviceReport,
    SwapRecommendation,
)
from commander_builder.bubble_analysis import (  # noqa: E402
    DeckScoreReport,
    apply_verdict_to_report,
)


def rec(card, action, source=""):
    return SwapRecommendation(card=card, action=action, reason="r",
                              evidence={"source": source} if source else {})


def make_report(*recs) -> AdviceReport:
    return AdviceReport(deck_filename="d.dck", deck_id="d", bracket=3,
                        recommendations=list(recs))


def fake_score(verdict, budget):
    ds = DeckScoreReport(total=80.0, verdict=verdict,
                         change_budget=budget, components={},
                         n_reference_decks=0)
    return lambda **kw: ds


def fake_bubbles(*cards):
    out = [BubbleCard(card=c, cut_score=90.0, support=0.0, salt=0.0,
                      replacement=None, ease=100.0 - i, reasons=())
           for i, c in enumerate(cards)]
    return lambda **kw: out


def test_verdict_keep_trims_adds_and_cuts_to_budget():
    report = make_report(
        rec("Add A", "add"), rec("Add B", "add"), rec("Add C", "add"),
        rec("Cut A", "cut"), rec("Cut B", "cut"), rec("Cut C", "cut"))
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("keep", (0, 2)),
        bubble_fn=fake_bubbles())
    adds = [r.card for r in out.recommendations if r.action == "add"]
    cuts = [r.card for r in out.recommendations if r.action == "cut"]
    assert adds == ["Add A", "Add B"]   # advisor order preserved
    assert len(cuts) == 2
    assert out.deck_score["verdict"] == "keep"


def test_verdict_bubble_cuts_lead_the_cut_list():
    report = make_report(rec("Cut A", "cut"), rec("Cut B", "cut"),
                         rec("Cut C", "cut"))
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("polish", (2, 2)),
        bubble_fn=fake_bubbles("Cut C", "Cut B"))
    cuts = [r.card for r in out.recommendations if r.action == "cut"]
    assert cuts == ["Cut C", "Cut B"]   # bubble order wins, budget caps


def test_verdict_essentials_survive_the_trim():
    report = make_report(
        rec("Command Tower", "add", source="manabase_essentials"),
        rec("Add A", "add"), rec("Add B", "add"))
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("keep", (0, 1)),
        bubble_fn=fake_bubbles())
    cards = [r.card for r in out.recommendations]
    assert "Command Tower" in cards
    assert cards.count("Add A") + cards.count("Add B") == 1


def test_verdict_overhaul_trims_nothing():
    report = make_report(*(rec(f"Add {i}", "add") for i in range(8)))
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("overhaul", (0, 0)),
        bubble_fn=fake_bubbles())
    assert len(out.recommendations) == 8
    assert out.deck_score["verdict"] == "overhaul"


def test_verdict_returns_new_report_and_never_mutates_input():
    report = make_report(rec("Add A", "add"), rec("Add B", "add"),
                         rec("Add C", "add"))
    before = [r.card for r in report.recommendations]
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("keep", (0, 1)),
        bubble_fn=fake_bubbles())
    assert out is not report
    assert [r.card for r in report.recommendations] == before
    assert report.deck_score is None


def test_verdict_attaches_bubble_dicts():
    report = make_report()
    out = apply_verdict_to_report(
        report, ctx=FakeCtx(), score_fn=fake_score("keep", (0, 2)),
        bubble_fn=fake_bubbles("Storm Crow"))
    assert out.bubble_cards[0]["card"] == "Storm Crow"


def test_land_candidates_never_offered_as_replacements():
    ctx = FakeCtx(deck_cards=("Storm Crow",),
                  cards={"temple garden": {"name": "Temple Garden",
                                           "type_line": "Land - Forest Plains"}})
    out = find_bubble_cards(
        corpus=None, ctx=ctx, candidates=["Temple Garden"],
        cutter=cutter_from({"Storm Crow": 90.0}),
        scorer=scorer_from({"Temple Garden": 99.0}))
    assert out[0].replacement is None


# ---------------------------------------------------------------------------
# Archidekt merge (third corpus source)
# ---------------------------------------------------------------------------

def test_build_corpus_merges_extra_lists(ref_cache):
    corpus = build_reference_corpus(
        "Krenko, Mob Boss",
        fetch_decks=lambda c, b, n: [moxfield_deck_json("Sol Ring")],
        fetch_average=lambda c: [],
        fetch_salt=lambda: {},
        fetch_extra_lists=lambda c, b, n: [["Skirk Prospector", "Sol Ring"],
                                           ["Impact Tremors"]],
    )
    assert corpus.n_decks == 3  # 1 moxfield + 2 archidekt
    assert corpus.display_names["skirk prospector"] == "Skirk Prospector"


def test_build_corpus_extra_lists_alone_suffice(ref_cache):
    corpus = build_reference_corpus(
        "Krenko, Mob Boss",
        fetch_decks=lambda c, b, n: [],
        fetch_average=lambda c: [],
        fetch_salt=lambda: {},
        fetch_extra_lists=lambda c, b, n: [["Sol Ring"]],
    )
    assert corpus is not None
    assert corpus.n_decks == 1


def test_build_corpus_extra_budget_capped():
    seen = {}

    def extra(c, b, n):
        seen["n"] = n
        return []

    import commander_builder.archidekt_client as ac
    build_reference_corpus(
        "X", n=50, cache=False,
        fetch_decks=lambda c, b, n: [moxfield_deck_json("A")],
        fetch_average=lambda c: [], fetch_salt=lambda: {},
        fetch_extra_lists=extra,
    )
    assert seen["n"] == ac.DEFAULT_N  # never the full Moxfield budget
