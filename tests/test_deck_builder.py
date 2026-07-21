"""deck_builder (FP-014.1) tests — offline via injected fake fetchers.

Every EDHREC/Scryfall touchpoint is either injected into ``build_deck`` /
``_assemble`` or monkeypatched at the module level, so nothing here reaches
the network. ``enforce_color_identity`` reaches for
``scryfall_client.lookup_card`` at call time, so tests that exercise the
color-identity filter monkeypatch that symbol with the same fake DB used for
the injected ``lookup``.
"""
from types import SimpleNamespace

import pytest

from commander_builder import deck_builder
from commander_builder.deck_builder import (
    _distribute_basics_by_pips,
    _assemble,
    build_deck,
)
from commander_builder.dck_utils import (
    count_main_cards,
    main_card_quantities,
    section_card_names,
)
from commander_builder.edhrec_client import CardEntry, CommanderPage
from commander_builder.log_parser import _normalize


# --- Fake card DB ---------------------------------------------------------

_R_CREATURE = {
    "type_line": "Creature — Goblin", "color_identity": ["R"],
    "mana_cost": "{1}{R}",
}
_U_CREATURE = {
    "type_line": "Creature — Merfolk", "color_identity": ["U"],
    "mana_cost": "{1}{U}",
}
_FAKE_CARDS = {
    "Krenko, Mob Boss": {
        "type_line": "Legendary Creature — Goblin",
        "color_identity": ["R"], "mana_cost": "{2}{R}{R}",
    },
    "Command Tower": {
        "type_line": "Land", "color_identity": [], "mana_cost": "",
    },
    "Sol Ring": {
        "type_line": "Artifact", "color_identity": [], "mana_cost": "{1}",
    },
    "Cultivate": {  # green — off-color for a mono-red commander
        "type_line": "Sorcery", "color_identity": ["G"], "mana_cost": "{2}{G}",
    },
}


def _fake_lookup(name):
    if name in _FAKE_CARDS:
        return _FAKE_CARDS[name]
    if name.startswith(("Goblin ", "Top ", "Spell ")):
        return dict(_R_CREATURE)
    if name.startswith("Merfolk "):
        return dict(_U_CREATURE)
    return None


def _avg(cards):
    """Minimal AverageDeck stand-in — build_deck only reads ``.cards``."""
    return SimpleNamespace(cards=[CardEntry(name=n) for n in cards])


def _page(top=None, high_synergy=None):
    return CommanderPage(
        commander_name="Fake", slug="fake", fetched_at="now",
        top_cards=[CardEntry(name=n) for n in (top or [])],
        high_synergy_cards=[CardEntry(name=n) for n in (high_synergy or [])],
    )


# --- Seed path ------------------------------------------------------------


def test_average_deck_seed_builds_legal_99():
    # Commander + 40 nonland spells + a nonbasic land + a basic in the seed.
    cards = (
        ["Krenko, Mob Boss"]
        + [f"Goblin {i}" for i in range(40)]
        + ["Command Tower", "Mountain"]
    )
    text = build_deck(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,  # pass-through CI filter (offline)
        lookup=_fake_lookup,
        name="Krenko",
    )
    # Exactly 99 main + commander in the command zone.
    assert count_main_cards(text) == 99
    assert section_card_names(text, "Commander") == ["Krenko, Mob Boss"]
    mains = main_card_quantities(text)
    # Commander never leaks into [Main].
    assert "Krenko, Mob Boss" not in mains
    # FP-014.2: the seed's own nonbasic land is KEPT (not discarded like
    # FP-014.1) — its tuned fixing survives into the output at singleton.
    assert mains.get("Command Tower") == 1
    # The seed's basic (Mountain) is dropped and recomputed by the manabase.
    # Singleton: every nonbasic (spell or land) sits at quantity 1.
    for name, qty in mains.items():
        if name != "Mountain":
            assert qty == 1, f"{name} broke singleton with qty {qty}"


def test_name_field_normalizes_to_stem():
    cards = ["Krenko, Mob Boss"] + [f"Goblin {i}" for i in range(60)]
    result = _assemble(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Krenko",
    )
    assert result.stem == "[USER] Krenko [B3]"
    name_field = next(
        line[len("Name="):]
        for line in result.text.splitlines()
        if line.startswith("Name=")
    )
    assert name_field == result.stem
    assert _normalize(name_field) == _normalize(result.stem)


def test_color_identity_drops_off_color_card(monkeypatch):
    # enforce_color_identity looks cards up via scryfall_client.lookup_card.
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )
    cards = (
        ["Krenko, Mob Boss"]
        + [f"Goblin {i}" for i in range(40)]
        + ["Cultivate"]  # green, illegal in a mono-red deck
    )
    result = _assemble(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "R",
        lookup=_fake_lookup,
        name="Krenko",
    )
    mains = main_card_quantities(result.text)
    assert "Cultivate" not in mains
    assert "Cultivate" in result.dropped_off_color
    assert count_main_cards(result.text) == 99
    # Mono-red identity → basics are Mountains only.
    assert set(mains) - {f"Goblin {i}" for i in range(40)} == {"Mountain"}


def test_singleton_dedupes_repeated_seed_card():
    cards = (
        ["Krenko, Mob Boss", "Goblin 1", "Goblin 1"]
        + [f"Goblin {i}" for i in range(2, 40)]
    )
    text = build_deck(
        "Krenko, Mob Boss", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Krenko",
    )
    assert main_card_quantities(text)["Goblin 1"] == 1
    assert count_main_cards(text) == 99


# --- Fallback path --------------------------------------------------------


def test_fallback_builds_from_commander_page():
    page = _page(top=[f"Top {i}" for i in range(50)],
                 high_synergy=[f"Spell {i}" for i in range(20)])
    result = _assemble(
        "Some Commander", 3,
        fetch_avg=lambda c, b: None,  # no average deck published
        fetch_page=lambda c: page,
        resolve_ci=lambda n: None,
        lookup=_fake_lookup,
        name="Some Commander",
    )
    assert result.source == "commander-page fallback"
    assert count_main_cards(result.text) == 99
    # Cards came from the page's top list.
    mains = main_card_quantities(result.text)
    assert any(n.startswith("Top ") for n in mains)


def test_neither_source_available_raises_clean_error():
    with pytest.raises(ValueError, match="cannot build: no EDHREC data"):
        build_deck(
            "Nobody Special", 3,
            fetch_avg=lambda c, b: None,
            fetch_page=lambda c: None,
            resolve_ci=lambda n: None,
            lookup=_fake_lookup,
        )


def test_empty_commander_page_raises_clean_error():
    with pytest.raises(ValueError, match="cannot build: no EDHREC data"):
        build_deck(
            "Nobody Special", 3,
            fetch_avg=lambda c, b: None,
            fetch_page=lambda c: _page(),  # a page with zero cards
            resolve_ci=lambda n: None,
            lookup=_fake_lookup,
        )


def test_invalid_bracket_raises():
    with pytest.raises(ValueError, match="bracket must be"):
        build_deck("Krenko, Mob Boss", 9,
                   fetch_avg=lambda c, b: None, fetch_page=lambda c: None,
                   resolve_ci=lambda n: None, lookup=_fake_lookup)


# --- Basic-land distribution ---------------------------------------------


def test_distribute_basics_sums_to_total_and_splits_by_pips():
    out = _distribute_basics_by_pips(["R", "G"], {"R": 30, "G": 10}, 20)
    assert sum(out.values()) == 20
    # Heavier red pip weight → more Mountains than Forests.
    assert out["Mountain"] > out["Forest"]
    # Every identity color keeps at least one source.
    assert out["Forest"] >= 1


def test_distribute_basics_colorless_is_all_wastes():
    assert _distribute_basics_by_pips([], {}, 5) == {"Wastes": 5}


def test_distribute_basics_zero_pips_even_split():
    out = _distribute_basics_by_pips(
        ["W", "U", "B"], {"W": 0, "U": 0, "B": 0}, 3,
    )
    assert out == {"Plains": 1, "Island": 1, "Swamp": 1}


def test_two_color_build_splits_basics_by_pips():
    cards = (
        ["Some Commander"]
        + [f"Goblin {i}" for i in range(30)]   # 30 red pips
        + [f"Merfolk {i}" for i in range(10)]  # 10 blue pips
    )
    result = _assemble(
        "Some Commander", 3,
        fetch_avg=lambda c, b: _avg(cards),
        fetch_page=lambda c: None,
        resolve_ci=lambda n: "UR",
        lookup=_fake_lookup,
        name="Izzet Test",
    )
    mains = main_card_quantities(result.text)
    assert count_main_cards(result.text) == 99
    # 40 nonlands seeded, default land target not read from a landless seed
    # → 99 - 40 = 59 basics, split red-heavy by pip weight.
    assert result.land_count == 59
    assert mains["Mountain"] > mains["Island"] >= 1


# --- CLI smoke ------------------------------------------------------------


def test_main_cli_smoke(tmp_path, monkeypatch):
    cards = ["Krenko, Mob Boss"] + [f"Goblin {i}" for i in range(60)]
    monkeypatch.setattr(
        deck_builder, "fetch_average_deck", lambda c, b: _avg(cards),
    )
    monkeypatch.setattr(deck_builder, "fetch_commander_page", lambda c: None)
    monkeypatch.setattr(deck_builder, "lookup_card", _fake_lookup)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )
    rc = deck_builder.main([
        "--commander", "Krenko, Mob Boss",
        "--bracket", "3",
        "--deck-dir", str(tmp_path),
    ])
    assert rc == 0
    files = list(tmp_path.glob("*.dck"))
    assert len(files) == 1
    assert files[0].name == "[USER] Krenko, Mob Boss Build [B3].dck"
    assert count_main_cards(files[0].read_text(encoding="utf-8")) == 99


def test_main_cli_reports_clean_error_on_no_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deck_builder, "fetch_average_deck", lambda c, b: None)
    monkeypatch.setattr(deck_builder, "fetch_commander_page", lambda c: None)
    monkeypatch.setattr(deck_builder, "lookup_card", _fake_lookup)
    rc = deck_builder.main([
        "--commander", "Nobody Special",
        "--deck-dir", str(tmp_path),
    ])
    assert rc == 1
    assert "cannot build: no EDHREC data" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.dck"))
