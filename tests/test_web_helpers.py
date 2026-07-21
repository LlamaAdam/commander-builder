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


# --------------------------------------------------------------------------- #
# _match_pct_from_evidence -- audit match-pct pill scoring
# --------------------------------------------------------------------------- #
def test_match_pct_none_only_when_no_scoring_fields():
    """None is reserved for evidence that carries NO scoring signal at
    all (the UI renders a source-tag badge for null)."""
    from commander_builder.web._helpers import _match_pct_from_evidence
    assert _match_pct_from_evidence(None) is None
    assert _match_pct_from_evidence({}) is None
    assert _match_pct_from_evidence({"unrelated": "field"}) is None


def test_match_pct_negative_synergy_clamps_to_floor_not_none():
    """Regression: negative EDHREC synergy used to drag the raw score
    <= 0, and the function returned None — a real inclusion signal was
    rendered as the 'no data' badge. A weak match must show as a real
    low pct (floor 1), never masquerade as missing data."""
    from commander_builder.web._helpers import _match_pct_from_evidence
    # 2% inclusion, -10% synergy: raw = -8 → floor 1, not None.
    assert _match_pct_from_evidence(
        {"inclusion_pct": 2, "synergy_pct": -10}) == 1
    # Negative synergy alone is still an explicit (bad) signal.
    assert _match_pct_from_evidence({"synergy_pct": -5}) == 1


def test_match_pct_explicit_zeros_are_a_real_low_score():
    """inclusion=0, synergy=0 is DATA (a genuinely unpopular card), not
    absence of data — must not return None."""
    from commander_builder.web._helpers import _match_pct_from_evidence
    assert _match_pct_from_evidence(
        {"inclusion_pct": 0, "synergy_pct": 0}) == 1
    assert _match_pct_from_evidence({"inclusion_pct": 0}) == 1


def test_match_pct_normal_signals_unchanged():
    """Positive-signal paths keep their pre-fix behavior."""
    from commander_builder.web._helpers import _match_pct_from_evidence
    # inclusion + capped synergy: 40 + min(30, 20) = 60.
    assert _match_pct_from_evidence(
        {"inclusion_pct": 40, "synergy_pct": 30}) == 60
    # bracket_peers reference-frequency math takes priority.
    assert _match_pct_from_evidence(
        {"total_references": 5, "in_n_references": 3}) == 60
