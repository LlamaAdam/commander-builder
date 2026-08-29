"""Tests for the deck-health signals module.

These signals feed the audit panel's "Deck Health" tile row. Each is
a deck-construction quality metric not previously surfaced by the
advisor:

  - MDFC count (modal double-faced lands)
  - Spell density (non-permanent ratio)
  - Mana sink count (X-cost spells)
  - Wincon-specific protection (Silence / Veil of Summer / Grand
    Abolisher / Defense Grid / Pact of Negation / Force of Will / ...)
  - Self-mill enablement (Stitcher's Supplier / Satyr Wayfinder /
    Mesmeric Orb / Hermit Druid / ...)

Hardcoded-list signals (MDFC / wincon / self-mill) test by named
membership; type-based signals (spell density / mana sinks) test by
mocking ``scryfall_client.lookup_card`` so the suite stays hermetic.
"""
from __future__ import annotations

import pytest

from commander_builder import deck_health


# ---------------------------------------------------------------------------
# _iter_main_cards -- parse [Main] section into (qty, name) tuples
# ---------------------------------------------------------------------------

def test_iter_main_cards_extracts_qty_and_name():
    """Parser yields one tuple per line, quantity preserved, casing
    intact, edition tail stripped."""
    deck = (
        "[metadata]\nName=X\n"
        "[Commander]\n1 Test Commander\n"
        "[Main]\n"
        "27 Mountain|EXP|123\n"
        "1 Sol Ring|CLB|871\n"
        "1 Cultivate\n"
    )
    cards = list(deck_health._iter_main_cards(deck))
    assert cards == [
        (27, "Mountain"),
        (1, "Sol Ring"),
        (1, "Cultivate"),
    ]


def test_iter_main_cards_skips_commander_and_metadata():
    """Only [Main] section yields cards. Commander, metadata,
    sideboard sections are ignored."""
    deck = (
        "[metadata]\nName=X\nProtect=Sol Ring\n"  # has '=' but not in [Main]
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Cultivate\n"
        "[Sideboard]\n1 NotCounted\n"
    )
    cards = list(deck_health._iter_main_cards(deck))
    assert cards == [(1, "Cultivate")]


def test_iter_main_cards_handles_empty_deck():
    """Empty deck text returns no cards (defensive)."""
    assert list(deck_health._iter_main_cards("")) == []


# ---------------------------------------------------------------------------
# MDFC count
# ---------------------------------------------------------------------------

def test_count_mdfc_lands_finds_known_mdfcs():
    """The hardcoded MDFC list is the source of truth. A deck with
    several MDFCs from the Kamigawa Channel cycle + Zendikar
    Rising lands is detected."""
    deck = (
        "[Main]\n"
        "1 Boseiju, Who Endures\n"
        "1 Otawara, Soaring City\n"
        "1 Takenuma, Abandoned Mire\n"
        "1 Bala Ged Recovery\n"
        "1 Sol Ring\n"          # not an MDFC
        "1 Lightning Bolt\n"    # not an MDFC
    )
    result = deck_health.count_mdfc_lands(deck)
    assert result["count"] == 4
    assert "Boseiju, Who Endures" in result["cards"]
    assert "Bala Ged Recovery" in result["cards"]
    # Non-MDFCs not listed.
    assert "Sol Ring" not in result["cards"]


def test_count_mdfc_lands_includes_skyclave_cleric():
    """Skyclave Cleric IS a ZNR MDFC (back face: Skyclave Basilica).

    Regression: it was wrongly listed in the not-MDFC filter set inside
    deck_health.py, so decks running it under-counted their MDFC land
    equivalents. True non-MDFCs from the same curation pass (Felidar
    Retreat) must stay excluded.
    """
    deck = (
        "[Main]\n"
        "1 Skyclave Cleric\n"
        "1 Felidar Retreat\n"   # genuinely not an MDFC — stays filtered
    )
    result = deck_health.count_mdfc_lands(deck)
    assert result["count"] == 1
    assert result["cards"] == ["Skyclave Cleric"]


def test_count_mdfc_lands_zero_when_none_present():
    """Deck with no MDFCs returns count=0 and empty card list."""
    deck = "[Main]\n1 Sol Ring\n1 Cultivate\n27 Mountain\n"
    result = deck_health.count_mdfc_lands(deck)
    assert result["count"] == 0
    assert result["cards"] == []


def test_count_mdfc_lands_case_insensitive_match():
    """Casing in the .dck file shouldn't matter -- ``boseiju, who
    endures`` (lowercase) still matches."""
    deck = "[Main]\n1 boseiju, who endures\n1 OTAWARA, SOARING CITY\n"
    result = deck_health.count_mdfc_lands(deck)
    assert result["count"] == 2


def test_count_mdfc_lands_deduplicates_in_card_list():
    """If two MDFC lines exist for the same card (rare -- different
    printings), card list shows the card once but quantity sums."""
    deck = (
        "[Main]\n"
        "1 Boseiju, Who Endures|NEO|266\n"
        "1 Boseiju, Who Endures|NEO|266p\n"  # different collector
    )
    result = deck_health.count_mdfc_lands(deck)
    assert result["count"] == 2
    assert result["cards"] == ["Boseiju, Who Endures"]  # one entry


# ---------------------------------------------------------------------------
# Wincon-specific protection
# ---------------------------------------------------------------------------

def test_count_wincon_protection_finds_silence_class_cards():
    """Silence-class cards (Silence, Orim's Chant, Grand Abolisher,
    City of Solitude, Dosan, Defense Grid) are wincon-specific
    protection: they prevent interaction during a combo turn."""
    deck = (
        "[Main]\n"
        "1 Silence\n"
        "1 Grand Abolisher\n"
        "1 Defense Grid\n"
        "1 Sol Ring\n"  # generic ramp, not protection
    )
    result = deck_health.count_wincon_protection(deck)
    assert result["count"] == 3
    assert set(result["cards"]) == {"Silence", "Grand Abolisher", "Defense Grid"}


def test_count_wincon_protection_finds_pact_and_force_class():
    """Free-mana counterspells (Pact of Negation, Force of Will,
    Force of Negation, Mindbreak Trap, Flusterstorm) are the
    blue-flavored wincon protection."""
    deck = (
        "[Main]\n"
        "1 Pact of Negation\n"
        "1 Force of Will\n"
        "1 Mindbreak Trap\n"
        "1 Counterspell\n"   # not wincon-specific -- generic counter
    )
    result = deck_health.count_wincon_protection(deck)
    # Pact, FoW, Mindbreak Trap = 3. Counterspell not in our list.
    assert result["count"] == 3


def test_count_wincon_protection_finds_green_anti_counter():
    """Veil of Summer / Autumn's Veil / Allosaurus Shepherd /
    Vexing Shusher are the green-flavored answers to counterspells
    on a combo turn."""
    deck = (
        "[Main]\n"
        "1 Veil of Summer\n"
        "1 Autumn's Veil\n"
        "1 Allosaurus Shepherd\n"
        "1 Vexing Shusher\n"
    )
    result = deck_health.count_wincon_protection(deck)
    assert result["count"] == 4


def test_count_wincon_protection_zero_for_pure_value_deck():
    """A deck full of value/ramp cards but no protection returns 0.
    Real B4 combo decks NEED protection; this signal flags decks
    where the wincon is brittle."""
    deck = (
        "[Main]\n"
        "1 Sol Ring\n"
        "1 Cultivate\n"
        "1 Phyrexian Arena\n"
        "1 Krenko, Mob Boss\n"
    )
    result = deck_health.count_wincon_protection(deck)
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Self-mill enablement
# ---------------------------------------------------------------------------

def test_count_self_mill_enablers_finds_classic_enablers():
    """Stitcher's Supplier / Satyr Wayfinder / Mesmeric Orb /
    Hermit Druid are the standard self-mill enabler suite."""
    deck = (
        "[Main]\n"
        "1 Stitcher's Supplier\n"
        "1 Satyr Wayfinder\n"
        "1 Mesmeric Orb\n"
        "1 Hermit Druid\n"
        "1 Lightning Bolt\n"  # not self-mill
    )
    result = deck_health.count_self_mill_enablers(deck)
    assert result["count"] == 4
    assert "Stitcher's Supplier" in result["cards"]
    assert "Hermit Druid" in result["cards"]


def test_count_self_mill_enablers_finds_tutor_class():
    """Buried Alive and Entomb are graveyard tutors -- they put
    SPECIFIC cards in the graveyard. Distinct from random self-mill
    but functionally the same role (graveyard FUEL)."""
    deck = "[Main]\n1 Buried Alive\n1 Entomb\n"
    result = deck_health.count_self_mill_enablers(deck)
    assert result["count"] == 2


def test_count_self_mill_enablers_excludes_payoffs():
    """The signal counts ENABLERS (cards that put cards in your
    graveyard), not PAYOFFS (cards that read 'while in graveyard'
    or reanimate). Lord of Extinction is a payoff that grows with
    graveyard size; should NOT count."""
    deck = "[Main]\n1 Lord of Extinction\n1 Living Death\n"
    result = deck_health.count_self_mill_enablers(deck)
    # Neither is in our enabler list.
    assert result["count"] == 0


def test_count_self_mill_enablers_zero_for_aggro_deck():
    """A creature-aggro deck with no graveyard plan returns 0.
    Combined with the theme detector, the UI can warn 'you have 12
    graveyard payoffs but 0 enablers'."""
    deck = (
        "[Main]\n"
        "1 Goblin Lackey\n"
        "1 Skirk Prospector\n"
        "1 Krenko, Mob Boss\n"
    )
    result = deck_health.count_self_mill_enablers(deck)
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Spell density -- requires Scryfall type_line
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_lookup(monkeypatch):
    """Patch scryfall_client.lookup_card with a small in-memory map.
    Tests use the canonical type_line strings Scryfall returns."""
    # Default: every card is a creature unless overridden. Tests
    # override per-name via the returned dict's mutability.
    types: dict[str, str] = {}

    def _fake(name, **_kw):
        type_line = types.get(name.lower())
        if type_line is None:
            return None
        return {
            "name": name,
            "type_line": type_line,
            "mana_cost": "",
        }

    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    return types


def test_compute_spell_density_counts_instants_and_sorceries(fake_lookup):
    """Spells with type ``Instant`` or ``Sorcery`` are non-permanent.
    The ratio is non_permanent / total."""
    fake_lookup["lightning bolt"] = "Instant"
    fake_lookup["wrath of god"] = "Sorcery"
    fake_lookup["sol ring"] = "Artifact"
    fake_lookup["forest"] = "Basic Land — Forest"

    deck = (
        "[Main]\n"
        "1 Lightning Bolt\n"
        "1 Wrath of God\n"
        "1 Sol Ring\n"
        "1 Forest\n"
    )
    result = deck_health.compute_spell_density(deck)
    assert result["non_permanent_count"] == 2
    assert result["total_main_count"] == 4
    assert result["ratio"] == 0.5


def test_compute_spell_density_handles_quantities(fake_lookup):
    """``27 Mountain`` contributes 27 to total_main_count, not 1.
    Same for non-permanent quantities (rare for spells but possible
    for tokens / multi-printing setups)."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["lightning bolt"] = "Instant"
    deck = (
        "[Main]\n"
        "27 Mountain\n"
        "1 Lightning Bolt\n"
    )
    result = deck_health.compute_spell_density(deck)
    assert result["non_permanent_count"] == 1
    assert result["total_main_count"] == 28


def test_compute_spell_density_returns_none_ratio_for_empty(fake_lookup):
    """Empty deck -- defensive case. ``ratio`` is None rather than
    zero or div-by-zero crash."""
    result = deck_health.compute_spell_density("[Main]\n")
    assert result["total_main_count"] == 0
    assert result["ratio"] is None


def test_compute_spell_density_partial_failure_uses_successful_subset(
    fake_lookup,
):
    """Cards Scryfall doesn't return (typo, custom card) still count in
    total_main_count, but the RATIO is computed from the cards that
    could be classified -- an unknown card must not silently count as
    'permanent'. The miss count is surfaced via lookup_failures so the
    UI can annotate the tile. (Half-or-fewer misses stay below the
    outage threshold; see the all-fail test below.)"""
    fake_lookup["lightning bolt"] = "Instant"
    # "Madeup Card" is not in fake_lookup -> lookup returns None.
    deck = "[Main]\n1 Lightning Bolt\n1 Madeup Card\n"
    result = deck_health.compute_spell_density(deck)
    assert result is not None  # 1 of 2 misses == half, NOT an outage
    assert result["non_permanent_count"] == 1
    assert result["total_main_count"] == 2
    # Ratio from the classified subset: 1 instant / 1 classified card.
    assert result["ratio"] == 1.0
    assert result["lookup_failures"] == 1


def test_compute_spell_density_returns_none_when_all_lookups_fail(
    monkeypatch,
):
    """Module contract: 'Scryfall unreachable -> the signal returns
    None instead of a misleading zero.' Pre-fix, an all-lookups-fail
    outage yielded ratio == 0.0 ('0% spells', warn styling) on a
    healthy deck."""
    def _boom(name, **_kw):
        raise ConnectionError("Scryfall down")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    deck = "[Main]\n1 Lightning Bolt\n1 Sol Ring\n1 Forest\n"
    assert deck_health.compute_spell_density(deck) is None


def test_compute_spell_density_healthy_path_has_no_failures(fake_lookup):
    """When every lookup succeeds the shape carries lookup_failures == 0
    and the ratio matches the classic full-deck computation."""
    fake_lookup["lightning bolt"] = "Instant"
    fake_lookup["sol ring"] = "Artifact"
    deck = "[Main]\n1 Lightning Bolt\n1 Sol Ring\n"
    result = deck_health.compute_spell_density(deck)
    assert result["ratio"] == 0.5
    assert result["lookup_failures"] == 0


# ---------------------------------------------------------------------------
# Mana sink count -- X-cost spell detection via Scryfall mana_cost
# ---------------------------------------------------------------------------

def test_count_mana_sinks_finds_x_cost_spells(fake_lookup, monkeypatch):
    """Cards with ``{X}`` in their mana_cost are mana sinks -- they
    scale to whatever excess mana the user has."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "mana_cost": {
                "genesis wave": "{X}{G}{G}{G}",
                "comet storm": "{X}{R}",
                "walking ballista": "{X}{X}",
                "lightning bolt": "{R}",  # not a sink
            }.get(name.lower(), ""),
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = (
        "[Main]\n"
        "1 Genesis Wave\n"
        "1 Comet Storm\n"
        "1 Walking Ballista\n"
        "1 Lightning Bolt\n"
    )
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 3
    assert "Genesis Wave" in result["cards"]
    assert "Lightning Bolt" not in result["cards"]


def test_count_mana_sinks_handles_mdfc_x_cost(monkeypatch):
    """MDFCs put their mana_cost on the front face in ``card_faces[0]``.
    A future Bala-Ged-Recovery-style X spell on the front face is
    still a mana sink even though the top-level mana_cost is empty."""
    def _fake(name, **_kw):
        if name.lower() == "hypothetical x mdfc":
            return {
                "name": name,
                "mana_cost": "",  # MDFCs have empty top-level mana_cost
                "card_faces": [
                    {"mana_cost": "{X}{X}{R}"},
                    {"mana_cost": ""},  # back face is land
                ],
            }
        return None
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Hypothetical X MDFC\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 1


def test_count_mana_sinks_zero_for_fixed_cost_deck(monkeypatch):
    """A deck full of fixed-cost spells has no late-game outlets ->
    will flood out at high mana counts. Signal should report 0."""
    def _fake(name, **_kw):
        return {"name": name, "mana_cost": "{R}", "type_line": "Instant"}
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Lightning Bolt\n1 Lava Spike\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 0


# ---------------------------------------------------------------------------
# Oracle-text activated-ability mana sinks (TIER-2.1 fix). The
# {X}-in-mana_cost heuristic misses Spikeshot Goblin's ``{R}: ...``,
# Inkmoth Nexus's ``{1}: ...``, and self-untap loops like Staff of
# Domination. Oracle text below is sourced verbatim from Scryfall
# (scryfall.com/search?q=!"<card name>").
# ---------------------------------------------------------------------------

def test_count_mana_sinks_finds_pure_mana_activated_ability(monkeypatch):
    """Spikeshot Goblin's ``{R}: deal 1 damage`` is a mana sink: pay
    {R} repeatedly for value. Missed by the {X}-cost heuristic because
    the printed mana_cost is the fixed ``{1}{R}``."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "mana_cost": "{1}{R}",
            "oracle_text": "{R}: Spikeshot Goblin deals 1 damage to any target.",
            "type_line": "Creature — Goblin",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Spikeshot Goblin\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 1
    assert "Spikeshot Goblin" in result["cards"]


def test_count_mana_sinks_finds_manland_activation(monkeypatch):
    """Inkmoth Nexus's ``{1}: Inkmoth Nexus becomes a 1/1 [...]`` is a
    sink: in long games you keep pumping mana into manland activations
    plus combat damage."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "mana_cost": "",
            "oracle_text": (
                "{T}: Add {C}.\n"
                "{1}: Inkmoth Nexus becomes a 1/1 Phyrexian Insect "
                "artifact creature with flying and infect until end "
                "of turn. It's still a land."
            ),
            "type_line": "Land",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Inkmoth Nexus\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 1
    assert "Inkmoth Nexus" in result["cards"]


def test_count_mana_sinks_finds_self_untap_loop(monkeypatch):
    """Staff of Domination loops via the ``{5}, {T}: Untap Staff of
    Domination.`` clause: arbitrary mana can be poured into the prior
    activations over a single turn, so it's a sink even though every
    individual ability has ``{T}`` in its cost."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "mana_cost": "{5}",
            "oracle_text": (
                "{1}, {T}: You gain 1 life.\n"
                "{2}, {T}: Untap up to two target creatures.\n"
                "{3}, {T}: Draw a card.\n"
                "{4}, {T}: Each opponent loses 1 life.\n"
                "{5}, {T}: Untap Staff of Domination."
            ),
            "type_line": "Artifact",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Staff of Domination\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 1
    assert "Staff of Domination" in result["cards"]


def test_count_mana_sinks_skips_tap_only_abilities(monkeypatch):
    """Activated abilities gated on ``{T}`` with no self-untap aren't
    sinks (they're once-per-turn). Sol Ring (tap for mana), Mind Stone
    (tap+mana+sac for one-shot draw), and Icy Manipulator (``{1}, {T}:
    Tap ...``) should NOT count."""
    cards = {
        "sol ring": {
            "name": "Sol Ring",
            "mana_cost": "{1}",
            "oracle_text": "{T}: Add {C}{C}.",
            "type_line": "Artifact",
        },
        "mind stone": {
            "name": "Mind Stone",
            "mana_cost": "{2}",
            "oracle_text": (
                "{T}: Add {C}.\n"
                "{1}, {T}, Sacrifice Mind Stone: Draw a card."
            ),
            "type_line": "Artifact",
        },
        "icy manipulator": {
            "name": "Icy Manipulator",
            "mana_cost": "{4}",
            "oracle_text": (
                "{1}, {T}: Tap target artifact, creature, or land."
            ),
            "type_line": "Artifact",
        },
    }

    def _fake(name, **_kw):
        return cards.get(name.lower())
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Sol Ring\n1 Mind Stone\n1 Icy Manipulator\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 0


def test_count_mana_sinks_does_not_double_count_x_spell_with_activation(monkeypatch):
    """Walking Ballista is both an X-cost spell AND has a ``{4}: ...``
    activation. Count it once, not twice."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "mana_cost": "{X}{X}",
            "oracle_text": (
                "Walking Ballista enters with X +1/+1 counters on it.\n"
                "{4}: Put a +1/+1 counter on Walking Ballista.\n"
                "Remove a +1/+1 counter from Walking Ballista: "
                "It deals 1 damage to any target."
            ),
            "type_line": "Artifact Creature — Construct",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Walking Ballista\n"
    result = deck_health.count_mana_sinks(deck)
    assert result["count"] == 1
    assert result["cards"] == ["Walking Ballista"]


def test_count_mana_sinks_returns_none_when_all_lookups_fail(monkeypatch):
    """Same outage contract as spell density: an all-lookups-fail
    Scryfall outage returns None, NOT {'count': 0} -- pre-fix the zero
    rendered as a warn-flavored 'no mana sinks' on unclassifiable
    decks."""
    def _boom(name, **_kw):
        raise ConnectionError("Scryfall down")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    deck = "[Main]\n1 Genesis Wave\n1 Walking Ballista\n"
    assert deck_health.count_mana_sinks(deck) is None


def test_count_mana_sinks_partial_failure_counts_successes(monkeypatch):
    """Half-or-fewer lookup misses stay below the outage threshold:
    the count comes from the cards that DID resolve, and the miss
    count is surfaced via lookup_failures."""
    def _fake(name, **_kw):
        if name.lower() == "madeup card":
            return None  # simulated single-card miss
        return {
            "name": name,
            "type_line": "Sorcery",
            "mana_cost": "{X}{G}{G}{G}",  # X-cost -> mana sink
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Genesis Wave\n1 Madeup Card\n"
    result = deck_health.count_mana_sinks(deck)
    assert result is not None  # 1 of 2 misses == half, NOT an outage
    assert result["count"] == 1
    assert result["cards"] == ["Genesis Wave"]
    assert result["lookup_failures"] == 1


# ---------------------------------------------------------------------------
# compute_deck_health -- the aggregator
# ---------------------------------------------------------------------------

def test_compute_deck_health_returns_all_five_signals(monkeypatch):
    """The audit endpoint relies on all 5 keys being present in the
    output even if the deck has zero of each signal. UI tile row
    iterates known keys and would crash on missing entries."""
    def _fake(name, **_kw):
        return {"name": name, "type_line": "Creature", "mana_cost": "{1}"}
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = "[Main]\n1 Goblin Recruiter\n"
    result = deck_health.compute_deck_health(deck)
    assert set(result.keys()) == {
        "mdfc", "spell_density", "mana_sinks",
        "wincon_protection", "self_mill", "role_targets", "consistency",
        "consistency_targets",
    }
    # Each signal has its expected shape.
    assert "count" in result["mdfc"]
    assert "cards" in result["mdfc"]
    assert "ratio" in result["spell_density"]


def test_compute_deck_health_realistic_deck_signals(monkeypatch):
    """End-to-end on a realistic deck shape: mix of MDFCs + a wincon
    protection card + a self-mill enabler + a fixed-cost spell.
    Verifies the aggregator wires through correctly."""
    def _fake(name, **_kw):
        return {
            "name": name,
            "type_line": {
                "sol ring": "Artifact",
                "genesis wave": "Sorcery",
            }.get(name.lower(), "Creature"),
            "mana_cost": {
                "genesis wave": "{X}{G}{G}{G}",
            }.get(name.lower(), "{1}"),
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    deck = (
        "[Main]\n"
        "1 Boseiju, Who Endures\n"     # MDFC
        "1 Otawara, Soaring City\n"    # MDFC
        "1 Grand Abolisher\n"          # wincon protection
        "1 Stitcher's Supplier\n"      # self-mill enabler
        "1 Genesis Wave\n"             # mana sink (X cost)
        "1 Sol Ring\n"                 # plain artifact
    )
    health = deck_health.compute_deck_health(deck)
    assert health["mdfc"]["count"] == 2
    assert health["wincon_protection"]["count"] == 1
    assert health["self_mill"]["count"] == 1
    assert health["mana_sinks"]["count"] == 1
    # Spell density: Genesis Wave is the only Sorcery; 1 / 6 = 0.166
    assert health["spell_density"]["ratio"] == pytest.approx(1 / 6, abs=0.01)


# ---------------------------------------------------------------------------
# consistency_signal -- the wired opening-hand signal (2026-08)
# ---------------------------------------------------------------------------
#
# The consistency MODULE's math is pinned by tests/test_consistency.py;
# these tests pin the WIRING: the deck_health projection reproduces
# hand-checkable probabilities on a known deck, honors the None outage
# contract, and is strictly additive (old fields + grade untouched).

def _consistency_fake_lookup(monkeypatch):
    """Offline Scryfall stub for the consistency-signal tests. The
    consistency module never looks up basic Forests
    (staples.is_basic_land short-circuits) but the OTHER health signals
    do, so the stub classifies them properly; everything else is a
    cheap green sorcery, and the commander is a {1}{G} legend."""
    def _fake(name, **_kw):
        if name.lower() == "test commander":
            return {
                "name": name,
                "type_line": "Legendary Creature — Elf Druid",
                "mana_cost": "{1}{G}", "color_identity": ["G"],
                "oracle_text": "",
            }
        if "forest" in name.lower():
            return {
                "name": name, "type_line": "Basic Land — Forest",
                "mana_cost": "", "color_identity": ["G"],
                "oracle_text": "", "produced_mana": ["G"],
            }
        return {
            "name": name, "type_line": "Sorcery",
            "mana_cost": "{G}", "color_identity": ["G"],
            "oracle_text": "",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )


# 38-land / 61-spell canonical 99 -- the same shape the consistency
# module's own suite uses, so every probability below is hand-checkable
# against the closed-form hypergeometric.
_CONSISTENCY_DECK = (
    "[metadata]\nName=X\n"
    "[Commander]\n1 Test Commander\n"
    "[Main]\n38 Forest\n61 Green Ritual\n"
)


def test_consistency_signal_matches_hand_checked_probabilities(monkeypatch):
    """The wired signal reproduces the numbers the closed-form layer
    predicts for a 38-land 99: E[lands in 7] = 7*38/99 exactly, and
    p_keepable_7 = P(2 <= lands <= 5) = at_least(2) - at_least(6)."""
    from commander_builder.consistency import hypergeom_at_least

    _consistency_fake_lookup(monkeypatch)
    sig = deck_health.consistency_signal(_CONSISTENCY_DECK)
    assert sig is not None
    assert sig["avg_lands_in_7"] == pytest.approx(7 * 38 / 99, abs=0.1)
    closed = (
        hypergeom_at_least(99, 38, 7, 2) - hypergeom_at_least(99, 38, 7, 6)
    )
    assert sig["p_keepable_7"] == pytest.approx(closed, abs=0.04)
    # mulligan_rate is 1 - p_keepable_7 by definition under the module's
    # keep policy -- an exact identity, not a sampled one.
    assert sig["mulligan_rate"] == pytest.approx(
        1 - sig["p_keepable_7"], abs=1e-12,
    )
    # A mono-green 38-Forest manabase casts a {1}{G} commander on curve
    # nearly always, and can never be color screwed.
    assert sig["p_commander_on_curve"] > 0.8
    assert sig["p_color_screw"] == 0.0
    # Provenance fields the tile's tooltip renders.
    assert sig["convention"] == "on_play"
    assert sig["trials"] == deck_health._CONSISTENCY_TRIALS
    assert sig["seed"] == deck_health._CONSISTENCY_SEED
    assert sig["lookup_failures"] == 0


def test_consistency_signal_is_deterministic(monkeypatch):
    """Fixed seed at the call site: the same deck text yields the
    identical dict on every audit (the consistency module's regression
    requirement, honored by the wiring)."""
    _consistency_fake_lookup(monkeypatch)
    a = deck_health.consistency_signal(_CONSISTENCY_DECK)
    b = deck_health.consistency_signal(_CONSISTENCY_DECK)
    assert a == b


def test_consistency_signal_none_on_outage(monkeypatch):
    """The standard outage contract: a majority of card lines failing
    to resolve (or the lookup layer raising outright) degrades to None
    -- never a fabricated 0% keepable."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")),
    )
    deck = "[Main]\n1 Mystery A\n1 Mystery B\n1 Mystery C\n"
    assert deck_health.consistency_signal(deck) is None
    # Same through the aggregator: the key is present, its value None.
    assert deck_health.compute_deck_health(deck)["consistency"] is None


def test_consistency_signal_none_on_empty_deck(monkeypatch):
    _consistency_fake_lookup(monkeypatch)
    assert deck_health.consistency_signal("") is None


def test_compute_deck_health_consistency_is_strictly_additive(monkeypatch):
    """THE wiring contract: the new key rides alongside the existing
    signals without changing a single one of them, and the letter grade
    ignores it entirely (fold-in would silently re-grade every deck --
    see the module docstring's consistency bullet)."""
    _consistency_fake_lookup(monkeypatch)
    # staples binds lookup_card by value at import; patch its copy too
    # so role_target_report stays offline (same seam test_web_app uses).
    import commander_builder.staples as _staples
    monkeypatch.setattr(
        _staples, "lookup_card",
        lambda name, **_kw: {"name": name, "type_line": "Sorcery",
                             "mana_cost": "{G}", "oracle_text": ""},
    )
    health = deck_health.compute_deck_health(_CONSISTENCY_DECK)
    assert isinstance(health.get("consistency"), dict)
    # consistency_targets (FP-019.2) rides the same additive contract:
    # present, and equal to feeding the evaluator the SAME projection.
    assert health["consistency_targets"] == \
        deck_health._consistency_targets_signal(
            _CONSISTENCY_DECK, health["consistency"])
    legacy = {k: v for k, v in health.items()
              if k not in ("consistency", "consistency_targets")}
    # Every pre-wiring field equals the individually-computed signal --
    # the aggregator added a key, it did not touch the others.
    assert legacy == {
        "mdfc": deck_health.count_mdfc_lands(_CONSISTENCY_DECK),
        "spell_density": deck_health.compute_spell_density(_CONSISTENCY_DECK),
        "mana_sinks": deck_health.count_mana_sinks(_CONSISTENCY_DECK),
        "wincon_protection": deck_health.count_wincon_protection(
            _CONSISTENCY_DECK),
        "self_mill": deck_health.count_self_mill_enablers(_CONSISTENCY_DECK),
        "role_targets": deck_health._role_targets_signal(_CONSISTENCY_DECK),
    }
    # The grade is identical whether or not the health dict carries the
    # new key: consistency is reported, never graded.
    with_key = deck_health.compute_health_grade(
        _CONSISTENCY_DECK, health=health,
    )
    without_key = deck_health.compute_health_grade(
        _CONSISTENCY_DECK, health=legacy,
    )
    assert with_key == without_key


# ---------------------------------------------------------------------------
# compute_health_grade -- letter grade aggregating the signals
# ---------------------------------------------------------------------------
#
# The grade tests pass a synthetic ``health`` dict (compute_deck_health
# shape) directly so only the land-count walk needs the fake Scryfall
# lookup -- keeps each test hermetic and focused on the aggregation
# math, not the underlying signal detectors (tested above).

def _health_fixture(
    *,
    role_counts: dict | None = None,
    mana_sinks_count: int | None = 4,
    wincon_count: int = 3,
) -> dict:
    """Build a compute_deck_health-shaped dict. ``role_counts`` maps
    role -> count against staples.ROLE_TARGETS; default = every role
    exactly at target. ``mana_sinks_count=None`` simulates the Scryfall
    outage contract (signal is None)."""
    from commander_builder.staples import ROLE_TARGETS
    counts = dict(role_counts or {})
    roles = {
        role: {
            "count": counts.get(role, target),
            "target": target,
            "deficit": max(0, target - counts.get(role, target)),
        }
        for role, target in ROLE_TARGETS.items()
    }
    under = sorted(
        (r for r, v in roles.items() if v["deficit"] > 0),
        key=lambda r: roles[r]["deficit"], reverse=True,
    )
    return {
        "mdfc": {"count": 0, "cards": []},
        "spell_density": {
            "non_permanent_count": 0, "total_main_count": 99,
            "ratio": 0.0, "lookup_failures": 0,
        },
        "mana_sinks": (
            None if mana_sinks_count is None
            else {"count": mana_sinks_count, "cards": [], "lookup_failures": 0}
        ),
        "wincon_protection": {"count": wincon_count, "cards": []},
        "self_mill": {"count": 0, "cards": []},
        "role_targets": {"roles": roles, "under_built": under},
    }


# 37 lands (inside the 33-38 healthy band) + creatures filling to 99.
_BALANCED_DECK = "[Main]\n37 Mountain\n62 Grizzly Bears\n"


def test_health_grade_weights_sum_to_one():
    """The documented weighting dict must be a full partition of the
    score -- reweighting on unavailability divides by the available
    subset, so the full-availability case must sum to exactly 1.0."""
    assert sum(deck_health._GRADE_WEIGHTS.values()) == pytest.approx(1.0)


def test_health_grade_healthy_deck_gets_a(fake_lookup):
    """Every role at target + lands in band + sinks/protection at the
    UI's 'good' cutoff (>=3) -> perfect score, grade A.

    ``_BALANCED_DECK`` has no [Commander] section and its stub cards
    carry no color identity, so ``commander_alignment`` and the Karsten
    half of ``mana_health`` are both UNAVAILABLE here -- the score is
    computed over the three components that can be measured. That is the
    outage contract doing its job: a deck file we can't read a command
    zone out of must not be marked down for it."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK, health=_health_fixture(),
    )
    assert grade["grade"] == "A"
    assert grade["score"] == 100
    assert grade["reasons"] == []
    assert grade["components"]["commander_alignment"]["available"] is False
    assert all(
        grade["components"][name]["available"]
        for name in ("role_deficits", "mana_health", "construction_signals")
    )


def test_health_grade_missing_ramp_and_draw_reasons_first(fake_lookup):
    """A deck with zero ramp and zero draw (everything else healthy)
    drops to C, and the two engine-role deficits are the FIRST reasons
    -- they are the largest weighted point-losses, and the stable sort
    keeps ramp (ROLE_TARGETS order) ahead of the equal-severity draw."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK,
        health=_health_fixture(role_counts={"ramp": 0, "draw": 0}),
    )
    # role component: 100 * (1 - 20/38) = 47.37 (total_target is 38, not
    # 35, now that finisher:3 is a role target). Weighted over the three
    # AVAILABLE components (commander_alignment is unavailable for this
    # headless fixture, so its 0.10 is renormalized away):
    #   (0.40*47.37 + 0.22*100 + 0.28*100) / 0.90 = 77.5 -> 77.
    # Same 77 and the same C as before the rebalance: role_deficits kept
    # its 0.40 weight precisely so this calibration point held.
    assert grade["score"] == 77
    assert grade["grade"] == "C"
    assert "ramp" in grade["reasons"][0].lower()
    assert "draw" in grade["reasons"][1].lower()
    assert len(grade["reasons"]) == 2


def test_health_grade_land_shortage_reason_and_penalty(fake_lookup):
    """20 lands is 13 under the 33-38 band -> the land-count half of
    mana_health floors at 0 and the land-count reason surfaces first (it
    outweighs everything else when the rest of the deck is healthy)."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    deck = "[Main]\n20 Mountain\n79 Grizzly Bears\n"
    grade = deck_health.compute_health_grade(deck, health=_health_fixture())
    # The Karsten half is unavailable (stub cards have no color identity),
    # so mana_health is the land half alone -> 0, exactly as before.
    # (0.40*100 + 0.22*0 + 0.28*100) / 0.90 = 75.6 -> 76 (was 75 when the
    # denominator was a flat 1.00 and mana carried 0.25). Still C.
    assert grade["score"] == 76
    assert grade["grade"] == "C"
    assert "lands" in grade["reasons"][0]
    assert "below" in grade["reasons"][0]


def test_health_grade_unavailable_signal_excluded_from_denominator(
    monkeypatch,
):
    """The outage contract carries through to the grade: with the
    mana walk unavailable (all Scryfall lookups fail) and mana_sinks
    None, the grade is computed from the REMAINING components with
    weights renormalized -- pinned value, not dragged down by zeros."""
    def _boom(name, **_kw):
        raise ConnectionError("Scryfall down")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK,
        health=_health_fixture(mana_sinks_count=None, wincon_count=1),
    )
    # Available: role_deficits (100) + construction_signals (wincon
    # only, count 1 -> neutral 70). Both mana halves and the commander
    # lookup are dead in the outage. Reweighted over 0.40+0.28:
    # (0.40*100 + 0.28*70) / 0.68 = 87.6 -> 88 (was 86 when construction
    # carried 0.35 of a 0.75 denominator -- the same two components, a
    # slightly larger role share of the surviving weight).
    assert grade["score"] == 88
    assert grade["grade"] == "B"
    assert grade["components"]["mana_health"]["available"] is False
    assert grade["components"]["mana_health"]["score"] is None
    assert grade["components"]["role_deficits"]["available"] is True
    # The unavailable component must NOT appear among the reasons.
    assert not any("band" in r for r in grade["reasons"])


def test_health_grade_all_unavailable_is_na_for_empty_deck():
    """No parseable [Main] cards -> nothing to grade -> 'N/A' with a
    None score. NEVER 'F': absence of data is not an unhealthy deck."""
    grade = deck_health.compute_health_grade("")
    assert grade["grade"] == "N/A"
    assert grade["score"] is None
    assert grade["reasons"] == []
    assert all(
        c["available"] is False for c in grade["components"].values()
    )


def test_health_grade_all_unavailable_is_na_for_degraded_health(
    monkeypatch,
):
    """Total-outage shape: roles degraded to empty, mana_sinks None,
    wincon key missing, and every Scryfall lookup failing (so the
    land walk is None too). Every component is excluded -> 'N/A'."""
    def _boom(name, **_kw):
        raise ConnectionError("Scryfall down")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    degraded = {
        "role_targets": {"roles": {}, "under_built": []},
        "mana_sinks": None,
        # wincon_protection deliberately absent (degraded payload).
    }
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK, health=degraded,
    )
    assert grade["grade"] == "N/A"
    assert grade["score"] is None


def test_health_grade_payload_shape(fake_lookup):
    """The payload contract the audit route and both UIs rely on:
    grade/score/reasons/components keys, and per-component
    score/weight/available with weights echoing _GRADE_WEIGHTS."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK, health=_health_fixture(),
    )
    assert set(grade.keys()) == {"grade", "score", "reasons", "components"}
    assert set(grade["components"].keys()) == set(
        deck_health._GRADE_WEIGHTS.keys()
    )
    for name, comp in grade["components"].items():
        assert set(comp.keys()) == {"score", "weight", "available"}
        assert comp["weight"] == deck_health._GRADE_WEIGHTS[name]
    assert isinstance(grade["reasons"], list)
    assert len(grade["reasons"]) <= 3


def test_health_grade_construction_warns_surface_as_reasons(fake_lookup):
    """Zero mana sinks + zero wincon protection hit the UI's 'warn'
    cutoff (score 30 each) and both surface as reasons."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK,
        health=_health_fixture(mana_sinks_count=0, wincon_count=0),
    )
    # (0.40*100 + 0.22*100 + 0.28*30) / 0.90 = 78 -> C band (B needs
    # >= 80). Construction's weight moved 0.35 -> 0.28 in the 2026-07
    # rebalance, so an all-warn construction row costs a little less
    # than it used to; the assertion stays a band, not a point value.
    assert grade["grade"] in {"C", "D"}
    joined = " ".join(grade["reasons"]).lower()
    assert "mana sink" in joined
    assert "wincon protection" in joined


# ---------------------------------------------------------------------------
# The finisher role target (2026-07) -- "can this deck actually win?"
# ---------------------------------------------------------------------------

def test_health_grade_missing_finisher_costs_points(fake_lookup):
    """A deck at target on every OTHER role but with zero win conditions
    used to be indistinguishable from a complete deck (the finisher
    target did not exist). Now it loses points and says why."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK, health=_health_fixture(role_counts={"finisher": 0}),
    )
    # role component: 100 * (1 - 3/38) = 92.1; over the three available
    # components: (0.40*92.1 + 0.22*100 + 0.28*100) / 0.90 = 96.5 -> 96.
    assert grade["score"] == 96
    assert any("finisher" in r.lower() for r in grade["reasons"])


def test_health_grade_ramp_and_draw_only_deck_grades_badly(fake_lookup):
    """The motivating failure: 99 cards of ramp and card draw. Every
    pre-2026-07 role target was satisfied by construction (ramp and draw
    were two of the five), so this deck graded A. It should not: it has
    no removal, no wipes, no protection and no way to win."""
    fake_lookup["mountain"] = "Basic Land — Mountain"
    fake_lookup["grizzly bears"] = "Creature — Bear"
    grade = deck_health.compute_health_grade(
        _BALANCED_DECK,
        health=_health_fixture(
            role_counts={"ramp": 40, "draw": 59, "removal": 0, "wipe": 0,
                         "protection": 0, "finisher": 0},
            mana_sinks_count=0, wincon_count=0,
        ),
    )
    # role deficits 8+3+4+3 = 18 of 38 -> 52.6; construction all-warn 30:
    # (0.40*52.6 + 0.22*100 + 0.28*30) / 0.90 = 57.5 -> 57 -> D.
    assert grade["grade"] in {"D", "F"}
    joined = " ".join(grade["reasons"]).lower()
    assert "finisher" in joined or "removal" in joined


# ---------------------------------------------------------------------------
# commander_alignment -- the grade can finally see the command zone
# ---------------------------------------------------------------------------

def _commander_deck(lands: int = 37) -> str:
    return (
        "[Commander]\n1 Test Commander\n"
        f"[Main]\n{lands} Mountain\n{99 - lands} Grizzly Bears\n"
    )


def _commander_lookup(monkeypatch, commander_cmc: float):
    """Stub Scryfall so only the commander's cost varies between runs."""
    def _fake(name, **_kw):
        low = name.lower()
        if low == "test commander":
            return {
                "name": name, "type_line": "Legendary Creature — Human",
                "cmc": commander_cmc, "mana_cost": "", "oracle_text": "",
            }
        return {
            "name": name,
            "type_line": (
                "Basic Land — Mountain" if low == "mountain"
                else "Creature — Bear"
            ),
            "mana_cost": "", "oracle_text": "",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )


def test_expensive_commander_demands_more_ramp(monkeypatch):
    """The same 8 ramp pieces are fine behind a 2-drop and thin behind a
    6-drop. Before this component the two decks graded identically --
    the module never read the [Commander] section at all."""
    health = _health_fixture(role_counts={"ramp": 8})

    _commander_lookup(monkeypatch, 2.0)
    cheap = deck_health.compute_health_grade(_commander_deck(), health=health)
    _commander_lookup(monkeypatch, 6.0)
    expensive = deck_health.compute_health_grade(
        _commander_deck(), health=health,
    )

    # 2-MV: expected ramp 10 + 1.5*(2-3.5) = 7.75 -> 8 ramp clears it.
    assert cheap["components"]["commander_alignment"]["score"] == 100
    # 6-MV: 10 + 1.5*2.5 = 13.75, capped by the band at 12 -> 8/12 = 67.
    assert expensive["components"]["commander_alignment"]["score"] == 67
    assert expensive["score"] < cheap["score"]
    assert any("commander" in r.lower() for r in expensive["reasons"])


def test_commander_alignment_unavailable_without_a_command_zone(
    monkeypatch,
):
    """No [Commander] section -> the component is excluded and the
    remaining weights renormalize. Absence of data is not a bad deck
    (the module's standing outage contract)."""
    _commander_lookup(monkeypatch, 6.0)
    grade = deck_health.compute_health_grade(
        "[Main]\n37 Mountain\n62 Grizzly Bears\n",
        health=_health_fixture(role_counts={"ramp": 8}),
    )
    comp = grade["components"]["commander_alignment"]
    assert comp["available"] is False
    assert comp["score"] is None
    assert grade["grade"] != "N/A"      # everything else still grades


# ---------------------------------------------------------------------------
# mana_health's second half -- Karsten color sources, not just land COUNT
# ---------------------------------------------------------------------------

def test_mana_health_penalizes_a_color_below_its_karsten_target(monkeypatch):
    """37 lands is a perfect land COUNT, and until 2026-07 that was the
    entire mana grade -- a Command Tower and a Wastes scored identically.
    This deck's black requirement ({2}{B}{B} -> 26 sources) is met by 4
    Swamps, and the grade now says so."""
    cards = {
        "test commander": {
            "name": "Test Commander", "type_line": "Legendary Creature",
            "color_identity": ["B"], "mana_cost": "{2}{B}{B}", "cmc": 4.0,
        },
        "swamp": {
            "name": "Swamp", "type_line": "Basic Land — Swamp",
            "produced_mana": ["B"], "color_identity": [], "mana_cost": "",
        },
        "wastes": {
            "name": "Wastes", "type_line": "Basic Land",
            "produced_mana": ["C"], "color_identity": [], "mana_cost": "",
        },
        "grizzly bears": {
            "name": "Grizzly Bears", "type_line": "Creature — Bear",
            "color_identity": [], "mana_cost": "{1}{G}", "cmc": 2.0,
        },
    }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_kw: cards.get(name.lower()),
    )
    deck = (
        "[Commander]\n1 Test Commander\n"
        "[Main]\n4 Swamp\n33 Wastes\n62 Grizzly Bears\n"
    )
    grade = deck_health.compute_health_grade(deck, health=_health_fixture())
    # Land count (37 effective) is perfect -> 100; Karsten black is
    # 4/26 -> 15.4. The component is the mean of its two halves.
    assert grade["components"]["mana_health"]["score"] == 58
    assert any("karsten" in r.lower() for r in grade["reasons"])
    assert any("B sources 4/26" in r for r in grade["reasons"])


def test_commander_mana_value_falls_back_to_the_printed_cost(monkeypatch):
    """Snapshots without a ``cmc`` field still yield a mana value, via
    the manabase module's existing cost parser (no second mana-cost
    regex in this file). A commander with NEITHER is unavailable, not a
    free 0-drop."""
    def _fake(name, **_kw):
        return {
            "name": name, "type_line": "Legendary Creature",
            "mana_cost": "{3}{W}{W}", "oracle_text": "",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake,
    )
    assert deck_health._commander_mana_value(_commander_deck()) == 5.0

    def _costless(name, **_kw):
        return {"name": name, "type_line": "Legendary Creature",
                "mana_cost": "", "oracle_text": ""}
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _costless,
    )
    assert deck_health._commander_mana_value(_commander_deck()) is None


def test_commander_alignment_unavailable_when_roles_are_degraded(
    monkeypatch,
):
    """A degraded role signal (Scryfall outage inside count_deck_roles)
    leaves no ramp count to compare the commander against -> unavailable,
    never a fabricated 0."""
    _commander_lookup(monkeypatch, 6.0)
    degraded = dict(_health_fixture())
    degraded["role_targets"] = {"roles": {}, "under_built": []}
    grade = deck_health.compute_health_grade(
        _commander_deck(), health=degraded,
    )
    assert grade["components"]["commander_alignment"]["available"] is False


# CLI surface: the commander-advise report header renders the grade
# next to the Estimated-bracket block. Tested here (not in the slow
# advisor module) so it runs in the default suite.

def test_cli_report_renders_health_grade_line():
    """_format_report_text places 'Health grade: <L> (<score>/100)'
    plus indented reasons in the header; None keeps legacy output."""
    from commander_builder.improvement_advisor import (
        AdviceReport, _format_report_text,
    )
    report = AdviceReport(
        deck_filename="X.dck", deck_id=None, bracket=3,
        commander_names=["Hakbal"],
    )
    grade = {
        "grade": "B", "score": 82,
        "reasons": ["Ramp 4/10 — 6 below target"],
        "components": {},
    }
    text = _format_report_text(report, health_grade=grade)
    assert "Health grade: B (82/100)" in text
    assert "Ramp 4/10" in text
    # Legacy path: without the grade the line is absent entirely.
    assert "Health grade" not in _format_report_text(report)


def test_cli_report_renders_na_health_grade_explicitly():
    """'N/A' (all signals unavailable) prints an explicit availability
    note instead of masquerading as a real grade."""
    from commander_builder.improvement_advisor import (
        AdviceReport, _format_report_text,
    )
    report = AdviceReport(
        deck_filename="X.dck", deck_id=None, bracket=3,
        commander_names=["Hakbal"],
    )
    grade = {"grade": "N/A", "score": None, "reasons": [], "components": {}}
    text = _format_report_text(report, health_grade=grade)
    assert "Health grade: N/A (signals unavailable)" in text


# ---------------------------------------------------------------------------
# Land-band reconciliation (2026-08): _LAND_BAND is the builder's clamp band
# ---------------------------------------------------------------------------


def test_land_band_is_the_builders_clamp_band():
    """deck_builder_manabase clamps its builds to LAND_COUNT_BAND
    (33-40); the grade must accept that whole range — before the
    reconciliation _LAND_BAND was a hand-kept (33, 38) and the app
    docked ~12 points per land on its own 39/40-land builds."""
    from commander_builder.deck_builder_manabase import LAND_COUNT_BAND
    assert deck_health._LAND_BAND == LAND_COUNT_BAND == (33, 40)


def test_score_land_band_accepts_builder_max_land_counts():
    """39/40 effective lands (a high-curve build the assembler itself
    emits) now score a clean 100 on the land half of mana_health."""
    for eff in (33.0, 38.0, 39.0, 40.0):
        score = deck_health._score_land_band({"effective_lands": eff})
        assert score == 100.0, f"{eff} effective lands should be in-band"


def test_score_land_band_still_penalizes_outside_the_shared_band():
    """The linear penalty survives the widening: below 33 and above 40
    are still charged _LAND_BAND_PENALTY per effective land."""
    per = deck_health._LAND_BAND_PENALTY
    assert deck_health._score_land_band({"effective_lands": 32.0}) == 100.0 - per
    # A seed-trusted 42-land build is charged mildly (2 past the band) —
    # intentional: the band covers what OUR model would choose.
    assert deck_health._score_land_band({"effective_lands": 42.0}) == 100.0 - 2 * per
    # The 27-land greed manabase still craters (6 under the floor).
    assert deck_health._score_land_band({"effective_lands": 27.0}) == 100.0 - 6 * per
