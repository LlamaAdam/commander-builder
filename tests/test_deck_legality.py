"""Tests for ``commander_builder.deck_legality``.

Fully offline: every test injects a dict-backed ``lookup`` stub
through ``validate_deck``'s / ``scan_banned``'s keyword seam, so the
suite never touches Scryfall (same hermetic-suite policy as
tests/test_deck_health.py, which monkeypatches
``scryfall_client.lookup_card``; the injection seam is preferred here
because it needs no monkeypatch at all).

The stubs return the SHAPE Scryfall returns — ``type_line``,
``oracle_text``, ``color_identity``, ``legalities.commander`` — so a
projection change upstream shows up as a failure here rather than as
a silent behavior change in production.
"""
from __future__ import annotations

import pytest

from commander_builder import deck_legality
from commander_builder.deck_legality import (
    BanScan,
    LegalityReport,
    copy_limit,
    scan_banned,
    validate_deck,
)


# ---------------------------------------------------------------------------
# Card / deck builders
# ---------------------------------------------------------------------------

def _card(
    name: str,
    *,
    type_line: str = "Artifact",
    oracle: str = "",
    ci: str = "",
    legality: str = "legal",
) -> dict:
    """A Scryfall-shaped card dict. ``ci`` is a WUBRG letter string."""
    card: dict = {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "color_identity": list(ci),
    }
    if legality is not None:
        card["legalities"] = {"commander": legality}
    return card


def _lookup(*cards: dict):
    """A ``name -> card`` stub over ``cards``, case-insensitive."""
    index = {c["name"].lower(): c for c in cards}

    def _fn(name: str):
        return index.get(name.strip().lower())

    return _fn


def _deck(commanders, main) -> str:
    """Render a .dck blob. ``main`` is a list of ``(qty, name)``."""
    lines = ["[metadata]", "Name=Test", "", "[Commander]"]
    lines += [f"1 {c}" for c in commanders]
    lines += ["", "[Main]"]
    lines += [f"{qty} {name}" for qty, name in main]
    return "\n".join(lines) + "\n"


# Commanders reused across tests.
GENERIC_CMDR = _card(
    "Test Commander",
    type_line="Legendary Creature — Elder Dragon",
    ci="G",
)
FOREST = _card("Forest", type_line="Basic Land — Forest", ci="G")
SOL_RING = _card("Sol Ring", type_line="Artifact")


def _filler(n: int) -> list[tuple[int, str]]:
    """``n`` mainboard slots of basic Forest (legal in any quantity)."""
    return [(n, "Forest")]


def _legal_deck() -> str:
    """A minimal, genuinely legal 100-card deck: cmdr + 98 Forest + Sol Ring."""
    return _deck(["Test Commander"], _filler(98) + [(1, "Sol Ring")])


def _legal_lookup():
    return _lookup(GENERIC_CMDR, FOREST, SOL_RING)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_legal_deck_is_verified_legal():
    report = validate_deck(_legal_deck(), lookup=_legal_lookup())
    assert isinstance(report, LegalityReport)
    assert report.legal is True
    assert report.violations == ()
    assert report.unverified == ()
    assert report.verified is True
    assert report.status == "legal"
    assert report.card_count == 100
    assert report.commander_count == 1
    assert report.lookup_failures == 0


def test_report_to_dict_is_json_shaped():
    report = validate_deck(_legal_deck(), lookup=_legal_lookup())
    body = report.to_dict()
    assert body["legal"] is True
    assert body["status"] == "legal"
    assert body["violations"] == []
    assert body["unverified"] == []
    assert body["card_count"] == 100


# ---------------------------------------------------------------------------
# 1. Deck size
# ---------------------------------------------------------------------------

def test_deck_size_short_deck_is_illegal():
    deck = _deck(["Test Commander"], _filler(50))
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_DECK_SIZE in report.codes()
    assert report.legal is False
    assert report.status == "illegal"
    assert "51" in report.violations[0].message


def test_deck_size_counts_quantities_not_lines():
    """``98 Forest`` is 98 cards, not one line."""
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Sol Ring")])
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_DECK_SIZE not in report.codes()


def test_deck_size_partner_pair_is_98_plus_2():
    """A partner pair is a legal 98 + 2, not 99 + 1."""
    a = _card(
        "Thrasios, Triton Hero",
        type_line="Legendary Creature — Merfolk Wizard",
        oracle="Partner (You can have two commanders if both have partner.)",
        ci="GU",
    )
    b = _card(
        "Tymna the Weaver",
        type_line="Legendary Creature — Human Cleric",
        oracle="Partner (You can have two commanders if both have partner.)",
        ci="WB",
    )
    deck = _deck(
        ["Thrasios, Triton Hero", "Tymna the Weaver"],
        [(98, "Forest")],
    )
    report = validate_deck(deck, lookup=_lookup(a, b, FOREST))
    assert report.violations == ()
    assert report.legal is True
    assert report.commander_count == 2


# ---------------------------------------------------------------------------
# 2. Singleton + its exemptions
# ---------------------------------------------------------------------------

def test_duplicate_nonbasic_is_illegal():
    deck = _deck(["Test Commander"], [(97, "Forest"), (2, "Sol Ring")])
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_DUPLICATE_CARD in report.codes()
    violation = report.violations[0]
    assert violation.cards == ("Sol Ring (x2)",)


def test_basic_lands_are_exempt_from_singleton():
    """98 Forest needs no lookup at all — the exemption is by name."""
    report = validate_deck(_legal_deck(), lookup=_legal_lookup())
    assert deck_legality.CODE_DUPLICATE_CARD not in report.codes()


def test_snow_covered_basics_are_exempt():
    snow = _card("Snow-Covered Forest", type_line="Basic Snow Land — Forest", ci="G")
    deck = _deck(["Test Commander"], [(98, "Snow-Covered Forest"), (1, "Sol Ring")])
    report = validate_deck(deck, lookup=_lookup(GENERIC_CMDR, snow, SOL_RING))
    assert report.violations == ()


def test_any_number_of_cards_named_exemption():
    """Relentless Rats et al. say so in their own oracle text."""
    rats = _card(
        "Relentless Rats",
        type_line="Creature — Rat",
        oracle=(
            "Relentless Rats gets +1/+1 for each other creature on the "
            "battlefield named Relentless Rats.\n"
            "A deck can have any number of cards named Relentless Rats."
        ),
        ci="B",
    )
    cmdr = _card(
        "Rat Boss", type_line="Legendary Creature — Rat", ci="B",
    )
    deck = _deck(["Rat Boss"], [(30, "Relentless Rats"), (69, "Swamp")])
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    report = validate_deck(deck, lookup=_lookup(cmdr, rats, swamp))
    assert report.violations == ()
    assert report.legal is True


@pytest.mark.parametrize("name", [
    "Persistent Petitioners", "Dragon's Approach", "Shadowborn Apostle",
])
def test_any_number_exemption_is_text_driven_not_name_driven(name):
    """The exemption comes from oracle text, so it covers every card
    with the templated wording — no per-name allowlist to maintain."""
    card = _card(
        name,
        type_line="Creature",
        oracle=f"A deck can have any number of cards named {name}.",
        ci="B",
    )
    cmdr = _card("Rat Boss", type_line="Legendary Creature — Rat", ci="B")
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    deck = _deck(["Rat Boss"], [(30, name), (69, "Swamp")])
    report = validate_deck(deck, lookup=_lookup(cmdr, card, swamp))
    assert report.violations == ()


def _nazgul_card() -> dict:
    return _card(
        "Nazgûl",
        type_line="Creature — Wraith",
        oracle=(
            "Menace\nWhenever the Ring tempts you, put a +1/+1 counter "
            "on Nazgûl.\nA deck can have up to nine cards named Nazgûl."
        ),
        ci="B",
    )


def test_nazgul_nine_copies_is_legal():
    cmdr = _card("Wraith Lord", type_line="Legendary Creature — Wraith", ci="B")
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    deck = _deck(["Wraith Lord"], [(9, "Nazgûl"), (90, "Swamp")])
    report = validate_deck(deck, lookup=_lookup(cmdr, _nazgul_card(), swamp))
    assert report.violations == ()
    assert report.legal is True


def test_nazgul_ten_copies_exceeds_its_cap():
    cmdr = _card("Wraith Lord", type_line="Legendary Creature — Wraith", ci="B")
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    deck = _deck(["Wraith Lord"], [(10, "Nazgûl"), (89, "Swamp")])
    report = validate_deck(deck, lookup=_lookup(cmdr, _nazgul_card(), swamp))
    assert deck_legality.CODE_COPY_LIMIT in report.codes()
    assert deck_legality.CODE_DUPLICATE_CARD not in report.codes()
    over = [v for v in report.violations
            if v.code == deck_legality.CODE_COPY_LIMIT][0]
    assert "limit 9" in over.cards[0]


def test_copy_limit_parses_the_three_templates():
    assert copy_limit(_card("Sol Ring")) == 1
    assert copy_limit(_nazgul_card()) == 9
    assert copy_limit(_card(
        "Relentless Rats",
        oracle="A deck can have any number of cards named Relentless Rats.",
    )) is None
    # No card data at all -> the singleton default; callers treat a
    # missing card as unverified BEFORE reaching this helper.
    assert copy_limit(None) == 1


# ---------------------------------------------------------------------------
# 3. Color identity
# ---------------------------------------------------------------------------

def test_off_color_card_is_illegal():
    lightning = _card("Lightning Bolt", type_line="Instant", ci="R")
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Lightning Bolt")])
    report = validate_deck(
        deck, lookup=_lookup(GENERIC_CMDR, FOREST, lightning),
    )
    assert deck_legality.CODE_COLOR_IDENTITY in report.codes()
    violation = [v for v in report.violations
                 if v.code == deck_legality.CODE_COLOR_IDENTITY][0]
    assert violation.cards == ("Lightning Bolt (R)",)


def test_color_identity_uses_scryfall_field_including_rules_text():
    """Kenrith costs {4}{W} but his ACTIVATED ABILITIES make him WUBRG.

    Scryfall's ``color_identity`` already folds in mana symbols that
    appear only in rules text; a mana-cost-derived identity would call
    a Swamp off-color in a Kenrith deck. This test fails loudly if the
    module ever starts re-deriving identity from ``mana_cost``.
    """
    kenrith = _card(
        "Kenrith, the Returned King",
        type_line="Legendary Creature — Human Noble",
        oracle=(
            "{R}: All creatures gain trample and haste until end of turn.\n"
            "{1}{G}: Put a +1/+1 counter on target creature.\n"
            "{2}{W}: Target player gains 5 life.\n"
            "{3}{U}: Target player draws a card.\n"
            "{4}{B}: Return target creature card from a graveyard."
        ),
        # Mana cost is {4}{W}; identity is all five colors.
        ci="WUBRG",
    )
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    bolt = _card("Lightning Bolt", type_line="Instant", ci="R")
    deck = _deck(
        ["Kenrith, the Returned King"],
        [(98, "Swamp"), (1, "Lightning Bolt")],
    )
    report = validate_deck(deck, lookup=_lookup(kenrith, swamp, bolt))
    assert deck_legality.CODE_COLOR_IDENTITY not in report.codes()
    assert report.legal is True


def test_colorless_commander_only_admits_colorless_cards():
    karn = _card(
        "Karn, Silver Golem",
        type_line="Legendary Artifact Creature — Golem",
        ci="",
    )
    wastes = _card("Wastes", type_line="Basic Land", ci="")
    forest = FOREST
    deck = _deck(["Karn, Silver Golem"], [(98, "Wastes"), (1, "Forest")])
    report = validate_deck(deck, lookup=_lookup(karn, wastes, forest))
    violation = [v for v in report.violations
                 if v.code == deck_legality.CODE_COLOR_IDENTITY][0]
    assert violation.cards == ("Forest (G)",)
    assert "colorless" in violation.message


def test_partner_pair_color_identity_is_the_union():
    a = _card(
        "Thrasios, Triton Hero",
        type_line="Legendary Creature — Merfolk Wizard",
        oracle="Partner (You can have two commanders if both have partner.)",
        ci="GU",
    )
    b = _card(
        "Tymna the Weaver",
        type_line="Legendary Creature — Human Cleric",
        oracle="Partner (You can have two commanders if both have partner.)",
        ci="WB",
    )
    swamp = _card("Swamp", type_line="Basic Land — Swamp", ci="B")
    island = _card("Island", type_line="Basic Land — Island", ci="U")
    bolt = _card("Lightning Bolt", type_line="Instant", ci="R")
    # B and U are both in the WUBG union; R is not.
    deck = _deck(
        ["Thrasios, Triton Hero", "Tymna the Weaver"],
        [(50, "Swamp"), (47, "Island"), (1, "Lightning Bolt")],
    )
    report = validate_deck(deck, lookup=_lookup(a, b, swamp, island, bolt))
    violation = [v for v in report.violations
                 if v.code == deck_legality.CODE_COLOR_IDENTITY][0]
    assert violation.cards == ("Lightning Bolt (R)",)


# ---------------------------------------------------------------------------
# 4. Commander eligibility
# ---------------------------------------------------------------------------

def test_non_legendary_commander_is_illegal():
    bear = _card("Grizzly Bears", type_line="Creature — Bear", ci="G")
    deck = _deck(["Grizzly Bears"], _filler(98) + [(1, "Sol Ring")])
    report = validate_deck(deck, lookup=_lookup(bear, FOREST, SOL_RING))
    assert deck_legality.CODE_COMMANDER_INELIGIBLE in report.codes()


def test_legendary_noncreature_commander_is_illegal():
    """A Legendary Artifact with no "can be your commander" text."""
    ring = _card("The One Ring", type_line="Legendary Artifact", ci="")
    deck = _deck(["The One Ring"], _filler(98) + [(1, "Sol Ring")])
    report = validate_deck(deck, lookup=_lookup(ring, FOREST, SOL_RING))
    assert deck_legality.CODE_COMMANDER_INELIGIBLE in report.codes()


def test_can_be_your_commander_text_qualifies_a_planeswalker():
    rowan = _card(
        "Rowan, Scholar of Sparks",
        type_line="Legendary Planeswalker — Rowan",
        oracle="Rowan, Scholar of Sparks can be your commander.",
        ci="UR",
    )
    island = _card("Island", type_line="Basic Land — Island", ci="U")
    deck = _deck(["Rowan, Scholar of Sparks"], [(98, "Island"), (1, "Sol Ring")])
    report = validate_deck(deck, lookup=_lookup(rowan, island, SOL_RING))
    assert report.violations == ()
    assert report.legal is True


def test_missing_commander_section():
    deck = "[metadata]\nName=Test\n\n[Main]\n" + "1 Forest\n" * 100
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_COMMANDER_MISSING in report.codes()


def test_three_commanders_is_illegal():
    deck = _deck(
        ["Test Commander", "Test Commander", "Test Commander"],
        [(97, "Forest")],
    )
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_COMMANDER_COUNT in report.codes()


# ---------------------------------------------------------------------------
# 5. Partner / Friends forever / Background / Doctor's companion
# ---------------------------------------------------------------------------

def _pair_report(card_a, card_b, extra=()):
    """Validate a two-commander deck padded with colorless Wastes, so
    the pairing check is isolated from the color-identity check."""
    wastes = _card("Wastes", type_line="Basic Land", ci="")
    deck = _deck([card_a["name"], card_b["name"]], [(98, "Wastes")])
    return validate_deck(deck, lookup=_lookup(card_a, card_b, wastes, *extra))


def test_bare_partner_pair_is_legal():
    a = _card("Sidar Kondo of Jamuraa",
              type_line="Legendary Creature — Human Soldier",
              oracle="Partner (You can have two commanders if both have partner.)",
              ci="GW")
    b = _card("Tana, the Bloodsower",
              type_line="Legendary Creature — Human Warrior",
              oracle="Partner (You can have two commanders if both have partner.)",
              ci="RG")
    assert _pair_report(a, b).violations == ()


def test_partner_with_named_pair_is_legal():
    a = _card("Kydele, Chosen of Kruphix",
              type_line="Legendary Creature — Human Wizard",
              oracle="Partner with Thrasios, Triton Hero (When this creature "
                     "enters, target player may put this card into their hand.)",
              ci="GU")
    b = _card("Thrasios, Triton Hero",
              type_line="Legendary Creature — Merfolk Wizard",
              oracle="Partner with Kydele, Chosen of Kruphix",
              ci="GU")
    assert _pair_report(a, b).violations == ()


def test_partner_with_the_wrong_card_is_illegal():
    a = _card("Kydele, Chosen of Kruphix",
              type_line="Legendary Creature — Human Wizard",
              oracle="Partner with Thrasios, Triton Hero",
              ci="GU")
    b = _card("Krenko, Mob Boss",
              type_line="Legendary Creature — Goblin Warrior",
              ci="R")
    assert deck_legality.CODE_COMMANDER_PAIR in _pair_report(a, b).codes()


def test_friends_forever_pair_is_legal():
    a = _card("Faceless Agent",
              type_line="Legendary Creature — Shapeshifter Agent",
              oracle="Changeling\nFriends forever (You can have two commanders "
                     "if both have friends forever.)",
              ci="")
    b = _card("Grimgrin, Corpse-Born",
              type_line="Legendary Creature — Zombie Horror",
              oracle="Friends forever",
              ci="U")
    assert _pair_report(a, b).violations == ()


def test_background_pair_is_legal():
    """A Background is a Legendary ENCHANTMENT — it is never a legendary
    creature and never says "can be your commander". Its eligibility
    comes entirely from the other commander's "Choose a Background"."""
    a = _card("Wilson, Refined Grizzly",
              type_line="Legendary Creature — Bear",
              oracle="Vigilance, trample\nChoose a Background (You can have a "
                     "Background as a second commander.)",
              ci="G")
    b = _card("Raised by Giants",
              type_line="Legendary Enchantment — Background",
              oracle="Commander creatures you own have base power and "
                     "toughness 10/10 and are Giants in addition to their "
                     "other types.",
              ci="G")
    report = _pair_report(a, b)
    assert report.violations == ()
    assert report.legal is True


def test_doctors_companion_pair_is_legal():
    a = _card("The Fifteenth Doctor",
              type_line="Legendary Creature — Time Lord Doctor",
              oracle="Whenever you attack, draw a card.",
              ci="WU")
    b = _card("Ruby Sunday",
              type_line="Legendary Creature — Human",
              oracle="Doctor's companion (You can have two commanders if the "
                     "other is the Doctor.)",
              ci="R")
    assert _pair_report(a, b).violations == ()


def test_two_unrelated_commanders_is_illegal():
    a = _card("Krenko, Mob Boss",
              type_line="Legendary Creature — Goblin Warrior", ci="R")
    b = _card("Atraxa, Praetors' Voice",
              type_line="Legendary Creature — Phyrexian Angel Horror",
              ci="WUBG")
    assert deck_legality.CODE_COMMANDER_PAIR in _pair_report(a, b).codes()


# ---------------------------------------------------------------------------
# 6. Bans — the Scryfall-backed replacement for the hardcoded list
# ---------------------------------------------------------------------------

# The 2026-02-09 B&R state for the cards the old hardcoded ``_CORE_BANS``
# set got wrong in each direction.
_FALSE_POSITIVES = [
    "Coalition Victory", "Panoptic Mirror", "Painter's Servant",
    "Worldfire", "Sway of the Stars", "Tempest Efreet",
]
_ACTUALLY_BANNED = [
    "Balance", "Fastbond", "Flash", "Golos, Tireless Pilgrim",
    "Griselbrand", "Karakas", "Leovold, Emissary of Trest",
    "Paradox Engine", "Rofellos, Llanowar Emissary", "Tolarian Academy",
]


def _ban_lookup():
    cards = [_card(n, legality="legal") for n in _FALSE_POSITIVES]
    cards += [_card(n, legality="banned") for n in _ACTUALLY_BANNED]
    return _lookup(*cards)


@pytest.mark.parametrize("name", _FALSE_POSITIVES)
def test_legal_cards_are_not_reported_as_banned(name):
    """Coalition Victory and Panoptic Mirror are on the *Game Changers*
    list — they are LEGAL. The old hardcoded set called them banned."""
    scan = scan_banned([name], lookup=_ban_lookup())
    assert scan is not None
    assert scan.banned == ()
    assert scan.unverified == ()


@pytest.mark.parametrize("name", _ACTUALLY_BANNED)
def test_banned_cards_are_reported(name):
    """Fastbond, Griselbrand and friends ARE banned and were missing
    from the old hardcoded set entirely."""
    scan = scan_banned([name], lookup=_ban_lookup())
    assert scan is not None
    assert scan.banned == (name,)


def test_scan_banned_separates_the_three_buckets():
    names = ["Fastbond", "Coalition Victory", "Some Custom Card"]
    scan = scan_banned(names, lookup=_ban_lookup())
    assert scan is not None
    assert scan.banned == ("Fastbond",)
    assert scan.unverified == ("Some Custom Card",)
    assert scan.checked == 3
    assert scan.outage is False


def test_scan_banned_dedupes_names():
    scan = scan_banned(
        ["Fastbond", "Fastbond", "fastbond"], lookup=_ban_lookup(),
    )
    assert scan is not None
    assert scan.banned == ("Fastbond",)
    assert scan.checked == 1


def test_scan_banned_reports_not_legal_separately():
    """``not_legal`` (Un-cards, Conspiracies) is illegal for a
    different reason than ``banned`` and must not be conflated."""
    scan = scan_banned(
        ["Chaos Confetti"],
        lookup=_lookup(_card("Chaos Confetti", legality="not_legal")),
    )
    assert scan is not None
    assert scan.banned == ()
    assert scan.not_in_format == ("Chaos Confetti",)


def test_banned_card_in_deck_is_a_violation():
    cmdr = _card("Test Commander",
                 type_line="Legendary Creature — Elder Dragon", ci="G")
    fastbond = _card("Fastbond", type_line="Enchantment", ci="G",
                     legality="banned")
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Fastbond")])
    report = validate_deck(deck, lookup=_lookup(cmdr, FOREST, fastbond))
    violation = [v for v in report.violations
                 if v.code == deck_legality.CODE_BANNED_CARD][0]
    assert violation.cards == ("Fastbond",)
    assert report.legal is False


def test_coalition_victory_in_deck_is_not_a_violation():
    cmdr = _card("Test Commander",
                 type_line="Legendary Creature — Elder Dragon", ci="G")
    victory = _card("Coalition Victory", type_line="Sorcery", ci="G",
                    legality="legal")
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Coalition Victory")])
    report = validate_deck(deck, lookup=_lookup(cmdr, FOREST, victory))
    assert report.violations == ()
    assert report.legal is True


# ---------------------------------------------------------------------------
# 7. Lutri — the companion carve-out Scryfall structurally cannot model
# ---------------------------------------------------------------------------

def test_lutri_in_the_99_is_legal_but_flagged_unverified():
    """Unbanned as a deck card on 2026-02-09, still banned as a
    COMPANION. A .dck has no companion slot, so we report, never
    accuse."""
    cmdr = _card("Test Commander",
                 type_line="Legendary Creature — Elder Dragon", ci="G")
    lutri = _card("Lutri, the Spellchaser",
                  type_line="Legendary Creature — Otter Wizard",
                  oracle="Flash\nCompanion — Your starting deck contains no "
                         "more than one of each nonland card.",
                  ci="G", legality="legal")
    deck = _deck(["Test Commander"],
                 [(98, "Forest"), (1, "Lutri, the Spellchaser")])
    report = validate_deck(deck, lookup=_lookup(cmdr, FOREST, lutri))
    assert report.violations == ()
    assert report.legal is True
    assert report.status == "unverified"
    codes = [v.code for v in report.unverified]
    assert deck_legality.CODE_LUTRI_COMPANION in codes
    note = [v for v in report.unverified
            if v.code == deck_legality.CODE_LUTRI_COMPANION][0]
    assert note.cards == ("Lutri, the Spellchaser",)
    assert "companion" in note.message.lower()


# ---------------------------------------------------------------------------
# Outage contract
# ---------------------------------------------------------------------------

def _boom(name: str):
    raise RuntimeError("scryfall is down")


def test_outage_never_claims_illegality():
    """Every lookup raising must degrade to "could not verify" —
    not to a deck full of fabricated violations."""
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Fastbond")])
    report = validate_deck(deck, lookup=_boom)
    assert report.violations == ()
    assert report.legal is True
    assert report.verified is False
    assert report.status == "unverified"
    assert report.lookup_failures > 0
    codes = {v.code for v in report.unverified}
    assert deck_legality.CODE_UNVERIFIED_COMMANDER in codes
    assert deck_legality.CODE_UNVERIFIED_COLOR_IDENTITY in codes
    assert deck_legality.CODE_UNVERIFIED_BANNED in codes


def test_outage_still_reports_textless_violations():
    """Deck size needs no card data, so it survives a total outage —
    the outage contract silences the checks that need Scryfall, not
    the ones that don't."""
    deck = _deck(["Test Commander"], [(50, "Forest")])
    report = validate_deck(deck, lookup=_boom)
    assert report.codes() == [deck_legality.CODE_DECK_SIZE]
    assert report.status == "illegal"


def test_unknown_card_returning_none_is_unverified_not_illegal():
    """A 404 (typo / custom card) is indistinguishable from an outage
    at this layer, and gets the same treatment."""
    deck = _deck(["Test Commander"], [(98, "Forest"), (1, "Nonexistent Card")])
    report = validate_deck(deck, lookup=_legal_lookup())
    assert report.violations == ()
    unverified_names = {n for v in report.unverified for n in v.cards}
    assert "Nonexistent Card" in unverified_names


def test_duplicate_of_unknown_card_is_unverified_not_illegal():
    """``2 Nazgûl`` and ``2 Sol Ring`` are indistinguishable without
    oracle text, and only one of them is illegal — so say nothing."""
    deck = _deck(["Test Commander"], [(97, "Forest"), (2, "Mystery Card")])
    report = validate_deck(deck, lookup=_legal_lookup())
    assert deck_legality.CODE_DUPLICATE_CARD not in report.codes()
    codes = {v.code for v in report.unverified}
    assert deck_legality.CODE_UNVERIFIED_SINGLETON in codes


def test_missing_legalities_field_is_unverified_not_legal():
    """A projected/slim cache snapshot without ``legalities`` must not
    read as "verified legal"."""
    slim = _card("Fastbond", type_line="Enchantment", ci="G")
    del slim["legalities"]
    scan = scan_banned(["Fastbond"], lookup=_lookup(slim))
    # One name, one unknown -> majority failure -> outage.
    assert scan is None


def test_scan_banned_returns_none_on_majority_failure():
    """deck_health's ``failed * 2 > total`` outage contract."""
    known = [_card(n, legality="legal") for n in ("Sol Ring", "Forest")]
    names = ["Sol Ring", "Forest", "Unknown A", "Unknown B", "Unknown C"]
    assert scan_banned(names, lookup=_lookup(*known)) is None


def test_scan_banned_tolerates_minority_failure():
    """Half-or-fewer misses are noise, not an outage — the scan is
    still returned, with the misses quarantined."""
    known = [_card(n, legality="legal")
             for n in ("Sol Ring", "Forest", "Island")]
    names = ["Sol Ring", "Forest", "Island", "Unknown A"]
    scan = scan_banned(names, lookup=_lookup(*known))
    assert scan is not None
    assert scan.unverified == ("Unknown A",)
    assert scan.outage is False


def test_ban_scan_outage_property_boundary():
    """Exactly half unknown is NOT an outage; one more is."""
    assert BanScan(unverified=("a", "b"), checked=4).outage is False
    assert BanScan(unverified=("a", "b", "c"), checked=4).outage is True
    assert BanScan().outage is False


def test_lookup_is_memoized_per_distinct_name():
    """A 98-Forest deck must not cost 98 lookups."""
    calls: list[str] = []
    inner = _legal_lookup()

    def _counting(name: str):
        calls.append(name)
        return inner(name)

    validate_deck(_legal_deck(), lookup=_counting)
    # One call per distinct name, no repeats across the four checks
    # that all want the same card data.
    assert len(calls) == len({c.lower() for c in calls})
    assert len(calls) <= 3


def test_status_prefers_illegal_over_unverified():
    """A confirmed violation beats an un-run check: the deck is
    illegal regardless of what we couldn't check."""
    cmdr = _card("Test Commander",
                 type_line="Legendary Creature — Elder Dragon", ci="G")
    fastbond = _card("Fastbond", type_line="Enchantment", ci="G",
                     legality="banned")
    deck = _deck(["Test Commander"],
                 [(97, "Forest"), (1, "Fastbond"), (1, "Mystery Card")])
    report = validate_deck(deck, lookup=_lookup(cmdr, FOREST, fastbond))
    assert report.unverified  # Mystery Card couldn't be checked
    assert report.legal is False
    assert report.status == "illegal"


# ---------------------------------------------------------------------------
# Field-shape regressions (what we rely on Scryfall projecting)
# ---------------------------------------------------------------------------

def test_face_level_oracle_text_is_read():
    """MDFC / transform payloads carry text per face and often leave
    the parent's ``oracle_text`` empty."""
    dfc = _card("Weird Rats", type_line="", oracle="", ci="B")
    dfc["card_faces"] = [
        {"type_line": "Creature — Rat", "oracle_text": "A deck can have any "
         "number of cards named Weird Rats."},
        {"type_line": "Creature — Rat Horror", "oracle_text": "Menace"},
    ]
    assert copy_limit(dfc) is None


def test_face_level_type_line_makes_a_commander_eligible():
    dfc = _card("Flip Legend", type_line="", oracle="", ci="R")
    dfc["card_faces"] = [
        {"type_line": "Legendary Creature — Human", "oracle_text": ""},
        {"type_line": "Legendary Creature — Demon", "oracle_text": ""},
    ]
    assert deck_legality.is_eligible_commander(dfc) is True


def test_is_eligible_commander_unknown_on_missing_card():
    assert deck_legality.is_eligible_commander(None) is None


# ---------------------------------------------------------------------------
# /api/deck_audit — the route that used to carry the hardcoded ban set
# ---------------------------------------------------------------------------

flask = pytest.importorskip("flask")  # skip if [web] extra not installed


@pytest.fixture
def audit_client(tmp_path, monkeypatch):
    """Flask test client over a one-deck dir, Scryfall fully stubbed."""
    from commander_builder.web.app import create_app

    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    (deck_dir / "Banned.dck").write_text(
        _deck(
            ["Test Commander"],
            [(97, "Forest"), (1, "Fastbond"), (1, "Coalition Victory")],
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers",
        lambda **kw: {"Coalition Victory"},
    )
    stub = _lookup(
        GENERIC_CMDR,
        FOREST,
        _card("Fastbond", type_line="Enchantment", ci="G", legality="banned"),
        _card("Coalition Victory", type_line="Sorcery", ci="G",
              legality="legal"),
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: stub(name),
    )
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


def test_deck_audit_reports_only_real_bans(audit_client):
    resp = audit_client.get("/api/deck_audit?deck=Banned&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    # Fastbond is banned; Coalition Victory is a Game Changer, i.e. LEGAL.
    assert body["illegal_cards"] == ["Fastbond"]
    assert "Coalition Victory" not in body["illegal_cards"]
    assert body["in_deck_game_changers"] == ["Coalition Victory"]
    assert body["unverified_cards"] == []
    assert any("banned in Commander" in w for w in body["warnings"])


def test_deck_audit_outage_reports_nothing_as_banned(tmp_path, monkeypatch):
    """A dead Scryfall must not render as "0 banned cards" either —
    every name lands in ``unverified_cards`` with a warning."""
    from commander_builder.web.app import create_app

    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    (deck_dir / "Outage.dck").write_text(
        _deck(["Test Commander"], [(98, "Forest"), (1, "Fastbond")]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers", lambda **kw: set(),
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: (_ for _ in ()).throw(RuntimeError("down")),
    )
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    resp = app.test_client().get("/api/deck_audit?deck=Outage&bracket=3")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["illegal_cards"] == []
    assert sorted(body["unverified_cards"]) == [
        "Fastbond", "Forest", "Test Commander",
    ]
    assert any("could not be verified" in w for w in body["warnings"])
