"""Tests for ``forge_script_parser`` — the first slice of FP-001.

Fixtures under ``tests/fixtures/forge_scripts/`` are byte-exact
copies of Forge 2.0.12's card-script files (sourced from
https://github.com/Card-Forge/forge/tree/master/forge-gui/res/cardsfolder).
Same rule as ``tests/fixtures/real_oracles.py``: do NOT paraphrase
or "clean up" — real DSL has whitespace quirks, escaped ``\\n`` in
Oracle text, mixed PT casing, and other shapes the parser must
handle as Forge ships them. If a future Forge version changes the
DSL, the fixture refresh is intentional and visible in the diff.

Each test pins one specific behavior so a parser regression points
at the exact contract that broke. The ``raw_unparsed_lines`` empty
check on each fixture is the early-warning system for DSL drift:
when Forge adds a new top-level key (e.g. a new ``Energy:`` for
Phyrexia: All Will Be One mechanics), the assertion fires loudly
and we know to extend the parser.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commander_builder.forge_script_parser import (
    Ability,
    CardScript,
    parse_card_script,
    parse_card_script_file,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "forge_scripts"


# ---------------------------------------------------------------------------
# Top-level parse on the 8 fixture cards
# ---------------------------------------------------------------------------

def test_parse_vanilla_creature():
    """Runeclaw Bear — the simplest possible creature script. No
    abilities, no SVars, just Name / ManaCost / Types / PT / Oracle."""
    card = parse_card_script_file(FIXTURE_DIR / "runeclaw_bear.txt")
    assert card.name == "Runeclaw Bear"
    assert card.mana_cost == "1 G"
    assert card.types == ["Creature", "Bear"]
    assert card.pt == ("2", "2")
    assert card.keywords == []
    assert card.abilities == []
    assert card.svars == {}
    assert card.is_creature is True
    assert card.is_land is False
    assert card.is_dfc is False
    assert card.raw_unparsed_lines == []


def test_parse_basic_land_with_two_mana_abilities():
    """Underground River — two ``A:AB$ Mana`` lines (one for {C},
    one for {U}/{B} with a damage subability) plus an SVar."""
    card = parse_card_script_file(FIXTURE_DIR / "underground_river.txt")
    assert card.name == "Underground River"
    assert card.mana_cost is None  # "no cost" → None
    assert card.types == ["Land"]
    assert card.is_land is True
    assert len(card.abilities) == 2
    a1, a2 = card.abilities
    assert a1.kind == "A"
    assert a1.category == "AB"
    assert a1.effect == "Mana"
    assert a1.params["Cost"] == "T"
    assert a1.params["Produced"] == "C"
    assert a2.params["Produced"] == "Combo U B"
    assert a2.params["SubAbility"] == "DBPain"
    # SVar carries the sub-ability body verbatim.
    assert "DealDamage" in card.svars["DBPain"]
    assert card.raw_unparsed_lines == []


def test_parse_sorcery_with_chained_subabilities():
    """Cultivate — ``A:SP$ ChangeZone`` + 3 chained SVars
    (DBChangeZone1 → DBChangeZone2 → DBCleanup). Pins the SP$
    category and the SVar collection."""
    card = parse_card_script_file(FIXTURE_DIR / "cultivate.txt")
    assert card.name == "Cultivate"
    assert card.types == ["Sorcery"]
    assert len(card.abilities) == 1
    spell = card.abilities[0]
    assert spell.kind == "A"
    assert spell.category == "SP"
    assert spell.effect == "ChangeZone"
    assert spell.params["Origin"] == "Library"
    assert spell.params["Destination"] == "Library"
    assert spell.params["ChangeNum"] == "2"
    assert spell.params["SubAbility"] == "DBChangeZone1"
    # Three sub-ability SVars present, each with the expected body kind.
    assert set(card.svars.keys()) == {
        "DBChangeZone1", "DBChangeZone2", "DBCleanup",
    }
    assert card.svars["DBChangeZone1"].startswith("DB$ ChangeZone")
    assert card.svars["DBCleanup"].startswith("DB$ Cleanup")
    assert card.raw_unparsed_lines == []


def test_parse_creature_with_keywords_only():
    """Deadly Recluse — two ``K:`` lines for Reach + Deathtouch,
    no other abilities. Verifies keyword list preservation in
    order."""
    card = parse_card_script_file(FIXTURE_DIR / "deadly_recluse.txt")
    assert card.keywords == ["Reach", "Deathtouch"]
    assert card.abilities == []
    assert card.is_creature is True
    assert card.raw_unparsed_lines == []


def test_parse_enchantment_with_static_effect():
    """Glorious Anthem — ``S:Mode$ Continuous`` with AddPower /
    AddToughness. Static effects have category=Mode (not AB/SP) so
    callers can dispatch by category. Also includes the playmain1
    SVar that Forge uses for AI decision hints."""
    card = parse_card_script_file(FIXTURE_DIR / "glorious_anthem.txt")
    assert card.types == ["Enchantment"]
    assert len(card.abilities) == 1
    static = card.abilities[0]
    assert static.kind == "S"
    assert static.category == "Mode"
    assert static.effect == "Continuous"
    assert static.params["Affected"] == "Creature.YouCtrl"
    assert static.params["AddPower"] == "1"
    assert static.params["AddToughness"] == "1"
    # PlayMain1 SVar (AI hint) is preserved verbatim.
    assert card.svars.get("PlayMain1") == "TRUE"
    assert card.raw_unparsed_lines == []


def test_parse_creature_with_activated_ability_using_svar_count():
    """Krenko, Mob Boss — ``A:AB$ Token`` with TokenAmount referring
    to SVar X (a ``Count$Valid Goblin.YouCtrl`` expression). Pins
    that SVar values stay symbolic strings (not interpreted)."""
    card = parse_card_script_file(FIXTURE_DIR / "krenko_mob_boss.txt")
    assert card.name == "Krenko, Mob Boss"
    assert card.types == ["Legendary", "Creature", "Goblin", "Warrior"]
    assert card.pt == ("3", "3")
    assert card.mana_cost == "2 R R"
    activated = card.abilities[0]
    assert activated.effect == "Token"
    assert activated.params["TokenAmount"] == "X"
    assert activated.params["TokenScript"] == "r_1_1_goblin"
    # X stays as the raw Count$ expression — interpretation is the
    # rules engine's job, not the parser's.
    assert card.svars["X"] == "Count$Valid Goblin.YouCtrl"
    # DeckHints surface for archetype detection downstream.
    assert "Type$Goblin" in card.deck_hints
    assert card.raw_unparsed_lines == []


def test_parse_land_with_trigger_and_replacement():
    """Kabira Crossroads — covers both ``R:Event$ Moved`` (replacement
    effect: ETB tapped) AND ``T:Mode$ ChangesZone`` (trigger: gain
    life on ETB) in one file. Three abilities total: Mana A:, R:, T:."""
    card = parse_card_script_file(FIXTURE_DIR / "kabira_crossroads.txt")
    assert card.types == ["Land"]
    assert len(card.abilities) == 3
    kinds = [a.kind for a in card.abilities]
    assert kinds == ["A", "R", "T"]
    repl = card.abilities[1]
    assert repl.category == "Event"
    assert repl.effect == "Moved"
    assert repl.params["Destination"] == "Battlefield"
    assert repl.params["ReplaceWith"] == "ETBTapped"
    trig = card.abilities[2]
    assert trig.category == "Mode"
    assert trig.effect == "ChangesZone"
    assert trig.params["Execute"] == "TrigGainLife"
    assert card.svars["TrigGainLife"].startswith("DB$ GainLife")
    assert card.raw_unparsed_lines == []


def test_parse_legendary_land_with_channel_ability():
    """Boseiju, Who Endures — channel ability is implemented as a
    second ``A:AB$ Destroy`` with ``Cost$ 1 G Discard<1/CARDNAME>``
    and ``ActivationZone$ Hand``. Pins that the non-trivial Cost
    string stays verbatim (parser doesn't try to decompose it)."""
    card = parse_card_script_file(FIXTURE_DIR / "boseiju_who_endures.txt")
    assert card.types == ["Legendary", "Land"]
    assert len(card.abilities) == 2
    channel = card.abilities[1]
    assert channel.effect == "Destroy"
    # The whole cost string survives intact.
    assert "Discard<1/CARDNAME>" in channel.params["Cost"]
    assert channel.params["ActivationZone"] == "Hand"
    assert channel.params["PrecostDesc"] == "Channel —"
    assert card.raw_unparsed_lines == []


# ---------------------------------------------------------------------------
# Direct parse from text (not file-mediated)
# ---------------------------------------------------------------------------

def test_parse_card_script_from_string_handles_no_cost_marker():
    """Lands use ``ManaCost:no cost``; parser normalizes to None."""
    src = (
        "Name:Forest\n"
        "ManaCost:no cost\n"
        "Types:Basic Land Forest\n"
        "A:AB$ Mana | Cost$ T | Produced$ G\n"
        "Oracle:({T}: Add {G}.)\n"
    )
    card = parse_card_script(src)
    assert card.mana_cost is None
    assert "Basic" in card.types


def test_parse_card_script_handles_variable_pt():
    """Tarmogoyf-style ``PT:*/1+*`` should round-trip as symbolic
    strings — the parser doesn't try to coerce to int."""
    card = parse_card_script(
        "Name:Tarmogoyf\n"
        "ManaCost:1 G\n"
        "Types:Creature Lhurgoyf\n"
        "PT:*/1+*\n"
        "Oracle:Tarmogoyf's power is equal to the number of card "
        "types among cards in all graveyards and its toughness is "
        "equal to that number plus 1.\n"
    )
    assert card.pt == ("*", "1+*")


def test_parse_card_script_handles_loyalty():
    """Planeswalker scripts use ``Loyalty:N`` instead of ``PT:``."""
    card = parse_card_script(
        "Name:Test Walker\n"
        "ManaCost:2 W\n"
        "Types:Legendary Planeswalker Test\n"
        "Loyalty:3\n"
        "Oracle:[+1]: Foo.\n"
    )
    assert card.loyalty == 3
    assert card.pt is None


def test_parse_card_script_unknown_top_level_key_captured_for_audit():
    """An unrecognized top-level key (DSL drift) lands in
    ``raw_unparsed_lines`` so a test can flag the new vocabulary
    without the parser failing on real cards."""
    warnings: list[str] = []
    card = parse_card_script(
        "Name:Future Card\n"
        "ManaCost:1\n"
        "Types:Artifact\n"
        "NewKeyword2026:experimental-value\n"
        "Oracle:placeholder\n",
        warn=warnings.append,
    )
    assert card.name == "Future Card"
    assert "NewKeyword2026:experimental-value" in card.raw_unparsed_lines
    assert any("NewKeyword2026" in w for w in warnings)


def test_parse_card_script_malformed_ability_segment_preserved():
    """If an A: line contains a segment that doesn't match the
    Key$ Value shape, the parser keeps it under a synthetic key
    rather than dropping it. We want to AUDIT what's malformed,
    not silently lose data."""
    card = parse_card_script(
        "Name:Half-Baked\n"
        "ManaCost:R\n"
        "Types:Instant\n"
        "A:AB$ DealDamage | NumDmg$ 1 | totally weird segment\n"
        "Oracle:placeholder\n"
    )
    ability = card.abilities[0]
    assert ability.effect == "DealDamage"
    assert ability.params["NumDmg"] == "1"
    # The weird segment is captured under a synthetic key so the
    # test/audit can find it.
    assert any(
        k.startswith("_unparsed_") and "weird" in v
        for k, v in ability.params.items()
    )


# ---------------------------------------------------------------------------
# DFC (AlternateMode) handling
# ---------------------------------------------------------------------------

def test_parse_dfc_splits_at_alternatemode():
    """Synthetic DFC: parent face is the spell, secondary face is
    the land. ``AlternateMode:DoubleFaced`` belongs to the parent
    (records the mode); the next ``Name:`` starts the child face."""
    src = (
        "Name:Test MDFC Front\n"
        "ManaCost:2 G\n"
        "Types:Sorcery\n"
        "A:SP$ Draw | NumCards$ 2\n"
        "AlternateMode:DoubleFaced\n"
        "Oracle:Draw two cards.\n"
        "Name:Test MDFC Back\n"
        "ManaCost:no cost\n"
        "Types:Land\n"
        "A:AB$ Mana | Cost$ T | Produced$ G\n"
        "Oracle:{T}: Add {G}.\n"
    )
    card = parse_card_script(src)
    assert card.name == "Test MDFC Front"
    assert card.is_dfc is True
    assert card.alternate_mode == "DoubleFaced"
    assert card.types == ["Sorcery"]
    assert len(card.faces) == 1
    back = card.faces[0]
    assert back.name == "Test MDFC Back"
    assert back.types == ["Land"]
    assert back.abilities[0].effect == "Mana"


# ---------------------------------------------------------------------------
# Empty / pathological input
# ---------------------------------------------------------------------------

def test_parse_empty_string_returns_empty_card_script():
    """Defensive: empty input → empty CardScript, not a crash."""
    card = parse_card_script("")
    assert card.name == ""
    assert card.abilities == []


def test_parse_whitespace_only_returns_empty_card_script():
    card = parse_card_script("   \n\n  \t\n")
    assert card.name == ""
    assert card.abilities == []


def test_parse_card_script_file_tolerates_non_utf8_bytes(tmp_path):
    """Latin-1 byte snuck into a fixture (real-world Forge has had
    encoding glitches in old set scripts) shouldn't crash the
    parser. UTF-8 replacement mode swaps the bad byte for U+FFFD."""
    p = tmp_path / "bad_encoding.txt"
    # Latin-1 ``é`` (0xE9) in the Oracle text without proper UTF-8.
    p.write_bytes(
        b"Name:Encoding Test\n"
        b"ManaCost:1\n"
        b"Types:Artifact\n"
        b"Oracle:Tap to make caf\xe9.\n"
    )
    card = parse_card_script_file(p)
    assert card.name == "Encoding Test"
    # Bad byte replaced; no crash.
    assert "caf" in card.oracle
