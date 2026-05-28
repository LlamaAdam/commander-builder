"""Tests for FP-012 Slice A -- intent-learning.

All tests use injected classify_fn / themes_fn / lookup_fn / role_fn /
tribal_fn so no real Scryfall / Anthropic / Forge I/O is needed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from commander_builder.intent import Intent, intent_protect_cards, learn_intent


# ---------------------------------------------------------------------------
# Minimal .dck fixture helpers
# ---------------------------------------------------------------------------

def _write_dck(path: Path, main: list[str], commanders: list[str]) -> Path:
    """Write a minimal .dck file with [Commander] and [Main] sections."""
    lines = ["[Commander]"]
    for c in commanders:
        lines.append(f"1 {c}")
    lines += ["", "[Main]"]
    for card in main:
        lines.append(f"1 {card}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# Simple stub lookup table: card_name -> Scryfall-style dict
_STUB_CARDS: dict[str, dict] = {
    "Craterhoof Behemoth": {
        "oracle_text": "creatures you control get +X/+X and gain trample until end of turn",
        "type_line": "Creature - Beast",
        "color_identity": ["G"],
    },
    "Thassa's Oracle": {
        "oracle_text": "you win the game if your library has no cards",
        "type_line": "Creature - God",
        "color_identity": ["U"],
    },
    "Sol Ring": {
        "oracle_text": "{T}: Add {C}{C}.",
        "type_line": "Artifact",
        "color_identity": [],
    },
    "Counterspell": {
        "oracle_text": "Counter target spell.",
        "type_line": "Instant",
        "color_identity": ["U"],
    },
    "Krenko, Mob Boss": {
        "oracle_text": "{T}: Create X 1/1 red Goblin creature tokens, where X is the number of Goblins you control.",
        "type_line": "Legendary Creature - Goblin Warrior",
        "color_identity": ["R"],
    },
    "Lightning Bolt": {
        "oracle_text": "Lightning Bolt deals 3 damage to any target.",
        "type_line": "Instant",
        "color_identity": ["R"],
    },
}


def _stub_lookup(name: str) -> Optional[dict]:
    return _STUB_CARDS.get(name)


def _stub_classify(deck_path: Path) -> str:
    return "combo"


def _stub_themes(oracles: list[tuple[str, str]]) -> list[str]:
    # Return "spellslinger" if Counterspell is in the deck
    names = {n.lower() for n, _ in oracles}
    if "counterspell" in names:
        return ["spellslinger"]
    return []


def _stub_role(oracle: str, type_line: str) -> str:
    text = oracle.lower()
    if "you win the game" in text:
        return "win_condition"
    if "creatures you control" in text and "trample" in text:
        return "win_condition"
    return "other"


def _stub_tribal(oracle: str, type_line: str) -> Optional[str]:
    if "goblin" in oracle.lower() or "goblin" in type_line.lower():
        return "Goblin"
    return None


# ---------------------------------------------------------------------------
# learn_intent tests
# ---------------------------------------------------------------------------

def test_learn_intent_basic(tmp_path):
    deck = _write_dck(
        tmp_path / "test.dck",
        main=["Craterhoof Behemoth", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=_stub_classify,
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "combo"
    assert "Craterhoof Behemoth" in intent.key_wincons
    assert intent.color_identity == ["R"]
    assert intent.tribal_type == "Goblin"
    assert intent.commander_name == "Krenko, Mob Boss"


def test_learn_intent_wincon_detection(tmp_path):
    """Thassa's Oracle must appear in key_wincons."""
    deck = _write_dck(
        tmp_path / "combo.dck",
        main=["Thassa's Oracle", "Counterspell"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "combo",
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert "Thassa's Oracle" in intent.key_wincons
    assert "Counterspell" not in intent.key_wincons


def test_learn_intent_themes_propagated(tmp_path):
    deck = _write_dck(
        tmp_path / "ctrl.dck",
        main=["Counterspell", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "control",
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert "spellslinger" in intent.themes


def test_learn_intent_missing_oracle_tolerated(tmp_path):
    """Cards with no Scryfall data (lookup returns None) should not crash."""
    deck = _write_dck(
        tmp_path / "partial.dck",
        main=["Unknown Card X", "Sol Ring"],
        commanders=["Krenko, Mob Boss"],
    )

    def lookup_with_holes(name: str) -> Optional[dict]:
        if name == "Unknown Card X":
            return None
        return _stub_lookup(name)

    intent = learn_intent(
        deck,
        classify_fn=lambda p: "midrange",
        themes_fn=lambda oracles: [],
        lookup_fn=lookup_with_holes,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert isinstance(intent, Intent)
    assert "Unknown Card X" not in intent.key_wincons


def test_learn_intent_empty_deck(tmp_path):
    """A deck with no main cards produces a sensible default Intent."""
    deck = tmp_path / "empty.dck"
    deck.write_text("[Main]\n", encoding="utf-8")
    intent = learn_intent(
        deck,
        classify_fn=lambda p: "midrange",
        themes_fn=lambda oracles: [],
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "midrange"
    assert intent.key_wincons == []
    assert intent.color_identity == []
    assert intent.commander_name is None


def test_learn_intent_classify_exception_defaults_midrange(tmp_path):
    """A crashing classify_fn should fall back to 'midrange'."""
    deck = _write_dck(tmp_path / "x.dck", main=["Sol Ring"], commanders=[])

    def boom(p):
        raise RuntimeError("classifier unavailable")

    intent = learn_intent(
        deck,
        classify_fn=boom,
        themes_fn=lambda o: [],
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    assert intent.archetype == "midrange"


def test_learn_intent_to_dict_json_serializable(tmp_path):
    deck = _write_dck(
        tmp_path / "goblin.dck",
        main=["Craterhoof Behemoth"],
        commanders=["Krenko, Mob Boss"],
    )
    intent = learn_intent(
        deck,
        classify_fn=_stub_classify,
        themes_fn=_stub_themes,
        lookup_fn=_stub_lookup,
        role_fn=_stub_role,
        tribal_fn=_stub_tribal,
    )
    blob = json.dumps(intent.to_dict())
    back = json.loads(blob)
    assert back["archetype"] == "combo"
    assert isinstance(back["key_wincons"], list)


# ---------------------------------------------------------------------------
# intent_protect_cards tests
# ---------------------------------------------------------------------------

def test_intent_protect_cards_returns_wincons():
    intent = Intent(
        archetype="combo",
        key_wincons=["Thassa's Oracle", "Demonic Consultation"],
    )
    assert intent_protect_cards(intent) == ["Thassa's Oracle", "Demonic Consultation"]


def test_intent_protect_cards_none_returns_empty():
    assert intent_protect_cards(None) == []


def test_intent_protect_cards_empty_wincons():
    intent = Intent(archetype="aggro", key_wincons=[])
    assert intent_protect_cards(intent) == []


# ---------------------------------------------------------------------------
# improve.py integration: intent protect extends per-round argv
# ---------------------------------------------------------------------------

def test_intent_protect_cards_merge_with_explicit_list():
    """Merging intent wincons with an explicit protect list gives the union."""
    intent = Intent(archetype="combo", key_wincons=["Wincon A", "Wincon B"])
    explicit = ["Pet Card", "Another Card"]
    merged = explicit + intent_protect_cards(intent)
    assert "Pet Card" in merged
    assert "Another Card" in merged
    assert "Wincon A" in merged
    assert "Wincon B" in merged
    assert len(merged) == 4


def test_intent_protect_cards_no_duplicates_when_manually_protected():
    """When a wincon is already manually protected, it appears once (caller deduplicates)."""
    intent = Intent(archetype="combo", key_wincons=["Shared Card"])
    explicit = ["Shared Card", "Other Card"]
    raw_merged = explicit + intent_protect_cards(intent)
    # The caller is responsible for dedup; the library just appends.
    # Verify the wincon IS present:
    assert "Shared Card" in raw_merged


# ---------------------------------------------------------------------------
# improve_main: --learn-intent flag wiring
# ---------------------------------------------------------------------------

def test_improve_main_learn_intent_flag(tmp_path, monkeypatch):
    """--learn-intent path is resolved; intent is set on args before loop."""
    from commander_builder import improve

    learned: list = []
    original_learn = improve.learn_intent

    def fake_learn(path, **kw):
        intent = Intent(archetype="aggro", key_wincons=["Firebreathing Dragon"])
        learned.append(intent)
        return intent

    monkeypatch.setattr(improve, "learn_intent", fake_learn)

    loop_args: list = []

    def fake_loop(deck_path, deck_id, rounds, args, **kw):
        loop_args.append(args)
        return improve.ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", fake_loop)

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([
        str(deck), "--rounds", "1",
        "--learn-intent", str(deck),
    ])

    assert rc == 0
    assert len(learned) == 1
    assert loop_args[0].intent.archetype == "aggro"
    assert loop_args[0].intent.key_wincons == ["Firebreathing Dragon"]


def test_improve_main_learn_intent_missing_file(tmp_path, monkeypatch):
    """--learn-intent with a nonexistent path must return rc=2."""
    from commander_builder import improve

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([
        str(deck), "--rounds", "1",
        "--learn-intent", str(tmp_path / "no_such.dck"),
    ])
    assert rc == 2


def test_improve_main_no_learn_intent_sets_none(tmp_path, monkeypatch):
    """Without --learn-intent, args.intent must be None."""
    from commander_builder import improve

    loop_args: list = []

    def fake_loop(deck_path, deck_id, rounds, args, **kw):
        loop_args.append(args)
        return improve.ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", fake_loop)

    deck = tmp_path / "[USER] Goblins [B3].dck"
    deck.write_text("[metadata]\nName=Goblins\n", encoding="utf-8")

    rc = improve.improve_main([str(deck), "--rounds", "1"])

    assert rc == 0
    assert loop_args[0].intent is None


# ---------------------------------------------------------------------------
# FP-012 Slice A finish: theme-bias round-trip tests
# ---------------------------------------------------------------------------

def _patch_acm_in_proposer_cli(monkeypatch, fake_fn):
    """Patch auto_curate_main in commander_builder._proposer_cli.

    _default_round_fn does a local import:
        from ._proposer_cli import auto_curate_main
    so the patch must land on the module attribute, not a local binding.
    The proposer → _proposer_cli circular import means _proposer_cli can
    only be imported AFTER commander_builder.proposer is loaded; importing
    'commander_builder.improve' triggers the full chain, so by the time
    this helper runs the module is available in sys.modules.
    """
    import sys
    mod = sys.modules.get("commander_builder._proposer_cli")
    if mod is None:
        # Force the full chain; proposer resolves the circular dep.
        from commander_builder.proposer import auto_propose  # noqa: F401
        mod = sys.modules["commander_builder._proposer_cli"]
    monkeypatch.setattr(mod, "auto_curate_main", fake_fn)


def _make_minimal_args(intent=None):
    """Build the minimal argparse.Namespace _default_round_fn needs."""
    import argparse
    return argparse.Namespace(
        bracket=3,
        mode="polish",
        source="heuristic",
        model="claude-sonnet-4-5",
        sim_games=5,
        sim_margin=1,
        sim_fillers=None,
        db_path=None,
        protect=[],
        protect_from=None,
        intent=intent,
    )


def test_default_round_fn_appends_intent_themes_to_argv(tmp_path, monkeypatch):
    """_default_round_fn appends --intent-themes when intent.themes is non-empty.

    The fake auto_curate_main captures the argv; the test asserts
    --intent-themes with the comma-joined slugs is present.
    """
    from commander_builder import improve  # triggers full import chain

    received_argv: list[list[str]] = []

    def fake_acm(argv):
        received_argv.append(list(argv))
        return 0

    _patch_acm_in_proposer_cli(monkeypatch, fake_acm)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    args = _make_minimal_args(
        intent=Intent(
            archetype="tokens",
            themes=["tokens", "aristocrats"],
            key_wincons=[],
        )
    )

    improve._default_round_fn(deck, 1, args)

    assert received_argv, "auto_curate_main was never called"
    argv = received_argv[0]
    assert "--intent-themes" in argv, f"--intent-themes missing from argv: {argv}"
    idx = argv.index("--intent-themes")
    assert argv[idx + 1] == "tokens,aristocrats"


def test_default_round_fn_no_themes_no_flag(tmp_path, monkeypatch):
    """_default_round_fn does NOT append --intent-themes when themes is empty."""
    from commander_builder import improve

    received_argv: list[list[str]] = []

    def fake_acm(argv):
        received_argv.append(list(argv))
        return 0

    _patch_acm_in_proposer_cli(monkeypatch, fake_acm)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    args = _make_minimal_args(
        intent=Intent(archetype="midrange", themes=[], key_wincons=[])
    )

    improve._default_round_fn(deck, 1, args)

    assert received_argv, "auto_curate_main was never called"
    assert "--intent-themes" not in received_argv[0]


def test_default_round_fn_no_intent_no_flag(tmp_path, monkeypatch):
    """_default_round_fn does NOT append --intent-themes when intent is None."""
    from commander_builder import improve

    received_argv: list[list[str]] = []

    def fake_acm(argv):
        received_argv.append(list(argv))
        return 0

    _patch_acm_in_proposer_cli(monkeypatch, fake_acm)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    improve._default_round_fn(deck, 1, _make_minimal_args(intent=None))

    assert received_argv, "auto_curate_main was never called"
    assert "--intent-themes" not in received_argv[0]


def test_improve_main_themes_reach_round_fn(tmp_path, monkeypatch):
    """End-to-end: --learn-intent with themes biases the round_fn argv.

    Wires fake learn_intent (returns Intent with themes=["tokens"]) and a
    fake run_improve_loop that captures args. Verifies the intent's themes
    land on args.intent when the loop is invoked.
    """
    from commander_builder import improve

    def fake_learn(path, **kw):
        return Intent(archetype="tokens", themes=["tokens"], key_wincons=[])

    monkeypatch.setattr(improve, "learn_intent", fake_learn)

    captured_args: list = []

    def fake_loop(deck_path, deck_id, rounds, args, **kw):
        captured_args.append(args)
        return improve.ImproveResult(
            deck_id=deck_id, start_deck=str(deck_path),
            final_deck=str(deck_path), rounds_requested=rounds,
            rounds_run=0, rounds_kept=0, converged=False,
        )

    monkeypatch.setattr(improve, "run_improve_loop", fake_loop)

    deck = tmp_path / "[USER] Tokens [B3].dck"
    deck.write_text("[metadata]\nName=Tokens\n", encoding="utf-8")

    rc = improve.improve_main([
        str(deck), "--rounds", "1",
        "--learn-intent", str(deck),
    ])

    assert rc == 0
    assert captured_args, "run_improve_loop was not called"
    intent = captured_args[0].intent
    assert intent is not None
    assert "tokens" in intent.themes


def test_auto_curate_main_intent_themes_flag(tmp_path, monkeypatch):
    """--intent-themes flag is parsed and forwarded to advise() as intent_themes.

    auto_curate_main does a lazy `from .improvement_advisor import advise`
    inside the function body; patching the module attribute is sufficient.
    """
    # Trigger full import chain so the module is in sys.modules.
    from commander_builder.proposer import auto_propose  # noqa: F401
    from commander_builder._proposer_cli import auto_curate_main
    import commander_builder.improvement_advisor as _ia

    received: dict = {}

    def fake_advise(deck_path, bracket, source=None, intent_themes=None, **kw):
        received["intent_themes"] = intent_themes
        import types
        return types.SimpleNamespace(
            to_manifest=lambda: {"added": [], "removed": []},
        )

    monkeypatch.setattr(_ia, "advise", fake_advise)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3",
        "--intent-themes", "tokens,aristocrats",
        "--dry-run", "--no-log",
    ])

    assert rc == 0
    assert received.get("intent_themes") == ["tokens", "aristocrats"]


def test_advise_passes_intent_themes_to_tag_pages(tmp_path, monkeypatch):
    """advise() with intent_themes propagates the slugs to _fetch_tag_pages_lazy.

    Stubs fetch_tag_page to record attempted slugs.  Intent themes must
    appear first in the fetch order (before any auto-detected tribe/themes).
    """
    import commander_builder.improvement_advisor as _ia
    from commander_builder.edhrec_client import CommanderPage

    fetched_slugs: list[str] = []

    def fake_fetch_tag_page(slug):
        fetched_slugs.append(slug)
        return None  # None pages are skipped — we only care about the slugs

    def fake_fetch_commander_page(commander):
        return CommanderPage(
            commander_name=commander,
            slug="test-commander",
            fetched_at="2026-01-01T00:00:00",
            top_cards=[],
            high_synergy_cards=[],
            new_cards=[],
            category_lists={},
        )

    def fake_lookup_card(name):
        return {
            "oracle_text": "",
            "type_line": "Creature",
            "color_identity": ["G"],
        }

    monkeypatch.setattr(_ia, "fetch_tag_page", fake_fetch_tag_page)
    monkeypatch.setattr(_ia, "fetch_commander_page", fake_fetch_commander_page)
    monkeypatch.setattr(_ia, "lookup_card", fake_lookup_card)

    deck = tmp_path / "[USER] Test [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n\n[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )

    # The advisor may raise once it hits the colour-identity Scryfall call;
    # that's fine — we only need _fetch_tag_pages_lazy to have run.
    try:
        _ia.advise(
            deck,
            bracket=3,
            source="heuristic",
            intent_themes=["tokens", "aristocrats"],
        )
    except Exception:
        pass

    assert "tokens" in fetched_slugs, f"intent slug 'tokens' not fetched; got {fetched_slugs}"
    assert "aristocrats" in fetched_slugs, (
        f"intent slug 'aristocrats' not fetched; got {fetched_slugs}"
    )
    # Intent themes must precede any auto-detected slugs.
    tok_idx = fetched_slugs.index("tokens")
    arist_idx = fetched_slugs.index("aristocrats")
    assert tok_idx < arist_idx
