"""ml_dataset feature extraction + split tests."""
from collections import Counter

from commander_builder.knowledge_log import Iteration
from commander_builder.ml_dataset import (
    FEATURE_NAMES,
    FeatureRow,
    build_dataset,
    dataset_summary,
    extract_features,
    split_train_eval,
)


def _it(id, deck_id, verdict="kept", **overrides) -> Iteration:
    sim_report = overrides.pop("sim_report", {
        "total_games": 10,
        "draws": 0,
        "old_stats": {"wins": 4, "avg_ending_life": 18.0},
        "new_stats": {"wins": 6, "avg_ending_life": 22.0},
    })
    manifest = overrides.pop("audit_manifest", {"added": ["A"], "removed": ["B"]})
    return Iteration(
        id=id,
        deck_id=deck_id,
        deck_name=f"deck-{deck_id}",
        bracket=overrides.get("bracket", 3),
        audit_version="v3",
        audit_manifest=manifest,
        sim_report=sim_report,
        verdict=verdict,
    )


def test_extract_features_returns_row_with_all_columns():
    row = extract_features(_it(1, "abc"))
    assert row is not None
    assert row.iteration_id == 1
    assert row.deck_id == "abc"
    assert row.label == "kept"
    # Every declared feature is in the dict.
    for name in FEATURE_NAMES:
        assert name in row.features


def test_extract_features_computes_derived_metrics():
    row = extract_features(_it(1, "abc", sim_report={
        "total_games": 10, "draws": 2,
        "old_stats": {"wins": 3}, "new_stats": {"wins": 5},
    }))
    # Decisive = 8, win rate old = 3/8, new = 5/8, delta = 2/8.
    assert row.features["decisive_games"] == 8.0
    assert row.features["draw_rate"] == 0.2
    assert row.features["win_rate_old"] == 0.375
    assert row.features["win_rate_new"] == 0.625
    assert row.features["win_rate_delta"] == 0.25
    assert row.features["margin"] == 2.0


def test_extract_features_fallback_win_rates_exclude_filler_wins():
    """When the authoritative win_rate columns are NULL, the fallback
    derivation must follow the 2026-07-20 knowledge_log convention:
    wins / head-to-head decisive (old + new wins), NOT total - draws.
    Filler-heavy compare report: 20 attributed games, old 3 / new 5 /
    2 draws, fillers took the other 10 — rates are 3/8 and 5/8, not
    3/18 and 5/18. The decisive_games FEATURE keeps its attributed
    non-draw meaning (18) — only the win-rate denominator is
    head-to-head."""
    row = extract_features(_it(1, "abc", sim_report={
        "total_games": 20, "draws": 2,
        "old_stats": {"wins": 3}, "new_stats": {"wins": 5},
    }))
    assert row.features["win_rate_old"] == 3 / 8
    assert row.features["win_rate_new"] == 5 / 8
    assert row.features["decisive_games"] == 18.0


def test_extract_features_reads_real_ab_sim_schema():
    """Regression guard: forge_runner.ABResult.to_dict() emits wins_a/wins_b/
    games -- NOT old_stats/new_stats/total_games. extract_features must read
    the real schema, else every win/margin feature is silently zeroed (the
    bug fixed 2026-05-20)."""
    row = extract_features(_it(1, "abc", verdict="reverted", sim_report={
        "wins_a": 2, "wins_b": 0, "games": 2,
        "deck_a": "x", "deck_b": "y", "status": "done",
    }))
    assert row.features["old_wins"] == 2.0
    assert row.features["new_wins"] == 0.0
    assert row.features["total_games"] == 2.0
    assert row.features["margin"] == -2.0
    assert row.features["win_rate_old"] == 1.0
    assert row.features["win_rate_new"] == 0.0


def test_extract_features_prefers_authoritative_win_rate_columns():
    """When the iteration row carries computed win_rate_old/new columns, use
    them (the analyst persisted them next to the verdict)."""
    it = _it(1, "abc", verdict="kept", sim_report={"wins_a": 1, "wins_b": 3, "games": 4})
    it.win_rate_old, it.win_rate_new = 0.25, 0.75
    row = extract_features(it)
    assert row.features["win_rate_old"] == 0.25
    assert row.features["win_rate_new"] == 0.75


def test_extract_features_includes_deck_composition_features():
    """Pre-sim deck-health features come from deck_snapshot. dh_basic_lands is
    a pure-regex count (robust offline); the rest are present (default 0 if the
    card DB is unavailable)."""
    deck = ("[metadata]\nName=T\n[Commander]\n1 Cmdr\n[Main]\n"
            "5 Forest|J25|1\n5 Island|J25|2\n1 Sol Ring|C20|1\n")
    it = _it(1, "abc")
    it.deck_snapshot = deck
    row = extract_features(it)
    for f in ("dh_spell_density", "dh_mana_sinks", "dh_wincon_protection",
              "dh_self_mill", "dh_mdfc", "dh_basic_lands"):
        assert f in row.features
    assert row.features["dh_basic_lands"] == 10.0  # 5 Forest + 5 Island


def test_extract_features_tolerates_explicit_null_stats():
    """ComparisonReport legitimately persists None for undefined stats (e.g.
    avg_turns_when_won when a version never won a game). The key EXISTS with
    value null, so ``.get(key, 0)`` returns None — and ``float(None)`` used to
    raise TypeError and kill build_dataset for the whole iteration list.
    Nulls must degrade to the default (0.0), not crash."""
    it = _it(1, "abc", sim_report={
        "total_games": 4, "draws": 0,
        # old won all 4 games → its turns_when_lost is null; new never won
        # → its turns_when_won / ending_life are null.
        "old_stats": {"wins": 4, "avg_ending_life": 21.0,
                      "avg_turns_when_lost": None},
        "new_stats": {"wins": 0, "avg_ending_life": None,
                      "avg_turns_when_won": None},
    })
    row = extract_features(it)
    assert row is not None
    assert row.features["old_avg_turns_when_lost"] == 0.0
    assert row.features["new_avg_turns_when_won"] == 0.0
    assert row.features["new_avg_ending_life"] == 0.0
    # Non-null values still pass through untouched.
    assert row.features["old_avg_ending_life"] == 21.0


def test_build_dataset_survives_a_null_stat_row():
    """One iteration with null stats must not discard the whole dataset —
    the exact blast radius of the pre-fix TypeError."""
    its = [
        _it(1, "abc", sim_report={
            "total_games": 2, "draws": 0,
            "old_stats": {"wins": 2, "avg_turns_when_won": None},
            "new_stats": {"wins": 0, "avg_turns_when_won": None},
        }),
        _it(2, "def"),
    ]
    rows = build_dataset(its)
    assert len(rows) == 2


def test_extract_features_skips_pending_verdict():
    assert extract_features(_it(1, "abc", verdict="pending")) is None


def test_extract_features_skips_iteration_without_sim_report():
    it = _it(1, "abc")
    it.sim_report = None
    assert extract_features(it) is None


def test_extract_features_skips_iteration_without_id():
    it = _it(None, "abc")
    assert extract_features(it) is None


def test_extract_features_handles_zero_decisive_games():
    """All draws → win rates default to 0 instead of dividing by zero."""
    row = extract_features(_it(1, "abc", sim_report={
        "total_games": 5, "draws": 5,
        "old_stats": {"wins": 0}, "new_stats": {"wins": 0},
    }))
    assert row.features["win_rate_old"] == 0.0
    assert row.features["win_rate_new"] == 0.0


def test_feature_vector_orders_columns_consistently():
    row = extract_features(_it(1, "abc"))
    vec = row.feature_vector()
    assert len(vec) == len(FEATURE_NAMES)
    # Same row, called twice, should give the same vector.
    assert vec == row.feature_vector()


def test_build_dataset_skips_pending():
    its = [
        _it(1, "abc", verdict="kept"),
        _it(2, "abc", verdict="pending"),
        _it(3, "def", verdict="reverted"),
    ]
    rows = build_dataset(its)
    assert len(rows) == 2
    assert {r.label for r in rows} == {"kept", "reverted"}


def test_build_dataset_optionally_skips_neutral():
    its = [
        _it(1, "abc", verdict="kept"),
        _it(2, "abc", verdict="neutral"),
        _it(3, "def", verdict="reverted"),
    ]
    rows_all = build_dataset(its, skip_neutral=False)
    rows_filtered = build_dataset(its, skip_neutral=True)
    assert len(rows_all) == 3
    assert len(rows_filtered) == 2
    assert "neutral" not in {r.label for r in rows_filtered}


# --- split_train_eval ------------------------------------------------------

def test_split_train_eval_keeps_decks_intact():
    """All iterations of the same deck must stay on the same side. Otherwise
    the model peeks at deck identity through training data."""
    its = []
    for deck_idx in range(10):
        for it_idx in range(3):
            its.append(_it(deck_idx * 3 + it_idx, f"deck-{deck_idx}"))
    rows = build_dataset(its)
    train, eval_ = split_train_eval(rows, eval_fraction=0.3, seed=42)

    train_decks = {r.deck_id for r in train}
    eval_decks = {r.deck_id for r in eval_}
    assert train_decks.isdisjoint(eval_decks)


def test_split_is_deterministic_for_same_seed():
    its = [_it(i, f"deck-{i % 5}") for i in range(20)]
    rows = build_dataset(its)
    a = split_train_eval(rows, seed=7)
    b = split_train_eval(rows, seed=7)
    assert [r.iteration_id for r in a[0]] == [r.iteration_id for r in b[0]]


# --- dataset_summary -------------------------------------------------------

def test_dataset_summary_reports_label_distribution():
    its = [
        _it(1, "a", verdict="kept"),
        _it(2, "a", verdict="kept"),
        _it(3, "b", verdict="reverted"),
        _it(4, "c", verdict="neutral"),
    ]
    rows = build_dataset(its)
    s = dataset_summary(rows)
    assert s["total_rows"] == 4
    assert s["unique_decks"] == 3
    assert s["label_distribution"]["kept"] == 2
    assert s["label_distribution"]["reverted"] == 1
    assert s["label_distribution"]["neutral"] == 1
    assert s["feature_count"] == len(FEATURE_NAMES)


def test_dataset_summary_handles_empty_input():
    s = dataset_summary([])
    assert s["total_rows"] == 0
    assert s["rows_per_deck_min"] == 0
