"""Tests for scripts/margin_analysis.py -- the reframed FP-002 margin
regression (regress curator improvement margin on deck features).

Pure-logic tests: row aggregation, Pearson, verdict banding, and the
deck-file join. No Forge, no network, no card DB -- we test the join +
stats, not the specific health numbers, so an autouse fixture pins
``compute_deck_health`` to a deterministic stub. The REAL function
resolves card types through ``scryfall_client.lookup_card``, which
consults the live network plus mutable cross-run state (the shared
disk snapshot dir and a process-wide memo), so two ``main()`` calls in
one test could resolve differently -- the PR #63 CI flake in
``test_default_features_output_is_byte_identical``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import margin_analysis as ma  # noqa: E402


# A fully-RESOLVED deck-health shape covering every key deck_features
# reads. Constant across decks on purpose: these tests assert on the
# join + stats, never on real health numbers.
_HEALTH_STUB = {
    "spell_density": {"non_permanent_count": 3, "total_main_count": 11,
                      "ratio": 3 / 11, "lookup_failures": 0},
    "mana_sinks": {"count": 2},
    "wincon_protection": {"count": 1},
    "self_mill": {"count": 0},
    "mdfc": {"count": 1},
    "role_targets": {
        "roles": {"ramp": {"count": 1, "target": 10, "base_target": 10,
                           "commander_credit": 0, "deficit": 9},
                  "draw": {"count": 0, "target": 8, "base_target": 8,
                           "commander_credit": 0, "deficit": 8}},
        "under_built": ["ramp", "draw"],
    },
}


@pytest.fixture(autouse=True)
def _hermetic_deck_health(monkeypatch):
    """Pin ``compute_deck_health`` to a pure deterministic stub.

    ``deck_features`` resolves the function from the module at call time
    (``from commander_builder.deck_health import compute_deck_health``
    inside the function body), so patching the module attribute is the
    established seam -- same pattern as the ``score_deck`` /
    ``assign_cluster`` stubs used elsewhere in this file.

    Why autouse: the real function type-classifies cards through
    ``scryfall_client.lookup_card`` -- live network plus two layers of
    cross-run mutable state (the shared disk snapshot dir and the
    process-wide lookup memo, which persists ACROSS the multiple
    ``main()`` calls inside one test). On CI that made
    ``test_default_features_output_is_byte_identical`` flake: a
    transient lookup failure degraded one deck's features in run 1
    while run 2 resolved from the freshly warmed memo/disk cache, so
    the two runs' bytes differed. Stubbing the seam makes every test in
    this file offline-deterministic by construction; the byte-identical
    assertion itself (the --features flag-off contract) is unchanged.
    """
    import copy

    from commander_builder import deck_health as _dh
    monkeypatch.setattr(_dh, "compute_deck_health",
                        lambda deck_text: copy.deepcopy(_HEALTH_STUB))


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


def test_loader_accepts_loop_unattributed_short_rows(tmp_path):
    """'loop_unattributed' rows (batch cut by a looping game no seat could be
    credited for) carry only COMPLETED games — they load like 'done' rows and
    are gated by min_games downstream, not discarded as errors."""
    f = tmp_path / "x_throughput.jsonl"
    import json
    f.write_text(
        json.dumps(_row("D.dck", "D v2.dck", 17, 5, 9,
                        status="loop_unattributed")) + "\n"
        + json.dumps(_row("E.dck", "E v2.dck", 40, 5, 5, status="failed")) + "\n",
        encoding="utf-8",
    )
    rows = ma.load_rows(str(tmp_path))
    assert len(rows) == 1 and rows[0]["deck_a"] == "D.dck"
    # min_games still gates the short row out of a 40-game aggregation.
    assert ma.aggregate_pairs(rows, min_games=40) == {}
    assert "D.dck" in ma.aggregate_pairs(rows, min_games=0)


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
    samples, skipped, missing = ma.build_samples(pairs, [str(tmp_path)])
    assert len(samples) == 1 and not skipped and not missing
    s = samples[0]
    assert s.features["bracket"] == 4.0          # parsed from [B4]
    assert s.features["basic_lands"] == 10.0      # 10 Forest
    assert s.margin == pytest.approx((14 - 10) / 24)


def test_build_samples_reports_missing_deck(tmp_path, capsys):
    pairs = ma.aggregate_pairs(
        [_row("[USER] Gone [B3].dck", "[USER] Gone v2 [B3].dck", 40, 5, 7),
         _row("[USER] Gone [B3].dck", "[USER] Gone v2 [B3].dck", 40, 6, 8)],
        min_games=40)
    samples, skipped, missing = ma.build_samples(pairs, [str(tmp_path)])
    # excluded from the regression ...
    assert not samples and len(skipped) == 1 and "not found" in skipped[0]
    # ... counted (2 soak rows dropped for this one missing deck) ...
    assert missing == {"[USER] Gone [B3].dck": 2}
    # ... and warned LOUDLY on stderr, once per missing deck.
    err = capsys.readouterr().err
    assert err.count("WARNING: deck file not found") == 1
    assert "[USER] Gone [B3].dck" in err and "2 soak row(s)" in err


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


def test_gauntlet_aggregation_includes_premade_pairs(tmp_path):
    # FP-002's unit is pair_base regardless of role prefix: minted
    # [PREMADE] pairs (commander_builder.premade_mint) aggregate and
    # join exactly like [USER] pairs -- no prefix filter anywhere.
    (tmp_path / "[PREMADE] Popular [B4].dck").write_text(_DECK, encoding="utf-8")
    rows = [
        _grow(pair_base="[PREMADE] Popular [B4].dck", role="base",
              games=40, wins=10, losses=30),
        _grow(pair_base="[PREMADE] Popular [B4].dck", role="v2",
              games=40, wins=20, losses=20),
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    samples, skipped, missing = ma.build_gauntlet_samples(pairs, [str(tmp_path)])
    assert len(samples) == 1 and samples[0].deck == "[PREMADE] Popular [B4].dck"
    assert samples[0].margin == pytest.approx(0.25)
    assert skipped == [] and missing == {}


def test_build_gauntlet_samples_joins_and_skips(tmp_path):
    (tmp_path / "[USER] D [B4].dck").write_text(_DECK, encoding="utf-8")
    rows = [
        _grow(pair_base="[USER] D [B4].dck", role="base", games=40, wins=10, losses=30),
        _grow(pair_base="[USER] D [B4].dck", role="v2", games=40, wins=20, losses=20),
        _grow(pair_base="[USER] Gone [B3].dck", role="base", games=40, wins=5, losses=35),
        _grow(pair_base="[USER] Gone [B3].dck", role="v2", games=40, wins=5, losses=35),
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    samples, skipped, missing = ma.build_gauntlet_samples(pairs, [str(tmp_path)])
    assert len(samples) == 1 and samples[0].deck == "[USER] D [B4].dck"
    assert samples[0].margin == pytest.approx(0.25)
    assert len(skipped) == 1 and "not found" in skipped[0]
    assert missing == {"[USER] Gone [B3].dck": 2}   # 2 rows (base + v2) dropped


def test_gauntlet_missing_deck_warns_once_and_counts_rows(tmp_path, capsys):
    rows = [
        _grow(pair_base="[USER] Gone [B3].dck", role="base", games=40, wins=5, losses=35),
        _grow(pair_base="[USER] Gone [B3].dck", role="base", games=40, wins=6, losses=34),
        _grow(pair_base="[USER] Gone [B3].dck", role="v2", games=40, wins=5, losses=35),
    ]
    pairs = ma.aggregate_gauntlet(rows, min_games=40)
    samples, skipped, missing = ma.build_gauntlet_samples(pairs, [str(tmp_path)])
    assert not samples                                # excluded from the math
    assert missing == {"[USER] Gone [B3].dck": 3}     # all 3 rows counted
    err = capsys.readouterr().err
    assert err.count("WARNING: deck file not found") == 1   # once per deck
    assert "3 soak row(s)" in err


def test_main_reports_missing_deck_totals_in_json(tmp_path, capsys):
    import json
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "x_throughput.jsonl").write_text(
        json.dumps(_row("[USER] Gone [B3].dck", "[USER] Gone v2 [B3].dck",
                        40, 5, 7)) + "\n",
        encoding="utf-8")
    decks = tmp_path / "decks"          # empty -> the deck is missing
    decks.mkdir()
    rc = ma.main(["--inbox", str(inbox), "--decks", str(decks),
                  "--min-games", "40", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["missing_deck_files"] == {"[USER] Gone [B3].dck": 1}
    assert report["missing_deck_rows_dropped"] == 1
    assert "WARNING: deck file not found" in captured.err


def test_analyze_counts_verdicts_and_ranks_features(tmp_path):
    # Two decks, opposite outcomes -> one kept, one reverted.
    for name in ("[USER] A [B4].dck", "[USER] B [B4].dck"):
        (tmp_path / name).write_text(_DECK, encoding="utf-8")
    rows = [
        _row("[USER] A [B4].dck", "[USER] A v2 [B4].dck", 40, 4, 16),   # kept
        _row("[USER] B [B4].dck", "[USER] B v2 [B4].dck", 40, 16, 4),   # reverted
    ]
    pairs = ma.aggregate_pairs(rows, min_games=40)
    samples, _, _ = ma.build_samples(pairs, [str(tmp_path)])
    report = ma.analyze(samples)
    assert report["n_decks"] == 2
    assert report["verdicts"]["kept"] == 1
    assert report["verdicts"]["reverted"] == 1
    # feature_correlations is sorted by |r| descending
    rs = [abs(f["pearson_r"]) for f in report["feature_correlations"]
          if f["pearson_r"] is not None]
    assert rs == sorted(rs, reverse=True)


# --------------------------------------------------------------------------- #
# single_feature_ols -- the analysis->predictor step (FP-002)
# --------------------------------------------------------------------------- #
def test_single_feature_ols_recovers_linear_signal():
    # margin = 2 * feature exactly -> slope 2, r2 1, ~0 out-of-sample error.
    samples = [
        ma.Sample(deck=f"d{i}", margin=2.0 * i, games=40, features={"f": float(i)})
        for i in range(8)
    ]
    res = ma.single_feature_ols(samples, "f")
    assert res["n"] == 8
    assert abs(res["slope"] - 2.0) < 1e-6
    assert res["r2"] > 0.99
    assert res["loo_rmse"] < 1e-6


def test_single_feature_ols_constant_feature_is_safe():
    samples = [
        ma.Sample(deck=f"d{i}", margin=0.1 * i, games=40, features={"f": 5.0})
        for i in range(5)
    ]
    res = ma.single_feature_ols(samples, "f")
    assert res["slope"] == 0.0   # no variance -> no slope, not a crash
    assert res["r2"] == 0.0


# --------------------------------------------------------------------------- #
# --features substrates (FP-002 reopening probe)
# --------------------------------------------------------------------------- #
def _fixture_inbox(tmp_path):
    """A tiny inbox + deck dir the CLI can run end-to-end against."""
    import json
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    decks = tmp_path / "decks"
    decks.mkdir()
    for name in ("[USER] A [B4].dck", "[USER] B [B4].dck"):
        (decks / name).write_text(_DECK, encoding="utf-8")
    rows = [
        _row("[USER] A [B4].dck", "[USER] A v2 [B4].dck", 40, 4, 16),
        _row("[USER] B [B4].dck", "[USER] B v2 [B4].dck", 40, 16, 4),
    ]
    (inbox / "x_throughput.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return inbox, decks


# The default report's exact JSON key set -- the byte-identical contract's
# schema half. New substrate keys (features / cluster_analysis /
# card_score_analysis / *_multiple_testing) must NOT appear by default.
_DEFAULT_REPORT_KEYS = {
    "n_decks", "total_games", "mean_margin", "verdicts",
    "feature_correlations", "mode", "skipped", "min_games",
    "missing_deck_files", "missing_deck_rows_dropped",
}


def test_default_features_output_is_byte_identical(tmp_path, capsys):
    """No --features flag and --features deck_health emit the same bytes
    (text AND json), and the default JSON schema is pinned."""
    import json
    inbox, decks = _fixture_inbox(tmp_path)
    base = ["--inbox", str(inbox), "--decks", str(decks), "--min-games", "40"]

    outs = {}
    for tag, extra in (("default", []),
                       ("explicit", ["--features", "deck_health"])):
        assert ma.main(base + extra) == 0
        outs[tag] = capsys.readouterr().out
    assert outs["default"] == outs["explicit"]
    # None of the new-substrate sections leak into the default text.
    for banned in ("multiple-testing honesty", "corpus-theme cluster",
                   "CardScore", "features="):
        assert banned not in outs["default"]

    for tag, extra in (("default", []),
                       ("explicit", ["--features", "deck_health"])):
        assert ma.main(base + extra + ["--json"]) == 0
        outs[tag] = capsys.readouterr().out
    assert outs["default"] == outs["explicit"]
    assert set(json.loads(outs["default"]).keys()) == _DEFAULT_REPORT_KEYS


def _tribal_deck(n_goblins=14):
    lines = ["[metadata]", "Name=G", "[Commander]", "1 Gob Boss|X|1", "[Main]"]
    for i in range(n_goblins):
        lines.append(f"1 Goblin {i}|X|1")
    lines.append("30 Mountain|X|1")
    return "\n".join(lines) + "\n"


def _stub_lookup_goblins(name):
    if "Mountain" in name:
        return {"type_line": "Basic Land — Mountain", "oracle_text": "",
                "color_identity": ["R"]}
    return {"type_line": "Creature — Goblin", "oracle_text": "",
            "cmc": 2.0, "color_identity": ["R"]}


def test_assign_cluster_uses_corpus_themes_ladder():
    text = _tribal_deck()
    assert ma.assign_cluster(text, lookup=_stub_lookup_goblins) == "tribal-goblin"


def test_assign_cluster_is_stable_across_repeat_runs():
    """Same synthetic corpus, two passes -> identical labels (the ladder is
    deterministic; a flapping label would poison the indicator regressors)."""
    corpus = [_tribal_deck(12 + i) for i in range(4)]
    first = [ma.assign_cluster(t, lookup=_stub_lookup_goblins) for t in corpus]
    second = [ma.assign_cluster(t, lookup=_stub_lookup_goblins) for t in corpus]
    assert first == second == ["tribal-goblin"] * 4


def test_assign_cluster_defaults_and_degrades_honestly():
    # No dominant signal -> the ladder's honest default bucket.
    plain = ("[metadata]\nName=P\n[Commander]\n1 Cmdr|X|1\n[Main]\n"
             "1 Some Card|X|1\n30 Forest|X|1\n")

    def lookup(name):
        return {"type_line": "Sorcery", "oracle_text": "", "cmc": 3.0,
                "color_identity": ["G"]}
    assert ma.assign_cluster(plain, lookup=lookup) == "goodstuff-midrange"
    # Empty deck text -> the explicit unclassified sentinel, never a raise.
    assert ma.assign_cluster("", lookup=lookup) == ma.CLUSTER_UNCLASSIFIED


def test_card_score_features_extracts_components(monkeypatch):
    """The FP-015 seam is called directly (no env flag) with the filename
    bracket, and component values (incl. legit Nones) map to cs_* keys."""
    import commander_builder.bubble_analysis as ba
    seen = {}

    class _Stub:
        total = 61.8
        components = {
            "role_fit": {"value": 0.9, "weight": 0.5, "detail": "ok"},
            "mana_fit": {"value": 0.75, "weight": 0.5, "detail": "ok"},
            "salt_fit": {"value": None, "weight": 0.0,
                         "detail": "unavailable — skipped"},
            "reference_alignment": {"value": None, "weight": 0.0,
                                    "detail": "unavailable — skipped"},
        }

    def stub_score_deck(**kwargs):
        seen.update(kwargs)
        return _Stub()

    monkeypatch.setattr(ba, "score_deck", stub_score_deck)
    feats = ma.card_score_features(_DECK, "[USER] D [B3].dck")
    assert seen["bracket"] == 3          # parsed from [B3]
    assert seen["corpus"] is None        # never fetches a corpus in the loop
    assert feats["cs_total"] == pytest.approx(61.8)
    assert feats["cs_role_fit"] == pytest.approx(0.9)
    assert feats["cs_mana_fit"] == pytest.approx(0.75)
    assert feats["cs_salt_fit"] is None
    assert feats["cs_reference_alignment"] is None


def test_card_score_features_survive_seam_failure(monkeypatch):
    import commander_builder.bubble_analysis as ba

    def boom(**kwargs):
        raise RuntimeError("no card db")
    monkeypatch.setattr(ba, "score_deck", boom)
    feats = ma.card_score_features(_DECK, "[USER] D [B3].dck")
    assert feats == {n: None for n in ma.CARD_SCORE_FEATURES}


def test_analyze_card_score_uses_per_feature_availability():
    samples = [
        ma.Sample(deck=f"d{i}", margin=0.1 * i, games=40,
                  features={"cs_total": float(i),
                            "cs_role_fit": (float(i) if i < 3 else None),
                            "cs_reference_alignment": None})
        for i in range(6)
    ]
    rep = ma.analyze_card_score(samples)
    by_name = {f["feature"]: f for f in rep["features"]}
    assert by_name["cs_total"]["n_avail"] == 6
    assert by_name["cs_total"]["pearson_r"] == pytest.approx(1.0)
    assert by_name["cs_role_fit"]["n_avail"] == 3      # Nones excluded
    assert by_name["cs_reference_alignment"]["n_avail"] == 0
    assert by_name["cs_reference_alignment"]["pearson_r"] is None
    # honesty: only features that produced an r count as tested.
    mt = rep["multiple_testing"]
    assert mt["features_tested"] == 2                  # total + role_fit
    assert mt["expected_false_positives_p05"] == pytest.approx(0.1)


def test_analyze_clusters_lumps_small_clusters_into_other():
    def s(i, cluster, margin):
        return ma.Sample(deck=f"d{i}", margin=margin, games=40,
                         features={}, cluster=cluster)
    samples = (
        [s(i, "tribal-goblin", 0.10 + 0.001 * i) for i in range(6)]  # tested
        + [s(10 + i, "mill", -0.10) for i in range(2)]       # small: lumped
        + [s(20, "blink-flicker", -0.10)]                    # small: lumped
    )
    rep = ma.analyze_clusters(samples, min_n=5)
    assert rep["lumped_into_other"] == ["blink-flicker", "mill"]
    by_label = {c["cluster"]: c for c in rep["clusters"]}
    assert set(by_label) == {"tribal-goblin", "other"}
    assert by_label["tribal-goblin"]["n"] == 6
    assert by_label["other"]["n"] == 3
    assert by_label["other"]["mean_margin"] == pytest.approx(-0.10)
    # "other" (n=3 < min_n) is reported but NOT tested; goblins are tested
    # one-vs-rest and (near-)cleanly separate the margins.
    assert by_label["other"]["pearson_r"] is None
    assert by_label["tribal-goblin"]["pearson_r"] > 0.99
    mt = rep["multiple_testing"]
    assert mt["features_tested"] == 1
    assert mt["hits_abs_t_ge_2"] == 1


def test_analyze_clusters_none_cluster_buckets_as_unclassified():
    samples = [
        ma.Sample(deck=f"d{i}", margin=0.01 * i, games=40, features={},
                  cluster=None)
        for i in range(5)
    ]
    rep = ma.analyze_clusters(samples, min_n=5)
    (only,) = rep["clusters"]
    assert only["cluster"] == ma.CLUSTER_UNCLASSIFIED
    assert only["n"] == 5
    # A single all-decks group has a constant indicator -> no variance -> NA.
    assert only["pearson_r"] is None
    assert rep["multiple_testing"]["features_tested"] == 0


def test_features_clusters_mode_end_to_end(tmp_path, capsys, monkeypatch):
    """--features clusters: no deck_health table, cluster section + honesty
    line present, and the JSON carries the cluster_analysis block."""
    import json
    inbox, decks = _fixture_inbox(tmp_path)
    monkeypatch.setattr(ma, "assign_cluster", lambda text: "tribal-goblin")
    rc = ma.main(["--inbox", str(inbox), "--decks", str(decks),
                  "--min-games", "40", "--features", "clusters"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "features=clusters" in out
    assert "feature -> margin correlation" not in out   # deck_health skipped
    assert "corpus-theme cluster -> margin" in out
    assert "multiple-testing honesty (clusters)" in out

    rc = ma.main(["--inbox", str(inbox), "--decks", str(decks),
                  "--min-games", "40", "--features", "clusters", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["features"] == "clusters"
    assert report["feature_correlations"] == []
    assert report["cluster_analysis"]["clusters"][0]["n"] == 2


def test_features_all_reports_every_family(tmp_path, capsys, monkeypatch):
    import commander_builder.bubble_analysis as ba
    inbox, decks = _fixture_inbox(tmp_path)
    monkeypatch.setattr(ma, "assign_cluster", lambda text: "mill")

    class _Stub:
        total = 50.0
        components = {"role_fit": {"value": 0.5},
                      "mana_fit": {"value": None},
                      "salt_fit": {"value": None},
                      "reference_alignment": {"value": None}}
    monkeypatch.setattr(ba, "score_deck", lambda **kw: _Stub())
    rc = ma.main(["--inbox", str(inbox), "--decks", str(decks),
                  "--min-games", "40", "--features", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "feature -> margin correlation" in out       # deck_health family
    assert "multiple-testing honesty (deck_health)" in out
    assert "corpus-theme cluster -> margin" in out
    assert "multiple-testing honesty (clusters)" in out
    assert "CardScore deck-level component" in out
    assert "multiple-testing honesty (card_score)" in out
