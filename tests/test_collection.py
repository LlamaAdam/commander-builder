"""Tests for the owned-card collection registry (ManaFoundry parity).

Covers, bottom-up:

- ``collection.py`` core: name-key normalization (case fold + DFC
  front face), plain/CSV imports (CSV reuses ``import_formats``),
  save/load round trip, the None-vs-empty contract, implicit basic-
  land ownership.
- The advisor filter (``_filter_for_ownership``): exclude drops,
  flag annotates, ``None`` collection is a pass-through.
- The proposer filter (``enforce_ownership``): same contract in the
  ``(kept, dropped)`` shape the auto_propose chain uses.
- Full ``advise()`` pipeline behavior (marked slow, like the rest of
  the advise-pipeline suite): the no-collection snapshot pin, flag
  annotation, and exclude mode with skipped_for_ownership.
- CLI flag threading for commander-advise and commander-auto-curate
  (advise/auto_propose stubbed — fast lane).
- Web: /api/collection GET/PUT round trip + clearing, and the
  ``owned_only`` query param threading through /api/audit.

Isolation: the autouse ``_isolate_collection_path`` fixture in
conftest.py points ``COMMANDER_BUILDER_COLLECTION`` at a nonexistent
per-test tmp file, so these tests write THAT path (or pass explicit
paths) and never touch the real ``~/.commander-builder/`` user dir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder import collection


def _registered_path() -> Path:
    """The per-test collection path the conftest autouse fixture
    installed via COMMANDER_BUILDER_COLLECTION. Writing here is how a
    test 'registers' a collection through the default accessor."""
    return collection.collection_path()


def _register(names: list[str]) -> Path:
    p = _registered_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(names) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# name_key + owns
# ---------------------------------------------------------------------------

def test_name_key_case_folds():
    assert collection.name_key("Sol RING  ") == "sol ring"


def test_name_key_folds_dfc_to_front_face():
    # Both the full Scryfall form and the bare front face must land on
    # the same key — collection exports vs. advisor recs drift here.
    assert (
        collection.name_key("Malakir Rebirth // Malakir Mire")
        == collection.name_key("malakir rebirth")
        == "malakir rebirth"
    )


def test_owns_matches_registered_key():
    keys = frozenset({"sol ring"})
    assert collection.owns(keys, "SOL RING")
    assert not collection.owns(keys, "Rhystic Study")


def test_owns_treats_basic_lands_as_always_owned():
    empty = frozenset()
    for basic in ("Island", "wastes", "Snow-Covered Forest"):
        assert collection.owns(empty, basic), basic
    # A nonbasic land is NOT implicitly owned.
    assert not collection.owns(empty, "Command Tower")


# ---------------------------------------------------------------------------
# Parsing (plain lines + CSV via import_formats)
# ---------------------------------------------------------------------------

def test_parse_collection_lines_strips_qty_comments_blanks():
    text = (
        "# my collection\n"
        "\n"
        "Sol Ring\n"
        "3 Lightning Bolt\n"
        "  2 Rhystic Study  \n"
    )
    assert collection.parse_collection_lines(text) == [
        "Sol Ring", "Lightning Bolt", "Rhystic Study",
    ]


def test_parse_collection_lines_dedupes_by_key_keeps_first_casing():
    text = "Sol Ring\nSOL RING\nMalakir Rebirth // Malakir Mire\nmalakir rebirth\n"
    assert collection.parse_collection_lines(text) == [
        "Sol Ring", "Malakir Rebirth // Malakir Mire",
    ]


def test_parse_collection_text_plain_passthrough():
    assert collection.parse_collection_text("Sol Ring\n1 Brainstorm\n") == [
        "Sol Ring", "Brainstorm",
    ]


def test_parse_collection_text_csv_reuses_import_formats():
    # A ManaFoundry/Moxfield-style collection CSV: extra columns and
    # per-printing duplicate rows. The CSV path must go through
    # import_formats.csv_to_lines (quoting, BOM, dedup all inherited).
    text = (
        "Count,Name,Edition,Foil\n"
        '1,"Krenko, Mob Boss",M13,No\n'
        "2,Sol Ring,C21,No\n"
        "1,Sol Ring,LTC,Yes\n"
    )
    assert collection.parse_collection_text(text) == [
        "Krenko, Mob Boss", "Sol Ring",
    ]


def test_parse_collection_text_csv_bad_row_raises_import_format_error():
    from commander_builder.import_formats import ImportFormatError
    text = "Count,Name\nnope,Sol Ring\n"
    with pytest.raises(ImportFormatError):
        collection.parse_collection_text(text)


# ---------------------------------------------------------------------------
# load / save / clear + path contract
# ---------------------------------------------------------------------------

def test_load_collection_missing_file_returns_none():
    assert collection.load_collection() is None


def test_load_collection_none_vs_empty_distinction():
    # A present-but-empty file is an EMPTY collection (own nothing),
    # not the inert None sentinel.
    p = _registered_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# nothing yet\n", encoding="utf-8")
    keys = collection.load_collection()
    assert keys is not None and len(keys) == 0


def test_save_and_load_round_trip_normalizes_keys():
    collection.save_collection(
        ["Sol Ring", "Malakir Rebirth // Malakir Mire"],
    )
    keys = collection.load_collection()
    assert keys == frozenset({"sol ring", "malakir rebirth"})


def test_collection_path_env_override_wins(monkeypatch, tmp_path):
    override = tmp_path / "elsewhere" / "cards.txt"
    monkeypatch.setenv("COMMANDER_BUILDER_COLLECTION", str(override))
    assert collection.collection_path() == override


def test_explicit_path_beats_default(tmp_path):
    explicit = tmp_path / "explicit.txt"
    collection.save_collection(["Brainstorm"], path=explicit)
    assert collection.load_collection(explicit) == frozenset({"brainstorm"})
    # The default (registered) path stayed untouched.
    assert collection.load_collection() is None


def test_clear_collection_restores_inert_none():
    _register(["Sol Ring"])
    assert collection.load_collection() is not None
    collection.clear_collection()
    assert collection.load_collection() is None
    # Clearing twice is a no-op, not an error.
    collection.clear_collection()


# ---------------------------------------------------------------------------
# Advisor filter (_filter_for_ownership)
# ---------------------------------------------------------------------------

def _rec(card: str, action: str = "add", evidence=None):
    from commander_builder.improvement_advisor import SwapRecommendation
    return SwapRecommendation(
        card=card, action=action, reason="test",
        evidence=dict(evidence or {}),
    )


def test_ownership_filter_none_collection_is_identity():
    from commander_builder.improvement_advisor import _filter_for_ownership
    recs = [_rec("Rhystic Study"), _rec("Old Card", action="cut")]
    kept, skipped = _filter_for_ownership(recs, None, mode="exclude")
    assert kept is recs and skipped == []
    # No annotation either — evidence stays untouched.
    assert "owned" not in recs[0].evidence


def test_ownership_filter_exclude_drops_unowned_adds_only():
    from commander_builder.improvement_advisor import _filter_for_ownership
    recs = [
        _rec("Sol Ring"),                      # owned
        _rec("Rhystic Study"),                 # unowned -> dropped
        _rec("Island"),                        # basic -> implicitly owned
        _rec("Rhystic Study", action="cut"),   # cuts never filtered
    ]
    kept, skipped = _filter_for_ownership(
        recs, frozenset({"sol ring"}), mode="exclude",
    )
    assert [r.card for r in kept] == ["Sol Ring", "Island", "Rhystic Study"]
    assert kept[-1].action == "cut"
    assert skipped == [{"card": "Rhystic Study", "reason": "not owned"}]
    # Survivors carry the owned=True annotation for a uniform payload.
    assert kept[0].evidence["owned"] is True


def test_ownership_filter_flag_annotates_without_dropping():
    from commander_builder.improvement_advisor import _filter_for_ownership
    recs = [
        _rec("Sol Ring"),
        _rec("Rhystic Study"),
        _rec("Old Card", action="cut"),
    ]
    kept, skipped = _filter_for_ownership(
        recs, frozenset({"sol ring"}), mode="flag",
    )
    assert len(kept) == 3 and skipped == []
    assert kept[0].evidence["owned"] is True
    assert kept[1].evidence["owned"] is False
    # Cuts are never annotated — ownership is moot for in-deck cards.
    assert "owned" not in kept[2].evidence


def test_ownership_filter_dfc_front_face_matches():
    from commander_builder.improvement_advisor import _filter_for_ownership
    # Collection saved from a Scryfall-style export (full DFC name);
    # the advisor recommends the front face. Must match.
    keys = frozenset({collection.name_key("Malakir Rebirth // Malakir Mire")})
    kept, skipped = _filter_for_ownership(
        [_rec("Malakir Rebirth")], keys, mode="exclude",
    )
    assert [r.card for r in kept] == ["Malakir Rebirth"]
    assert skipped == []


# ---------------------------------------------------------------------------
# Proposer filter (enforce_ownership)
# ---------------------------------------------------------------------------

def test_enforce_ownership_none_is_passthrough():
    from commander_builder.proposer import enforce_ownership
    adds = ["Rhystic Study", "Sol Ring"]
    kept, dropped = enforce_ownership(adds, None)
    assert kept == adds and dropped == []


def test_enforce_ownership_drops_unowned_keeps_basics():
    from commander_builder.proposer import enforce_ownership
    kept, dropped = enforce_ownership(
        ["Sol Ring", "Rhystic Study", "Mountain"],
        frozenset({"sol ring"}),
    )
    assert kept == ["Sol Ring", "Mountain"]
    assert dropped == ["Rhystic Study"]


# ---------------------------------------------------------------------------
# Full advise() pipeline (slow lane, like the rest of the advise suite)
# ---------------------------------------------------------------------------

@pytest.fixture
def _offline_advisor(monkeypatch):
    """Stub every network-adjacent seam the advise pipeline touches so
    the ownership tests are deterministic and offline. Mirrors the
    stubbing pattern in test_improvement_advisor.py (autouse
    supplemental-fetcher isolation + fake commander page)."""
    from commander_builder.edhrec_client import CardEntry, CommanderPage
    fake_page = CommanderPage(
        commander_name="Test Commander",
        slug="test-commander",
        fetched_at="2026-07-20T00:00:00",
        top_cards=[CardEntry(name="Rhystic Study", inclusion_pct=80.0),
                   CardEntry(name="Smothering Tithe", inclusion_pct=70.0)],
        high_synergy_cards=[],
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_commander_page",
        lambda name, **kw: fake_page,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_tag_page",
        lambda slug, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.fetch_average_deck",
        lambda *a, **kw: None,
    )
    # All Scryfall lookups offline: manabase phase degrades to no
    # recs, the CI filter skips (commander unresolvable), and
    # name-validation lands on None — all documented degradations.
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.lookup_card",
        lambda name, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )


def _write_dck(tmp_path: Path) -> tuple[Path, Path, Path]:
    deck_dir = tmp_path / "decks"
    match_dir = tmp_path / "matches"
    deck_dir.mkdir(exist_ok=True)
    match_dir.mkdir(exist_ok=True)
    deck = deck_dir / "[USER] Test Commander [B3].dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n"
        "[Main]\n1 Sol Ring\n1 Forest\n1 Old Card\n",
        encoding="utf-8",
    )
    return deck, deck_dir, match_dir


@pytest.mark.slow
def test_advise_no_collection_output_unchanged(tmp_path, _offline_advisor):
    """Snapshot-style pin: with NO collection file registered, passing
    the new ownership params changes NOTHING — same recs, same
    evidence (no 'owned' key anywhere), empty skipped_for_ownership.
    This is the hard backward-compatibility contract."""
    from commander_builder.improvement_advisor import advise
    deck, deck_dir, match_dir = _write_dck(tmp_path)

    baseline = advise(deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    with_flags = advise(
        deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir,
        owned_only=True,  # can't exclude against a nonexistent collection
    )

    def _comparable(report):
        d = report.to_dict()
        d.pop("timestamp", None)  # only legitimately varying field
        return d

    assert _comparable(baseline) == _comparable(with_flags)
    assert baseline.skipped_for_ownership == []
    assert all(
        "owned" not in (r.evidence or {}) for r in baseline.recommendations
    )


@pytest.mark.slow
def test_advise_flag_mode_annotates_when_collection_registered(
    tmp_path, _offline_advisor,
):
    """Collection present + owned_only NOT requested = flag mode: every
    add carries evidence['owned'], nothing is dropped."""
    from commander_builder.improvement_advisor import advise
    deck, deck_dir, match_dir = _write_dck(tmp_path)
    _register(["Rhystic Study"])

    report = advise(deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir)
    adds = {r.card: r for r in report.recommendations if r.action == "add"}
    assert adds["Rhystic Study"].evidence["owned"] is True
    assert adds["Smothering Tithe"].evidence["owned"] is False
    assert report.skipped_for_ownership == []


@pytest.mark.slow
def test_advise_owned_only_excludes_and_discloses(tmp_path, _offline_advisor):
    from commander_builder.improvement_advisor import advise
    deck, deck_dir, match_dir = _write_dck(tmp_path)
    _register(["Rhystic Study"])

    report = advise(
        deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir,
        owned_only=True,
    )
    adds = [r.card for r in report.recommendations if r.action == "add"]
    assert "Rhystic Study" in adds
    assert "Smothering Tithe" not in adds
    assert {"card": "Smothering Tithe", "reason": "not owned"} in (
        report.skipped_for_ownership
    )


@pytest.mark.slow
def test_advise_explicit_collection_path_beats_registered(
    tmp_path, _offline_advisor,
):
    """--collection PATH must override the default accessor."""
    from commander_builder.improvement_advisor import advise
    deck, deck_dir, match_dir = _write_dck(tmp_path)
    _register(["Rhystic Study"])          # registered default
    explicit = tmp_path / "other-collection.txt"
    explicit.write_text("Smothering Tithe\n", encoding="utf-8")

    report = advise(
        deck, bracket=3, deck_dir=deck_dir, match_dir=match_dir,
        collection_path=explicit, owned_only=True,
    )
    adds = [r.card for r in report.recommendations if r.action == "add"]
    assert "Smothering Tithe" in adds
    assert "Rhystic Study" not in adds


# ---------------------------------------------------------------------------
# CLI threading — commander-advise (advise stubbed; fast lane)
# ---------------------------------------------------------------------------

def _stub_advise_capture(monkeypatch, seen: dict):
    from commander_builder.improvement_advisor import AdviceReport
    def fake_advise(deck_path, bracket, **kwargs):
        seen.update(kwargs)
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Commander"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )


def test_cli_advise_owned_only_threads_when_collection_registered(
    monkeypatch, capsys,
):
    # ``--user`` stays a cwd-relative non-file (the existing CLI-test
    # pattern): advise is stubbed, and the bracket-estimate/health
    # read_text fails fast into its documented fail-quiet except.
    from commander_builder.improvement_advisor import main as advise_main
    _register(["Sol Ring"])
    seen: dict = {}
    _stub_advise_capture(monkeypatch, seen)

    rc = advise_main(["--user", "x.dck", "--bracket", "3", "--owned-only"])
    assert rc == 0
    assert seen.get("owned_only") is True


def test_cli_advise_owned_only_warns_and_stays_inert_without_collection(
    monkeypatch, capsys,
):
    from commander_builder.improvement_advisor import main as advise_main
    seen: dict = {}
    _stub_advise_capture(monkeypatch, seen)

    rc = advise_main(["--user", "x.dck", "--bracket", "3", "--owned-only"])
    assert rc == 0
    assert "owned_only" not in seen          # filter stayed inert
    assert "no collection file" in capsys.readouterr().err


def test_cli_advise_collection_flag_overrides_path(
    tmp_path, monkeypatch,
):
    from commander_builder.improvement_advisor import main as advise_main
    explicit = tmp_path / "mine.txt"
    explicit.write_text("Sol Ring\n", encoding="utf-8")
    seen: dict = {}
    _stub_advise_capture(monkeypatch, seen)

    rc = advise_main([
        "--user", "x.dck", "--bracket", "3",
        "--collection", str(explicit), "--owned-only",
    ])
    assert rc == 0
    assert seen.get("collection_path") == explicit
    assert seen.get("owned_only") is True


def test_cli_report_marks_unowned_adds():
    """Flag-mode report text tags unowned adds and ONLY unowned adds."""
    from commander_builder.improvement_advisor import (
        AdviceReport, SwapRecommendation, _format_report_text,
    )
    report = AdviceReport(
        deck_filename="x.dck", deck_id=None, bracket=3,
        commander_names=["Test Commander"],
        recommendations=[
            SwapRecommendation(card="Sol Ring", action="add", reason="r",
                               evidence={"owned": True}),
            SwapRecommendation(card="Rhystic Study", action="add", reason="r",
                               evidence={"owned": False}),
        ],
    )
    text = _format_report_text(report)
    lines = {ln.strip() for ln in text.splitlines()}
    assert any("Rhystic Study" in ln and "[not owned]" in ln for ln in lines)
    assert not any("Sol Ring" in ln and "[not owned]" in ln for ln in lines)


def test_cli_report_discloses_ownership_skips():
    from commander_builder.improvement_advisor import (
        AdviceReport, _format_report_text,
    )
    report = AdviceReport(
        deck_filename="x.dck", deck_id=None, bracket=3,
        commander_names=["Test Commander"],
        skipped_for_ownership=[{"card": "Dockside Extortionist",
                                "reason": "not owned"}],
    )
    text = _format_report_text(report)
    assert "skipped 1 unowned adds" in text
    assert "Dockside Extortionist" in text


# ---------------------------------------------------------------------------
# CLI threading — commander-auto-curate (pipeline stubbed; fast lane)
# ---------------------------------------------------------------------------

def _stub_curate_pipeline(monkeypatch, seen: dict):
    """Stub advise + auto_propose + apply so auto_curate_main runs the
    argv/threading logic without EDHREC, Claude, or disk writes."""
    from commander_builder.improvement_advisor import AdviceReport
    from commander_builder.proposer import Proposal

    def fake_advise(deck_path, bracket, **kwargs):
        seen["advise_kwargs"] = kwargs
        return AdviceReport(
            deck_filename=Path(deck_path).name, deck_id=None,
            bracket=bracket, commander_names=["Test Commander"],
        )
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    def fake_auto_propose(**kwargs):
        seen["auto_propose_kwargs"] = kwargs
        return Proposal(adds=[], cuts=[], rationale="stub")
    # auto_propose is imported into _proposer_cli's namespace at module
    # load, so patch the CLI-side binding (the one auto_curate_main
    # actually calls).
    monkeypatch.setattr(
        "commander_builder._proposer_cli.auto_propose", fake_auto_propose,
    )
    monkeypatch.setattr(
        "commander_builder._proposer_cli.apply_proposal_to_deck",
        lambda deck_path, proposal, dry_run=False: deck_path,
    )


def test_auto_curate_owned_only_threads_collection_keys(
    tmp_path, monkeypatch,
):
    from commander_builder._proposer_cli import auto_curate_main
    deck, _, _ = _write_dck(tmp_path)
    _register(["Sol Ring"])
    seen: dict = {}
    _stub_curate_pipeline(monkeypatch, seen)

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--owned-only", "--dry-run", "--json",
    ])
    assert rc == 0
    # Advisor ran in exclude mode...
    assert seen["advise_kwargs"].get("owned_only") is True
    # ...and the curator got the loaded key set for its safety net.
    assert seen["auto_propose_kwargs"]["collection_keys"] == frozenset(
        {"sol ring"},
    )


def test_auto_curate_without_owned_only_passes_none(
    tmp_path, monkeypatch,
):
    """No --owned-only = no ownership filtering anywhere, even with a
    collection registered (annotation-only behavior belongs to the
    advisor's default path, not the curator)."""
    from commander_builder._proposer_cli import auto_curate_main
    deck, _, _ = _write_dck(tmp_path)
    _register(["Sol Ring"])
    seen: dict = {}
    _stub_curate_pipeline(monkeypatch, seen)

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--dry-run", "--json",
    ])
    assert rc == 0
    assert seen["advise_kwargs"].get("owned_only") is False
    assert seen["auto_propose_kwargs"]["collection_keys"] is None


def test_auto_curate_owned_only_warns_without_collection(
    tmp_path, monkeypatch, capsys,
):
    from commander_builder._proposer_cli import auto_curate_main
    deck, _, _ = _write_dck(tmp_path)
    seen: dict = {}
    _stub_curate_pipeline(monkeypatch, seen)

    rc = auto_curate_main([
        str(deck), "--bracket", "3", "--owned-only", "--dry-run", "--json",
    ])
    assert rc == 0
    assert seen["auto_propose_kwargs"]["collection_keys"] is None
    assert "no collection file" in capsys.readouterr().err


def test_auto_propose_ownership_post_filter(monkeypatch, tmp_path):
    """Unowned adds Claude proposes anyway land in dropped_for_unowned
    (post-response safety net, mirroring the color-identity filter)."""
    from commander_builder import proposer

    deck = tmp_path / "deck.dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n[Main]\n1 Sol Ring\n1 Old Card\n",
        encoding="utf-8",
    )
    # Force the SDK path with a canned curator response.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test1234567890abcdef")
    payload = json.dumps({
        "adds": ["Rhystic Study", "Brainstorm"],
        "cuts": ["Old Card"],
        "rationale": "stub",
    })

    class _FakeMessages:
        def create(self, **kwargs):
            class _Block:
                text = payload
            class _Resp:
                content = [_Block()]
            return _Resp()

    class _FakeAnthropic:
        def __init__(self, api_key=None):
            self.messages = _FakeMessages()

    import sys as _sys
    import types as _types
    fake_mod = _types.ModuleType("anthropic")
    fake_mod.Anthropic = _FakeAnthropic
    monkeypatch.setitem(_sys.modules, "anthropic", fake_mod)
    # Neutralize the other post-filters' external lookups.
    monkeypatch.setattr(
        "commander_builder._proposer_filters._load_game_changers",
        lambda: set(),
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )

    proposal = proposer.auto_propose(
        deck_path=deck, bracket=3,
        advice_report={"added": ["Rhystic Study", "Brainstorm"],
                       "removed": ["Old Card"], "rationale": ""},
        collection_keys=frozenset({"brainstorm"}),
    )
    assert proposal.adds == ["Brainstorm"]
    assert proposal.dropped_for_unowned == ["Rhystic Study"]


# ---------------------------------------------------------------------------
# Web — /api/collection + owned_only threading through /api/audit
# ---------------------------------------------------------------------------

flask = pytest.importorskip("flask")


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    from commander_builder.web.app import create_app
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    deck = deck_dir / "Alpha.dck"
    deck.write_text(
        "[Commander]\n1 Test Commander\n[Main]\n"
        + "1 Forest\n" * 35 + "1 Cultivate\n" * 5,
        encoding="utf-8",
    )
    # Isolate the config store like test_web_app does, so the BYO-key
    # resolver can't read a real developer config.
    monkeypatch.setenv(
        "COMMANDER_BUILDER_CONFIG", str(tmp_path / "no_config.json"),
    )
    # Keep the audit payload builder fully offline + deterministic:
    # pricing lookups and the EDHREC salt fetch would otherwise probe
    # the network (all fail-quiet, but slow and nondeterministic).
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints",
        lambda name, **kw: None,
    )
    monkeypatch.setattr(
        "commander_builder.web.routes_audit.fetch_salt_list",
        lambda **kw: {},
    )
    app = create_app(deck_dir=deck_dir, knowledge_db=tmp_path / "kl.sqlite")
    app.config["TESTING"] = True
    return app.test_client()


def test_web_collection_get_unconfigured(web_client):
    body = web_client.get("/api/collection").get_json()
    assert body["configured"] is False
    assert body["count"] == 0
    assert body["path"]  # path surfaced for manual-edit guidance


def test_web_collection_put_get_round_trip(web_client):
    resp = web_client.put("/api/collection", json={
        "text": "2 Sol Ring\nRhystic Study\n",
    })
    assert resp.status_code == 200
    assert resp.get_json() == {
        "status": "ok", "configured": True, "count": 2,
    }
    body = web_client.get("/api/collection").get_json()
    assert body["configured"] is True and body["count"] == 2
    # The file landed at the (test-isolated) registered path.
    assert collection.load_collection() == frozenset(
        {"sol ring", "rhystic study"},
    )


def test_web_collection_put_csv(web_client):
    resp = web_client.put("/api/collection", json={
        "text": "Count,Name\n1,Sol Ring\n3,Brainstorm\n",
    })
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 2


def test_web_collection_put_empty_text_clears(web_client):
    web_client.put("/api/collection", json={"text": "Sol Ring\n"})
    assert collection.load_collection() is not None
    resp = web_client.put("/api/collection", json={"text": ""})
    assert resp.status_code == 200
    assert resp.get_json()["configured"] is False
    assert collection.load_collection() is None


def test_web_collection_put_rejects_bad_csv_row(web_client):
    resp = web_client.put("/api/collection", json={
        "text": "Count,Name\nnope,Sol Ring\n",
    })
    assert resp.status_code == 400
    assert "line" in resp.get_json()["error"]
    assert collection.load_collection() is None  # nothing persisted


def test_web_collection_put_rejects_missing_text(web_client):
    assert web_client.put("/api/collection", json={}).status_code == 400
    assert web_client.put(
        "/api/collection", json=["not", "dict"],
    ).status_code == 400


def _fake_report():
    from types import SimpleNamespace
    return SimpleNamespace(
        recommendations=[
            SimpleNamespace(card="Lotus Cobra", action="add", reason="r",
                            evidence={"owned": False}),
            SimpleNamespace(card="Sol Ring", action="add", reason="r",
                            evidence={"owned": True}),
        ],
        diagnosis=SimpleNamespace(pattern_summary="", weakness_signals=[]),
        source="heuristic",
        skipped_for_ownership=[{"card": "Dockside Extortionist",
                                "reason": "not owned"}],
    )


def test_web_audit_threads_owned_only_param(web_client, monkeypatch):
    seen: dict = {}

    def fake_advise(deck_path, bracket, **kwargs):
        seen.update(kwargs)
        return _fake_report()
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise", fake_advise,
    )

    resp = web_client.get("/api/audit?deck=Alpha&bracket=3&owned_only=1")
    assert resp.status_code == 200
    assert seen.get("owned_only") is True

    # Default (param absent) threads False — flag-mode annotation only.
    web_client.get("/api/audit?deck=Alpha&bracket=3")
    assert seen.get("owned_only") is False


def test_web_audit_payload_surfaces_owned_and_skips(web_client, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda deck_path, bracket, **kw: _fake_report(),
    )
    body = web_client.get(
        "/api/audit?deck=Alpha&bracket=3&owned_only=1",
    ).get_json()
    by_card = {a["card"]: a for a in body["added"]}
    assert by_card["Lotus Cobra"]["owned"] is False
    assert by_card["Sol Ring"]["owned"] is True
    assert body["skipped_for_ownership"] == [
        {"card": "Dockside Extortionist", "reason": "not owned"},
    ]


def test_web_audit_payload_owned_null_when_feature_unused(
    web_client, monkeypatch,
):
    """No collection → advisor never annotates → owned is null in the
    payload, so the UI renders exactly as before the feature."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        "commander_builder.improvement_advisor.advise",
        lambda deck_path, bracket, **kw: SimpleNamespace(
            recommendations=[
                SimpleNamespace(card="Lotus Cobra", action="add",
                                reason="r", evidence={}),
            ],
            diagnosis=SimpleNamespace(pattern_summary="",
                                      weakness_signals=[]),
            source="heuristic",
        ),
    )
    body = web_client.get("/api/audit?deck=Alpha&bracket=3").get_json()
    assert body["added"][0]["owned"] is None
    assert body["skipped_for_ownership"] == []
