"""Tests for standalone helpers in commander_builder.web._helpers."""
from __future__ import annotations


# --------------------------------------------------------------------------- #
# decks_containing_card -- cross-deck library search (FP-007)
# --------------------------------------------------------------------------- #
def test_decks_containing_card_lists_decks_with_the_card(tmp_path):
    from commander_builder.web._helpers import decks_containing_card
    (tmp_path / "Alpha [B3].dck").write_text(
        "[Commander]\n1 Atraxa, Praetors' Voice\n"
        "[Main]\n1 Sol Ring|CLB|871\n9 Forest\n",
        encoding="utf-8")
    (tmp_path / "Beta [B4].dck").write_text(
        "[Commander]\n1 Krenko, Mob Boss\n[Main]\n1 Lightning Bolt\n",
        encoding="utf-8")
    # qty + |SET|CN stripped, case-insensitive
    assert decks_containing_card(tmp_path, "sol ring") == ["Alpha [B3]"]
    # commander section counts; comma-in-name preserved
    assert decks_containing_card(tmp_path, "Atraxa, Praetors' Voice") == ["Alpha [B3]"]
    assert decks_containing_card(tmp_path, "Forest") == ["Alpha [B3]"]
    # absent -> empty
    assert decks_containing_card(tmp_path, "Counterspell") == []
