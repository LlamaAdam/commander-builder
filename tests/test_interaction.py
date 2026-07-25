"""Tests for the interaction coverage matrix.

The module answers a question ``ROLE_TARGETS`` cannot: not "how many
answers does this deck have" but "which of the things that beat me can
it answer, and at what speed". These tests cover both classifiers:

  * the ORACLE-REGEX fallback (no Forge corpus), driven by real Scryfall
    oracle text through an injected ``lookup``;
  * the FORGE CARD SCRIPT path, driven by a tiny on-disk cards corpus
    built in ``tmp_path`` and read through a real ``CardsLoader`` — the
    same loader ``deck_library_analyzer`` uses in production.

Everything is offline. Oracle text below is quoted from Scryfall
(scryfall.com/search?q=!"<card name>") so the regexes are exercised
against real templating, not against text written to make them pass.
"""
from __future__ import annotations

import pytest

from commander_builder import interaction
from commander_builder.forge_cards_loader import CardsLoader
from commander_builder.forge_script_parser import parse_card_script
from commander_builder.interaction import (
    BRACKET_INTERACTION_MINIMUMS,
    INTERACTION_CATEGORIES,
    classify_interaction,
    interaction_report,
    is_instant_speed,
    minimums_for_bracket,
    oracle_categories,
)


# --- Real oracle text, by card -------------------------------------------

_ORACLES: dict[str, tuple[str, str]] = {
    "Swords to Plowshares": (
        "Exile target creature. Its controller gains life equal to its power.",
        "Instant",
    ),
    "Go for the Throat": (
        "Destroy target nonartifact creature.", "Instant",
    ),
    "Beast Within": (
        "Destroy target permanent. Its controller creates a 3/3 green "
        "Beast creature token.",
        "Instant",
    ),
    "Putrefy": (
        "Destroy target artifact or creature. It can't be regenerated.",
        "Instant",
    ),
    "Naturalize": (
        "Destroy target artifact or enchantment.", "Instant",
    ),
    "Vandalblast": (
        "Destroy target artifact you don't control.\n"
        "Overload {4}{R}",
        "Sorcery",
    ),
    "Bojuka Bog": (
        "Bojuka Bog enters tapped.\n"
        "When Bojuka Bog enters, exile target player's graveyard.\n"
        "{T}: Add {B}.",
        "Land",
    ),
    "Rest in Peace": (
        "When Rest in Peace enters, exile all cards from all graveyards.\n"
        "If a card or token would be put into a graveyard from anywhere, "
        "exile it instead.",
        "Enchantment",
    ),
    "Counterspell": ("Counter target spell.", "Instant"),
    "Wrath of God": (
        "Destroy all creatures. They can't be regenerated.", "Sorcery",
    ),
    "Blasphemous Act": (
        "This spell costs {1} less to cast for each creature on the "
        "battlefield.\nBlasphemous Act deals 13 damage to each creature.",
        "Sorcery",
    ),
    # --- non-interaction controls ---------------------------------------
    "Cultivate": (
        "Search your library for up to two basic land cards, reveal those "
        "cards, put one onto the battlefield tapped and the other into "
        "your hand, then shuffle.",
        "Sorcery",
    ),
    "Sol Ring": ("{T}: Add {C}{C}.", "Artifact"),
    "Grizzly Bears": ("", "Creature — Bear"),
    "Krenko, Mob Boss": (
        "{T}: Create X 1/1 red Goblin creature tokens, where X is the "
        "number of Goblins you control.",
        "Legendary Creature — Goblin Warrior",
    ),
    # Escape/flashback control: exiling YOUR OWN graveyard as a cost is
    # not graveyard hate.
    "Rise of the Dread Marn": (
        "Escape—{1}{B}, Exile four other cards from your graveyard.",
        "Instant",
    ),
}


def _lookup(name: str):
    entry = _ORACLES.get(name.strip())
    if entry is None:
        return None
    oracle_text, type_line = entry
    return {"name": name, "oracle_text": oracle_text, "type_line": type_line}


def _deck(*names: str, commander: str = "Krenko, Mob Boss") -> str:
    lines = "".join(f"1 {n}\n" for n in names)
    return f"[Commander]\n1 {commander}\n[Main]\n{lines}"


def _report(*names: str, bracket: int = 3, **kwargs):
    return interaction_report(
        _deck(*names), bracket=bracket, lookup=_lookup, use_forge=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The tuning dict
# ---------------------------------------------------------------------------

def test_every_bracket_defines_every_category():
    """A missing row would silently read as 'minimum 0' — i.e. the deck
    is never told about that gap. Pin the shape."""
    for bracket, minimums in BRACKET_INTERACTION_MINIMUMS.items():
        assert 1 <= bracket <= 5
        for category in INTERACTION_CATEGORIES:
            assert category in minimums, (bracket, category)
        assert 0.0 <= minimums["instant_speed_share"] <= 1.0


def test_minimums_tighten_with_bracket():
    """The reference column is bracket 3 (REVIEW.md's table). Higher
    brackets demand more of the rows that decide fast games."""
    assert minimums_for_bracket(3)["creature_removal"] == 4
    assert minimums_for_bracket(3)["artifact_enchantment"] == 2
    assert minimums_for_bracket(3)["instant_speed_share"] == 0.40
    # Stack interaction is optional up to B3 and mandatory past it.
    assert minimums_for_bracket(2)["stack"] == 0
    assert minimums_for_bracket(4)["stack"] > 0
    # Instant-speed share climbs monotonically with bracket.
    shares = [
        minimums_for_bracket(b)["instant_speed_share"] for b in range(1, 6)
    ]
    assert shares == sorted(shares)


def test_bracket_clamps_instead_of_raising():
    """A junk ``?bracket=`` query param must not cost the whole signal."""
    assert minimums_for_bracket(0) == minimums_for_bracket(1)
    assert minimums_for_bracket(9) == minimums_for_bracket(5)
    assert minimums_for_bracket(None) == minimums_for_bracket(3)
    assert minimums_for_bracket("junk") == minimums_for_bracket(3)


# ---------------------------------------------------------------------------
# Oracle-regex classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("card,expected", [
    ("Swords to Plowshares", {"creature_removal"}),
    ("Go for the Throat", {"creature_removal"}),
    ("Naturalize", {"artifact_enchantment"}),
    ("Counterspell", {"stack"}),
    ("Wrath of God", {"board_wipe"}),
    ("Blasphemous Act", {"board_wipe"}),
    ("Bojuka Bog", {"graveyard_hate"}),
    ("Rest in Peace", {"graveyard_hate"}),
    # "Destroy target permanent" genuinely answers both — the matrix
    # would lie if a catch-all had to pick one row.
    ("Beast Within", {"creature_removal", "artifact_enchantment"}),
    ("Putrefy", {"creature_removal", "artifact_enchantment"}),
])
def test_oracle_classifier_buckets_real_cards(card, expected):
    oracle_text, type_line = _ORACLES[card]
    assert oracle_categories(oracle_text, type_line) == expected


@pytest.mark.parametrize("card", [
    "Cultivate", "Sol Ring", "Grizzly Bears", "Krenko, Mob Boss",
    "Rise of the Dread Marn",
])
def test_oracle_classifier_ignores_non_interaction(card):
    """Ramp, mana rocks, vanilla creatures and an escape COST that exiles
    your OWN graveyard are not answers. The escape case matters: counting
    it would tell a graveyard deck it was defended against graveyards."""
    oracle_text, type_line = _ORACLES[card]
    assert oracle_categories(oracle_text, type_line) == set()


def test_negated_type_words_do_not_create_false_coverage():
    """'nonartifact creature' is not an artifact answer and 'noncreature
    permanent' is not creature removal. Both are one missing ``\\b`` away
    from papering over exactly the gap this module exists to find."""
    assert oracle_categories("Destroy target nonartifact creature.", "Instant") \
        == {"creature_removal"}
    assert oracle_categories(
        "Destroy target noncreature permanent.", "Instant",
    ) == {"artifact_enchantment"}


def test_instant_speed_detection():
    assert is_instant_speed("", "Instant") is True
    assert is_instant_speed("Flash\nDestroy target creature.", "Creature") is True
    assert is_instant_speed("Destroy all creatures.", "Sorcery") is False


# ---------------------------------------------------------------------------
# The report — the actual coverage matrix
# ---------------------------------------------------------------------------

def test_deck_passing_removal_target_can_still_fail_coverage():
    """THE motivating case. Eight creature-removal spells clears
    ``ROLE_TARGETS['removal'] == 8`` outright — and this deck still has
    no answer to an artifact, a graveyard, or the stack. The count said
    'perfect'; the matrix says what actually loses the game."""
    report = _report(*["Go for the Throat"] * 4, *["Swords to Plowshares"] * 4)
    assert report["categories"]["creature_removal"]["count"] == 8
    assert report["categories"]["creature_removal"]["gap"] == 0
    assert report["categories"]["artifact_enchantment"]["count"] == 0
    assert report["categories"]["artifact_enchantment"]["gap"] == 2
    assert report["categories"]["graveyard_hate"]["gap"] == 1
    assert report["categories"]["board_wipe"]["gap"] == 2
    gaps = " ".join(report["gaps"])
    assert "Artifact/enchantment answers: 0 (bracket 3 wants 2)" in gaps
    # Actionable, not a restatement of the number.
    assert "Smothering Tithe" in gaps


def test_covered_deck_reports_no_gaps():
    report = _report(
        "Swords to Plowshares", "Go for the Throat", "Beast Within",
        "Putrefy", "Naturalize", "Bojuka Bog", "Counterspell",
        "Wrath of God", "Blasphemous Act",
    )
    assert report["gaps"] == []
    assert all(
        report["categories"][c]["gap"] == 0 for c in INTERACTION_CATEGORIES
    )


def test_report_lists_the_cards_behind_each_row():
    """The card list is how a false positive stays visible — the same
    disclosure convention the deck_health tiles use."""
    report = _report("Naturalize", "Beast Within", "Cultivate")
    # Deck order, one entry per distinct card (not per copy).
    assert report["categories"]["artifact_enchantment"]["cards"] == [
        "Naturalize", "Beast Within",
    ]
    assert "Cultivate" not in str(report["categories"])


# ---------------------------------------------------------------------------
# Instant-speed share — 8 sorceries is not 8 instants
# ---------------------------------------------------------------------------

def test_instant_speed_share_distinguishes_two_identical_counts():
    """Same five answers by count; completely different decks to play.
    Before this row nothing in the codebase could tell them apart."""
    fast = _report(*["Swords to Plowshares"] * 3, *["Naturalize"] * 2)
    slow = _report(*["Wrath of God"] * 3, *["Vandalblast"] * 2)
    assert fast["interaction_total"] == slow["interaction_total"] == 5
    assert fast["instant_speed"]["share"] == 1.0
    assert slow["instant_speed"]["share"] == 0.0
    # ...and only the sorcery-speed deck is told about it.
    assert fast["instant_speed"]["gap"] == 0.0
    assert slow["instant_speed"]["gap"] == pytest.approx(0.40)
    assert any("Instant-speed interaction" in g for g in slow["gaps"])
    assert not any("Instant-speed" in g for g in fast["gaps"])


def test_instant_speed_share_is_none_without_interaction():
    """A share of nothing is undefined, not 0% — the module's
    'unavailable returns None, never a fabricated 0' contract applies to
    ratios too."""
    report = _report("Cultivate", "Sol Ring", "Grizzly Bears")
    assert report["interaction_total"] == 0
    assert report["instant_speed"]["share"] is None
    assert report["instant_speed"]["gap"] is None
    assert not any("Instant-speed" in g for g in report["gaps"])


def test_instant_speed_minimum_follows_the_bracket():
    """40% is fine at bracket 3 and short at bracket 5."""
    cards = ["Swords to Plowshares", "Naturalize", "Wrath of God",
             "Vandalblast", "Blasphemous Act"]
    b3 = _report(*cards, bracket=3)
    b5 = _report(*cards, bracket=5)
    assert b3["instant_speed"]["share"] == pytest.approx(0.4)
    assert b3["instant_speed"]["gap"] == 0.0
    assert b5["instant_speed"]["gap"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Outage contract
# ---------------------------------------------------------------------------

def test_report_returns_none_when_most_of_the_deck_is_unreadable():
    """'We could not read your deck' and 'your deck has no interaction'
    are opposite conclusions and must never render the same."""
    mostly_unknown = _deck("Swords to Plowshares", "Unknown A", "Unknown B",
                           commander="Unknown C")
    assert interaction_report(
        mostly_unknown, lookup=_lookup, use_forge=False,
    ) is None  # 3 of 4 lines unreadable -> over the threshold
    # A single miss (1 of 4) is ordinary noise, not an outage: the
    # report still comes back, with the miss disclosed.
    one_miss = _deck("Swords to Plowshares", "Naturalize", "Unknown A")
    report = interaction_report(one_miss, lookup=_lookup, use_forge=False)
    assert report is not None
    assert report["lookup_failures"] == 1


def test_report_survives_a_lookup_that_raises():
    def boom(name):
        raise ConnectionError("Scryfall down")
    assert interaction_report(
        _deck("Swords to Plowshares"), lookup=boom, use_forge=False,
    ) is None


def test_report_returns_none_for_an_empty_deck():
    assert interaction_report("", lookup=_lookup, use_forge=False) is None
    assert interaction_report(
        "[Main]\n", lookup=_lookup, use_forge=False,
    ) is None


# ---------------------------------------------------------------------------
# The Forge card-script classifier (preferred) and its fallback
# ---------------------------------------------------------------------------

_FORGE_SCRIPTS: dict[str, str] = {
    # Real Forge DSL shapes: effect primitive + structured targets.
    "swords_to_plowshares": (
        "Name:Swords to Plowshares\n"
        "ManaCost:W\n"
        "Types:Instant\n"
        "A:SP$ ChangeZone | Cost$ W | Origin$ Battlefield | "
        "Destination$ Exile | ValidTgts$ Creature | "
        "SubAbility$ DBGainLife\n"
        "Oracle:Exile target creature. Its controller gains life equal "
        "to its power.\n"
    ),
    "naturalize": (
        "Name:Naturalize\n"
        "ManaCost:1 G\n"
        "Types:Instant\n"
        "A:SP$ Destroy | Cost$ 1 G | ValidTgts$ Artifact,Enchantment\n"
        "Oracle:Destroy target artifact or enchantment.\n"
    ),
    "counterspell": (
        "Name:Counterspell\n"
        "ManaCost:U U\n"
        "Types:Instant\n"
        "A:SP$ Counter | Cost$ U U | TargetType$ Spell | TgtPrompt$ "
        "Select target spell\n"
        "Oracle:Counter target spell.\n"
    ),
    "wrath_of_god": (
        "Name:Wrath of God\n"
        "ManaCost:2 W W\n"
        "Types:Sorcery\n"
        "A:SP$ DestroyAll | Cost$ 2 W W | ValidCards$ Creature | "
        "NoRegen$ True\n"
        "Oracle:Destroy all creatures. They can't be regenerated.\n"
    ),
    "bojuka_bog": (
        "Name:Bojuka Bog\n"
        "ManaCost:no cost\n"
        "Types:Land\n"
        "K:ETBReplacement:Other:BogTapped\n"
        "T:Mode$ ChangesZone | Origin$ Any | Destination$ Battlefield | "
        "ValidCard$ Card.Self | Execute$ TrigChangeZoneAll\n"
        "SVar:TrigChangeZoneAll:DB$ ChangeZoneAll | ChangeType$ Card | "
        "Origin$ Graveyard | Destination$ Exile | "
        "TargetType$ Player\n"
        "A:AB$ Mana | Cost$ T | Produced$ B\n"
        "Oracle:Bojuka Bog enters tapped.\n"
    ),
}


@pytest.fixture
def forge_loader(tmp_path):
    """A real ``CardsLoader`` over a tiny lettered corpus on disk."""
    corpus = tmp_path / "cardsfolder"
    for slug, text in _FORGE_SCRIPTS.items():
        letter_dir = corpus / slug[0]
        letter_dir.mkdir(parents=True, exist_ok=True)
        (letter_dir / f"{slug}.txt").write_text(text, encoding="utf-8")
    return CardsLoader(directory=corpus)


@pytest.mark.parametrize("slug,expected", [
    # ChangeZone out of the battlefield with a Creature target = spot
    # creature removal. The regex has to infer this from prose; Forge
    # states it.
    ("swords_to_plowshares", {"creature_removal"}),
    ("naturalize", {"artifact_enchantment"}),
    ("counterspell", {"stack"}),
    ("wrath_of_god", {"board_wipe"}),
])
def test_forge_effect_primitives_classify_cards(slug, expected):
    script = parse_card_script(_FORGE_SCRIPTS[slug])
    assert interaction.forge_categories(script) == expected


def test_forge_classifier_wins_over_oracle_regex():
    """When Forge has a verdict it is authoritative — that is the whole
    reason to prefer the effect primitive over prose matching."""
    script = parse_card_script(_FORGE_SCRIPTS["counterspell"])
    assert classify_interaction(
        "some unrelated prose", "Instant", script=script,
    ) == {"stack"}


def test_forge_no_verdict_falls_back_to_oracle_regex():
    """Bojuka Bog's graveyard exile lives inside an SVar-expanded
    sub-ability, which ``forge_script_parser`` deliberately does not
    expand. An empty Forge verdict must therefore mean 'no opinion' and
    defer to the regex — not silently drop the card from the matrix."""
    script = parse_card_script(_FORGE_SCRIPTS["bojuka_bog"])
    assert interaction.forge_categories(script) == set()
    oracle_text, type_line = _ORACLES["Bojuka Bog"]
    assert classify_interaction(
        oracle_text, type_line, script=script,
    ) == {"graveyard_hate"}


def test_report_uses_the_forge_corpus_when_it_resolves(forge_loader):
    """End to end with the loader injected: the report records WHICH
    classifier decided each card, so a corpus-less install is visible in
    the payload rather than inferred."""
    report = interaction_report(
        _deck("Swords to Plowshares", "Naturalize", "Counterspell",
              "Wrath of God"),
        bracket=3, lookup=_lookup, loader=forge_loader,
    )
    assert report["classified_by"]["forge"] == 4
    assert report["classified_by"]["oracle"] == 0
    assert report["categories"]["creature_removal"]["count"] == 1
    assert report["categories"]["artifact_enchantment"]["count"] == 1
    assert report["categories"]["stack"]["count"] == 1
    assert report["categories"]["board_wipe"]["count"] == 1


def test_report_without_a_corpus_uses_oracle_regex(forge_loader):
    """Same deck, no loader: identical rows, credited to the fallback."""
    report = _report("Swords to Plowshares", "Naturalize", "Counterspell",
                     "Wrath of God")
    assert report["classified_by"] == {"forge": 0, "oracle": 4}
    assert report["categories"]["creature_removal"]["count"] == 1
    assert report["categories"]["stack"]["count"] == 1


def test_forge_path_classifies_without_scryfall_at_all(forge_loader):
    """The Forge corpus carries types AND effects, so a deck can be
    graded with Scryfall completely unreachable — the property that makes
    this the preferred classifier, not just the more accurate one."""
    report = interaction_report(
        "[Main]\n1 Swords to Plowshares\n1 Counterspell\n1 Wrath of God\n",
        bracket=3, lookup=lambda name: None, loader=forge_loader,
    )
    assert report is not None
    assert report["categories"]["creature_removal"]["count"] == 1
    # Types came from the Forge script, so instant-speed still works.
    assert report["instant_speed"]["count"] == 2
    assert report["instant_speed"]["share"] == pytest.approx(2 / 3)


def test_default_loader_is_optional_and_fails_quiet(monkeypatch):
    """No vendor/forge (fresh checkout, CI) is not an error — the module
    must never REQUIRE the corpus."""
    monkeypatch.setattr(
        interaction, "VENDOR_FORGE", interaction.REPO_ROOT / "no-such-dir",
    )
    assert interaction._default_loader() is None
    report = interaction_report(
        _deck("Counterspell"), lookup=_lookup,
    )
    assert report["classified_by"]["oracle"] == 1


# ---------------------------------------------------------------------------
# The commander counts
# ---------------------------------------------------------------------------

def test_commander_answers_count_toward_coverage():
    """A commander that IS the deck's repeatable artifact answer really
    does cover that row — it's available in every game. Same reasoning
    as the health grade's commander role credit."""
    oracles = dict(_ORACLES)
    oracles["Answer Commander"] = (
        "{2}{W}, {T}: Destroy target artifact or enchantment.",
        "Legendary Creature — Human Cleric",
    )
    def lookup(name):
        entry = oracles.get(name.strip())
        if entry is None:
            return None
        return {"name": name, "oracle_text": entry[0], "type_line": entry[1]}

    report = interaction_report(
        _deck("Cultivate", commander="Answer Commander"),
        bracket=3, lookup=lookup, use_forge=False,
    )
    assert report["categories"]["artifact_enchantment"]["cards"] == [
        "Answer Commander",
    ]
