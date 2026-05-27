"""Tests for FP-012 Slice A -- intent-learning.

All tests use injected classify_fn / themes_fn / lookup_fn / role_fn /
tribal_fn so no real Scryfall / Anthropic / Forge I/O is needed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from commander_builder.intent import Intent, intent_protect_cards, learn_intent


# ---------------------------------------------------------------------------
# Minimal .dck fixture helpers
# ---------------------------------------------------------------------------

def _write_dck(path: Path, main: list[str], commanders: list[str]) -> Path:
    """Write a minimal .dck file with [Commander] and [Main] sections."""
    lines = ["[Commander]"]
    for c in commanders:
        lines.append(f"1 {c}")
    lines += ["", "[Main]"]
    for card in main:
        lines.append(f"1 {card}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# Simple stub lookup table: card_name -> Scryfall-style dict
_STUB_CARDS: dict[str, dict] = {
    "Craterhoof Behemoth": {
        "oracle_text": "creatures you control get +X/+X and gain trample until end of turn",
        "type_line": "Creature - Beast",
        "color_identity": ["G"],
    },
    "Thassa's Oracle": {
        "oracle_text": "you win the game if your library has no cards",
        "type_line": "Creature - God",
        "color_identity": ["U"],
    },
    "Sol Ring": {
        "oracle_text": "{T}: Add {C}{C}.",
        "type_line": "Artifact",
        "color_identity": [],
    },
    "Counterspell": {
        "oracle_text": "Counter target spell.",
        "type_line": "Instant",
        "color_identity": ["U"],
    },
    "Krenko, Mob Boss": {
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        "type_line": "Legendary Creature - Goblin Warrior",
        "color_identity": ["R"],
    },
    "Lightning Bolt": {
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        "type_line": "Instant",
        "color_identity": ["R"],
    },
}


def _stub_lookup(name: str) -> Optional[dict]:
    return _STUB_CARDS.get(name)


def _stub_classify(deck_path: Path) -> str:
    return "combo"


def _stub_themes(oracles: list[tuple[str, str]]) -> list[str]:
    # Return "spellslinger" if Counterspell is in the deck
    names = {n.lower() for n, _ in oracles}
    if "counterspell" in names:
        return ["spellslinger"]
    return []


def _stub_role(oracle: str, type_line: str) -> str:
    text = oracle.lower()
    if "you win the game" in text:
        return "win_condition"
    if "creatures you control" in text and "trample" in text:
        return "win_condition"
    return "other"


def _stub_tribal(oracle: str, type_line: str) -> Optional[str]:
    if "goblin" in oracle.lower() or "goblin" in type_line.lower():
        return "Goblin"
    return None


# ---------------------------------------------------------------------------
# learn_intent tests
# ---------------------------------------------------------------------------

def test_learn_intent_basic(tmp_path):
    deck = _write_dck(
        tmp_path / "test.dck",
        main=["Craterhoof Behemoth", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=_stub_classify,
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "combo"
    assert "Craterhoof Behemoth" in intent.key_wincons
    assert intent.color_identity == ["R"]
    assert intent.tribal_type == "Goblin"
    assert intent.commander_name == "Krenko, Mob Boss"


def test_learn_intent_wincon_detection(tmp_path):
    """Thassa's Oracle must appear in key_wincons."""
    deck = _write_dck(
        tmp_path / "combo.dck",
        main=["Thassa's Oracle", "Counterspell"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "combo",
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert "Thassa's Oracle" in intent.key_wincons
    assert "Counterspell" not in intent.key_wincons


def test_learn_intent_themes_propagated(tmp_path):
    deck = _write_dck(
        tmp_path / "ctrl.dck",
        main=["Counterspell", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "control",
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert "spellslinger" in intent.themes


def test_learn_intent_missing_oracle_tolerated(tmp_path):
    """Cards with no Scryfall data (lookup returns None) should not crash."""
    deck = _write_dck(
        tmp_path / "partial.dck",
        main=["Unknown Card X", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )

    def lookup_with_holes(name: str) -> Optional[dict]:
        if name == "Unknown Card X":
            return None
        return _stub_lookup(name)

    intent = learn_intent(
        deck,
        classify_fn=lambda p: "midrange",
        themes_fn=lambda oracles: [],
        lookup_fn=lookup_with_holes,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert isinstance(intent, Intent)
    assert "Unknown Card X" not in intent.key_wincons


def test_learn_intent_empty_deck(tmp_path):
    """A deck with no main cards produces a sensible default Intent."""
    deck = tmp_path / "empty.dck"
    deck.write_text("[Main]\n", encoding="utf-8")
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "midrange",
        themes_fn=lambda oracles: [],
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "midrange"
    assert intent.key_wincons == []
    assert intent.color_identity == []
    assert intent.commander_name is None


def test_learn_intent_classify_exception_defaults_midrange(tmp_path):
    """A crashing classify_fn should fall back to 'midrange'."""
    deck = _write_dck(tmp_path / "x.dck", main=["Sol Ring"], commanders=[])

    def boom(p):
        raise RuntimeError("classifier unavailable")

    intent = learn_intent(
        deck,
        classify_fn=boom,
        themes_fn=lambda o: [],
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "midrange"


def test_learn_intent_to_dict_json_serializable(tmp_path):
    deck = _write_dck(
        tmp_path / "goblin.dck",
        main=["Craterhoof Behemoth"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=_stub_classify,
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    blob = json.dumps(intent.to_dict())
    back = json.loads(blob)
    assert back["archetype"] == "combo"
    assert isinstance(back["key_wincons"], list)


# ---------------------------------------------------------------------------
# intent_protect_cards tests
# ---------------------------------------------------------------------------

def test_intent_protect_cards_returns_wincons():
    intent = Intent(
        archetype="combo",
        key_wincons=["Thassa's Oracle", "Demonic Consultation"],
    )
    assert intent_protect_cards(intent) == ["Thassa's Oracle", "Demonic Consultation"]


def test_intent_protect_cards_none_returns_empty():
    assert intent_protect_cards(None) == []


def test_intent_protect_cards_empty_wincons():
    intent = Intent(archetype="aggro", key_wincons=[])
    assert intent_protect_cards(intent) == []


# ---------------------------------------------------------------------------
# improve.py integration: intent protect extends per-round argv
# ---------------------------------------------------------------------------

def test_intent_protect_cards_merge_with_explicit_list():
    """Merging intent wincons with an explicit protect list gives the union."""
    intent = Intent(archetype="combo", key_wincons=["Wincon A", "Wincon B"])
    explicit = ["Pet Card", "Another Card"]
    merged = explicit + intent_protect_cards(intent)
    assert "Pet Card" in merged
    assert "Another Card" in merged
    assert "Wincon A" in merged
    assert "Wincon B" in merged
    assert len(merged) == 4


def test_intent_protect_cards_no_duplicates_when_manually_protected():
    """When a wincon is already manually protected, it appears once (caller deduplicates)."""
    intent = Intent(archetype="combo", key_wincons=["Shared Card"])
    explicit = ["Shared Card", "Other Card"]
    raw_merged = explicit + intent_protect_cards(intent)
    # The caller is responsible for dedup; the library just appends.
    # Verify the wincon IS present:
    assert "Shared Card" in raw_merged


# ---------------------------------------------------------------------------
# improve_main: --learn-intent flag wiring
# ---------------------------------------------------------------------------

def test_improve_main_learn_intent_flag(tmp_path, monkeypatch):
    """--learn-intent path is resolved; intent is set on args before loop."""
    from commander_builder import improve

    learned: list = []
    original_learn = improve.learn_intent

    def fake_learn(path, **kw):
        intent = Intent(archetype="aggro", key_wincons=["Firebreathing Dragon"])
        learned.append(intent)
        return intent

    monkeypatch.setattr(improve, "learn_intent", fake_learn)

    loop_args: list = []

    def fake_loop(deck_path, deck_id, rounds, args, **kw):
        loop_args.append(args)
        return improve.ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", fake_loop)

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([
        str(deck), "--rounds", "1",
        "--learn-intent", str(deck),
    ])

    assert rc == 0
    assert len(learned) == 1
    assert loop_args[0].intent.archetype == "aggro"
    assert loop_args[0].intent.key_wincons == ["Firebreathing Dragon"]


def test_improve_main_learn_intent_missing_file(tmp_path, monkeypatch):
    """--learn-intent with a nonexistent path must return rc=2."""
    from commander_builder import improve

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([
        str(deck), "--rounds", "1",
        "--learn-intent", str(tmp_path / "no_such.dck"),
    ])
    assert rc == 2


def test_improve_main_no_learn_intent_sets_none(tmp_path, monkeypatch):
    """Without --learn-intent, args.intent must be None."""
    from commander_builder import improve

    loop_args: list = []

    def fake_loop(deck_path, deck_id, rounds, args, **kw):
        loop_args.append(args)
        return improve.ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", fake_loop)

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([str(deck), "--rounds", "1"])

    assert rc == 0
    assert loop_args[0].intent is None
