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
# _extract_curator_json — resilient JSON parser for Claude responses
# ---------------------------------------------------------------------------

def test_extract_curator_json_raw_object():
    """Happy path: Claude obeyed the prompt and returned only JSON."""
    from commander_builder.proposer import _extract_curator_json
    raw = '{"adds": ["A"], "cuts": ["B"], "rationale": "swap"}'
    assert _extract_curator_json(raw) == {
        "adds": ["A"], "cuts": ["B"], "rationale": "swap",
    }


def test_extract_curator_json_with_code_fence():
    """Tolerates markdown code-fence wrapping (``` json ... ```)."""
    from commander_builder.proposer import _extract_curator_json
    raw = '```json\n{"adds": [], "cuts": [], "rationale": "x"}\n```'
    assert _extract_curator_json(raw) == {
        "adds": [], "cuts": [], "rationale": "x",
    }


def test_extract_curator_json_with_prose_preamble():
    """Regression for the 2026-05-15 live smoke: Claude led with
    'Looking at this deck...' prose before emitting JSON. Parser
    must extract the embedded JSON block."""
    from commander_builder.proposer import _extract_curator_json
    raw = (
        "Looking at this Bracket 4 Krenko deck, I need to assess if it "
        "genuinely needs changes. Let me analyze:\n\n"
        "**Deck Assessment:**\n"
        "- Strong core combo package\n"
        "- Some mana rocks could be tighter\n\n"
        '{"adds": ["Mana Crypt"], "cuts": ["Coalition Relic"], '
        '"rationale": "upgrade slow ramp to fast"}'
    )
    parsed = _extract_curator_json(raw)
    assert parsed is not None
    assert parsed["adds"] == ["Mana Crypt"]
    assert parsed["cuts"] == ["Coalition Relic"]


def test_extract_curator_json_with_trailing_prose():
    """Sometimes Claude follows JSON with explanation. Extract the
    first balanced JSON object and ignore the rest."""
    from commander_builder.proposer import _extract_curator_json
    raw = (
        '{"adds": ["A"], "cuts": ["B"], "rationale": "x"}\n\n'
        "Hope this helps! Let me know if you want a different angle."
    )
    parsed = _extract_curator_json(raw)
    assert parsed is not None
    assert parsed["adds"] == ["A"]


def test_extract_curator_json_handles_braces_inside_strings():
    """The rationale string can legally contain ``{`` or ``}`` (e.g.
    mana cost notation). Brace counter must respect string context
    so a ``{R}`` inside rationale doesn't confuse the parser."""
    from commander_builder.proposer import _extract_curator_json
    raw = (
        'Here is the JSON:\n'
        '{"adds": ["Lightning Bolt"], "cuts": [], '
        '"rationale": "Add cheap removal at the {R} slot"}'
    )
    parsed = _extract_curator_json(raw)
    assert parsed is not None
    assert "{R}" in parsed["rationale"]


def test_extract_curator_json_handles_escaped_quotes_in_strings():
    """JSON strings can contain escaped double-quotes ``\\"``. The
    brace counter's string-context detection must respect escapes
    or it'll prematurely exit string mode."""
    from commander_builder.proposer import _extract_curator_json
    raw = '{"adds": [], "cuts": [], "rationale": "Quoted \\"text\\" here"}'
    parsed = _extract_curator_json(raw)
    assert parsed is not None
    assert 'Quoted "text" here' == parsed["rationale"]


def test_extract_curator_json_returns_none_when_no_object_found():
    """Pure prose with no JSON → None so the caller can surface a
    diagnostic error instead of silently dropping changes."""
    from commander_builder.proposer import _extract_curator_json
    assert _extract_curator_json("No JSON anywhere in this response.") is None


def test_extract_curator_json_skips_unparseable_block_and_tries_next():
    """If the first ``{...}`` block in the text isn't valid JSON
    (e.g. prose like 'use the form {X} to denote...'), the parser
    skips it and tries the next ``{`` until it finds a valid object."""
    from commander_builder.proposer import _extract_curator_json
    raw = (
        "Note: the syntax {X} means mana cost X.\n"
        "Here's the actual proposal:\n"
        '{"adds": ["A"], "cuts": ["B"], "rationale": "swap"}'
    )
    parsed = _extract_curator_json(raw)
    assert parsed is not None
    assert parsed["adds"] == ["A"]


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


def test_auto_propose_fails_fast_without_api_key_or_cli(tmp_path, monkeypatch):
    """With NEITHER ``ANTHROPIC_API_KEY`` nor the subscription ``claude`` CLI
    available, the user should get a clear error before any work -- not a
    silent fallback that runs no LLM.

    Note: when the ``claude`` CLI IS on PATH, auto_propose now routes the
    curator through it under the Max subscription (no API key required); see
    ``_curator_complete_via_cli``. This test pins the no-auth-at-all path.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Make the subscription CLI unavailable too, so neither auth path exists.
    monkeypatch.setattr(
        "commander_builder.proposer._claude_cli_available", lambda: False,
    )
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


def test_auto_propose_prompt_says_caps_are_ceilings_not_targets(
    tmp_path, monkeypatch,
):
    """The system prompt MUST tell Claude that max_adds / max_cuts are
    ceilings, not targets. Without this rule, Claude tends to fill the
    cap (5 of 5, 15 of 15) regardless of the deck's actual needs,
    producing bloated proposals that dilute the high-confidence picks.

    Also pinned: the user message reiterates the rule at the bottom
    (closest to where Claude generates its response — repetition
    reinforces) and the system prompt explicitly permits empty lists
    as a valid response when the deck needs no changes."""
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

    # System prompt rule.
    system = captured["system"]
    assert "CEILINGS, NOT TARGETS" in system or "ceilings, not targets" in system.lower()
    assert "empty lists" in system.lower() or "zero" in system.lower()

    # User message reiterates at the bottom.
    user_msg = captured["messages"][0]["content"]
    assert "CEILING" in user_msg or "ceiling" in user_msg.lower()


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
# enforce_color_identity -- off-color rejection for the curator's adds
# ---------------------------------------------------------------------------

def _patch_scryfall_lookup(monkeypatch, ci_map: dict):
    """Install a fake scryfall_client.lookup_card whose ``color_identity``
    field comes from ``ci_map`` (lowercase name -> list of WUBRG letters).

    Cards not in the map return None (matches Scryfall's 404 behavior
    for unknown card names)."""
    def _fake_lookup(name, cache=True):
        key = (name or "").lower()
        if key not in ci_map:
            return None
        return {
            "name": name,
            "color_identity": ci_map[key],
            "type_line": "Creature",
        }
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _fake_lookup,
    )


def test_enforce_color_identity_passes_in_color_cards(monkeypatch):
    """Mono-red deck adding red cards: all pass through."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "lightning bolt": ["R"],
        "shock": ["R"],
        "monastery swiftspear": ["R"],
    })
    kept, dropped = enforce_color_identity(
        ["Lightning Bolt", "Shock", "Monastery Swiftspear"],
        deck_color_identity="R",
    )
    assert kept == ["Lightning Bolt", "Shock", "Monastery Swiftspear"]
    assert dropped == []


def test_enforce_color_identity_rejects_off_color_in_mono(monkeypatch):
    """Regression target: mono-red deck + green creature -- the
    green creature gets stripped. Without this, Claude's occasional
    hallucination of Llanowar Elves into a Krenko deck would make
    the .dck illegal."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "lightning bolt": ["R"],
        "llanowar elves": ["G"],
        "counterspell": ["U"],
    })
    kept, dropped = enforce_color_identity(
        ["Lightning Bolt", "Llanowar Elves", "Counterspell"],
        deck_color_identity="R",
    )
    assert kept == ["Lightning Bolt"]
    assert set(dropped) == {"Llanowar Elves", "Counterspell"}


def test_enforce_color_identity_handles_colorless_cards(monkeypatch):
    """Colorless cards (Sol Ring, artifacts) have CI=[] which is a
    subset of any deck CI -- legal in mono-red, in mono-anything,
    even in colorless decks."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "sol ring": [],
        "wastes": [],
        "ornithopter": [],
    })
    kept, _ = enforce_color_identity(
        ["Sol Ring", "Wastes", "Ornithopter"],
        deck_color_identity="R",
    )
    assert kept == ["Sol Ring", "Wastes", "Ornithopter"]


def test_enforce_color_identity_dual_color_accepts_both(monkeypatch):
    """Izzet (UR) accepts blue, red, multi-blue-red, AND colorless.
    Green gets rejected."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "counterspell": ["U"],
        "lightning bolt": ["R"],
        "expressive iteration": ["U", "R"],
        "sol ring": [],
        "llanowar elves": ["G"],
    })
    kept, dropped = enforce_color_identity(
        ["Counterspell", "Lightning Bolt", "Expressive Iteration",
         "Sol Ring", "Llanowar Elves"],
        deck_color_identity="UR",
    )
    assert kept == ["Counterspell", "Lightning Bolt",
                    "Expressive Iteration", "Sol Ring"]
    assert dropped == ["Llanowar Elves"]


def test_enforce_color_identity_atraxa_wubg_rejects_red(monkeypatch):
    """Atraxa (WUBG, four-color, no red) accepts any subset of those
    four. A red card gets rejected."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "esper sentinel": ["W"],
        "rhystic study": ["U"],
        "necropotence": ["B"],
        "rampant growth": ["G"],
        "purphoros, god of the forge": ["R"],
        "esper charm": ["W", "U", "B"],
    })
    kept, dropped = enforce_color_identity(
        ["Esper Sentinel", "Rhystic Study", "Necropotence",
         "Rampant Growth", "Purphoros, God of the Forge", "Esper Charm"],
        deck_color_identity="WUBG",
    )
    assert "Purphoros, God of the Forge" in dropped
    assert "Esper Charm" in kept  # WUB is subset of WUBG


def test_enforce_color_identity_colorless_deck_strict(monkeypatch):
    """Colorless commander (Karn, Kozilek) accepts ONLY colorless
    cards. Even Lightning Bolt is illegal."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "sol ring": [],
        "lightning bolt": ["R"],
        "ornithopter": [],
    })
    kept, dropped = enforce_color_identity(
        ["Sol Ring", "Lightning Bolt", "Ornithopter"],
        deck_color_identity="",
    )
    assert "Sol Ring" in kept
    assert "Ornithopter" in kept
    assert "Lightning Bolt" in dropped


def test_enforce_color_identity_hybrid_mana_requires_all_colors(monkeypatch):
    """Hybrid mana cards (Lightning Helix = {R/W}) have BOTH colors
    in CI per Scryfall. Mono-white deck can't run Lightning Helix
    because R is not in the deck's CI."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "lightning helix": ["R", "W"],
        "dryad militant": ["W", "G"],
    })
    kept, dropped = enforce_color_identity(
        ["Lightning Helix", "Dryad Militant"],
        deck_color_identity="W",
    )
    # Both illegal in mono-W: Helix needs R too; Dryad needs G too.
    assert kept == []
    assert "Lightning Helix" in dropped
    assert "Dryad Militant" in dropped


def test_enforce_color_identity_unknown_card_kept(monkeypatch):
    """Cards Scryfall returns None for (typo / custom card / Claude
    hallucination) are kept rather than rejected. The name_known
    flag elsewhere catches hallucinations; the color filter doesn't
    need to double-duty."""
    from commander_builder.proposer import enforce_color_identity
    _patch_scryfall_lookup(monkeypatch, {
        "lightning bolt": ["R"],
    })
    kept, dropped = enforce_color_identity(
        ["Lightning Bolt", "Made Up Card 9000"],
        deck_color_identity="R",
    )
    assert kept == ["Lightning Bolt", "Made Up Card 9000"]
    assert dropped == []


def test_enforce_color_identity_scryfall_failure_degrades_gracefully(
    monkeypatch,
):
    """If lookup_card raises (network blip), treat the card as
    in-color rather than stripping the whole proposal. Better noisy
    than empty."""
    from commander_builder.proposer import enforce_color_identity

    def _boom(name, cache=True):
        raise ConnectionError("Scryfall unreachable")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _boom,
    )
    kept, _ = enforce_color_identity(
        ["Lightning Bolt", "Llanowar Elves"], deck_color_identity="R",
    )
    assert kept == ["Lightning Bolt", "Llanowar Elves"]


def test_enforce_color_identity_empty_adds_returns_empty():
    """Defensive: empty input -> empty output, no Scryfall calls."""
    from commander_builder.proposer import enforce_color_identity
    kept, dropped = enforce_color_identity([], "WUBRG")
    assert kept == []
    assert dropped == []


def test_auto_propose_strips_off_color_adds_end_to_end(tmp_path, monkeypatch):
    """End-to-end: Claude returns mixed in-color + off-color adds for
    a mono-red Goblin deck. After auto_propose:
      - Off-color cards land in proposal.dropped_for_color_identity
      - In-color + colorless adds are kept
    Regression net for the whole feature."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Lightning Bolt", "Llanowar Elves", "Counterspell",
                 "Sol Ring"],
        "cuts": ["OldCard"],
        "rationale": "mixed",
    }))
    _patch_scryfall_lookup(monkeypatch, {
        "krenko, mob boss": ["R"],   # commander -> mono-red deck CI
        "lightning bolt": ["R"],
        "llanowar elves": ["G"],
        "counterspell": ["U"],
        "sol ring": [],
    })

    deck = tmp_path / "[USER] Krenko [B4].dck"
    deck.write_text(
        "[metadata]\nName=Krenko\n[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 OldCard\n",
        encoding="utf-8",
    )

    from commander_builder.proposer import auto_propose
    proposal = auto_propose(
        deck_path=deck, bracket=4,
        advice_report=_stub_advice_report(),
        max_adds=5, max_cuts=5,
    )

    assert "Llanowar Elves" not in proposal.adds
    assert "Counterspell" not in proposal.adds
    assert set(proposal.dropped_for_color_identity) == {
        "Llanowar Elves", "Counterspell",
    }
    assert "Lightning Bolt" in proposal.adds
    assert "Sol Ring" in proposal.adds


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


# ---------------------------------------------------------------------------
# enforce_bracket_caps — WotC 3-card cap at B3/B4 (2026-05-16 follow-up)
# ---------------------------------------------------------------------------

def test_enforce_bracket_caps_b3_caps_game_changers_at_three(monkeypatch):
    """B3/B4 (Upgraded/Optimized) allow at most 3 game-changers per
    deck. When the current deck has zero, the curator can add up to 3."""
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: {"Smothering Tithe", "Rhystic Study", "Cyclonic Rift",
                 "Mana Drain", "Mana Crypt"},
    )
    adds = [
        "Smothering Tithe", "Rhystic Study", "Cyclonic Rift", "Mana Drain",
        "Sol Ring",  # not a game-changer
    ]
    kept, dropped = enforce_bracket_caps(
        adds, bracket=3, current_game_changer_count=0,
    )
    # First 3 game-changer adds kept, 4th dropped. Sol Ring passes through.
    assert kept == ["Smothering Tithe", "Rhystic Study", "Cyclonic Rift",
                    "Sol Ring"]
    assert dropped == ["Mana Drain"]


def test_enforce_bracket_caps_b3_respects_existing_game_changers(monkeypatch):
    """When the deck already has 2 game-changers, only 1 more is
    allowed under the 3-card cap. Additional GCs get dropped."""
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: {"Mana Drain", "Cyclonic Rift", "Smothering Tithe"},
    )
    adds = ["Mana Drain", "Cyclonic Rift", "Smothering Tithe"]
    kept, dropped = enforce_bracket_caps(
        adds, bracket=4, current_game_changer_count=2,
    )
    # Only 1 slot remains (3 - 2 = 1).
    assert kept == ["Mana Drain"]
    assert dropped == ["Cyclonic Rift", "Smothering Tithe"]


def test_enforce_bracket_caps_b3_full_cap_drops_all_game_changers(monkeypatch):
    """When the deck already has 3 game-changers, no more are allowed.
    All curator GC adds get dropped; non-GC adds still pass through."""
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: {"Mana Drain", "Cyclonic Rift"},
    )
    adds = ["Mana Drain", "Sol Ring", "Cyclonic Rift", "Lightning Greaves"]
    kept, dropped = enforce_bracket_caps(
        adds, bracket=3, current_game_changer_count=3,
    )
    assert kept == ["Sol Ring", "Lightning Greaves"]
    assert dropped == ["Mana Drain", "Cyclonic Rift"]


def test_enforce_bracket_caps_b3_over_cap_clamps_to_zero(monkeypatch):
    """Decks that ALREADY violate the 3-card cap (e.g. legacy import
    with 5 game-changers) should still cleanly reject new ones rather
    than crashing on a negative remaining-budget."""
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: {"Mana Crypt"},
    )
    kept, dropped = enforce_bracket_caps(
        ["Mana Crypt"], bracket=3, current_game_changer_count=5,
    )
    assert kept == []
    assert dropped == ["Mana Crypt"]


def test_enforce_bracket_caps_b3_legacy_signature_is_pass_through(monkeypatch):
    """When ``current_game_changer_count`` is None (the legacy default,
    older callers), B3+ behavior stays pass-through. Pinned so the
    add-the-kwarg change doesn't silently break callers that haven't
    been updated."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe"},
    )
    kept, dropped = enforce_bracket_caps(
        ["Smothering Tithe", "Sol Ring"], bracket=3,
    )
    assert kept == ["Smothering Tithe", "Sol Ring"]
    assert dropped == []


def test_enforce_bracket_caps_b5_unbounded(monkeypatch):
    """B5 (cEDH) has no game-changer cap. Pass through regardless of
    current count."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Mana Crypt", "Mana Drain"},
    )
    kept, dropped = enforce_bracket_caps(
        ["Mana Crypt", "Mana Drain"], bracket=5,
        current_game_changer_count=10,
    )
    assert kept == ["Mana Crypt", "Mana Drain"]
    assert dropped == []


def test_count_game_changers_in_deck_quantity_aware(monkeypatch):
    """count_game_changers_in_deck sums quantities so ``2 Smothering
    Tithe`` (legal but unusual) counts as 2 against the cap, matching
    WotC's deck-level audit."""
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: {"Smothering Tithe", "Mana Drain"},
    )
    from commander_builder._proposer_filters import count_game_changers_in_deck
    deck = (
        "[Commander]\n1 Krenko\n"
        "[Main]\n2 Smothering Tithe\n1 Mana Drain\n1 Sol Ring\n"
    )
    assert count_game_changers_in_deck(deck) == 3


def test_count_game_changers_in_deck_zero_when_none_present(monkeypatch):
    """Clean deck → 0; sanity-check the entry point for auto_propose
    so the cap math starts from 0 instead of misreading an empty deck."""
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: {"Smothering Tithe"},
    )
    from commander_builder._proposer_filters import count_game_changers_in_deck
    deck = "[Commander]\n1 Krenko\n[Main]\n1 Sol Ring\n"
    assert count_game_changers_in_deck(deck) == 0


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


def test_apply_proposal_rewrites_name_to_new_stem(tmp_path):
    """Regression: ``_apply_swaps_to_dck`` deliberately preserves the
    [metadata] section, so the v2 deck used to keep the v1 deck's Name=.
    Forge then emitted the SAME ``Ai(N)-<Name>`` token for both sides of
    an A/B sim, and any name-keyed attribution (compare_versions,
    pool_curator) either credited nobody or piled both decks' wins onto
    one side. The output must carry its OWN filename stem as Name= so
    ``log_parser._normalize`` maps results back to the right file."""
    import re

    from commander_builder.log_parser import _normalize

    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck", ["Sol Ring", "OldCard A"],
    )
    proposal = Proposal(
        adds=["NewCard"], cuts=["OldCard A"], rationale="x",
        source="claude-auto",
    )
    out = apply_proposal_to_deck(src, proposal)
    new_text = out.read_text(encoding="utf-8")

    old_name = re.search(
        r"^Name=(.+)$", src.read_text(encoding="utf-8"), re.MULTILINE,
    ).group(1)
    m = re.search(r"^Name=(.+)$", new_text, re.MULTILINE)
    assert m, "v2 deck must carry a Name= line"
    new_name = m.group(1)
    # The old bug: new_name == old_name ('Test'). The fix: the v2 deck is
    # named after its own file, distinct from the source.
    assert new_name != old_name
    assert _normalize(new_name) == _normalize(out.stem) == "Foo v2"
    # Only Name= changes — the rest of the metadata section survives so
    # resolve_deck_id can still read the Moxfield= id from the v2 file.
    assert "Moxfield=abc" in new_text
    assert len(re.findall(r"^Name=", new_text, re.MULTILINE)) == 1


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
# Decklist validation + the never-write-a-non-99-mainboard guard
# (2026-07-19 fix). Cuts that match nothing in [Main] used to be
# silently skipped while their paired adds still landed, writing
# 100-card mainboards Forge rejects (or silently mis-sims).
# ---------------------------------------------------------------------------

def _count_main(text: str) -> int:
    from commander_builder.web._helpers import _count_main_cards
    return _count_main_cards(text)


def test_apply_proposal_drops_unmatched_cut_pair_and_stays_legal(tmp_path):
    """A hallucinated cut (card not in the deck) drops its paired add
    too, is reported under dropped_unmatched_cut, and the written deck
    still lands at exactly 99 mainboard."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "Cultivate", "Brainstorm", "Filler"],
    )
    proposal = Proposal(
        adds=["Lotus Cobra"],
        cuts=["Card That Does Not Exist"],
        rationale="x", source="claude-auto",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")

    # Neither half of the invalid pair landed.
    assert "1 Lotus Cobra" not in text
    assert proposal.applied_adds == []
    assert proposal.applied_cuts == []
    # Reported under exactly one reason — not silently lost, not
    # misfiled under balance.
    assert proposal.dropped_unmatched_cut == [
        {"cut": "Card That Does Not Exist", "add": "Lotus Cobra"},
    ]
    assert proposal.dropped_for_balance == []
    # The written deck is legal: padding topped the 4-card fixture up
    # to exactly 99.
    assert _count_main(text) == 99


def test_apply_proposal_guard_refuses_to_write_non_99_mainboard(tmp_path):
    """Last-resort invariant: if the swap/pad pipeline somehow ends at
    != 99 mainboard (here: an over-sized 100-card source that padding
    can only top UP, never trim), apply_proposal_to_deck raises instead
    of writing a deck Forge would reject."""
    import pytest

    # 100 distinct main cards — swaps preserve size, padding is a
    # no-op above 99, so the output would be a 100-card mainboard.
    main_cards = [f"Card {i}" for i in range(99)] + ["Sol Ring"]
    src = _make_dck(tmp_path, "[USER] Fat [B3].dck", main_cards)
    proposal = Proposal(
        adds=["Lotus Cobra"], cuts=["Sol Ring"], rationale="x",
    )
    with pytest.raises(RuntimeError, match="not 99"):
        apply_proposal_to_deck(src, proposal)
    # And nothing landed on disk.
    assert not (tmp_path / "[USER] Fat v2 [B3].dck").exists()


def test_apply_proposal_guard_fires_on_dry_run_too(tmp_path):
    """Regression (2026-07-20): the ``if dry_run: return`` used to sit
    BEFORE the 99-card guard, so --dry-run previewed "success" on
    exactly the deck a real run refuses to write. Preview and real run
    must agree: dry-run raises the SAME RuntimeError (and, as always,
    writes nothing)."""
    import pytest

    # Same over-sized fixture as the real-run guard test above: 100
    # distinct main cards, which padding can never trim down to 99.
    main_cards = [f"Card {i}" for i in range(99)] + ["Sol Ring"]
    src = _make_dck(tmp_path, "[USER] Fat [B3].dck", main_cards)
    proposal = Proposal(
        adds=["Lotus Cobra"], cuts=["Sol Ring"], rationale="x",
    )
    with pytest.raises(RuntimeError, match="not 99"):
        apply_proposal_to_deck(src, proposal, dry_run=True)
    # Dry-run never touches disk — doubly so when refusing.
    assert not (tmp_path / "[USER] Fat v2 [B3].dck").exists()
    # And the source is untouched.
    assert "1 Sol Ring" in src.read_text(encoding="utf-8")


def test_apply_proposal_duplicate_add_pair_dropped_and_reported(tmp_path):
    """An add for a non-basic already in [Main] would write an illegal
    ``2 <Name>`` line — the pair drops and lands under
    dropped_duplicate_add."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "Cultivate", "Brainstorm"],
    )
    proposal = Proposal(
        adds=["Sol Ring"], cuts=["Cultivate"], rationale="x",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")

    assert "2 Sol Ring" not in text
    assert "1 Sol Ring" in text
    # The cut half didn't apply either — Cultivate survives.
    assert "1 Cultivate" in text
    assert proposal.dropped_duplicate_add == [
        {"cut": "Cultivate", "add": "Sol Ring"},
    ]
    assert _count_main(text) == 99


def test_apply_proposal_commander_add_pair_dropped_and_reported(tmp_path):
    """An add naming the [Commander] card drops with its paired cut
    and lands under dropped_commander_add."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "Cultivate"],
    )
    proposal = Proposal(
        adds=["Test Commander"], cuts=["Cultivate"], rationale="x",
    )
    out = apply_proposal_to_deck(src, proposal)
    text = out.read_text(encoding="utf-8")

    assert "1 Cultivate" in text
    # Commander appears only in the [Commander] section.
    assert text.count("Test Commander") == 1
    assert proposal.dropped_commander_add == [
        {"cut": "Cultivate", "add": "Test Commander"},
    ]
    assert _count_main(text) == 99


def test_apply_proposal_protected_cut_reported_under_single_reason(tmp_path):
    """Regression for the double-report bug: a protected cut stripped
    inside apply_proposal_to_deck used to ALSO land in
    dropped_for_balance (it was 'requested but not applied'). Each
    dropped swap must appear under exactly one reason."""
    src = _make_dck(
        tmp_path, "[USER] Foo [B3].dck",
        ["Sol Ring", "Cultivate", "Brainstorm"],
    )
    text = src.read_text(encoding="utf-8")
    text = text.replace("Moxfield=abc\n", "Moxfield=abc\nProtect=Sol Ring\n")
    src.write_text(text, encoding="utf-8")

    proposal = Proposal(
        adds=["Lotus Cobra", "Tireless Tracker"],
        cuts=["Sol Ring", "Cultivate"],
        rationale="x", source="claude-auto",
    )
    apply_proposal_to_deck(src, proposal)

    # Sol Ring: protection only. Tireless Tracker: balance surplus
    # only (after Sol Ring was stripped, 2 adds vs 1 cut).
    assert proposal.dropped_for_protection == ["Sol Ring"]
    assert "Sol Ring" not in proposal.dropped_for_balance
    assert proposal.dropped_for_balance == ["Tireless Tracker"]
    assert proposal.applied_adds == ["Lotus Cobra"]
    assert proposal.applied_cuts == ["Cultivate"]


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
    assert "Adds requested (3) -> applied (1)" in out
    assert "Cuts requested (1) -> applied (1)" in out
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


def test_auto_curate_main_resolves_relative_deck_path_to_absolute(
    tmp_path, monkeypatch, capsys,
):
    """Regression test for the path-doubling bug discovered 2026-05-15.

    The advisor's ``advise()`` function treats relative paths as
    deck_dir-relative and prepends its internal deck_dir. When the
    user passes a path that already contains the deck_dir prefix
    (the natural way to invoke commander-auto-curate from the repo
    root: ``vendor/forge/userdata/decks/commander/[USER] X [B3].dck``),
    the advisor double-prefixes and reports the deck as not found.

    Fix: ``auto_curate_main`` resolves args.deck_path to absolute
    BEFORE calling advise(). This test pins the fix by simulating a
    relative-path invocation and asserting advise() receives an
    absolute Path.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["NewCard"], "cuts": ["OldCard"], "rationale": "x",
    }))

    # Track what path the advisor saw — if it's still relative, the
    # fix regressed and the advisor would double-prefix.
    captured_path: list[Path] = []

    class _FakeReport:
        def to_manifest(self):
            return _stub_advice_report()

    def _fake_advise(deck_path, bracket, **kw):
        captured_path.append(deck_path)
        return _FakeReport()

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", _fake_advise,
    )

    deck_abs = tmp_path / "[USER] Resolved [B3].dck"
    deck_abs.write_text(
        "[metadata]\nName=Resolved\nMoxfield=res-id\n"
        "[Commander]\n1 Test\n[Main]\n1 OldCard\n",
        encoding="utf-8",
    )
    # Invoke with a RELATIVE path. argparse stores whatever the user
    # typed; the fix happens inside auto_curate_main.
    import os as _os
    orig_cwd = _os.getcwd()
    try:
        _os.chdir(tmp_path)
        from commander_builder.proposer import auto_curate_main
        rc = auto_curate_main([
            "[USER] Resolved [B3].dck",  # relative to cwd
            "--bracket", "3", "--dry-run", "--no-log",
        ])
    finally:
        _os.chdir(orig_cwd)

    assert rc == 0
    # The path advise() saw MUST be absolute. If relative, the advisor
    # would prepend deck_dir and the file would not be found.
    assert len(captured_path) == 1
    assert captured_path[0].is_absolute(), (
        f"advise() got a relative path: {captured_path[0]}. "
        f"auto_curate_main should resolve to absolute first to avoid "
        f"the deck_dir double-prefix bug."
    )


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
    """If auto_propose raises RuntimeError (e.g. no auth available, empty
    Claude response), the CLI exits 3 rather than crashing — lets the
    batch driver log the failure and continue with the next deck.

    Merge note (FP curator-CLI): auto_propose no longer fails on a missing
    API key alone — it falls back to the subscription `claude` CLI. To still
    exercise the RuntimeError path, this test makes BOTH auth modes
    unavailable (no key AND no CLI on PATH); the raised message still names
    ANTHROPIC_API_KEY, so the assertion below holds."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "commander_builder.proposer._claude_cli_available", lambda: False,
    )
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


# ---------------------------------------------------------------------------
# Forge A/B-sim integration (--run-sim)
# ---------------------------------------------------------------------------

def test_verdict_from_ab_kept_when_new_deck_wins():
    """20-12 over 32 decisive games (>= the min-decisive threshold) with
    margin=1 -> kept."""
    from commander_builder.proposer import _verdict_from_ab
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=12, wins_b=20, games=32, status="done")
    assert _verdict_from_ab(ab, margin=1) == "kept"


def test_verdict_from_ab_reverted_when_old_deck_wins():
    """20-12 the other way (wins_a > wins_b) over enough games -> reverted."""
    from commander_builder.proposer import _verdict_from_ab
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=20, wins_b=12, games=32, status="done")
    assert _verdict_from_ab(ab, margin=1) == "reverted"


def test_verdict_from_ab_neutral_within_margin():
    """20-21 over 41 decisive games with margin=2 -> neutral: a genuine
    near-tie at a TRUSTWORTHY sample size (not 'inconclusive')."""
    from commander_builder.proposer import _verdict_from_ab
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=20, wins_b=21, games=41, status="done")
    assert _verdict_from_ab(ab, margin=2) == "neutral"


def test_verdict_from_ab_inconclusive_below_min_decisive():
    """A 1-3 result (decisive=4, below the noise floor) is 'inconclusive',
    NOT 'kept' -- even though the margin would otherwise call it kept. This
    is the gate that stops a low-N coin-flip being recorded as authoritative."""
    from commander_builder.proposer import _verdict_from_ab
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=1, wins_b=3, games=4, status="done")
    assert _verdict_from_ab(ab, margin=1) == "inconclusive"
    # Just under the threshold is still inconclusive ...
    assert _verdict_from_ab(
        ABResult(wins_a=9, wins_b=10, games=19, status="done")) == "inconclusive"
    # ... and exactly at the threshold resolves normally.
    assert _verdict_from_ab(
        ABResult(wins_a=8, wins_b=12, games=20, status="done"), margin=1) == "kept"
    # The threshold is tunable for callers that know N is trustworthy.
    assert _verdict_from_ab(ab, margin=1, min_decisive=1) == "kept"


def test_verdict_from_ab_pending_when_sim_did_not_complete():
    """Status 'skipped' or 'failed' → pending. Doesn't claim a verdict
    we can't actually support."""
    from commander_builder.proposer import _verdict_from_ab
    from commander_builder.forge_runner import ABResult
    skipped = ABResult(wins_a=0, wins_b=0, games=0, status="skipped",
                       error="Forge not installed")
    assert _verdict_from_ab(skipped) == "pending"
    failed = ABResult(wins_a=1, wins_b=0, games=1, status="failed",
                      error="JVM crashed")
    assert _verdict_from_ab(failed) == "pending"


def test_ab_to_iteration_fields_includes_win_rates(tmp_path):
    """Win rates are wins/DECISIVE (2026-07-19 convention: decisive =
    wins_a + wins_b, the same denominator _verdict_from_ab gates on),
    rounded to 4 decimals. Margin is wins_b - wins_a."""
    from commander_builder.proposer import _ab_to_iteration_fields
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=2, wins_b=3, games=5, status="done",
                  avg_turns_a=11.0, avg_turns_b=9.5)
    fields = _ab_to_iteration_fields(ab)
    assert fields["win_rate_old"] == 0.4
    assert fields["win_rate_new"] == 0.6
    assert fields["margin"] == 1
    assert fields["sim_report"]["wins_b"] == 3


def test_ab_to_iteration_fields_excludes_filler_and_draw_games():
    """The old wins/games denominator counted filler-won and unresolved-
    draw games the head-to-head pair can never win, deflating both rates
    vs the other knowledge_log writers. decisive = wins_a + wins_b."""
    from commander_builder.proposer import _ab_to_iteration_fields
    from commander_builder.forge_runner import ABResult
    # 20 games: A won 8, B won 10, and 2 went to fillers/unresolved draws.
    ab = ABResult(wins_a=8, wins_b=10, games=20, status="done")
    fields = _ab_to_iteration_fields(ab)
    assert fields["win_rate_old"] == round(8 / 18, 4)
    assert fields["win_rate_new"] == round(10 / 18, 4)
    assert fields["margin"] == 2


def test_ab_to_iteration_fields_omits_rates_when_zero_games():
    """A skipped sim (games=0) shouldn't write 0.0 win rates to the
    DB -- that would overwrite legitimate columns with misleading
    values. The shape is sim_report only."""
    from commander_builder.proposer import _ab_to_iteration_fields
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=0, wins_b=0, games=0, status="skipped",
                  error="no fillers")
    fields = _ab_to_iteration_fields(ab)
    assert "win_rate_old" not in fields
    assert "win_rate_new" not in fields
    assert "margin" not in fields
    assert fields["sim_report"]["status"] == "skipped"


def test_ab_to_iteration_fields_null_rates_when_no_decisive_games():
    """Sim ran (games > 0) but every game drew or went to a filler:
    decisive == 0 -> win_rate keys omitted (columns stay NULL), while
    margin=0 is still a real observation and is recorded."""
    from commander_builder.proposer import _ab_to_iteration_fields
    from commander_builder.forge_runner import ABResult
    ab = ABResult(wins_a=0, wins_b=0, games=5, status="done")
    fields = _ab_to_iteration_fields(ab)
    assert "win_rate_old" not in fields
    assert "win_rate_new" not in fields
    assert fields["margin"] == 0


def test_pick_filler_decks_skips_user_prefix_and_excludes(tmp_path):
    """Auto-pick rules:
      - Skip [USER] decks (those are the user's own work)
      - Skip the excluded paths (the v_n + v_n+1 being compared)
      - Pick `count` from what remains
      - Deterministic via the seeded rng arg"""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    (tmp_path / "[USER] Mine [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "[USER] Mine v2 [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler A.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler B.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler C.dck").write_text("a", encoding="utf-8")
    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[
            tmp_path / "[USER] Mine [B3].dck",
            tmp_path / "[USER] Mine v2 [B3].dck",
        ],
        count=2,
        rng=_rnd.Random(42),
    )
    assert len(picks) == 2
    assert all(not p.startswith("[USER]") for p in picks)
    assert "[USER] Mine [B3].dck" not in picks
    assert "[USER] Mine v2 [B3].dck" not in picks


def test_pick_filler_decks_prefers_same_bracket(tmp_path):
    """Regression for the 2026-05-15 live-bug: B4 user deck was matched
    against B5 cEDH + B2 casual fillers, producing a noise-dominated
    verdict. With target_bracket=4, the auto-pick MUST prefer B4
    fillers over more distant brackets."""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    # 2 B4 fillers + a B5 cEDH + a B2 casual.
    (tmp_path / "Some B4 Filler [B4].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Another B4 Filler [B4].dck").write_text("a", encoding="utf-8")
    (tmp_path / "cEDH Filler [B5].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Casual Filler [B2].dck").write_text("a", encoding="utf-8")

    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[],
        count=2,
        target_bracket=4,
        rng=_rnd.Random(42),
    )
    # Both picks MUST be the B4 fillers -- never B5 or B2.
    assert len(picks) == 2
    for name in picks:
        assert "[B4]" in name, (
            f"expected only B4 fillers when matching a B4 deck, got {name}"
        )


def test_pick_filler_decks_falls_back_to_adjacent_bracket(tmp_path):
    """When same-bracket pool is too small, fall through to adjacent
    brackets (|delta| = 1) before reaching far-bracket fillers."""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    # Only 1 B4 filler available, but plenty of B3 + B5.
    (tmp_path / "Lone B4 [B4].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B3 One [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B3 Two [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B5 One [B5].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B1 Far [B1].dck").write_text("a", encoding="utf-8")

    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[],
        count=2,
        target_bracket=4,
        rng=_rnd.Random(42),
    )
    assert len(picks) == 2
    # The B4 is first; the second pick is delta=1 (B3 or B5), not
    # delta=3 (B1). B1 should NEVER appear when adjacent decks exist.
    assert "Lone B4 [B4].dck" in picks
    assert "B1 Far [B1].dck" not in picks
    # The second pick is from delta=1 (B3 or B5).
    second = [p for p in picks if p != "Lone B4 [B4].dck"][0]
    assert "[B3]" in second or "[B5]" in second


def test_pick_filler_decks_falls_back_to_distant_bracket_when_needed(
    tmp_path,
):
    """When same-bracket AND adjacent are exhausted, the picker
    accepts further-out brackets rather than returning empty. Better
    a noisy sim than no sim at all -- the verdict is 'pending'
    classified, not silently dropped."""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    # Only B1 and B5 fillers -- B4 user has no nearby pool.
    (tmp_path / "B5 cEDH [B5].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B1 Casual [B1].dck").write_text("a", encoding="utf-8")
    (tmp_path / "B1 Other [B1].dck").write_text("a", encoding="utf-8")

    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[],
        count=2,
        target_bracket=4,
        rng=_rnd.Random(42),
    )
    assert len(picks) == 2  # took whatever was available


def test_pick_filler_decks_handles_unparseable_bracket(tmp_path):
    """Files without a [B<N>] suffix (legacy imports, weird names)
    land in a fallback bucket -- used only when bracket-tagged
    fillers can't fill the count quota."""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    (tmp_path / "Tagged B4 [B4].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Untagged Filler.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Another Untagged.dck").write_text("a", encoding="utf-8")

    # With target_bracket=4 + 1 same-bracket filler available, second
    # pick falls through to the unparseable bucket.
    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[],
        count=2,
        target_bracket=4,
        rng=_rnd.Random(42),
    )
    assert "Tagged B4 [B4].dck" in picks
    assert len(picks) == 2
    second = [p for p in picks if p != "Tagged B4 [B4].dck"][0]
    # Untagged filler used as fallback.
    assert "[B" not in second


def test_pick_filler_decks_no_target_bracket_falls_back_to_alpha(tmp_path):
    """When target_bracket=None, behaves like the original picker:
    sorted candidates, shuffled, first ``count``. Backwards-compat
    for callers that don't care about bracket matching."""
    import random as _rnd
    from commander_builder.proposer import _pick_filler_decks
    (tmp_path / "Filler A [B5].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler B [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler C [B1].dck").write_text("a", encoding="utf-8")

    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[],
        count=2,
        target_bracket=None,
        rng=_rnd.Random(42),
    )
    assert len(picks) == 2  # any 2, no bracket preference


def test_pick_filler_decks_returns_empty_when_too_few(tmp_path):
    """Caller distinguishes 'no opponent pool' from a real pick.
    Returns [] so the sim path can warn + skip cleanly."""
    from commander_builder.proposer import _pick_filler_decks
    (tmp_path / "[USER] Mine [B3].dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler A.dck").write_text("a", encoding="utf-8")
    # Only 1 filler available, requesting 2 -> empty list.
    picks = _pick_filler_decks(
        tmp_path,
        exclude_paths=[tmp_path / "[USER] Mine [B3].dck"],
        count=2,
    )
    assert picks == []


def test_auto_curate_main_run_sim_records_verdict(
    tmp_path, monkeypatch, capsys,
):
    """End-to-end: --run-sim runs the A/B harness (mocked), updates
    the iteration row with verdict + sim_report + win rates."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["Brainstorm"], "cuts": ["OldCard"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    # Mock the A/B harness so we don't actually run Forge in the test
    # suite. Returns a "new wins 3-1" outcome.
    from commander_builder.forge_runner import ABResult

    def fake_ab_sim(deck_a_path, deck_b_path, games=5, **kw):
        # 10-30 over 40 total games, all decisive: same 0.25/0.75 win
        # rates as a 1-3, but the 40 decisive clear both the 20-decisive
        # verdict gate ('kept', not 'inconclusive') and the post-sim
        # low-decisive honesty check (no stderr note).
        return ABResult(
            deck_a=deck_a_path.name, deck_b=deck_b_path.name,
            wins_a=10, wins_b=30, games=40,
            avg_turns_a=12.0, avg_turns_b=10.5,
            status="done",
        )
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation", fake_ab_sim,
    )

    deck = tmp_path / "[USER] SimDeck [B3].dck"
    deck.write_text(
        "[metadata]\nName=SimDeck\nMoxfield=sim-id\n"
        "[Commander]\n1 Test\n[Main]\n1 OldCard\n",
        encoding="utf-8",
    )
    # Provide filler decks so auto-pick succeeds.
    (tmp_path / "Filler A.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler B.dck").write_text("a", encoding="utf-8")
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--run-sim", "--sim-games", "40",
    ])
    assert rc == 0

    # Iteration row updated with sim results.
    from commander_builder.knowledge_log import iterations_for_deck
    its = iterations_for_deck("sim-id", db_path=db)
    assert len(its) == 1
    it = its[0]
    assert it.verdict == "kept"               # new won 30-10
    assert it.win_rate_old == 0.25            # 10/40
    assert it.win_rate_new == 0.75            # 30/40
    assert it.margin == 20                     # 30 - 10
    assert it.sim_report is not None
    assert it.sim_report["wins_b"] == 30

    captured = capsys.readouterr()
    assert "A/B sim" in captured.out
    assert "verdict: kept" in captured.out
    # UNITS: --sim-games is TOTAL pod games; 40 total ~= 20 expected
    # decisive (the 2 filler seats win ~half) meets MIN_DECISIVE_GAMES_
    # FOR_VERDICT -> no sub-threshold warning on stderr. (20 total, the
    # pre-2026-07-20 value here, would now warn: ~10 expected decisive.)
    assert "WARNING" not in captured.err
    # ... and the mocked run's 40 actual decisive also clears the
    # post-sim low-decisive honesty note.
    assert "decisive of" not in captured.err


def test_auto_curate_main_run_sim_skipped_when_no_fillers(
    tmp_path, monkeypatch, capsys,
):
    """If the deck_dir has fewer than 2 non-[USER] files, the sim is
    skipped with a clear message + the iteration row gets verdict=
    'pending' explicitly (not silently left). User isn't surprised
    by their nightly batch producing 'pending' rows forever."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A"], "cuts": ["C"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    # No filler decks in deck_dir -> auto-pick returns [].
    deck = tmp_path / "[USER] LoneDeck [B3].dck"
    deck.write_text(
        "[metadata]\nName=Lone\nMoxfield=lone-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db), "--run-sim",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Need 2+ filler decks" in out or "Sim skipped" in out

    from commander_builder.knowledge_log import iterations_for_deck
    its = iterations_for_deck("lone-id", db_path=db)
    assert len(its) == 1
    assert its[0].verdict == "pending"


def test_auto_curate_main_run_sim_ignored_under_dry_run(
    tmp_path, monkeypatch, capsys,
):
    """--run-sim --dry-run is contradictory: dry-run skips the file
    write so there's no v_n+1 deck to compare. Print a clear nudge
    and skip the sim, don't crash."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A"], "cuts": ["C"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    deck = tmp_path / "[USER] Dry [B3].dck"
    deck.write_text(
        "[metadata]\nName=Dry\nMoxfield=dry-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C\n",
        encoding="utf-8",
    )
    db = tmp_path / "knowledge_log.sqlite"
    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--dry-run", "--run-sim",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--run-sim ignored under --dry-run" in out


def test_auto_curate_main_json_mode_surfaces_sim_block(
    tmp_path, monkeypatch, capsys,
):
    """--json output gains sim_run / sim_verdict / sim_report fields
    so batch drivers can read the verdict programmatically."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A"], "cuts": ["C"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    from commander_builder.forge_runner import ABResult

    def fake_ab_sim(deck_a_path, deck_b_path, games=5, **kw):
        return ABResult(
            deck_a=deck_a_path.name, deck_b=deck_b_path.name,
            wins_a=2, wins_b=3, games=5, status="done",
        )
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation", fake_ab_sim,
    )

    deck = tmp_path / "[USER] JsonSim [B3].dck"
    deck.write_text(
        "[metadata]\nName=Js\nMoxfield=js-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C\n",
        encoding="utf-8",
    )
    (tmp_path / "Filler A.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler B.dck").write_text("a", encoding="utf-8")
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--run-sim", "--sim-games", "5", "--sim-margin", "2", "--json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # Stdout must stay pure JSON: the sub-threshold warning goes to
    # stderr precisely so batch drivers (and commander-improve's
    # stdout capture) can keep parsing this blob.
    payload = json.loads(captured.out)
    assert payload["sim_run"] is True
    # 3-2 over only 5 decisive games is below the min-decisive threshold ->
    # 'inconclusive' (the low-N gate fires before the margin check).
    assert payload["sim_verdict"] == "inconclusive"
    assert payload["sim_report"]["wins_b"] == 3
    assert payload["sim_error"] is None
    # ... and the operator was told, loudly, why the verdict can't
    # resolve at 5 total games (the pre-sim warning speaks in decisive
    # units: 5 total ~= 2 expected decisive, gate needs 20 ~= 40+ total).
    assert "WARNING" in captured.err
    assert "inconclusive" in captured.err
    assert "decisive" in captured.err
    assert "40" in captured.err
    # ... and the post-sim honesty note reports the MEASURED decisive
    # count (all 5 mocked games were decisive) plus the total budget a
    # verdict actually needs.
    assert "got 5 decisive of 5 total games" in captured.err
    assert "40+ total games" in captured.err


def test_auto_curate_main_post_sim_reports_actual_decisive_shortfall(
    tmp_path, monkeypatch, capsys,
):
    """The pre-sim warning is an EXPECTATION (total * 0.5); a run whose
    total cleared the 40-game floor can still come up short on decisive
    games when the filler seats run hot. The post-sim honesty note must
    then report the MEASURED decisive count and the suggested total --
    otherwise the operator sees a 40-game run land 'inconclusive' with
    no explanation and no warning ever fired."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(monkeypatch, json.dumps({
        "adds": ["A"], "cuts": ["C"], "rationale": "x",
    }))
    _patch_advisor(monkeypatch, _stub_advice_report())

    from commander_builder.forge_runner import ABResult

    def fake_ab_sim(deck_a_path, deck_b_path, games=5, **kw):
        # Unlucky pod: fillers took 29 of 40 games -- only 4+7=11
        # decisive, below the 20-decisive gate despite the healthy total.
        return ABResult(
            deck_a=deck_a_path.name, deck_b=deck_b_path.name,
            wins_a=4, wins_b=7, games=40, status="done",
        )
    monkeypatch.setattr(
        "commander_builder.forge_runner.run_ab_simulation", fake_ab_sim,
    )

    deck = tmp_path / "[USER] Shortfall [B3].dck"
    deck.write_text(
        "[metadata]\nName=Sf\nMoxfield=sf-id\n"
        "[Commander]\n1 Test\n[Main]\n1 C\n",
        encoding="utf-8",
    )
    (tmp_path / "Filler A.dck").write_text("a", encoding="utf-8")
    (tmp_path / "Filler B.dck").write_text("a", encoding="utf-8")
    db = tmp_path / "knowledge_log.sqlite"

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--db-path", str(db),
        "--run-sim", "--sim-games", "40",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    # 40 total games clears the expected-decisive pre-sim check ...
    assert "WARNING" not in captured.err
    # ... but the measured outcome fell short, and the note says by
    # exactly how much and what to budget instead.
    assert "got 11 decisive of 40 total games" in captured.err
    assert "40+ total games" in captured.err

    from commander_builder.knowledge_log import iterations_for_deck
    its = iterations_for_deck("sf-id", db_path=db)
    assert len(its) == 1
    assert its[0].verdict == "inconclusive"


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


# --- subscription-CLI curator adapter (_curator_complete_via_cli) ----------
# These mock subprocess.run + shutil.which -- no real `claude` call is made.
# They guard the adapter that lets curation run under a Max subscription with
# no ANTHROPIC_API_KEY (the path that unblocked the FP-002 data generation).

import subprocess as _subprocess
import shutil as _shutil

from commander_builder.proposer import _curator_complete_via_cli


def _fake_completed(returncode, stdout, stderr=""):
    return type("CP", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def test_curator_cli_sends_prompt_via_stdin_and_scrubs_api_key(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\claude.CMD")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["input"] = kw.get("input")
        captured["env"] = kw.get("env")
        return _fake_completed(0, json.dumps({"is_error": False, "result": '{"adds": []}'}))

    monkeypatch.setattr(_subprocess, "run", fake_run)
    out = _curator_complete_via_cli(system="SYS", user_msg="USER")

    assert out == '{"adds": []}'
    # Prompt goes on stdin, NOT as an argv element (Windows cmdline-length fix).
    assert captured["input"] == "SYS\n\n---\n\nUSER"
    assert all("USER" not in str(a) for a in captured["cmd"])
    # API-key env vars are scrubbed so the CLI uses subscription auth.
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]


def test_curator_cli_retries_once_then_succeeds(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\claude.CMD")
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)  # skip the backoff
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_completed(1, "", "transient blip")
        return _fake_completed(0, json.dumps({"result": "OK-after-retry"}))

    monkeypatch.setattr(_subprocess, "run", fake_run)
    out = _curator_complete_via_cli(system="S", user_msg="U")
    assert out == "OK-after-retry"
    assert calls["n"] == 2


def test_curator_cli_raises_after_retry_exhausted(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\claude.CMD")
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(_subprocess, "run",
                        lambda cmd, **kw: _fake_completed(1, "", "still failing"))
    with pytest.raises(RuntimeError, match="after retry"):
        _curator_complete_via_cli(system="S", user_msg="U")


def test_curator_cli_is_error_envelope_raises(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\claude.CMD")
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(_subprocess, "run",
                        lambda cmd, **kw: _fake_completed(
                            0, json.dumps({"is_error": True, "result": "rate limit"})))
    with pytest.raises(RuntimeError):
        _curator_complete_via_cli(system="S", user_msg="U")


def test_curator_cli_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not found"):
        _curator_complete_via_cli(system="S", user_msg="U")


# ---------------------------------------------------------------------------
# Batch mode (AGENT_BACKLOG #011) — auto-curate-main --batch <glob>
# ---------------------------------------------------------------------------
#
# Per-deck pipeline behavior is exercised exhaustively by the
# test_auto_curate_main_* family above. These tests focus on the
# batch dispatcher specifically:
#   - glob resolution to .dck files only
#   - JSON-only output stream (NDJSON) plus a final batch_summary record
#   - resume-skip when a versioned sibling already exists, unless --force
#   - per-deck failure isolation (one bad deck shouldn't kill the batch)
#   - argparse validation: deck_path XOR --batch (not both, not neither)
#
# Auto-marked slow by the test_auto_curate_main_ name prefix in
# conftest.py — these go through argparse + the full curator path.


def _setup_batch_env(tmp_path, monkeypatch, *, anthropic_payload=None):
    """Standard scaffolding for batch tests: fake Anthropic key + SDK,
    no-op game-changers loader, stubbed advisor. Returns nothing —
    callers create their own decks afterwards."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setattr(
        "commander_builder.proposer._load_game_changers",
        lambda: set(),
    )
    _patch_anthropic(
        monkeypatch,
        anthropic_payload or json.dumps({
            "adds": ["NewCard"],
            "cuts": ["OldCard"],
            "rationale": "trim",
        }),
    )
    _patch_advisor(monkeypatch, _stub_advice_report())


def _write_minimal_deck(path: Path, name: str = "Foo") -> None:
    """Write the smallest deck shape the auto_curate pipeline accepts."""
    path.write_text(
        f"[metadata]\nName={name}\n[Commander]\n1 Test\n"
        f"[Main]\n1 OldCard\n",
        encoding="utf-8",
    )


def test_auto_curate_main_batch_runs_pipeline_per_deck(
    tmp_path, monkeypatch, capsys,
):
    """Happy path: --batch <glob> resolves to two .dck files, runs the
    pipeline over each, and emits one JSON record per deck plus a
    final batch_summary."""
    _setup_batch_env(tmp_path, monkeypatch)
    a = tmp_path / "[USER] A [B3].dck"
    b = tmp_path / "[USER] B [B3].dck"
    _write_minimal_deck(a, "A")
    _write_minimal_deck(b, "B")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
    ])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    # Two deck records + one batch_summary.
    assert len(lines) == 3
    records = [json.loads(ln) for ln in lines]
    deck_records = [r for r in records if "deck" in r]
    assert len(deck_records) == 2
    assert all(r["status"] == "ok" for r in deck_records)
    summary = records[-1]["batch_summary"]
    assert summary["matched"] == 2
    assert summary["succeeded"] == 2
    assert summary["skipped"] == 0
    assert summary["failed"] == 0


def test_auto_curate_main_batch_skips_already_versioned(
    tmp_path, monkeypatch, capsys,
):
    """Resume-skip: when ``<name> v2 [B3].dck`` exists next to the
    input, the deck is skipped without invoking the pipeline (no
    Anthropic spend on already-curated decks). --force overrides."""
    _setup_batch_env(tmp_path, monkeypatch)
    a = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(a, "A")
    # Pre-existing v2 marks this deck as already curated.
    (tmp_path / "[USER] A v2 [B3].dck").write_text(
        "[Commander]\n1 Test\n[Main]\n1 X\n", encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    # The pre-existing v2 file also matches the glob, but the original
    # is detected as already-versioned and skipped. The v2 deck itself
    # is NOT already-versioned (no v3 exists) so it runs through the
    # pipeline.
    summary = records[-1]["batch_summary"]
    assert summary["matched"] == 2
    assert summary["skipped"] == 1
    deck_records = [r for r in records if "deck" in r]
    skipped = [r for r in deck_records if r["status"] == "skipped"]
    assert len(skipped) == 1
    assert "already-versioned" in skipped[0]["reason"]


def test_auto_curate_main_batch_force_re_curates_versioned(
    tmp_path, monkeypatch, capsys,
):
    """--force bypasses the resume-skip so a deck whose v2 sibling
    already exists gets re-curated. Useful when the prior batch's
    curator output was rejected and the user wants a fresh take."""
    _setup_batch_env(tmp_path, monkeypatch)
    a = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(a, "A")
    (tmp_path / "[USER] A v2 [B3].dck").write_text(
        "[Commander]\n1 Test\n[Main]\n1 X\n", encoding="utf-8",
    )

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER] A [B3].dck"),
        "--bracket", "3", "--dry-run", "--no-log", "--force",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    summary = records[-1]["batch_summary"]
    assert summary["skipped"] == 0
    assert summary["succeeded"] == 1


def test_auto_curate_main_batch_isolates_per_deck_failures(
    tmp_path, monkeypatch, capsys,
):
    """One bad deck must not abort the rest of the batch. We force a
    failure on the second deck by passing an out-of-range bracket
    only when the pipeline runs against that specific deck — easier:
    write one good deck + one non-existent path the glob will resolve.

    Actually simpler: make the second deck unreadable by writing an
    empty file (no [Commander] section). The pipeline rejects empty
    decks; we want the batch to record the failure and continue.
    """
    _setup_batch_env(tmp_path, monkeypatch)
    good = tmp_path / "[USER] Good [B3].dck"
    _write_minimal_deck(good, "Good")
    bad = tmp_path / "[USER] Bad [B3].dck"
    bad.write_text("", encoding="utf-8")  # empty → pipeline rejects

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
    ])
    # Mixed outcome (at least one succeeded) returns 0 with the
    # failure captured in the summary.
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    summary = records[-1]["batch_summary"]
    assert summary["matched"] == 2
    assert summary["succeeded"] + summary["failed"] == 2
    assert summary["succeeded"] >= 1  # at least Good went through


def test_auto_curate_main_batch_returns_2_when_glob_matches_nothing(
    tmp_path, monkeypatch, capsys,
):
    """An empty glob is almost certainly a user typo (wrong dir, wrong
    pattern). Return 2 so a batch driver script's `if cmd; then` knows
    to alert."""
    _setup_batch_env(tmp_path, monkeypatch)
    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "nonexistent*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
    ])
    assert rc == 2
    out = capsys.readouterr().out
    summary = json.loads(out)["batch_summary"]
    assert summary["matched"] == 0
    assert "no .dck files matched" in summary["error"]


def test_auto_curate_main_batch_rejects_both_positional_and_batch(
    tmp_path, capsys,
):
    """deck_path and --batch are mutually exclusive. Passing both
    should fail fast with a clear error rather than silently using
    one or the other."""
    deck = tmp_path / "[USER] Foo [B3].dck"
    _write_minimal_deck(deck, "Foo")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        str(deck),
        "--batch", str(tmp_path / "*.dck"),
        "--bracket", "3", "--dry-run",
    ])
    assert rc == 2
    err = capsys.readouterr().out
    assert "deck_path positional OR --batch" in err


def test_auto_curate_main_batch_rejects_neither_positional_nor_batch(
    tmp_path, capsys,
):
    """Without either, the user almost certainly meant to pass
    something. Don't silently exit 0."""
    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main(["--bracket", "3", "--dry-run"])
    assert rc == 2
    err = capsys.readouterr().out
    assert "deck_path positional OR --batch" in err


# ---------------------------------------------------------------------------
# --parallelism N (AGENT_BACKLOG #016 / FP-003) — concurrent Forge sims
# ---------------------------------------------------------------------------
#
# The 2026-05-19 feasibility spike (scripts/experiments/_spike_concurrent_forge.py)
# confirmed two Forge JVMs co-exist in the same install cwd with no
# lock contention. These tests cover the dispatch layer over that
# substrate:
#   - parallelism > 1 produces the same set of records as sequential
#     (just possibly reordered by completion time)
#   - emission order is "as completed" under parallelism, vs "glob
#     order" under sequential — both produce a valid NDJSON stream
#   - parallelism=1 path is bit-for-bit identical to the pre-#016
#     sequential code path (regression safety for users who don't
#     opt in)
#   - the batch_summary record carries the parallelism setting so
#     downstream tooling can sanity-check what was actually used
#
# These tests exercise --dry-run (no Forge subprocesses) — the spike
# script handles the live-JVM validation separately. Keeping the
# functional tests fast keeps them in the auto-marked-slow batch
# without ballooning the suite runtime.


def test_auto_curate_main_batch_parallelism_processes_all_decks(
    tmp_path, monkeypatch, capsys,
):
    """parallelism=2 over a 4-deck batch produces 4 deck records +
    1 summary, same total as sequential. Order may differ (records
    emit as workers complete) but the multiset of decks must match."""
    _setup_batch_env(tmp_path, monkeypatch)
    names = ["A", "B", "C", "D"]
    for n in names:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--parallelism", "2",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    deck_records = [r for r in records if "deck" in r]
    assert len(deck_records) == 4
    assert all(r["status"] == "ok" for r in deck_records)
    seen_decks = sorted(Path(r["deck"]).stem for r in deck_records)
    expected = sorted(f"[USER] {n} [B3]" for n in names)
    assert seen_decks == expected
    summary = records[-1]["batch_summary"]
    assert summary["succeeded"] == 4
    assert summary["parallelism"] == 2


def test_auto_curate_main_batch_parallelism_caps_at_deck_count(
    tmp_path, monkeypatch, capsys,
):
    """--parallelism=10 on a 2-deck batch shouldn't spin up 10 idle
    workers. Internal cap is ``min(parallelism, len(paths))``;
    behaviorally we just confirm the batch still completes correctly."""
    _setup_batch_env(tmp_path, monkeypatch)
    for n in ["X", "Y"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--parallelism", "10",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    summary = records[-1]["batch_summary"]
    assert summary["succeeded"] == 2
    # The summary still records the REQUESTED parallelism (10) even
    # though the executor was capped at 2 internally. That's the
    # user's flag echo, not the worker count.
    assert summary["parallelism"] == 10


def test_auto_curate_main_batch_parallelism_one_is_sequential_path(
    tmp_path, monkeypatch, capsys,
):
    """--parallelism=1 (the default) MUST take the pre-#016
    sequential path so users who haven't opted into concurrency
    get bit-for-bit identical behavior. Pin via the
    'glob-order emission' invariant — sequential emits in sorted
    glob order; parallel emits as-completed."""
    _setup_batch_env(tmp_path, monkeypatch)
    # Names chosen so sorted-glob-order is unambiguous: A, B, C.
    for n in ["A", "B", "C"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        # parallelism default = 1; verify omitting the flag works
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    deck_records = [r for r in records if "deck" in r]
    seen_stems = [Path(r["deck"]).stem for r in deck_records]
    # Sequential path preserves glob order.
    assert seen_stems == [f"[USER] {n} [B3]" for n in ["A", "B", "C"]]


def test_auto_curate_main_batch_parallelism_isolates_per_deck_failures(
    tmp_path, monkeypatch, capsys,
):
    """Per-deck failure isolation must hold under parallel dispatch
    too — a bad deck on one worker can't poison the others. Mix one
    empty deck (pipeline rejects it) with good ones and verify the
    good ones still succeed."""
    _setup_batch_env(tmp_path, monkeypatch)
    _write_minimal_deck(tmp_path / "[USER] Good1 [B3].dck", "Good1")
    _write_minimal_deck(tmp_path / "[USER] Good2 [B3].dck", "Good2")
    (tmp_path / "[USER] Bad [B3].dck").write_text("", encoding="utf-8")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--parallelism", "3",
    ])
    assert rc == 0  # mixed outcome with successes
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    summary = records[-1]["batch_summary"]
    assert summary["succeeded"] >= 2
    assert summary["failed"] + summary["succeeded"] == 3


def test_auto_curate_main_batch_parallelism_zero_and_negative_treated_as_one(
    tmp_path, monkeypatch, capsys,
):
    """Defensive: ``--parallelism 0`` / negative shouldn't spawn
    zero workers (deadlock) or raise. Treat as 1."""
    _setup_batch_env(tmp_path, monkeypatch)
    _write_minimal_deck(tmp_path / "[USER] A [B3].dck", "A")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--parallelism", "0",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    assert records[-1]["batch_summary"]["succeeded"] == 1


# ---------------------------------------------------------------------------
# Parallelism UX hint (#016 new_during_work follow-up)
# ---------------------------------------------------------------------------


def test_auto_curate_main_batch_hints_parallelism_when_run_sim_default_one(
    tmp_path, monkeypatch, capsys,
):
    """Multi-deck batch + --run-sim + default --parallelism=1 should
    emit a one-line stderr tip suggesting ``--parallelism 2`` so users
    don't accidentally pay the full sequential wall-time tax.

    The stub `_setup_batch_env` makes --run-sim a no-op at the deck
    level (no actual Forge JVM spawn) but the CLI's hint is wired to
    the *argv* presence of --run-sim, not the sim's runtime behavior.
    """
    _setup_batch_env(tmp_path, monkeypatch)
    for n in ["A", "B"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--run-sim",
        # --parallelism omitted; default = 1
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "tip:" in err
    assert "--parallelism" in err
    # The tip must include the actual deck count so the user knows
    # the suggestion is sized for their batch.
    assert "2 decks" in err


def test_auto_curate_main_batch_no_hint_when_parallelism_already_set(
    tmp_path, monkeypatch, capsys,
):
    """User who already passed --parallelism > 1 doesn't need the
    hint. Avoids stderr noise on the well-tuned overnight workflow."""
    _setup_batch_env(tmp_path, monkeypatch)
    for n in ["A", "B"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--run-sim", "--parallelism", "2",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "tip:" not in err


def test_auto_curate_main_batch_no_hint_when_run_sim_off(
    tmp_path, monkeypatch, capsys,
):
    """No --run-sim means Forge wall time doesn't dominate; the
    Anthropic-curator-only path benefits much less from parallelism
    (~3-5s per call vs 5-15 min per Forge sim). Skip the hint to
    avoid steering the user toward a marginal win."""
    _setup_batch_env(tmp_path, monkeypatch)
    for n in ["A", "B"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        # no --run-sim, default parallelism = 1
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "tip:" not in err


def test_auto_curate_main_batch_no_hint_when_single_deck(
    tmp_path, monkeypatch, capsys,
):
    """Single-deck batch can't benefit from parallelism (nothing to
    parallelize against). Skip the hint."""
    _setup_batch_env(tmp_path, monkeypatch)
    _write_minimal_deck(tmp_path / "[USER] Solo [B3].dck", "Solo")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--run-sim",
    ])
    assert rc == 0
    err = capsys.readouterr().err
    assert "tip:" not in err


# ---------------------------------------------------------------------------
# Batch argv rewriting is flag-AWARE (adversarial-review 2026-07-19)
# ---------------------------------------------------------------------------
#
# The original _build_per_deck_argv dropped "the positional deck token"
# by dropping the first bare token ending in .dck — but flag VALUES can
# end in .dck too (--sim-fillers "PodA.dck,PodB.dck" is the documented
# value shape; --protect-from list.dck is a plausible filename). The
# rewriter ate the value, leaving a dangling flag that argparse then fed
# the next unrelated token (or errored on). And because argparse errors
# raise SystemExit (BaseException, not Exception), that error blew
# through the per-deck isolation boundary and killed the whole
# overnight batch. These tests pin both fixes.


def _capture_per_deck_argv_build(monkeypatch):
    """Stub _process_one_deck to capture its (batch_argv, value_flags)
    inputs without running the pipeline. Returns the capture list;
    each entry is the fully rebuilt per-deck argv, computed with the
    REAL _build_per_deck_argv so the assertion covers the production
    rewrite against the production parser's flag set."""
    import commander_builder._proposer_cli as cli

    built: list[list[str]] = []

    def fake_process(deck_path, batch_argv, force, value_flags):
        built.append(
            cli._build_per_deck_argv(deck_path, batch_argv, value_flags),
        )
        return {"deck": str(deck_path), "status": "ok", "rc": 0, "result": {}}

    monkeypatch.setattr(cli, "_process_one_deck", fake_process)
    return built


def test_batch_argv_keeps_dck_flag_values(tmp_path, monkeypatch, capsys):
    """--sim-fillers "PodA.dck,PodB.dck" and --protect-from list.dck are
    flag VALUES, not the deck positional — the rewriter must copy them
    through verbatim and still substitute the per-deck path up front."""
    _setup_batch_env(tmp_path, monkeypatch)
    deck = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(deck, "A")
    protect_file = tmp_path / "list.dck"
    protect_file.write_text("Some Unrelated Card\n", encoding="utf-8")

    built = _capture_per_deck_argv_build(monkeypatch)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--sim-fillers", "PodA.dck,PodB.dck",
        "--protect-from", str(protect_file),
    ])
    assert rc == 0
    assert len(built) == 1
    argv = built[0]
    # The per-deck path is the (only) positional, substituted up front.
    assert argv[0] == str(deck)
    # Both value-taking flags kept their values, adjacent as passed.
    assert argv[argv.index("--sim-fillers") + 1] == "PodA.dck,PodB.dck"
    assert argv[argv.index("--protect-from") + 1] == str(protect_file)
    # Batch-only flags were stripped.
    assert "--batch" not in argv
    assert str(tmp_path / "[USER]*.dck") not in argv


def test_batch_argv_inline_equals_dck_value_safe(tmp_path, monkeypatch, capsys):
    """The one-token '--flag=value.dck' form carries its value inline;
    the rewriter must pass it through as a single untouched token."""
    _setup_batch_env(tmp_path, monkeypatch)
    deck = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(deck, "A")

    built = _capture_per_deck_argv_build(monkeypatch)

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--sim-fillers=PodA.dck,PodB.dck",
    ])
    assert rc == 0
    argv = built[0]
    assert argv[0] == str(deck)
    assert "--sim-fillers=PodA.dck,PodB.dck" in argv


def test_batch_with_dck_flag_values_runs_end_to_end(
    tmp_path, monkeypatch, capsys,
):
    """Full-pipeline regression: pre-fix, --protect-from list.dck lost
    its value to the positional-stripper, argparse choked on the
    dangling flag, and the SystemExit killed the batch. Now the batch
    completes with every deck ok."""
    _setup_batch_env(tmp_path, monkeypatch)
    for n in ["A", "B"]:
        _write_minimal_deck(tmp_path / f"[USER] {n} [B3].dck", n)
    protect_file = tmp_path / "list.dck"
    protect_file.write_text("Some Unrelated Card\n", encoding="utf-8")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--protect-from", str(protect_file),
        "--sim-fillers", "PodA.dck,PodB.dck",
    ])
    assert rc == 0
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    deck_records = [r for r in records if "deck" in r]
    assert len(deck_records) == 2
    assert all(r["status"] == "ok" for r in deck_records)
    assert records[-1]["batch_summary"]["failed"] == 0


def test_value_taking_flags_derived_from_parser():
    """_value_taking_flags keys off nargs: store_true-style actions
    (nargs == 0) take no value; plain store / append actions do."""
    import argparse

    from commander_builder._proposer_cli import _value_taking_flags

    p = argparse.ArgumentParser()
    p.add_argument("positional")
    p.add_argument("--takes-value")
    p.add_argument("--appends", action="append")
    p.add_argument("--boolean", action="store_true")
    flags = _value_taking_flags(p)
    assert "--takes-value" in flags
    assert "--appends" in flags
    assert "--boolean" not in flags
    assert "positional" not in flags


# ---------------------------------------------------------------------------
# SystemExit isolation at the per-deck boundary
# ---------------------------------------------------------------------------


def _force_bad_argv_for(monkeypatch, marker: str):
    """Wrap the real _build_per_deck_argv so decks whose filename
    contains ``marker`` get an argv argparse rejects (unknown flag).
    This is the cleanest way to reproduce a per-deck-only parse
    failure: the batch-level parse of the same flags already
    succeeded, so a naturally occurring bad per-deck argv requires a
    rewriter bug — which the flag-aware rewrite just fixed."""
    import commander_builder._proposer_cli as cli

    real_build = cli._build_per_deck_argv

    def evil_build(deck_path, batch_argv, value_flags):
        argv = real_build(deck_path, batch_argv, value_flags)
        if marker in deck_path.name:
            argv = [*argv, "--definitely-not-a-real-flag"]
        return argv

    monkeypatch.setattr(cli, "_build_per_deck_argv", evil_build)


def test_batch_systemexit_recorded_and_batch_continues(
    tmp_path, monkeypatch, capsys,
):
    """A deck whose per-deck argv makes argparse exit must be recorded
    as an error for THAT deck (with argparse's stderr message in the
    record) while the batch continues to the next deck. Pre-fix the
    SystemExit sailed past `except Exception` and aborted everything."""
    _setup_batch_env(tmp_path, monkeypatch)
    _write_minimal_deck(tmp_path / "[USER] Bad [B3].dck", "Bad")
    _write_minimal_deck(tmp_path / "[USER] Good [B3].dck", "Good")
    _force_bad_argv_for(monkeypatch, "Bad")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
    ])
    assert rc == 0  # mixed outcome: Good succeeded
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    by_stem = {Path(r["deck"]).stem: r for r in records if "deck" in r}
    bad = by_stem["[USER] Bad [B3]"]
    assert bad["status"] == "error"
    assert "SystemExit" in bad["exception"]
    # argparse's actual error text (written to stderr pre-exit) is
    # captured into the record so overnight logs are actionable.
    assert "--definitely-not-a-real-flag" in bad["exception"]
    good = by_stem["[USER] Good [B3]"]
    assert good["status"] == "ok"
    summary = records[-1]["batch_summary"]
    assert summary["failed"] == 1
    assert summary["succeeded"] == 1


def test_batch_parallel_systemexit_isolated_and_proxies_restored(
    tmp_path, monkeypatch, capsys,
):
    """Same isolation under parallel dispatch, plus: the thread-local
    stdout/stderr proxies installed for the pool are restored by the
    outer finally even when a worker's deck died via SystemExit."""
    import sys as _sys

    from commander_builder._proposer_cli import _ThreadLocalStdoutProxy

    _setup_batch_env(tmp_path, monkeypatch)
    _write_minimal_deck(tmp_path / "[USER] Bad [B3].dck", "Bad")
    _write_minimal_deck(tmp_path / "[USER] Good [B3].dck", "Good")
    _force_bad_argv_for(monkeypatch, "Bad")

    from commander_builder.proposer import auto_curate_main
    rc = auto_curate_main([
        "--batch", str(tmp_path / "[USER]*.dck"),
        "--bracket", "3", "--dry-run", "--no-log",
        "--parallelism", "2",
    ])
    assert rc == 0
    # Proxies swapped back — a leaked proxy would silently reroute all
    # later stdout/stderr through batch thread-local dispatch.
    assert not isinstance(_sys.stdout, _ThreadLocalStdoutProxy)
    assert not isinstance(_sys.stderr, _ThreadLocalStdoutProxy)
    records = [
        json.loads(ln) for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    ]
    by_stem = {Path(r["deck"]).stem: r for r in records if "deck" in r}
    assert by_stem["[USER] Bad [B3]"]["status"] == "error"
    assert "SystemExit" in by_stem["[USER] Bad [B3]"]["exception"]
    assert by_stem["[USER] Good [B3]"]["status"] == "ok"


@pytest.mark.parametrize("exit_code", [0, None])
def test_process_one_deck_systemexit_zero_is_not_an_error(
    tmp_path, monkeypatch, exit_code,
):
    """SystemExit with code 0/None is a SUCCESSFUL early exit (e.g.
    ``--help`` sneaking into the per-deck argv makes argparse print
    usage and raise SystemExit(0)). Recording it as status 'error' made
    the batch summary count a clean exit as a failure."""
    import commander_builder._proposer_cli as cli

    deck = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(deck, "A")

    def exit_cleanly(argv):
        raise SystemExit(exit_code)

    monkeypatch.setattr(cli, "auto_curate_main", exit_cleanly)
    record = cli._process_one_deck(deck, ["--bracket", "3"], False, frozenset())
    assert record["status"] == "ok"
    assert record["rc"] == 0
    # The note explains why there's no per-deck JSON payload.
    assert "SystemExit" in record.get("note", "")


def test_process_one_deck_systemexit_stderr_capped_but_replayed(
    tmp_path, monkeypatch, capsys,
):
    """The captured stderr is replayed VERBATIM to the real stderr (so
    diagnostics stay visible) while the copy embedded in the failure
    record is trimmed to a cap — pre-fix the full text landed in BOTH
    places, bloating the NDJSON stream."""
    import sys as _sys

    import commander_builder._proposer_cli as cli

    deck = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(deck, "A")
    huge = "x" * 5000

    def exit_noisily(argv):
        _sys.stderr.write(huge)
        raise SystemExit(2)

    monkeypatch.setattr(cli, "auto_curate_main", exit_noisily)
    record = cli._process_one_deck(deck, ["--bracket", "3"], False, frozenset())
    assert record["status"] == "error"
    # Record copy: capped (1000 chars + truncation marker + prefix).
    assert len(record["exception"]) < 1200
    assert "truncated" in record["exception"]
    # Real stderr: the full text was replayed untrimmed.
    assert huge in capsys.readouterr().err


def test_process_one_deck_keyboard_interrupt_propagates(
    tmp_path, monkeypatch,
):
    """The SystemExit catch is deliberately narrow: KeyboardInterrupt
    (also BaseException) must still escape the per-deck boundary so
    Ctrl-C actually stops an overnight batch instead of being logged
    as N per-deck 'errors'."""
    import commander_builder._proposer_cli as cli

    deck = tmp_path / "[USER] A [B3].dck"
    _write_minimal_deck(deck, "A")

    def raise_interrupt(argv):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "auto_curate_main", raise_interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli._process_one_deck(deck, ["--bracket", "3"], False, frozenset())
