"""FP-014.2 — color-source-aware manabase tests (offline).

Covers the three documented models in ``deck_builder_manabase`` and their
integration through ``deck_builder._assemble``:

  * MODEL 1 — land count from the curve (+ seed reconciliation);
  * MODEL 2 — Karsten-anchored per-color source targets;
  * MODEL 3 — the fill order (keep seed lands → top-up fixing → basics),
    that a dual counts for both its colors, mono/2/5-color behavior, and
    graceful degrade to basics-only when land data is unavailable.

Every card touch is an injected fake ``lookup`` — nothing reaches Scryfall.
"""
from types import SimpleNamespace

from commander_builder import deck_builder
from commander_builder.deck_builder import _assemble
from commander_builder.deck_builder_manabase import (
    DOUBLE_PIP_SOURCES,
    SINGLE_PIP_SOURCES,
    _PipStats,
    build_manabase,
    color_source_targets,
    land_color_sources,
    pip_stats,
    target_land_count,
)
from commander_builder.dck_utils import (
    count_main_cards,
    main_card_quantities,
)
from commander_builder.edhrec_client import CardEntry


# --- Fake card DB ---------------------------------------------------------

_FAKE_CARDS = {
    # Golgari (BG) commander whose oracle text is NOT tribal.
    "Golgari Boss": {
        "type_line": "Legendary Creature — Elf", "color_identity": ["B", "G"],
        "mana_cost": "{2}{B}{G}", "oracle_text": "Draw a card.",
    },
    # A mono-red tribal commander (oracle text mentions Goblins).
    "Goblin Chief": {
        "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "mana_cost": "{1}{R}{R}",
        "oracle_text": "Create a Goblin creature token. "
                       "Goblins you control get +1/+1.",
    },
    # A named seed dual (BG) — must resolve as a LAND so the assembler
    # routes it into the kept-seed-lands pile.
    "Bayou": {
        "type_line": "Land — Swamp Forest", "color_identity": ["B", "G"],
        "mana_cost": "",
    },
}


def _fake_lookup(name):
    if name in _FAKE_CARDS:
        return _FAKE_CARDS[name]
    # Synthetic single-pip spells by color prefix.
    prefix_cost = {
        "B ": "{1}{B}", "G ": "{1}{G}", "R ": "{1}{R}",
        "W ": "{1}{W}", "U ": "{1}{U}",
    }
    for prefix, cost in prefix_cost.items():
        if name.startswith(prefix):
            return {"type_line": "Creature", "mana_cost": cost,
                    "color_identity": [prefix.strip()]}
    return None


def _names(spec):
    """spec like {'R': 30, 'U': 10} -> ['R 0', ... 'U 9'] synthetic spells."""
    out = []
    for color, n in spec.items():
        out.extend(f"{color} {i}" for i in range(n))
    return out


def _avg(cards):
    return SimpleNamespace(cards=[CardEntry(name=n) for n in cards])


# ===========================================================================
# MODEL 1 — land count from the curve.
# ===========================================================================


def test_land_count_curve_model_pivot_and_slope():
    # 3.5 MV is the pivot → the 38-land baseline.
    assert target_land_count(3.5) == 38
    # 2 lands per point of MV away from the pivot.
    assert target_land_count(2.5) == 36
    assert target_land_count(4.5) == 40


def test_land_count_clamps_to_sane_band():
    # A degenerate low curve still floors at 33; a huge curve caps at 40.
    assert target_land_count(0.0) == 33
    assert target_land_count(9.0) == 40


def test_land_count_trusts_a_plausible_seed_over_the_model():
    # Seed count in the 33-42 band wins (community-tuned) even when the
    # curve model would say otherwise (2.0 MV → 35).
    assert target_land_count(2.0, seed_land_count=37) == 37
    # An implausible seed count (sparse fixture) is ignored → curve model.
    assert target_land_count(2.0, seed_land_count=2) == 35


# ===========================================================================
# MODEL 2 — Karsten-anchored source targets.
# ===========================================================================


def test_source_target_single_pip_hits_the_single_anchor():
    # All white cards are single-pip → target is exactly the single anchor.
    stats = _PipStats(weights={"W": 10}, cards_with={"W": 10},
                      cards_double={"W": 0})
    assert color_source_targets(["W"], stats) == {"W": SINGLE_PIP_SOURCES}


def test_source_target_double_pip_hits_the_double_anchor():
    # Every white card is {W}{W} → target rises to the double anchor.
    stats = _PipStats(weights={"W": 20}, cards_with={"W": 10},
                      cards_double={"W": 10})
    assert color_source_targets(["W"], stats) == {"W": DOUBLE_PIP_SOURCES}


def test_source_target_interpolates_between_anchors():
    # Half the white cards are double-pip → halfway between 14 and 21.
    stats = _PipStats(weights={"W": 15}, cards_with={"W": 10},
                      cards_double={"W": 5})
    target = color_source_targets(["W"], stats)["W"]
    assert SINGLE_PIP_SOURCES < target < DOUBLE_PIP_SOURCES
    assert target == 18  # round(14 + 7*0.5)


def test_source_target_zero_pip_color_is_zero():
    # A color in the identity that no spell needs → 0 (basics floor gives 1).
    stats = _PipStats(weights={}, cards_with={}, cards_double={})
    assert color_source_targets(["W"], stats) == {"W": 0}


def test_pip_stats_counts_double_pips_and_mana_value():
    stats = pip_stats(["Golgari Boss"], _fake_lookup)
    # {2}{B}{G} → single B pip + single G pip, MV 4.
    assert stats.cards_with["B"] == 1
    assert stats.cards_with["G"] == 1
    assert stats.cards_double["B"] == 0
    assert stats.avg_mana_value == 4.0


# ===========================================================================
# MODEL 3a — land -> color source resolution (a dual counts for BOTH).
# ===========================================================================


def test_dual_land_counts_for_both_its_colors():
    # Bayou (BG ABU dual) is a source for BOTH black and green.
    assert land_color_sources("Bayou", {"B", "G"}, _fake_lookup) == {"B", "G"}


def test_basic_and_any_color_land_sources():
    assert land_color_sources("Mountain", {"R"}, _fake_lookup) == {"R"}
    # Wastes is colorless — no colored source.
    assert land_color_sources("Wastes", {"R"}, _fake_lookup) == set()
    # Command Tower fixes every color in the identity.
    assert land_color_sources(
        "Command Tower", {"B", "G"}, _fake_lookup
    ) == {"B", "G"}


# ===========================================================================
# MODEL 3b — the fill: mono / 2-color / 5-color / kept seed / degrade.
# ===========================================================================


def test_mono_color_is_all_basics_of_one_color():
    names = _names({"R": 40})
    stats = pip_stats(names, _fake_lookup)
    mb = build_manabase(["R"], names, [], 37, lookup=_fake_lookup, stats=stats)
    # Mono-red has no eligible duals → a pure Mountain base.
    assert mb.lands == []
    assert mb.basics == {"Mountain": 37}
    assert mb.summary.land_count == 37
    assert mb.summary.fixing_land_count == 0


def test_two_color_keeps_seed_dual_and_splits_sources():
    names = _names({"B": 20, "G": 20})
    stats = pip_stats(names, _fake_lookup)
    mb = build_manabase(
        ["B", "G"], names, ["Bayou"], 37,
        lookup=_fake_lookup, stats=stats,
    )
    # The kept seed dual survives and is reported as kept.
    assert "Bayou" in mb.lands
    assert mb.summary.kept_seed_lands == 1
    # Top-up added more BG fixing from the advisor tiers (not hand-rolled).
    assert mb.summary.fixing_land_count > 1
    # Both colors reach their (single-pip, 14) source target; the dual and
    # every BG fixer counted toward BOTH.
    assert mb.summary.sources["B"] >= mb.summary.targets["B"]
    assert mb.summary.sources["G"] >= mb.summary.targets["G"]
    # Exactly the land budget, no drift.
    assert mb.total_cards() == 37


def test_five_color_sane_land_count_every_color_gets_sources():
    names = _names({"W": 8, "U": 8, "B": 8, "R": 8, "G": 8})
    stats = pip_stats(names, _fake_lookup)
    mb = build_manabase(
        ["W", "U", "B", "R", "G"], names, [], 38,
        lookup=_fake_lookup, stats=stats,
    )
    assert mb.total_cards() == 38
    # Targets can't all be met in 5c (Karsten notes the same) — but every
    # color must get real sources, and the base is fixing-heavy.
    for c in "WUBRG":
        assert mb.summary.sources[c] >= 1, f"{c} left with no sources"
    assert mb.summary.fixing_land_count > 5  # many duals pulled in.
    # A basics floor was reserved so no color is basic-less.
    assert mb.summary.basic_count >= 1


def test_degrades_to_basics_only_when_no_colors():
    # No resolvable identity (colorless / data outage) → basics-only, flagged.
    mb = build_manabase([], [], [], 38, lookup=lambda n: None)
    assert mb.lands == []
    assert mb.basics == {"Wastes": 38}
    assert mb.summary.degraded is True


# ===========================================================================
# Integration through _assemble.
# ===========================================================================


def test_assemble_keeps_named_seed_dual_into_output():
    cards = (
        ["Golgari Boss"]
        + _names({"B": 25, "G": 25})
        + ["Bayou", "Swamp", "Forest"]  # a real dual + two basics in the seed.
    )
    result = _assemble(
        "Golgari Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "BG",
        lookup=_fake_lookup,
        name="Golgari",
    )
    mains = main_card_quantities(result.text)
    # The seed's tuned dual is KEPT (FP-014.2), at singleton.
    assert mains.get("Bayou") == 1
    assert result.manabase.kept_seed_lands >= 1
    # Still exactly 99, and the summary exposes per-color sources vs target.
    assert count_main_cards(result.text) == 99
    assert set(result.manabase.targets) == {"B", "G"}


def test_assemble_adds_tribal_lands_for_a_tribal_commander():
    # Goblin Chief's oracle text reads tribal → Cavern of Souls et al. get
    # pulled from tribal_essential_lands (mono-red has no color duals).
    cards = ["Goblin Chief"] + _names({"R": 40})
    result = _assemble(
        "Goblin Chief", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_fake_lookup,
        name="Goblins",
    )
    mains = main_card_quantities(result.text)
    assert "Cavern of Souls" in mains
    assert count_main_cards(result.text) == 99


def test_assemble_cli_prints_manabase_summary(tmp_path, monkeypatch, capsys):
    cards = ["Golgari Boss"] + _names({"B": 25, "G": 25}) + ["Bayou"]
    monkeypatch.setattr(
        deck_builder, "fetch_average_deck", lambda c, b: _avg(cards),
    )
    monkeypatch.setattr(deck_builder, "fetch_commander_page", lambda c: None)
    monkeypatch.setattr(deck_builder, "lookup_card", _fake_lookup)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )
    rc = deck_builder.main([
        "--commander", "Golgari Boss", "--bracket", "3",
        "--deck-dir", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "manabase:" in out
    assert "sources (have/target):" in out
