"""Tests for scripts/validate_card_score_perswap.py — offline, injected fns.

Follows tests/test_validate_card_score.py: the script is loaded via
importlib, every advisor / scorer / sim seam is injected, and nothing
here touches the network, the env flag, or Forge.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "validate_card_score_perswap",
    REPO_ROOT / "scripts" / "validate_card_score_perswap.py")
vps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vps)

from commander_builder.card_score import CARD_SCORE_ENV_VAR  # noqa: E402

DECK_TEXT = (
    "[metadata]\nName=PS\n\n[Commander]\n1 Test Cmdr\n\n[Main]\n"
    "1 Cut Me\n1 Keep Me\n" + "1 Forest\n" * 35
)


# Cleanup regression stem: REAL deck names carry square brackets
# ([USER], [PREMADE], [B3]), which pathlib.glob treats as character
# classes — the pre-fix stem-based cleanup glob matched nothing and
# staged decks leaked into the live Forge deck dir forever.
BRACKET_STEM = "[USER] Cleanup Deck [B3]"


def staged_leftovers(stage_dir, stem, marker="__perswap_"):
    """Leftover staged files, found WITHOUT the glob under test."""
    if not stage_dir.is_dir():
        return []
    return [n for n in os.listdir(stage_dir)
            if n.startswith(f"{stem}{marker}")]


def rec(card, action, evidence=None):
    return SimpleNamespace(card=card, action=action,
                           evidence=evidence or {})


def make_advise(adds, cuts):
    def advise(deck_path, bracket):
        return SimpleNamespace(recommendations=(
            [rec(a, "add") for a in adds]
            + [rec(c, "cut") for c in cuts]))
    return advise


def score_by(table, default=0.0):
    return lambda r: table.get(r.card, default)


def write_deck(tmp_path, name="t.dck"):
    deck = tmp_path / name
    deck.write_text(DECK_TEXT, encoding="utf-8")
    return deck


def ok_compare(margins_by_name=None):
    """compare_fn stub: wins keyed by staged deck filename substring."""
    calls = []

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        calls.append(new_deck)
        wins = 5
        for frag, w in (margins_by_name or {}).items():
            if frag in new_deck:
                wins = w
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=10 - wins, games=games_per_pod),
            new_stats=SimpleNamespace(wins=wins, games=games_per_pod))
    compare_fn.calls = calls
    return compare_fn


# ── selection correctness ──


def test_rank_candidates_sorts_desc_with_name_tiebreak():
    adds = [rec("Bcard", "add"), rec("Acard", "add"), rec("Zcard", "add")]
    ranked, unscored = vps.rank_candidates(
        adds, score_by({"Bcard": 10.0, "Acard": 10.0, "Zcard": 50.0}))
    assert [r["card"] for r in ranked] == ["Zcard", "Acard", "Bcard"]
    assert unscored == []


def test_rank_candidates_reports_unscored_instead_of_zeroing():
    def scorer(r):
        if r.card == "Bad":
            raise RuntimeError("boom")
        return 5.0
    ranked, unscored = vps.rank_candidates(
        [rec("Good", "add"), rec("Bad", "add")], scorer)
    assert [r["card"] for r in ranked] == ["Good"]
    assert unscored == ["Bad"]


def test_select_swaps_top_and_bottom_by_score():
    ranked = [{"card": f"C{i}", "card_score": float(90 - i)}
              for i in range(8)]
    swaps = vps.select_swaps(ranked, 3, 3)
    assert [(s["card"], s["group"]) for s in swaps] == [
        ("C0", "top"), ("C1", "top"), ("C2", "top"),
        ("C5", "bottom"), ("C6", "bottom"), ("C7", "bottom")]


def test_select_swaps_disjoint_when_candidates_are_scarce():
    ranked = [{"card": f"C{i}", "card_score": float(90 - i)}
              for i in range(4)]
    swaps = vps.select_swaps(ranked, 3, 3)
    # Top takes 3, bottom gets ONLY the one remaining — never a card
    # already claimed by the top group.
    assert [(s["card"], s["group"]) for s in swaps] == [
        ("C0", "top"), ("C1", "top"), ("C2", "top"), ("C3", "bottom")]


def test_run_deck_never_touches_the_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(CARD_SCORE_ENV_VAR, raising=False)
    seen = []

    def advise(deck_path, bracket):
        seen.append(CARD_SCORE_ENV_VAR in os.environ)
        return SimpleNamespace(recommendations=[
            rec("Add A", "add"), rec("Cut Me", "cut")])

    vps.run_deck(write_deck(tmp_path), 3, 1, 1, 10, tmp_path / "s",
                 dry_run=True, advise_fn=advise,
                 score_fn=score_by({"Add A": 50.0}))
    assert seen == [False]
    assert CARD_SCORE_ENV_VAR not in os.environ


# ── staging + sims ──


def test_run_deck_sims_each_selected_swap_against_base(tmp_path):
    # Bracketed stem on purpose: cleanup must survive [USER]/[B3]-style
    # names, and the assertion must NOT reuse the glob under test.
    deck = write_deck(tmp_path, f"{BRACKET_STEM}.dck")
    advise = make_advise(["A1", "A2", "A3", "A4"], ["Cut Me", "Keep Me"])
    compare_fn = ok_compare({"_top": 7, "_bottom": 3})
    row = vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                       advise_fn=advise,
                       score_fn=score_by({"A1": 90.0, "A2": 50.0,
                                          "A3": 40.0, "A4": 10.0}),
                       compare_fn=compare_fn)
    assert len(compare_fn.calls) == 2
    assert row["paired_cut"] == "Cut Me"
    top, bottom = row["swaps"]
    assert (top["card"], top["group"], top["margin"]) == ("A1", "top", 0.4)
    assert (bottom["card"], bottom["group"]) == ("A4", "bottom")
    assert bottom["margin"] == -0.4
    # Full ranked list is recorded, not just the selected swaps.
    assert [r["card"] for r in row["ranked"]] == ["A1", "A2", "A3", "A4"]
    # The staged decks really were written to the stage dir…
    assert any(BRACKET_STEM in name for name in compare_fn.calls)
    # …and cleaned up (they live in the REAL deck dir).
    assert staged_leftovers(tmp_path / "stage", BRACKET_STEM) == []


def test_run_deck_staged_names_and_texts_are_legal(tmp_path):
    """Single-swap staging: exactly one add landed, one cut gone, and
    Name= restamped to the staged filename stem (attribution invariant)."""
    deck = write_deck(tmp_path)
    advise = make_advise(["Add A", "Add B"], ["Cut Me"])
    seen = {}

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        base = (deck_dir / old_deck).read_text(encoding="utf-8")
        staged = (deck_dir / new_deck).read_text(encoding="utf-8")
        seen[new_deck] = (base, staged)
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=5, games=games_per_pod),
            new_stats=SimpleNamespace(wins=5, games=games_per_pod))

    vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                 advise_fn=advise,
                 score_fn=score_by({"Add A": 90.0, "Add B": 10.0}),
                 compare_fn=compare_fn)
    assert len(seen) == 2
    for new_deck, (base, staged) in seen.items():
        assert f"Name={new_deck[:-4]}" in staged
        assert "Name=t__perswap_base" in base
        assert "1 Cut Me" in base and "1 Cut Me" not in staged
        added = "Add A" if "_top" in new_deck else "Add B"
        assert f"1 {added}" in staged
        # Single swap only: the other candidate never leaks in.
        other = "Add B" if added == "Add A" else "Add A"
        assert other not in staged


def test_run_deck_cleans_staged_decks_when_a_sim_crashes(tmp_path):
    """Crash path with a REAL-shaped bracketed stem: the staged decks
    (base + both swap arms) must still be removed, and the assertion
    must not reuse the broken stem-glob that hid the leak."""
    deck = write_deck(tmp_path, f"{BRACKET_STEM}.dck")
    advise = make_advise(["A1", "A2"], ["Cut Me"])
    calls = []

    def compare_fn(old_deck, new_deck, bracket, games_per_pod, deck_dir):
        calls.append(new_deck)
        # The staged files exist mid-sim — the later emptiness is
        # cleanup, not a failure to stage.
        assert (deck_dir / old_deck).is_file()
        assert (deck_dir / new_deck).is_file()
        if len(calls) == 2:
            raise RuntimeError("forge died mid-pod")
        return SimpleNamespace(
            old_stats=SimpleNamespace(wins=4, games=games_per_pod),
            new_stats=SimpleNamespace(wins=6, games=games_per_pod))

    with pytest.raises(RuntimeError, match="forge died"):
        vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                     advise_fn=advise,
                     score_fn=score_by({"A1": 90.0, "A2": 10.0}),
                     compare_fn=compare_fn)
    assert len(calls) == 2
    assert staged_leftovers(tmp_path / "stage", BRACKET_STEM) == []


def test_run_deck_degenerate_stage_is_skipped_not_simmed(tmp_path):
    """A candidate already in the deck drops its (cut, add) pair in the
    legality path, staging a text identical to the base — that swap must
    be skipped, not simmed as pure noise."""
    deck = write_deck(tmp_path)
    advise = make_advise(["Keep Me", "Fresh Add"], ["Cut Me"])
    compare_fn = ok_compare()
    row = vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                       advise_fn=advise,
                       score_fn=score_by({"Keep Me": 90.0,
                                          "Fresh Add": 10.0}),
                       compare_fn=compare_fn)
    top, bottom = row["swaps"]
    assert "degenerate" in top["skipped"]
    assert "margin" not in top
    assert bottom["margin"] is not None
    assert len(compare_fn.calls) == 1


def test_run_deck_skips_when_no_cut_matches_the_decklist(tmp_path):
    deck = write_deck(tmp_path)
    advise = make_advise(["A1"], ["Ghost Card"])

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("no matchable cut must not sim")

    row = vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                       advise_fn=advise, score_fn=score_by({"A1": 5.0}),
                       compare_fn=compare_fn)
    assert row["paired_cut"] is None
    assert "no cut matching" in row["skipped"]


def test_run_deck_paired_cut_skips_unmatchable_top_cut(tmp_path):
    deck = write_deck(tmp_path)
    advise = make_advise(["A1"], ["Ghost Card", "Cut Me"])
    compare_fn = ok_compare()
    row = vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage",
                       advise_fn=advise, score_fn=score_by({"A1": 5.0}),
                       compare_fn=compare_fn)
    assert row["paired_cut"] == "Cut Me"
    assert row["swaps"][0]["margin"] is not None


def test_run_deck_no_candidates_skips(tmp_path):
    row = vps.run_deck(write_deck(tmp_path), 3, 3, 3, 10, tmp_path / "s",
                       advise_fn=make_advise([], ["Cut Me"]),
                       score_fn=score_by({}))
    assert "no candidate adds" in row["skipped"]


# ── dry run ──


def test_run_deck_dry_run_shape_and_no_sims(tmp_path):
    deck = write_deck(tmp_path)
    advise = make_advise(["A1", "A2", "Keep Me"], ["Cut Me"])

    def compare_fn(**kw):  # pragma: no cover - must not be called
        raise AssertionError("dry run must not sim")

    row = vps.run_deck(deck, 3, 1, 1, 10, tmp_path / "stage", dry_run=True,
                       advise_fn=advise,
                       score_fn=score_by({"A1": 90.0, "A2": 50.0,
                                          "Keep Me": 10.0}),
                       compare_fn=compare_fn)
    assert row["skipped"] == "dry run"
    assert [r["card"] for r in row["ranked"]] == ["A1", "A2", "Keep Me"]
    assert row["paired_cut"] == "Cut Me"
    top, bottom = row["swaps"]
    assert (top["card"], top["group"]) == ("A1", "top")
    assert top["skipped"] == "dry run"
    # The degenerate stage keeps its REAL reason even in a dry run.
    assert (bottom["card"], bottom["group"]) == ("Keep Me", "bottom")
    assert "degenerate" in bottom["skipped"]
    # No staged text blob leaks into the (JSON-serialized) row.
    assert "_proposed" not in top and "_proposed" not in bottom
    json.dumps(row)


def test_build_summary_dry_run_label():
    """The summary is self-identifying: dry_run true iff --dry-run.

    (2026-08-05 incident: a dry-run --out file on a shared path was
    pooled as if it were a completed arm — the label is what lets
    pool_perswap_results.py refuse it.)
    """
    assert vps.build_summary([], [])["dry_run"] is False
    assert vps.build_summary([], [], dry_run=False)["dry_run"] is False
    assert vps.build_summary([], [], dry_run=True)["dry_run"] is True


def test_main_out_labels_dry_run_true_and_real_false(tmp_path, capsys,
                                                     monkeypatch):
    deck = write_deck(tmp_path)
    monkeypatch.setattr(
        vps, "run_deck",
        lambda p, *a, dry_run=False, **k: (
            {"deck": p.name, "skipped": "dry run", "swaps": []}
            if dry_run else
            {"deck": p.name, "swaps": [
                swap("T1", 90.0, 0.2, "top", deck=p.name)]}))

    out_dry = tmp_path / "dry.json"
    assert vps.main([str(deck), "--dry-run", "--out", str(out_dry)]) == 0
    dry = json.loads(out_dry.read_text(encoding="utf-8"))
    assert dry["dry_run"] is True
    # The human summary line calls the dry run out loudly.
    printed = capsys.readouterr().out
    assert "DRY RUN" in printed
    assert "must not be pooled" in printed

    out_real = tmp_path / "real.json"
    assert vps.main([str(deck), "--out", str(out_real)]) == 0
    real = json.loads(out_real.read_text(encoding="utf-8"))
    assert real["dry_run"] is False
    assert "DRY RUN" not in capsys.readouterr().out


# ── Spearman math on hand-checked fixtures ──


def test_average_ranks_mid_ranks_ties():
    assert vps.average_ranks([10, 20, 20, 40]) == [1.0, 2.5, 2.5, 4.0]
    assert vps.average_ranks([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_spearman_rho_hand_checked():
    assert vps.spearman_rho([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert vps.spearman_rho([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    # Ranks (1,2,3,4) vs (1,3,2,4): Pearson over ranks = 4/5.
    assert abs(vps.spearman_rho([1, 2, 3, 4], [5, 30, 20, 40]) - 0.8) < 1e-12


def test_spearman_rho_with_ties_hand_checked():
    # rx = [1, 2.5, 2.5, 4], ry = [1, 2, 3, 4]:
    # cov 4.5, vx 4.5, vy 5 -> rho = 4.5 / sqrt(22.5) = 0.9486832...
    rho = vps.spearman_rho([10, 20, 20, 30], [1, 2, 3, 4])
    assert abs(rho - 4.5 / (22.5 ** 0.5)) < 1e-12


def test_spearman_rho_undefined_cases():
    assert vps.spearman_rho([1], [2]) is None
    assert vps.spearman_rho([1, 1, 1], [1, 2, 3]) is None  # constant side


def test_spearman_test_is_seeded_and_detects_monotone_data():
    xs = list(range(10))
    ys = [x * 2.0 for x in xs]
    a = vps.spearman_test(xs, ys, permutations=2000)
    b = vps.spearman_test(xs, ys, permutations=2000)
    assert a == b  # deterministic under the fixed seed
    assert a["rho"] == 1.0 and a["p_value"] < 0.05
    # Anti-correlated data must NOT pass a one-sided (greater) test.
    c = vps.spearman_test(xs, list(reversed(ys)), permutations=2000)
    assert c["rho"] == -1.0 and c["p_value"] > 0.5


def test_group_contrast_welch_hand_checked():
    # top [0.2, 0.4], bottom [0.0, 0.0, 0.0]: v1=0.02, v2=0 ->
    # se = sqrt(0.01), df = (0.01)^2 / ((0.01)^2/1) = 1 -> t 12.706.
    ct = vps.group_contrast([0.2, 0.4], [0.0, 0.0, 0.0])
    assert abs(ct["diff"] - 0.3) < 1e-12
    assert abs(ct["ci_high"] - (0.3 + 12.706 * 0.1)) < 1e-9
    assert ct["df"] == 1.0


def test_group_contrast_needs_both_groups_and_degrades_honestly():
    assert vps.group_contrast([], [0.1]) is None
    ct = vps.group_contrast([0.5], [0.1, 0.2])
    assert ct["diff"] == pytest.approx(0.35)
    assert ct["ci_low"] is None  # interval needs >= 2 per group


# ── summary + gate ──


def swap(card, score, margin, group, deck="d.dck"):
    return {"card": card, "card_score": score, "margin": margin,
            "group": group, "deck": deck}


def deck_row(deck, swaps):
    return {"deck": deck, "swaps": swaps}


def test_build_summary_gate_passes_on_predictive_data():
    swaps = ([swap(f"T{i}", 90.0 - i, 0.30 - 0.01 * i, "top")
              for i in range(4)]
             + [swap(f"B{i}", 20.0 - i, -0.20 - 0.01 * i, "bottom")
                for i in range(4)])
    s = vps.build_summary([deck_row("d.dck", swaps)], [])
    assert s["measured_swaps"] == 8
    assert s["spearman"]["rho"] > 0 and s["spearman"]["p_value"] < 0.05
    assert s["gate"]["spearman"].startswith("pass")
    assert s["gate"]["group_contrast"].startswith("pass")
    assert s["gate"]["overall"].startswith("pass")
    assert "pre-registered" in s["gate_policy"]
    assert "NOT multiplicity-corrected" in s["multiple_testing"]


def test_build_summary_gate_fails_on_anti_predictive_data():
    swaps = ([swap(f"T{i}", 90.0 - i, -0.30 + 0.01 * i, "top")
              for i in range(4)]
             + [swap(f"B{i}", 20.0 - i, 0.20 + 0.01 * i, "bottom")
                for i in range(4)])
    s = vps.build_summary([deck_row("d.dck", swaps)], [])
    assert s["spearman"]["rho"] < 0
    assert s["gate"]["spearman"].startswith("fail")
    assert s["gate"]["group_contrast"].startswith("fail")
    assert s["gate"]["overall"].startswith("fail")


def test_build_summary_not_evaluated_with_too_few_swaps():
    s = vps.build_summary(
        [deck_row("d.dck", [swap("A", 90.0, 0.2, "top")])], [])
    assert s["gate"]["spearman"].startswith("not evaluated")
    assert s["gate"]["group_contrast"].startswith("not evaluated")
    assert "not evaluated" in s["gate"]["overall"]


def test_build_summary_positive_rho_alone_does_not_pass():
    """Conjunction gate: rho can be positive while the top group still
    loses to the bottom group on means — that must NOT read as pass."""
    swaps = [swap("T1", 90.0, 0.05, "top"), swap("T2", 80.0, 0.01, "top"),
             swap("B1", 20.0, 0.00, "bottom"),
             swap("B2", 10.0, 0.30, "bottom")]
    s = vps.build_summary([deck_row("d.dck", swaps)], [])
    assert s["group_contrast"]["diff"] < 0
    assert s["gate"]["group_contrast"].startswith("fail")
    assert not s["gate"]["overall"].startswith("pass")


def test_build_summary_noise_floor_is_context_not_criterion():
    swaps = ([swap(f"T{i}", 90.0 - i, 0.30 - 0.01 * i, "top")
              for i in range(4)]
             + [swap(f"B{i}", 20.0 - i, -0.20 - 0.01 * i, "bottom")
                for i in range(4)])
    nulls = [{"deck": "n", "null": True, "margin": 0.9},
             {"deck": "n2", "null": True, "margin": -0.8}]
    s = vps.build_summary([deck_row("d.dck", swaps)], nulls)
    # A huge noise reference is published but does NOT flip the gate.
    assert s["noise_floor"]["mean_abs_margin"] == pytest.approx(0.85)
    assert s["noise_floor"]["sufficient"] is True
    assert s["gate"]["overall"].startswith("pass")
    assert "not one of them" in s["noise_floor"]["note"]


def test_build_summary_records_per_deck_rho_as_exploratory():
    rows = [
        deck_row("a.dck", [swap(f"A{i}", 90.0 - i, 0.1 - 0.01 * i,
                                "top", deck="a.dck") for i in range(3)]),
        deck_row("b.dck", [swap(f"B{i}", 90.0 - i, -0.1 + 0.01 * i,
                                "bottom", deck="b.dck")
                           for i in range(3)]),
    ]
    s = vps.build_summary(rows, [])
    assert s["per_deck_spearman_exploratory"]["a.dck"] == 1.0
    assert s["per_deck_spearman_exploratory"]["b.dck"] == -1.0


# ── partial results / containment ──


def test_main_contains_per_deck_failures_and_still_summarizes(
        tmp_path, capsys, monkeypatch):
    d1 = write_deck(tmp_path, "a.dck")
    d2 = write_deck(tmp_path, "b.dck")

    def fake_run_deck(p, *a, **k):
        if p.name == "a.dck":
            raise RuntimeError("forge timeout")
        return deck_row(p.name, [
            swap("T1", 90.0, 0.2, "top", deck=p.name),
            swap("B1", 10.0, -0.2, "bottom", deck=p.name)])

    monkeypatch.setattr(vps, "run_deck", fake_run_deck)
    monkeypatch.setattr(
        vps._t3, "run_null_replicate",
        lambda p, *a, **k: {"deck": p.name, "null": True, "margin": 0.05})
    out = tmp_path / "summary.json"
    rc = vps.main([str(d1), str(d2), "--null-replicates", "2",
                   "--out", str(out)])
    assert rc == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["failed"] == 1
    assert summary["failed_decks"] == [
        {"deck": "a.dck", "error": "RuntimeError: forge timeout"}]
    assert "excluded from every statistic" in summary["failure_note"]
    # The surviving deck and BOTH null replicates still ran.
    assert [r["deck"] for r in summary["rows"]] == ["a.dck", "b.dck"]
    assert summary["measured_swaps"] == 2
    assert summary["noise_floor"]["n"] == 2
    printed = capsys.readouterr().out
    assert "a.dck: FAILED (RuntimeError: forge timeout)" in printed
    assert "gate[overall]:" in printed


def test_main_contains_null_replicate_failures(tmp_path, capsys,
                                               monkeypatch):
    deck = write_deck(tmp_path, "a.dck")
    monkeypatch.setattr(
        vps, "run_deck",
        lambda p, *a, **k: deck_row(p.name, [
            swap("T1", 90.0, 0.2, "top", deck=p.name)]))

    def boom(*a, **k):
        raise RuntimeError("null crashed")

    monkeypatch.setattr(vps._t3, "run_null_replicate", boom)
    rc = vps.main([str(deck), "--null-replicates", "1", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["failed_null_replicates"] == [
        {"deck": "a.dck", "error": "RuntimeError: null crashed"}]
    assert summary["noise_floor"] is None


# ── CLI plumbing ──


def test_main_dry_run_end_to_end(tmp_path, capsys, monkeypatch):
    deck = write_deck(tmp_path)
    seen = {}

    def fake_run_deck(p, bracket, top_k, bottom_k, games, stage_dir,
                      dry_run=False, **k):
        seen.update(bracket=bracket, top_k=top_k, bottom_k=bottom_k,
                    games=games, dry_run=dry_run)
        return {"deck": p.name, "skipped": "dry run", "paired_cut": "C",
                "swaps": [{"card": "A", "group": "top",
                           "card_score": 42.0, "skipped": "dry run"}]}

    monkeypatch.setattr(vps, "run_deck", fake_run_deck)
    rc = vps.main([str(deck), "--dry-run", "--top-k", "2",
                   "--bottom-k", "4", "--games", "60", "--bracket", "2"])
    assert rc == 0
    assert seen == {"bracket": 2, "top_k": 2, "bottom_k": 4,
                    "games": 60, "dry_run": True}
    out = capsys.readouterr().out
    assert "SKIPPED (dry run)" in out
    assert "gate policy: pre-registered" in out


def test_main_missing_deck_exits_2(tmp_path, capsys):
    rc = vps.main([str(tmp_path / "nope.dck")])
    assert rc == 2
    assert "no such deck" in capsys.readouterr().err


def test_main_rejects_zero_k(tmp_path, capsys):
    deck = write_deck(tmp_path)
    rc = vps.main([str(deck), "--top-k", "0"])
    assert rc == 2
    assert "must each be >= 1" in capsys.readouterr().err
