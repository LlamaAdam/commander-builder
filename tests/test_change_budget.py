"""Tests for the adaptive change budget (``change_budget.py``).

Covers the tier boundary mapping (34/35/54/55/74/75 + None fallback),
the manabase-rebuild planner (offline, stubbed lookups) and its
staging through the normal legality path, ``--mode auto`` resolution
printing on commander-auto-curate and commander-advise, the rebuild
budget plumbing (injected proposer), and the byte-invariance of
default runs when auto is not requested.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from commander_builder.change_budget import (
    FALLBACK_TIER,
    TIER_CAPS,
    BudgetTier,
    format_auto_mode_line,
    plan_manabase_rebuild,
    resolve_tier,
    suggested_mode_payload,
    trim_recommendations,
)


# ---------------------------------------------------------------------------
# Tier boundary mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected_mode", [
    (34, "rebuild"),
    (35, "overhaul"),
    (54, "overhaul"),
    (55, "polish"),
    (74, "polish"),
    (75, "keep"),
    (0, "rebuild"),
    (100, "keep"),
])
def test_resolve_tier_boundaries(score, expected_mode):
    tier = resolve_tier(score)
    assert tier.mode == expected_mode
    assert (tier.max_adds, tier.max_cuts) == TIER_CAPS[expected_mode]
    assert tier.health_score == score
    assert tier.fallback is False


def test_resolve_tier_none_score_falls_back_to_polish():
    """Score unavailable (outage / empty deck) -> polish fallback,
    flagged so callers print the note. Never an escalation on missing
    data."""
    tier = resolve_tier(None)
    assert tier.mode == FALLBACK_TIER == "polish"
    assert (tier.max_adds, tier.max_cuts) == TIER_CAPS["polish"] == (5, 5)
    assert tier.health_score is None
    assert tier.fallback is True


def test_tier_caps_preserve_historical_presets():
    """polish/overhaul/free must stay byte-identical to the historical
    commander-auto-curate presets (TIER_CAPS is now their single
    source of truth); keep mirrors bubble_analysis's 0-2 band; rebuild
    is the new widest tier."""
    assert TIER_CAPS["polish"] == (5, 5)
    assert TIER_CAPS["overhaul"] == (15, 15)
    assert TIER_CAPS["free"] == (999, 999)
    assert TIER_CAPS["keep"] == (2, 2)
    assert TIER_CAPS["rebuild"] == (30, 30)


def test_format_auto_mode_line():
    assert format_auto_mode_line(
        BudgetTier("overhaul", 15, 15, 42),
    ) == "auto mode: overhaul (health 42/100)"
    line = format_auto_mode_line(
        BudgetTier("polish", 5, 5, None, fallback=True),
    )
    assert "auto mode: polish" in line
    assert "unavailable" in line


def test_suggested_mode_payload_shapes():
    assert suggested_mode_payload(42) == {
        "mode": "overhaul", "health_score": 42, "fallback": False,
    }
    assert suggested_mode_payload(None) == {
        "mode": "polish", "health_score": None, "fallback": True,
    }


def test_trim_recommendations_caps_adds_and_cuts_preserving_order():
    from commander_builder.improvement_advisor import SwapRecommendation
    recs = (
        [SwapRecommendation(card=f"Add{i}", action="add", reason="")
         for i in range(8)]
        + [SwapRecommendation(card=f"Cut{i}", action="cut", reason="")
           for i in range(8)]
    )
    trimmed = trim_recommendations(recs, 3, 2)
    assert [r.card for r in trimmed if r.action == "add"] == [
        "Add0", "Add1", "Add2",
    ]
    assert [r.card for r in trimmed if r.action == "cut"] == [
        "Cut0", "Cut1",
    ]
    # Input not mutated.
    assert len(recs) == 16


# ---------------------------------------------------------------------------
# Manabase rebuild planner (offline, stubbed lookups)
# ---------------------------------------------------------------------------

def _fake_lookup(name: str):
    """Offline Scryfall stub for a mono-skewed BG deck."""
    if "Forest" in name:
        return {"type_line": "Basic Land — Forest", "mana_cost": "",
                "color_identity": ["G"], "produced_mana": ["G"]}
    if "Swamp" in name:
        return {"type_line": "Basic Land — Swamp", "mana_cost": "",
                "color_identity": ["B"], "produced_mana": ["B"]}
    if name == "Test Cmdr BG":
        return {"type_line": "Legendary Creature — Elf Warlock",
                "mana_cost": "{2}{B}{G}", "color_identity": ["B", "G"]}
    if name == "Dark Rite":
        return {"type_line": "Sorcery", "mana_cost": "{B}{B}",
                "color_identity": ["B"]}
    if name == "Nature Class":
        return {"type_line": "Sorcery", "mana_cost": "{G}",
                "color_identity": ["G"]}
    return None  # unknown (e.g. staples fixer candidates) -> miss


_REBUILD_DECK = (
    "[metadata]\n"
    "Name=Rebuild Test\n"
    "[Commander]\n"
    "1 Test Cmdr BG\n"
    "[Main]\n"
    "40 Dark Rite\n"
    "21 Nature Class\n"
    "38 Forest\n"
)


def test_plan_manabase_rebuild_offline_balanced_swaps():
    """A BG deck with heavy black pips and an all-Forest manabase gets
    Swamps in; the plan is balanced (adds == cuts, land-count-neutral)
    and touches only lands."""
    plan = plan_manabase_rebuild(_REBUILD_DECK, lookup=_fake_lookup)
    assert plan is not None
    assert len(plan["adds"]) == len(plan["cuts"]) > 0
    assert "Swamp" in plan["adds"]
    # Cuts come from the CURRENT manabase only.
    assert set(plan["cuts"]) == {"Forest"}
    assert isinstance(plan["summary"], list)


def test_plan_manabase_rebuild_none_on_outage():
    """Majority-lookup failure -> None (the manabase_report outage
    contract): never rebuild on data we don't have."""
    plan = plan_manabase_rebuild(_REBUILD_DECK, lookup=lambda _n: None)
    assert plan is None


def test_plan_manabase_rebuild_none_on_empty_deck():
    assert plan_manabase_rebuild("", lookup=_fake_lookup) is None


def test_plan_manabase_rebuild_stages_through_legality_path(
    tmp_path, monkeypatch,
):
    """The plan's swaps, staged through the SAME legality path every
    curated change uses (``apply_proposal_to_deck``), produce a legal
    99-card mainboard. Offline: Scryfall stubbed."""
    from commander_builder.dck_utils import count_main_cards
    from commander_builder.proposer import Proposal, apply_proposal_to_deck

    def _kw_lookup(name, **_kw):
        return _fake_lookup(name)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", _kw_lookup,
    )

    deck = tmp_path / "[USER] Rebuild Test [B3].dck"
    deck.write_text(_REBUILD_DECK, encoding="utf-8")
    plan = plan_manabase_rebuild(_REBUILD_DECK, lookup=_fake_lookup)
    assert plan is not None and plan["adds"]

    proposal = Proposal(
        adds=list(plan["adds"]), cuts=list(plan["cuts"]),
        rationale="manabase rebuild",
    )
    out_path = apply_proposal_to_deck(deck, proposal, dry_run=False)
    out_text = out_path.read_text(encoding="utf-8")
    assert count_main_cards(out_text) == 99
    assert "Swamp" in out_text
    # Every planned swap landed (balanced pairs, all names resolvable).
    assert len(proposal.applied_adds) == len(plan["adds"])
    assert len(proposal.applied_cuts) == len(plan["cuts"])


# ---------------------------------------------------------------------------
# commander-auto-curate: --mode auto resolution + rebuild plumbing
# (test names use the test_auto_curate_main_ prefix so conftest's
# auto-slow tagging applies, matching the existing CLI-test family)
# ---------------------------------------------------------------------------

def _write_min_deck(tmp_path) -> Path:
    p = tmp_path / "[USER] Budget Test [B3].dck"
    p.write_text(_REBUILD_DECK, encoding="utf-8")
    return p


def _stub_pipeline(monkeypatch, seen: dict):
    """Stub advise + auto_propose + apply so auto_curate_main runs the
    argv/threading logic without EDHREC, Claude, or Scryfall."""
    from commander_builder.improvement_advisor import AdviceReport
    from commander_builder.proposer import Proposal

    def fake_advise(deck_path, bracket, **kwargs):
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Cmdr BG"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    def fake_auto_propose(**kwargs):
        seen["auto_propose_kwargs"] = kwargs
        return Proposal(adds=[], cuts=[], rationale="stub")
    monkeypatch.setattr(
        "commander_builder._proposer_cli.auto_propose", fake_auto_propose,
    )

    def fake_apply(deck_path, proposal, dry_run=False):
        seen["applied_proposal"] = proposal
        return deck_path
    monkeypatch.setattr(
        "commander_builder._proposer_cli.apply_proposal_to_deck", fake_apply,
    )


def test_auto_curate_main_auto_mode_resolves_and_prints(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)
    # Health score 42 -> overhaul (35-54 band).
    monkeypatch.setattr(
        "commander_builder.change_budget.health_score_for_deck",
        lambda _text: 42,
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--mode", "auto", "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto mode: overhaul (health 42/100)" in out
    # The resolved tier drives the caps AND the curator's mode hint.
    assert seen["auto_propose_kwargs"]["mode"] == "overhaul"
    assert seen["auto_propose_kwargs"]["max_adds"] == 15
    assert seen["auto_propose_kwargs"]["max_cuts"] == 15


def test_auto_curate_main_auto_mode_none_score_falls_back_to_polish(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)
    monkeypatch.setattr(
        "commander_builder.change_budget.health_score_for_deck",
        lambda _text: None,
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--mode", "auto", "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto mode: polish" in out
    assert "unavailable" in out
    assert seen["auto_propose_kwargs"]["mode"] == "polish"
    assert seen["auto_propose_kwargs"]["max_adds"] == 5


def test_auto_curate_main_auto_mode_json_keeps_stdout_parseable(
    tmp_path, monkeypatch, capsys,
):
    """Under --json the disclosure line goes to stderr; stdout stays a
    single JSON document carrying the additive auto keys."""
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)
    monkeypatch.setattr(
        "commander_builder.change_budget.health_score_for_deck",
        lambda _text: 20,
    )
    monkeypatch.setattr(
        "commander_builder.change_budget.plan_manabase_rebuild",
        lambda _text: None,
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--mode", "auto", "--dry-run", "--json",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "auto mode: rebuild (health 20/100)" in captured.err
    payload = json.loads(captured.out)
    assert payload["mode"] == "rebuild"
    assert payload["requested_mode"] == "auto"
    assert payload["auto_health_score"] == 20
    assert "manabase_rebuild" in payload


def test_auto_curate_main_rebuild_budget_and_manabase_plumbing(
    tmp_path, monkeypatch, capsys,
):
    """Explicit --mode rebuild: 30+30 caps reach the injected proposer
    and the manabase plan's swaps are appended to the proposal that
    flows through the normal apply path."""
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)
    monkeypatch.setattr(
        "commander_builder.change_budget.plan_manabase_rebuild",
        lambda _text: {
            "adds": ["Swamp", "Swamp"], "cuts": ["Forest", "Forest"],
            "summary": [],
        },
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--mode", "rebuild", "--dry-run",
        "--json",
    ])
    assert rc == 0
    assert seen["auto_propose_kwargs"]["mode"] == "rebuild"
    assert seen["auto_propose_kwargs"]["max_adds"] == 30
    assert seen["auto_propose_kwargs"]["max_cuts"] == 30
    # The staged proposal (what apply saw) carries the land swaps.
    assert seen["applied_proposal"].adds == ["Swamp", "Swamp"]
    assert seen["applied_proposal"].cuts == ["Forest", "Forest"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["manabase_rebuild"] == {
        "adds": ["Swamp", "Swamp"], "cuts": ["Forest", "Forest"],
    }


def test_auto_curate_main_no_manabase_rebuild_flag_skips_step(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)

    def _boom(_text):
        raise AssertionError("plan_manabase_rebuild must not be called")
    monkeypatch.setattr(
        "commander_builder.change_budget.plan_manabase_rebuild", _boom,
    )

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--mode", "rebuild",
        "--no-manabase-rebuild", "--dry-run", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["manabase_rebuild"] is None
    assert seen["applied_proposal"].adds == []


def test_auto_curate_main_default_json_payload_unchanged(
    tmp_path, monkeypatch, capsys,
):
    """No --mode auto / rebuild -> the JSON payload carries NO
    adaptive-budget keys (the byte-invariance contract for default
    runs)."""
    from commander_builder._proposer_cli import auto_curate_main
    deck = _write_min_deck(tmp_path)
    seen: dict = {}
    _stub_pipeline(monkeypatch, seen)

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--dry-run", "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "polish"
    assert "requested_mode" not in payload
    assert "auto_health_score" not in payload
    assert "manabase_rebuild" not in payload


# ---------------------------------------------------------------------------
# commander-advise: --mode auto resolution + trimming
# ---------------------------------------------------------------------------

def test_advise_cli_auto_mode_prints_and_trims(tmp_path, monkeypatch, capsys):
    from commander_builder import improvement_advisor as ia
    from commander_builder.improvement_advisor import (
        AdviceReport,
        SwapRecommendation,
    )

    deck = tmp_path / "[USER] Advise Budget [B3].dck"
    deck.write_text(_REBUILD_DECK, encoding="utf-8")

    recs = (
        [SwapRecommendation(card=f"AddCard{i:02d}", action="add", reason="r")
         for i in range(20)]
        + [SwapRecommendation(card=f"CutCard{i:02d}", action="cut", reason="r")
           for i in range(20)]
    )

    def fake_advise(deck_path, bracket, **kwargs):
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Cmdr BG"],
            recommendations=list(recs),
        )
    monkeypatch.setattr(ia, "advise", fake_advise)
    # Health 42 -> overhaul (15+15); the CLI reuses the header's grade.
    monkeypatch.setattr(
        "commander_builder.deck_health.compute_health_grade",
        lambda _text: {
            "grade": "D", "score": 42, "reasons": [], "components": {},
        },
    )

    rc = ia.main([
        "--user", str(deck), "--bracket", "3", "--mode", "auto",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto mode: overhaul (health 42/100)" in out
    # Trimmed to the overhaul budget: first 15 adds / 15 cuts survive.
    assert "AddCard14" in out
    assert "AddCard15" not in out
    assert "CutCard14" in out
    assert "CutCard15" not in out


def test_advise_cli_without_mode_is_untrimmed(tmp_path, monkeypatch, capsys):
    from commander_builder import improvement_advisor as ia
    from commander_builder.improvement_advisor import (
        AdviceReport,
        SwapRecommendation,
    )

    deck = tmp_path / "[USER] Advise Default [B3].dck"
    deck.write_text(_REBUILD_DECK, encoding="utf-8")

    def fake_advise(deck_path, bracket, **kwargs):
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Cmdr BG"],
            recommendations=[
                SwapRecommendation(
                    card=f"AddCard{i:02d}", action="add", reason="r",
                )
                for i in range(20)
            ],
        )
    monkeypatch.setattr(ia, "advise", fake_advise)

    rc = ia.main(["--user", str(deck), "--bracket", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "auto mode:" not in out
    assert "AddCard19" in out  # nothing trimmed


# ---------------------------------------------------------------------------
# commander-improve: pass-through of the new modes
# ---------------------------------------------------------------------------

def test_improve_forwards_auto_mode_and_manabase_flag(monkeypatch, tmp_path):
    """commander-improve accepts --mode auto / --no-manabase-rebuild
    and forwards both to the per-round auto-curate argv."""
    from types import SimpleNamespace

    from commander_builder.improve import _default_round_fn

    captured: dict = {}

    def fake_auto_curate_main(argv):
        captured["argv"] = argv
        print(json.dumps({
            "proposal": {"applied_adds": [], "applied_cuts": []},
            "sim_report": {}, "output_deck": None,
        }))
        return 0
    monkeypatch.setattr(
        "commander_builder._proposer_cli.auto_curate_main",
        fake_auto_curate_main,
    )

    deck = tmp_path / "[USER] Improve Budget [B3].dck"
    deck.write_text(_REBUILD_DECK, encoding="utf-8")
    args = SimpleNamespace(
        bracket=3, mode="auto", source="heuristic",
        model="claude-sonnet-4-5", sim_games=5, sim_margin=1,
        sim_fillers=None, db_path=None, protect=[], protect_from=None,
        intent=None, no_manabase_rebuild=True,
    )
    _default_round_fn(deck, 1, args)
    argv = captured["argv"]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "auto"
    assert "--no-manabase-rebuild" in argv
