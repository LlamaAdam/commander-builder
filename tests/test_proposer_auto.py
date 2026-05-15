"""Tests for the headless ``commander-iterate --auto-propose`` curator path.

This complements ``tests/test_proposer.py`` (which covers the audit-from-
scratch ``ManualProposer``/``ClaudeProposer``/``OllamaProposer`` trio) with
the *curator* path: take an already-computed ``AdviceReport``, let Claude
pick a small applicable subset, apply it to the .dck file, and log the
iteration.

Why a curator instead of running the existing claude_propose() ?
    The audit-from-scratch path issues a fresh prompt + system message
    every time, costing ~$0.10–$0.50 per call and ~30s of latency. For
    unattended runs (overnight batch refinement of many decks) we already
    have a wide candidate set from the EDHREC advisor — what's missing is
    a tight, bracket-aware curator. The curator path reuses the advisor's
    EDHREC scrape and feeds it to Claude with a small, focused prompt:
    "given these candidates, pick the best N adds and N cuts, justify
    them, and respect the bracket cap." That's a fast cheap call.

All Anthropic SDK calls are mocked — the SDK is treated as a black box.
The game-changers list is also mocked at the proposer-local symbol so we
don't depend on game_changers.py internals or live WotC HTTP.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder.proposer import (
    Proposal,
    apply_proposal_to_deck,
    auto_propose,
    enforce_bracket_caps,
)


# --- Fake Anthropic SDK injection -----------------------------------------

def _patch_anthropic(monkeypatch, payload_text: str):
    """Install a fake ``anthropic`` module whose
    ``Anthropic().messages.create()`` returns one assistant message with
    ``payload_text`` as the only content block."""
    import sys
    import types

    block = type("_Block", (), {"text": payload_text})
    msg = type("_Msg", (), {"content": [block()]})

    class FakeClient:
        def __init__(self, **kw):
            self.calls: list[dict] = []

        @property
        def messages(self):
            outer = self

            class M:
                def create(self, **kw):
                    outer.calls.append(kw)
                    return msg()
            return M()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def _stub_advice_report(adds=None, cuts=None) -> dict:
    """Shape matches ``AdviceReport.to_manifest()`` so the auto-proposer
    can be exercised without spinning up the full advisor pipeline."""
    return {
        "deck_id": "stable-id",
        "bracket": 3,
        "audit_version": "advisor-heuristic",
        "audit_timestamp": "2026-05-14T12:00:00+00:00",
        "added": adds or [
            "Card A", "Card B", "Card C", "Card D", "Card E",
            "Card F", "Card G", "Card H",
        ],
        "removed": cuts or [
            "Cut 1", "Cut 2", "Cut 3", "Cut 4", "Cut 5", "Cut 6", "Cut 7",
        ],
        "rationale": "EDHREC heuristic suggestions",
        "details": {},
    }


# ---------------------------------------------------------------------------
# auto_propose — return shape + guardrails
# ---------------------------------------------------------------------------

def test_proposer_returns_valid_proposal_shape(tmp_path, monkeypatch):
    """``auto_propose()`` returns a ``Proposal`` with adds/cuts/rationale;
    source is tagged ``claude-auto`` so downstream consumers (knowledge log,
    web UI) can distinguish autonomous proposals from manual ones."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Card A", "Card B"],
        "cuts": ["Cut 1"],
        "rationale": "Strengthens removal, trims dead mana rocks.",
    }))

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Atraxa, Praetors' Voice\n"
        "[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    proposal = auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
        max_adds=5, max_cuts=5,
    )

    assert isinstance(proposal, Proposal)
    assert proposal.adds == ["Card A", "Card B"]
    assert proposal.cuts == ["Cut 1"]
    assert proposal.rationale
    assert proposal.source == "claude-auto"
    # Empty bracket cap → nothing dropped.
    assert proposal.dropped_for_bracket == []


def test_auto_propose_respects_max_changes(tmp_path, monkeypatch):
    """Even if Claude overruns the requested change cap, the caller-supplied
    ``max_adds`` / ``max_cuts`` is enforced so a flaky LLM can't blow up
    the deck size."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Card A", "Card B", "Card C", "Card D",
                 "Card E", "Card F", "Card G"],  # 7 — over the cap of 3
        "cuts": ["Cut 1", "Cut 2", "Cut 3", "Cut 4", "Cut 5"],  # 5 over 2
        "rationale": "many ideas",
    }))

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    proposal = auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
        max_adds=3, max_cuts=2,
    )
    assert len(proposal.adds) == 3
    assert len(proposal.cuts) == 2
    # Caps preserve order so the top-N priorities Claude returned land.
    assert proposal.adds == ["Card A", "Card B", "Card C"]
    assert proposal.cuts == ["Cut 1", "Cut 2"]


def test_auto_propose_fails_fast_without_api_key(tmp_path, monkeypatch):
    """Live mode demands ``ANTHROPIC_API_KEY`` — the user should get a
    clear error before any work, not a silent fallback that runs no LLM."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        auto_propose(
            deck_path=deck, bracket=3,
            advice_report=_stub_advice_report(),
        )


def test_auto_propose_tolerates_claude_wrapping_json_in_code_fences(
    tmp_path, monkeypatch,
):
    """Claude sometimes ignores 'no code fences' instructions. The parser
    strips ```json ... ``` wrappers so a chatty model doesn't crash the
    iteration loop."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    fenced = (
        "```json\n"
        + json.dumps({
            "adds": ["Card A"],
            "cuts": ["Cut 1"],
            "rationale": "trim",
        })
        + "\n```"
    )
    _patch_anthropic(monkeypatch, fenced)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    proposal = auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
    )
    assert proposal.adds == ["Card A"]
    assert proposal.cuts == ["Cut 1"]


def test_auto_propose_raises_on_empty_response(tmp_path, monkeypatch):
    """An empty Claude response is a real failure — we don't silently
    apply zero changes when the caller asked for a curated proposal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, "")

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="empty response"):
        auto_propose(
            deck_path=deck, bracket=3,
            advice_report=_stub_advice_report(),
        )


# ---------------------------------------------------------------------------
# enforce_bracket_caps — game-changer filter at low brackets
# ---------------------------------------------------------------------------

def test_enforce_bracket_caps_strips_game_changers_below_b3(monkeypatch):
    """Working agreement: B1/B2 (low-power) can't run game-changers. The
    filter drops them from ``adds`` and reports them so the caller can log
    the omission."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe", "Rhystic Study", "Cyclonic Rift"},
    )
    adds = ["Smothering Tithe", "Rhystic Study", "Sol Ring", "Lightning Greaves"]
    kept, dropped = enforce_bracket_caps(adds, bracket=2)
    assert "Smothering Tithe" not in kept
    assert "Rhystic Study" not in kept
    assert "Sol Ring" in kept
    assert "Lightning Greaves" in kept
    assert set(dropped) == {"Smothering Tithe", "Rhystic Study"}


def test_enforce_bracket_caps_no_op_at_b3_and_above(monkeypatch):
    """B3+ is high-power; game-changers are allowed. Filter passes through."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe"},
    )
    adds = ["Smothering Tithe", "Sol Ring"]
    kept, dropped = enforce_bracket_caps(adds, bracket=3)
    assert kept == adds
    assert dropped == []


def test_enforce_bracket_caps_case_insensitive_match(monkeypatch):
    """Card-name casing varies across EDHREC scrape, Moxfield export,
    and the .dck format. The bracket filter must normalize before
    comparing or a misspelled-case 'smothering tithe' slips through."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe"},
    )
    adds = ["smothering tithe", "SMOTHERING TITHE", "Sol Ring"]
    kept, dropped = enforce_bracket_caps(adds, bracket=1)
    assert kept == ["Sol Ring"]
    assert len(dropped) == 2


def test_auto_propose_b2_proposal_cannot_add_game_changers(tmp_path, monkeypatch):
    """Even if Claude picks a game-changer for a B2 deck, the bracket-cap
    enforcement strips it before the call returns. End-to-end check."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe", "Cyclonic Rift"},
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Smothering Tithe", "Sol Ring", "Cyclonic Rift"],
        "cuts": ["Cut 1"],
        "rationale": "tighten removal + ramp",
    }))

    deck = tmp_path / "[USER] Test [B2].dck"
    deck.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    proposal = auto_propose(
        deck_path=deck, bracket=2,
        advice_report=_stub_advice_report(),
        max_adds=5, max_cuts=5,
    )
    assert "Smothering Tithe" not in proposal.adds
    assert "Cyclonic Rift" not in proposal.adds
    assert "Sol Ring" in proposal.adds
    assert "Smothering Tithe" in proposal.dropped_for_bracket
    assert "Cyclonic Rift" in proposal.dropped_for_bracket


# ---------------------------------------------------------------------------
# apply_proposal_to_deck — .dck file mutation + version bumping
# ---------------------------------------------------------------------------

def _make_dck(tmp_path, name: str, main_cards: list[str]) -> Path:
    body = (
        "[metadata]\nName=Test\nMoxfield=abc\n"
        "[Commander]\n1 Test Commander\n[Main]\n"
    )
    body += "\n".join(f"1 {c}" for c in main_cards) + "\n"
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_apply_proposal_writes_new_dck_with_adds_and_without_cuts(tmp_path):
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "OldCard A", "OldCard B"],
    )
    proposal = Proposal(
        adds=["NewCard A", "NewCard B"], cuts=["OldCard A"],
        rationale="trim duds", source="claude-auto",
    )
    out_path = apply_proposal_to_deck(src, proposal)

    assert out_path.exists()
    assert out_path != src
    text = out_path.read_text(encoding="utf-8")
    # Adds appended.
    assert "1 NewCard A" in text
    assert "1 NewCard B" in text
    # Cut card is gone.
    assert "1 OldCard A" not in text
    # Other untouched cards survive.
    assert "1 Sol Ring" in text
    assert "1 OldCard B" in text
    # Commander section preserved.
    assert "1 Test Commander" in text


def test_apply_proposal_dry_run_does_not_write_or_mutate(tmp_path):
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "OldCard A"],
    )
    original_text = src.read_text(encoding="utf-8")
    proposal = Proposal(
        adds=["NewCard"], cuts=["OldCard A"],
        rationale="x", source="claude-auto",
    )
    out_path = apply_proposal_to_deck(src, proposal, dry_run=True)

    # Dry-run returns the path it WOULD have written, but nothing's on
    # disk and the input file is untouched.
    assert not out_path.exists()
    assert src.read_text(encoding="utf-8") == original_text


def test_apply_proposal_bumps_version_in_filename(tmp_path):
    """Convention: ``[USER] Foo [B3].dck`` → ``[USER] Foo v2 [B3].dck``
    (auto-incremented). Bracket suffix stays last so tooling that filters
    on ``[B3]`` still finds the new file."""
    src = _make_dck(tmp_path, "[USER] Foo [B3].dck", ["Sol Ring"])
    proposal = Proposal(adds=["NewCard"], cuts=[], rationale="x")
    out = apply_proposal_to_deck(src, proposal)
    assert out.name == "[USER] Foo v2 [B3].dck"


def test_apply_proposal_increments_existing_version(tmp_path):
    src = _make_dck(tmp_path, "[USER] Foo v3 [B3].dck", ["Sol Ring"])
    proposal = Proposal(adds=["NewCard"], cuts=[], rationale="x")
    out = apply_proposal_to_deck(src, proposal)
    assert out.name == "[USER] Foo v4 [B3].dck"


def test_apply_proposal_handles_filename_without_bracket_suffix(tmp_path):
    """Some imported decks land without the [B<n>] convention — the
    mutator must still version-bump without crashing. Falls back to
    inserting ' v2' before the .dck extension."""
    src = _make_dck(tmp_path, "MyDeck.dck", ["Sol Ring"])
    proposal = Proposal(adds=["NewCard"], cuts=[], rationale="x")
    out = apply_proposal_to_deck(src, proposal)
    assert out.name == "MyDeck v2.dck"


def test_apply_proposal_case_insensitive_cuts(tmp_path):
    """Card names in .dck files can have different casing from EDHREC
    scrape output. Cuts should match case-insensitively so a 'sol ring'
    cut still finds the deck's '1 Sol Ring' line."""
    src = _make_dck(tmp_path, "[USER] Foo [B3].dck", ["Sol Ring"])
    proposal = Proposal(adds=[], cuts=["sol ring"], rationale="x")
    out = apply_proposal_to_deck(src, proposal)
    assert "1 Sol Ring" not in out.read_text(encoding="utf-8")


def test_apply_proposal_handles_edition_codes_in_card_lines(tmp_path):
    """Real .dck lines look like ``1 Sol Ring|CLB|871`` — the cut matcher
    must compare the name portion before the pipe, not the whole line."""
    p = tmp_path / "[USER] Foo [B3].dck"
    p.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Krenko, Mob Boss|FDN|204\n"
        "[Main]\n1 Sol Ring|CLB|871\n1 Lightning Bolt|PLST|E01-54\n",
        encoding="utf-8",
    )
    proposal = Proposal(
        adds=[], cuts=["Sol Ring", "Lightning Bolt"], rationale="x",
    )
    out = apply_proposal_to_deck(p, proposal)
    text = out.read_text(encoding="utf-8")
    assert "Sol Ring" not in text
    assert "Lightning Bolt" not in text
    # Commander line untouched.
    assert "1 Krenko, Mob Boss|FDN|204" in text


# ---------------------------------------------------------------------------
# auto_curate_main — the commander-auto-curate CLI entry point
# ---------------------------------------------------------------------------

def _patch_advisor(monkeypatch, advice: dict):
    """Replace the advisor's ``advise`` function with one that returns
    a fake AdviceReport-like object whose ``to_manifest()`` returns
    ``advice``. Keeps the CLI smoke test hermetic — no EDHREC HTTP."""
    class _FakeReport:
        def to_manifest(self):
            return advice
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda **kw: _FakeReport(),
    )


def test_auto_curate_main_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    """End-to-end: --dry-run wires advisor → curator → apply_proposal_to_deck
    with dry_run=True. The summary prints, no file lands on disk."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["NewCard"],
        "cuts": ["OldCard"],
        "rationale": "trim",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 OldCard\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3", "--dry-run"])
    assert rc == 0

    # Output file MUST NOT exist.
    expected = tmp_path / "[USER] Foo v2 [B3].dck"
    assert not expected.exists()
    # Source file untouched.
    assert "1 OldCard" in deck.read_text(encoding="utf-8")

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "NewCard" in out
    assert "OldCard" in out


def test_auto_curate_main_json_mode_emits_machine_readable(
    tmp_path, monkeypatch, capsys,
):
    """--json swaps the human summary for a stable JSON payload a batch
    driver can pipe into jq / sqlite. The proposal + dry_run flag round-
    trip through ``json.loads`` cleanly."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["NewCard"], "cuts": ["OldCard"], "rationale": "trim",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 OldCard\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3", "--dry-run", "--json"])
    assert rc == 0

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["proposal"]["adds"] == ["NewCard"]
    assert payload["proposal"]["cuts"] == ["OldCard"]
    assert payload["proposal"]["source"] == "claude-auto"
    assert payload["input_deck"].endswith("[USER] Foo [B3].dck")
    assert payload["output_deck"].endswith("[USER] Foo v2 [B3].dck")


def test_auto_curate_main_writes_versioned_file_without_dry_run(
    tmp_path, monkeypatch, capsys,
):
    """Without --dry-run, the new versioned .dck lands on disk with the
    expected name and content. Smoke-tests the full happy path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Brainstorm"], "cuts": ["Random Filler"],
        "rationale": "+draw -filler",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n"
        "[Main]\n1 Random Filler\n1 Sol Ring\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3"])
    assert rc == 0

    out_path = tmp_path / "[USER] Foo v2 [B3].dck"
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "1 Brainstorm" in text
    assert "1 Sol Ring" in text
    assert "Random Filler" not in text


def test_auto_curate_main_returns_nonzero_on_missing_deck(tmp_path, capsys):
    """A batch driver should treat exit code != 0 as 'skip this deck'.
    Missing file → 2 (argparse-style 'invocation error')."""
    from commander_builder.proposer import auto_curate_main
    bogus = tmp_path / "nope.dck"
    rc = auto_curate_main([str(bogus), "--bracket", "3", "--dry-run"])
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_auto_curate_main_returns_nonzero_on_runtime_error(
    tmp_path, monkeypatch, capsys,
):
    """If auto_propose raises RuntimeError (e.g. missing API key, empty
    Claude response), the CLI exits 3 rather than crashing — lets the
    batch driver log the failure and continue with the next deck."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3"])
    assert rc == 3
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_auto_curate_main_logs_iteration_to_knowledge_log(
    tmp_path, monkeypatch, capsys,
):
    """The default (no --no-log) writes a 'pending' iteration row to
    knowledge_log so the auto-curate output threads into the iteration
    history alongside manual audits."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Brainstorm"], "cuts": ["Random Filler"],
        "rationale": "+draw -filler",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] LogTest [B3].dck"
    deck.write_text(
        "[metadata]\nName=LogTest\nMoxfield=test-public-id\n"
        "[Commander]\n1 Test\n[Main]\n1 Random Filler\n1 Sol Ring\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
    ])
    assert rc == 0

    # Iteration is persisted with the expected shape.
    from commander_builder.knowledge_log import iterations_for_deck
    iterations = iterations_for_deck("test-public-id", db_path=db)
    assert len(iterations) == 1
    it = iterations[0]
    assert it.bracket == 3
    assert it.verdict == "pending"
    assert it.audit_version == "claude-auto"
    assert it.audit_manifest["added"] == ["Brainstorm"]
    assert it.audit_manifest["removed"] == ["Random Filler"]
    assert it.audit_manifest["source"] == "claude-auto"
    assert it.audit_manifest["src_deck"] == "[USER] LogTest [B3].dck"
    # Deck snapshot captured for reproducibility.
    assert "1 Brainstorm" in it.deck_snapshot
    assert "Random Filler" not in it.deck_snapshot
    # First iteration → no parent.
    assert it.parent_id is None

    out = capsys.readouterr().out
    assert "Logged iteration" in out


def test_auto_curate_main_threads_parent_id_to_prior_iteration(
    tmp_path, monkeypatch, capsys,
):
    """When a deck already has prior iterations in knowledge_log, the
    new auto-curate row threads parent_id to the latest one — keeps
    the v1→v2→...→vN chain navigable for the iteration graph view."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["NewCard"], "cuts": ["OldCard"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Chained [B3].dck"
    deck.write_text(
        "[metadata]\nName=Chained\nMoxfield=chained-id\n"
        "[Commander]\n1 Test\n[Main]\n1 OldCard\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    # Seed a prior iteration for this deck so the new one threads to it.
    from commander_builder.knowledge_log import Iteration, record_iteration
    seed_id = record_iteration(
        Iteration(
            deck_id="chained-id", deck_name="Chained", bracket=3,
            audit_version="manual", verdict="kept",
        ),
        db_path=db,
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
    ])
    assert rc == 0

    from commander_builder.knowledge_log import iterations_for_deck
    iterations = iterations_for_deck("chained-id", db_path=db)
    assert len(iterations) == 2
    # The new (second) row's parent_id is the seed's id.
    assert iterations[1].parent_id == seed_id


def test_auto_curate_main_no_log_flag_skips_iteration_write(
    tmp_path, monkeypatch, capsys,
):
    """--no-log opts out. Lets users run ad-hoc curate without
    polluting the persistent history (e.g. exploring a 'what would
    Claude propose at B5' question without saving)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["X"], "cuts": ["Y"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Quiet [B3].dck"
    deck.write_text(
        "[metadata]\nName=Quiet\nMoxfield=quiet-id\n"
        "[Commander]\n1 Test\n[Main]\n1 Y\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--no-log", "--db-path", str(db),
    ])
    assert rc == 0

    from commander_builder.knowledge_log import iterations_for_deck
    assert iterations_for_deck("quiet-id", db_path=db) == []
    assert "skipped knowledge_log per --no-log" in capsys.readouterr().out


def test_auto_curate_main_dry_run_skips_iteration_write(
    tmp_path, monkeypatch, capsys,
):
    """Dry-run mode never writes the .dck → it must also never write
    the iteration log (there's no real deck to thread to). Pinning
    so a refactor doesn't slip in a 'dry-run still logs' regression."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["X"], "cuts": ["Y"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] DryQuiet [B3].dck"
    deck.write_text(
        "[metadata]\nName=DryQuiet\nMoxfield=dry-id\n"
        "[Commander]\n1 Test\n[Main]\n1 Y\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--dry-run", "--db-path", str(db),
    ])
    assert rc == 0

    from commander_builder.knowledge_log import iterations_for_deck
    assert iterations_for_deck("dry-id", db_path=db) == []


def test_auto_curate_main_logfail_is_nonfatal(
    tmp_path, monkeypatch, capsys,
):
    """If knowledge_log write fails for any reason (disk full, schema
    drift, permissions), the .dck stays on disk and the CLI exits 0
    with a WARN line. We never lose the audit work to a logging quirk."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["X"], "cuts": ["Y"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    def boom(it, db_path):
        raise RuntimeError("simulated knowledge_log failure")
    monkeypatch.setattr(
        "commander_builder.knowledge_log.record_iteration", boom,
    )

    deck = tmp_path / "[USER] LogBoom [B3].dck"
    deck.write_text(
        "[metadata]\nName=LogBoom\nMoxfield=boom-id\n"
        "[Commander]\n1 Test\n[Main]\n1 Y\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3"])
    assert rc == 0
    # New .dck still exists.
    assert (tmp_path / "[USER] LogBoom v2 [B3].dck").exists()
    # Warning surfaced.
    assert "knowledge_log write failed" in capsys.readouterr().out


def test_auto_curate_main_rejects_out_of_range_bracket(tmp_path, capsys):
    """Bracket validation lives in the CLI, not the library — the
    library functions accept any int. Out-of-range → exit 2."""
    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "9", "--dry-run"])
    assert rc == 2
    assert "bracket must be 1-5" in capsys.readouterr().out


def test_proposal_to_dict_is_json_safe():
    """Proposal.to_dict() round-trips through dict→json so the iteration
    row can persist it without bespoke serialization."""
    p = Proposal(
        adds=["A", "B"], cuts=["C"], rationale="r",
        source="claude-auto", dropped_for_bracket=["X"],
    )
    blob = json.dumps(p.to_dict())
    parsed = json.loads(blob)
    assert parsed["adds"] == ["A", "B"]
    assert parsed["cuts"] == ["C"]
    assert parsed["rationale"] == "r"
    assert parsed["source"] == "claude-auto"
    assert parsed["dropped_for_bracket"] == ["X"]
