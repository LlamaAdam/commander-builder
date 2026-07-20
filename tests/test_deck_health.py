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
        "wincon_protection", "self_mill", "role_targets",
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
