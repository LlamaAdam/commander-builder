"""archetype v2 tests — oracle-signal classification.

REAL-ORACLE-FIXTURE STYLE. Every oracle string in ``ORACLES`` below was
copied from a live Scryfall response, ``\\n`` paragraph breaks and mana
symbols included — the same discipline ``tests/fixtures/real_oracles.py``
documents, and for the same reason: the 2026-05 audit found nine
classifier bugs that synthetic "close enough" oracle text hid. The stax
table in ``archetype.py`` is regex over prose; a paraphrase would test the
paraphrase, not the classifier.

The fixtures live HERE rather than in ``tests/fixtures/real_oracles.py``
because that module is shared with the role-classifier suites and is
owned by another change in flight; nothing below needs to be visible
outside this file.

Everything is OFFLINE: the lookup is a dict, injected either through
``derive_archetype_signals(lookup=...)`` or by monkeypatching
``archetype._cached_scryfall``. No test touches the network or the real
snapshot cache.
"""
from pathlib import Path

import pytest

from commander_builder import archetype
from commander_builder.archetype import (
    MIN_CONTENT_MATCHES,
    MIN_STAX_CARDS,
    MIN_TRIBAL_MATCHES,
    MIN_TUTORS_FOR_COMBO,
    _content_scan,
    _filename_hint,
    _read_main_card_names,
    classify,
    derive_archetype_signals,
    stax_categories,
)

# ---------------------------------------------------------------------------
# Oracle fixtures — verbatim Scryfall text.
# ---------------------------------------------------------------------------

ORACLES: dict[str, dict] = {
    # --- stax / resource denial -------------------------------------------
    "Winter Orb": {
        "oracle_text": (
            "As long as Winter Orb is untapped, players can't untap more "
            "than one land during their untap steps."
        ),
        "type_line": "Artifact",
        "cmc": 2.0,
    },
    "Static Orb": {
        "oracle_text": (
            "As long as Static Orb is untapped, players can't untap more "
            "than two permanents during their untap steps."
        ),
        "type_line": "Artifact",
        "cmc": 3.0,
    },
    "Stasis": {
        "oracle_text": (
            "Players skip their untap steps.\n"
            "At the beginning of your upkeep, sacrifice Stasis unless you "
            "pay {U}."
        ),
        "type_line": "Enchantment",
        "cmc": 2.0,
    },
    "Smokestack": {
        "oracle_text": (
            "At the beginning of your upkeep, you may put a soot counter "
            "on Smokestack.\n"
            "At the beginning of each player's upkeep, that player "
            "sacrifices a permanent for each soot counter on Smokestack."
        ),
        "type_line": "Artifact",
        "cmc": 4.0,
    },
    "Sphere of Resistance": {
        "oracle_text": "Spells cost {1} more to cast.",
        "type_line": "Artifact",
        "cmc": 2.0,
    },
    "Thorn of Amethyst": {
        "oracle_text": "Noncreature spells cost {1} more to cast.",
        "type_line": "Artifact",
        "cmc": 2.0,
    },
    "Ghostly Prison": {
        "oracle_text": (
            "Creatures can't attack you unless their controller pays {2} "
            "for each creature they control that's attacking you."
        ),
        "type_line": "Enchantment",
        "cmc": 3.0,
    },
    "Rule of Law": {
        "oracle_text": "Each player can't cast more than one spell each turn.",
        "type_line": "Enchantment",
        "cmc": 3.0,
    },
    "Drannith Magistrate": {
        "oracle_text": (
            "Your opponents can't cast spells from anywhere other than "
            "their hands."
        ),
        "type_line": "Creature — Human Wizard",
        "cmc": 2.0,
    },
    "Spirit of the Labyrinth": {
        "oracle_text": "Each player can't draw more than one card each turn.",
        "type_line": "Creature — Spirit",
        "cmc": 2.0,
    },
    # --- the two guard cases ----------------------------------------------
    # "unless that player pays" WITHOUT a prohibition — the Rhystic Study /
    # Mystic Remora template. Present in a large share of blue decks that
    # are in no sense stax.
    "Rhystic Study": {
        "oracle_text": (
            "Whenever an opponent casts a spell, you may draw a card "
            "unless that player pays {1}."
        ),
        "type_line": "Enchantment",
        "cmc": 3.0,
    },
    "Mystic Remora": {
        "oracle_text": (
            "Cumulative upkeep {1} (At the beginning of your upkeep, put "
            "an age counter on this permanent, then sacrifice it unless "
            "you pay its upkeep cost for each age counter on it.)\n"
            "Whenever an opponent casts a noncreature spell, you may draw "
            "a card unless that player pays {4}."
        ),
        "type_line": "Enchantment",
        "cmc": 1.0,
    },
    # Your-OWN-cost reducer: says "less", and is self-scoped. Must never
    # read as a tax.
    "Goblin Warchief": {
        "oracle_text": (
            "Goblin spells you cast cost {1} less to cast.\n"
            "Goblins you control have haste."
        ),
        "type_line": "Creature — Goblin Warrior",
        "cmc": 3.0,
    },
    # --- control ----------------------------------------------------------
    "Baral, Chief of Compliance": {
        "oracle_text": (
            "Instant and sorcery spells you cast cost {1} less to cast.\n"
            "Whenever a spell or ability an opponent controls is "
            "countered, draw a card, then discard a card."
        ),
        "type_line": "Legendary Creature — Human Wizard",
        "cmc": 2.0,
    },
    "Counterspell": {
        "oracle_text": "Counter target spell.",
        "type_line": "Instant",
        "cmc": 2.0,
    },
    "Negate": {
        "oracle_text": "Counter target noncreature spell.",
        "type_line": "Instant",
        "cmc": 2.0,
    },
    "Swan Song": {
        "oracle_text": (
            "Counter target enchantment, instant, or sorcery spell. Its "
            "controller creates a 2/2 blue Bird creature token with flying."
        ),
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Dovin's Veto": {
        "oracle_text": (
            "This spell can't be countered.\n"
            "Counter target noncreature spell."
        ),
        "type_line": "Instant",
        "cmc": 2.0,
    },
    "Arcane Denial": {
        "oracle_text": (
            "Counter target spell. Its controller may draw up to two "
            "cards at the beginning of the next turn's upkeep.\n"
            "You draw a card at the beginning of the next turn's upkeep."
        ),
        "type_line": "Instant",
        "cmc": 2.0,
    },
    "Dispel": {
        "oracle_text": "Counter target instant spell.",
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Wrath of God": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
        "cmc": 4.0,
    },
    "Damnation": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
        "cmc": 4.0,
    },
    "Toxic Deluge": {
        "oracle_text": (
            "As an additional cost to cast this spell, pay X life.\n"
            "All creatures get -X/-X until end of turn."
        ),
        "type_line": "Sorcery",
        "cmc": 3.0,
    },
    # --- combo ------------------------------------------------------------
    "Thassa's Oracle": {
        "oracle_text": (
            "When this creature enters, look at the top X cards of your "
            "library, where X is your devotion to blue. Put up to one of "
            "them on top of your library and the rest on the bottom of "
            "your library in a random order. If X is greater than or "
            "equal to the number of cards in your library, you win the "
            "game."
        ),
        "type_line": "Creature — Merfolk Wizard",
        "cmc": 2.0,
    },
    "Demonic Consultation": {
        "oracle_text": (
            "Name a card. Exile the top five cards of your library, then "
            "reveal cards from the top of your library until you exile "
            "the named card. Put all cards exiled this way except the "
            "named card into your graveyard, then shuffle."
        ),
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Kess, Dissident Mage": {
        "oracle_text": (
            "Flying\n"
            "During each of your turns, you may cast an instant or "
            "sorcery card from your graveyard. If a spell cast this way "
            "would be put into your graveyard, exile it instead."
        ),
        "type_line": "Legendary Creature — Human Wizard",
        "cmc": 5.0,
    },
    # --- tutors (names come from bracket_estimator._TUTOR_CARDS) ----------
    "Demonic Tutor": {
        "oracle_text": (
            "Search your library for a card, put that card into your "
            "hand, then shuffle."
        ),
        "type_line": "Sorcery",
        "cmc": 2.0,
    },
    "Vampiric Tutor": {
        "oracle_text": (
            "Search your library for a card, then shuffle and put that "
            "card on top. You lose 2 life."
        ),
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Mystical Tutor": {
        "oracle_text": (
            "Search your library for an instant or sorcery card, then "
            "shuffle and put that card on top."
        ),
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Diabolic Intent": {
        "oracle_text": (
            "As an additional cost to cast this spell, sacrifice a "
            "creature.\n"
            "Search your library for a card, put that card into your "
            "hand, then shuffle."
        ),
        "type_line": "Sorcery",
        "cmc": 2.0,
    },
    "Grim Tutor": {
        "oracle_text": (
            "Search your library for a card, put it into your hand, then "
            "shuffle. You lose 3 life."
        ),
        "type_line": "Sorcery",
        "cmc": 3.0,
    },
    # --- aggro / tribal ---------------------------------------------------
    "Krenko, Mob Boss": {
        "oracle_text": (
            "{T}: Create X 1/1 red Goblin creature tokens, where X is the "
            "number of Goblins you control."
        ),
        "type_line": "Legendary Creature — Goblin Warrior",
        "cmc": 4.0,
    },
    "Skirk Prospector": {
        "oracle_text": "Sacrifice a Goblin: Add {R}.",
        "type_line": "Creature — Goblin",
        "cmc": 1.0,
    },
    "Mogg War Marshal": {
        "oracle_text": (
            "Echo {1}{R} (At the beginning of your upkeep, if this came "
            "under your control since the beginning of your last upkeep, "
            "sacrifice it unless you pay its echo cost.)\n"
            "When this creature enters or dies, create a 1/1 red Goblin "
            "creature token."
        ),
        "type_line": "Creature — Goblin Warrior",
        "cmc": 2.0,
    },
    "Warren Instigator": {
        "oracle_text": (
            "Double strike\n"
            "Whenever this creature deals damage to a player, you may put "
            "a Goblin creature card from your hand onto the battlefield."
        ),
        "type_line": "Creature — Goblin Berserker",
        "cmc": 2.0,
    },
    "Legion Loyalist": {
        "oracle_text": (
            "Haste\n"
            "Battalion — Whenever this creature and at least two other "
            "creatures attack, creatures you control gain first strike "
            "and trample until end of turn and can't be blocked this turn "
            "by creatures with power 1 or less."
        ),
        "type_line": "Creature — Goblin",
        "cmc": 1.0,
    },
    "Pashalik Mons": {
        "oracle_text": (
            "Whenever this creature or another Goblin you control dies, "
            "this creature deals 1 damage to any target.\n"
            "{3}{R}, Sacrifice another Goblin: Create two 1/1 red Goblin "
            "creature tokens."
        ),
        "type_line": "Legendary Creature — Goblin Rogue",
        "cmc": 3.0,
    },
    "Conspicuous Snoop": {
        "oracle_text": (
            "Play with the top card of your library revealed.\n"
            "You may cast Goblin spells from the top of your library.\n"
            "As long as the top card of your library is a Goblin card "
            "with an activated ability, this creature has that ability."
        ),
        "type_line": "Creature — Goblin",
        "cmc": 2.0,
    },
    "Boggart Harbinger": {
        "oracle_text": (
            "When this creature enters, you may search your library for a "
            "Goblin card, reveal that card, then shuffle and put that "
            "card on top.\n"
            "Whenever a Goblin you control attacks, it gets +1/+0 until "
            "end of turn."
        ),
        "type_line": "Creature — Goblin Rogue",
        "cmc": 3.0,
    },
    "Mudbutton Torchrunner": {
        "oracle_text": (
            "When this creature dies, it deals 3 damage to any target."
        ),
        "type_line": "Creature — Goblin Warrior",
        "cmc": 3.0,
    },
    "Ember Hauler": {
        "oracle_text": (
            "{1}, Sacrifice this creature: It deals 2 damage to any target."
        ),
        "type_line": "Creature — Goblin",
        "cmc": 2.0,
    },
    # --- midrange goodstuff -----------------------------------------------
    "Atraxa, Praetors' Voice": {
        "oracle_text": (
            "Flying, vigilance, deathtouch, lifelink\n"
            "At the beginning of your end step, proliferate."
        ),
        "type_line": "Legendary Creature — Phyrexian Angel Horror",
        "cmc": 4.0,
    },
    "Sol Ring": {
        "oracle_text": "{T}: Add {C}{C}.",
        "type_line": "Artifact",
        "cmc": 1.0,
    },
    "Arcane Signet": {
        "oracle_text": (
            "{T}: Add one mana of any color in your commander's color "
            "identity."
        ),
        "type_line": "Artifact",
        "cmc": 2.0,
    },
    "Cultivate": {
        "oracle_text": (
            "Search your library for up to two basic land cards, reveal "
            "those cards, put one onto the battlefield tapped and the "
            "other into your hand, then shuffle."
        ),
        "type_line": "Sorcery",
        "cmc": 3.0,
    },
    "Kodama's Reach": {
        "oracle_text": (
            "Search your library for up to two basic land cards, reveal "
            "those cards, put one onto the battlefield tapped and the "
            "other into your hand, then shuffle."
        ),
        "type_line": "Sorcery",
        "cmc": 3.0,
    },
    "Swords to Plowshares": {
        "oracle_text": (
            "Exile target creature. Its controller gains life equal to "
            "its power."
        ),
        "type_line": "Instant",
        "cmc": 1.0,
    },
    "Beast Within": {
        "oracle_text": (
            "Destroy target permanent. Its controller creates a 3/3 green "
            "Beast creature token."
        ),
        "type_line": "Instant",
        "cmc": 3.0,
    },
    "Solemn Simulacrum": {
        "oracle_text": (
            "When this creature enters, search your library for a basic "
            "land card, put that card onto the battlefield tapped, then "
            "shuffle.\n"
            "When this creature dies, you may draw a card."
        ),
        "type_line": "Artifact Creature — Golem",
        "cmc": 4.0,
    },
    "Eternal Witness": {
        "oracle_text": (
            "When this creature enters, you may return target card from "
            "your graveyard to your hand."
        ),
        "type_line": "Creature — Human Shaman",
        "cmc": 3.0,
    },
    "Sun Titan": {
        "oracle_text": (
            "Vigilance\n"
            "Whenever this creature enters or attacks, you may return "
            "target permanent card with mana value 3 or less from your "
            "graveyard to the battlefield."
        ),
        "type_line": "Creature — Giant",
        "cmc": 6.0,
    },
    "Sylvan Library": {
        "oracle_text": (
            "At the beginning of your draw step, you may draw two "
            "additional cards. If you do, choose two cards in your hand "
            "drawn this turn. For each of those cards, pay 4 life unless "
            "you put that card on top of your library."
        ),
        "type_line": "Enchantment",
        "cmc": 2.0,
    },
    # --- lands ------------------------------------------------------------
    "Island": {
        "oracle_text": "({T}: Add {U}.)",
        "type_line": "Basic Land — Island",
        "cmc": 0.0,
    },
    "Mountain": {
        "oracle_text": "({T}: Add {R}.)",
        "type_line": "Basic Land — Mountain",
        "cmc": 0.0,
    },
    "Forest": {
        "oracle_text": "({T}: Add {G}.)",
        "type_line": "Basic Land — Forest",
        "cmc": 0.0,
    },
    "Plains": {
        "oracle_text": "({T}: Add {W}.)",
        "type_line": "Basic Land — Plains",
        "cmc": 0.0,
    },
    "Swamp": {
        "oracle_text": "({T}: Add {B}.)",
        "type_line": "Basic Land — Swamp",
        "cmc": 0.0,
    },
    "Command Tower": {
        "oracle_text": (
            "{T}: Add one mana of any color in your commander's color "
            "identity."
        ),
        "type_line": "Land",
        "cmc": 0.0,
    },
}


def _lookup(name: str):
    """The offline stand-in for the Scryfall disk cache."""
    return ORACLES.get(name)


def _deck_text(commander: list[str], main: list[str]) -> str:
    lines = ["[metadata]", "Name=Fixture", "[Commander]"]
    lines.extend(f"1 {c}" for c in commander)
    lines.append("[Main]")
    lines.extend(f"1 {c}" for c in main)
    return "\n".join(lines) + "\n"


def _write_dck(tmp_path, name: str, cards: list[str],
               commander: list[str] | None = None) -> Path:
    """Write a synthetic .dck with the given commander + main card names."""
    p = tmp_path / name
    p.write_text(
        _deck_text(commander or ["Test Commander"], cards), encoding="utf-8",
    )
    return p


# The five reference decklists. Each is a plausible skeleton of the
# archetype, not a legal 99 — every card resolves through ``ORACLES`` so
# oracle coverage is 100% and the signals are exercised, not the outage
# path (which has its own tests further down).

STAX_MAIN = [
    "Winter Orb", "Static Orb", "Stasis", "Smokestack",
    "Sphere of Resistance", "Thorn of Amethyst", "Ghostly Prison",
    "Rule of Law", "Drannith Magistrate", "Spirit of the Labyrinth",
    "Sol Ring", "Arcane Signet",
] + ["Plains"] * 12

CONTROL_MAIN = [
    "Counterspell", "Negate", "Swan Song", "Dovin's Veto",
    "Arcane Denial", "Dispel",
    "Wrath of God", "Damnation", "Toxic Deluge",
    "Sol Ring", "Arcane Signet",
] + ["Island"] * 16

COMBO_MAIN = [
    "Thassa's Oracle", "Demonic Consultation",
    "Sol Ring", "Arcane Signet", "Command Tower",
] + ["Island"] * 15

GOBLINS = [
    "Skirk Prospector", "Mogg War Marshal", "Warren Instigator",
    "Legion Loyalist", "Pashalik Mons", "Conspicuous Snoop",
    "Boggart Harbinger", "Mudbutton Torchrunner", "Ember Hauler",
    "Goblin Warchief",
]
AGGRO_MAIN = GOBLINS + ["Sol Ring"] + ["Mountain"] * 14

MIDRANGE_MAIN = [
    "Sol Ring", "Arcane Signet", "Cultivate", "Kodama's Reach",
    "Swords to Plowshares", "Beast Within", "Solemn Simulacrum",
    "Eternal Witness", "Sun Titan", "Sylvan Library",
    "Rhystic Study", "Mystic Remora", "Wrath of God", "Counterspell",
] + ["Forest"] * 14


# ---------------------------------------------------------------------------
# stax_categories — the new oracle table, card by card
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("card,expected", [
    ("Winter Orb", {"players_cant", "cant_untap"}),
    ("Static Orb", {"players_cant", "cant_untap"}),
    ("Stasis", {"skip_step"}),
    ("Smokestack", {"recurring_sac"}),
    ("Sphere of Resistance", {"cost_tax"}),
    ("Thorn of Amethyst", {"cost_tax"}),
    ("Ghostly Prison", {"pay_or_cant"}),
    ("Rule of Law", {"players_cant", "cant_cast"}),
    ("Drannith Magistrate", {"players_cant", "cant_cast"}),
    ("Spirit of the Labyrinth", {"players_cant", "draw_limit"}),
])
def test_stax_categories_fire_on_real_denial_wording(card, expected):
    assert stax_categories(ORACLES[card]["oracle_text"]) == expected


def test_stax_guard_rhystic_study_is_not_stax():
    """GUARD 2. "unless that player pays" is the Rhystic Study / Mystic
    Remora template — two of the most-played blue value cards in the
    format. Without the "can't" requirement the pattern would tag a large
    share of NON-stax decks."""
    assert stax_categories(ORACLES["Rhystic Study"]["oracle_text"]) == set()
    assert stax_categories(ORACLES["Mystic Remora"]["oracle_text"]) == set()


def test_stax_guard_own_cost_reducer_is_not_a_tax():
    """GUARD 1. "Goblin spells you cast cost {1} less to cast" is a
    payoff, the exact opposite of a Sphere of Resistance."""
    assert stax_categories(ORACLES["Goblin Warchief"]["oracle_text"]) == set()


def test_stax_guard_self_scoped_tax_is_not_a_lock():
    """The direction guard ("more", never "less") is not enough on its
    own: a card that taxes ONLY YOUR OWN spells is a drawback, not a
    lock, so the clause-scope guard drops it too."""
    assert stax_categories("Spells you cast cost {1} more to cast.") == set()
    # ... while the symmetric and opponent-scoped versions still fire.
    assert "cost_tax" in stax_categories("Spells cost {1} more to cast.")
    assert "cost_tax" in stax_categories(
        "Spells your opponents cast cost {1} more to cast."
    )


def test_stax_guard_one_shot_edict_is_not_recurring_sacrifice():
    """"Each player sacrifices a creature" on an ETB is removal
    (Fleshbag Marauder), not a Smokestack lock — only the upkeep-trigger
    template counts."""
    assert stax_categories(
        "When this creature enters, each player sacrifices a creature."
    ) == set()


def test_stax_categories_empty_for_blank_text():
    assert stax_categories("") == set()
    assert stax_categories(None) == set()


# ---------------------------------------------------------------------------
# derive_archetype_signals + classify — the five reference decks
# ---------------------------------------------------------------------------

def test_stasis_winter_orb_list_is_stax():
    signals = derive_archetype_signals(
        _deck_text(["Baral, Chief of Compliance"], STAX_MAIN), lookup=_lookup,
    )
    assert signals["oracle_available"] is True
    assert signals["stax_cards"] >= MIN_STAX_CARDS
    assert signals["label"] == "stax"


def test_counterspell_and_wipe_dense_list_is_control():
    signals = derive_archetype_signals(
        _deck_text(["Baral, Chief of Compliance"], CONTROL_MAIN),
        lookup=_lookup,
    )
    assert signals["stack_count"] == 6
    assert signals["wipe_count"] == 3
    assert signals["instant_share"] >= 0.5
    assert signals["stax_cards"] == 0
    assert signals["label"] == "control"


def test_game_ending_combo_list_is_combo():
    signals = derive_archetype_signals(
        _deck_text(["Kess, Dissident Mage"], COMBO_MAIN), lookup=_lookup,
    )
    assert signals["game_ending_combos"] >= 1
    assert signals["label"] == "combo"


def test_tutor_dense_list_is_combo_without_an_assembled_combo():
    """The second combo signal: no combo is actually PRESENT, but four
    tutors in a 99 means the deck is searching for something specific."""
    main = [
        "Demonic Tutor", "Vampiric Tutor", "Mystical Tutor",
        "Diabolic Intent", "Sol Ring",
    ] + ["Island"] * 15
    signals = derive_archetype_signals(
        _deck_text(["Kess, Dissident Mage"], main), lookup=_lookup,
    )
    assert signals["game_ending_combos"] == 0
    assert signals["tutors"] >= MIN_TUTORS_FOR_COMBO
    assert signals["label"] == "combo"


def test_stax_outranks_tutor_density():
    """A cEDH prison list runs tutors too. Its lock pieces are the more
    specific evidence, so the ladder must not relabel it combo."""
    main = STAX_MAIN + [
        "Demonic Tutor", "Vampiric Tutor", "Mystical Tutor", "Grim Tutor",
    ]
    signals = derive_archetype_signals(
        _deck_text(["Baral, Chief of Compliance"], main), lookup=_lookup,
    )
    assert signals["tutors"] >= MIN_TUTORS_FOR_COMBO
    assert signals["label"] == "stax"


def test_tribal_curve_out_is_aggro():
    signals = derive_archetype_signals(
        _deck_text(["Krenko, Mob Boss"], AGGRO_MAIN), lookup=_lookup,
    )
    assert signals["tribal_type"] == "Goblin"
    assert signals["creature_share"] >= 0.30
    assert signals["label"] == "aggro"


def test_tribal_aggro_is_invisible_to_the_v1_name_scan():
    """THE REGRESSION V2 EXISTS FOR. None of these Goblin card NAMES
    contains a tribal noun, so the v1 scan sees nothing and the deck
    falls to "midrange". The commander's ORACLE text names the tribe
    outright, which is what v2 reads."""
    winner, _score = _content_scan(AGGRO_MAIN)
    assert winner is None, "the v1 name scan must still be blind here"
    signals = derive_archetype_signals(
        _deck_text(["Krenko, Mob Boss"], AGGRO_MAIN), lookup=_lookup,
    )
    assert signals["label"] == "aggro"


def test_low_curve_creature_dense_list_is_aggro_without_a_tribe():
    """The non-tribal aggro path: no tribe anywhere, but three quarters
    of the nonlands are creatures on a two-mana average curve."""
    main = (
        ["Skirk Prospector", "Legion Loyalist", "Ember Hauler",
         "Conspicuous Snoop", "Warren Instigator", "Mogg War Marshal"]
        + ["Sol Ring", "Arcane Signet"]
        + ["Mountain"] * 12
    )
    signals = derive_archetype_signals(
        _deck_text(["Atraxa, Praetors' Voice"], main), lookup=_lookup,
    )
    assert signals["tribal_type"] is None
    assert signals["creature_share"] >= 0.40
    assert signals["avg_cmc"] <= 2.8
    assert signals["label"] == "aggro"


def test_goodstuff_pile_stays_midrange():
    """The honest default. A goodstuff pile trips nothing: no combo, two
    ramp spells short of tutor density, one counterspell, one wipe, a
    27% creature share on a mid curve."""
    signals = derive_archetype_signals(
        _deck_text(["Atraxa, Praetors' Voice"], MIDRANGE_MAIN),
        lookup=_lookup,
    )
    assert signals["game_ending_combos"] == 0
    assert signals["tutors"] < MIN_TUTORS_FOR_COMBO
    assert signals["stax_cards"] == 0
    assert signals["stack_count"] == 1
    assert signals["creature_share"] < 0.40
    assert signals["label"] is None  # abstain -> classify() says midrange


# ---------------------------------------------------------------------------
# classify() — the full ladder, with the disk cache monkeypatched
# ---------------------------------------------------------------------------

@pytest.fixture
def offline_cache(monkeypatch):
    """Point ``archetype``'s disk-cache reader at the ORACLES fixture."""
    monkeypatch.setattr(archetype, "_cached_scryfall", _lookup)


@pytest.mark.parametrize("filename,commander,main,expected", [
    ("[USER] Mystery A [B4].dck", "Baral, Chief of Compliance",
     STAX_MAIN, "stax"),
    ("[USER] Mystery B [B4].dck", "Baral, Chief of Compliance",
     CONTROL_MAIN, "control"),
    ("[USER] Mystery C [B4].dck", "Kess, Dissident Mage",
     COMBO_MAIN, "combo"),
    ("[USER] Mystery D [B3].dck", "Krenko, Mob Boss",
     AGGRO_MAIN, "aggro"),
    ("[USER] Mystery E [B3].dck", "Atraxa, Praetors' Voice",
     MIDRANGE_MAIN, "midrange"),
])
def test_classify_end_to_end(tmp_path, offline_cache, filename, commander,
                             main, expected):
    """Neutral filenames throughout — every label below is earned by the
    oracle rung, not by the deck's name."""
    p = _write_dck(tmp_path, filename, main, commander=[commander])
    assert classify(p) == expected


def test_classify_produces_diverse_labels_for_diverse_decks(tmp_path,
                                                            offline_cache):
    """The pool-diversity precondition, stated as a test: five genuinely
    different decks with neutral filenames must get five different
    labels. Under v1 all five came back "midrange", which is what made
    ``pool_curator._slice_violates`` fire on every arrangement."""
    decks = [
        ("Baral, Chief of Compliance", STAX_MAIN),
        ("Baral, Chief of Compliance", CONTROL_MAIN),
        ("Kess, Dissident Mage", COMBO_MAIN),
        ("Krenko, Mob Boss", AGGRO_MAIN),
        ("Atraxa, Praetors' Voice", MIDRANGE_MAIN),
    ]
    labels = [
        classify(_write_dck(tmp_path, f"[USER] Deck {i} [B3].dck", main,
                            commander=[cmdr]))
        for i, (cmdr, main) in enumerate(decks)
    ]
    assert len(set(labels)) == 5, labels


# --- the degradation path (cold snapshot cache) ----------------------------

@pytest.fixture
def cold_cache(monkeypatch):
    """Every lookup misses — the fresh-checkout / cold-cache case."""
    monkeypatch.setattr(archetype, "_cached_scryfall", lambda name: None)


def test_cold_cache_degrades_to_the_name_scan(tmp_path, cold_cache):
    """No oracle data at all: the oracle rung abstains and v1's card-NAME
    scan takes over. A loudly-named stax list is still stax."""
    p = _write_dck(tmp_path, "[USER] Mystery [B4].dck", STAX_MAIN,
                   commander=["Baral, Chief of Compliance"])
    signals = derive_archetype_signals(
        _deck_text(["Baral, Chief of Compliance"], STAX_MAIN),
        lookup=lambda n: None,
    )
    assert signals["oracle_available"] is False
    # None, not 0: "we couldn't read the deck" and "the deck has no lock
    # pieces" are opposite conclusions.
    assert signals["stax_cards"] is None
    assert signals["label"] is None
    assert classify(p) == "stax"  # ... via _content_scan


def test_cold_cache_then_midrange(tmp_path, cold_cache):
    p = _write_dck(tmp_path, "[USER] Mystery [B3].dck", MIDRANGE_MAIN,
                   commander=["Atraxa, Praetors' Voice"])
    assert classify(p) == "midrange"


def test_cold_cache_still_finds_name_based_combos(tmp_path, cold_cache):
    """Combo detection and tutor density are NAME matches — they need no
    oracle text, so they must keep working on a cold cache."""
    signals = derive_archetype_signals(
        _deck_text(["Kess, Dissident Mage"], COMBO_MAIN),
        lookup=lambda n: None,
    )
    assert signals["oracle_available"] is False
    assert signals["label"] == "combo"


def test_classify_never_raises_on_a_broken_lookup(tmp_path, monkeypatch):
    def _boom(_name):
        raise RuntimeError("snapshot cache exploded")
    monkeypatch.setattr(archetype, "_cached_scryfall", _boom)
    p = _write_dck(tmp_path, "[USER] Mystery [B3].dck", MIDRANGE_MAIN,
                   commander=["Atraxa, Praetors' Voice"])
    assert classify(p) == "midrange"


# ---------------------------------------------------------------------------
# Rung 1 — filename hints (unchanged contract)
# ---------------------------------------------------------------------------

def test_filename_hint_matches_combo():
    assert _filename_hint("[USER] Storm Combo [B4].dck") == "combo"
    assert _filename_hint("Hermit Druid Combo Pile [B4].dck") == "combo"


def test_filename_hint_matches_stax():
    assert _filename_hint("Stax Lockdown [B5].dck") == "stax"
    assert _filename_hint("Hatebear Brigade [B3].dck") == "stax"


def test_filename_hint_matches_aggro():
    assert _filename_hint("Goblin Tribal Aggro [B3].dck") == "aggro"


def test_filename_hint_returns_none_for_neutral_names():
    assert _filename_hint("My Deck v1 [B3].dck") is None
    assert _filename_hint("Atraxa Stuff [B4].dck") is None


def test_filename_hint_wins_over_oracle_signals(tmp_path, offline_cache):
    """Rung 1 stays first: a user who named the deck "Stax Prison" is
    telling us the strategy, and it costs one regex instead of 99 cache
    reads."""
    p = _write_dck(tmp_path, "Stax Prison [B5].dck", COMBO_MAIN,
                   commander=["Kess, Dissident Mage"])
    assert classify(p) == "stax"


# ---------------------------------------------------------------------------
# Rung 3 — the v1 card-NAME scan, kept verbatim
# ---------------------------------------------------------------------------
#
# ``bracket_estimator._derive_archetype`` imports ``_content_scan``
# directly (for deck text with no file on disk), so its ``(winner, score)``
# contract is public in practice and these tests pin it.

def test_read_main_card_names_strips_qty_and_set_suffix(tmp_path):
    p = _write_dck(tmp_path, "x.dck", ["Sol Ring|CMM|1", "Mana Crypt"])
    assert _read_main_card_names(p) == ["Sol Ring", "Mana Crypt"]


def test_read_main_card_names_skips_other_sections(tmp_path):
    p = tmp_path / "x.dck"
    p.write_text("[Commander]\n1 Foo\n[Sideboard]\n1 Bar\n[Main]\n1 Baz\n",
                 encoding="utf-8")
    assert _read_main_card_names(p) == ["Baz"]


def test_read_main_card_names_missing_file():
    assert _read_main_card_names(Path("/does/not/exist.dck")) == []


def test_content_scan_picks_combo_for_combo_pieces():
    cards = [
        "Thassa's Oracle", "Demonic Consultation", "Tainted Pact",
        "Sol Ring", "Mana Crypt",
    ]
    winner, score = _content_scan(cards)
    assert winner == "combo"
    assert score >= MIN_CONTENT_MATCHES


def test_content_scan_picks_stax_for_resource_denial():
    cards = [
        "Winter Orb", "Static Orb", "Stasis", "Sphere of Resistance",
        "Sol Ring",
    ]
    winner, _ = _content_scan(cards)
    assert winner == "stax"


def test_content_scan_returns_none_below_threshold():
    winner, score = _content_scan(["Thassa's Oracle", "Forest", "Plains"])
    assert winner is None
    assert score < MIN_CONTENT_MATCHES


def test_content_scan_handles_empty_input():
    assert _content_scan([]) == (None, 0)


def test_content_scan_goodstuff_with_few_tribal_nouns_is_not_aggro():
    cards = [
        "Shivan Dragon", "Serra Angel", "Omnath, Locus of Creation",
        "Solemn Simulacrum", "Sol Ring", "Arcane Signet", "Cultivate",
        "Swords to Plowshares", "Beast Within", "Farseek",
    ]
    winner, _score = _content_scan(cards)
    assert winner != "aggro"
    assert winner is None


def test_content_scan_true_tribal_deck_still_claims_aggro():
    cards = [f"Goblin Test Card {i}" for i in range(MIN_TRIBAL_MATCHES)]
    winner, score = _content_scan(cards)
    assert winner == "aggro"
    assert score >= MIN_TRIBAL_MATCHES


def test_content_scan_control_via_named_staples():
    cards = [
        "Cyclonic Rift", "Propaganda", "Ghostly Prison",
        "Teferi, Hero of Dominaria", "Sol Ring",
    ]
    winner, _score = _content_scan(cards)
    assert winner == "control"


# ---------------------------------------------------------------------------
# Rung 4 + robustness
# ---------------------------------------------------------------------------

def test_classify_handles_missing_file(tmp_path, offline_cache):
    """Must NOT crash on missing files — return midrange so the curator
    keeps running."""
    assert classify(tmp_path / "ghost.dck") == "midrange"


def test_classify_handles_empty_deck(tmp_path, offline_cache):
    p = tmp_path / "[USER] Empty [B3].dck"
    p.write_text("", encoding="utf-8")
    assert classify(p) == "midrange"


def test_classify_picks_aggro_from_filename(tmp_path, offline_cache):
    p = _write_dck(tmp_path, "[USER] Voltron Brawler [B3].dck",
                   ["Sol Ring", "Forest"])
    assert classify(p) == "aggro"


def test_derive_signals_never_raises_on_garbage(tmp_path):
    for junk in ("", "\x00 not a deck", "[Main]\n"):
        signals = derive_archetype_signals(junk, lookup=lambda n: None)
        assert signals["label"] is None


def test_llm_stubs_are_gone():
    """v2 classifies offline from oracle signals, which is what the
    Claude/Ollama stubs were a placeholder for. Removing them (rather
    than leaving two functions that only ever raise) is part of the
    change; this pins that they stay gone."""
    assert not hasattr(archetype, "claude_archetype")
    assert not hasattr(archetype, "ollama_archetype")


# ---------------------------------------------------------------------------
# Cold-cache disclosure (round-2 review 2026-08-20, R2-P12)
# ---------------------------------------------------------------------------
#
# The finding: on a cold snapshot cache the oracle rungs abstain, decks
# land on "midrange" via the name scan, and ``pool_curator``'s diversity
# check trusts that answer with no way to tell it from a measured one.
# The fix is disclosure, not a behavior change — these tests pin BOTH
# halves (the warning fires; the labels are unchanged).

@pytest.fixture
def rearm_cold_warning():
    """The warning is once-per-process; re-arm around each test so
    ordering can't make one of them pass for the wrong reason."""
    archetype.reset_cold_cache_warning()
    yield
    archetype.reset_cold_cache_warning()


def _cold_cache(monkeypatch):
    """Every lookup misses — the fresh-machine / misconfigured
    MTG_CARDS_DIR case."""
    monkeypatch.setattr(archetype, "_cached_scryfall", lambda name: None)


def test_cold_cache_classify_warns_once_per_process(
        tmp_path, monkeypatch, capsys, rearm_cold_warning):
    _cold_cache(monkeypatch)
    decks = [
        _write_dck(tmp_path, f"[USER] Mystery {i} [B4].dck",
                   ["Sol Ring", "Arcane Signet", "Command Tower"])
        for i in range(3)
    ]
    labels = [classify(p) for p in decks]

    # Behavior unchanged: still the honest v1 fallback.
    assert labels == ["midrange", "midrange", "midrange"]

    err = capsys.readouterr().err
    assert err.count("[archetype] WARN") == 1, (
        "expected exactly one cold-cache disclosure for the batch, got:\n"
        f"{err}"
    )
    # The message has to name the cause AND the remedy — a warning the
    # operator can't act on is noise.
    assert "coverage" in err
    assert "MTG_CARDS_DIR" in err


def test_warm_cache_classify_stays_silent(
        tmp_path, offline_cache, capsys, rearm_cold_warning):
    """A deck whose cards all resolve prints nothing, whatever label it
    gets — the warning must mean 'blind', not 'midrange'."""
    p = _write_dck(tmp_path, "[USER] Mystery E [B3].dck", MIDRANGE_MAIN,
                   commander=["Atraxa, Praetors' Voice"])
    assert classify(p) == "midrange"
    assert "[archetype] WARN" not in capsys.readouterr().err


def test_cold_cache_label_from_name_rung_still_warns(
        tmp_path, monkeypatch, capsys, rearm_cold_warning):
    """The name scan CAN produce a label on a cold cache (rung 3). That
    label is still name-derived, so the disclosure fires."""
    _cold_cache(monkeypatch)
    p = _write_dck(
        tmp_path, "[USER] Mystery Z [B4].dck",
        ["Cyclonic Rift", "Propaganda", "Ghostly Prison",
         "Teferi, Hero of Dominaria", "Sol Ring"],
    )
    assert classify(p) == "control"
    assert "[archetype] WARN" in capsys.readouterr().err


def test_reset_cold_cache_warning_re_arms(
        tmp_path, monkeypatch, capsys, rearm_cold_warning):
    """A long-lived process (the web app) can re-arm the disclosure per
    batch instead of getting one line for its whole lifetime."""
    _cold_cache(monkeypatch)
    p = _write_dck(tmp_path, "[USER] Mystery Y [B4].dck", ["Sol Ring"])
    classify(p)
    capsys.readouterr()
    classify(p)
    assert "[archetype] WARN" not in capsys.readouterr().err
    archetype.reset_cold_cache_warning()
    classify(p)
    assert "[archetype] WARN" in capsys.readouterr().err


def test_oracle_scan_with_coverage_reports_blindness():
    """The seam classify branches on: cold lookup -> (None, False);
    a readable deck -> the flag is True."""
    from commander_builder.archetype import _oracle_scan_with_coverage
    label, available = _oracle_scan_with_coverage(
        _deck_text(["Test Commander"], ["Sol Ring", "Forest"]),
        lambda name: None,
    )
    assert (label, available) == (None, False)

    label, available = _oracle_scan_with_coverage(
        _deck_text(["Baral, Chief of Compliance"], STAX_MAIN), _lookup,
    )
    assert available is True
    assert label == "stax"
