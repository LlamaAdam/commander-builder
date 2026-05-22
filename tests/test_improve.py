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
