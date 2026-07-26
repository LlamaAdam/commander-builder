"""Tests for scripts/validate_card_score.py — offline via injected fns."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "validate_card_score", REPO_ROOT / "scripts" / "validate_card_score.py")
vcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vcs)

from commander_builder.card_score import CARD_SCORE_ENV_VAR  # noqa: E402

DECK_TEXT = (
    "[metadata]\nName=T3\n\n[Commander]\n1 Test Cmdr\n\n[Main]\n"
    "1 Cut Me\n1 Keep Me\n" + "1 Forest\n" * 35
)


def rec(card, action):
    return SimpleNamespace(card=card, action=action)


def make_advise(flag_orders):
    """advise() stub whose rec order depends on the flag env var."""
    def advise(deck_path, bracket):
        flag = os.environ.get(CARD_SCORE_ENV_VAR, "") == "1"
        adds, cuts = flag_orders[flag]
        return SimpleNamespace(recommendations=(
            [rec(a, "add") for a in adds] + [rec(c, "cut") for c in cuts]))
    return advise


def test_build_arm_swaps_toggles_flag_and_restores_env(monkeypatch):
    monkeypatch.setenv(CARD_SCORE_ENV_VAR, "original")
    seen = []

    def advise(deck_path, bracket):
        seen.append(os.environ.get(CARD_SCORE_ENV_VAR))
        return SimpleNamespace(recommendations=[rec("A", "add")])

    vcs.build_arm_swaps(Path("x.dck"), 3, 5, flag_on=True,
                        advise_fn=advise)
    vcs.build_arm_swaps(Path("x.dck"), 3, 5, flag_on=False,
                        advise_fn=advise)
    assert seen == ["1", "0"]
    assert os.environ[CARD_SCORE_ENV_VAR] == "original"


def test_build_arm_swaps_restores_env_on_failure(monkeypatch):
    monkeypatch.delenv(CARD_SCORE_ENV_VAR, raising=False)

    def advise(deck_path, bracket):
        raise RuntimeError("boom")

    try:
        vcs.build_arm_swaps(Path("x.dck"), 3, 5, flag_on=True,
                            advise_fn=advise)
    except RuntimeError:
        pass
    assert CARD_SCORE_ENV_VAR not in os.environ


def test_build_arm_swaps_caps_at_k():
    advise = make_advise({False: (["A1", "A2", "A3"], ["C1", "C2", "C3"]),
                          True: ([], [])})
    adds, cuts = vcs.build_arm_swaps(Path("x.dck"), 3, 2, flag_on=False,
                                     advise_fn=advise)
    assert adds == ["A1", "A2"] and cuts == ["C1", "C2"]


def test_run_deck_identical_arms_skips(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["A"], ["Cut Me"]),
                          True: (["A"], ["Cut Me"])})
    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise)
    assert row["arms_identical"] is True
    assert "identical" in row["skipped"]


def test_run_deck_dry_run_stops_before_sims(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["Bucket Add"], ["Cut Me"]),
                          True: (["Score Add"], ["Cut Me"])})

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("dry run must not sim")

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage", dry_run=True,
                       advise_fn=advise, compare_fn=compare_fn)
    assert row["skipped"] == "dry run"
    assert row["bucket_arm"]["adds"] == ["Bucket Add"]
    assert row["score_arm"]["adds"] == ["Score Add"]


def test_run_deck_sims_both_arms_and_picks_winner(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["Bucket Add"], ["Cut Me"]),
                          True: (["Score Add"], ["Cut Me"])})
    simmed = []

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        simmed.append(new_deck)
        wins = 7 if "score" in new_deck else 3
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=10 - wins, games=games_per_pod),
            new_stats=SimpleNamespace(wins=wins, games=games_per_pod))

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn)
    assert len(simmed) == 2
    assert row["winner"] == "score"
    assert row["score_margin"] > row["bucket_margin"]
    # Staged decks are cleaned up (they live in the REAL deck dir) —
    # the persisted compare reports are the durable record.
    assert not list((tmp_path / "stage").glob("t__tier3_*.dck"))


def test_run_deck_noop_arm_records_none_margin(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    # Score arm's swaps reference cards not in the deck -> no-op stage.
    advise = make_advise({False: (["Bucket Add"], ["Cut Me"]),
                          True: ([], ["Not In Deck"])})

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=5, games=games_per_pod),
            new_stats=SimpleNamespace(wins=5, games=games_per_pod))

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn)
    assert row["score_margin"] is None
    assert "winner" not in row


def test_main_dry_run_end_to_end(tmp_path, capsys, monkeypatch):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        vcs, "run_deck",
        lambda *a, **k: {"deck": "t.dck", "skipped": "dry run",
                         "arms_identical": False,
                         "bucket_arm": {"adds": ["A"], "cuts": ["C"]},
                         "score_arm": {"adds": ["B"], "cuts": ["C"]}})
    rc = vcs.main([str(deck), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIPPED (dry run)" in out
    assert "skipped 1" in out


def test_main_missing_deck_exits_2(tmp_path, capsys):
    rc = vcs.main([str(tmp_path / "nope.dck")])
    assert rc == 2
    assert "no such deck" in capsys.readouterr().err


# ── bubble arm (FP-015 addendum: bubble-first swaps vs bucket order) ──


def make_verdict(adds, cuts, verdict="polish", budget=(2, 5)):
    """apply_verdict_to_report stub — returns a NEW trimmed report."""
    def verdict_fn(report, *, deck_text, corpus, bracket):
        return SimpleNamespace(
            recommendations=([rec(a, "add") for a in adds]
                             + [rec(c, "cut") for c in cuts]),
            commander_names=getattr(report, "commander_names", []),
            deck_score={"verdict": verdict, "change_budget": list(budget),
                        "score": 61.0},
            bubble_cards=[{"card": c} for c in cuts],
        )
    return verdict_fn


def write_deck(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    return deck


def test_build_bubble_arm_runs_flag_on_and_restores_env(monkeypatch,
                                                        tmp_path):
    monkeypatch.setenv(CARD_SCORE_ENV_VAR, "original")
    seen = []

    def advise(deck_path, bracket):
        seen.append(os.environ.get(CARD_SCORE_ENV_VAR))
        return SimpleNamespace(recommendations=[rec("A", "add")],
                               commander_names=["Test Cmdr"])

    vcs.build_bubble_arm_swaps(
        write_deck(tmp_path), 3, 5, advise_fn=advise,
        verdict_fn=make_verdict(["A"], []), corpus_fn=lambda *a, **k: None)
    assert seen == ["1"]
    assert os.environ[CARD_SCORE_ENV_VAR] == "original"


def test_build_bubble_arm_returns_trimmed_swaps_and_verdict(tmp_path):
    advise = make_advise({True: (["A1", "A2", "A3"], ["C1", "C2", "C3"]),
                          False: ([], [])})
    adds, cuts, info = vcs.build_bubble_arm_swaps(
        write_deck(tmp_path), 3, 5, advise_fn=advise,
        verdict_fn=make_verdict(["A2"], ["C3"], verdict="keep",
                                budget=(0, 2)),
        corpus_fn=lambda *a, **k: None)
    assert adds == ["A2"] and cuts == ["C3"]
    assert info["verdict"] == "keep"
    assert info["change_budget"] == [0, 2]
    assert info["bubble_cards"] == ["C3"]


def test_build_bubble_arm_passes_commander_to_corpus_fn(tmp_path):
    seen = {}

    def advise(deck_path, bracket):
        return SimpleNamespace(recommendations=[rec("A", "add")],
                               commander_names=["Krenko", "Partner"])

    def corpus_fn(commander, bracket):
        seen["commander"] = commander
        seen["bracket"] = bracket
        return "CORPUS"

    def verdict_fn(report, *, deck_text, corpus, bracket):
        seen["corpus"] = corpus
        return report

    vcs.build_bubble_arm_swaps(write_deck(tmp_path), 3, 5, advise_fn=advise,
                               verdict_fn=verdict_fn, corpus_fn=corpus_fn)
    assert seen["commander"] == "Krenko // Partner"
    assert seen["bracket"] == 3
    assert seen["corpus"] == "CORPUS"


def test_run_deck_bubble_arm_caps_bucket_arm_to_same_budget(tmp_path):
    """Equal budgets: only the ORDERING may differ between arms."""
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["B1", "B2", "B3"], ["Cut Me", "Keep Me"]),
                          True: (["S1", "S2", "S3"], ["Keep Me", "Cut Me"])})
    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage", dry_run=True,
                       advise_fn=advise, arms=("bucket", "bubble"),
                       verdict_fn=make_verdict(["S2"], ["Cut Me"]),
                       corpus_fn=lambda *a, **k: None)
    assert row["bubble_arm"] == {"adds": ["S2"], "cuts": ["Cut Me"]}
    assert row["bucket_arm"] == {"adds": ["B1"], "cuts": ["Cut Me"]}
    assert row["budget"] == {"adds": 1, "cuts": 1}
    assert row["bubble_verdict"]["verdict"] == "polish"
    assert "score_arm" not in row


def test_run_deck_bubble_arm_sims_and_picks_winner(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["Bucket Add"], ["Keep Me"]),
                          True: (["Bubble Add"], ["Cut Me"])})
    simmed = []

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        simmed.append(new_deck)
        wins = 8 if "bubble" in new_deck else 4
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=10 - wins, games=games_per_pod),
            new_stats=SimpleNamespace(wins=wins, games=games_per_pod))

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn,
                       arms=("bucket", "bubble"),
                       verdict_fn=make_verdict(["Bubble Add"], ["Cut Me"]),
                       corpus_fn=lambda *a, **k: None)
    assert len(simmed) == 2
    assert row["winner"] == "bubble"
    assert row["bubble_margin"] > row["bucket_margin"]
    assert not list((tmp_path / "stage").glob("t__tier3_*.dck"))


def test_run_deck_skips_when_bubble_budget_is_zero(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["B1"], ["Cut Me"]), True: (["S1"], [])})

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("empty budget must not sim")

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn,
                       arms=("bucket", "bubble"),
                       verdict_fn=make_verdict([], [], verdict="overhaul",
                                               budget=(0, 0)),
                       corpus_fn=lambda *a, **k: None)
    assert "budget" in row["skipped"]


def test_run_deck_three_arms_all_identical_skips(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["A"], ["Cut Me"]),
                          True: (["A"], ["Cut Me"])})
    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, arms=("bucket", "score", "bubble"),
                       verdict_fn=make_verdict(["A"], ["Cut Me"]),
                       corpus_fn=lambda *a, **k: None)
    assert row["arms_identical"] is True
    assert "identical" in row["skipped"]


def test_main_rejects_unknown_arm(tmp_path, capsys):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    rc = vcs.main([str(deck), "--arms", "bucket,nonsense"])
    assert rc == 2
    assert "unknown arm" in capsys.readouterr().err


def test_main_passes_arms_through(tmp_path, capsys, monkeypatch):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    seen = {}

    def fake_run_deck(*a, **k):
        seen.update(k)
        return {"deck": "t.dck", "skipped": "dry run", "arms_identical": False}

    monkeypatch.setattr(vcs, "run_deck", fake_run_deck)
    rc = vcs.main([str(deck), "--arms", "bucket,bubble", "--dry-run"])
    assert rc == 0
    assert seen["arms"] == ("bucket", "bubble")


def test_main_summary_counts_wins_per_arm(tmp_path, capsys, monkeypatch):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        vcs, "run_deck",
        lambda *a, **k: {"deck": "t.dck", "winner": "bubble",
                         "bucket_margin": 0.1, "bubble_margin": 0.3,
                         "arms_identical": False})
    rc = vcs.main([str(deck), "--arms", "bucket,bubble", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["wins_by_arm"]["bubble"] == 1
    assert summary["wins_by_arm"]["bucket"] == 0
