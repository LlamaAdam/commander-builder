"""Tests for deck_dashboard.py — FP-006 backend prep.

Coverage:
- Expanded role taxonomy (land_payoff, win_condition).
- Price extraction from Scryfall card dicts.
- Power-level heuristic (bracket anchoring + game-changer count + cmc).
- Match-score combination of inclusion% + synergy% + rank bonus.
- Top-level build_dashboard end-to-end with mocked Scryfall lookups.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from commander_builder.deck_dashboard import (
    DISPLAY_CATEGORIES,
    DashboardData,
    _extract_price_usd,
    _power_bracket,
    _read_main_with_quantities,
    build_dashboard,
    classify_role_extended,
    match_score,
)


# ---------------------------------------------------------------------------
# Expanded role taxonomy
# ---------------------------------------------------------------------------

def test_classify_role_extended_landfall_payoff():
    role = classify_role_extended(
        "Landfall — Whenever a land enters under your control, this "
        "creature gets +2/+2 until end of turn.",
        "Creature — Beast",
    )
    assert role == "land_payoff"


def test_classify_role_extended_play_a_land_trigger():
    role = classify_role_extended(
        "Whenever you play a land, draw a card.",
        "Creature — Snake",
    )
    assert role == "land_payoff"


def test_classify_role_extended_win_condition():
    role = classify_role_extended(
        "Target opponent loses the game.",
        "Sorcery",
    )
    assert role == "win_condition"


def test_classify_role_extended_each_opponent_loses_life():
    role = classify_role_extended(
        "Each opponent loses 10 life.",
        "Creature — Beast",
    )
    assert role == "win_condition"


def test_classify_role_extended_you_win_the_game():
    # Coalition Victory was returning ``"other"`` instead of
    # ``"win_condition"`` because the original patterns only matched
    # "target opponent loses" / "each opponent loses" idioms.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Coalition Victory")
    assert classify_role_extended(o["oracle_text"], o["type_line"]) == "win_condition"


def test_classify_role_extended_craterhoof_trample_then_pump():
    # Craterhoof Behemoth's real Scryfall oracle reads "gain trample
    # and get +X/+X" — opposite word order from the original pattern.
    from tests.fixtures.real_oracles import oracle
    o = oracle("Craterhoof Behemoth")
    assert classify_role_extended(o["oracle_text"], o["type_line"]) == "win_condition"


def test_classify_role_extended_falls_back_to_base_taxonomy():
    """When no land/win patterns match, fall through to staples.classify_role."""
    role = classify_role_extended(
        "Destroy target creature.",
        "Instant",
    )
    assert role == "removal"


def test_classify_role_extended_handles_empty():
    assert classify_role_extended("", "") == "other"


# ---------------------------------------------------------------------------
# _extract_price_usd
# ---------------------------------------------------------------------------

def test_extract_price_returns_float():
    data = {"prices": {"usd": "8.99", "eur": "7.50"}}
    assert _extract_price_usd(data) == 8.99


def test_extract_price_returns_none_when_missing():
    assert _extract_price_usd({"prices": {}}) is None
    assert _extract_price_usd({}) is None
    assert _extract_price_usd(None) is None


def test_extract_price_returns_none_for_unparseable():
    """Sometimes Scryfall returns null; ensure we don't crash."""
    assert _extract_price_usd({"prices": {"usd": None}}) is None


def test_extract_price_handles_zero():
    """Zero is a valid price (free promos)."""
    assert _extract_price_usd({"prices": {"usd": "0.05"}}) == 0.05


# ---------------------------------------------------------------------------
# _power_bracket — Wizards' 1..5 Commander Bracket system
# ---------------------------------------------------------------------------

def test_power_bracket_low_for_high_cmc_no_game_changers():
    """Slow deck (avg cmc 4, 0 changers) is bracket 1 (Exhibition)."""
    p = _power_bracket(avg_cmc=4.5, n_game_changers=0, bracket=None)
    assert p == 1


def test_power_bracket_high_for_fast_deck_with_changers():
    """Fast deck with 3+ game changers is bracket 4 (Optimized)."""
    p = _power_bracket(avg_cmc=2.2, n_game_changers=4, bracket=None)
    assert p == 4


def test_power_bracket_user_supplied_bracket_wins():
    """An explicit bracket trumps the heuristic — it's what the user
    declares the deck is built for."""
    p_no_bracket = _power_bracket(avg_cmc=3.0, n_game_changers=1, bracket=None)
    p_bracket_4 = _power_bracket(avg_cmc=3.0, n_game_changers=1, bracket=4)
    assert p_bracket_4 == 4
    assert p_no_bracket != 4  # heuristic landed elsewhere


def test_power_bracket_clamped_to_1_to_5():
    """Output is always a valid bracket integer."""
    very_high = _power_bracket(avg_cmc=1.0, n_game_changers=20, bracket=5)
    very_low = _power_bracket(avg_cmc=8.0, n_game_changers=0, bracket=1)
    assert 1 <= very_high <= 5
    assert 1 <= very_low <= 5


def test_power_bracket_combo_archetype_nudges_up():
    """Combo decks are at least bracket 3 (Upgraded) even with 0 GCs
    in our list (combo lines always pack interaction & tutors)."""
    p_combo = _power_bracket(avg_cmc=3.0, n_game_changers=1, bracket=None,
                           archetype="combo")
    p_other = _power_bracket(avg_cmc=3.0, n_game_changers=1, bracket=None,
                           archetype="midrange")
    assert p_combo >= p_other


def test_power_bracket_user_override_can_underdeclare():
    """Sanity: if user declares B2 on a deck the heuristic thinks is
    B4, the explicit bracket wins. Bracket auto-inference UI uses
    `inferred_bracket` to surface the divergence; the *displayed*
    bracket still respects the user's choice."""
    declared = _power_bracket(avg_cmc=2.0, n_game_changers=4, bracket=2)
    inferred = _power_bracket(avg_cmc=2.0, n_game_changers=4, bracket=None)
    assert declared == 2
    assert inferred == 4


def test_dashboard_emits_inferred_bracket_alongside_declared(
    tmp_path, monkeypatch,
):
    """build_dashboard exposes both the user's declared bracket and
    the heuristic's standalone guess so the UI can warn on
    divergence."""
    from commander_builder.deck_dashboard import build_dashboard

    deck = tmp_path / "deck.dck"
    deck.write_text(
        "[metadata]\nName=Test\n"
        "[Commander]\n1 Test Cmdr\n"
        "[Main]\n"
        + "1 Mountain\n" * 35
        + "1 Sol Ring\n1 Mana Vault\n1 Mana Crypt\n"
        + "1 Demonic Tutor\n1 Vampiric Tutor\n"
        + "1 Lightning Bolt\n" * 60,
        encoding="utf-8",
    )
    # Stub Scryfall: the GC-list cards (Mana Vault, Mana Crypt,
    # Demonic Tutor, Vampiric Tutor) so n_game_changers reads high.
    def fake_lookup(name, **_kw):
        return {
            "type_line": "Sorcery" if "Tutor" in name or "Bolt" in name else "Artifact",
            "oracle_text": "",
            "cmc": 1.0,
            "color_identity": ["R"],
            "prices": {"usd": "0.50"},
        }
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )

    # User declares B2 ("Core") but the deck has multiple game-changers.
    result = build_dashboard(deck, bracket=2)
    assert result.stat_tiles["bracket"] == 2
    # Heuristic should land at 4 (4+ GCs from our staples list).
    # The exact value depends on what's in UNIVERSAL_STAPLES_LC vs the
    # GC list — check it's at least higher than declared.
    assert result.stat_tiles["inferred_bracket"] >= 2


# ---------------------------------------------------------------------------
# match_score
# ---------------------------------------------------------------------------

def test_match_score_inclusion_pct_is_baseline():
    """A card with 70% inclusion and no synergy should score ~70."""
    s = match_score(inclusion_pct=70.0, synergy_pct=0.0, rank_in_list=10)
    # No rank bonus at rank 10; just inclusion.
    assert s == 70


def test_match_score_synergy_adds_capped_bonus():
    """Synergy% adds a capped bonus (max 20pp)."""
    no_synergy = match_score(70.0, 0.0, rank_in_list=10)
    with_synergy = match_score(70.0, 30.0, rank_in_list=10)
    # Synergy 30% caps at +20 → 90.
    assert with_synergy - no_synergy <= 20


def test_match_score_rank_bonus_top_first():
    """Top of list gets a small bonus over rank 10."""
    top = match_score(60.0, 0.0, rank_in_list=0)
    bottom = match_score(60.0, 0.0, rank_in_list=10)
    assert top > bottom


def test_match_score_clamped_to_1_100():
    """Even extreme inputs must produce a value in [1, 100]."""
    very_high = match_score(99.0, 50.0, rank_in_list=0)
    very_low = match_score(0.0, 0.0, rank_in_list=10)
    assert 1 <= very_high <= 100
    assert 1 <= very_low <= 100


# ---------------------------------------------------------------------------
# _read_main_with_quantities
# ---------------------------------------------------------------------------

def test_read_main_parses_qty_name(tmp_path):
    deck = tmp_path / "test.dck"
    deck.write_text(
        "[metadata]\nMoxfield=abc\n[Commander]\n1 My Commander\n"
        "[Main]\n4 Forest\n2 Lightning Bolt\n1 Sol Ring\n",
        encoding="utf-8",
    )
    out = _read_main_with_quantities(deck)
    assert out == [("Forest", 4), ("Lightning Bolt", 2), ("Sol Ring", 1)]


def test_read_main_returns_empty_for_missing_file(tmp_path):
    assert _read_main_with_quantities(tmp_path / "missing.dck") == []


def test_read_main_skips_set_collector_suffix(tmp_path):
    """Lines with |SET|CN markers should still parse name correctly."""
    deck = tmp_path / "test.dck"
    deck.write_text(
        "[Main]\n1 Forest|MID|275\n",
        encoding="utf-8",
    )
    out = _read_main_with_quantities(deck)
    assert out == [("Forest", 1)]


# ---------------------------------------------------------------------------
# build_dashboard end-to-end (mocked Scryfall lookups)
# ---------------------------------------------------------------------------

def _write_simple_deck(tmp_path: Path, name: str = "test.dck") -> Path:
    """Synthesize a simple .dck file."""
    p = tmp_path / name
    p.write_text(
        "[metadata]\nMoxfield=test\n"
        "[Commander]\n1 Omnath, Locus of Creation\n"
        "[Main]\n"
        + ("1 Forest\n" * 37)
        + "1 Lotus Cobra\n"
        + "1 Cultivate\n"
        + "1 Wrath of God\n"
        + "1 Lightning Bolt\n",
        encoding="utf-8",
    )
    return p


def test_build_dashboard_returns_all_panels(tmp_path, monkeypatch):
    """All seven UI panels should be present in the result."""
    deck = _write_simple_deck(tmp_path)

    def fake_lookup(name):
        return {
            "Omnath, Locus of Creation": {
                "type_line": "Legendary Creature — Elemental Incarnation",
                "color_identity": ["W", "U", "R", "G"],
                "cmc": 4.0,
            },
            "Forest": {
                "type_line": "Basic Land — Forest",
                "oracle_text": "{T}: Add {G}.",
                "cmc": 0.0,
            },
            "Lotus Cobra": {
                "type_line": "Creature — Snake",
                "oracle_text": "Whenever a land enters the battlefield "
                               "under your control, add one mana of any "
                               "color.",
                "cmc": 2.0,
                "prices": {"usd": "8.00"},
            },
            "Cultivate": {
                "type_line": "Sorcery",
                "oracle_text": "Search your library for up to two basic "
                               "land cards.",
                "cmc": 3.0,
                "prices": {"usd": "0.50"},
            },
            "Wrath of God": {
                "type_line": "Sorcery",
                "oracle_text": "Destroy all creatures.",
                "cmc": 4.0,
                "prices": {"usd": "5.00"},
            },
            "Lightning Bolt": {
                "type_line": "Instant",
                "oracle_text": "Lightning Bolt deals 3 damage to any target.",
                "cmc": 1.0,
                "prices": {"usd": "1.50"},
            },
        }.get(name)

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", fake_lookup,
    )

    result = build_dashboard(deck, bracket=3)

    assert isinstance(result, DashboardData)
    # Commander panel.
    assert result.commander["name"] == "Omnath, Locus of Creation"
    # Deck progress.
    assert result.deck_progress["target"] == 100
    assert result.deck_progress["current"] >= 40
    # Stat tiles.
    assert result.stat_tiles["lands"] == 37  # 37 forests
    assert result.stat_tiles["est_price_usd"] == pytest.approx(15.0)
    # Mana curve has 0..6 buckets.
    curve_buckets = [b for b, _ in result.mana_curve]
    for b in range(7):
        assert b in curve_buckets
    # Categories slot for every display category.
    for cat in DISPLAY_CATEGORIES:
        assert cat in result.categories
    # We had Cultivate (ramp), Wrath of God (wipe), Lightning Bolt (removal),
    # Lotus Cobra (land_payoff trigger).
    assert result.categories["ramp"] >= 1
    assert result.categories["wipe"] >= 1
    assert result.categories["removal"] >= 1
    assert result.categories["land_payoff"] >= 1


def test_build_dashboard_with_suggestions_adds_match_pct(tmp_path, monkeypatch):
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: {"prices": {"usd": "3.00"}, "cmc": 0.0,
                   "type_line": "Land", "oracle_text": ""},
    )
    suggestions = [
        {"card": "Field of the Dead", "inclusion_pct": 80.0,
         "synergy_pct": 30.0, "rationale": "Landfall payoff"},
        {"card": "Scute Swarm", "inclusion_pct": 60.0,
         "synergy_pct": 50.0, "rationale": "Token landfall"},
    ]
    result = build_dashboard(deck, bracket=3, suggested=suggestions)
    assert len(result.suggested_adds) == 2
    field_dead = result.suggested_adds[0]
    assert field_dead["card"] == "Field of the Dead"
    assert 80 <= field_dead["match_pct"] <= 100
    assert field_dead["price_usd"] == 3.0
    assert "Landfall" in field_dead["rationale"]


def test_build_dashboard_to_dict_serializable(tmp_path, monkeypatch):
    """The DashboardData should round-trip through json.dumps cleanly
    so it can be served by the future Flask layer."""
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: None,
    )
    result = build_dashboard(deck, bracket=3)
    serialized = json.dumps(result.to_dict())
    assert "commander" in serialized
    assert "stat_tiles" in serialized


# ---------------------------------------------------------------------------
# theme_tags — tribal detection wired into dashboard (2026-05-13 fix)
# ---------------------------------------------------------------------------

def _write_dragon_tribal_deck(tmp_path: Path) -> Path:
    """Synthetic Dragon-tribal deck: The Ur-Dragon commander + 8
    Dragon creatures + filler. Mirrors the Wyrm Sovereign B4 deck
    shape that surfaced the original 2026-05-13 bug (theme pill
    showed only "Aggro", not "Dragon")."""
    p = tmp_path / "[USER] Wyrm Sovereign [B4].dck"
    main = (
        "1 Mountain\n" * 30
        + "".join(f"1 Dragon{i}\n" for i in range(8))
        + "1 Sol Ring\n"
        + "1 Lightning Bolt\n"
    )
    p.write_text(
        "[metadata]\nMoxfield=ur-dragon-test\n"
        "[Commander]\n1 The Ur-Dragon\n"
        "[Main]\n" + main,
        encoding="utf-8",
    )
    return p


def test_build_dashboard_surfaces_tribe_from_commander_oracle(
    tmp_path, monkeypatch,
):
    """Regression (2026-05-13 chrome audit): the Ur-Dragon deck
    rendered with only "Aggro" in the theme pills, no "Dragon",
    even though detect_tribal_type would have caught it from the
    commander's oracle text. The dashboard's theme_tags aggregator
    didn't call detect_tribal_type — fixed by wiring it in
    alongside the archetype classifier.
    """
    deck = _write_dragon_tribal_deck(tmp_path)

    def fake_lookup(name):
        if name == "The Ur-Dragon":
            # Real oracle text mentions Dragon several times.
            return {
                "type_line": "Legendary Creature — Elder Dragon Avatar",
                "color_identity": ["W", "U", "B", "R", "G"],
                "cmc": 9.0,
                "oracle_text": (
                    "Eminence — As long as The Ur-Dragon is in the command "
                    "zone or on the battlefield, other Dragon spells you cast "
                    "cost {1} less to cast. Flying. Whenever one or more "
                    "Dragons you control attack, draw that many cards, then "
                    "you may put a permanent card from your hand onto the "
                    "battlefield."
                ),
            }
        if name == "Mountain":
            return {
                "type_line": "Basic Land — Mountain",
                "oracle_text": "{T}: Add {R}.",
                "cmc": 0.0,
            }
        if name.startswith("Dragon"):
            return {
                "type_line": "Creature — Dragon",
                "oracle_text": "Flying.",
                "cmc": 5.0,
            }
        return None

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", fake_lookup,
    )
    result = build_dashboard(deck, bracket=4)
    # Tribal tag must appear, distinct from the archetype tag.
    assert any("Dragon tribal" in t for t in result.theme_tags), (
        f"expected 'Dragon tribal' in theme_tags, got {result.theme_tags!r}"
    )


def test_build_dashboard_tribe_falls_back_to_deck_subtype_frequency(
    tmp_path, monkeypatch,
):
    """When the commander itself isn't explicitly tribal (e.g.
    Maelstrom Wanderer goodstuff commander) but the deck runs a
    heavy tribal package, the dashboard should still surface the
    tribe — caught by the secondary subtype-frequency pass over
    the actual deck creatures. The ≥6-of-one-subtype floor is
    high enough that random toolbox decks don't get spuriously
    tagged but low enough that a real tribal deck always lands.
    """
    deck = tmp_path / "tribal-goodstuff.dck"
    deck.write_text(
        "[metadata]\nMoxfield=tribal-test\n"
        "[Commander]\n1 Generic Commander\n"
        "[Main]\n"
        + ("1 Mountain\n" * 30)
        + "".join(f"1 Goblin{i}\n" for i in range(8)),
        encoding="utf-8",
    )

    def fake_lookup(name):
        if name == "Generic Commander":
            # Oracle has zero tribal references.
            return {
                "type_line": "Legendary Creature — Human Wizard",
                "color_identity": ["R"],
                "cmc": 4.0,
                "oracle_text": "Draw a card.",
            }
        if name == "Mountain":
            return {
                "type_line": "Basic Land — Mountain",
                "oracle_text": "{T}: Add {R}.",
                "cmc": 0.0,
            }
        if name.startswith("Goblin"):
            return {
                "type_line": "Creature — Goblin",
                "oracle_text": "Haste.",
                "cmc": 2.0,
            }
        return None

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )
    result = build_dashboard(deck, bracket=3)
    assert any("Goblin tribal" in t for t in result.theme_tags), (
        f"expected 'Goblin tribal' fallback, got {result.theme_tags!r}"
    )


def test_build_dashboard_no_tribal_tag_for_random_creature_mix(
    tmp_path, monkeypatch,
):
    """Conservative floor: a deck with ≤5 of any single creature
    subtype shouldn't get a tribal tag — that's just a normal
    toolbox of creatures, not an intentional tribal build. Without
    this floor, every deck running 5 spell-slingers would
    spuriously read "Wizard tribal."
    """
    deck = tmp_path / "toolbox.dck"
    deck.write_text(
        "[metadata]\nMoxfield=toolbox-test\n"
        "[Commander]\n1 Generic Commander\n"
        "[Main]\n"
        + ("1 Mountain\n" * 30)
        # 3 goblins, 3 elves, 3 wizards — no clear tribal signal.
        + "1 Goblin1\n1 Goblin2\n1 Goblin3\n"
        + "1 Elf1\n1 Elf2\n1 Elf3\n"
        + "1 Wiz1\n1 Wiz2\n1 Wiz3\n",
        encoding="utf-8",
    )

    def fake_lookup(name):
        if name == "Generic Commander":
            return {
                "type_line": "Legendary Creature — Human",
                "color_identity": ["R"],
                "cmc": 4.0,
                "oracle_text": "",
            }
        if name == "Mountain":
            return {
                "type_line": "Basic Land — Mountain",
                "oracle_text": "{T}: Add {R}.",
                "cmc": 0.0,
            }
        if name.startswith("Goblin"):
            return {"type_line": "Creature — Goblin", "cmc": 2.0,
                    "oracle_text": ""}
        if name.startswith("Elf"):
            return {"type_line": "Creature — Elf", "cmc": 2.0,
                    "oracle_text": ""}
        if name.startswith("Wiz"):
            return {"type_line": "Creature — Human Wizard", "cmc": 3.0,
                    "oracle_text": ""}
        return None

    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card", fake_lookup,
    )
    result = build_dashboard(deck, bracket=3)
    # No tribal tag should fire.
    tribal_tags = [t for t in result.theme_tags if "tribal" in t]
    assert tribal_tags == [], (
        f"expected no tribal tag, got {tribal_tags!r}"
    )


# ---------------------------------------------------------------------------
# Salt-list cross-reference
# ---------------------------------------------------------------------------

def test_build_dashboard_surfaces_salt_cards(tmp_path, monkeypatch):
    """Dashboard payload should count + list this deck's salt-list cards.

    The /top/salt page is the EDHREC-canonical "cards opponents hate
    seeing" ranking. A B1-B3 user reviewing their deck should be able
    to glance at the pill below the Categories grid and see whether
    they're packing many high-salt picks.
    """
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: {"type_line": "Sorcery", "oracle_text": "", "cmc": 1.0,
                   "color_identity": [], "prices": {"usd": "1.00"}},
    )
    # Three of the deck's cards are salty per the stubbed list;
    # Forest is not. The dashboard should count exactly 3 and sort by
    # score descending.
    monkeypatch.setattr(
        "commander_builder.edhrec_client.fetch_salt_list",
        lambda *a, **kw: {
            "lotus cobra": 2.10,
            "cultivate": 1.30,
            "wrath of god": 3.45,
            "rhystic study": 4.20,  # not in deck — must not be counted
        },
    )

    result = build_dashboard(deck, bracket=3)

    assert result.legality["salt_cards_count"] == 3
    cards = result.legality["salt_cards"]
    assert len(cards) == 3
    # Sorted by score descending — Wrath (3.45) first.
    assert cards[0]["name"] == "Wrath of God"
    assert cards[0]["score"] == pytest.approx(3.45)
    # All entries carry name + score.
    for entry in cards:
        assert "name" in entry and "score" in entry


def test_build_dashboard_handles_salt_list_fetch_failure(tmp_path, monkeypatch):
    """fetch_salt_list failing must not break the dashboard."""
    deck = _write_simple_deck(tmp_path)
    monkeypatch.setattr(
        "commander_builder.deck_dashboard.lookup_card",
        lambda n: {"type_line": "Land", "oracle_text": "", "cmc": 0.0},
    )
    def boom(*a, **kw):
        raise RuntimeError("salt CDN down")
    monkeypatch.setattr(
        "commander_builder.edhrec_client.fetch_salt_list", boom,
    )
    result = build_dashboard(deck, bracket=3)
    # Graceful degradation: count is 0, list is empty.
    assert result.legality["salt_cards_count"] == 0
    assert result.legality["salt_cards"] == []
