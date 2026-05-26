"""Tests for scripts/margin_analysis.py -- the reframed FP-002 margin
regression (regress curator improvement margin on deck features).

Pure-logic tests: row aggregation, Pearson, verdict banding, and the
deck-file join. No Forge, no network, no card DB (deck_health degrades
to zeros offline, which is fine -- we test the join + stats, not the
specific health numbers).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import margin_analysis as ma  # noqa: E402


def _row(a, b, games, wa, wb, status="done"):
    return {"deck_a": a, "deck_b": b, "games": games,
            "wins_a": wa, "wins_b": wb, "status": status}


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_sums_across_rows():
    rows = [
        _row("D.dck", "D v2.dck", 40, 10, 14),
        _row("D.dck", "D v2.dck", 40, 8, 12),
    ]
    pairs = ma.aggregate_pairs(rows, min_games=40)
    p = pairs["D.dck"]
    assert p.wins_a == 18 and p.wins_b == 26 and p.games == 80 and p.rows == 2


def test_min_games_filters_low_confidence_rows():
    rows = [
        _row("D.dck", "D v2.dck", 5, 3, 1),    # dropped
        _row("D.dck", "D v2.dck", 40, 10, 14),  # kept
    ]
    pairs = ma.aggregate_pairs(rows, min_games=40)
    assert pairs["D.dck"].games == 40 and pairs["D.dck"].rows == 1


def test_non_done_rows_ignored_by_loader(tmp_path):
    f = tmp_path / "x_throughput.jsonl"
    import json
    f.write_text(
        json.dumps(_row("D.dck", "D v2.dck", 40, 5, 5)) + "\n"
        + json.dumps(_row("E.dck", "E v2.dck", 40, 5, 5, status="error")) + "\n"
        + "not json\n",
        encoding="utf-8",
    )
    rows = ma.load_rows(str(tmp_path))
    assert len(rows) == 1 and rows[0]["deck_a"] == "D.dck"


# --------------------------------------------------------------------------- #
# margin + verdict
# --------------------------------------------------------------------------- #
def test_margin_is_signed_winrate_delta():
    p = ma.Pair("a", "b", wins_a=10, wins_b=14)  # decisive=24
    assert p.margin == pytest.approx((14 - 10) / 24)


def test_margin_none_without_decisive_games():
    assert ma.Pair("a", "b", wins_a=0, wins_b=0).margin is None


def test_verdict_bands():
    assert ma.Pair("a", "b", wins_a=1, wins_b=99).verdict() == "kept"
    assert ma.Pair("a", "b", wins_a=99, wins_b=1).verdict() == "reverted"
    assert ma.Pair("a", "b", wins_a=50, wins_b=50).verdict() == "neutral"
    assert ma.Pair("a", "b", wins_a=0, wins_b=0).verdict() == "undecided"


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #
def test_pearson_perfect_positive():
    assert ma.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    assert ma.pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_none_on_zero_variance():
    assert ma.pearson([5, 5, 5], [1, 2, 3]) is None
    assert ma.pearson([1], [1]) is None


def test_t_stat_grows_with_n():
    assert ma.t_stat(0.5, 10) < ma.t_stat(0.5, 50)


# --------------------------------------------------------------------------- #
# join + end-to-end analyze
# --------------------------------------------------------------------------- #
_DECK = ("[metadata]\nName=T\n[Commander]\n1 Cmdr|S|1\n[Main]\n"
         "1 Sol Ring|C|1\n10 Forest|J|1\n")


def test_build_samples_joins_deck_file(tmp_path):
    (tmp_path / "[USER] D [B4].dck").write_text(_DECK, encoding="utf-8")
    pairs = ma.aggregate_pairs(
        [_row("[USER] D [B4].dck", "[USER] D v2 [B4].dck", 40, 10, 14)],
        min_games=40)
    samples, skipped = ma.build_samples(pairs, [str(tmp_path)])
    assert len(samples) == 1 and not skipped
    s = samples[0]
    assert s.features["bracket"] == 4.0          # parsed from [B4]
    assert s.features["basic_lands"] == 10.0      # 10 Forest
    assert s.margin == pytest.approx((14 - 10) / 24)


def test_build_samples_reports_missing_deck(tmp_path):
    pairs = ma.aggregate_pairs(
        [_row("[USER] Gone [B3].dck", "[USER] Gone v2 [B3].dck", 40, 5, 7)],
        min_games=40)
    samples, skipped = ma.build_samples(pairs, [str(tmp_path)])
    assert not samples and len(skipped) == 1 and "not found" in skipped[0]


def _grow(*, pair_base, role, games, wins, losses, draws=0):
    return {"mode": "gauntlet", "pair_base": pair_base, "role": role,
            "test_deck": pair_base, "games": games, "wins": wins,
            "losses": losses, "draws": draws, "status": "done"}


# --------------------------------------------------------------------------- #
# gauntlet mode (unconfounded: base & v2 each vs the same fixed gauntlet)
# --------------------------------------------------------------------------- #
def test_gauntlet_aggregates_base_and_v2_separately():
    rows = [
        _grow(pair_base="D.dck", role="base", games=40, wins=10, losses=30),
        _grow(pair_base="D.dck", role="v2", games=40, wins=18, losses=22),
        _grow(pair_base="D.dck", role="v2", games=40, wins=2, losses=38),  # sums
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    p = pairs["D.dck"]
    assert (p.base_w, p.base_l) == (10, 30)
    assert (p.v2_w, p.v2_l) == (20, 60)
    assert p.complete


def test_gauntlet_margin_is_winrate_difference():
    p = ma.GauntletPair("D.dck", base_w=10, base_l=30, v2_w=20, v2_l=20)
    # base wr = 10/40 = .25 ; v2 wr = 20/40 = .5 ; margin = +.25
    assert p.margin == pytest.approx(0.25)
    assert p.verdict() == "kept"


def test_gauntlet_pair_incomplete_without_both_roles():
    p = ma.GauntletPair("D.dck", base_w=10, base_l=30)  # no v2 games
    assert not p.complete and p.margin is None and p.verdict() == "undecided"


def test_gauntlet_ignores_bad_roles_and_low_games():
    rows = [
        _grow(pair_base="D.dck", role="base", games=5, wins=2, losses=3),   # low
        _grow(pair_base="D.dck", role="filler", games=40, wins=1, losses=1),  # bad role
        _grow(pair_base="D.dck", role="base", games=40, wins=10, losses=30),
        _grow(pair_base="D.dck", role="v2", games=40, wins=15, losses=25),
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    p = pairs["D.dck"]
    assert (p.base_w, p.base_l) == (10, 30) and (p.v2_w, p.v2_l) == (15, 25)


def test_build_gauntlet_samples_joins_and_skips(tmp_path):
    (tmp_path / "[USER] D [B4].dck").write_text(_DECK, encoding="utf-8")
    rows = [
        _grow(pair_base="[USER] D [B4].dck", role="base", games=40, wins=10, losses=30),
        _grow(pair_base="[USER] D [B4].dck", role="v2", games=40, wins=20, losses=20),
        _grow(pair_base="[USER] Gone [B3].dck", role="base", games=40, wins=5, losses=35),
        _grow(pair_base="[USER] Gone [B3].dck", role="v2", games=40, wins=5, losses=35),
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    samples, skipped = ma.build_gauntlet_samples(pairs, [str(tmp_path)])
    assert len(samples) == 1 and samples[0].deck == "[USER] D [B4].dck"
    assert samples[0].margin == pytest.approx(0.25)
    assert len(skipped) == 1 and "not found" in skipped[0]


def test_analyze_counts_verdicts_and_ranks_features(tmp_path):
    # Two decks, opposite outcomes -> one kept, one reverted.
    for name in ("[USER] A [B4].dck", "[USER] B [B4].dck"):
        (tmp_path / name).write_text(_DECK, encoding="utf-8")
    rows = [
        _row("[USER] A [B4].dck", "[USER] A v2 [B4].dck", 40, 4, 16),   # kept
        _row("[USER] B [B4].dck", "[USER] B v2 [B4].dck", 40, 16, 4),   # reverted
    ]
    pairs = ma.aggregate_pairs(rows, min_games=40)
    samples, _ = ma.build_samples(pairs, [str(tmp_path)])
    report = ma.analyze(samples)
    assert report["n_decks"] == 2
    assert report["verdicts"]["kept"] == 1
    assert report["verdicts"]["reverted"] == 1
    # feature_correlations is sorted by |r| descending
    rs = [abs(f["pearson_r"]) for f in report["feature_correlations"]
          if f["pearson_r"] is not None]
    assert rs == sorted(rs, reverse=True)
