"""Tests for the forge_py correlation harness.

Verifies the offline contract: log shape, header creation, summary
math. The actual forge_py.combat call is integration-tested
opportunistically (skipped when forge_py isn't installed) — the unit
tests here stub it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commander_builder.forge_py_correlation import (
    ForgePyABResult,
    correlation_summary,
    log_correlation_row,
    pearson_r,
    run_forge_py_ab,
)


# --- pearson_r (pure helper) ----------------------------------------------

def test_pearson_perfect_positive():
    assert pearson_r([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    assert pearson_r([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_uncorrelated_is_near_zero():
    r = pearson_r([1, 2, 3, 4], [1, 2, 1, 2])
    assert r is not None and abs(r) < 0.5


def test_pearson_undefined_cases():
    assert pearson_r([1], [1]) is None          # <2 points
    assert pearson_r([1, 2], [1, 2, 3]) is None  # length mismatch
    assert pearson_r([5, 5, 5], [1, 2, 3]) is None  # zero variance in x
    assert pearson_r([1, 2, 3], [7, 7, 7]) is None  # zero variance in y


# --- correlation_summary: pearson_r wiring --------------------------------

def _write(log, **margins):
    """Helper: write one row from forge/py (old,new) win pairs."""
    log_correlation_row(
        log, old_deck="x.dck", new_deck="y.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=margins["f_o"], forge_new_wins=margins["f_n"], forge_draws=0,
        forge_duration_sec=30.0,
        py_old_wins=margins["p_o"], py_new_wins=margins["p_n"], py_draws=0,
        py_duration_sec=0.5,
    )


def test_summary_pearson_none_on_empty(tmp_path):
    s = correlation_summary(tmp_path / "ghost.csv")
    assert s["pearson_r"] is None
    assert s["pearson_n"] == 0


def test_summary_pearson_perfect_when_margins_track(tmp_path):
    log = tmp_path / "corr.csv"
    # forge margin (f_n - f_o) and py margin move together perfectly.
    for f_o, f_n, p_o, p_n in [(5, 0, 4, 1), (3, 2, 3, 2), (0, 5, 1, 4)]:
        _write(log, f_o=f_o, f_n=f_n, p_o=p_o, p_n=p_n)
    s = correlation_summary(log)
    assert s["pearson_n"] == 3
    assert s["pearson_r"] == pytest.approx(1.0, abs=0.05)


def test_summary_pearson_skips_error_rows(tmp_path):
    log = tmp_path / "corr.csv"
    _write(log, f_o=1, f_n=4, p_o=1, p_n=4)
    log_correlation_row(
        log, old_deck="x.dck", new_deck="y.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=0, forge_new_wins=0, forge_draws=0, forge_duration_sec=0.0,
        py_old_wins=0, py_new_wins=0, py_draws=0, py_duration_sec=0.0,
        py_error="boom",
    )
    _write(log, f_o=4, f_n=1, p_o=4, p_n=1)
    s = correlation_summary(log)
    # Error row excluded from the correlation series.
    assert s["pearson_n"] == 2
    assert s["errors"] == 1


def test_log_correlation_row_creates_file_with_header(tmp_path):
    log = tmp_path / "corr.csv"
    log_correlation_row(
        log,
        old_deck="A.dck", new_deck="B.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=2, forge_new_wins=3, forge_draws=0,
        forge_duration_sec=30.0,
        py_old_wins=2, py_new_wins=3, py_draws=0,
        py_duration_sec=0.4,
    )
    text = log.read_text(encoding="utf-8")
    # Header line first.
    assert text.splitlines()[0].startswith("ts,old_deck,new_deck,")
    # One data row.
    assert len(text.splitlines()) == 2
    assert "A.dck" in text
    assert "B.dck" in text


def test_log_correlation_row_appends_subsequent_writes(tmp_path):
    log = tmp_path / "corr.csv"
    for i in range(3):
        log_correlation_row(
            log,
            old_deck=f"a{i}.dck", new_deck=f"b{i}.dck",
            bracket=3, mode="1v1", games_per_pod=5,
            forge_old_wins=2, forge_new_wins=3, forge_draws=0,
            forge_duration_sec=30.0,
            py_old_wins=2, py_new_wins=3, py_draws=0,
            py_duration_sec=0.4,
        )
    lines = log.read_text(encoding="utf-8").splitlines()
    # 1 header + 3 rows.
    assert len(lines) == 4


def test_correlation_summary_reports_zero_when_no_log(tmp_path):
    summary = correlation_summary(tmp_path / "ghost.csv")
    assert summary["rows"] == 0
    assert summary["agreement_rate"] == 0.0


def test_correlation_summary_computes_agreement_rate(tmp_path):
    log = tmp_path / "corr.csv"
    # 3 agreeing rows, 1 disagreeing.
    rows = [
        # forge: new wins, py: new wins → agree
        (1, 4, 0, 1, 4, 0),
        # forge: old wins, py: old wins → agree
        (5, 1, 0, 4, 2, 0),
        # forge: tie, py: tie → agree
        (3, 3, 0, 3, 3, 0),
        # forge: old wins, py: new wins → DISAGREE
        (4, 1, 0, 1, 4, 0),
    ]
    for f_o, f_n, f_d, p_o, p_n, p_d in rows:
        log_correlation_row(
            log,
            old_deck="x.dck", new_deck="y.dck",
            bracket=3, mode="1v1", games_per_pod=5,
            forge_old_wins=f_o, forge_new_wins=f_n, forge_draws=f_d,
            forge_duration_sec=30.0,
            py_old_wins=p_o, py_new_wins=p_n, py_draws=p_d,
            py_duration_sec=0.5,
        )
    summary = correlation_summary(log)
    assert summary["rows"] == 4
    assert summary["agree"] == 3
    assert summary["disagree"] == 1
    assert summary["agreement_rate"] == 0.75


def test_correlation_summary_skips_error_rows(tmp_path):
    log = tmp_path / "corr.csv"
    log_correlation_row(
        log,
        old_deck="x.dck", new_deck="y.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=2, forge_new_wins=3, forge_draws=0,
        forge_duration_sec=30.0,
        py_old_wins=0, py_new_wins=0, py_draws=0,
        py_duration_sec=0.0,
        py_error="forge_py crashed: tag_cards failed",
    )
    summary = correlation_summary(log)
    assert summary["rows"] == 1
    assert summary["errors"] == 1
    assert summary["agree"] == 0


def test_run_forge_py_ab_returns_error_when_missing_files(tmp_path):
    """Smoke: feed the harness paths that don't exist; it should
    surface the error in the result rather than raising."""
    out = run_forge_py_ab(
        tmp_path / "nope_old.dck", tmp_path / "nope_new.dck",
        games_per_pod=2, mode="1v1",
    )
    assert isinstance(out, ForgePyABResult)
    assert out.error is not None
    assert "parse_dck" in out.error or "forge_py not importable" in out.error


def test_run_forge_py_ab_handles_missing_forge_py(monkeypatch, tmp_path):
    """When forge_py isn't importable, the harness must NOT raise —
    it must return an error result so the parent flow keeps working."""
    import commander_builder.forge_py_correlation as fp
    monkeypatch.setattr(fp, "_maybe_import_forge_py", lambda: None)
    out = fp.run_forge_py_ab(
        tmp_path / "x.dck", tmp_path / "y.dck",
        games_per_pod=2, mode="1v1",
    )
    assert out.error == "forge_py not importable"
    assert out.total_games == 0


def test_cli_main_prints_human_summary(tmp_path, capsys):
    """The CLI's default text output is grep-friendly."""
    from commander_builder.forge_py_correlation import _cli_main
    log = tmp_path / "corr.csv"
    log_correlation_row(
        log,
        old_deck="a.dck", new_deck="b.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=2, forge_new_wins=3, forge_draws=0,
        forge_duration_sec=30.0,
        py_old_wins=2, py_new_wins=3, py_draws=0,
        py_duration_sec=0.5,
    )
    rc = _cli_main(["--log", str(log)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total rows:" in out
    assert "Agreement rate:" in out


def test_cli_main_json_output(tmp_path, capsys):
    """--json emits parseable JSON."""
    import json as _json
    from commander_builder.forge_py_correlation import _cli_main
    log = tmp_path / "corr.csv"
    log_correlation_row(
        log,
        old_deck="a.dck", new_deck="b.dck",
        bracket=3, mode="1v1", games_per_pod=5,
        forge_old_wins=2, forge_new_wins=3, forge_draws=0,
        forge_duration_sec=30.0,
        py_old_wins=2, py_new_wins=3, py_draws=0,
        py_duration_sec=0.5,
    )
    rc = _cli_main(["--log", str(log), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = _json.loads(out)
    assert parsed["rows"] == 1
    assert parsed["agreement_rate"] == 1.0
