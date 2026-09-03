"""Tests for the [PREMADE] deck role + popularity importers.

All offline: Moxfield/EDHREC fetchers are monkeypatched with stubbed
payloads (the established pattern from test_moxfield_import /
test_edhrec_top). Covers:

- [PREMADE] naming / role detection + same-id role scoping,
- exclusion from opponent/filler selection (run_match fallback,
  pool_curator candidates, _proposer_sim fillers),
- the web deck list typing premades as "premade",
- importer metadata (Source=/Likes=/Salt=) from stubbed API payloads,
- top-N selection/ordering by likes,
- commander diversity (on-disk any-role + intra-pull dedupe),
- bracket tagging (declared vs estimator fallback),
- the commander-import --premade CLI wiring.
"""
from __future__ import annotations

import http.client
import urllib.error
from pathlib import Path

import pytest

from commander_builder import edhrec_client, moxfield_import, premade_import
from commander_builder.dck_utils import count_commander_cards, count_main_cards
from commander_builder.edhrec_client import AverageDeck, CardEntry
from commander_builder.premade_import import (
    PREMADE_PREFIX,
    _commander_card_names,
    _premade_destination,
    existing_commander_names,
    import_edhrec_premades,
    import_moxfield_premades,
    repair_premade_text,
    repair_premades,
    run_premade_pull,
)


# ---------------------------------------------------------------------------
# Stub payload builders
# ---------------------------------------------------------------------------

def _deck_json(pid: str, name: str, commander: str,
               bracket: int | None = None, likes: int = 0) -> dict:
    # 99-card mainboard: the importer's post-fetch validation skips any
    # deck off the `main == 100 - commanders` invariant, so the stub must
    # be size-legal like every real Moxfield deck.
    d = {
        "publicId": pid,
        "name": name,
        "format": "commander",
        "likeCount": likes,
        "boards": {
            "commanders": {"cards": {
                "c1": {"quantity": 1,
                       "card": {"name": commander, "set": "abc", "cn": "1"}},
            }},
            "mainboard": {"cards": {
                "m1": {"quantity": 1,
                       "card": {"name": "Sol Ring", "set": "abc", "cn": "2"}},
                "m2": {"quantity": 1,
                       "card": {"name": "Arcane Signet", "set": "abc",
                                "cn": "3"}},
                "m3": {"quantity": 97,
                       "card": {"name": "Mountain", "set": "abc", "cn": "4"}},
            }},
        },
    }
    if bracket is not None:
        d["bracket"] = bracket
    return d


def _search_row(pid: str, commander: str, likes: int) -> dict:
    return {"publicId": pid, "likeCount": likes,
            "commanders": [{"name": commander}]}


def _stub_moxfield(monkeypatch, rows: list[dict], decks: dict[str, dict]):
    """Wire the Moxfield leg to stubbed search rows + deck JSONs."""
    monkeypatch.setattr(
        premade_import, "_search_top_liked",
        lambda page_size=50, page=1: rows if page == 1 else [],
    )
    monkeypatch.setattr(
        moxfield_import, "fetch_deck", lambda pid: decks[pid],
    )


def _avg_deck(commander: str) -> AverageDeck:
    return AverageDeck(
        commander_name=commander,
        slug=edhrec_client.commander_slug(commander),
        url="https://edhrec.com/average-decks/x",
        bracket_slug=None,
        budget_slug=None,
        cards=[
            CardEntry(name=commander, num_decks=100),
            CardEntry(name="Sol Ring", num_decks=99),
            CardEntry(name="Forest", num_decks=90),
        ],
    )


def _stub_edhrec(monkeypatch, commanders: list[CardEntry],
                 salt: dict[str, float]):
    monkeypatch.setattr(
        edhrec_client, "fetch_top_commanders", lambda **_kw: commanders,
    )
    monkeypatch.setattr(
        edhrec_client, "fetch_salt_list", lambda **_kw: salt,
    )
    monkeypatch.setattr(
        edhrec_client, "fetch_average_deck",
        lambda name, **_kw: _avg_deck(name),
    )


@pytest.fixture(autouse=True)
def _fixed_estimate(monkeypatch):
    """Pin the bracket estimator so tests don't depend on its weights.

    Tests that exercise the declared-vs-estimated split override this
    per-test; everything else just needs a stable tag."""
    monkeypatch.setattr(
        premade_import, "estimate_bracket",
        lambda text, *a, **k: {"estimate": 2},
    )


@pytest.fixture(autouse=True)
def _no_network_lookup(monkeypatch):
    """Fail loudly if any test reaches the Scryfall lookup un-stubbed.

    ``_commander_card_names`` only consults ``lookup_card`` for ``//``
    names (partner-vs-DFC disambiguation); plain names must never touch
    it. Partner tests override this with their own stub."""
    def _boom(name, **_kw):
        raise AssertionError(f"unexpected network lookup for {name!r}")
    monkeypatch.setattr(premade_import, "lookup_card", _boom)


def _stub_partner_lookup(monkeypatch, halves: set[str]):
    """lookup_card stub: each name in ``halves`` is its own card; the
    joined 'A // B' string resolves to nothing (partner pair, not DFC)."""
    monkeypatch.setattr(
        premade_import, "lookup_card",
        lambda name, **_kw: {"name": name} if name in halves else None,
    )


# ---------------------------------------------------------------------------
# Naming / role detection
# ---------------------------------------------------------------------------

def test_premade_destination_prefix_and_bracket(tmp_path):
    dest = _premade_destination("My Deck", 3, tmp_path)
    assert dest.name == "[PREMADE] My Deck [B3].dck"


def test_premade_destination_unknown_bracket(tmp_path):
    dest = _premade_destination("My Deck", 0, tmp_path)
    assert dest.name == "[PREMADE] My Deck [B?].dck"


def test_deck_role_classifies_all_three_roles(tmp_path):
    role = moxfield_import._deck_role
    assert role(tmp_path / "[USER] Foo [B3].dck") == "user"
    assert role(tmp_path / "[PREMADE] Foo [B3].dck") == "premade"
    assert role(tmp_path / "Foo [B3].dck") == "pool"
    assert role(tmp_path / "[REF] mox Foo [B3].dck") == "pool"


def test_pool_role_id_scan_excludes_premade_files(tmp_path):
    """A premade copy of a Moxfield id must not read as 'already
    harvested' — the pool-role scan skips [PREMADE] files."""
    (tmp_path / "[PREMADE] Foo [B3].dck").write_text(
        "[metadata]\nName=x\nMoxfield=abc123\n[Main]\n1 Forest\n",
        encoding="utf-8")
    (tmp_path / "Bar [B3].dck").write_text(
        "[metadata]\nName=y\nMoxfield=def456\n[Main]\n1 Forest\n",
        encoding="utf-8")
    pool_map = moxfield_import._existing_moxfield_ids(tmp_path, is_user=False)
    assert "abc123" not in pool_map
    assert "def456" in pool_map
    # Role-agnostic scan still sees everything.
    all_map = moxfield_import._existing_moxfield_ids(tmp_path, is_user=None)
    assert set(all_map) == {"abc123", "def456"}


def test_existing_premade_ids_scopes_to_premade_role(tmp_path):
    (tmp_path / "[PREMADE] Foo [B3].dck").write_text(
        "[metadata]\nName=x\nMoxfield=abc123\n[Main]\n1 Forest\n",
        encoding="utf-8")
    (tmp_path / "[USER] Bar [B3].dck").write_text(
        "[metadata]\nName=y\nMoxfield=user789\n[Main]\n1 Forest\n",
        encoding="utf-8")
    ids = premade_import._existing_premade_ids(tmp_path)
    assert set(ids) == {"abc123"}


# ---------------------------------------------------------------------------
# Opponent / filler exclusion
# ---------------------------------------------------------------------------

def _touch_deck(d: Path, name: str) -> None:
    (d / name).write_text("[metadata]\nName=x\n[Main]\n1 Forest\n",
                          encoding="utf-8")


def test_fallback_opponents_exclude_premade_and_user(tmp_path, monkeypatch):
    """Extended 2026-09-03 (R3 C-03): the fallback now applies decision
    C1's whole list via ``filler_policy`` — ``[REF]`` (same popularity
    selection as ``[PREMADE]``) and ``[CONTROL]`` (a do-nothing deck
    inflates decisive counts) are turned away too."""
    from commander_builder import run_match
    monkeypatch.setattr(run_match, "DECK_DIR", tmp_path)
    for n in ("Alpha [B3].dck", "Beta [B3].dck",
              "[USER] Mine [B3].dck", "[PREMADE] Hot [B3].dck",
              "[REF] TopLikes [B3].dck", "[CONTROL] do-nothing [B3].dck"):
        _touch_deck(tmp_path, n)
    got = run_match._fallback_opponents(3, exclude="", n=10)
    assert got == ["Alpha [B3].dck", "Beta [B3].dck"]


def test_pool_curator_candidates_exclude_premade(tmp_path):
    from commander_builder.pool_curator import _list_bracket_candidates
    for n in ("Alpha [B3].dck", "[PREMADE] Hot [B3].dck",
              "[USER] Mine [B3].dck", "[CONTROL] do-nothing calib1 [B3].dck"):
        _touch_deck(tmp_path, n)
    assert _list_bracket_candidates(3, deck_dir=tmp_path) == ["Alpha [B3].dck"]


def test_proposer_fillers_exclude_premade(tmp_path):
    from commander_builder._proposer_sim import _pick_filler_decks
    for n in ("Alpha [B3].dck", "Beta [B3].dck",
              "[PREMADE] Hot [B3].dck", "[USER] Mine [B3].dck"):
        _touch_deck(tmp_path, n)
    got = _pick_filler_decks(tmp_path, [], count=2, target_bracket=3)
    assert sorted(got) == ["Alpha [B3].dck", "Beta [B3].dck"]


# ---------------------------------------------------------------------------
# Web deck list
# ---------------------------------------------------------------------------

def test_web_list_decks_types_premade(tmp_path):
    from commander_builder.web.app import _list_decks
    for n in ("[USER] Mine [B3].dck", "[PREMADE] Hot [B4].dck",
              "Filler [B3].dck"):
        _touch_deck(tmp_path, n)
    default = _list_decks(tmp_path)
    by_name = {d["name"]: d for d in default}
    # Premades appear in the DEFAULT (sidebar) listing, typed.
    assert by_name["Hot [B4]"]["type"] == "premade"
    assert by_name["Mine [B3]"]["type"] == "user"
    # Pool decks stay hidden by default...
    assert "Filler [B3]" not in by_name
    # ...but show as type "pool" in the unfiltered listing.
    all_mode = {d["name"]: d for d in _list_decks(tmp_path, user_only=False)}
    assert all_mode["Filler [B3]"]["type"] == "pool"
    # Display names strip the role prefix; ids keep it.
    assert by_name["Hot [B4]"]["id"] == "[PREMADE] Hot [B4]"


# ---------------------------------------------------------------------------
# Moxfield importer
# ---------------------------------------------------------------------------

def test_moxfield_premades_write_metadata_from_stub(tmp_path, monkeypatch):
    rows = [_search_row("p1", "Krenko, Mob Boss", 500)]
    decks = {"p1": _deck_json("p1", "Goblin Bomb", "Krenko, Mob Boss",
                              bracket=4, likes=500)}
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=1, out_dir=tmp_path, sleep_sec=0)
    assert len(out) == 1
    dest = Path(out[0]["path"])
    assert dest.name == "[PREMADE] Goblin Bomb [B4].dck"
    text = dest.read_text(encoding="utf-8")
    meta = text.split("[Commander]")[0]
    assert "Source=moxfield" in meta
    assert "Likes=500" in meta
    assert "Moxfield=p1" in meta
    # Name= is stamped to the final stem (dck_meta invariant).
    assert "Name=[PREMADE] Goblin Bomb [B4]" in meta
    assert out[0]["source"] == "moxfield"
    assert out[0]["metric_label"] == "likes"
    assert out[0]["metric_value"] == 500
    assert out[0]["bracket"] == 4


def test_moxfield_premades_top_n_ordering_by_likes(tmp_path, monkeypatch):
    # Unordered search rows → picks the N most-liked, in likes order.
    rows = [
        _search_row("mid", "Cmdr Mid", 300),
        _search_row("low", "Cmdr Low", 10),
        _search_row("top", "Cmdr Top", 900),
        _search_row("high", "Cmdr High", 700),
    ]
    decks = {
        "mid": _deck_json("mid", "Mid", "Cmdr Mid", 3, 300),
        "low": _deck_json("low", "Low", "Cmdr Low", 3, 10),
        "top": _deck_json("top", "Top", "Cmdr Top", 3, 900),
        "high": _deck_json("high", "High", "Cmdr High", 3, 700),
    }
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=3, out_dir=tmp_path, sleep_sec=0)
    assert [r["metric_value"] for r in out] == [900, 700, 300]
    assert [Path(r["path"]).name for r in out] == [
        "[PREMADE] Top [B3].dck",
        "[PREMADE] High [B3].dck",
        "[PREMADE] Mid [B3].dck",
    ]


def test_moxfield_premades_estimator_fallback_when_no_bracket(
        tmp_path, monkeypatch):
    rows = [_search_row("p1", "Cmdr A", 100)]
    decks = {"p1": _deck_json("p1", "NoBracket", "Cmdr A", None, 100)}
    _stub_moxfield(monkeypatch, rows, decks)
    monkeypatch.setattr(
        premade_import, "estimate_bracket",
        lambda text, *a, **k: {"estimate": 5},
    )
    out = import_moxfield_premades(count=1, out_dir=tmp_path, sleep_sec=0)
    assert out[0]["bracket"] == 5
    assert Path(out[0]["path"]).name.endswith(" [B5].dck")


def test_moxfield_premades_skip_commanders_already_on_disk(
        tmp_path, monkeypatch):
    """Diversity, on-disk half: a commander represented by ANY role on
    disk is skipped; the ranking is walked further to backfill."""
    # Krenko already exists as a plain pool deck (no role prefix).
    (tmp_path / "Pool Krenko [B3].dck").write_text(
        "[metadata]\nName=Pool Krenko [B3]\n[Commander]\n"
        "1 Krenko, Mob Boss\n[Main]\n1 Forest\n", encoding="utf-8")
    rows = [
        _search_row("p1", "Krenko, Mob Boss", 900),   # skipped: on disk
        _search_row("p2", "Cmdr Fresh", 500),          # picked
        _search_row("p3", "Cmdr Backfill", 100),       # backfills slot 2
    ]
    decks = {
        "p2": _deck_json("p2", "Fresh", "Cmdr Fresh", 3, 500),
        "p3": _deck_json("p3", "Backfill", "Cmdr Backfill", 3, 100),
    }
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=2, out_dir=tmp_path, sleep_sec=0)
    picked = [Path(r["path"]).name for r in out]
    assert picked == ["[PREMADE] Fresh [B3].dck",
                      "[PREMADE] Backfill [B3].dck"]


def test_moxfield_premades_intra_pull_commander_dedupe(tmp_path, monkeypatch):
    """Diversity, intra-pull half: two top decks sharing a commander →
    only the more-liked one is written; the next distinct commander
    backfills."""
    rows = [
        _search_row("p1", "Cmdr Same", 900),
        _search_row("p2", "Cmdr Same", 800),   # same commander: skipped
        _search_row("p3", "Cmdr Other", 100),
    ]
    decks = {
        "p1": _deck_json("p1", "First", "Cmdr Same", 3, 900),
        "p2": _deck_json("p2", "Second", "Cmdr Same", 3, 800),
        "p3": _deck_json("p3", "Other", "Cmdr Other", 3, 100),
    }
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=2, out_dir=tmp_path, sleep_sec=0)
    assert [Path(r["path"]).name for r in out] == [
        "[PREMADE] First [B3].dck", "[PREMADE] Other [B3].dck"]


def test_moxfield_premades_diversity_checks_fetched_json(
        tmp_path, monkeypatch):
    """The authoritative commander check runs on the FETCHED deck JSON:
    a search row that omits its commanders still dedupes."""
    rows = [
        {"publicId": "p1", "likeCount": 900},   # no commanders on the row
        {"publicId": "p2", "likeCount": 800},
        _search_row("p3", "Cmdr Other", 100),
    ]
    decks = {
        "p1": _deck_json("p1", "First", "Cmdr Same", 3, 900),
        "p2": _deck_json("p2", "Second", "Cmdr Same", 3, 800),
        "p3": _deck_json("p3", "Other", "Cmdr Other", 3, 100),
    }
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=2, out_dir=tmp_path, sleep_sec=0)
    assert [Path(r["path"]).name for r in out] == [
        "[PREMADE] First [B3].dck", "[PREMADE] Other [B3].dck"]


def test_existing_commander_names_spans_all_roles(tmp_path):
    (tmp_path / "[USER] A [B3].dck").write_text(
        "[metadata]\nName=a\n[Commander]\n1 Cmdr User|ABC|1\n[Main]\n1 Forest\n",
        encoding="utf-8")
    (tmp_path / "[PREMADE] B [B3].dck").write_text(
        "[metadata]\nName=b\n[Commander]\n1 Cmdr Premade\n[Main]\n1 Forest\n",
        encoding="utf-8")
    (tmp_path / "C [B3].dck").write_text(
        "[metadata]\nName=c\n[Commander]\n1 Cmdr Pool\n[Main]\n1 Forest\n",
        encoding="utf-8")
    got = existing_commander_names(tmp_path)
    assert got == {"cmdr user", "cmdr premade", "cmdr pool"}


# ---------------------------------------------------------------------------
# EDHREC importer
# ---------------------------------------------------------------------------

def test_edhrec_premades_record_salt_and_source(tmp_path, monkeypatch):
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Tergrid, God of Fright",
                              num_decks=50000)],
        salt={"tergrid, god of fright": 2.71},
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    assert len(out) == 1
    dest = Path(out[0]["path"])
    assert dest.name == "[PREMADE] EDHREC Tergrid, God of Fright [B2].dck"
    text = dest.read_text(encoding="utf-8")
    meta = text.split("[Commander]")[0]
    assert "Source=edhrec" in meta
    assert "Salt=2.71" in meta
    assert out[0]["source"] == "edhrec"
    assert out[0]["metric_label"] == "salt"
    assert out[0]["metric_value"] == 2.71
    # The average deck's commander landed in the [Commander] section.
    assert "1 Tergrid, God of Fright" in text.split("[Commander]")[1]


def test_edhrec_premades_zero_salt_when_not_on_list(tmp_path, monkeypatch):
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Friendly Cmdr", num_decks=100)],
        salt={},
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    text = Path(out[0]["path"]).read_text(encoding="utf-8")
    assert "Salt=0.00" in text


def test_edhrec_premades_top_n_walks_ranking_with_diversity(
        tmp_path, monkeypatch):
    # "Cmdr Taken" is already on disk as a [USER] deck → skipped, and
    # the ranking is walked further to backfill.
    (tmp_path / "[USER] Mine [B3].dck").write_text(
        "[metadata]\nName=m\n[Commander]\n1 Cmdr Taken\n[Main]\n1 Forest\n",
        encoding="utf-8")
    _stub_edhrec(
        monkeypatch,
        commanders=[
            CardEntry(name="Cmdr Taken", num_decks=900),
            CardEntry(name="Cmdr One", num_decks=800),
            CardEntry(name="Cmdr Two", num_decks=700),
            CardEntry(name="Cmdr Three", num_decks=600),
        ],
        salt={},
    )
    out = import_edhrec_premades(count=2, out_dir=tmp_path)
    assert [r["name"] for r in out] == [
        "[PREMADE] EDHREC Cmdr One [B2]",
        "[PREMADE] EDHREC Cmdr Two [B2]",
    ]


def test_edhrec_premades_skip_unpublished_average_deck(tmp_path, monkeypatch):
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Cmdr NoAvg", num_decks=900),
                    CardEntry(name="Cmdr Ok", num_decks=800)],
        salt={},
    )
    monkeypatch.setattr(
        edhrec_client, "fetch_average_deck",
        lambda name, **_kw: None if name == "Cmdr NoAvg" else _avg_deck(name),
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    assert [r["name"] for r in out] == ["[PREMADE] EDHREC Cmdr Ok [B2]"]


# ---------------------------------------------------------------------------
# EDHREC importer — [Commander] section correctness (the missing-commander
# bug: real average-deck payloads never list the commander itself)
# ---------------------------------------------------------------------------

def _avg_deck_cards(commander: str, cards: list[CardEntry]) -> AverageDeck:
    return AverageDeck(
        commander_name=commander,
        slug=edhrec_client.commander_slug(commander),
        url="https://edhrec.com/average-decks/x",
        bracket_slug=None,
        budget_slug=None,
        cards=cards,
    )


def test_edhrec_premades_inject_commander_missing_from_payload(
        tmp_path, monkeypatch):
    """The production payload shape: the commander is NOT in the card
    list. The written .dck must still get a [Commander] section and a
    99-card [Main] (100 - 1 commander)."""
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Urtet, Remnant of Memnarch",
                              num_decks=9000)],
        salt={},
    )
    monkeypatch.setattr(
        edhrec_client, "fetch_average_deck",
        lambda name, **_kw: _avg_deck_cards(name, [
            CardEntry(name="Sol Ring", num_decks=99),
            CardEntry(name="Alibou, Ancient Witness", num_decks=95),
            CardEntry(name="Island", num_decks=90),
        ]),
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    text = Path(out[0]["path"]).read_text(encoding="utf-8")
    assert "[Commander]" in text
    cmdr_section = text.split("[Commander]")[1].split("[Main]")[0]
    assert "1 Urtet, Remnant of Memnarch" in cmdr_section
    assert count_commander_cards(text) == 1
    assert count_main_cards(text) == 99
    # The commander occupies the command zone only, never a [Main] slot.
    assert "Urtet" not in text.split("[Main]")[1]


def test_edhrec_premades_dedupe_commander_also_in_payload(
        tmp_path, monkeypatch):
    """A payload that DOES list the commander (belt-and-braces) must not
    duplicate it: once in [Commander], zero times in [Main]."""
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Krenko, Mob Boss", num_decks=9000)],
        salt={},
    )
    # The shared _avg_deck stub includes the commander in the card list.
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    text = Path(out[0]["path"]).read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1
    assert count_main_cards(text) == 99
    assert "1 Krenko, Mob Boss" in text.split("[Commander]")[1].split("[Main]")[0]
    assert "Krenko" not in text.split("[Main]")[1]


def test_edhrec_premades_partner_pair_two_commanders_98_main(
        tmp_path, monkeypatch):
    """An EDHREC partner-pair entry ('A // B' where each half is its own
    card) writes TWO [Commander] lines and a 98-card [Main]."""
    pair = "Alpha One // Beta Two"
    _stub_partner_lookup(monkeypatch, {"Alpha One", "Beta Two"})
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name=pair, num_decks=9000)],
        salt={},
    )
    monkeypatch.setattr(
        edhrec_client, "fetch_average_deck",
        lambda name, **_kw: _avg_deck_cards(name, [
            # One partner sneaks into the payload — must be deduped.
            CardEntry(name="Alpha One", num_decks=100),
            CardEntry(name="Sol Ring", num_decks=99),
            CardEntry(name="Forest", num_decks=90),
        ]),
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    text = Path(out[0]["path"]).read_text(encoding="utf-8")
    cmdr_section = text.split("[Commander]")[1].split("[Main]")[0]
    assert "1 Alpha One" in cmdr_section
    assert "1 Beta Two" in cmdr_section
    assert count_commander_cards(text) == 2
    assert count_main_cards(text) == 98          # 100 - 2 commanders
    assert "Alpha One" not in text.split("[Main]")[1]


def test_commander_card_names_dfc_stays_single(monkeypatch):
    """A '//' name that resolves as ONE card (a DFC commander) stays a
    single [Commander] line under its full name."""
    dfc = "Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel"
    monkeypatch.setattr(
        premade_import, "lookup_card",
        lambda name, **_kw: {"name": name} if name == dfc else None,
    )
    assert _commander_card_names(dfc) == [dfc]


def test_commander_card_names_plain_name_skips_lookup():
    """No '//' → no Scryfall lookup at all (the autouse guard raises on
    any call, so simply resolving proves the no-network path)."""
    assert _commander_card_names("Krenko, Mob Boss") == ["Krenko, Mob Boss"]


# ---------------------------------------------------------------------------
# Repair — retrofitting [Commander] onto broken on-disk EDHREC premades
# ---------------------------------------------------------------------------

def _broken_edhrec_premade(
    d: Path,
    commander: str = "Urtet, Remnant of Memnarch",
    main_lines: list[str] | None = None,
) -> Path:
    """Write a pre-fix EDHREC premade: Source=edhrec, DisplayName, a
    99-card [Main], and NO [Commander] — the exact broken shape on disk."""
    # Real files sanitize the stem (safe_filename: '/' → '_'); the
    # faithful commander name lives in DisplayName=.
    stem = f"[PREMADE] EDHREC {moxfield_import.safe_filename(commander)} [B3]"
    if main_lines is None:
        main_lines = ["1 Sol Ring", "1 Arcane Signet", "97 Island"]
    p = d / f"{stem}.dck"
    p.write_text(
        "\n".join([
            "[metadata]",
            f"Name={stem}",
            f"DisplayName=EDHREC Average — {commander}",
            "Source=edhrec",
            "Salt=0.00",
            "[Main]",
            *main_lines,
        ]) + "\n",
        encoding="utf-8",
    )
    return p


def test_repair_adds_commander_section_and_is_idempotent(tmp_path):
    p = _broken_edhrec_premade(tmp_path)
    assert count_commander_cards(p.read_text(encoding="utf-8")) == 0
    assert repair_premades(out_dir=tmp_path) == 0
    text = p.read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1
    assert count_main_cards(text) == 99
    cmdr_section = text.split("[Commander]")[1].split("[Main]")[0]
    assert "1 Urtet, Remnant of Memnarch" in cmdr_section
    # [Commander] sits between [metadata] and [Main]; metadata survives.
    assert text.index("Source=edhrec") < text.index("[Commander]")
    assert text.index("[Commander]") < text.index("[Main]")
    assert "Salt=0.00" in text
    # Idempotent: a second run changes nothing.
    assert repair_premades(out_dir=tmp_path) == 0
    assert p.read_text(encoding="utf-8") == text


def test_repair_moves_commander_out_of_main_and_tops_up_basic(tmp_path):
    """Broken variant where the commander DID land in [Main] (payload
    name matched under a different casing path): the repair moves it to
    [Commander] and tops the mainboard back up via the basics."""
    p = _broken_edhrec_premade(
        tmp_path,
        main_lines=["1 Urtet, Remnant of Memnarch", "1 Sol Ring",
                    "97 Island"],
    )
    assert repair_premades(out_dir=tmp_path) == 0
    text = p.read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1
    assert count_main_cards(text) == 99
    main = text.split("[Main]")[1]
    assert "Urtet" not in main
    assert "98 Island" in main                    # topped up 97 → 98


def test_repair_partner_pair_trims_main_to_98(tmp_path, monkeypatch):
    _stub_partner_lookup(monkeypatch, {"Alpha One", "Beta Two"})
    p = _broken_edhrec_premade(
        tmp_path, commander="Alpha One // Beta Two",
        main_lines=["1 Sol Ring", "1 Arcane Signet", "97 Island"],
    )
    assert repair_premades(out_dir=tmp_path) == 0
    text = p.read_text(encoding="utf-8")
    assert count_commander_cards(text) == 2
    assert count_main_cards(text) == 98           # 100 - 2, Island 97 → 96
    assert "96 Island" in text.split("[Main]")[1]


def test_repair_skips_healthy_edhrec_and_non_edhrec_files(tmp_path):
    healthy = tmp_path / "[PREMADE] EDHREC Fine Cmdr [B2].dck"
    healthy.write_text(
        "[metadata]\nName=[PREMADE] EDHREC Fine Cmdr [B2]\nSource=edhrec\n"
        "[Commander]\n1 Fine Cmdr\n[Main]\n99 Island\n", encoding="utf-8")
    mox = tmp_path / "[PREMADE] MoxDeck [B3].dck"
    mox.write_text(
        "[metadata]\nName=[PREMADE] MoxDeck [B3]\nSource=moxfield\n"
        "[Commander]\n1 Mox Cmdr|ABC|1\n[Main]\n99 Island\n",
        encoding="utf-8")
    user = tmp_path / "[USER] Mine [B3].dck"
    user.write_text(
        "[metadata]\nName=[USER] Mine [B3]\n[Main]\n1 Forest\n",
        encoding="utf-8")
    before = {p.name: p.read_text(encoding="utf-8")
              for p in tmp_path.glob("*.dck")}
    assert repair_premades(out_dir=tmp_path) == 0
    after = {p.name: p.read_text(encoding="utf-8")
             for p in tmp_path.glob("*.dck")}
    assert after == before


def test_repair_falls_back_to_filename_stem_without_displayname(tmp_path):
    p = tmp_path / "[PREMADE] EDHREC Nekusar, the Mindrazer [B4].dck"
    p.write_text(
        "[metadata]\nName=[PREMADE] EDHREC Nekusar, the Mindrazer [B4]\n"
        "Source=edhrec\nSalt=1.50\n[Main]\n1 Sol Ring\n98 Island\n",
        encoding="utf-8")
    assert repair_premades(out_dir=tmp_path) == 0
    text = p.read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1
    assert "1 Nekusar, the Mindrazer" in \
        text.split("[Commander]")[1].split("[Main]")[0]


def test_repair_premade_text_leaves_deck_short_without_basics():
    """No basic-land line to absorb the rebalance → deck stays short
    rather than inventing cards (mirrors to_moxfield_shape's stance)."""
    text = ("[metadata]\nName=x\nDisplayName=EDHREC Average — Cmdr X\n"
            "Source=edhrec\n[Main]\n1 Sol Ring\n1 Arcane Signet\n")
    fixed = repair_premade_text(text, ["Cmdr X"])
    assert count_commander_cards(fixed) == 1
    assert count_main_cards(fixed) == 2           # untouched, just short


def test_repaired_file_passes_advisor_commander_detection(tmp_path):
    """The exact failure this fixes: improvement_advisor's commander
    parse raised 'no commanders found' on broken premades. After repair
    it must find the commander (pure file parse — no network)."""
    from commander_builder.improvement_advisor import (
        _parse_commander_names_from_dck,
    )
    p = _broken_edhrec_premade(tmp_path)
    assert _parse_commander_names_from_dck(p) == []      # the bug
    assert repair_premades(out_dir=tmp_path) == 0
    assert _parse_commander_names_from_dck(p) == [
        "Urtet, Remnant of Memnarch"]


def test_freshly_imported_premade_passes_advisor_commander_detection(
        tmp_path, monkeypatch):
    from commander_builder.improvement_advisor import (
        _parse_commander_names_from_dck,
    )
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Tergrid, God of Fright",
                              num_decks=50000)],
        salt={},
    )
    out = import_edhrec_premades(count=1, out_dir=tmp_path)
    assert _parse_commander_names_from_dck(Path(out[0]["path"])) == [
        "Tergrid, God of Fright"]


# ---------------------------------------------------------------------------
# Combined pull + CLI wiring
# ---------------------------------------------------------------------------

def test_run_premade_pull_shares_diversity_across_sources(
        tmp_path, monkeypatch, capsys):
    """One taken-set across both legs: the Moxfield pick's commander
    blocks the same commander on the EDHREC leg."""
    rows = [_search_row("p1", "Cmdr Shared", 900)]
    decks = {"p1": _deck_json("p1", "MoxDeck", "Cmdr Shared", 3, 900)}
    _stub_moxfield(monkeypatch, rows, decks)
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name="Cmdr Shared", num_decks=900),
                    CardEntry(name="Cmdr Edh", num_decks=800)],
        salt={},
    )
    # run_premade_pull uses the default politeness sleep; null it out.
    monkeypatch.setattr(premade_import.time, "sleep", lambda s: None)
    rc = run_premade_pull(moxfield_count=1, edhrec_count=1, out_dir=tmp_path)
    assert rc == 0
    names = sorted(p.name for p in tmp_path.glob("*.dck"))
    assert names == [
        "[PREMADE] EDHREC Cmdr Edh [B2].dck",
        "[PREMADE] MoxDeck [B3].dck",
    ]
    # Summary table prints one row per deck with source + metric.
    out = capsys.readouterr().out
    assert "Premade pull summary (2 decks)" in out
    assert "likes=900" in out
    assert "salt=0.00" in out


def test_run_premade_pull_returns_1_when_nothing_written(
        tmp_path, monkeypatch):
    _stub_moxfield(monkeypatch, [], {})
    monkeypatch.setattr(edhrec_client, "fetch_top_commanders",
                        lambda **_kw: [])
    monkeypatch.setattr(edhrec_client, "fetch_salt_list", lambda **_kw: {})
    assert run_premade_pull(1, 1, out_dir=tmp_path) == 1


def test_cli_premade_flag_runs_pull_with_defaults(monkeypatch):
    calls = {}

    def fake_pull(moxfield_count, edhrec_count):
        calls["counts"] = (moxfield_count, edhrec_count)
        return 0

    monkeypatch.setattr(premade_import, "run_premade_pull", fake_pull)
    assert moxfield_import.main(["--premade"]) == 0
    assert calls["counts"] == (10, 10)


def test_cli_premade_count_flags_imply_premade(monkeypatch):
    calls = {}

    def fake_pull(moxfield_count, edhrec_count):
        calls["counts"] = (moxfield_count, edhrec_count)
        return 0

    monkeypatch.setattr(premade_import, "run_premade_pull", fake_pull)
    assert moxfield_import.main(
        ["--premade-moxfield", "20", "--premade-edhrec", "20"]) == 0
    assert calls["counts"] == (20, 20)


def test_cli_premade_failure_propagates(monkeypatch):
    monkeypatch.setattr(premade_import, "run_premade_pull",
                        lambda **_kw: 1)
    assert moxfield_import.main(["--premade"]) == 1


def test_cli_premade_repair_flag_runs_repair(monkeypatch):
    calls = {"n": 0}

    def fake_repair(*a, **kw):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(premade_import, "repair_premades", fake_repair)
    assert moxfield_import.main(["--premade-repair"]) == 0
    assert calls["n"] == 1


def test_cli_premade_repair_failure_propagates(monkeypatch):
    monkeypatch.setattr(premade_import, "repair_premades", lambda **_kw: 2)
    assert moxfield_import.main(["--premade-repair"]) == 1


# ---------------------------------------------------------------------------
# Robustness — post-fetch validation, offline degrade, version-token stems,
# long/hostile names, write containment, repair honesty, partner diversity
# ---------------------------------------------------------------------------

def test_moxfield_premades_skip_deck_with_no_commanders(
        tmp_path, monkeypatch, capsys):
    """A fetched deck with an EMPTY commanders board must be skipped with
    a reason — written verbatim it would be permanently unusable (the
    advisor raises 'no commanders found' and the repair path only covers
    EDHREC files)."""
    rows = [_search_row("p1", "Cmdr Broken", 900),
            _search_row("p2", "Cmdr Ok", 100)]
    broken = _deck_json("p1", "Broken", "Cmdr Broken", 3, 900)
    broken["boards"]["commanders"]["cards"] = {}
    decks = {"p1": broken, "p2": _deck_json("p2", "Ok", "Cmdr Ok", 3, 100)}
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=1, out_dir=tmp_path, sleep_sec=0)
    assert [Path(r["path"]).name for r in out] == ["[PREMADE] Ok [B3].dck"]
    assert "SKIP p1: fetched deck has no commanders" in \
        capsys.readouterr().out
    assert not (tmp_path / "[PREMADE] Broken [B3].dck").exists()


def test_moxfield_premades_skip_illegal_main_count(
        tmp_path, monkeypatch, capsys):
    """Decks off the `main == 100 - commanders` invariant (98 or 101)
    are skipped with a reason; the ranking backfills."""
    rows = [_search_row("p1", "Cmdr Short", 900),
            _search_row("p2", "Cmdr Long", 800),
            _search_row("p3", "Cmdr Ok", 100)]
    short = _deck_json("p1", "Short", "Cmdr Short", 3, 900)
    short["boards"]["mainboard"]["cards"]["m3"]["quantity"] = 96   # 98 main
    long_ = _deck_json("p2", "Long", "Cmdr Long", 3, 800)
    long_["boards"]["mainboard"]["cards"]["m3"]["quantity"] = 99   # 101 main
    decks = {"p1": short, "p2": long_,
             "p3": _deck_json("p3", "Ok", "Cmdr Ok", 3, 100)}
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=1, out_dir=tmp_path, sleep_sec=0)
    assert [Path(r["path"]).name for r in out] == ["[PREMADE] Ok [B3].dck"]
    printed = capsys.readouterr().out
    assert ("SKIP p1: illegal deck size "
            "(98 main + 1 commander(s) != 100)") in printed
    assert ("SKIP p2: illegal deck size "
            "(101 main + 1 commander(s) != 100)") in printed
    assert [p.name for p in tmp_path.glob("*.dck")] == \
        ["[PREMADE] Ok [B3].dck"]


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("connection refused"),
    urllib.error.HTTPError("https://x", 503, "unavailable", {}, None),
    http.client.BadStatusLine("garbage"),
])
def test_commander_card_names_network_failure_degrades(monkeypatch, exc):
    """Non-404 network failures on the partner-vs-DFC lookup degrade to
    the documented single-commander shape instead of raising out of the
    whole pull/repair."""
    def _down(name, **_kw):
        raise exc
    monkeypatch.setattr(premade_import, "lookup_card", _down)
    pair = "Alpha One // Beta Two"
    assert _commander_card_names(pair) == [pair]


def test_edhrec_pull_survives_partner_lookup_network_failure(
        tmp_path, monkeypatch):
    """A partner-pair EDHREC commander + a dead network must not abort
    the pull: the pair degrades to one [Commander] line and the walk
    continues to the next candidate."""
    def _down(name, **_kw):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(premade_import, "lookup_card", _down)
    pair = "Alpha One // Beta Two"
    _stub_edhrec(
        monkeypatch,
        commanders=[CardEntry(name=pair, num_decks=900),
                    CardEntry(name="Cmdr Next", num_decks=800)],
        salt={},
    )
    out = import_edhrec_premades(count=2, out_dir=tmp_path)
    assert len(out) == 2
    text = Path(out[0]["path"]).read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1
    assert f"1 {pair}" in text.split("[Commander]")[1].split("[Main]")[0]


def test_premade_destination_strips_trailing_version_token(tmp_path):
    """A deck literally NAMED 'Something v2' must not mint a stem that
    parses as a version snapshot of root 'Something'."""
    assert _premade_destination("Hot Deck v2", 3, tmp_path).name == \
        "[PREMADE] Hot Deck [B3].dck"
    # Stacked tokens all strip; non-token names pass through untouched.
    assert _premade_destination("Foo v2 v3", 3, tmp_path).name == \
        "[PREMADE] Foo [B3].dck"
    assert _premade_destination("v2", 3, tmp_path).name == \
        "[PREMADE] v2 [B3].dck"


def test_v2_named_premade_mintable_and_distinct_root_not_blocked(
        tmp_path, monkeypatch):
    """The confounded pair: a pulled deck named 'Foo v2' plus a DISTINCT
    deck named 'Foo'. Both must land as mintable BASES — the first under
    a version-token-free stem (pretty name kept in DisplayName=), the
    second under a uniquify counter (a different deck, not a version)."""
    from commander_builder.premade_mint import premade_bases_without_v2
    rows = [_search_row("p1", "Cmdr A", 900),
            _search_row("p2", "Cmdr B", 800)]
    decks = {"p1": _deck_json("p1", "Foo v2", "Cmdr A", 3, 900),
             "p2": _deck_json("p2", "Foo", "Cmdr B", 3, 800)}
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=2, out_dir=tmp_path, sleep_sec=0)
    names = [Path(r["path"]).name for r in out]
    assert names == ["[PREMADE] Foo [B3].dck", "[PREMADE] Foo (2) [B3].dck"]
    # Neither file reads as an already-minted v2: both are coverage-free
    # bases for premade_mint.
    bases = premade_bases_without_v2(tmp_path)
    assert sorted(p.name for p in bases) == sorted(names)
    # The pretty Moxfield name survives in DisplayName=.
    text = (tmp_path / "[PREMADE] Foo [B3].dck").read_text(encoding="utf-8")
    assert "DisplayName=Foo v2" in text


def test_moxfield_premades_long_author_name_written_not_fatal(
        tmp_path, monkeypatch):
    """A 300+ char author-controlled deck name lands under a truncated
    filename instead of aborting the leg with a Windows path OSError."""
    long_name = "Very Long Deck Name " * 20        # ~400 chars
    rows = [_search_row("p1", "Cmdr A", 900)]
    decks = {"p1": _deck_json("p1", long_name, "Cmdr A", 3, 900)}
    _stub_moxfield(monkeypatch, rows, decks)
    out = import_moxfield_premades(count=1, out_dir=tmp_path, sleep_sec=0)
    assert len(out) == 1
    dest = Path(out[0]["path"])
    assert dest.exists()
    assert len(dest.name) < 255


def test_moxfield_premades_write_error_contained_leg_continues(
        tmp_path, monkeypatch, capsys):
    """A per-deck failure AFTER the fetch (render/stamp/write) is
    contained like a fetch failure: logged, and the leg moves on."""
    rows = [_search_row("p1", "Cmdr Bad", 900),
            _search_row("p2", "Cmdr Ok", 100)]
    decks = {"p1": _deck_json("p1", "Bad", "Cmdr Bad", 3, 900),
             "p2": _deck_json("p2", "Ok", "Cmdr Ok", 3, 100)}
    _stub_moxfield(monkeypatch, rows, decks)
    real_stamp = premade_import.stamp_name_preserving_display

    def _flaky(dck, stem):
        if "Bad" in stem:
            raise OSError("disk exploded")
        return real_stamp(dck, stem)

    monkeypatch.setattr(
        premade_import, "stamp_name_preserving_display", _flaky)
    out = import_moxfield_premades(count=2, out_dir=tmp_path, sleep_sec=0)
    assert [Path(r["path"]).name for r in out] == ["[PREMADE] Ok [B3].dck"]
    assert "ERROR writing p1: OSError: disk exploded" in \
        capsys.readouterr().out


def test_repair_reports_residual_size_illegal_deck(tmp_path, capsys):
    """Repair honesty: a basic-less deck the rebalance cannot fix keeps
    its retrofitted [Commander] but is reported as a FAILURE (nonzero
    return), never stamped 'repaired' with exit 0."""
    p = _broken_edhrec_premade(
        tmp_path, main_lines=["1 Sol Ring", "1 Arcane Signet"])
    assert repair_premades(out_dir=tmp_path) == 1
    text = p.read_text(encoding="utf-8")
    assert count_commander_cards(text) == 1        # retrofit still applied
    assert count_main_cards(text) == 2             # residually short
    printed = capsys.readouterr().out
    assert "still size-illegal after repair" in printed
    assert "0 repaired, 1 failed" in printed


def test_edhrec_partner_pair_blocks_both_names_same_run(
        tmp_path, monkeypatch):
    """Diversity records BOTH partner names: after 'Alpha One // Beta
    Two' is written, a same-run 'Beta Two' candidate is skipped and the
    ranking backfills."""
    _stub_partner_lookup(monkeypatch, {"Alpha One", "Beta Two"})
    _stub_edhrec(
        monkeypatch,
        commanders=[
            CardEntry(name="Alpha One // Beta Two", num_decks=900),
            CardEntry(name="Beta Two", num_decks=800),
            CardEntry(name="Cmdr Other", num_decks=700),
        ],
        salt={},
    )
    out = import_edhrec_premades(count=2, out_dir=tmp_path)
    assert [r["name"] for r in out] == [
        "[PREMADE] EDHREC Alpha One __ Beta Two [B2]",
        "[PREMADE] EDHREC Cmdr Other [B2]",
    ]
