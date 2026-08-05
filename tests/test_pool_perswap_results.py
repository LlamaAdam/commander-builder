"""Tests for scripts/pool_perswap_results.py — synthetic fixtures only.

COMMITTED BLIND per the FP-015 two-box pre-registration: every result
file here is a hand-built synthetic fixture whose statistics are
hand-computable; no real per-swap result file was read to write these
tests (or the script under test). The fixture schema mirrors the
``--out`` serialization of scripts/validate_card_score_perswap.py
(``build_summary``'s ``rows``), learned from that writer's CODE.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_pool_spec = importlib.util.spec_from_file_location(
    "pool_perswap_results",
    REPO_ROOT / "scripts" / "pool_perswap_results.py")
pool = importlib.util.module_from_spec(_pool_spec)
_pool_spec.loader.exec_module(pool)


def swap(card, score, group, margin=None, skipped=None, source="edhrec"):
    """A swap dict shaped like rank_candidates + run_deck emit."""
    s = {"card": card, "card_score": score, "source": source,
         "role": None, "group": group}
    if margin is not None:
        s["margin"] = margin
        s["sim"] = {"old_wins": 1, "new_wins": 2, "games": 40}
    if skipped is not None:
        s["skipped"] = skipped
    return s


def write_result(tmp_path, name, rows, dry_run=None):
    """A result file shaped like the harness's --out summary.

    ``dry_run=None`` omits the key (legacy files, written before the
    2026-08-05 labeling, carry no label); True/False writes it the way
    ``build_summary`` now does.
    """
    p = tmp_path / name
    data = {
        "rows": rows,
        "decks": len(rows),
        "gate": {"overall": "single-run gate, ignored by pooling"},
    }
    if dry_run is not None:
        data["dry_run"] = dry_run
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The hand-computed pooled fixture (ties in BOTH scores and margins,
# one skipped swap, one failed deck, one skipped deck).
#
# Pooled measured swaps: scores [9, 8, 2, 7, 2],
#                        margins [0.5, 0.25, -0.25, 0.25, -0.5].
# Score ranks (mid-ranked): [5, 4, 1.5, 3, 1.5]
# Margin ranks (mid-ranked): [5, 3.5, 2, 3.5, 1]
# rho = 9 / 9.5 = +0.947368...
# Exact permutation null: 4 of 120 orderings reach rho >= observed,
# so the seeded Monte Carlo p sits near 1/30 = 0.0333 (< .05).
# Contrast: top [0.5, 0.25, 0.25] mean 1/3; bottom [-0.25, -0.5]
# mean -3/8; diff = 17/24; se = sqrt(13)/24; Welch df = 1.899.
# ---------------------------------------------------------------------------

def arm_a(tmp_path):
    return write_result(tmp_path, "arm_a.json", [
        {"deck": "A1.dck", "bracket": 3, "paired_cut": "Cut A", "swaps": [
            swap("Top A1", 9.0, "top", margin=0.5),
            swap("Top A2", 8.0, "top", margin=0.25),
            swap("Bot A1", 2.0, "bottom", margin=-0.25),
            swap("Bot Skip", 1.0, "bottom",
                 skipped="degenerate stage — staged text is identical "
                         "to the base deck"),
        ]},
        {"deck": "A2.dck", "failed": "RuntimeError: forge died"},
    ])


def arm_b(tmp_path):
    return write_result(tmp_path, "arm_b.json", [
        {"deck": "B1.dck", "bracket": 3, "paired_cut": "Cut B", "swaps": [
            swap("Top B1", 7.0, "top", margin=0.25),
            swap("Bot B1", 2.0, "bottom", margin=-0.5),
        ]},
        {"deck": "B2.dck", "candidates": 0,
         "skipped": "advisor produced no candidate adds"},
    ])


@pytest.fixture()
def pooled(tmp_path):
    arms = [pool.load_arm(arm_a(tmp_path)), pool.load_arm(arm_b(tmp_path))]
    return pool.build_pooled_summary(arms)


def test_pooled_rho_hand_computed_with_ties(pooled):
    sp = pooled["spearman"]
    assert sp["n"] == 5
    assert sp["rho"] == pytest.approx(9 / 9.5)


def test_pooled_permutation_p_near_exact_null(pooled):
    # Exact favorable fraction is 4/120 = 1/30; the seeded 10k-shuffle
    # Monte Carlo p must land close (and, gate-relevantly, below .05).
    p = pooled["spearman"]["p_value"]
    assert p == pytest.approx(1 / 30, abs=0.01)
    assert p < 0.05
    assert "10000 shuffles" in pooled["spearman"]["method"]


def test_pooled_contrast_hand_computed(pooled):
    ct = pooled["group_contrast"]
    assert (ct["top_n"], ct["bottom_n"]) == (3, 2)
    assert ct["top_mean"] == pytest.approx(1 / 3)
    assert ct["bottom_mean"] == pytest.approx(-3 / 8)
    assert ct["diff"] == pytest.approx(17 / 24)
    assert ct["df"] == pytest.approx(1.9, abs=0.005)
    # Welch interval endpoints: diff +/- t_crit(int(1.899)) * se.
    half = pool._ps._t3._t_crit(1) * math.sqrt(13) / 24
    assert ct["ci_low"] == pytest.approx(17 / 24 - half)
    assert ct["ci_high"] == pytest.approx(17 / 24 + half)


def test_per_arm_counts(pooled):
    a, b = pooled["arms"]
    assert (a["file"], a["decks"], a["measured_swaps"], a["skipped_swaps"],
            a["skipped_decks"], a["failed_decks"]) == (
        "arm_a.json", 2, 3, 1, 0, 1)
    assert (b["file"], b["decks"], b["measured_swaps"], b["skipped_swaps"],
            b["skipped_decks"], b["failed_decks"]) == (
        "arm_b.json", 2, 2, 0, 1, 0)
    assert pooled["pooled_measured_swaps"] == 5
    assert pooled["swaps_by_group"] == {"top": 3, "bottom": 2}


def test_source_file_tagging_preserves_evidence_source(pooled):
    tags = {s["card"]: s["source_file"] for s in pooled["measured"]}
    assert tags == {"Top A1": "arm_a.json", "Top A2": "arm_a.json",
                    "Bot A1": "arm_a.json", "Top B1": "arm_b.json",
                    "Bot B1": "arm_b.json"}
    # The harness's own "source" (candidate evidence source) survives.
    assert all(s["source"] == "edhrec" for s in pooled["measured"])


def test_gate_pass_verdict_verbatim(pooled):
    gate = pooled["gate"]
    assert gate["spearman"].startswith("pass (rho +0.947, p 0.0")
    assert gate["group_contrast"] == (
        "pass (top mean +0.333 > bottom mean -0.375)")
    assert gate["overall"] == "pass — CardScore is predictive per policy"
    assert pooled["gate_policy"] == pool.GATE_POLICY
    assert "pre-registered" in pooled["multiple_testing"]
    assert "exploratory" in pooled["multiple_testing"]


def test_gate_fail_verdict_verbatim(tmp_path):
    # Anti-correlated: high scores measure WORSE margins.
    f1 = write_result(tmp_path, "anti1.json", [
        {"deck": "C1.dck", "swaps": [
            swap("C hi1", 9.0, "top", margin=-0.5),
            swap("C hi2", 8.0, "top", margin=-0.25),
            swap("C lo1", 2.0, "bottom", margin=0.5),
        ]}])
    f2 = write_result(tmp_path, "anti2.json", [
        {"deck": "D1.dck", "swaps": [
            swap("D hi1", 7.0, "top", margin=-0.25),
            swap("D lo1", 1.0, "bottom", margin=0.25),
        ]}])
    summary = pool.build_pooled_summary(
        [pool.load_arm(f1), pool.load_arm(f2)])
    gate = summary["gate"]
    assert gate["spearman"].startswith("fail (rho -")
    assert gate["spearman"].endswith("needs rho > 0 and p < .05)")
    assert gate["group_contrast"].startswith("fail (top mean ")
    assert gate["overall"] == "fail — CardScore is not shown predictive"


def test_gate_not_evaluated_below_min_n(tmp_path):
    f1 = write_result(tmp_path, "tiny1.json", [
        {"deck": "T1.dck", "swaps": [swap("T hi", 9.0, "top", margin=0.5)]}])
    f2 = write_result(tmp_path, "tiny2.json", [
        {"deck": "T2.dck", "swaps": [
            swap("T lo", 1.0, "bottom", margin=-0.5)]}])
    summary = pool.build_pooled_summary(
        [pool.load_arm(f1), pool.load_arm(f2)])
    assert summary["spearman"] is None
    assert summary["gate"]["spearman"].startswith(
        "not evaluated (need >= 3 measured swaps")
    assert summary["gate"]["overall"] == (
        "not evaluated (a criterion lacks data)")


def test_per_arm_exploratory_rho(pooled):
    # arm_a has 3 measured swaps (>= MIN_SPEARMAN_N): monotone, rho +1.
    # arm_b has only 2: no per-arm rho is emitted.
    assert pooled["per_arm_spearman_exploratory"] == {
        "arm_a.json": pytest.approx(1.0)}


def test_stats_are_imported_not_reimplemented():
    perswap = str(REPO_ROOT / "scripts" / "validate_card_score_perswap.py")
    for fn in (pool._ps.spearman_test, pool._ps.spearman_rho,
               pool._ps.group_contrast, pool._ps.gate_verdict):
        assert fn.__code__.co_filename == perswap
    assert pool.GATE_POLICY is pool._ps.GATE_POLICY


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_json_and_out_two_files(tmp_path, capsys):
    out = tmp_path / "pooled.json"
    rc = pool.main([str(arm_a(tmp_path)), str(arm_b(tmp_path)),
                    "--json", "--out", str(out)])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert printed == written
    assert printed["pooled_measured_swaps"] == 5
    assert printed["gate"]["overall"] == (
        "pass — CardScore is predictive per policy")


def test_cli_human_output(tmp_path, capsys):
    rc = pool.main([str(arm_a(tmp_path)), str(arm_b(tmp_path))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "arm_a.json: 3 measured / 1 skipped swaps" in out
    assert "arm_b.json: 2 measured / 0 skipped swaps" in out
    assert "pooled n: 5 measured swaps (top 3 / bottom 2)" in out
    assert "pooled spearman: rho +0.947" in out
    assert "gate[overall]: pass — CardScore is predictive per policy" in out
    assert "gate policy: pre-registered 2026-08-01" in out
    assert "multiple testing: one pre-registered test family" in out


def test_cli_three_files_accepted(tmp_path, capsys):
    third = write_result(tmp_path, "arm_c.json", [
        {"deck": "E1.dck", "swaps": [
            swap("E hi", 6.0, "top", margin=0.25),
            swap("E lo", 3.0, "bottom", margin=-0.25),
        ]}])
    rc = pool.main([str(arm_a(tmp_path)), str(arm_b(tmp_path)),
                    str(third), "--json"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["arms"]) == 3
    assert printed["pooled_measured_swaps"] == 7


def test_cli_single_file_rejected(tmp_path, capsys):
    rc = pool.main([str(arm_a(tmp_path))])
    assert rc == 2
    assert "at least 2 result files" in capsys.readouterr().err


def test_cli_invalid_json_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = pool.main([str(arm_a(tmp_path)), str(bad)])
    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_missing_rows_clean_error(tmp_path, capsys):
    bad = tmp_path / "norows.json"
    bad.write_text(json.dumps({"gate": {}}), encoding="utf-8")
    rc = pool.main([str(arm_a(tmp_path)), str(bad)])
    assert rc == 2
    assert "no 'rows' list" in capsys.readouterr().err


def test_cli_malformed_rows_clean_error(tmp_path, capsys):
    bad = tmp_path / "badrows.json"
    bad.write_text(json.dumps({"rows": ["not a row"]}), encoding="utf-8")
    rc = pool.main([str(arm_a(tmp_path)), str(bad)])
    assert rc == 2
    assert "malformed rows" in capsys.readouterr().err


def test_cli_missing_file_clean_error(tmp_path, capsys):
    rc = pool.main([str(arm_a(tmp_path)),
                    str(tmp_path / "nope.json")])
    assert rc == 2
    assert "cannot read" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Dry-run / empty-arm guards (2026-08-05 incident: a collaborator's
# --dry-run file on a shared --out path was pooled as a completed arm).
# ---------------------------------------------------------------------------


def dry_run_rows():
    """Rows shaped like a run_deck dry run: swaps staged, none simmed."""
    return [{"deck": "DR1.dck", "bracket": 3, "paired_cut": "Cut D",
             "skipped": "dry run", "swaps": [
                 swap("DR hi", 9.0, "top", skipped="dry run"),
                 swap("DR lo", 1.0, "bottom", skipped="dry run"),
             ]}]


def test_cli_rejects_labeled_dry_run_file(tmp_path, capsys):
    bad = write_result(tmp_path, "box2b.json", dry_run_rows(),
                       dry_run=True)
    rc = pool.main([str(arm_a(tmp_path)), str(bad)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "box2b.json" in err
    assert "DRY-RUN" in err
    assert "never be pooled" in err


def test_cli_labeled_dry_run_refused_even_with_allow_empty_arm(
        tmp_path, capsys):
    bad = write_result(tmp_path, "box2b.json", dry_run_rows(),
                       dry_run=True)
    rc = pool.main([str(arm_a(tmp_path)), str(bad),
                    "--allow-empty-arm"])
    assert rc == 2
    assert "DRY-RUN" in capsys.readouterr().err


def test_cli_accepts_labeled_real_output(tmp_path, capsys):
    # dry_run: false — a labeled REAL arm pools exactly like before.
    a = write_result(tmp_path, "arm_a.json", [
        {"deck": "A1.dck", "swaps": [
            swap("Top A1", 9.0, "top", margin=0.5),
            swap("Top A2", 8.0, "top", margin=0.25),
            swap("Bot A1", 2.0, "bottom", margin=-0.25),
        ]}], dry_run=False)
    b = write_result(tmp_path, "arm_b.json", [
        {"deck": "B1.dck", "swaps": [
            swap("Top B1", 7.0, "top", margin=0.25),
            swap("Bot B1", 2.0, "bottom", margin=-0.5),
        ]}], dry_run=False)
    rc = pool.main([str(a), str(b), "--json"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["pooled_measured_swaps"] == 5


def test_cli_rejects_unlabeled_zero_measured_arm(tmp_path, capsys):
    # No dry_run key at all — a legacy dry-run and a genuinely empty
    # arm are indistinguishable; the refusal must name both and the
    # override.
    bad = write_result(tmp_path, "empty.json", dry_run_rows())
    rc = pool.main([str(arm_a(tmp_path)), str(bad)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "empty.json" in err
    assert "ZERO measured swaps" in err
    assert "DRY-RUN" in err            # possibility 1: unlabeled legacy
    assert "genuinely empty" in err    # possibility 2: real empty arm
    assert "--allow-empty-arm" in err  # the override, by name


def test_cli_allow_empty_arm_admits_with_caveat(tmp_path, capsys):
    empty = write_result(tmp_path, "empty.json", dry_run_rows())
    out = tmp_path / "pooled.json"
    rc = pool.main([str(arm_a(tmp_path)), str(empty),
                    "--allow-empty-arm", "--out", str(out)])
    assert rc == 0
    captured = capsys.readouterr()
    # Prominent caveat: on stderr, in the human output, and in the
    # written JSON record.
    assert "CAVEAT" in captured.err
    assert "empty.json" in captured.err
    caveat_lines = [ln for ln in captured.out.splitlines()
                    if ln.startswith("CAVEAT")]
    assert len(caveat_lines) == 1
    assert "remaining arm(s) alone" in caveat_lines[0]
    written = json.loads(out.read_text(encoding="utf-8"))
    assert "empty.json" in written["empty_arm_caveat"]
    # The gate really did run over arm_a's 3 measured swaps alone.
    assert written["pooled_measured_swaps"] == 3
    assert [a["measured_swaps"] for a in written["arms"]] == [3, 0]


def test_cli_allow_empty_arm_no_caveat_when_no_arm_is_empty(
        tmp_path, capsys):
    rc = pool.main([str(arm_a(tmp_path)), str(arm_b(tmp_path)),
                    "--allow-empty-arm", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "CAVEAT" not in captured.err
    assert json.loads(captured.out)["empty_arm_caveat"] is None
