"""Tests for scripts/validate_card_score.py — offline via injected fns."""

from __future__ import annotations

import importlib.util
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
    # Staged files exist for post-mortem.
    assert (tmp_path / "stage" / "t__tier3_score.dck").exists()


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
