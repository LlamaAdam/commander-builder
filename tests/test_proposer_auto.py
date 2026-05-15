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
# Protected-cards filter — pet cards locked against cuts
# ---------------------------------------------------------------------------

def test_auto_propose_strips_protected_cards_from_cuts(tmp_path, monkeypatch):
    """Cards in the protected_cards list MUST be filtered out of
    Claude's cut proposals. They land in dropped_for_protection so
    the iteration log records what Claude tried to cut anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["NewA", "NewB"],
        "cuts": ["Krenko, Mob Boss", "Goblin Lackey", "RandomFiller"],
        "rationale": "trim filler",
    }))

    deck = tmp_path / "[USER] Goblin [B3].dck"
    deck.write_text(
        "[metadata]\nName=Goblin\n[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Goblin Lackey\n1 RandomFiller\n",
        encoding="utf-8",
    )

    # Protected list is passed as a plain list of strings — no parsing
    # required at this layer. The .dck-metadata parse rule (one card
    # per line, commas literal) only applies when reading from disk.
    proposal = auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
        max_adds=5, max_cuts=5,
        protected_cards=["Krenko, Mob Boss", "Goblin Lackey"],
    )

    # Protected cuts stripped from cuts list.
    assert "Krenko, Mob Boss" not in proposal.cuts
    assert "Goblin Lackey" not in proposal.cuts
    # Non-protected cut survives.
    assert "RandomFiller" in proposal.cuts
    # Stripped names surface for log/UI.
    assert "Krenko, Mob Boss" in proposal.dropped_for_protection
    assert "Goblin Lackey" in proposal.dropped_for_protection
    # Adds untouched — protection only applies to cuts.
    assert proposal.adds == ["NewA", "NewB"]


def test_auto_propose_protection_is_case_insensitive(tmp_path, monkeypatch):
    """Casing variations across EDHREC scrape vs .dck format must not
    let a protected card slip through as a cut. 'sol ring' in the
    protect list must catch a 'Sol Ring' cut proposal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": [],
        "cuts": ["Sol Ring", "SOL RING", "sol ring"],
        "rationale": "redundant ramp",
    }))

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    proposal = auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
        protected_cards=["sol ring"],  # lowercase
    )
    assert proposal.cuts == []
    assert len(proposal.dropped_for_protection) == 3


def test_auto_propose_injects_protected_block_into_prompt(
    tmp_path, monkeypatch,
):
    """The curator system prompt tells Claude 'NEVER propose cutting
    a card in the user's PROTECTED CARDS list.' For that to work, the
    list MUST appear in the user message — verify by capturing the
    Anthropic call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )

    captured: dict = {}

    # Custom anthropic stub that records the user message.
    import sys, types as _types
    block = type("_Block", (), {"text": json.dumps({
        "adds": [], "cuts": [], "rationale": "ok",
    })})
    msg = type("_Msg", (), {"content": [block()]})

    class CapturingClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    captured.update(kw)
                    return msg()
            return M()
    fake = _types.ModuleType("anthropic")
    fake.Anthropic = CapturingClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
        protected_cards=["Krenko, Mob Boss", "Goblin Lackey"],
    )

    user_msg = captured["messages"][0]["content"]
    assert "PROTECTED CARDS" in user_msg
    assert "Krenko, Mob Boss" in user_msg
    assert "Goblin Lackey" in user_msg


def test_auto_propose_no_protected_block_when_empty(
    tmp_path, monkeypatch,
):
    """When the protected list is empty, the PROTECTED CARDS block is
    omitted from the prompt entirely — keeps token cost minimal for
    the common case where the user hasn't locked anything."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )

    captured: dict = {}
    import sys, types as _types
    block = type("_Block", (), {"text": json.dumps({
        "adds": [], "cuts": [], "rationale": "ok",
    })})
    msg = type("_Msg", (), {"content": [block()]})

    class CapturingClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    captured.update(kw)
                    return msg()
            return M()
    fake = _types.ModuleType("anthropic")
    fake.Anthropic = CapturingClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    auto_propose(
        deck_path=deck, bracket=3,
        advice_report=_stub_advice_report(),
    )

    user_msg = captured["messages"][0]["content"]
    assert "PROTECTED CARDS" not in user_msg


def test_apply_proposal_strips_protected_from_metadata_as_defense(tmp_path):
    """Defense-in-depth: even if a Proposal is constructed by hand
    with a protected card in cuts (skipping auto_propose's filter),
    apply_proposal_to_deck reads [metadata] Protect= and refuses to
    cut the locked card. The card lands in dropped_for_protection
    so the surface still records what got blocked."""
    src = _make_dck(tmp_path, "[USER] Foo [B3].dck",
                    ["Sol Ring", "Cultivate", "Lightning Bolt"])
    # Inject Protect= entries into the .dck metadata.
    text = src.read_text(encoding="utf-8")
    text = text.replace("Moxfield=abc\n",
                        "Moxfield=abc\nProtect=Sol Ring\n")
    src.write_text(text, encoding="utf-8")

    proposal = Proposal(
        adds=["A", "B"],
        # Sol Ring is locked but the proposal lists it anyway —
        # simulating a stale proposal or hand construction.
        cuts=["Sol Ring", "Cultivate"],
        rationale="x", source="claude-auto",
    )
    out = apply_proposal_to_deck(src, proposal)
    new_text = out.read_text(encoding="utf-8")

    # Sol Ring NOT cut — protected by [metadata].
    assert "1 Sol Ring" in new_text
    # Cultivate cut — not protected.
    assert "1 Cultivate" not in new_text
    # The protection action got recorded on the proposal.
    assert "Sol Ring" in proposal.dropped_for_protection


def test_auto_curate_main_unions_three_protection_sources(
    tmp_path, monkeypatch, capsys,
):
    """[metadata] Protect= entries + --protect CLI flags + --protect-
    from file are unioned. Test all three sources and verify the
    summary mentions the combined count."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Brainstorm"],
        # Claude tries to cut all 3 protected cards.
        "cuts": ["FromMeta", "FromFlag", "FromFile"],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Protected [B3].dck"
    deck.write_text(
        "[metadata]\nName=Protected\nMoxfield=prot-id\n"
        "Protect=FromMeta\n"
        "[Commander]\n1 Test\n"
        "[Main]\n1 FromMeta\n1 FromFlag\n1 FromFile\n1 Filler\n",
        encoding="utf-8",
    )
    protect_file = tmp_path / "protect.txt"
    protect_file.write_text("FromFile\n", encoding="utf-8")
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--protect", "FromFlag",
        "--protect-from", str(protect_file),
    ])
    assert rc == 0

    out = capsys.readouterr().out
    # CLI reports the combined protected count (3 sources).
    assert "3 protected cards locked" in out
    # All 3 cards listed in the dropped-for-protection summary.
    assert "FromMeta" in out
    assert "FromFlag" in out
    assert "FromFile" in out


def test_auto_curate_main_protect_from_missing_file(
    tmp_path, monkeypatch, capsys,
):
    """--protect-from with a non-existent file → exit 2 (invocation
    error) so the user sees the typo before the run starts."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Foo [B3].dck"
    deck.write_text(
        "[metadata]\nName=Foo\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3",
        "--protect-from", str(tmp_path / "does_not_exist.txt"),
    ])
    assert rc == 2
    assert "not found" in capsys.readouterr().out


def test_audit_iteration_log_records_protected_lists(
    tmp_path, monkeypatch, capsys,
):
    """The iteration log's audit_manifest captures both the
    protected_cards list (state at audit time) and
    dropped_for_protection (what Claude tried to cut anyway) so
    post-hoc analysis can answer 'did protection ever block a swap?'"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Brainstorm"],
        "cuts": ["PetCard", "FillerCard"],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Pets [B3].dck"
    deck.write_text(
        "[metadata]\nName=Pets\nMoxfield=pets-id\nProtect=PetCard\n"
        "[Commander]\n1 Test\n[Main]\n1 PetCard\n1 FillerCard\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([str(deck), "--bracket", "3", "--db-path", str(db)])
    assert rc == 0

    from commander_builder.knowledge_log import iterations_for_deck
    it = iterations_for_deck("pets-id", db_path=db)[0]
    assert "PetCard" in it.audit_manifest["dropped_for_protection"]
    # The new iteration's deck snapshot still has PetCard (NOT cut).
    assert "1 PetCard" in it.deck_snapshot


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
    """Happy path with a BALANCED proposal: 2 adds + 2 cuts. All four
    cards land/leave because adds and cuts are pair-matched and the
    balancing step is a no-op.

    The 'unbalanced lists silently drop adds' scenario is covered
    separately in test_apply_proposal_balances_excess_adds_via_min."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "OldCard A", "OldCard B", "Filler"],
    )
    proposal = Proposal(
        adds=["NewCard A", "NewCard B"],
        cuts=["OldCard A", "OldCard B"],
        rationale="trim duds, add finishers", source="claude-auto",
    )
    out_path = apply_proposal_to_deck(src, proposal)

    assert out_path.exists()
    assert out_path != src
    text = out_path.read_text(encoding="utf-8")
    # Both adds landed.
    assert "1 NewCard A" in text
    assert "1 NewCard B" in text
    # Both cuts applied.
    assert "1 OldCard A" not in text
    assert "1 OldCard B" not in text
    # Other untouched cards survive.
    assert "1 Sol Ring" in text
    assert "1 Filler" in text
    # Commander section preserved.
    assert "1 Test Commander" in text
    # Balancing was a no-op (lengths matched).
    assert proposal.dropped_for_balance == []
    assert proposal.applied_adds == ["NewCard A", "NewCard B"]
    assert proposal.applied_cuts == ["OldCard A", "OldCard B"]


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
    cut still finds the deck's '1 Sol Ring' line. Pinned with a
    balanced 1-add/1-cut proposal so the balancing step doesn't drop
    the cut before it gets a chance to match."""
    src = _make_dck(tmp_path, "[USER] Foo [B3].dck", ["Sol Ring", "Filler"])
    proposal = Proposal(
        adds=["Brainstorm"], cuts=["sol ring"], rationale="x",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")
    # The Sol Ring main-section line is gone. The substring "Sol Ring"
    # might survive elsewhere (e.g. metadata Name field), but no
    # quantity-prefixed line remains.
    assert "1 Sol Ring" not in text
    assert "1 Brainstorm" in text
    assert "1 Filler" in text


def test_apply_proposal_handles_edition_codes_in_card_lines(tmp_path):
    """Real .dck lines look like ``1 Sol Ring|CLB|871`` — the cut matcher
    must compare the name portion before the pipe, not the whole line.
    Balanced 2-add/2-cut proposal so both cuts land."""
    p = tmp_path / "[USER] Foo [B3].dck"
    p.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Krenko, Mob Boss|FDN|204\n"
        "[Main]\n1 Sol Ring|CLB|871\n1 Lightning Bolt|PLST|E01-54\n"
        "1 Forest\n",
        encoding="utf-8",
    )
    proposal = Proposal(
        adds=["Brainstorm", "Counterspell"],
        cuts=["Sol Ring", "Lightning Bolt"],
        rationale="x",
    )
    out = apply_proposal_to_deck(p, proposal)
    text = out.read_text(encoding="utf-8")
    # Both cut main-section lines are gone.
    assert "1 Sol Ring|CLB|871" not in text
    assert "1 Lightning Bolt|PLST|E01-54" not in text
    # Both adds landed.
    assert "1 Brainstorm" in text
    assert "1 Counterspell" in text
    # Commander line untouched.
    assert "1 Krenko, Mob Boss|FDN|204" in text


# ---------------------------------------------------------------------------
# Balancing + padding — the legal-deck invariants
# ---------------------------------------------------------------------------

def test_apply_proposal_balances_excess_adds_via_min(tmp_path):
    """If Claude proposes more adds than cuts, the surplus adds get
    sliced off so the resulting deck stays the same size. The dropped
    cards land on Proposal.dropped_for_balance so the iteration log
    records 'Claude wanted X but we dropped it to keep the deck legal'."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["A", "B", "C", "D", "OldOne", "OldTwo"],
    )
    proposal = Proposal(
        adds=["Add1", "Add2", "Add3", "Add4", "Add5"],  # 5 adds
        cuts=["OldOne", "OldTwo"],                       # 2 cuts
        rationale="curated", source="claude-auto",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")

    # Only the first 2 adds landed (min(5, 2) = 2). Order preserved.
    assert "1 Add1" in text
    assert "1 Add2" in text
    assert "1 Add3" not in text
    assert "1 Add4" not in text
    assert "1 Add5" not in text
    # Both cuts applied.
    assert "1 OldOne" not in text
    assert "1 OldTwo" not in text
    # Proposal records what got dropped.
    assert proposal.applied_adds == ["Add1", "Add2"]
    assert proposal.applied_cuts == ["OldOne", "OldTwo"]
    assert set(proposal.dropped_for_balance) == {"Add3", "Add4", "Add5"}


def test_apply_proposal_balances_excess_cuts_via_min(tmp_path):
    """Mirror case: more cuts than adds → surplus cuts sliced off so
    the proposed deck doesn't shrink below its original size."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["A", "B", "C", "D", "E"],
    )
    proposal = Proposal(
        adds=["Add1"],                            # 1 add
        cuts=["A", "B", "C", "D"],                # 4 cuts
        rationale="curated", source="claude-auto",
    )
    out = apply_proposal_to_deck(src, proposal)

    assert proposal.applied_adds == ["Add1"]
    assert proposal.applied_cuts == ["A"]  # only first cut survives min()
    # The surplus cuts surface on dropped_for_balance.
    assert set(proposal.dropped_for_balance) == {"B", "C", "D"}


def test_apply_proposal_pads_short_deck_to_99(tmp_path):
    """Source decks shorter than 99 main get padded with basics
    mirroring the deck's existing color distribution. Without this,
    the proposed deck inherits the deficit and Forge refuses to load
    it. proposal.padded_count records the synthesis so the user sees
    'we added 27 basics' instead of being surprised by the deck size."""
    # Build a sub-99 deck: 5 main cards including 3 Forests.
    src = _make_dck(
        tmp_path, "[USER] Short [B3].dck",
        ["Forest", "Forest", "Forest", "Sol Ring", "Cultivate"],
    )
    proposal = Proposal(
        adds=["Brainstorm"], cuts=["Sol Ring"], rationale="x",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")

    # The deck got padded.
    assert proposal.padded_count > 0
    # Padded with basics matching the deck's existing distribution —
    # Forest is the only basic present, so all padding goes to Forest.
    assert "Forest" in proposal.padded_breakdown
    # Final main count check: count quantity-prefixed [Main] lines.
    import re as _re
    in_main = False
    total = 0
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            in_main = s.lower() == "[main]"
            continue
        if in_main:
            m = _re.match(r"^(\d+)\s+([^|]+)", s)
            if m:
                total += int(m.group(1))
    assert total == 99


def test_apply_proposal_does_not_pad_when_deck_already_legal(tmp_path):
    """If the source deck is at 99 main already, padding is a no-op
    and proposal.padded_count == 0. Pin so a refactor doesn't start
    over-padding legal decks."""
    # Build a 99-card mainboard.
    main_cards = ["Forest"] * 50 + ["Sol Ring"] + ["Mountain"] * 48
    src = _make_dck(tmp_path, "[USER] Legal [B3].dck", main_cards)
    proposal = Proposal(
        adds=["Brainstorm"], cuts=["Sol Ring"], rationale="x",
    )
    apply_proposal_to_deck(src, proposal)
    assert proposal.padded_count == 0
    assert proposal.padded_breakdown == {}


def test_apply_proposal_records_applied_fields_on_dry_run(tmp_path):
    """Dry-run skips the file write but still populates the
    applied_adds / applied_cuts / dropped_for_balance / padded_count
    fields so the CLI summary can preview what WOULD have landed."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "OldA", "OldB"],
    )
    proposal = Proposal(
        adds=["NewA", "NewB", "NewC"],
        cuts=["OldA"],
        rationale="x",
    )
    out_path = apply_proposal_to_deck(src, proposal, dry_run=True)

    assert not out_path.exists()
    # Source still untouched.
    src_text = src.read_text(encoding="utf-8")
    assert "1 NewA" not in src_text
    # But the proposal carries the projected result.
    assert proposal.applied_adds == ["NewA"]  # min(3, 1) = 1
    assert proposal.applied_cuts == ["OldA"]
    assert set(proposal.dropped_for_balance) == {"NewB", "NewC"}


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
    expected name and content. Smoke-tests the full happy path.

    Passes ``--no-log`` so the test focuses on the file-write contract
    without depending on knowledge_log state. The autouse
    ``_isolate_knowledge_log_default_path`` fixture in conftest.py
    would catch a leak even without this flag, but being explicit
    here documents intent.
    """
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
    rc = auto_curate_main([str(deck), "--bracket", "3", "--no-log"])
    assert rc == 0

    out_path = tmp_path / "[USER] Foo v2 [B3].dck"
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    assert "1 Brainstorm" in text
    assert "1 Sol Ring" in text
    assert "Random Filler" not in text


def test_auto_curate_main_summary_surfaces_dropped_for_balance(
    tmp_path, monkeypatch, capsys,
):
    """When Claude proposes more adds than cuts (or vice versa), the
    CLI summary tells the user what got dropped to keep the deck
    legal. Without this surfacing, an unattended overnight run would
    silently produce balanced .dcks with no way to audit what Claude
    wanted vs what landed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    # Claude proposes 3 adds + 1 cut → 2 adds get dropped for balance.
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["KeepMe", "DropA", "DropB"],
        "cuts": ["OldCard"],
        "rationale": "+draw -filler",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Imbalanced [B3].dck"
    deck.write_text(
        "[metadata]\nName=Imbalanced\nMoxfield=imb-id\n"
        "[Commander]\n1 Test\n[Main]\n1 OldCard\n1 Filler\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
    ])
    assert rc == 0

    out = capsys.readouterr().out
    # Headline shows requested → applied counts.
    assert "Adds requested (3) → applied (1)" in out
    assert "Cuts requested (1) → applied (1)" in out
    # The "Dropped to keep deck size legal" block surfaces with the
    # specific cards that didn't land.
    assert "Dropped to keep deck size legal" in out
    assert "DropA" in out
    assert "DropB" in out


def test_auto_curate_main_json_mode_surfaces_applied_fields(
    tmp_path, monkeypatch, capsys,
):
    """JSON output includes applied_adds / applied_cuts / dropped_for_
    balance / padded_count so batch drivers can verify what landed
    in the .dck without re-parsing the file."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A1", "A2", "A3"],
        "cuts": ["OldA"],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] JsonImb [B3].dck"
    deck.write_text(
        "[metadata]\nName=JsonImb\nMoxfield=json-id\n"
        "[Commander]\n1 Test\n[Main]\n1 OldA\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--dry-run", "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    prop = payload["proposal"]
    assert prop["adds"] == ["A1", "A2", "A3"]            # requested
    assert prop["applied_adds"] == ["A1"]                # min(3,1)=1
    assert prop["applied_cuts"] == ["OldA"]
    assert set(prop["dropped_for_balance"]) == {"A2", "A3"}


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
    # Manifest's added/removed reflect what ACTUALLY LANDED (after
    # balancing). With 1 add + 1 cut, both land — applied == requested.
    assert it.audit_manifest["added"] == ["Brainstorm"]
    assert it.audit_manifest["removed"] == ["Random Filler"]
    # requested_* fields preserve Claude's original intent for analysis.
    assert it.audit_manifest["requested_adds"] == ["Brainstorm"]
    assert it.audit_manifest["requested_cuts"] == ["Random Filler"]
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


def test_auto_curate_main_polish_mode_is_default_5_5(
    tmp_path, monkeypatch, capsys,
):
    """No --mode flag → polish preset → max-adds 5, max-cuts 5.
    Backwards-compatibility check for batch drivers that ran without
    --mode before this commit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A1", "A2", "A3", "A4", "A5", "A6", "A7"],  # 7 — over 5
        "cuts": ["C1", "C2", "C3", "C4", "C5", "C6"],         # 6 — over 5
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Polish [B3].dck"
    deck.write_text(
        "[metadata]\nName=Polish\nMoxfield=p-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C1\n1 C2\n1 C3\n1 C4\n1 C5\n1 C6\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--dry-run", "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "polish"
    assert payload["max_adds"] == 5
    assert payload["max_cuts"] == 5
    # Proposal caps at 5 each.
    assert len(payload["proposal"]["adds"]) == 5
    assert len(payload["proposal"]["cuts"]) == 5


def test_auto_curate_main_overhaul_mode_is_15_15(
    tmp_path, monkeypatch, capsys,
):
    """--mode overhaul → max-adds 15, max-cuts 15. Pinned so the
    'deliberate major revision' preset never silently regresses to
    the polish defaults."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    # Claude returns 12/10 — both under the 15-cap so all survive.
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": [f"A{i}" for i in range(12)],
        "cuts": [f"C{i}" for i in range(10)],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Overhaul [B3].dck"
    deck.write_text(
        "[metadata]\nName=Overhaul\nMoxfield=oh-id\n"
        "[Commander]\n1 Test\n[Main]\n"
        + "".join(f"1 C{i}\n" for i in range(10)),
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--mode", "overhaul", "--dry-run", "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "overhaul"
    assert payload["max_adds"] == 15
    assert payload["max_cuts"] == 15
    # Both lists land within the cap.
    assert len(payload["proposal"]["adds"]) == 12
    assert len(payload["proposal"]["cuts"]) == 10


def test_auto_curate_main_free_mode_is_unbounded(
    tmp_path, monkeypatch, capsys,
):
    """--mode free → caps at 999/999 (effectively unbounded). Trust
    Claude to pick the right count for the deck's actual needs."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": [f"A{i}" for i in range(30)],
        "cuts": [f"C{i}" for i in range(20)],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Free [B3].dck"
    deck.write_text(
        "[metadata]\nName=Free\nMoxfield=fr-id\n"
        "[Commander]\n1 Test\n[Main]\n"
        + "".join(f"1 C{i}\n" for i in range(20)),
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--mode", "free", "--dry-run", "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "free"
    assert payload["max_adds"] == 999
    assert payload["max_cuts"] == 999
    # Nothing dropped to cap — Claude's full output survives.
    assert len(payload["proposal"]["adds"]) == 30
    assert len(payload["proposal"]["cuts"]) == 20


def test_auto_curate_main_explicit_cap_overrides_mode_preset(
    tmp_path, monkeypatch, capsys,
):
    """``--mode overhaul --max-adds 3`` → 3, not 15. Explicit cap
    wins over the mode preset so power users can tune precisely."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": [f"A{i}" for i in range(20)],
        "cuts": [f"C{i}" for i in range(20)],
        "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Mixed [B3].dck"
    deck.write_text(
        "[metadata]\nName=Mixed\nMoxfield=mx-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C1\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--mode", "overhaul",
        "--max-adds", "3",   # overrides overhaul's 15
        # max-cuts not passed → falls through to overhaul's 15
        "--dry-run", "--json",
    ])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "overhaul"
    assert payload["max_adds"] == 3   # explicit override
    assert payload["max_cuts"] == 15  # preset


def test_auto_curate_main_summary_mentions_mode(
    tmp_path, monkeypatch, capsys,
):
    """Human-readable summary surfaces the mode + effective caps so
    a user looking at output knows whether they got polish or
    overhaul behavior."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A1"], "cuts": ["C1"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] ModeShow [B3].dck"
    deck.write_text(
        "[metadata]\nName=ModeShow\nMoxfield=ms-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C1\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--mode", "overhaul",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mode='overhaul'" in out
    assert "15 adds" in out
    assert "15 cuts" in out


def test_auto_curate_main_rejects_invalid_mode(tmp_path, capsys):
    """argparse choices=[polish, overhaul, free] rejects anything else
    before the run starts. SystemExit code 2 (argparse-style)."""
    deck = tmp_path / "[USER] Bad [B3].dck"
    deck.write_text(
        "[metadata]\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    from commander_builder.proposer import auto_curate_main
    with pytest.raises(SystemExit) as exc_info:
        auto_curate_main([
            str(deck), "--bracket", "3", "--mode", "BANANA", "--dry-run",
        ])
    assert exc_info.value.code == 2


def test_auto_curate_main_rejects_negative_max(tmp_path, capsys):
    """Negative explicit cap → exit 2 with a clear error. Without
    this guard, a typo (e.g. ``--max-adds -5``) could pass through
    argparse silently and slice the list to 0 with confusing UX."""
    deck = tmp_path / "[USER] Neg [B3].dck"
    deck.write_text(
        "[metadata]\n[Commander]\n1 Test\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--max-adds", "-5", "--dry-run",
    ])
    assert rc == 2
    assert "non-negative" in capsys.readouterr().out


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
