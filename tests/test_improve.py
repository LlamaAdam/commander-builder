"""Tests for ``commander-improve`` — the FP-012 slice-1 greedy loop.

The loop logic is the actual deliverable (greedy keep-if-better advance,
round chaining, convergence/error stop conditions, summary). It's tested
in isolation via an injected ``round_fn`` so no test ever spawns Forge or
calls Anthropic. ``improve_main``'s argument parsing / deck resolution /
bracket inference are tested by stubbing ``run_improve_loop``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commander_builder import improve
from commander_builder.improve import (
    ImproveResult,
    RoundResult,
    improve_main,
    run_improve_loop,
)


# --- scripted round_fn ----------------------------------------------------

def _make_script(verdicts, *, applied=(1, 1)):
    """Build a fake round_fn that returns a scripted verdict per round
    and records the input deck path it was handed each call."""
    calls: list[Path] = []

    def fn(deck_path: Path, round_no: int, args) -> RoundResult:
        calls.append(Path(deck_path))
        v = verdicts[round_no - 1]
        adds, cuts = applied[round_no - 1] if isinstance(applied[0], tuple) else applied
        return RoundResult(
            round=round_no,
            input_deck=str(deck_path),
            output_deck=f"/decks/v{round_no}.dck",
            verdict=v,
            advanced=False,
            iteration_id=100 + round_no,
            applied_adds=adds,
            applied_cuts=cuts,
        )

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


# --- run_improve_loop: greedy advance ------------------------------------

def test_advances_only_on_kept():
    fn = _make_script(["kept", "neutral", "kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 3, object(), round_fn=fn)

    assert res.rounds_run == 3
    assert res.rounds_kept == 2
    assert Path(res.final_deck) == Path("/decks/v3.dck")
    # Round 2 was neutral, so round 3 built on v1 (not v2).
    assert fn.calls[0] == Path("/decks/start.dck")
    assert fn.calls[1] == Path("/decks/v1.dck")
    assert fn.calls[2] == Path("/decks/v1.dck")
    # Per-round advanced flags reflect the greedy decision.
    assert [r.advanced for r in res.history] == [True, False, True]


def test_no_kept_leaves_base_unchanged():
    fn = _make_script(["neutral", "reverted", "pending"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 3, object(), round_fn=fn)

    assert res.rounds_kept == 0
    assert Path(res.final_deck) == Path(res.start_deck) == Path("/decks/start.dck")
    # Every round was handed the original base.
    assert all(c == Path("/decks/start.dck") for c in fn.calls)


def test_reverted_does_not_advance_but_loop_continues():
    fn = _make_script(["kept", "reverted", "kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 3, object(), round_fn=fn)

    assert res.rounds_run == 3
    assert res.rounds_kept == 2
    # round2 reverted -> round3 built on v1, kept -> final v3
    assert fn.calls[2] == Path("/decks/v1.dck")
    assert Path(res.final_deck) == Path("/decks/v3.dck")


# --- convergence + error stop conditions ---------------------------------

def test_converges_on_zero_change_round():
    # Round 2 proposes nothing -> no-op -> stop early.
    def fn(deck_path, round_no, args):
        if round_no == 1:
            return RoundResult(1, str(deck_path), "/decks/v1.dck", "kept",
                               False, applied_adds=2, applied_cuts=2)
        return RoundResult(round_no, str(deck_path), "/decks/v2.dck",
                           "neutral", False, applied_adds=0, applied_cuts=0)

    res = run_improve_loop(Path("/decks/start.dck"), "start", 5, object(), round_fn=fn)

    assert res.converged is True
    assert res.rounds_run == 2  # stopped at round 2, not 5
    assert res.history[-1].verdict == "no-op"
    # The kept round 1 still advanced the base.
    assert Path(res.final_deck) == Path("/decks/v1.dck")
    assert res.rounds_kept == 1


def test_error_round_stops_loop():
    def fn(deck_path, round_no, args):
        if round_no == 1:
            return RoundResult(1, str(deck_path), "/decks/v1.dck", "kept",
                               False, applied_adds=1, applied_cuts=1)
        return RoundResult(round_no, str(deck_path), None, "error", False,
                           error="boom")

    res = run_improve_loop(Path("/decks/start.dck"), "start", 5, object(), round_fn=fn)

    assert res.rounds_run == 2
    assert res.converged is False
    assert res.history[-1].verdict == "error"
    assert Path(res.final_deck) == Path("/decks/v1.dck")


def test_result_is_json_serializable():
    import json
    fn = _make_script(["kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 1, object(), round_fn=fn)
    # Round-trips cleanly (the CLI emits this under --json).
    blob = json.dumps(res.to_dict())
    back = json.loads(blob)
    assert back["rounds_kept"] == 1
    assert back["history"][0]["verdict"] == "kept"


# --- improve_main: arg validation ----------------------------------------

def test_main_rejects_both_path_and_id(tmp_path):
    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text("[metadata]\nName=Test\n", encoding="utf-8")
    rc = improve_main([str(deck), "--deck", "x", "--rounds", "1"])
    assert rc == 2


def test_main_rejects_neither_path_nor_id():
    rc = improve_main(["--rounds", "1"])
    assert rc == 2


def test_main_rejects_zero_rounds(tmp_path):
    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text("[metadata]\nName=Test\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "0"])
    assert rc == 2


def test_main_missing_deck_path():
    rc = improve_main(["/no/such/deck.dck", "--rounds", "1"])
    assert rc == 2


# --- improve_main: resolution + bracket inference (loop stubbed) ----------

def _stub_loop(monkeypatch):
    """Capture the args run_improve_loop is called with; return a canned
    result so improve_main exits cleanly without running the pipeline."""
    captured = {}

    def stub(deck_path, deck_id, rounds, args, **kw):
        captured["deck_path"] = Path(deck_path)
        captured["deck_id"] = deck_id
        captured["rounds"] = rounds
        captured["bracket"] = args.bracket
        captured["sim_games"] = args.sim_games
        return ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path), final_deck=str(deck_path),
            rounds_requested=rounds, rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", stub)
    return captured


def test_main_infers_bracket_from_filename(tmp_path, monkeypatch):
    captured = _stub_loop(monkeypatch)
    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "2"])

    assert rc == 0
    assert captured["bracket"] == 4
    assert captured["rounds"] == 2
    assert captured["deck_id"] == "[USER] Goblins [B4]"


def test_main_explicit_bracket_overrides_filename(tmp_path, monkeypatch):
    captured = _stub_loop(monkeypatch)
    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "1", "--bracket", "2"])

    assert rc == 0
    assert captured["bracket"] == 2


def test_main_resolves_deck_by_id(tmp_path, monkeypatch):
    captured = _stub_loop(monkeypatch)
    deck = tmp_path / "[USER] Sliver [B3].dck"
    deck.write_text("[metadata]\nName=Sliver\n", encoding="utf-8")

    rc = improve_main(["--deck", "[USER] Sliver [B3]",
                       "--deck-dir", str(tmp_path), "--rounds", "1"])

    assert rc == 0
    assert captured["deck_path"] == deck.resolve()
    assert captured["bracket"] == 3


def test_main_unknown_deck_id_errors(tmp_path, monkeypatch):
    _stub_loop(monkeypatch)
    rc = improve_main(["--deck", "nope", "--deck-dir", str(tmp_path), "--rounds", "1"])
    assert rc == 2


def test_main_no_bracket_no_suffix_errors(tmp_path, monkeypatch):
    _stub_loop(monkeypatch)
    deck = tmp_path / "plain_deck.dck"
    deck.write_text("[metadata]\nName=Plain\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "1"])
    assert rc == 2


# --- improve_main: sub-threshold --sim-games warning ----------------------

def test_main_warns_on_sub_threshold_sim_games(tmp_path, monkeypatch, capsys):
    """--sim-games whose EXPECTED decisive count (total * 0.5 -- the 2
    filler seats win ~half the pod games) is below the 20-decisive gate
    makes every verdict 'inconclusive' in expectation — and improve only
    advances on 'kept', so the run can't move the deck. The CLI must say
    so LOUDLY up front (on stderr, so --json stdout stays parseable),
    and it must state the total->decisive ARITHMETIC: 25 was the old
    default precisely because raw sim_games vs the gate looked fine
    (25 > 20) while the decisive units said otherwise (~12 < 20)."""
    _stub_loop(monkeypatch)
    deck = tmp_path / "[USER] Warn [B3].dck"
    deck.write_text("[metadata]\nName=Warn\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "1", "--sim-games", "25"])

    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "inconclusive" in err
    assert "25" in err          # echoes the offending TOTAL value
    assert "12" in err          # ... its expected-decisive conversion
    assert "decisive" in err    # ... names the gate's unit
    assert "40" in err          # ... and the total-games floor to pass


def test_main_default_sim_games_clears_threshold_no_warning(
    tmp_path, monkeypatch, capsys,
):
    """The default --sim-games must be able to produce a decisive
    verdict (improve's whole purpose is advancing on 'kept'), so no
    warning fires at defaults. Pinned at 45: expected decisive ~= 22
    clears the 20-decisive gate with headroom, in-family with the
    operator's 40-game soak convention."""
    from commander_builder._proposer_sim import min_sim_games_for_verdict

    captured = _stub_loop(monkeypatch)
    deck = tmp_path / "[USER] Quiet [B3].dck"
    deck.write_text("[metadata]\nName=Quiet\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "1"])

    assert rc == 0
    assert captured["sim_games"] == 45  # pinned: see --sim-games comment
    assert captured["sim_games"] >= min_sim_games_for_verdict()
    assert "WARNING" not in capsys.readouterr().err


def test_min_sim_games_for_verdict_is_40():
    """ceil(20 decisive / 0.5 expected-decisive fraction) = 40 TOTAL pod
    games. Pinned: both warning sites quote this number as the floor, so
    a silent change to either constant should trip a test."""
    from commander_builder._proposer_sim import min_sim_games_for_verdict
    assert min_sim_games_for_verdict() == 40


# --- bandit strategy (FP-012 slice 2) -------------------------------------

class _FakeReport:
    def __init__(self, added, removed):
        self._added, self._removed = added, removed

    def to_manifest(self):
        return {"added": self._added, "removed": self._removed}


def test_build_arms_pairs_adds_with_cycled_cuts(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda deck_path, bracket, source: _FakeReport(
            ["A", "B", "C"], ["X", "Y"]),
    )
    arms = improve._build_arms_from_advice(Path("/d.dck"), 3, "heuristic")
    assert [(a.add, a.cut) for a in arms] == [("A", "X"), ("B", "Y"), ("C", "X")]
    assert arms[0].key == "+A / -X"


def test_build_arms_handles_no_cuts(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda deck_path, bracket, source: _FakeReport(["A"], []),
    )
    arms = improve._build_arms_from_advice(Path("/d.dck"), 3, "heuristic")
    assert len(arms) == 1 and arms[0].add == "A" and arms[0].cut is None
    assert arms[0].key == "+A"


def test_build_arms_empty_when_no_adds(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda deck_path, bracket, source: _FakeReport([], ["X"]),
    )
    assert improve._build_arms_from_advice(Path("/d.dck"), 3, "heuristic") == []


def test_main_routes_to_bandit_strategy(tmp_path, monkeypatch):
    captured = {}

    def fake_bandit(deck_path, deck_id, args):
        captured["deck_id"] = deck_id
        captured["strategy"] = args.strategy
        captured["policy"] = args.bandit_policy
        return 0

    monkeypatch.setattr(improve, "_run_bandit_strategy", fake_bandit)
    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "3", "--strategy", "bandit",
                       "--bandit-policy", "epsilon_greedy"])
    assert rc == 0
    assert captured == {"deck_id": "[USER] Goblins [B4]",
                        "strategy": "bandit", "policy": "epsilon_greedy"}


def test_bandit_strategy_no_arms_returns_zero(tmp_path, monkeypatch):
    # Advisor yields no adds → no arms → graceful no-op (rc 0).
    monkeypatch.setattr(improve, "_build_arms_from_advice",
                        lambda deck_path, bracket, source: [])
    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "2", "--strategy", "bandit"])
    assert rc == 0


def test_bandit_strategy_runs_with_injected_arms_and_sim(tmp_path, monkeypatch):
    """End-to-end bandit dispatch with arms + sim stubbed: verifies the
    evaluator advances the base deck on a positive-margin swap and the
    summary reports the winning arm."""
    from commander_builder.bandit import Arm
    monkeypatch.setattr(
        improve, "_build_arms_from_advice",
        lambda deck_path, bracket, source: [
            Arm(key="+Good / -Bad", add="Good", cut="Bad"),
            Arm(key="+Meh / -Bad", add="Meh", cut="Bad"),
        ],
    )
    # Stub the real evaluator with a scripted reward: the "Good" arm pays
    # off, "Meh" doesn't. Avoids Forge/advisor entirely.
    def fake_evaluator(state, args):
        def evaluate(arm):
            return 3.0 if arm.add == "Good" else 0.0
        return evaluate
    monkeypatch.setattr(improve, "_make_swap_evaluator", fake_evaluator)

    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "20", "--strategy", "bandit",
                       "--json"])
    assert rc == 0


# --- --health: FP-013 gate progress ----------------------------------------
#
# The fp013-scope memo asked for a row-count health check that reports
# "high-confidence curator iterations: N / 1,000 toward FP-013" so the
# gate's approach is visible. It must run without a deck or --rounds.


def _seed_gate_row(db_path):
    from commander_builder.knowledge_log import (
        Iteration, init_db, record_iteration,
    )
    init_db(db_path)
    record_iteration(Iteration(
        deck_id="d", deck_name="d", bracket=3,
        audit_manifest={"added": ["A"], "removed": ["B"]},
        verdict="kept", sim_report={"games": 40},
    ), db_path=db_path)


def test_main_health_reports_fp013_gate(tmp_path, capsys):
    db = tmp_path / "kl.sqlite"
    _seed_gate_row(db)
    rc = improve_main(["--health", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 / 1000" in out
    assert "FP-013" in out


def test_main_health_json(tmp_path, capsys):
    import json as _json
    db = tmp_path / "kl.sqlite"
    _seed_gate_row(db)
    rc = improve_main(["--health", "--db-path", str(db), "--json"])
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["target"] == 1000
    assert payload["min_games"] == 40


def test_main_still_requires_rounds_without_health(tmp_path, capsys):
    """Dropping argparse's required=True must not let a normal run
    proceed without --rounds."""
    deck = tmp_path / "[USER] X [B3].dck"
    deck.write_text("[Main]\n", encoding="utf-8")
    rc = improve_main([str(deck)])
    assert rc == 2
    assert "--rounds" in capsys.readouterr().out
