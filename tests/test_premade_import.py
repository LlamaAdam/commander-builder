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

from pathlib import Path

import pytest

from commander_builder import edhrec_client, moxfield_import, premade_import
from commander_builder.edhrec_client import AverageDeck, CardEntry
from commander_builder.premade_import import (
    PREMADE_PREFIX,
    _premade_destination,
    existing_commander_names,
    import_edhrec_premades,
    import_moxfield_premades,
    run_premade_pull,
)


# ---------------------------------------------------------------------------
# Stub payload builders
# ---------------------------------------------------------------------------

def _deck_json(pid: str, name: str, commander: str,
               bracket: int | None = None, likes: int = 0) -> dict:
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
    from commander_builder import run_match
    monkeypatch.setattr(run_match, "DECK_DIR", tmp_path)
    for n in ("Alpha [B3].dck", "Beta [B3].dck",
              "[USER] Mine [B3].dck", "[PREMADE] Hot [B3].dck"):
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
