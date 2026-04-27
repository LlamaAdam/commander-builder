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
