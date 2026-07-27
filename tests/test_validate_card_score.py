"""Tests for scripts/validate_card_score.py — offline via injected fns."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_run_deck_reordered_arms_are_identical_and_skip(tmp_path):
    """The 2026-07-25 pilot's Hash row: same cards, different cut order.

    Ordered-list comparison called this a real row and simmed two
    card-for-card identical decks against the original, producing a
    +0.130 / -0.217 split from noise alone. Multiset comparison skips
    it.
    """
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["A1", "A2"], ["Cut Me", "Keep Me"]),
                          True: (["A2", "A1"], ["Keep Me", "Cut Me"])})

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("identical decklists must not be simmed")

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn)
    assert row["arms_identical"] is True
    assert "identical" in row["skipped"]


def test_run_deck_repeated_staged_card_is_a_real_difference(tmp_path):
    """Quantities count: staging two extra Forests differs from one."""
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise(
        {False: (["Forest", "Forest"], ["Cut Me", "Keep Me"]),
         True: (["Forest"], ["Cut Me"])})
    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage", dry_run=True,
                       advise_fn=advise)
    assert row["arms_identical"] is False


def test_run_deck_balance_trimmed_surplus_is_not_a_difference(tmp_path):
    """Requested multisets differ ({Forest x2} vs {Forest}) but the
    surplus add is dropped for balance, so both arms STAGE the same
    deck — requested-multiset dedupe simmed this as a real row."""
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["Forest", "Forest"], ["Cut Me"]),
                          True: (["Forest"], ["Cut Me"])})

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("identical decklists must not be simmed")

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn)
    assert row["arms_identical"] is True
    assert "identical" in row["skipped"]


def test_run_deck_same_multiset_different_staged_decks_runs(tmp_path):
    """Pair-drop order-dependence, the false-skip direction.

    ``_apply_swaps_to_dck`` validates positional (cut[i], add[i]) pairs
    and drops the whole pair when the cut matches nothing. With cuts
    [Cut Me, Ghost] the dropped Ghost pair takes A2 with it (stages
    A1); with cuts [Ghost, Cut Me] it takes A1 (stages A2). Identical
    swap MULTISETS, different staged decks — requested-multiset dedupe
    called these "arms identical" and skipped a genuine difference.
    """
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["A1", "A2"], ["Cut Me", "Ghost"]),
                          True: (["A1", "A2"], ["Ghost", "Cut Me"])})
    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage", dry_run=True,
                       advise_fn=advise)
    assert row["arms_identical"] is False
    assert row["skipped"] == "dry run"


def test_run_deck_different_multisets_identical_staged_decks_skip(tmp_path):
    """Pair-drop order-dependence, the false-run direction: the arm
    requesting an extra (Ghost, A2) pair loses it to validation and
    stages the same deck as the arm that never asked — simming the two
    would feed pure noise into the paired CI."""
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["A1"], ["Cut Me"]),
                          True: (["A1", "A2"], ["Cut Me", "Ghost"])})

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("identical decklists must not be simmed")

    row = vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                       advise_fn=advise, compare_fn=compare_fn)
    assert row["arms_identical"] is True
    assert "identical" in row["skipped"]


def test_staged_signature_ignores_name_line_and_printing_tails():
    base = "[metadata]\nName={n}\n\n[Main]\n1 Forest{tail}\n2 Island\n"
    same = vcs._staged_signature(base.format(n="A", tail="|ZEN|249"))
    assert vcs._staged_signature(base.format(n="B", tail="")) == same
    # Quantities are part of the identity: 1 Forest != 2 Forest.
    bumped = vcs._staged_signature(
        "[metadata]\nName=A\n\n[Main]\n2 Forest\n2 Island\n")
    assert bumped != same


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


# ── (b) null replicates + (d) CI-based gating ──


def test_paired_ci_math_hand_checked():
    # diffs [1, 2, 3]: mean 2, sd 1, se 1/sqrt(3), t(2)=4.303
    ci = vcs.paired_ci([1.0, 2.0, 3.0])
    assert ci["n"] == 3 and ci["mean"] == 2.0
    assert abs(ci["ci_high"] - (2.0 + 4.303 / (3 ** 0.5))) < 1e-9
    assert ci["excludes_zero"] is False  # interval spans zero? low<0
    tight = vcs.paired_ci([0.10, 0.12, 0.11, 0.09, 0.10, 0.11])
    assert tight["excludes_zero"] is True and tight["mean"] > 0


def test_paired_ci_needs_two_points():
    assert vcs.paired_ci([]) is None
    assert vcs.paired_ci([0.5]) is None


def row(deck, bucket=None, bubble=None, winner=None):
    r = {"deck": deck, "bucket_margin": bucket, "bubble_margin": bubble}
    if winner:
        r["winner"] = winner
    return r


ARMS2 = ("bucket", "bubble")


def test_gate_insufficient_n():
    rows = [row(f"d{i}", bucket=0.0, bubble=0.2) for i in range(3)]
    s = vcs.build_summary(rows, ARMS2, [])
    assert s["gate"]["bubble"].startswith("insufficient-n")


def test_gate_fail_when_ci_spans_zero():
    rows = [row(f"d{i}", bucket=0.0, bubble=m)
            for i, m in enumerate([0.3, -0.3, 0.2, -0.2, 0.1, -0.1])]
    s = vcs.build_summary(rows, ARMS2, [])
    assert s["gate"]["bubble"].startswith("fail (95% CI")


def test_gate_requires_measured_noise_floor():
    rows = [row(f"d{i}", bucket=0.0, bubble=0.2 + i * 0.001)
            for i in range(6)]
    s = vcs.build_summary(rows, ARMS2, [])
    assert s["gate"]["bubble"].startswith("insufficient-null-floor")


def test_gate_fail_below_noise_floor_and_pass_above():
    rows = [row(f"d{i}", bucket=0.0, bubble=0.2 + i * 0.001)
            for i in range(6)]
    noisy = [{"deck": "n", "null": True, "margin": 0.5},
             {"deck": "n2", "null": True, "margin": -0.45}]
    s = vcs.build_summary(rows, ARMS2, noisy)
    assert s["gate"]["bubble"].startswith("fail (advantage below")
    quiet = [{"deck": "n", "null": True, "margin": 0.05},
             {"deck": "n2", "null": True, "margin": -0.03}]
    s2 = vcs.build_summary(rows, ARMS2, quiet)
    assert s2["gate"]["bubble"] == "pass"
    assert s2["noise_floor"]["n"] == 2
    assert s2["noise_floor"]["sufficient"] is True
    assert abs(s2["noise_floor"]["mean_abs_margin"] - 0.04) < 1e-9


def test_gate_floor_not_evaluated_from_a_single_replicate():
    """n=1 is one draw from the noise distribution, not a floor — the
    gate must say the criterion could not be evaluated, not pass/fail
    on it (the pilot's accidental-null lesson, deliberately this time).
    """
    rows = [row(f"d{i}", bucket=0.0, bubble=0.2 + i * 0.001)
            for i in range(6)]
    one = [{"deck": "n", "null": True, "margin": 0.05}]
    s = vcs.build_summary(rows, ARMS2, one)
    assert s["noise_floor"]["n"] == 1
    assert s["noise_floor"]["sufficient"] is False
    verdict = s["gate"]["bubble"]
    assert verdict.startswith("insufficient-null-floor")
    assert "NOT evaluated" in verdict
    assert not verdict.startswith("fail")
    assert verdict != "pass"


def test_run_null_replicate_stages_two_copies_and_cleans_up(tmp_path):
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    seen = {}

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        seen["names"] = (old_deck, new_deck)
        a = (deck_dir / old_deck).read_text(encoding="utf-8")
        b = (deck_dir / new_deck).read_text(encoding="utf-8")
        seen["names_differ_in_meta_only"] = (
            a.replace(old_deck[:-4], "X") == b.replace(new_deck[:-4], "X"))
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=4, games=games_per_pod),
            new_stats=SimpleNamespace(wins=6, games=games_per_pod))

    out = vcs.run_null_replicate(deck, 3, 10, tmp_path / "stage",
                                 compare_fn=compare_fn)
    assert out["null"] is True
    assert abs(out["margin"] - 0.2) < 1e-9
    assert "nullA" in seen["names"][0] and "nullB" in seen["names"][1]
    assert seen["names_differ_in_meta_only"]
    assert not list((tmp_path / "stage").glob("*__tier3_null*.dck"))


# ── failure containment: one crashed deck must not vaporize the run ──


def test_run_deck_cleans_staged_decks_when_a_sim_crashes(tmp_path):
    """The staged decks live in the REAL Forge deck dir (the web UI
    lists it) — a mid-arm crash must not leave them behind."""
    deck = tmp_path / "t.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    advise = make_advise({False: (["Bucket Add"], ["Cut Me"]),
                          True: (["Score Add"], ["Cut Me"])})
    calls = []

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        calls.append(new_deck)
        if len(calls) == 2:
            raise RuntimeError("forge died mid-pod")
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=4, games=games_per_pod),
            new_stats=SimpleNamespace(wins=6, games=games_per_pod))

    with pytest.raises(RuntimeError, match="forge died"):
        vcs.run_deck(deck, 3, 5, 10, tmp_path / "stage",
                     advise_fn=advise, compare_fn=compare_fn)
    assert len(calls) == 2
    assert not list((tmp_path / "stage").glob("t__tier3_*.dck"))


def test_main_contains_per_deck_failures_and_still_summarizes(
        tmp_path, capsys, monkeypatch):
    """A crash on deck 1 of 2: deck 2 and the null replicates still
    run, --out is still written, and the failure is recorded honestly
    (excluded from the paired CI with a note, not silently)."""
    d1 = tmp_path / "a.dck"
    d2 = tmp_path / "b.dck"
    d1.write_text(DECK_TEXT, encoding="utf-8")
    d2.write_text(DECK_TEXT, encoding="utf-8")

    def fake_run_deck(p, *a, **k):
        if p.name == "a.dck":
            raise RuntimeError("forge timeout")
        return {"deck": p.name, "winner": "score", "bucket_margin": 0.0,
                "score_margin": 0.2, "arms_identical": False}

    monkeypatch.setattr(vcs, "run_deck", fake_run_deck)
    monkeypatch.setattr(
        vcs, "run_null_replicate",
        lambda p, *a, **k: {"deck": p.name, "null": True, "margin": 0.05})
    out = tmp_path / "summary.json"
    rc = vcs.main([str(d1), str(d2), "--null-replicates", "2",
                   "--out", str(out)])
    assert rc == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["failed"] == 1
    assert summary["failed_decks"] == [
        {"deck": "a.dck", "error": "RuntimeError: forge timeout"}]
    assert "excluded from the paired CI" in summary["failure_note"]
    # The surviving deck and BOTH null replicates still ran.
    assert [r["deck"] for r in summary["rows"]] == ["a.dck", "b.dck"]
    assert summary["wins_by_arm"]["score"] == 1
    assert summary["noise_floor"]["n"] == 2
    printed = capsys.readouterr().out
    assert "a.dck: FAILED (RuntimeError: forge timeout)" in printed
    assert "failed 1" in printed


def test_main_contains_null_replicate_failures(tmp_path, capsys,
                                               monkeypatch):
    deck = tmp_path / "a.dck"
    deck.write_text(DECK_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        vcs, "run_deck",
        lambda p, *a, **k: {"deck": p.name, "winner": "score",
                            "bucket_margin": 0.0, "score_margin": 0.2,
                            "arms_identical": False})

    def boom(*a, **k):
        raise RuntimeError("null crashed")

    monkeypatch.setattr(vcs, "run_null_replicate", boom)
    rc = vcs.main([str(deck), "--null-replicates", "1", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["failed_null_replicates"] == [
        {"deck": "a.dck", "error": "RuntimeError: null crashed"}]
    # No successful replicate -> no floor, and the row carries no margin.
    assert summary["noise_floor"] is None
