"""improvement_advisor tests — heuristic, LLM-mocked, and full flow."""
import json
from pathlib import Path

import pytest

from commander_builder.edhrec_client import CardEntry, CommanderPage
from commander_builder.improvement_advisor import (
    AdviceReport,
    DeckDiagnosis,
    SwapRecommendation,
    _aggregate_match_history,
    _heuristic_swap_recommendations,
    _validate_card_names,
    advise,
)


def _write_dck(tmp_path, name: str, commanders: list[str], main: list[str],
               moxfield_id: str = "abc-XYZ") -> Path:
    """Create a synthetic .dck file with [Commander] and [Main] sections."""
    p = tmp_path / name
    body = ["[metadata]", f"Moxfield={moxfield_id}", "[Commander]"]
    body.extend(f"1 {c}" for c in commanders)
    body.append("[Main]")
    body.extend(f"1 {c}" for c in main)
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def _fake_edhrec_page(top: list[tuple[str, float]], synergy: list[tuple[str, float, float]]) -> CommanderPage:
    """Build a CommanderPage stub with controlled card lists."""
    return CommanderPage(
        commander_name="Test Commander",
        slug="test-commander",
        fetched_at="2026-04-26T00:00:00",
        top_cards=[CardEntry(name=n, inclusion_pct=p) for n, p in top],
        high_synergy_cards=[
            CardEntry(name=n, inclusion_pct=p, synergy_pct=s)
            for n, p, s in synergy
        ],
    )


# --- _heuristic_swap_recommendations ---------------------------------------

def test_heuristic_recommends_high_synergy_first():
    """Cards in EDHREC's high-synergy bucket should rank above pure top-cards."""
    deck = {"Sol Ring", "Old Card"}
    page = _fake_edhrec_page(
        top=[("Common Staple", 80.0), ("Some Other", 60.0)],
        synergy=[("Synergy Card", 50.0, 40.0)],
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=3)
    adds = [r for r in recs if r.action == "add"]
    assert adds[0].card == "Synergy Card"  # high-synergy first
    assert "high_synergy" in adds[0].reason


def test_heuristic_skips_cards_already_in_deck():
    deck = {"Sol Ring", "Synergy Card"}
    page = _fake_edhrec_page(
        top=[("Common", 80.0)],
        synergy=[("Synergy Card", 50.0, 40.0)],
    )
    recs = _heuristic_swap_recommendations(deck, page)
    adds = {r.card for r in recs if r.action == "add"}
    assert "Synergy Card" not in adds  # already in deck


def test_heuristic_filters_by_inclusion_threshold():
    deck = {"X"}
    page = _fake_edhrec_page(
        top=[("Low Pop Card", 10.0)],  # below MIN_INCLUSION_PCT_FOR_ADD
        synergy=[],
    )
    recs = _heuristic_swap_recommendations(deck, page)
    adds = [r for r in recs if r.action == "add"]
    assert len(adds) == 0


def test_heuristic_recommends_cuts_for_off_archetype_cards():
    deck = {"Random Off-Archetype", "Sol Ring", "Forest"}
    page = _fake_edhrec_page(
        top=[("Sol Ring", 95.0)],
        synergy=[],
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=0)
    cuts = {r.card for r in recs if r.action == "cut"}
    assert "Random Off-Archetype" in cuts
    # Sol Ring + basics protected.
    assert "Sol Ring" not in cuts
    assert "Forest" not in cuts


def test_heuristic_protects_universal_staples_from_cuts():
    deck = {"Sol Ring", "Arcane Signet", "Command Tower", "Forest", "Off Card"}
    page = _fake_edhrec_page(top=[], synergy=[])
    recs = _heuristic_swap_recommendations(deck, page, add_limit=0)
    cuts = {r.card for r in recs if r.action == "cut"}
    for protected in ("Sol Ring", "Arcane Signet", "Command Tower", "Forest"):
        assert protected not in cuts
    assert "Off Card" in cuts


def test_heuristic_skips_universal_staples_from_adds(monkeypatch):
    """Sol Ring etc. should never appear as adds even if they're top of EDHREC."""
    deck = {"Some Card"}  # deck does NOT include Sol Ring
    page = _fake_edhrec_page(
        top=[("Sol Ring", 99.0), ("Arcane Signet", 95.0), ("Cyclonic Rift", 80.0)],
        synergy=[],
    )
    # Avoid network calls in role lookup
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: None,
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=10)
    adds = {r.card for r in recs if r.action == "add"}
    assert "Sol Ring" not in adds
    assert "Arcane Signet" not in adds
    # Non-staple still recommended
    assert "Cyclonic Rift" in adds


def test_heuristic_tags_role_on_add_recommendations(monkeypatch):
    """Each add recommendation should carry a role tag in its evidence dict."""
    deck = {"X"}
    page = _fake_edhrec_page(
        top=[("Cultivate", 70.0)],
        synergy=[],
    )

    def fake_lookup(name):
        if name == "Cultivate":
            return {
                "oracle_text": "Search your library for up to two basic land "
                               "cards, reveal those cards, put one onto the "
                               "battlefield tapped and the other into your hand.",
                "type_line": "Sorcery",
            }
        return None

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=2)
    adds = [r for r in recs if r.action == "add"]
    assert adds and adds[0].card == "Cultivate"
    assert adds[0].evidence.get("role") == "ramp"


def test_heuristic_role_lookup_handles_offline_gracefully(monkeypatch):
    """If Scryfall is offline, role tagging should fall back to 'unknown'."""
    deck = {"X"}
    page = _fake_edhrec_page(
        top=[("Some Card", 70.0)],
        synergy=[],
    )

    def raises(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", raises
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=2)
    adds = [r for r in recs if r.action == "add"]
    assert adds[0].evidence.get("role") == "unknown"


def test_signals_to_priority_roles_high_draw_rate():
    """'high draw rate' should map to finisher first (deck can't close)."""
    from commander_builder.improvement_advisor import _signals_to_priority_roles

    roles = _signals_to_priority_roles([
        "high draw rate (75%) — deck likely lacks a closer / finisher",
    ])
    assert roles[0] == "finisher"
    assert "wipe" in roles


def test_signals_to_priority_roles_early_aggression():
    """Fast-loss signal should map to removal + ramp + protection."""
    from commander_builder.improvement_advisor import _signals_to_priority_roles

    roles = _signals_to_priority_roles([
        "fastest loss at turn 5 — vulnerable to early aggression / no T1-T3 interaction",
    ])
    assert roles[0] == "removal"
    assert "protection" in roles


def test_signals_to_priority_roles_empty_signals():
    """No signals → empty role list (no re-ranking)."""
    from commander_builder.improvement_advisor import _signals_to_priority_roles
    assert _signals_to_priority_roles([]) == []


def test_signals_to_priority_roles_dedupes_and_caps():
    """Multiple signals should produce deduplicated, capped role list."""
    from commander_builder.improvement_advisor import _signals_to_priority_roles
    roles = _signals_to_priority_roles([
        "high draw rate — deck likely lacks a closer",
        "low win rate over 10 decisive games",
        "deck survives well; problem is offense, not defense",
    ])
    assert len(roles) <= 4
    assert len(roles) == len(set(roles))  # no duplicates
    assert roles[0] == "finisher"  # earliest signal's leading role wins


def test_heuristic_reranks_by_diagnosis_priority(monkeypatch):
    """When diagnosis has priority_roles, role-tagged adds re-rank to match."""
    from commander_builder.improvement_advisor import (
        DeckDiagnosis,
        _heuristic_swap_recommendations,
    )

    deck = {"X"}
    page = _fake_edhrec_page(
        top=[
            ("Cultivate", 60.0),       # ramp
            ("Craterhoof Behemoth", 50.0),  # finisher
            ("Brainstorm", 70.0),      # draw
        ],
        synergy=[],
    )

    def fake_lookup(name):
        return {
            "Cultivate": {
                "oracle_text": "Search your library for two basic land cards...",
                "type_line": "Sorcery",
            },
            "Craterhoof Behemoth": {
                "oracle_text": "When this enters, creatures you control gain "
                               "trample and get +X/+X where X is the number "
                               "of creatures you control. Each opponent loses "
                               "10 life.",
                "type_line": "Creature",
            },
            "Brainstorm": {
                "oracle_text": "Draw three cards, then put two cards from "
                               "your hand on top of your library.",
                "type_line": "Instant",
            },
        }.get(name)

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup
    )

    # Diagnosis says: deck can't close (priority: finisher first).
    diag = DeckDiagnosis(priority_roles=["finisher", "wipe", "tutor"])
    recs = _heuristic_swap_recommendations(deck, page, add_limit=10, diagnosis=diag)
    adds = [r for r in recs if r.action == "add"]
    # Craterhoof (finisher) should now be first, even though Cultivate (ramp)
    # came first in the original synergy/inclusion ordering.
    assert adds[0].card == "Craterhoof Behemoth"


def test_heuristic_no_diagnosis_keeps_original_order(monkeypatch):
    """Without diagnosis priority_roles, the natural inclusion-pct order
    should hold."""
    from commander_builder.improvement_advisor import (
        _heuristic_swap_recommendations,
    )

    deck = {"X"}
    page = _fake_edhrec_page(
        top=[
            ("Cultivate", 60.0),
            ("Craterhoof Behemoth", 50.0),
        ],
        synergy=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", lambda n: None,
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=10)
    adds = [r for r in recs if r.action == "add"]
    # No re-ranking — Cultivate (top of list) stays first.
    assert adds[0].card == "Cultivate"


def test_heuristic_respects_add_and_cut_limits():
    deck = {f"Off-{i}" for i in range(20)}
    page = _fake_edhrec_page(
        top=[(f"Top-{i}", 80.0) for i in range(20)],
        synergy=[],
    )
    recs = _heuristic_swap_recommendations(deck, page, add_limit=3, cut_limit=4)
    adds = [r for r in recs if r.action == "add"]
    cuts = [r for r in recs if r.action == "cut"]
    assert len(adds) == 3
    assert len(cuts) == 4


# --- _aggregate_match_history ----------------------------------------------

def test_aggregate_match_history_empty_dir(tmp_path):
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert diag.games_played == 0
    assert diag.weakness_signals == []


def test_aggregate_match_history_detects_high_draw_rate(tmp_path):
    """4-of-6-draws case from the real Hakbal match → 'high draw rate' signal."""
    p = tmp_path / "USER_Foo_B3_20260427T000000Z.json"
    p.write_text(json.dumps({
        "games_played": 6, "user_wins": 0, "user_losses": 2, "draws": 4,
        "avg_user_ending_life": 14.8, "avg_user_damage_taken": 22.2,
        "fastest_loss_turn": 14,
    }), encoding="utf-8")
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert diag.games_played == 6
    assert diag.draws == 4
    assert diag.draw_rate > 0.5
    assert any("high draw rate" in s for s in diag.weakness_signals)


def test_aggregate_match_history_detects_low_win_rate(tmp_path):
    p = tmp_path / "USER_Foo_B3_x.json"
    p.write_text(json.dumps({
        "games_played": 10, "user_wins": 0, "user_losses": 8, "draws": 2,
    }), encoding="utf-8")
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert any("low win rate" in s for s in diag.weakness_signals)


def test_aggregate_match_history_detects_early_elimination(tmp_path):
    p = tmp_path / "USER_Foo_B3_x.json"
    p.write_text(json.dumps({
        "games_played": 5, "user_wins": 0, "user_losses": 5, "draws": 0,
        "fastest_loss_turn": 6,
    }), encoding="utf-8")
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert any("vulnerable to" in s and "early aggression" in s
               for s in diag.weakness_signals)


def test_aggregate_match_history_detects_strong_defense(tmp_path):
    p = tmp_path / "USER_Foo_B3_x.json"
    p.write_text(json.dumps({
        "games_played": 5, "user_wins": 0, "user_losses": 0, "draws": 5,
        "avg_user_ending_life": 30.0,
    }), encoding="utf-8")
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert any("survives well" in s for s in diag.weakness_signals)


def test_aggregate_match_history_skips_corrupt_json(tmp_path):
    (tmp_path / "USER_Foo_B3_corrupt.json").write_text("{ broken")
    good = tmp_path / "USER_Foo_B3_good.json"
    good.write_text(json.dumps({"games_played": 3, "user_wins": 1, "draws": 0}), encoding="utf-8")
    diag = _aggregate_match_history("[USER] Foo [B3].dck", match_dir=tmp_path)
    assert diag.games_played == 3  # only the good file counted


# --- advise() — full pipeline (EDHREC mocked) ------------------------------

def test_advise_full_flow_heuristic(tmp_path, monkeypatch):
    deck_dir = tmp_path / "decks"
    match_dir = tmp_path / "matches"
    deck_dir.mkdir()
    match_dir.mkdir()

    deck = _write_dck(
        deck_dir, "[USER] Hakbal of the Surging Soul [B3].dck",
        commanders=["Hakbal of the Surging Soul"],
        main=["Sol Ring", "Forest", "Old Card"],
    )

    # Fake EDHREC page recommends a synergy card and flags Old Card as off.
    fake_page = _fake_edhrec_page(
        top=[("Sol Ring", 95.0), ("Coat of Arms", 60.0)],
        synergy=[("Kindred Discovery", 40.0, 50.0)],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )

    report = advise(deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    assert report.source == "heuristic"
    assert report.deck_id == "abc-XYZ"
    assert report.bracket == 3
    assert "Hakbal of the Surging Soul" in report.commander_names

    adds = {r.card for r in report.recommendations if r.action == "add"}
    cuts = {r.card for r in report.recommendations if r.action == "cut"}
    assert "Kindred Discovery" in adds
    assert "Coat of Arms" in adds
    assert "Old Card" in cuts
    # Universal staples not cut.
    assert "Sol Ring" not in cuts
    assert "Forest" not in cuts


def test_advise_to_manifest_matches_audit_schema(tmp_path, monkeypatch):
    deck_dir = tmp_path / "decks"
    match_dir = tmp_path / "matches"
    deck_dir.mkdir()
    match_dir.mkdir()

    deck = _write_dck(deck_dir, "[USER] X [B3].dck",
                     commanders=["Hakbal of the Surging Soul"],
                     main=["Sol Ring", "Old"], moxfield_id="public-id")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(
            top=[("Coat of Arms", 60.0)], synergy=[],
        ),
    )

    report = advise(deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    manifest = report.to_manifest()
    # Schema check: audit_manifest fields present.
    for key in ("deck_id", "bracket", "audit_version", "audit_timestamp",
                "added", "removed", "rationale"):
        assert key in manifest
    assert manifest["deck_id"] == "public-id"
    assert manifest["bracket"] == 3
    assert manifest["audit_version"].startswith("advisor-")
    assert "Coat of Arms" in manifest["added"]
    assert "Old" in manifest["removed"]


def test_advise_raises_on_missing_deck(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page([], []),
    )
    with pytest.raises(FileNotFoundError):
        advise(tmp_path / "ghost.dck", bracket=3,
               deck_dir=tmp_path, match_dir=tmp_path)


def test_advise_raises_on_no_commanders(tmp_path, monkeypatch):
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    p = deck_dir / "[USER] Empty [B3].dck"
    p.write_text("[Main]\n1 Sol Ring\n", encoding="utf-8")  # no [Commander]

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page([], []),
    )
    with pytest.raises(ValueError, match="no commanders"):
        advise(p, bracket=3, deck_dir=deck_dir, match_dir=deck_dir)


def test_advise_falls_back_when_claude_unavailable(tmp_path, monkeypatch):
    """use_claude=True but no API key → router catches and degrades to
    heuristic, returning a normal report."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(deck_dir, "[USER] X [B3].dck",
                     commanders=["Hakbal"], main=["Sol Ring"])
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(
            top=[("Coat of Arms", 60.0)], synergy=[],
        ),
    )
    report = advise(deck, bracket=3, use_claude=True,
                    deck_dir=deck_dir, match_dir=deck_dir)
    assert report.source == "heuristic"  # fell back


def test_advise_uses_claude_when_wired(tmp_path, monkeypatch):
    """API key present + mocked anthropic SDK → source is 'claude' and
    recommendations come from the LLM."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(deck_dir, "[USER] X [B3].dck",
                     commanders=["Hakbal"], main=["Sol Ring", "Old"])
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )

    fake_response_json = json.dumps({
        "rationale": "test rationale",
        "added": ["Claude Pick A", "Claude Pick B"],
        "removed": ["Old"],
    })

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _Msg(fake_response_json)
            return M()

    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    report = advise(deck, bracket=3, use_claude=True,
                    deck_dir=deck_dir, match_dir=deck_dir)
    assert report.source == "claude"
    cards = {r.card for r in report.recommendations}
    assert "Claude Pick A" in cards
    assert "Old" in cards
    # Rationale propagates to the diagnosis pattern_summary.
    assert "test rationale" in report.diagnosis.pattern_summary


# --- _validate_card_names — hallucination defense for Claude analyst -------

def test_validate_marks_known_cards_true(monkeypatch):
    """Scryfall returns a card dict → name_known is True."""
    recs = [
        SwapRecommendation(card="Sol Ring", action="add", reason=""),
        SwapRecommendation(card="Cultivate", action="cut", reason=""),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"name": name, "type_line": "Artifact"},
    )
    _validate_card_names(recs)
    assert all(r.name_known is True for r in recs)


def test_validate_marks_unknown_cards_false(monkeypatch):
    """Scryfall returns None (404) → name_known is False — hallucinated."""
    recs = [
        SwapRecommendation(card="Accursed Marauder", action="add", reason=""),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: None,
    )
    _validate_card_names(recs)
    assert recs[0].name_known is False


def test_validate_leaves_name_known_none_on_lookup_exception(monkeypatch):
    """Network failure / cache corruption → leave name_known as None.

    None means 'we couldn't check'; we never want to flag a legitimate
    card as hallucinated because Scryfall happened to be down.
    """
    recs = [SwapRecommendation(card="Sol Ring", action="add", reason="")]
    def boom(name):
        raise RuntimeError("network down")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", boom,
    )
    _validate_card_names(recs)
    assert recs[0].name_known is None


def test_validate_handles_empty_list(monkeypatch):
    """No-op on empty list; should not call lookup_card."""
    calls = []
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: calls.append(name) or {"name": name},
    )
    _validate_card_names([])
    assert calls == []


def test_validate_mixed_known_and_unknown(monkeypatch):
    """Each rec gets independently flagged."""
    recs = [
        SwapRecommendation(card="Sol Ring", action="add", reason=""),
        SwapRecommendation(card="Fake Card", action="add", reason=""),
        SwapRecommendation(card="Cultivate", action="cut", reason=""),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"name": name} if name != "Fake Card" else None,
    )
    _validate_card_names(recs)
    assert recs[0].name_known is True
    assert recs[1].name_known is False
    assert recs[2].name_known is True


def test_advise_populates_name_known_on_recommendations(tmp_path, monkeypatch):
    """End-to-end: advise() runs the validator so every rec carries a flag."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring", "Old"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(
            top=[("Coat of Arms", 60.0)], synergy=[],
        ),
    )
    # Pretend Scryfall knows "Coat of Arms" and "Old" but not anything else.
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"name": name} if name in {"Coat of Arms", "Old"} else None,
    )

    report = advise(deck, bracket=3, deck_dir=deck_dir, match_dir=deck_dir)
    # Every rec has name_known set (not None).
    assert all(r.name_known is not None for r in report.recommendations), (
        "validator should have populated name_known on every rec"
    )


def test_advise_flags_hallucinated_claude_card(tmp_path, monkeypatch):
    """When Claude invents a non-existent card, name_known=False on that rec
    while real cards remain True."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(deck_dir, "[USER] X [B3].dck",
                     commanders=["Hakbal"], main=["Sol Ring"])
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )

    fake_response_json = json.dumps({
        "rationale": "test",
        "added": ["Sol Ring", "Accursed Marauder"],  # second one is fake
        "removed": [],
    })

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self):
            class M:
                def create(self, **kw):
                    return _Msg(fake_response_json)
            return M()
    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"name": name} if name != "Accursed Marauder" else None,
    )

    report = advise(deck, bracket=3, use_claude=True,
                    deck_dir=deck_dir, match_dir=deck_dir)
    by_card = {r.card: r.name_known for r in report.recommendations}
    assert by_card.get("Sol Ring") is True
    assert by_card.get("Accursed Marauder") is False
