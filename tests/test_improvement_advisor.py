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
    main as _advise_main,
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


def test_role_for_card_surfaces_extended_taxonomy_win_condition(monkeypatch):
    """The advisor's role tagger must agree with the dashboard's
    categories panel: a card with a "target opponent loses the game"
    finisher line should bucket as ``win_condition`` (extended
    taxonomy), not the base ``finisher`` role. Without consolidation,
    the dashboard panel reads win_condition=1 while the advisor's
    evidence pill reads "finisher" for the same card — confusing
    drift the chrome audit (2026-05-13) flagged.
    """
    from commander_builder.improvement_advisor import _role_for_card

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {
            "oracle_text": "Target opponent loses the game.",
            "type_line": "Sorcery",
        },
    )
    assert _role_for_card("Coalition Victory") == "win_condition"


def test_role_for_card_surfaces_extended_taxonomy_land_payoff(monkeypatch):
    """Same consolidation contract for landfall-style payoffs. A card
    matching ``classify_role_extended``'s land_payoff patterns should
    surface as ``land_payoff`` through the advisor too — not as
    ``threat`` (the base taxonomy's creature fallback).
    """
    from commander_builder.improvement_advisor import _role_for_card

    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {
            "oracle_text": "Landfall — Whenever a land enters the battlefield "
                           "under your control, create a 2/2 Cat token.",
            "type_line": "Creature — Cat",
        },
    )
    assert _role_for_card("Felidar Retreat") == "land_payoff"


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

    # Diagnosis says: deck can't close (priority: closer-buckets first).
    # After the 2026-05-13 role-classifier consolidation, Craterhoof
    # tags as ``win_condition`` (its "each opponent loses 10 life"
    # text matches the wincon patterns) rather than the older base
    # ``finisher`` bucket. priority_roles lists both so any
    # closer-shape card surfaces — this matches the
    # ``_SIGNAL_TO_ROLES`` mapping the orchestrator builds.
    diag = DeckDiagnosis(priority_roles=["finisher", "win_condition", "wipe", "tutor"])
    recs = _heuristic_swap_recommendations(deck, page, add_limit=10, diagnosis=diag)
    adds = [r for r in recs if r.action == "add"]
    # Craterhoof (win_condition / finisher synonym) should now be
    # first, even though Cultivate (ramp) came first in the original
    # synergy/inclusion ordering.
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


# --- _bracket_peers_recommendations — N highest-liked decks at bracket -----
# This is the alternative to the EDHREC aggregate path. The Ur-Dragon B4
# audit (2026-05-13) revealed EDHREC's cross-bracket average produced
# generic ramp adds for a deck that already had 12+ ramp pieces, while
# missing archetype-specific cards like Moat. Bracket-peers sources
# recommendations from other tuned builds of the same commander at the
# same bracket — should be archetype-appropriate by construction.

def _moxfield_deck_with_cards(public_id: str, cards: list[str]) -> dict:
    """Synthesize a Moxfield deck JSON shape with the given main cards."""
    return {
        "publicId": public_id,
        "name": f"Deck {public_id}",
        "boards": {
            "mainboard": {
                "cards": {
                    f"card-{i}": {"card": {"name": name}, "quantity": 1}
                    for i, name in enumerate(cards)
                }
            },
        },
    }


def test_bracket_peers_recommends_must_add_cards(monkeypatch):
    """Cards appearing in ALL references that the user is missing
    surface as add recommendations with full confidence ('unanimous')."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations

    deck_cards = {"Sol Ring", "Forest"}  # user is missing the staples below
    fake_refs = [
        _moxfield_deck_with_cards("d1", [
            "Sol Ring", "Moat", "Last March of the Ents", "Forest",
        ]),
        _moxfield_deck_with_cards("d2", [
            "Sol Ring", "Moat", "Last March of the Ents", "Mountain",
        ]),
        _moxfield_deck_with_cards("d3", [
            "Sol Ring", "Moat", "Last March of the Ents", "Plains",
        ]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda name, bracket=None, n=5, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    recs, ref_count = _bracket_peers_recommendations(
        commander_name="The Ur-Dragon",
        bracket=4,
        deck_cards=deck_cards,
    )
    assert ref_count == 3
    add_names = {r.card for r in recs if r.action == "add"}
    # Both cards appeared in all 3 references; user is missing both.
    assert "Moat" in add_names
    assert "Last March of the Ents" in add_names
    # Universal staples (Sol Ring) must NOT surface as must-add even
    # when they appear in all references.
    assert "Sol Ring" not in add_names


def test_bracket_peers_recommends_cut_for_truly_off_meta(monkeypatch):
    """Cards in user's deck that appear in NO references AND aren't
    universal staples are cut candidates."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations

    deck_cards = {"Sol Ring", "Forest", "Goofy Janky Card"}
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Sol Ring", "Moat", "Forest"]),
        _moxfield_deck_with_cards("d2", ["Sol Ring", "Moat", "Mountain"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda name, bracket=None, n=5, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    recs, _ = _bracket_peers_recommendations(
        commander_name="The Ur-Dragon", bracket=4, deck_cards=deck_cards,
    )
    cut_names = {r.card for r in recs if r.action == "cut"}
    assert "Goofy Janky Card" in cut_names
    # Sol Ring is universal — never cut even if absent from refs.
    assert "Sol Ring" not in cut_names


def test_bracket_peers_carries_frequency_evidence(monkeypatch):
    """Each add rec should tag in_n_references / total_references so the
    UI can show 'in 5/5 reference decks' for ranking. The frequency
    label feeds the existing render_frequency_label helper."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations

    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Moat", "Mana Crypt"]),
        _moxfield_deck_with_cards("d2", ["Moat", "Other Card"]),
        _moxfield_deck_with_cards("d3", ["Moat", "Other Card"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda name, bracket=None, n=5, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(),
    )
    by_card = {r.card: r for r in recs if r.action == "add"}
    # Moat appeared in all 3, "Other Card" in 2/3 ("majority"),
    # Mana Crypt is a universal staple so it's filtered.
    assert by_card["Moat"].evidence.get("in_n_references") == 3
    assert by_card["Moat"].evidence.get("total_references") == 3
    assert "in 3/3 reference decks" in by_card["Moat"].reason \
        or "unanimous" in by_card["Moat"].reason
    if "Other Card" in by_card:
        assert by_card["Other Card"].evidence.get("in_n_references") == 2


def test_bracket_peers_returns_empty_when_no_references(monkeypatch):
    """When the Moxfield fetch returns no decks (commander too obscure,
    network down), the recommender returns empty — caller falls back."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: [],
    )
    recs, ref_count = _bracket_peers_recommendations(
        commander_name="Obscure Commander", bracket=3, deck_cards={"Foo"},
    )
    assert recs == []
    assert ref_count == 0


def test_bracket_peers_tags_role_on_adds(monkeypatch):
    """Adds inherit role classification so the UI can group them
    consistently with the heuristic / claude paths."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations

    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Moat"]),
        _moxfield_deck_with_cards("d2", ["Moat"]),
    ]

    def fake_lookup(name):
        if name == "Moat":
            return {
                "oracle_text": "Creatures without flying can't attack.",
                "type_line": "Enchantment",
            }
        return None
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(),
    )
    moat = next(r for r in recs if r.card == "Moat")
    # role tagging via staples.classify_role — Moat is "protection"
    # (creatures without flying can't attack). Whatever the classifier
    # returns, the field must be present so the UI render isn't lossy.
    assert "role" in moat.evidence
    assert moat.evidence["role"] != ""


def test_bracket_peers_never_cuts_nonbasic_lands(monkeypatch):
    """Regression for the 2026-05-13 Ur-Dragon audit: bracket-peers
    cut Savannah ($200 ABU dual) because none of the top-5 references
    happened to run it. Manabase is deliberate — the advisor must
    never auto-recommend cutting any land (basic, dual, fetch, etc.).
    """
    from commander_builder.improvement_advisor import _bracket_peers_recommendations
    # User runs Savannah; none of the references do. Without the land
    # guard the old code would emit Savannah in the cut list.
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Sol Ring", "Mountain"]),
        _moxfield_deck_with_cards("d2", ["Sol Ring", "Mountain"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    # Savannah's Scryfall hit says "Land" in the type_line, so
    # staples.is_land() returns True and the cut path skips it.
    def fake_lookup(name):
        if name == "Savannah":
            return {"type_line": "Land — Plains Forest", "oracle_text": ""}
        return {"type_line": "Creature — Dragon", "oracle_text": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )
    recs, _ = _bracket_peers_recommendations(
        commander_name="The Ur-Dragon", bracket=4,
        deck_cards={"Savannah", "Some Other Card"},
    )
    cut_cards = {r.card for r in recs if r.action == "cut"}
    assert "Savannah" not in cut_cards
    # Sanity: the non-land "Some Other Card" still gets recommended
    # for cut (so the guard isn't accidentally over-broad).
    assert "Some Other Card" in cut_cards


def test_heuristic_never_cuts_nonbasic_lands(monkeypatch):
    """Same guard as above, applied to the EDHREC heuristic cut path."""
    deck_cards = {"Savannah", "Some Other Card"}
    page = _fake_edhrec_page(top=[("Sol Ring", 90.0)], synergy=[])
    def fake_lookup(name):
        if name == "Savannah":
            return {"type_line": "Land — Plains Forest", "oracle_text": ""}
        return {"type_line": "Creature — Dragon", "oracle_text": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )
    recs = _heuristic_swap_recommendations(deck_cards, page)
    cut_cards = {r.card for r in recs if r.action == "cut"}
    assert "Savannah" not in cut_cards
    assert "Some Other Card" in cut_cards


def test_bracket_peers_excludes_user_cards_from_adds(monkeypatch):
    """A card already in the user's deck must NEVER appear as an add
    even if it's in every reference — that's a no-op recommendation."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Cyclonic Rift"]),
        _moxfield_deck_with_cards("d2", ["Cyclonic Rift"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )
    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards={"Cyclonic Rift"},
    )
    assert all(r.card != "Cyclonic Rift" for r in recs)


# --- advise(source="bracket_peers") integration ----------------------------

def test_advise_with_bracket_peers_source_routes_through_new_path(
    tmp_path, monkeypatch,
):
    """advise(source='bracket_peers') skips EDHREC entirely and uses
    the bracket-peers recommender."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["The Ur-Dragon"], main=["Sol Ring", "Old Card"],
    )
    # EDHREC must NOT be called when source=bracket_peers — pin it.
    def edhrec_should_not_fire(*a, **kw):
        raise AssertionError("EDHREC fetch must not run in bracket_peers mode")
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        edhrec_should_not_fire,
    )
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Moat", "Sol Ring"]),
        _moxfield_deck_with_cards("d2", ["Moat", "Sol Ring"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    report = advise(
        deck, bracket=4, source="bracket_peers",
        deck_dir=deck_dir, match_dir=deck_dir,
    )
    assert report.source == "bracket_peers"
    add_cards = {r.card for r in report.recommendations if r.action == "add"}
    assert "Moat" in add_cards


def test_advise_bracket_peers_falls_back_to_heuristic_on_empty_refs(
    tmp_path, monkeypatch,
):
    """When Moxfield returns no references (obscure commander, network),
    fall back to the EDHREC heuristic so the audit still produces output.
    fallback_reason names the cause for UI surfacing."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["Obscure"], main=["Sol Ring"],
    )
    fake_page = _fake_edhrec_page(
        top=[("Coat of Arms", 60.0)], synergy=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )
    report = advise(
        deck, bracket=4, source="bracket_peers",
        deck_dir=deck_dir, match_dir=deck_dir,
    )
    # Fell back to heuristic when no refs found.
    assert report.source == "heuristic"
    assert report.fallback_reason is not None
    assert "no bracket-peer references" in report.fallback_reason.lower() \
        or "no references" in report.fallback_reason.lower()


def test_advise_bracket_peers_validates_card_names(tmp_path, monkeypatch):
    """The hallucination-defense pass still runs in bracket-peers mode —
    if a reference deck somehow has a typo, the name_known flag surfaces
    it. (Less likely than Claude inventing names, but the pipeline shape
    stays uniform across all sources.)"""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["X"], main=["Sol Ring"],
    )
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Real Card", "Typo Card"]),
        _moxfield_deck_with_cards("d2", ["Real Card", "Typo Card"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: ({"oracle_text": "", "type_line": ""}
                      if name == "Real Card" else None),
    )
    report = advise(
        deck, bracket=4, source="bracket_peers",
        deck_dir=deck_dir, match_dir=deck_dir,
    )
    by_card = {r.card: r.name_known for r in report.recommendations}
    assert by_card.get("Real Card") is True
    assert by_card.get("Typo Card") is False


# --- Role-saturation guard (the Ur-Dragon "stop suggesting more ramp" fix) -
# Motivation: the 2026-05-13 Ur-Dragon B4 audit recommended 5 ramp/cost-
# reducer adds to a deck already running 12+ ramp pieces. The advisor
# was role-blind on the deck side. This filter sits in the rec pipeline
# and drops add candidates whose role bucket is already saturated.


def test_filter_for_saturation_drops_ramp_when_deck_has_too_much(monkeypatch):
    """Adds tagged as 'ramp' get filtered out when the deck already has
    ≥ROLE_SATURATION_THRESHOLDS['ramp'] ramp pieces."""
    from commander_builder.improvement_advisor import _filter_for_saturation
    from commander_builder.staples import ROLE_SATURATION_THRESHOLDS

    threshold = ROLE_SATURATION_THRESHOLDS["ramp"]

    candidates = [
        SwapRecommendation(
            card="Sol Ring",  # universal staple but rec'd anyway in this synth
            action="add", reason="",
            evidence={"role": "ramp"},
        ),
        SwapRecommendation(
            card="Cyclonic Rift", action="add", reason="",
            evidence={"role": "wipe"},
        ),
        SwapRecommendation(
            card="Old Card", action="cut", reason="",
            evidence={"role": "other"},
        ),
    ]
    # Pretend the deck has saturated ramp but no wipes.
    role_counts = {"ramp": threshold, "wipe": 2}

    kept, skipped = _filter_for_saturation(candidates, role_counts)
    kept_cards = {r.card for r in kept}
    assert "Sol Ring" not in kept_cards     # dropped — ramp saturated
    assert "Cyclonic Rift" in kept_cards    # kept — wipe not saturated
    assert "Old Card" in kept_cards         # cut, never filtered
    # The skipped record names the role + the count so the UI can show
    # "skipped: you already have 12 ramp pieces".
    assert any(
        s["card"] == "Sol Ring" and s["role"] == "ramp"
        and s["deck_count"] == threshold
        and s["threshold"] == threshold
        for s in skipped
    )


def test_filter_for_saturation_keeps_everything_when_no_role_saturated(
    monkeypatch,
):
    """Backward-compat: when no role bucket is saturated, the filter is
    a no-op."""
    from commander_builder.improvement_advisor import _filter_for_saturation

    candidates = [
        SwapRecommendation(card="A", action="add", reason="",
                           evidence={"role": "ramp"}),
        SwapRecommendation(card="B", action="add", reason="",
                           evidence={"role": "draw"}),
    ]
    role_counts = {"ramp": 4, "draw": 3}  # nowhere near threshold
    kept, skipped = _filter_for_saturation(candidates, role_counts)
    assert [r.card for r in kept] == ["A", "B"]
    assert skipped == []


def test_filter_for_saturation_treats_missing_role_as_other(monkeypatch):
    """Old recs without evidence.role (legacy or stub) bucket as 'other'
    which never saturates — they always pass through."""
    from commander_builder.improvement_advisor import _filter_for_saturation
    candidates = [
        SwapRecommendation(card="A", action="add", reason="", evidence={}),
    ]
    kept, skipped = _filter_for_saturation(candidates, {"ramp": 99})
    assert [r.card for r in kept] == ["A"]
    assert skipped == []


def test_advise_heuristic_drops_redundant_ramp_adds(tmp_path, monkeypatch):
    """End-to-end through advise(): a deck with 12+ ramp shouldn't get
    EDHREC's ramp recommendations applied. The Ur-Dragon failure mode."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    # Synthesize a deck text whose main cards will all classify as ramp.
    ramp_card_names = [f"Ramp Piece {i}" for i in range(1, 14)]  # 13 ramp
    deck = _write_dck(
        deck_dir, "[USER] RampHeavy [B3].dck",
        commanders=["Some Commander"],
        main=ramp_card_names + ["Old Filler"],
    )
    # EDHREC offers two more ramp candidates and one wipe.
    fake_page = _fake_edhrec_page(
        top=[
            ("Cultivate", 90.0),
            ("Rampant Growth", 85.0),
            ("Cyclonic Rift", 70.0),
        ],
        synergy=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )
    # Tag every recommended/existing card with appropriate roles.
    def fake_lookup(name):
        if name in ("Cultivate", "Rampant Growth"):
            return {
                "oracle_text": "Search your library for a basic land card",
                "type_line": "Sorcery",
            }
        if name == "Cyclonic Rift":
            return {
                "oracle_text": "destroy all nonland permanents",
                "type_line": "Instant",
            }
        if name.startswith("Ramp Piece"):
            return {
                "oracle_text": "Add {G}",
                "type_line": "Artifact",
            }
        return {"oracle_text": "", "type_line": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )

    report = advise(deck, bracket=3,
                    deck_dir=deck_dir, match_dir=deck_dir)
    add_names = {r.card for r in report.recommendations if r.action == "add"}
    # Ramp adds dropped (deck has 13 ramp pieces, threshold is 12).
    assert "Cultivate" not in add_names
    assert "Rampant Growth" not in add_names
    # Wipe ad survives — that bucket isn't saturated.
    assert "Cyclonic Rift" in add_names
    # Saturation report names which roles got filtered.
    assert hasattr(report, "skipped_for_saturation")
    skipped_roles = {s["role"] for s in report.skipped_for_saturation}
    assert "ramp" in skipped_roles


def test_advise_bracket_peers_drops_redundant_ramp_adds(tmp_path, monkeypatch):
    """Same redundancy guard, but in the bracket_peers source path."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    ramp_card_names = [f"Ramp Piece {i}" for i in range(1, 14)]
    deck = _write_dck(
        deck_dir, "[USER] RampHeavy [B4].dck",
        commanders=["X"], main=ramp_card_names,
    )
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Cultivate", "Cyclonic Rift"]),
        _moxfield_deck_with_cards("d2", ["Cultivate", "Cyclonic Rift"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )

    def fake_lookup(name):
        if name == "Cultivate":
            return {
                "oracle_text": "Search your library for a basic land card",
                "type_line": "Sorcery",
            }
        if name == "Cyclonic Rift":
            return {
                "oracle_text": "destroy all nonland permanents",
                "type_line": "Instant",
            }
        if name.startswith("Ramp Piece"):
            return {"oracle_text": "Add {G}", "type_line": "Artifact"}
        return {"oracle_text": "", "type_line": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )

    report = advise(
        deck, bracket=4, source="bracket_peers",
        deck_dir=deck_dir, match_dir=deck_dir,
    )
    add_names = {r.card for r in report.recommendations if r.action == "add"}
    assert "Cultivate" not in add_names
    assert "Cyclonic Rift" in add_names


def test_bracket_peers_drops_singleton_references(monkeypatch):
    """Phase A gap #3: with 5 references, a card in only 1/5 is a weak
    signal — surface it as 'consider' (lower confidence) not as a
    must-add. Default min_refs filters to majority of references."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Common Pick", "Rare Pick"]),
        _moxfield_deck_with_cards("d2", ["Common Pick"]),
        _moxfield_deck_with_cards("d3", ["Common Pick"]),
        _moxfield_deck_with_cards("d4", ["Common Pick"]),
        _moxfield_deck_with_cards("d5", ["Common Pick"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )
    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(), n=5,
    )
    add_cards = {r.card for r in recs if r.action == "add"}
    assert "Common Pick" in add_cards         # in 5/5 — kept
    assert "Rare Pick" not in add_cards       # in only 1/5 — dropped


def test_bracket_peers_respects_explicit_min_refs(monkeypatch):
    """Caller can override the default threshold."""
    from commander_builder.improvement_advisor import _bracket_peers_recommendations
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Card A", "Card B"]),
        _moxfield_deck_with_cards("d2", ["Card A"]),
        _moxfield_deck_with_cards("d3", ["Card A"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )
    # min_refs=1 — include singletons
    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(), n=3, min_refs=1,
    )
    add_cards = {r.card for r in recs if r.action == "add"}
    assert "Card B" in add_cards     # 1/3 — kept under min_refs=1
    # min_refs=3 — only ALL-refs survive
    recs2, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(), n=3, min_refs=3,
    )
    add_cards2 = {r.card for r in recs2 if r.action == "add"}
    assert "Card A" in add_cards2
    assert "Card B" not in add_cards2  # 1/3 — dropped under min_refs=3


def test_bracket_peers_reranks_by_diagnosis_priority_roles(monkeypatch):
    """Phase A gap #5: when the deck's diagnosis flags weakness signals
    that map to priority roles, the bracket-peers recommender should
    surface those roles first (matches the heuristic path's behavior)."""
    from commander_builder.improvement_advisor import (
        _bracket_peers_recommendations,
    )
    from commander_builder.improvement_advisor import DeckDiagnosis

    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Ramp Card", "Finisher Card"]),
        _moxfield_deck_with_cards("d2", ["Ramp Card", "Finisher Card"]),
    ]

    def fake_lookup(name):
        if name == "Ramp Card":
            return {"oracle_text": "Add {G}", "type_line": "Sorcery"}
        if name == "Finisher Card":
            return {
                "oracle_text": "Target opponent loses the game.",
                "type_line": "Sorcery",
            }
        return {"oracle_text": "", "type_line": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )

    # Diagnosis says deck has no closer → finisher / win_condition role
    # should surface first. After the 2026-05-13 role-classifier
    # consolidation, "Target opponent loses the game" tags as
    # ``win_condition`` (more specific than the base ``finisher``).
    # priority_roles includes both so the rerank floats either label.
    diagnosis = DeckDiagnosis(priority_roles=["win_condition", "finisher", "wipe"])
    recs, _ = _bracket_peers_recommendations(
        commander_name="X", bracket=4, deck_cards=set(),
        diagnosis=diagnosis, min_refs=1,
    )
    adds = [r for r in recs if r.action == "add"]
    # First add must be a closer (win_condition is the consolidated
    # label) because diagnosis priority kicks it ahead of ramp.
    assert adds[0].evidence.get("role") == "win_condition"


def test_peer_card_frequency_counts_each_deck_once_per_card():
    """Helper bug fix: basics that appear N times in one deck's
    mainboard count as 1 toward that card's reference frequency, not
    N. Previously _collect_bracket_peer_summary duplicated frequency
    logic with the wrong semantics — Forest x30 in a single ref
    inflated the prompt's 'in_n_refs' figure into nonsense."""
    from commander_builder.improvement_advisor import _peer_card_frequency
    fake_decks = [
        # Deck 1: 30 copies of "Forest" + 1 "Moat".
        {"boards": {"mainboard": {"cards": {
            **{f"forest-{i}": {"card": {"name": "Forest"}, "quantity": 1}
               for i in range(30)},
            "moat": {"card": {"name": "Moat"}, "quantity": 1},
        }}}},
        # Deck 2: 30 Forests + 1 Moat.
        {"boards": {"mainboard": {"cards": {
            **{f"forest-{i}": {"card": {"name": "Forest"}, "quantity": 1}
               for i in range(30)},
            "moat": {"card": {"name": "Moat"}, "quantity": 1},
        }}}},
    ]
    freq, case_map = _peer_card_frequency(fake_decks)
    # Each card counts once per deck regardless of duplicates within.
    assert freq["forest"] == 2
    assert freq["moat"] == 2
    # Case is preserved (first-seen wins).
    assert case_map["forest"] == "Forest"


def test_peer_card_frequency_empty_list():
    """No decks → empty Counter + empty map, never raises."""
    from commander_builder.improvement_advisor import _peer_card_frequency
    freq, case_map = _peer_card_frequency([])
    assert dict(freq) == {}
    assert case_map == {}


def test_collect_bracket_peer_summary_builds_frequency_map(monkeypatch):
    """The summary used by the Claude prompt is a compact frequency
    representation — for each card that appears in any reference, the
    count of references containing it. Lets Claude see at a glance
    'this card is in 5/5 references but missing here' without parsing
    full decklists."""
    from commander_builder.improvement_advisor import (
        _collect_bracket_peer_summary_for_prompt,
    )
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Moat", "Last March", "Sol Ring"]),
        _moxfield_deck_with_cards("d2", ["Moat", "Last March"]),
        _moxfield_deck_with_cards("d3", ["Moat"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    summary = _collect_bracket_peer_summary_for_prompt(
        "The Ur-Dragon", bracket=4, n=5,
    )
    assert summary is not None
    assert summary["ref_count"] == 3
    by_name = {c["name"]: c["in_n_refs"] for c in summary["cards_by_frequency"]}
    assert by_name["Moat"] == 3
    assert by_name["Last March"] == 2
    assert by_name["Sol Ring"] == 1


def test_collect_bracket_peer_summary_returns_none_when_no_refs(monkeypatch):
    """Empty references → None so callers can detect 'no peer data
    available' and skip enrichment."""
    from commander_builder.improvement_advisor import (
        _collect_bracket_peer_summary_for_prompt,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: [],
    )
    assert _collect_bracket_peer_summary_for_prompt(
        "X", bracket=4, n=5,
    ) is None


def test_collect_bracket_peer_summary_includes_ref_metadata(monkeypatch):
    """The summary should carry minimal reference-deck metadata
    (publicId + name) so Claude can cite which references support a
    recommendation in its rationale."""
    from commander_builder.improvement_advisor import (
        _collect_bracket_peer_summary_for_prompt,
    )
    fake_refs = [
        {"publicId": "abc-123", "name": "The Ur-Dragon Strawman",
         "boards": {"mainboard": {"cards": {
             "x": {"card": {"name": "Moat"}, "quantity": 1},
         }}}},
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )
    summary = _collect_bracket_peer_summary_for_prompt(
        "X", bracket=4, n=5,
    )
    assert len(summary["ref_metadata"]) == 1
    assert summary["ref_metadata"][0]["public_id"] == "abc-123"
    assert summary["ref_metadata"][0]["name"] == "The Ur-Dragon Strawman"


def test_claude_prompt_includes_bracket_peer_data_when_available(
    tmp_path, monkeypatch,
):
    """End-to-end: when source='claude' and bracket-peer references
    are fetchable, the request body sent to Anthropic includes them
    so Claude can reason about 'what's missing from this deck vs.
    same-bracket peers'. Pinning this prevents a future refactor
    from silently dropping the enrichment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["Ur-Dragon"], main=["Sol Ring", "Cultivate"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(
            top=[("Coat of Arms", 60.0)], synergy=[],
        ),
    )
    fake_refs = [
        _moxfield_deck_with_cards("d1", ["Moat", "Last March"]),
        _moxfield_deck_with_cards("d2", ["Moat", "Last March"]),
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )

    captured_message = {}

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class _M:
        def create(self, **kw):
            captured_message["user"] = kw["messages"][0]["content"]
            captured_message["system"] = kw.get("system", "")
            return _Msg(json.dumps({
                "rationale": "ok", "added": [], "removed": [],
            }))
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _M()
    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    advise(deck, bracket=4, source="claude",
           deck_dir=deck_dir, match_dir=deck_dir)

    # User message must include bracket-peer reference data.
    user_payload = captured_message["user"]
    assert "bracket_peer_references" in user_payload
    assert "Moat" in user_payload
    # System prompt must mention how to use the references.
    assert "bracket_peer_references" in captured_message["system"] \
        or "reference deck" in captured_message["system"].lower()


def test_advise_claude_records_peer_ref_count_on_report(tmp_path, monkeypatch):
    """When the Claude path successfully attaches bracket-peer refs to
    its prompt, the AdviceReport exposes how many references were used.
    Lets the UI disclose 'Claude analyst (5 peer refs)' so users can
    tell when the LLM had archetype data vs. just EDHREC averages."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )
    fake_refs = [
        _moxfield_deck_with_cards(f"d{i}", ["Card A"]) for i in range(3)
    ]
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: fake_refs,
    )

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class _M:
        def create(self, **kw):
            return _Msg(json.dumps({
                "rationale": "ok", "added": [], "removed": [],
            }))
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _M()
    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    report = advise(deck, bracket=4, source="claude",
                    deck_dir=deck_dir, match_dir=deck_dir)
    assert report.bracket_peer_ref_count == 3


def test_advise_claude_peer_ref_count_zero_when_no_refs(tmp_path, monkeypatch):
    """Obscure commander → no Moxfield references → count stays 0,
    matches the dataclass default. UI can then suppress the
    '(N peer refs)' suffix entirely."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["Obscure"], main=["Sol Ring"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: [],
    )

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class _M:
        def create(self, **kw):
            return _Msg(json.dumps({
                "rationale": "ok", "added": [], "removed": [],
            }))
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _M()
    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    report = advise(deck, bracket=4, source="claude",
                    deck_dir=deck_dir, match_dir=deck_dir)
    assert report.bracket_peer_ref_count == 0


def test_claude_prompt_omits_peer_section_when_no_refs(tmp_path, monkeypatch):
    """When Moxfield returns no references, Claude still runs — just
    without the peer-data section. Backward-compat for obscure
    commanders + the graceful degradation case."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["Obscure"], main=["Sol Ring"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.find_top_liked_decks_for_commander",
        lambda *a, **kw: [],   # no references found
    )

    captured_message = {}

    class _Block:
        def __init__(self, t): self.text = t
    class _Msg:
        def __init__(self, t): self.content = [_Block(t)]
    class _M:
        def create(self, **kw):
            captured_message["user"] = kw["messages"][0]["content"]
            return _Msg(json.dumps({
                "rationale": "ok", "added": [], "removed": [],
            }))
    class FakeClient:
        def __init__(self, **kw): pass
        @property
        def messages(self): return _M()
    import sys, types
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {"oracle_text": "", "type_line": ""},
    )

    advise(deck, bracket=4, source="claude",
           deck_dir=deck_dir, match_dir=deck_dir)
    user_payload = captured_message["user"]
    # The user payload should not include a non-empty peer section.
    # We're tolerant of an explicit None / "null" — what matters is
    # that the absence isn't accompanied by spurious peer card names.
    # Specifically: no card names from the synthetic ref fixtures used
    # in other tests should leak through.
    assert "Moat" not in user_payload
    assert "Last March" not in user_payload


def test_missing_manabase_recommendations_for_five_color(monkeypatch):
    """5-color deck missing ABU duals → all 10 surface as adds."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    # Deck contains only Sol Ring — none of the ABU duals.
    deck_cards = {"Sol Ring"}
    recs = _missing_manabase_recommendations(deck_cards, {"W", "U", "B", "R", "G"})
    add_cards = {r.card for r in recs}
    for dual in ["Bayou", "Savannah", "Tropical Island", "Volcanic Island"]:
        assert dual in add_cards


def test_missing_manabase_skips_already_owned_lands(monkeypatch):
    """The user's existing duals shouldn't surface as adds. Case-
    insensitive matching so `bayou` in the deck matches `Bayou` in
    the essentials map."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    deck_cards = {"Bayou", "Badlands", "Plateau"}  # owns 3 duals
    recs = _missing_manabase_recommendations(deck_cards, {"W", "U", "B", "R", "G"})
    add_cards = {r.card for r in recs}
    assert "Bayou" not in add_cards
    assert "Badlands" not in add_cards
    assert "Plateau" not in add_cards
    # But missing duals still surface.
    assert "Tundra" in add_cards


def test_missing_manabase_off_color_lands_filtered(monkeypatch):
    """Mono-red deck shouldn't see Bayou (BG) or Plateau (RW) but
    SHOULD see no duals at all (all are 2-color, and red+anything-
    else isn't in mono-red's identity)."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    deck_cards = {"Mountain"}
    recs = _missing_manabase_recommendations(deck_cards, {"R"})
    add_cards = {r.card for r in recs}
    assert "Bayou" not in add_cards
    assert "Plateau" not in add_cards


def test_missing_manabase_recs_tagged_as_land_role(monkeypatch):
    """Each manabase rec should carry evidence.role='land' so the UI
    can group it with other land suggestions, and evidence.source so
    the user knows it came from the curated safety net (not a peer
    reference)."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    recs = _missing_manabase_recommendations({}, {"W", "G"})
    assert recs, "Expected some recs for WG deck"
    for r in recs:
        assert r.action == "add"
        assert r.evidence.get("role") == "land"
        assert r.evidence.get("source") == "manabase_essentials"


def test_missing_manabase_includes_tribal_lands_for_dragon_commander():
    """Tribal commander → Cavern of Souls + Path of Ancestry + etc.
    surface as adds alongside the color-gated duals/fetches/etc."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    recs = _missing_manabase_recommendations(
        {"Mountain"}, {"W", "U", "B", "R", "G"}, tribe="Dragon",
    )
    add_cards = {r.card for r in recs}
    assert "Cavern of Souls" in add_cards
    assert "Path of Ancestry" in add_cards
    # Tagged so the UI can group these separately.
    tribal_recs = [r for r in recs
                   if (r.evidence or {}).get("source") == "tribal_essentials"]
    assert tribal_recs
    assert all(r.evidence.get("tribe") == "Dragon" for r in tribal_recs)


def test_missing_manabase_no_tribal_lands_when_tribe_none():
    """Non-tribal commander → no Cavern of Souls / Path of Ancestry."""
    from commander_builder.improvement_advisor import (
        _missing_manabase_recommendations,
    )
    recs = _missing_manabase_recommendations(
        {"Mountain"}, {"R"}, tribe=None,
    )
    add_cards = {r.card for r in recs}
    assert "Cavern of Souls" not in add_cards
    assert "Path of Ancestry" not in add_cards


def test_advise_recommends_cavern_of_souls_for_tribal_commander(
    tmp_path, monkeypatch,
):
    """End-to-end: a 5-color Dragon-tribal commander (Ur-Dragon) →
    Cavern of Souls surfaces in the recommendations. Pinning the
    user's stated requirement: "tribal decks should have cavern of
    souls.\""""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] X [B4].dck",
        commanders=["The Ur-Dragon"], main=["Sol Ring"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(top=[], synergy=[]),
    )
    # Commander oracle mentions "Dragon" multiple times → tribal.
    ur_dragon_oracle = (
        "Eminence — As long as The Ur-Dragon is in the command zone "
        "or on the battlefield, other Dragon spells you cast cost 1 "
        "less to cast. Flying. Whenever one or more Dragons you "
        "control attack, draw a card for each."
    )
    def fake_lookup(name):
        if name == "The Ur-Dragon":
            return {
                "oracle_text": ur_dragon_oracle,
                "type_line": "Legendary Creature — Dragon Avatar",
                "color_identity": ["W", "U", "B", "R", "G"],
            }
        return {"oracle_text": "", "type_line": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    report = advise(deck, bracket=4,
                    deck_dir=deck_dir, match_dir=deck_dir)
    add_cards = {r.card for r in report.recommendations if r.action == "add"}
    assert "Cavern of Souls" in add_cards


def test_advise_appends_manabase_essentials_to_heuristic_path(
    tmp_path, monkeypatch,
):
    """Integration: advise(source='heuristic') should now include the
    color-identity-appropriate manabase essentials in its
    recommendations, prepended before / mixed with EDHREC adds."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    # 5-color Ur-Dragon deck missing every dual.
    deck = _write_dck(
        deck_dir, "[USER] X [B3].dck",
        commanders=["The Ur-Dragon"], main=["Sol Ring", "Some Filler"],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: _fake_edhrec_page(
            top=[("Coat of Arms", 60.0)], synergy=[],
        ),
    )
    # Stub the commander color-identity lookup → WUBRG.
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name: {
            "oracle_text": "",
            "type_line": "Creature — Dragon Avatar",
            "color_identity": ["W", "U", "B", "R", "G"],
        },
    )
    report = advise(deck, bracket=3,
                    deck_dir=deck_dir, match_dir=deck_dir)
    add_cards = {r.card for r in report.recommendations if r.action == "add"}
    # At least one ABU dual should surface — pinning the integration,
    # not enumerating all 10 (_apply_swaps_to_dck balancing may trim).
    assert any(d in add_cards for d in [
        "Bayou", "Savannah", "Tropical Island", "Volcanic Island",
    ])


def test_advise_saturation_filter_preserves_when_threshold_not_hit(
    tmp_path, monkeypatch,
):
    """Don't break the existing happy path: a normal deck with 8 ramp
    pieces (under the threshold of 12) should still receive ramp
    recommendations."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] NormalDeck [B3].dck",
        commanders=["Some Commander"],
        main=[f"Ramp {i}" for i in range(1, 9)] + ["Filler"],  # 8 ramp
    )
    fake_page = _fake_edhrec_page(
        top=[("Cultivate", 90.0)], synergy=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )
    def fake_lookup(name):
        if name == "Cultivate":
            return {
                "oracle_text": "Search your library for a basic land card",
                "type_line": "Sorcery",
            }
        if name.startswith("Ramp "):
            return {"oracle_text": "Add {G}", "type_line": "Artifact"}
        return {"oracle_text": "", "type_line": ""}
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card", fake_lookup,
    )
    monkeypatch.setattr(
        "commander_builder.staples.lookup_card", fake_lookup,
    )
    report = advise(deck, bracket=3,
                    deck_dir=deck_dir, match_dir=deck_dir)
    add_names = {r.card for r in report.recommendations if r.action == "add"}
    assert "Cultivate" in add_names  # not saturated, kept


# --- commander-advise CLI flags --------------------------------------------

def test_cli_source_flag_routes_to_bracket_peers(tmp_path, monkeypatch, capsys):
    """`commander-advise --source bracket_peers` must call advise()
    with source='bracket_peers' so the CLI matches the web-app
    Source dropdown's expressive power. Was a gap from the original
    #8 self-audit."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] Hakbal [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        seen["claude_model"] = kwargs.get("claude_model")
        return AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Hakbal"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    # Ensure relative-path resolution finds the test deck.
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.DECK_DIR", deck_dir,
    )
    rc = _advise_main([
        "--user", deck.name,
        "--bracket", "3",
        "--source", "bracket_peers",
    ])
    assert rc == 0
    assert seen["source"] == "bracket_peers"


def test_cli_use_claude_legacy_flag_still_maps_to_claude(
    tmp_path, monkeypatch,
):
    """The legacy --use-claude flag continues to work — existing
    scripts shouldn't break."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] Hakbal [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        return AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Hakbal"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.DECK_DIR", deck_dir,
    )
    _advise_main([
        "--user", deck.name, "--bracket", "3", "--use-claude",
    ])
    assert seen["source"] == "claude"


def test_cli_source_overrides_use_claude_when_both_passed(
    tmp_path, monkeypatch,
):
    """When both --source and --use-claude appear, --source wins —
    it's the more expressive new-style argument."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] Hakbal [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["source"] = kwargs.get("source")
        return AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Hakbal"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.DECK_DIR", deck_dir,
    )
    _advise_main([
        "--user", deck.name, "--bracket", "3",
        "--use-claude", "--source", "bracket_peers",
    ])
    assert seen["source"] == "bracket_peers"


def test_cli_budget_flag_forwards_to_advise(tmp_path, monkeypatch):
    """--budget on the CLI should set advise(budget=True) so users can
    opt out of expensive manabase recommendations without touching the
    web UI."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] Hakbal [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["budget"] = kwargs.get("budget", False)
        return AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Hakbal"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.DECK_DIR", deck_dir,
    )
    # Without --budget → False.
    _advise_main(["--user", deck.name, "--bracket", "3"])
    assert seen["budget"] is False
    # With --budget → True.
    _advise_main(["--user", deck.name, "--bracket", "3", "--budget"])
    assert seen["budget"] is True


def test_cli_claude_model_flag_forwards_to_advise(tmp_path, monkeypatch):
    """--claude-model claude-haiku-4-5 must flow to advise() so users
    can pick the cheap tier from the CLI."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = _write_dck(
        deck_dir, "[USER] Hakbal [B3].dck",
        commanders=["Hakbal"], main=["Sol Ring"],
    )
    seen = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen["claude_model"] = kwargs.get("claude_model")
        return AdviceReport(
            deck_filename=deck_path.name, deck_id=None, bracket=bracket,
            commander_names=["Hakbal"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.DECK_DIR", deck_dir,
    )
    _advise_main([
        "--user", deck.name, "--bracket", "3",
        "--source", "claude",
        "--claude-model", "claude-haiku-4-5",
    ])
    assert seen["claude_model"] == "claude-haiku-4-5"
