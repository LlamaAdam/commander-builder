"""Tests for the forge_py screening gate (``forge_py_screen.py``).

SCREEN, NOT JUDGE: every test drives the injectable ``runner`` seam
with a fake — no test imports forge_py, spawns a subprocess, or touches
Forge. The fake mirrors ``forge_py_correlation.ForgePyABResult``, the
one sanctioned shape for forge_py results in this codebase.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commander_builder import forge_py_screen
from commander_builder.forge_py_correlation import ForgePyABResult
from commander_builder.forge_py_screen import (
    DEFAULT_KEEP_FRACTION,
    DEFAULT_SCREEN_GAMES,
    MIN_KEPT,
    SCREEN_ENV_FLAG,
    ScreenReport,
    screen_arms_for_search,
    screen_candidates,
    screening_enabled,
)


# --- helpers ----------------------------------------------------------------

def _result(old=0, new=0, error=None, total=None) -> ForgePyABResult:
    total = total if total is not None else old + new
    return ForgePyABResult(old_wins=old, new_wins=new, draws=0,
                           total_games=total, duration_sec=0.01,
                           error=error)


def _fake_runner(by_stem: dict, default=None):
    """Scripted runner keyed on the candidate path's stem. Records
    every call so tests can assert what was (not) simmed."""
    calls = []

    def runner(base_path, cand_path, games, seed):
        calls.append((Path(cand_path).stem, games, seed))
        res = by_stem.get(Path(cand_path).stem, default)
        if res is None:
            raise AssertionError(f"no scripted result for {cand_path}")
        return res

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _candidates(tmp_path, keys):
    """(key, path) pairs with real (empty) files so paths exist; the
    fake runner never reads them."""
    out = []
    for k in keys:
        p = tmp_path / f"{k}.dck"
        p.write_text("stub", encoding="utf-8")
        out.append((k, p))
    return out


# --- scoring math -----------------------------------------------------------

def test_score_is_new_deck_decisive_share(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C"])
    runner = _fake_runner({
        "A": _result(old=6, new=14),   # 0.7
        "B": _result(old=10, new=10),  # 0.5
        "C": _result(old=14, new=6),   # 0.3
    })
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=1.0, runner=runner)
    scores = {s.key: s.score for s in rep.scores}
    assert scores["A"] == pytest.approx(0.7)
    assert scores["B"] == pytest.approx(0.5)
    assert scores["C"] == pytest.approx(0.3)
    # keep_fraction=1.0 -> nothing pruned, but it WAS screened.
    assert rep.screened is True
    assert rep.pruned == []
    assert rep.kept_keys == ["A", "B", "C"]


def test_runner_receives_base_games_seed(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C"])
    runner = _fake_runner({}, default=_result(old=5, new=5))
    screen_candidates(tmp_path / "base.dck", cands, games=7, seed=3,
                      runner=runner)
    assert runner.calls == [("A", 7, 3), ("B", 7, 3), ("C", 7, 3)]


# --- pruning: fraction + min-kept floor -------------------------------------

def test_prunes_bottom_fraction_keeps_top(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C", "D", "E", "F"])
    runner = _fake_runner({
        "A": _result(1, 9), "B": _result(2, 8), "C": _result(3, 7),
        "D": _result(4, 6), "E": _result(5, 5), "F": _result(9, 1),
    })
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=0.5, runner=runner)
    assert rep.screened is True
    # ceil(6 * 0.5) = 3 kept, in ORIGINAL candidate order.
    assert rep.kept_keys == ["A", "B", "C"]
    assert [s.key for s in rep.pruned] == ["D", "E", "F"]
    # Pruned entries carry their scores — that is what gets logged.
    assert [s.score for s in rep.pruned] == pytest.approx([0.6, 0.5, 0.1])


def test_min_kept_floor_overrides_aggressive_fraction(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C", "D"])
    runner = _fake_runner({
        "A": _result(1, 9), "B": _result(2, 8),
        "C": _result(3, 7), "D": _result(4, 6),
    })
    # ceil(4 * 0.1) = 1, but the floor is MIN_KEPT = 2.
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=0.1, runner=runner)
    assert rep.kept_keys == ["A", "B"]
    assert len(rep.kept_keys) >= MIN_KEPT


def test_pool_at_or_below_min_kept_skips_sims_entirely(tmp_path, capsys):
    cands = _candidates(tmp_path, ["A", "B"])
    runner = _fake_runner({}, default=_result(1, 9))
    rep = screen_candidates(tmp_path / "base.dck", cands, runner=runner)
    assert rep.screened is False
    assert rep.kept_keys == ["A", "B"]
    assert runner.calls == []  # no screen time spent on an unprunable pool
    assert "skipped" in capsys.readouterr().err


def test_deterministic_tie_break_on_key(tmp_path):
    cands = _candidates(tmp_path, ["B", "A", "C"])
    runner = _fake_runner({}, default=_result(5, 5))  # all tied at 0.5
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=2 / 3, runner=runner)
    # ceil(3 * 2/3) = 2 kept; tie broken by key ascending -> A, B kept.
    assert sorted(rep.kept_keys) == ["A", "B"]
    assert [s.key for s in rep.pruned] == ["C"]


# --- unmeasurable candidates are always kept --------------------------------

def test_zero_decisive_candidate_is_kept_unmeasured(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C", "D"])
    runner = _fake_runner({
        "A": _result(1, 9), "B": _result(0, 0, total=20),  # zero decisive
        "C": _result(3, 7), "D": _result(9, 1),
    })
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=0.5, runner=runner)
    # B is unmeasured -> kept no matter its would-be rank; measured
    # keep = ceil(3 * 0.5) = 2 -> A, C. D (worst measured) pruned.
    assert rep.kept_keys == ["A", "B", "C"]
    assert [s.key for s in rep.pruned] == ["D"]
    b = next(s for s in rep.scores if s.key == "B")
    assert b.score is None and "decisive" in b.error


def test_unstageable_candidate_none_path_is_kept(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C"])
    cands.append(("Dud", None))  # caller could not stage this swap
    runner = _fake_runner({
        "A": _result(1, 9), "B": _result(2, 8), "C": _result(9, 1),
    })
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=0.5, runner=runner)
    assert "Dud" in rep.kept_keys
    dud = next(s for s in rep.scores if s.key == "Dud")
    assert dud.score is None and "stageable" in dud.error


def test_runner_exception_marks_unmeasured_not_fatal(tmp_path):
    cands = _candidates(tmp_path, ["A", "B", "C"])
    boom_calls = []

    def runner(base, cand, games, seed):
        stem = Path(cand).stem
        boom_calls.append(stem)
        if stem == "B":
            raise RuntimeError("forge_py exploded mid-sim")
        return _result(1, 9) if stem == "A" else _result(9, 1)

    rep = screen_candidates(tmp_path / "base.dck", cands,
                            keep_fraction=0.5, runner=runner)
    assert boom_calls == ["A", "B", "C"]  # the screen kept going
    assert "B" in rep.kept_keys  # unmeasured -> kept
    b = next(s for s in rep.scores if s.key == "B")
    assert "exploded" in b.error


# --- loud degrade when forge_py is missing/broken ---------------------------

def test_missing_forge_py_degrades_loudly_to_no_screening(tmp_path, capsys):
    """Every candidate erroring (the run_forge_py_ab 'forge_py not
    importable' shape) must stand the screen down: all kept, screened
    False, one loud stderr note — never an exception, never a block."""
    cands = _candidates(tmp_path, ["A", "B", "C"])
    runner = _fake_runner({}, default=_result(error="forge_py not importable"))
    rep = screen_candidates(tmp_path / "base.dck", cands, runner=runner)
    assert rep.screened is False
    assert rep.kept_keys == ["A", "B", "C"]
    assert rep.pruned == []
    err = capsys.readouterr().err
    assert "unavailable" in err
    assert "forge_py not importable" in err


# --- no silent drops: pruned arms logged with scores ------------------------

def test_pruned_candidates_logged_with_scores(tmp_path, capsys):
    cands = _candidates(tmp_path, ["A", "B", "C", "D"])
    runner = _fake_runner({
        "A": _result(1, 9), "B": _result(2, 8),
        "C": _result(7, 3), "D": _result(9, 1),
    })
    screen_candidates(tmp_path / "base.dck", cands,
                      keep_fraction=0.5, runner=runner)
    err = capsys.readouterr().err
    assert "PRUNED 'C' score=0.300" in err
    assert "PRUNED 'D' score=0.100" in err
    # Kept arms show their scores too, and the contract is restated.
    assert "kept   'A' score=0.900" in err
    assert "not a judge" in err


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("frac", [0.0, -0.5, 1.5])
def test_keep_fraction_out_of_range_rejected(tmp_path, frac):
    with pytest.raises(ValueError):
        screen_candidates(tmp_path / "base.dck",
                          _candidates(tmp_path, ["A", "B", "C"]),
                          keep_fraction=frac, runner=_fake_runner({}))


def test_games_below_one_rejected(tmp_path):
    with pytest.raises(ValueError):
        screen_candidates(tmp_path / "base.dck",
                          _candidates(tmp_path, ["A", "B", "C"]),
                          games=0, runner=_fake_runner({}))


# --- default runner: the one sanctioned forge_py invocation path ------------

def test_default_runner_delegates_to_correlation_harness(tmp_path, monkeypatch):
    """runner=None must route through forge_py_correlation.run_forge_py_ab
    (lazy import, parse_dck staging, ForgePyABResult shape) — the SAME
    path the FP-001 correlation harness measured r~0.898 on."""
    seen = []

    def fake_ab(old_path, new_path, games_per_pod, mode="1v1", seed_base=0):
        seen.append((Path(old_path).name, Path(new_path).name,
                     games_per_pod, mode, seed_base))
        return _result(1, 9)

    monkeypatch.setattr(forge_py_screen, "run_forge_py_ab", fake_ab)
    cands = _candidates(tmp_path, ["A", "B", "C"])
    rep = screen_candidates(tmp_path / "base.dck", cands,
                            games=5, seed=2, runner=None)
    assert rep.screened is True
    assert seen == [("base.dck", "A.dck", 5, "1v1", 2),
                    ("base.dck", "B.dck", 5, "1v1", 2),
                    ("base.dck", "C.dck", 5, "1v1", 2)]


# --- screen_arms_for_search: the FP-012 bandit hook -------------------------

def _make_dck(tmp_path, name, main_cards):
    body = ("[metadata]\nName=Test\n"
            "[Commander]\n1 Test Commander\n[Main]\n")
    body += "\n".join(f"1 {c}" for c in main_cards) + "\n"
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_screen_arms_stages_through_apply_path_and_prunes(tmp_path):
    """Arms are staged as real probe decks through the shared
    apply_proposal_to_deck legality path, scored by the injected
    runner on the STAGED deck's contents, and pruned bottom-first.
    Original arm objects and their advisor-ranked order are preserved."""
    import argparse
    from commander_builder.improve_search import SearchArm

    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck",
                     ["Sol Ring", "OldCard A", "OldCard B", "OldCard C",
                      "Forest", "Forest", "Forest"])
    arms = [
        SearchArm(key="+Good / -OldCard A", add="Good", cut="OldCard A"),
        SearchArm(key="+Meh / -OldCard B", add="Meh", cut="OldCard B"),
        SearchArm(key="+Bad / -OldCard C", add="Bad", cut="OldCard C"),
    ]

    def runner(base_path, cand_path, games, seed):
        text = Path(cand_path).read_text(encoding="utf-8")
        if "1 Good" in text:
            return _result(1, 9)
        if "1 Meh" in text:
            return _result(4, 6)
        return _result(9, 1)

    args = argparse.Namespace(screen_keep=0.5, screen_games=10)
    kept = screen_arms_for_search(deck, arms, args, runner=runner)
    # ceil(3 * 0.5) = 2 measured kept -> Good + Meh, original order,
    # SAME objects (the bandit's stats live on them).
    assert kept == [arms[0], arms[1]]
    assert kept[0] is arms[0]


def test_screen_arms_unstageable_arm_is_kept(tmp_path):
    """A swap the legality path drops entirely (add already in deck)
    cannot be measured -> kept; the bandit's own evaluate path deals
    with it at pull time, exactly as without screening."""
    import argparse
    from commander_builder.improve_search import SearchArm

    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck",
                     ["Sol Ring", "OldCard A", "OldCard B", "OldCard C",
                      "Forest", "Forest", "Forest"])
    arms = [
        SearchArm(key="+Good / -OldCard A", add="Good", cut="OldCard A"),
        SearchArm(key="+Sol Ring / -OldCard B", add="Sol Ring",
                  cut="OldCard B"),  # singleton violation -> unstageable
        SearchArm(key="+Bad / -OldCard C", add="Bad", cut="OldCard C"),
    ]

    def runner(base_path, cand_path, games, seed):
        text = Path(cand_path).read_text(encoding="utf-8")
        return _result(1, 9) if "1 Good" in text else _result(9, 1)

    args = argparse.Namespace(screen_keep=0.5, screen_games=10)
    kept = screen_arms_for_search(deck, arms, args, runner=runner)
    assert arms[1] in kept  # unmeasured -> kept
    assert arms[0] in kept  # best measured
    assert arms[2] not in kept  # worst measured -> pruned


def test_screen_arms_never_returns_empty_pool(tmp_path):
    import argparse
    from commander_builder.improve_search import SearchArm

    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck",
                     ["Sol Ring", "OldCard A", "Forest", "Forest"])
    arms = [SearchArm(key="+G / -OldCard A", add="G", cut="OldCard A")]
    args = argparse.Namespace(screen_keep=0.5, screen_games=10)
    kept = screen_arms_for_search(deck, arms, args,
                                  runner=_fake_runner({}, _result(9, 1)))
    # 1-arm pool is at/below the min-kept floor: kept untouched.
    assert kept == arms


# --- enablement helper ------------------------------------------------------

def test_screening_enabled_flag_env_and_default(monkeypatch):
    import argparse
    monkeypatch.delenv(SCREEN_ENV_FLAG, raising=False)
    assert screening_enabled(argparse.Namespace()) is False
    assert screening_enabled(argparse.Namespace(screen=False)) is False
    assert screening_enabled(argparse.Namespace(screen=True)) is True
    monkeypatch.setenv(SCREEN_ENV_FLAG, "1")
    assert screening_enabled(argparse.Namespace(screen=False)) is True
    monkeypatch.setenv(SCREEN_ENV_FLAG, "0")
    assert screening_enabled(argparse.Namespace(screen=False)) is False


def test_env_flag_literal_matches_improve_search_copy():
    """improve_search deliberately duplicates the env-flag literal so
    its disabled path never imports this module; the two copies must
    never drift."""
    from commander_builder import improve_search
    assert improve_search._SCREEN_ENV_FLAG == SCREEN_ENV_FLAG
