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


# --------------------------------------------------------------------------- #
# suggested_verdict -- the server-computed save default (R2-P20)
# --------------------------------------------------------------------------- #
#
# The browser used to pick this itself with the ERA-3 rule the 2026-08-14
# significance fix retired: `decisive < 20 ? "inconclusive" : winner ===
# "new" ? "kept" : ...`, where ComparisonReport.winner is ANY lead. Those
# defaults were stored verbatim into rows stamped with the CURRENT era, so
# a 21-20 pre-checked "Kept". The suggestion now runs the same exact
# binomial test + decisive floor every CLI writer uses.

def test_suggested_verdict_refuses_a_coin_flip_lead_at_20_plus_decisive():
    """THE BUG: 21-20 over 41 decisive games (exact two-sided p ~= 1.0)
    used to pre-check "Kept (apply changes)"."""
    from commander_builder.web._helpers import suggested_verdict
    out = suggested_verdict(20, 21)
    assert out["verdict"] == "neutral"
    assert out["decisive"] == 41
    assert out["p_value"] > 0.05
    assert "p=" in out["basis"]


def test_suggested_verdict_keeps_a_significant_new_deck_lead():
    from commander_builder.web._helpers import suggested_verdict
    out = suggested_verdict(15, 30)          # p = 0.036 < 0.05
    assert out["verdict"] == "kept"
    assert out["p_value"] < out["alpha"]


def test_suggested_verdict_reverts_a_significant_old_deck_lead():
    from commander_builder.web._helpers import suggested_verdict
    out = suggested_verdict(30, 15)
    assert out["verdict"] == "reverted"
    assert out["p_value"] < out["alpha"]


def test_suggested_verdict_is_inconclusive_below_the_decisive_floor():
    """A 3-2 is a coin flip, not a kept — and not a 'neutral' either:
    'neutral' is a trustworthy near-tie, 'inconclusive' is "not enough
    games to say"."""
    from commander_builder.web._helpers import suggested_verdict
    out = suggested_verdict(2, 3)
    assert out["verdict"] == "inconclusive"
    assert out["p_value"] is None
    assert out["decisive"] == 5
    assert str(out["min_decisive"]) in out["basis"]


def test_suggested_verdict_matches_the_cli_verdict_rule():
    """Same inputs, same label as _proposer_sim._verdict_from_ab — the
    point of computing it server-side is that there is ONE rule."""
    from types import SimpleNamespace
    from commander_builder._proposer_sim import _verdict_from_ab
    from commander_builder.web._helpers import suggested_verdict
    for old_w, new_w in [(20, 21), (15, 30), (30, 15), (2, 3), (0, 0),
                         (10, 10), (13, 27), (18, 23)]:
        ab = SimpleNamespace(status="done", wins_a=old_w, wins_b=new_w,
                             games=old_w + new_w)
        assert suggested_verdict(old_w, new_w)["verdict"] == _verdict_from_ab(ab)


def test_suggested_verdict_tolerates_missing_and_junk_win_counts():
    """Hand-built payloads must not 500 the save path."""
    from commander_builder.web._helpers import suggested_verdict
    assert suggested_verdict(None, None)["verdict"] == "inconclusive"
    assert suggested_verdict("x", 3)["verdict"] == "inconclusive"
