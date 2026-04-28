"""archetype.classify heuristic tests.

Builds synthetic .dck fixtures, verifies the classifier picks the right
archetype based on filename hint, content scan, and the midrange fallback.
"""
from pathlib import Path

import pytest

from commander_builder.archetype import (
    MIN_CONTENT_MATCHES,
    _content_scan,
    _filename_hint,
    _read_main_card_names,
    claude_archetype,
    classify,
    ollama_archetype,
)


def _write_dck(tmp_path, name: str, cards: list[str]) -> Path:
    """Write a synthetic .dck file with the given main-deck card names."""
    p = tmp_path / name
    body = ["[Commander]", "1 Test Commander", "[Main]"]
    body.extend(f"1 {card}" for card in cards)
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


# --- _read_main_card_names -------------------------------------------------

def test_read_main_card_names_strips_qty_and_set_suffix(tmp_path):
    p = _write_dck(tmp_path, "x.dck", ["Sol Ring|CMM|1", "Mana Crypt"])
    assert _read_main_card_names(p) == ["Sol Ring", "Mana Crypt"]


def test_read_main_card_names_skips_other_sections(tmp_path):
    p = tmp_path / "x.dck"
    p.write_text("[Commander]\n1 Foo\n[Sideboard]\n1 Bar\n[Main]\n1 Baz\n", encoding="utf-8")
    assert _read_main_card_names(p) == ["Baz"]


def test_read_main_card_names_missing_file():
    assert _read_main_card_names(Path("/does/not/exist.dck")) == []


# --- _filename_hint --------------------------------------------------------

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


# --- _content_scan ---------------------------------------------------------

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
    # Only one combo card — below MIN_CONTENT_MATCHES.
    winner, score = _content_scan(["Thassa's Oracle", "Forest", "Plains"])
    assert winner is None
    assert score < MIN_CONTENT_MATCHES


def test_content_scan_handles_empty_input():
    assert _content_scan([]) == (None, 0)


# --- classify (the public entry) -------------------------------------------

def test_classify_filename_hint_wins_over_content(tmp_path):
    """If the filename advertises 'Stax', that beats whatever the content
    scan says — high-confidence user labeling."""
    p = _write_dck(tmp_path, "Stax Prison [B5].dck", [
        "Thassa's Oracle", "Demonic Consultation", "Tainted Pact",  # combo content
    ])
    assert classify(p) == "stax"


def test_classify_uses_content_when_no_filename_hint(tmp_path):
    p = _write_dck(tmp_path, "[USER] Mystery [B4].dck", [
        "Winter Orb", "Static Orb", "Stasis", "Sphere of Resistance",
        "Trinisphere", "Thorn of Amethyst",
    ])
    assert classify(p) == "stax"


def test_classify_falls_back_to_midrange(tmp_path):
    p = _write_dck(tmp_path, "[USER] Something [B3].dck", [
        "Sol Ring", "Forest", "Plains", "Island",
    ])
    assert classify(p) == "midrange"


def test_classify_handles_missing_file(tmp_path):
    """Should NOT crash on missing files — return midrange so the curator
    keeps running."""
    assert classify(tmp_path / "ghost.dck") == "midrange"


def test_classify_picks_aggro_from_filename(tmp_path):
    p = _write_dck(tmp_path, "[USER] Voltron Brawler [B3].dck", [
        "Sol Ring", "Forest",  # generic — content scan won't trigger
    ])
    assert classify(p) == "aggro"


# --- LLM stubs -------------------------------------------------------------

def test_claude_archetype_is_unimplemented(tmp_path):
    with pytest.raises(NotImplementedError):
        claude_archetype(tmp_path / "x.dck")


def test_ollama_archetype_is_unimplemented(tmp_path):
    with pytest.raises(NotImplementedError):
        ollama_archetype(tmp_path / "x.dck")
