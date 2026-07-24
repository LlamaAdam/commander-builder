"""Tests for the FP-012 full slice — budget-bounded UCB1 swap search
inside the improve loop (``improve_search.py`` + the ``--search-budget``
CLI wiring in ``improve.py``).

Everything is deterministic and driven through the injected seams
(``arm_builder``, ``sim_fn``, scripted ``evaluate`` callables): no test
touches Forge, Anthropic, or the network. The UCB1 selection order is
pinned on a HAND-COMPUTED scenario so a silent change to the policy
math (or the exploration constant plumbing) trips a test rather than
quietly reshaping search behavior.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from commander_builder import improve, improve_search
from commander_builder.forge_batch import ABResult
from commander_builder.improve import improve_main, run_improve_loop
from commander_builder.improve_search import (
    BREAK_EVEN_REWARD,
    MAX_APPLIED_SWAPS,
    SearchArm,
    build_swap_arms,
    make_search_round_fn,
    margin_reward,
    run_swap_search,
)


# --- helpers ----------------------------------------------------------------

def _scripted_evaluate(rewards: dict):
    """Per-arm-key FIFO reward script. ``None`` entries model a
    no-signal pull. Records the pull order on ``.calls``."""
    queues = {k: list(v) for k, v in rewards.items()}
    calls: list[str] = []

    def evaluate(arm):
        calls.append(arm.key)
        q = queues[arm.key]
        if not q:
            raise AssertionError(f"reward script exhausted for arm {arm.key}")
        return q.pop(0)

    evaluate.calls = calls  # type: ignore[attr-defined]
    return evaluate


def _make_dck(tmp_path, name: str, main_cards: list[str]) -> Path:
    """Small legal-enough .dck; apply_proposal_to_deck pads to 99 with
    basics (fixture includes Forests so padding has a color to match),
    same shape as tests/test_proposer_auto.py's fixture."""
    body = (
        "[metadata]\nName=Test\n"
        "[Commander]\n1 Test Commander\n[Main]\n"
    )
    body += "\n".join(f"1 {c}" for c in main_cards) + "\n"
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


_FIXTURE_MAIN = ["Sol Ring", "OldCard A", "OldCard B",
                 "Forest", "Forest", "Forest"]


def _search_args(tmp_path, **overrides) -> argparse.Namespace:
    """The argparse-namespace shape make_search_round_fn's round reads."""
    base = dict(
        search_budget=5, search_min_pulls=2, ucb_c=1.4,
        bracket=3, source="heuristic",
        sim_games=45, sim_margin=1, sim_fillers="F1.dck,F2.dck",
        db_path=str(tmp_path / "kl.sqlite"),
        protect=[], protect_from=None, intent=None, json=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# --- margin_reward: decisive-margin -> [0,1] mapping ------------------------

def test_margin_reward_is_new_deck_decisive_share():
    # (m+1)/2 with m=(wins_b-wins_a)/decisive collapses to wins_b/decisive.
    assert margin_reward(10, 15) == pytest.approx(0.6)
    assert margin_reward(0, 20) == pytest.approx(1.0)
    assert margin_reward(20, 0) == pytest.approx(0.0)


def test_margin_reward_break_even_is_half():
    # Equal decisive wins = zero margin = exactly the break-even reward,
    # which winners must STRICTLY exceed.
    assert margin_reward(5, 5) == pytest.approx(BREAK_EVEN_REWARD)


def test_margin_reward_equals_affine_map_of_signed_margin():
    for wa, wb in [(3, 7), (12, 8), (1, 1), (0, 4), (9, 0)]:
        d = wa + wb
        m = (wb - wa) / d
        assert margin_reward(wa, wb) == pytest.approx((m + 1) / 2)


def test_margin_reward_zero_decisive_is_none():
    # All games to fillers/draws: NO margin exists. Returning 0.5 here
    # would launder "no signal" into break-even evidence.
    assert margin_reward(0, 0) is None


# --- run_swap_search: UCB1 selection order pinned (hand-computed) -----------

def test_ucb1_pull_sequence_pinned_hand_computed():
    """3 arms, c=1.0, budget 6. Hand-computed sequence:

    Pulls 1-3: cold start in list order -> A(0.8), B(0.4), C(0.6).
    Pull 4: N=3, bonus sqrt(ln3/1)=1.0482 equal for all -> argmax mean
            -> A. A gets 0.2 -> mean 0.5.
    Pull 5: N=4, ln4=1.3863:
            A: 0.5 + sqrt(1.3863/2) = 1.3326
            B: 0.4 + sqrt(1.3863/1) = 1.5774
            C: 0.6 + sqrt(1.3863/1) = 1.7774  -> C. C gets 0.6.
    Pull 6: N=5, ln5=1.6094:
            A: 0.5 + sqrt(1.6094/2) = 1.3972
            B: 0.4 + sqrt(1.6094/1) = 1.6686  -> B
            C: 0.6 + sqrt(1.6094/2) = 1.4972
    Sequence: A B C A C B. Winners (min_pulls=2, mean > 0.5): only C
    (A's mean is exactly break-even 0.5 -> excluded by the STRICT >).
    """
    arms = [SearchArm(key="A", add="a"), SearchArm(key="B", add="b"),
            SearchArm(key="C", add="c")]
    ev = _scripted_evaluate({"A": [0.8, 0.2], "B": [0.4, 0.4],
                             "C": [0.6, 0.6]})
    res = run_swap_search(arms, 6, ev, ucb_c=1.0, min_pulls=2)

    assert ev.calls == ["A", "B", "C", "A", "C", "B"]
    assert res.pulls_used == 6
    assert [w["key"] for w in res.chosen] == ["C"]
    assert res.chosen[0]["mean"] == pytest.approx(0.6)
    # A tied break-even exactly -> not applied.
    a = next(s for s in res.arm_stats if s["key"] == "A")
    assert a["mean"] == pytest.approx(0.5)


def test_zero_decisive_marks_arm_no_update_no_repull():
    """A None reward (zero-decisive sim) must NOT touch the arm's
    stats, must mark it, and must remove it from future selection."""
    arms = [SearchArm(key="A", add="a"), SearchArm(key="B", add="b")]
    ev = _scripted_evaluate({"A": [0.7, 0.7, 0.7], "B": [None]})
    res = run_swap_search(arms, 4, ev, min_pulls=2)

    # B died on its cold-start pull; A absorbed the rest of the budget.
    assert ev.calls == ["A", "B", "A", "A"]
    b = arms[1]
    assert b.dead is True
    assert b.death_reason == "no_signal"
    assert b.pulls == 0 and b.total_reward == 0.0  # marked, NOT updated
    assert [w["key"] for w in res.chosen] == ["A"]
    # History records the no-signal pull honestly.
    assert [(h.arm_key, h.no_signal) for h in res.history] == [
        ("A", False), ("B", True), ("A", False), ("A", False)]


def test_all_arms_dead_stops_early_budget_unspent():
    arms = [SearchArm(key="A", add="a"), SearchArm(key="B", add="b")]
    ev = _scripted_evaluate({"A": [None], "B": [None]})
    res = run_swap_search(arms, 10, ev, min_pulls=1)
    assert res.pulls_used == 2  # not 10 — nothing left to measure
    assert res.chosen == []


def test_budget_exhaustion_min_pulls_respected():
    """Budget 4 over 3 arms: only ONE arm can reach 2 pulls, so even
    though B and C have means above break-even they lack the evidence
    floor and must not be applied."""
    arms = [SearchArm(key="A", add="a"), SearchArm(key="B", add="b"),
            SearchArm(key="C", add="c")]
    ev = _scripted_evaluate({"A": [0.9, 0.9], "B": [0.55], "C": [0.52]})
    res = run_swap_search(arms, 4, ev, ucb_c=1.0, min_pulls=2)

    # Cold start A,B,C then the equal-bonus argmax-mean pull -> A.
    assert ev.calls == ["A", "B", "C", "A"]
    assert [w["key"] for w in res.chosen] == ["A"]


def test_winner_cap_and_ordering():
    """More than MAX_APPLIED_SWAPS qualifying arms: chosen is the top
    MAX_APPLIED_SWAPS by mean, best first (see the module's rationale
    for why the cap exists: individually-measured swaps compound
    extrapolation error when stacked)."""
    arms = []
    for i, mean in enumerate([0.6, 0.9, 0.7, 0.8, 0.65]):
        a = SearchArm(key=f"arm{i}", add=f"c{i}")
        a.pulls = 2
        a.total_reward = mean * 2
        arms.append(a)
    # Budget 0: no pulls — winner selection runs on existing stats.
    res = run_swap_search(arms, 0, lambda arm: None, min_pulls=2)
    assert len(res.chosen) == MAX_APPLIED_SWAPS == 3
    assert [w["key"] for w in res.chosen] == ["arm1", "arm3", "arm2"]


def test_negative_budget_rejected():
    with pytest.raises(ValueError):
        run_swap_search([SearchArm(key="A")], -1, lambda a: 0.5)


# --- build_swap_arms: offline candidate sourcing ----------------------------

class _FakeReport:
    def __init__(self, added, removed):
        self._added, self._removed = added, removed

    def to_manifest(self):
        return {"added": self._added, "removed": self._removed}


def test_build_arms_pairs_and_cycles_cuts():
    arms = build_swap_arms(
        Path("/d.dck"), 3, "heuristic",
        advise_fn=lambda **kw: _FakeReport(["A", "B", "C"], ["X", "Y"]),
    )
    assert [(a.add, a.cut) for a in arms] == [
        ("A", "X"), ("B", "Y"), ("C", "X")]
    assert arms[0].key == "+A / -X"
    assert all(isinstance(a, SearchArm) and not a.dead for a in arms)


def test_build_arms_protected_cuts_never_become_arms():
    """A protected cut would be stripped at apply time anyway — probing
    it would sim base-vs-base and burn a full pull on nothing. The
    filter is case-insensitive, like every other protection check."""
    arms = build_swap_arms(
        Path("/d.dck"), 3, "heuristic",
        protected=("x",),
        advise_fn=lambda **kw: _FakeReport(["A", "B"], ["X", "Y"]),
    )
    assert [(a.add, a.cut) for a in arms] == [("A", "Y"), ("B", "Y")]


def test_build_arms_coerces_claude_source_offline(capsys):
    """The curator stays OUT of the search inner loop: --source claude
    is coerced to the heuristic pool, loudly."""
    seen = {}

    def fake_advise(**kw):
        seen.update(kw)
        return _FakeReport(["A"], ["X"])

    arms = build_swap_arms(Path("/d.dck"), 3, "claude", advise_fn=fake_advise)
    assert seen["source"] == "heuristic"
    assert len(arms) == 1
    assert "claude" in capsys.readouterr().err.lower()


def test_build_arms_max_arms_caps_pool():
    arms = build_swap_arms(
        Path("/d.dck"), 3, "heuristic", max_arms=2,
        advise_fn=lambda **kw: _FakeReport(["A", "B", "C", "D"], ["X"]),
    )
    assert len(arms) == 2  # advisor order = ranked order; keep the best


# --- the search round: integration through run_improve_loop -----------------

def _scripted_sim(good_token="Good", decisive=(20, 5), total_games=45):
    """sim_fn double matching forge_batch.run_ab_simulation's shape.
    Decks containing ``good_token`` win ``decisive[0]`` of the decisive
    games; everything else loses. Records every call."""
    calls = []

    def sim(deck_a_path, deck_b_path, games=None, fillers=None, **kw):
        text = Path(deck_b_path).read_text(encoding="utf-8")
        calls.append({"a": str(deck_a_path), "b": str(deck_b_path),
                      "fillers": list(fillers or [])})
        hi, lo = decisive
        if good_token in text:
            wa, wb = lo, hi
        else:
            wa, wb = hi, lo
        return ABResult(deck_a=str(deck_a_path), deck_b=str(deck_b_path),
                        wins_a=wa, wins_b=wb, games=total_games,
                        status="done")

    sim.calls = calls  # type: ignore[attr-defined]
    return sim


def test_search_round_picks_scripted_best_and_keep_if_better_advances(tmp_path):
    """End-to-end round: the bandit finds the scripted-best swap
    (+Good), the combined proposal goes through the shared apply path,
    the verdict sim says 'kept', and run_improve_loop's UNCHANGED
    greedy machinery advances the base deck."""
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(tmp_path)

    arms_built = {}

    def arm_builder(deck_path, bracket, source, *, protected=(), max_arms=None):
        arms_built.update(dict(bracket=bracket, source=source,
                               max_arms=max_arms))
        return [SearchArm(key="+Good / -OldCard A", add="Good", cut="OldCard A"),
                SearchArm(key="+Meh / -OldCard B", add="Meh", cut="OldCard B")]

    sim = _scripted_sim()
    round_fn = make_search_round_fn(arm_builder=arm_builder, sim_fn=sim)
    res = run_improve_loop(deck, "foo", 1, args, round_fn=round_fn)

    # Pool sizing: budget 5 // min_pulls 2 = 2 arms allowed.
    assert arms_built == {"bracket": 3, "source": "heuristic", "max_arms": 2}

    rr = res.history[0]
    assert rr.verdict == "kept"
    assert rr.advanced is True
    assert res.rounds_kept == 1
    assert Path(res.final_deck).name == "[USER] Foo v2 [B3].dck"
    assert rr.applied_adds == 1 and rr.applied_cuts == 1

    # The applied deck carries the bandit's winner, not the loser.
    out_text = Path(res.final_deck).read_text(encoding="utf-8")
    assert "1 Good" in out_text
    assert "1 OldCard A" not in out_text
    assert "1 Meh" not in out_text

    # Budget accounting: 5 pulls + 1 verdict sim, all on the SAME
    # fillers (picked once per round so rewards are comparable).
    assert len(sim.calls) == args.search_budget + 1
    assert all(c["fillers"] == ["F1.dck", "F2.dck"] for c in sim.calls)

    # Display-convention win rates (wins / ALL games, like the greedy
    # round) and the raw margin from the verdict sim.
    assert rr.win_rate_new == round(20 / 45, 4)  # rounded like the greedy round
    assert rr.margin == 15


def test_search_round_writes_iteration_row_via_existing_machinery(tmp_path):
    """The round must land a knowledge_log row exactly like a curated
    round: audit manifest from the applied proposal (source
    'bandit-search'), verdict filled in by the verdict sim."""
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(tmp_path)

    def arm_builder(deck_path, bracket, source, *, protected=(), max_arms=None):
        return [SearchArm(key="+Good / -OldCard A", add="Good",
                          cut="OldCard A")]

    round_fn = make_search_round_fn(arm_builder=arm_builder,
                                    sim_fn=_scripted_sim())
    res = run_improve_loop(deck, "foo", 1, args, round_fn=round_fn)
    assert res.history[0].iteration_id is not None

    con = sqlite3.connect(args.db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT verdict, audit_manifest, sim_report FROM iterations"
    ).fetchone()
    con.close()
    assert row["verdict"] == "kept"
    manifest = json.loads(row["audit_manifest"])
    assert manifest["source"] == "bandit-search"
    assert manifest["added"] == ["Good"]
    assert manifest["removed"] == ["OldCard A"]
    assert json.loads(row["sim_report"])["wins_b"] == 20


def test_search_round_legality_guard_kills_illegal_swap(tmp_path):
    """A swap the shared apply path refuses (here: add already in the
    deck — singleton rule) must die at PROBE time without burning a
    sim, leave no winners, clean up the probe file, and convert the
    round into a zero-change no-op the loop converges on."""
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(tmp_path)

    def arm_builder(deck_path, bracket, source, *, protected=(), max_arms=None):
        # Sol Ring is already in the fixture deck.
        return [SearchArm(key="+Sol Ring / -OldCard A", add="Sol Ring",
                          cut="OldCard A")]

    sim = _scripted_sim()
    round_fn = make_search_round_fn(arm_builder=arm_builder, sim_fn=sim)
    res = run_improve_loop(deck, "foo", 3, args, round_fn=round_fn)

    assert sim.calls == []  # the doomed probe never reached Forge
    assert res.converged is True  # 0/0 round -> no-op -> stop
    assert res.history[0].verdict == "no-op"
    assert Path(res.final_deck) == deck  # base never advanced
    # The probe .dck was a discarded experiment, not a proposal.
    assert not (tmp_path / "[USER] Foo v2 [B3].dck").exists()


def test_search_round_no_arms_is_noop_round(tmp_path):
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(tmp_path)
    round_fn = make_search_round_fn(
        arm_builder=lambda *a, **k: [], sim_fn=_scripted_sim())
    res = run_improve_loop(deck, "foo", 2, args, round_fn=round_fn)
    assert res.converged is True
    assert res.history[0].verdict == "no-op"


def test_search_round_arm_builder_failure_is_error_round(tmp_path):
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("advisor exploded")

    round_fn = make_search_round_fn(arm_builder=boom, sim_fn=_scripted_sim())
    res = run_improve_loop(deck, "foo", 2, args, round_fn=round_fn)
    assert res.history[0].verdict == "error"
    assert "advisor exploded" in res.history[0].error
    assert res.rounds_run == 1  # error stops the loop, as ever


def test_search_round_protected_cards_reach_arm_builder(tmp_path):
    """--protect + intent wincons must flow into arm building so
    protected cuts are excluded BEFORE any budget is spent on them."""
    from commander_builder.intent import Intent
    deck = _make_dck(tmp_path, "[USER] Foo [B3].dck", _FIXTURE_MAIN)
    args = _search_args(
        tmp_path, protect=["OldCard A"],
        intent=Intent(archetype="aggro", themes=[], key_wincons=["Sol Ring"],
                      color_identity=[]),
    )
    seen = {}

    def arm_builder(deck_path, bracket, source, *, protected=(), max_arms=None):
        seen["protected"] = list(protected)
        return []

    round_fn = make_search_round_fn(arm_builder=arm_builder,
                                    sim_fn=_scripted_sim())
    run_improve_loop(deck, "foo", 1, args, round_fn=round_fn)
    assert "OldCard A" in seen["protected"]
    assert "Sol Ring" in seen["protected"]


# --- CLI wiring: --search-budget --------------------------------------------

def _stub_loop(monkeypatch):
    captured = {}

    def stub(deck_path, deck_id, rounds, args, **kw):
        captured["round_fn"] = kw.get("round_fn")
        captured["args"] = args
        from commander_builder.improve import ImproveResult
        return ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", stub)
    return captured


def test_budget_zero_is_greedy_path_bandit_never_constructed(tmp_path, monkeypatch):
    """--search-budget 0 (the default) must be byte-identical greedy:
    the search factory is never even constructed (spy raises if it is),
    and the loop receives the plain _default_round_fn."""
    captured = _stub_loop(monkeypatch)

    def trap(*a, **k):  # pragma: no cover — the point is it never runs
        raise AssertionError("make_search_round_fn constructed at budget 0")

    monkeypatch.setattr(improve_search, "make_search_round_fn", trap)
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "1"])
    assert rc == 0
    assert captured["round_fn"] is improve._default_round_fn
    assert captured["args"].search_budget == 0


def test_budget_positive_installs_search_round_fn(tmp_path, monkeypatch):
    captured = _stub_loop(monkeypatch)
    sentinel = object()
    made = {}

    def fake_factory(**kw):
        made["called"] = True
        return sentinel

    monkeypatch.setattr(improve_search, "make_search_round_fn", fake_factory)
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")

    rc = improve_main([str(deck), "--rounds", "1", "--search-budget", "6"])
    assert rc == 0
    assert made == {"called": True}
    assert captured["round_fn"] is sentinel
    assert captured["args"].search_budget == 6
    assert captured["args"].search_min_pulls == 2  # documented default


def test_budget_negative_rejected(tmp_path):
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")
    assert improve_main([str(deck), "--rounds", "1",
                         "--search-budget", "-1"]) == 2


def test_budget_incompatible_with_bandit_strategy(tmp_path, capsys):
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "1", "--strategy", "bandit",
                       "--search-budget", "4"])
    assert rc == 2
    assert "incompatible" in capsys.readouterr().out


def test_budget_below_min_pulls_rejected_up_front(tmp_path, capsys):
    """budget < min_pulls means NO arm can ever qualify — every round
    would burn the full Forge budget and apply nothing. Refused before
    any sim time is spent."""
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")
    rc = improve_main([str(deck), "--rounds", "1",
                       "--search-budget", "1", "--search-min-pulls", "2"])
    assert rc == 2
    assert "no-op" in capsys.readouterr().out


def test_min_pulls_below_one_rejected(tmp_path):
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text("[metadata]\nName=Foo\n", encoding="utf-8")
    assert improve_main([str(deck), "--rounds", "1", "--search-budget", "4",
                         "--search-min-pulls", "0"]) == 2


def test_help_carries_honest_cost_note(capsys):
    with pytest.raises(SystemExit):
        improve_main(["--help"])
    out = capsys.readouterr().out
    assert "--search-budget" in out
    # The flag's help must state the real price of a pull.
    assert "COST" in out
