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


# --- P03: the bandit evaluator's accept path -------------------------------
#
# Pre-2026-08-16 the evaluator advanced the base deck on
# ``reward >= args.sim_margin`` with reward = raw (wins_b - wins_a) and
# --sim-margin defaulting to 1 — i.e. ANY one-win edge promoted the
# candidate, with no significance test and no minimum-decisive gate,
# bypassing the discipline _proposer_sim._verdict_from_ab enforces
# everywhere else. Every failure mode (apply exception, <2 fillers, sim
# not done) also returned 0.0, entering crashed sims into the arm
# statistics as measured ties. These tests pin both fixes.

class _AB:
    """Minimal ABResult stand-in (only the fields the evaluator reads)."""

    def __init__(self, wins_a, wins_b, *, status="done", error=None,
                 games=None):
        self.wins_a = wins_a
        self.wins_b = wins_b
        self.status = status
        self.error = error
        self.games = games if games is not None else (wins_a + wins_b) * 2


def _eval_args(**over):
    import argparse
    base = dict(sim_fillers=None, bracket=4, sim_games=90, sim_margin=1)
    base.update(over)
    return argparse.Namespace(**base)


def _make_eval(monkeypatch, tmp_path, *, ab=None, apply_exc=None,
               fillers=("f1.dck", "f2.dck"), args=None, apply_drops_all=False):
    """Build the REAL ``_make_swap_evaluator`` closure with only its
    outermost dependencies stubbed (apply / filler pick / Forge sim), so
    the accept decision under test is the production one.

    ``apply_drops_all=True`` reproduces the pair-validation drop: the real
    ``apply_proposal_to_deck`` writes a candidate file and returns it even
    when it dropped the whole swap (unmatched cut / add already present),
    leaving ``applied_adds``/``applied_cuts`` empty.
    """
    base = tmp_path / "base.dck"
    base.write_text("[metadata]\nName=Base\n", encoding="utf-8")
    candidate = tmp_path / "candidate.dck"
    candidate.write_text("[metadata]\nName=Cand\n", encoding="utf-8")

    def fake_apply(deck_path, proposal, dry_run=False):
        if apply_exc is not None:
            raise apply_exc
        # Mirror production: apply_proposal_to_deck populates the APPLIED
        # lists on the proposal it was handed (empty when pair validation
        # dropped the swap). The evaluator's no-op guard reads them.
        if not apply_drops_all:
            proposal.applied_adds = list(proposal.adds)
            proposal.applied_cuts = list(proposal.cuts)
        return candidate

    monkeypatch.setattr("commander_builder.proposer.apply_proposal_to_deck",
                        fake_apply)
    monkeypatch.setattr(
        "commander_builder._proposer_sim._pick_filler_decks",
        lambda deck_dir, exclude_paths, count, target_bracket: list(fillers),
    )
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation",
        lambda deck_a_path, deck_b_path, games, fillers: ab,
    )
    state = {"deck": base}
    return state, base, candidate, improve._make_swap_evaluator(
        state, args or _eval_args())


def _arm(key="+Good / -Bad"):
    from commander_builder.bandit import Arm
    return Arm(key=key, add="Good", cut="Bad")


def test_evaluator_does_not_advance_base_on_23_22_split(monkeypatch, tmp_path):
    """(a) REGRESSION: a 23-22 coin-flip split over 45 decisive games is
    not significant (exact two-sided binomial p = 1.0), so the base deck
    must NOT advance. The old ``reward >= sim_margin`` rule accepted it."""
    state, base, candidate, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(22, 23, games=45))
    out = evaluate(_arm())
    assert out.verdict == "neutral"
    assert out.accepted is False
    assert state["deck"] == base  # base did NOT advance
    assert out.skipped is False
    assert out.reward == pytest.approx(1 / 45)  # a real, tiny measurement


def test_evaluator_advances_base_on_significant_split(monkeypatch, tmp_path):
    """(b) A significance-passing split (30-15 over 45 decisive,
    p = 0.036 < VERDICT_ALPHA) MUST advance the base."""
    state, base, candidate, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(15, 30, games=45))
    out = evaluate(_arm())
    assert out.verdict == "kept"
    assert out.accepted is True
    assert state["deck"] == candidate  # base advanced
    assert out.reward == pytest.approx(15 / 45)


def test_evaluator_never_advances_on_a_significant_loss(monkeypatch, tmp_path):
    """The mirror image: a significant split the WRONG way is 'reverted'
    and obviously must not advance — but it is still a real measurement,
    so it feeds the arm's stats with a negative reward."""
    state, base, _cand, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(30, 15, games=45))
    out = evaluate(_arm())
    assert out.verdict == "reverted"
    assert out.accepted is False
    assert state["deck"] == base
    assert out.reward == pytest.approx(-15 / 45)


def test_evaluator_respects_the_min_decisive_gate(monkeypatch, tmp_path):
    """A lopsided but tiny sample (12-3 = 15 decisive, under
    MIN_DECISIVE_GAMES_FOR_VERDICT) is 'inconclusive', never 'kept' —
    the decisive gate the rest of the branch enforces applies here too."""
    from commander_builder._proposer_sim import MIN_DECISIVE_GAMES_FOR_VERDICT
    assert 15 < MIN_DECISIVE_GAMES_FOR_VERDICT
    state, base, _cand, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(3, 12, games=30))
    out = evaluate(_arm())
    assert out.verdict == "inconclusive"
    assert out.accepted is False
    assert state["deck"] == base


def test_evaluator_sim_margin_stays_a_back_compat_prefilter(monkeypatch,
                                                            tmp_path):
    """--sim-margin survives as a PRE-filter: raising it above what
    significance demands still blocks the accept (30-15 is significant
    but its margin of 15 is under a --sim-margin of 20)."""
    state, base, _cand, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(15, 30, games=45),
        args=_eval_args(sim_margin=20))
    out = evaluate(_arm())
    assert out.verdict == "neutral"
    assert out.accepted is False
    assert state["deck"] == base


@pytest.mark.parametrize("kwargs,expect", [
    ({"apply_exc": RuntimeError("boom"), "ab": None}, "apply_failed"),
    ({"ab": _AB(0, 0, status="failed", error="jvm died")}, "sim_failed"),
    ({"ab": _AB(0, 0, status="skipped")}, "sim_skipped"),
    ({"ab": _AB(0, 0)}, "zero_decisive_games"),
    ({"ab": _AB(1, 1), "fillers": ("only-one.dck",)}, "fillers_unavailable"),
])
def test_evaluator_failure_modes_are_skips_not_zero_rewards(
        monkeypatch, tmp_path, capsys, kwargs, expect):
    """(c) REGRESSION: every failure mode returns a SKIP (no reward), not
    the old 0.0 'measured tie', and logs its reason."""
    state, base, _cand, evaluate = _make_eval(monkeypatch, tmp_path, **kwargs)
    out = evaluate(_arm())
    assert out.skipped is True
    assert out.reward is None
    assert out.accepted is False
    assert expect in out.skip_reason
    assert state["deck"] == base
    assert "skipped" in capsys.readouterr().err


def test_failed_pull_leaves_arm_statistics_untouched(monkeypatch, tmp_path):
    """(c) End-to-end through ``run_bandit``: a crashed pull must leave
    the arm's pull count and reward statistics unchanged and be logged
    as skipped. Previously it landed as a 0.0-reward pull, so a broken
    swap looked exactly like a perfectly neutral one."""
    import random
    from commander_builder.bandit import run_bandit, make_policy

    _state, _base, _cand, evaluate = _make_eval(
        monkeypatch, tmp_path, apply_exc=ValueError("illegal cut"), ab=None)
    arms = [_arm("+Good / -Bad")]
    res = run_bandit(arms, 3, evaluate, make_policy("ucb1"),
                     rng=random.Random(0))
    assert arms[0].pulls == 0
    assert arms[0].total_reward == 0.0
    assert arms[0].mean == 0.0
    assert arms[0].skips == 1
    assert "illegal cut" in arms[0].skip_reason
    assert res.skipped == 1
    assert res.accepted == 0
    assert res.total_reward == 0.0
    assert res.best_arm_key is None  # nothing was ever measured


# --- R2-P01: the no-op guard the bandit evaluator was missing --------------
#
# ``apply_proposal_to_deck`` does not RAISE when pair validation drops the
# whole swap (cut not in the decklist, add already present, add is the
# commander) — it writes a content-identical candidate and returns it. The
# evaluator went straight from there into a 45-game Forge run, measuring
# base-vs-base noise and booking it as the arm's real reward. Both sibling
# call sites already guarded this (improve_search's probe evaluator,
# iteration_loop's RuntimeError); this path did not.

def test_evaluator_skips_a_swap_the_applier_dropped(
    monkeypatch, tmp_path, capsys,
):
    """An arm whose cut card is absent from the deck: the applier drops
    the pair, so the candidate is content-identical to the base. The pull
    must SKIP (no Forge run, no reward, no advance), not sim a deck
    against a copy of itself."""
    sims: list = []

    state, base, _cand, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(15, 30, games=45),
        apply_drops_all=True,
    )
    # Re-patch the sim to record calls: nothing may reach Forge.
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation",
        lambda **kw: sims.append(kw),
    )

    out = evaluate(_arm())

    assert out.skipped is True
    assert out.skip_reason == "swap_dropped_by_legality"
    assert out.reward is None
    assert out.accepted is False
    assert state["deck"] == base          # base did NOT advance
    assert sims == []                     # no Forge budget spent
    assert "swap_dropped_by_legality" in capsys.readouterr().err


def test_dropped_swap_never_advances_even_on_a_significant_split(
    monkeypatch, tmp_path,
):
    """The point of the guard: a no-op sim CAN come back significant
    (~1 run in 83 at the shipped gate), and without the guard that
    'advances' the base to a restamped copy of itself. With the guard the
    scripted 30-15 sweep never even runs."""
    state, base, cand, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(15, 30, games=45),
        apply_drops_all=True,
    )
    out = evaluate(_arm())
    assert out.verdict is None            # no verdict was ever computed
    assert state["deck"] == base != cand


# --- R2-P07: --sim-margin is not the bandit's accept_threshold -------------

def test_run_bandit_is_not_handed_sim_margin_as_accept_threshold(
    tmp_path, monkeypatch,
):
    """``accept_threshold`` compares against the NORMALIZED [-1, +1]
    reward scale; ``--sim-margin`` is a raw decisive-game margin. Passing
    one as the other was a units error (it made `reward >= 1` = 'only a
    clean sweep counts', and any raised --sim-margin arithmetically
    unsatisfiable). improve must not pass the kwarg at all — acceptance
    on this path is the evaluator's significance test."""
    from commander_builder.bandit import Arm, BanditResult

    seen: dict = {}

    def fake_run_bandit(arms, rounds, evaluate, policy, **kwargs):
        seen.update(kwargs)
        return BanditResult(rounds_run=0, accepted=0, best_arm_key=None,
                            best_arm_mean=0.0, total_reward=0.0)

    monkeypatch.setattr("commander_builder.bandit.run_bandit", fake_run_bandit)
    monkeypatch.setattr(
        improve, "_build_arms_from_advice",
        lambda deck_path, bracket, source: [
            Arm(key="+Good / -Bad", add="Good", cut="Bad"),
        ],
    )
    deck = tmp_path / "[USER] Goblins [B4].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "2", "--strategy", "bandit",
                       "--sim-margin", "7", "--json"])

    assert rc == 0
    assert "accept_threshold" not in seen
    assert 7 not in seen.values()


# --- P16: reward normalization --------------------------------------------

def test_signed_margin_reward_bounds():
    """(d) The reward handed to the bandit is bounded in [-1, +1] — the
    O(1) scale UCB1's c=1.4 and Thompson's unit obs_var assume. The old
    raw margin was O(±20) at 45-game pulls, which swamped the
    exploration bonus and collapsed both policies to greedy."""
    f = improve._signed_margin_reward
    assert f(45, 0) == -1.0        # new deck lost every decisive game
    assert f(0, 45) == 1.0         # ...and won every one
    assert f(22, 23) == pytest.approx(1 / 45)
    assert f(23, 23) == 0.0        # exact tie = break-even
    for wins_a in range(0, 46):
        for wins_b in range(0, 46):
            if wins_a + wins_b == 0:
                continue
            assert -1.0 <= f(wins_a, wins_b) <= 1.0


def test_signed_margin_reward_none_on_zero_decisive():
    """No decisive games = no signal. Returning 0.0 would launder
    'nothing measured' into 'measured break-even'."""
    assert improve._signed_margin_reward(0, 0) is None


def test_signed_margin_reward_is_the_affine_sibling_of_search_reward():
    """The normalization matches the convention improve_search already
    uses: (m + 1) / 2 == margin_reward's wins_b / decisive."""
    from commander_builder.improve_search import margin_reward
    for wins_a, wins_b in [(30, 15), (15, 30), (22, 23), (0, 45), (45, 0)]:
        m = improve._signed_margin_reward(wins_a, wins_b)
        assert (m + 1) / 2 == pytest.approx(margin_reward(wins_a, wins_b))


def test_normalized_reward_keeps_ucb1_exploration_alive(monkeypatch, tmp_path):
    """P16 in behavioral terms: with rewards on the normalized scale,
    UCB1's exploration bonus is comparable to the means, so a single
    lucky pull cannot lock the policy onto one arm. On the old raw
    margin scale (O(±20)) the bonus was noise by comparison."""
    import random
    from commander_builder.bandit import Arm, run_bandit, make_policy

    # Arm 'lucky' wins its first pull outright (reward +1.0), then is
    # mediocre; 'steady' is consistently good. With a live exploration
    # term every arm keeps getting sampled.
    seen = {"lucky": 0, "steady": 0}

    def evaluate(arm):
        seen[arm.key] += 1
        if arm.key == "lucky":
            return 1.0 if seen["lucky"] == 1 else -0.2
        return 0.4

    arms = [Arm("lucky"), Arm("steady")]
    run_bandit(arms, 20, evaluate, make_policy("ucb1", c=1.4),
               rng=random.Random(0))
    assert seen["steady"] >= 5, "exploration collapsed to greedy-on-one-pull"
    assert min(a.pulls for a in arms) >= 2


# --- replication: unattended advances need a confirming second A/B ---------
#
# 2026-08-17 owner decision. At alpha 0.05 a truly neutral swap still
# earns a 'kept' about 1 run in 40, and the loop CHAINS its rounds — a
# false positive becomes the base every later round is measured against.
# Unattended runs therefore re-test a first 'kept' with a second
# independent A/B and advance only if it agrees; interactive single-swap
# (--strategy bandit) runs stay single-shot. These tests pin the gate at
# the loop, the default resolution, and the honest recording of a
# disagreement.

def _replicate_args(**over):
    import argparse
    base = dict(replicate=True, json=False, bracket=3, sim_games=40,
                sim_margin=1, sim_fillers="F1.dck,F2.dck", db_path=None,
                strategy="greedy")
    base.update(over)
    return argparse.Namespace(**base)


def _scripted_replicate_fn(verdicts):
    """replicate_fn double: returns a scripted second-run verdict per
    call and records (base, candidate, iteration_id) per invocation."""
    calls: list[tuple] = []

    def fn(base_path, candidate_path, args, iteration_id=None):
        v = verdicts[len(calls)]
        calls.append((Path(base_path), Path(candidate_path), iteration_id))
        confirmed = v == "kept"
        return improve.Replication(
            verdict=v, confirmed=confirmed,
            notes=("replication_confirmed" if confirmed
                   else f"replication_failed: run 1 kept, run 2 {v}"),
        )

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def test_single_kept_does_not_advance_under_replication():
    """The decision in one line: under replication, one 'kept' is not
    enough. The loop must consult the confirming run BEFORE advancing,
    and a non-confirming answer leaves the base where it was."""
    fn = _make_script(["kept"])
    rep = _scripted_replicate_fn(["neutral"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 1,
                           _replicate_args(), round_fn=fn, replicate_fn=rep)

    assert len(rep.calls) == 1                 # the gate was consulted
    assert res.rounds_kept == 0
    assert res.history[0].advanced is False
    assert Path(res.final_deck) == Path("/decks/start.dck")


def test_kept_plus_kept_advances():
    """A confirmed swap still advances — replication is a gate, not a
    block. The confirming run gets the SAME pairing the first one had
    (current base vs this round's candidate) and the round's iteration
    id, so the outcome can be written back to the right row."""
    fn = _make_script(["kept", "kept"])
    rep = _scripted_replicate_fn(["kept", "kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 2,
                           _replicate_args(), round_fn=fn, replicate_fn=rep)

    assert res.rounds_kept == 2
    assert [r.advanced for r in res.history] == [True, True]
    assert [r.replicated for r in res.history] == [True, True]
    assert [r.replication_verdict for r in res.history] == ["kept", "kept"]
    assert Path(res.final_deck) == Path("/decks/v2.dck")
    # Same pairing: round 1 confirms start-vs-v1, round 2 v1-vs-v2.
    assert rep.calls[0][:2] == (Path("/decks/start.dck"), Path("/decks/v1.dck"))
    assert rep.calls[1][:2] == (Path("/decks/v1.dck"), Path("/decks/v2.dck"))
    assert rep.calls[0][2] == 101  # iteration_id from the round result


def test_kept_plus_neutral_does_not_advance_and_is_recorded(capsys):
    """A disagreement must be recorded honestly, in the EXISTING verdict
    vocabulary: the round reports the second run's verdict (the
    non-advancing one), keeps the unconfirmed 'kept' legible in
    replication_verdict, and says so in the printed output."""
    fn = _make_script(["kept", "kept"])
    rep = _scripted_replicate_fn(["neutral", "kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 2,
                           _replicate_args(), round_fn=fn, replicate_fn=rep)

    r1, r2 = res.history
    assert r1.verdict == "neutral"          # not the unconfirmed 'kept'
    assert r1.replicated is False
    assert r1.replication_verdict == "neutral"
    assert r1.advanced is False
    # Round 2 built on the ORIGINAL base, since round 1 never advanced.
    assert fn.calls[1] == Path("/decks/start.dck")
    assert r2.advanced is True
    assert res.rounds_kept == 1

    out = capsys.readouterr().out
    assert "replication_failed" in out       # the reason is visible
    assert "confirming second A/B" in out

    # ... and it survives into the run summary the operator reads last.
    improve._print_summary(res)
    summary = capsys.readouterr().out
    assert "replication FAILED" in summary
    assert "run 2 neutral" in summary


def test_replication_off_keeps_single_shot_behaviour():
    """--no-replicate (and every pre-2026-08-17 caller, whose args carry
    no 'replicate' attribute at all) advances on one 'kept' and never
    spends the second sim."""
    fn = _make_script(["kept"])
    rep = _scripted_replicate_fn(["kept"])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 1,
                           _replicate_args(replicate=False),
                           round_fn=fn, replicate_fn=rep)
    assert rep.calls == []                   # no confirming sim was run
    assert res.rounds_kept == 1
    assert res.history[0].advanced is True
    assert res.history[0].replicated is None  # not attempted, not "failed"

    # Same for a bare args object with no replicate attribute.
    fn2 = _make_script(["kept"])
    rep2 = _scripted_replicate_fn(["kept"])
    res2 = run_improve_loop(Path("/decks/start.dck"), "start", 1, object(),
                            round_fn=fn2, replicate_fn=rep2)
    assert rep2.calls == [] and res2.rounds_kept == 1


def test_non_kept_rounds_never_trigger_a_confirming_sim():
    """Replication gates the ADVANCE, so it costs nothing on rounds that
    weren't going to advance anyway."""
    fn = _make_script(["neutral", "reverted", "pending"])
    rep = _scripted_replicate_fn([])
    res = run_improve_loop(Path("/decks/start.dck"), "start", 3,
                           _replicate_args(), round_fn=fn, replicate_fn=rep)
    assert rep.calls == []
    assert res.rounds_kept == 0


# --- replication defaults + CLI wiring -------------------------------------

def test_replicate_default_is_on_for_the_unattended_round_loop():
    """Unattended = the greedy/search round loop: it chains rounds for
    hours with nobody watching, so a false 'kept' compounds. ON."""
    import argparse
    args = argparse.Namespace(replicate=None, strategy="greedy")
    assert improve.resolve_replicate_default(args) is True
    # --search-budget runs INSIDE the greedy loop; same default.
    args = argparse.Namespace(replicate=None, strategy="greedy",
                              search_budget=8)
    assert improve.resolve_replicate_default(args) is True


def test_replicate_default_is_off_for_the_interactive_bandit_explorer():
    """--strategy bandit is the single-swap explorer the operator reads
    live, and UCB1 re-pulls promising arms on its own — doubling every
    accepting pull's Forge bill buys little. OFF (but overridable)."""
    import argparse
    args = argparse.Namespace(replicate=None, strategy="bandit")
    assert improve.resolve_replicate_default(args) is False
    # An explicit flag always wins over the shape-based default.
    assert improve.resolve_replicate_default(
        argparse.Namespace(replicate=True, strategy="bandit")) is True
    assert improve.resolve_replicate_default(
        argparse.Namespace(replicate=False, strategy="greedy")) is False


def test_main_resolves_replicate_on_by_default_and_states_the_cost(
    tmp_path, monkeypatch, capsys,
):
    """The 2x sim cost must be in the operator's face BEFORE the first
    sim, not discovered from the wall clock."""
    captured = {}

    def stub(deck_path, deck_id, rounds, args, **kw):
        captured["replicate"] = args.replicate
        return ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds, rounds_run=0,
            rounds_kept=0, converged=False,
        )
    monkeypatch.setattr(improve, "run_improve_loop", stub)

    deck = tmp_path / "[USER] Rep [B3].dck"
    deck.write_text("[metadata]\nName=Rep\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "1"])

    assert rc == 0
    assert captured["replicate"] is True
    out = capsys.readouterr().out
    assert "replication: ON" in out
    assert "2x sim time" in out
    assert "--no-replicate" in out


def test_main_no_replicate_flag_turns_it_off(tmp_path, monkeypatch, capsys):
    captured = {}

    def stub(deck_path, deck_id, rounds, args, **kw):
        captured["replicate"] = args.replicate
        return ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds, rounds_run=0,
            rounds_kept=0, converged=False,
        )
    monkeypatch.setattr(improve, "run_improve_loop", stub)

    deck = tmp_path / "[USER] Rep [B3].dck"
    deck.write_text("[metadata]\nName=Rep\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "1", "--no-replicate"])

    assert rc == 0
    assert captured["replicate"] is False
    assert "replication: OFF" in capsys.readouterr().out


def test_replicate_help_documents_the_default_and_the_cost(capsys):
    with pytest.raises(SystemExit):
        improve_main(["--help"])
    out = capsys.readouterr().out
    assert "--replicate" in out and "--no-replicate" in out
    # argparse re-wraps, so match words that survive wrapping.
    assert "Default ON" in out
    assert "bandit" in out


# --- _default_replicate_fn: the real confirming run ------------------------

class _RepAB:
    """ABResult stand-in with only the fields the confirm path reads."""

    def __init__(self, wins_a, wins_b, *, status="done", games=None):
        self.wins_a, self.wins_b, self.status = wins_a, wins_b, status
        self.games = games if games is not None else wins_a + wins_b


def _patch_confirm_sim(monkeypatch, ab, *, fillers=("F1.dck", "F2.dck")):
    """Stub the confirm run's two externals (filler pick + Forge) and
    record the sim calls. No JVM, no network."""
    calls: list[dict] = []

    def fake_sim(deck_a_path, deck_b_path, games, fillers):
        calls.append({"a": Path(deck_a_path), "b": Path(deck_b_path),
                      "games": games, "fillers": list(fillers)})
        return ab
    monkeypatch.setattr("commander_builder.forge_runner.run_ab_simulation",
                        fake_sim)
    monkeypatch.setattr(
        "commander_builder._proposer_sim._pick_filler_decks",
        lambda deck_dir, exclude_paths, count, target_bracket: list(fillers),
    )
    return calls


def _seed_pending_row(db_path, *, notes=None, sim_report=None) -> int:
    from commander_builder.knowledge_log import (
        Iteration, init_db, record_iteration,
    )
    init_db(db_path)
    return record_iteration(Iteration(
        deck_id="rep", deck_name="rep", bracket=3,
        audit_manifest={"added": ["A"], "removed": ["B"]},
        verdict="kept",
        verdict_notes=notes,
        sim_report=sim_report,
    ), db_path=db_path)


#: What run 1 (the auto-curate subprocess) leaves on the row.
_RUN1_NOTE = "A/B sim: old won 15, new won 30, neutral=0 (45 games, margin=1)"
_RUN1_SIM_REPORT = {"status": "done", "wins_a": 15, "wins_b": 30, "games": 45}


def test_default_replicate_fn_confirms_a_repeated_significant_win(
    tmp_path, monkeypatch,
):
    """A second significant win in the SAME direction confirms: the row
    keeps 'kept' and the note records that two runs agreed."""
    from commander_builder.knowledge_log import get_iteration

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db)
    calls = _patch_confirm_sim(monkeypatch, _RepAB(15, 30, games=45))
    args = _replicate_args(db_path=str(db), sim_fillers=None)

    rep = improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    assert rep.confirmed is True and rep.verdict == "kept"
    assert "replication_confirmed" in rep.notes
    assert len(calls) == 1 and calls[0]["games"] == 40
    row = get_iteration(iid, db_path=db)
    assert row.verdict == "kept"
    assert "replication_confirmed" in row.verdict_notes


def test_default_replicate_fn_records_a_failed_replication(tmp_path,
                                                           monkeypatch):
    """A coin-flip second run (23-22 = not significant) refuses the
    advance, and the row is rewritten from 'kept' to the second run's
    verdict with a replication_failed note — leaving 'kept' on a deck
    that never became the base would tell every later reader (dashboard,
    FP-013 counter, a future training set) a swap was adopted when it
    wasn't."""
    from commander_builder.knowledge_log import get_iteration

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db)
    _patch_confirm_sim(monkeypatch, _RepAB(22, 23, games=45))
    args = _replicate_args(db_path=str(db))

    rep = improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    assert rep.confirmed is False
    assert rep.verdict == "neutral"          # existing vocabulary, no new label
    assert "replication_failed" in rep.notes
    assert "run 1 kept, run 2 neutral" in rep.notes

    row = get_iteration(iid, db_path=db)
    assert row.verdict == "neutral"          # reflects the NON-advance
    assert "replication_failed" in row.verdict_notes


def test_default_replicate_fn_unrunnable_gate_stays_shut(tmp_path,
                                                         monkeypatch):
    """No fillers = no confirming evidence. An unattended gate that
    can't run must NOT wave the candidate through."""
    _patch_confirm_sim(monkeypatch, _RepAB(0, 0, status="skipped"),
                       fillers=("only-one.dck",))
    args = _replicate_args(sim_fillers=None, db_path=None)
    rep = improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, None)
    assert rep.confirmed is False
    assert rep.verdict == "pending"
    assert "replication_failed" in rep.notes


# --- R2-P04 / R2-P06: the replicate writer is a SECOND writer --------------
#
# It used to pass a fresh ``notes`` string into update_iteration_sim, which
# overwrites verdict_notes — destroying run 1's split, on a row whose own
# docstring claimed notes "gain a replication_confirmed line". And run 2's
# games lived nowhere structured, so a confirmed 'kept' understated the
# Forge games behind it by half.

def test_replication_appends_to_run_1s_note_instead_of_replacing_it(
    tmp_path, monkeypatch,
):
    from commander_builder.knowledge_log import get_iteration

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db, notes=_RUN1_NOTE,
                            sim_report=dict(_RUN1_SIM_REPORT))
    _patch_confirm_sim(monkeypatch, _RepAB(14, 31, games=45))
    args = _replicate_args(db_path=str(db), sim_fillers=None)

    improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    notes = get_iteration(iid, db_path=db).verdict_notes
    assert _RUN1_NOTE in notes               # run 1 survived
    assert "replication_confirmed" in notes  # run 2 was added
    assert notes.index(_RUN1_NOTE) < notes.index("replication_confirmed")


def test_replication_appends_on_a_failed_confirmation_too(
    tmp_path, monkeypatch,
):
    """The disagreeing case rewrites the VERDICT but must still keep run
    1's measured note beside the failure line."""
    from commander_builder.knowledge_log import get_iteration

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db, notes=_RUN1_NOTE,
                            sim_report=dict(_RUN1_SIM_REPORT))
    _patch_confirm_sim(monkeypatch, _RepAB(22, 23, games=45))
    args = _replicate_args(db_path=str(db), sim_fillers=None)

    improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    row = get_iteration(iid, db_path=db)
    assert row.verdict == "neutral"
    assert _RUN1_NOTE in row.verdict_notes
    assert "replication_failed" in row.verdict_notes


def test_replication_persists_run_2_structurally_in_sim_report(
    tmp_path, monkeypatch,
):
    """Run 2's split lands as DATA under sim_report['replication'] — and
    run 1's sim_report is not clobbered doing it."""
    from commander_builder.knowledge_log import (
        SIM_REPORT_REPLICATION_KEY, get_iteration,
    )

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db, notes=_RUN1_NOTE,
                            sim_report=dict(_RUN1_SIM_REPORT))
    _patch_confirm_sim(monkeypatch, _RepAB(14, 31, games=45))
    args = _replicate_args(db_path=str(db), sim_fillers=None)

    improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    report = get_iteration(iid, db_path=db).sim_report
    # Run 1's measured record is untouched.
    assert report["wins_a"] == 15 and report["wins_b"] == 30
    run2 = report[SIM_REPORT_REPLICATION_KEY]
    assert run2["ran"] is True
    assert run2["verdict"] == "kept" and run2["confirmed"] is True
    assert run2["wins_old"] == 14 and run2["wins_new"] == 31
    assert run2["games"] == 45 and run2["decisive"] == 45
    assert run2["margin"] == 17


def test_replication_records_an_unrunnable_confirm_structurally(
    tmp_path, monkeypatch,
):
    """A confirm that could not RUN is still a fact about the row."""
    from commander_builder.knowledge_log import (
        SIM_REPORT_REPLICATION_KEY, get_iteration,
    )

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db, notes=_RUN1_NOTE,
                            sim_report=dict(_RUN1_SIM_REPORT))
    _patch_confirm_sim(monkeypatch, _RepAB(0, 0, status="skipped"),
                       fillers=("only-one.dck",))
    args = _replicate_args(db_path=str(db), sim_fillers=None)

    improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    run2 = get_iteration(iid, db_path=db).sim_report[SIM_REPORT_REPLICATION_KEY]
    assert run2["ran"] is False
    assert run2["confirmed"] is False
    assert "filler" in (run2["error"] or "")


def test_replication_stamps_the_verdict_parameters_it_used(
    tmp_path, monkeypatch,
):
    """R2-P06: the row records the margin / alpha / decisive floor its
    verdict was scored under, so it is auditable without knowing which
    code version (or which --sim-margin) wrote it."""
    from commander_builder.analyst import MIN_DECISIVE_GAMES_FOR_VERDICT
    from commander_builder._proposer_sim import VERDICT_ALPHA
    from commander_builder.knowledge_log import (
        SIM_REPORT_VERDICT_PARAMS_KEY, get_iteration,
    )

    db = tmp_path / "kl.sqlite"
    iid = _seed_pending_row(db, notes=_RUN1_NOTE,
                            sim_report=dict(_RUN1_SIM_REPORT))
    _patch_confirm_sim(monkeypatch, _RepAB(14, 31, games=45))
    args = _replicate_args(db_path=str(db), sim_fillers=None, sim_margin=6)

    improve._default_replicate_fn(
        tmp_path / "base.dck", tmp_path / "cand.dck", args, iid)

    params = get_iteration(iid, db_path=db).sim_report[
        SIM_REPORT_VERDICT_PARAMS_KEY]
    assert params["margin"] == 6            # the RAISED margin, recorded
    assert params["alpha"] == VERDICT_ALPHA
    assert params["min_decisive"] == MIN_DECISIVE_GAMES_FOR_VERDICT


def test_replication_cannot_recurse(tmp_path, monkeypatch):
    """A confirming run is a BARE A/B sim: it must never re-enter the
    round machinery (which would re-curate, and whose own 'kept' would
    want confirming in turn — an unbounded confirm chain). Pinned by
    exploding if auto-curate is touched and by counting sims."""
    def boom(*a, **kw):  # pragma: no cover - must never be called
        raise AssertionError("replication re-entered the round pipeline")
    monkeypatch.setattr("commander_builder._proposer_cli.auto_curate_main",
                        boom)
    calls = _patch_confirm_sim(monkeypatch, _RepAB(15, 30, games=45))

    fn = _make_script(["kept"])
    res = run_improve_loop(Path(tmp_path / "start.dck"), "start", 1,
                           _replicate_args(db_path=None), round_fn=fn)
    assert len(calls) == 1                   # exactly one confirming sim
    assert res.history[0].replicated is True


# --- replication on the bandit path (opt-in there) -------------------------

def test_bandit_evaluator_replication_blocks_an_unconfirmed_advance(
    monkeypatch, tmp_path, capsys,
):
    """With --replicate on, a 'kept' pull whose confirming run disagrees
    reports the SECOND verdict, keeps accepted=False (so the arm records
    a non-advance) and leaves the base deck alone. The pull keeps run 1's
    reward — one pull is one budget unit."""
    args = _eval_args(replicate=True)
    sims = [_AB(15, 30, games=45), _AB(23, 22, games=45)]
    # Patch the sim BEFORE rebuilding the evaluator: the closure binds
    # run_ab_simulation at build time (the confirm path resolves it
    # lazily), so both runs must see the same scripted function.
    state, base, _cand, _ = _make_eval(
        monkeypatch, tmp_path, ab=None, args=args)
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation",
        lambda deck_a_path, deck_b_path, games, fillers: sims.pop(0),
    )
    evaluate = improve._make_swap_evaluator(state, args)

    out = evaluate(_arm())
    assert out.accepted is False
    assert out.verdict == "neutral"          # the SECOND run's verdict
    assert out.reward == pytest.approx(15 / 45)  # ... run 1's measurement
    assert state["deck"] == base             # base did NOT advance
    assert "replication failed" in capsys.readouterr().err
    assert sims == []                        # exactly two sims, no recursion


def test_bandit_evaluator_replication_confirms(monkeypatch, tmp_path):
    """Two independent 'kept' runs on the same pull advance the base."""
    args = _eval_args(replicate=True)
    sims = [_AB(15, 30, games=45), _AB(14, 31, games=45)]

    state, _base, candidate, _ = _make_eval(
        monkeypatch, tmp_path, ab=None, args=args)
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation",
        lambda deck_a_path, deck_b_path, games, fillers: sims.pop(0),
    )
    evaluate = improve._make_swap_evaluator(state, args)
    out = evaluate(_arm())
    assert out.accepted is True and out.verdict == "kept"
    assert state["deck"] == candidate


def test_bandit_evaluator_is_single_shot_by_default(monkeypatch, tmp_path):
    """Default OFF on this path: one significant pull still advances,
    exactly as before 2026-08-17."""
    state, _base, candidate, evaluate = _make_eval(
        monkeypatch, tmp_path, ab=_AB(15, 30, games=45))
    out = evaluate(_arm())
    assert out.accepted is True
    assert state["deck"] == candidate


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


# --- _safe_print / summary encoding safety (Windows cp1252 consoles) ------

import contextlib
import io


class _CP1252Stream(io.StringIO):
    """Mimics a Windows cp1252 console: ``write`` raises
    UnicodeEncodeError for text cp1252 can't represent (e.g. Δ), which
    is exactly how the FP-012 shakedown crash manifested."""

    encoding = "cp1252"

    def write(self, s: str) -> int:
        s.encode("cp1252")  # raises UnicodeEncodeError like a real console
        return super().write(s)


def test_safe_print_survives_stream_that_rejects_delta():
    stream = _CP1252Stream()
    improve._safe_print("margin (Δ+3)", file=stream)  # must not raise
    out = stream.getvalue()
    assert "+3" in out          # the payload survives...
    assert "Δ" not in out       # ...with the char replaced, not the line lost
    assert out.endswith("\n")


def test_safe_print_passes_text_through_on_capable_stream():
    stream = io.StringIO()  # StringIO accepts any str, like a UTF-8 console
    improve._safe_print("(Δ+3)", file=stream)
    assert stream.getvalue() == "(Δ+3)\n"


def test_print_summary_survives_cp1252_console():
    """Regression: the run summary's per-round Δ line killed the whole
    improve run with UnicodeEncodeError on cp1252 stdout AFTER all the
    work had completed. It must print (readably) instead."""
    result = ImproveResult(
        deck_id="deck", start_deck="/decks/a.dck", final_deck="/decks/v1.dck",
        rounds_requested=1, rounds_run=1, rounds_kept=1, converged=False,
        history=[RoundResult(
            round=1, input_deck="/decks/a.dck", output_deck="/decks/v1.dck",
            verdict="kept", advanced=True, iteration_id=7,
            win_rate_old=0.40, win_rate_new=0.47, margin=3,
            applied_adds=2, applied_cuts=2,
        )],
    )
    stream = _CP1252Stream()
    with contextlib.redirect_stdout(stream):
        improve._print_summary(result)  # must not raise
    out = stream.getvalue()
    assert "round 1: kept" in out
    assert "old=40% new=47%" in out
    assert "+3" in out  # margin still readable where the Δ was
    assert "Best deck: /decks/v1.dck" in out
