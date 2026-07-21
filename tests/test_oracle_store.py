"""Tests for the FP-009 oracle-text card-reference store.

Network is stubbed: ``scryfall_client.CACHE_DIR`` is redirected to a tmp
dir, and ``lookup_card`` / ``refresh_card`` are monkeypatched, so nothing
talks to Scryfall.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder import oracle_store, scryfall_client


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "oracle_snapshots"
    d.mkdir()
    monkeypatch.setattr(scryfall_client, "CACHE_DIR", d)
    return d


def _write_snapshot(name: str, oracle: str) -> Path:
    """Write a cached snapshot via the real slug logic so check_errata
    finds it at the expected path."""
    p = scryfall_client._cache_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "oracle_text": oracle}), encoding="utf-8")
    return p


# --- iter_cached_names / snapshot_age -------------------------------------

def test_iter_cached_names(cache_dir):
    _write_snapshot("Llanowar Elves", "{T}: Add {G}.")
    _write_snapshot("Sol Ring", "{T}: Add {C}{C}.")
    assert set(oracle_store.iter_cached_names()) == {"Llanowar Elves", "Sol Ring"}


def test_iter_cached_names_skips_corrupt(cache_dir):
    _write_snapshot("Good", "x")
    (cache_dir / "bad.json").write_text("{not json", encoding="utf-8")
    assert list(oracle_store.iter_cached_names()) == ["Good"]


def test_snapshot_age_none_when_uncached(cache_dir):
    assert oracle_store.snapshot_age_days("Nope") is None


def test_snapshot_age_recent(cache_dir):
    _write_snapshot("Sol Ring", "x")
    age = oracle_store.snapshot_age_days("Sol Ring")
    assert age is not None and age < 1.0


# --- check_errata ---------------------------------------------------------

def test_check_errata_not_cached(cache_dir):
    res = oracle_store.check_errata("Ghost Card")
    assert res["status"] == "not_cached"
    assert res["changed"] is False


def test_check_errata_detects_drift(cache_dir, monkeypatch):
    _write_snapshot("Sol Ring", "Add {C}{C}.")
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"name": name, "oracle_text": "{T}: Add {C}{C}."})
    res = oracle_store.check_errata("Sol Ring")
    assert res["status"] == "ok"
    assert res["changed"] is True
    assert res["before"] == "Add {C}{C}."
    assert res["after"] == "{T}: Add {C}{C}."


def test_check_errata_no_drift(cache_dir, monkeypatch):
    _write_snapshot("Sol Ring", "{T}: Add {C}{C}.")
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"name": name, "oracle_text": "{T}: Add {C}{C}."})
    res = oracle_store.check_errata("Sol Ring")
    assert res["status"] == "ok"
    assert res["changed"] is False


def test_check_errata_upstream_404(cache_dir, monkeypatch):
    _write_snapshot("Phantom", "old text")
    monkeypatch.setattr(scryfall_client, "lookup_card", lambda name, cache=True: None)
    res = oracle_store.check_errata("Phantom")
    assert res["status"] == "upstream_404"
    assert res["before"] == "old text"


def test_check_errata_corrupt_snapshot(cache_dir):
    scryfall_client._cache_path("Broken").write_text("{nope", encoding="utf-8")
    res = oracle_store.check_errata("Broken")
    assert res["status"] == "corrupt"


# --- bulk_refresh ---------------------------------------------------------

def test_bulk_refresh_reports_without_writing(cache_dir, monkeypatch):
    _write_snapshot("A", "old-a")
    _write_snapshot("B", "same-b")
    fresh = {"A": "new-a", "B": "same-b"}
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"name": name, "oracle_text": fresh[name]})
    refreshed_calls = []
    monkeypatch.setattr(scryfall_client, "refresh_card",
                        lambda name: refreshed_calls.append(name))

    summary = oracle_store.bulk_refresh(["A", "B"], write=False)
    assert summary["checked"] == 2
    assert summary["changed"] == 1   # only A drifted
    assert summary["refreshed"] == 0  # write=False
    assert refreshed_calls == []      # no rewrite happened


def test_bulk_refresh_writes_when_drifted(cache_dir, monkeypatch):
    _write_snapshot("A", "old-a")
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"name": name, "oracle_text": "new-a"})
    refreshed_calls = []
    monkeypatch.setattr(scryfall_client, "refresh_card",
                        lambda name: refreshed_calls.append(name))

    summary = oracle_store.bulk_refresh(["A"], write=True)
    assert summary["changed"] == 1
    assert summary["refreshed"] == 1
    assert refreshed_calls == ["A"]
    assert summary["results"][0]["refreshed"] is True


def test_bulk_refresh_stale_days_skips_fresh(cache_dir, monkeypatch):
    _write_snapshot("Fresh", "x")  # just written -> age ~0
    called = []
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: called.append(name) or {"oracle_text": "y"})
    summary = oracle_store.bulk_refresh(["Fresh"], stale_days=7)
    assert summary["skipped"] == 1
    assert summary["results"][0]["status"] == "skipped_fresh"
    assert called == []  # never hit the network for a fresh snapshot


def test_bulk_refresh_walks_whole_store_when_names_none(cache_dir, monkeypatch):
    _write_snapshot("A", "a")
    _write_snapshot("B", "b")
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"oracle_text": "a" if name == "A" else "b"})
    summary = oracle_store.bulk_refresh(None)
    assert summary["checked"] == 2


# --- names_from_deck + presentation alias ---------------------------------

def test_names_from_deck(tmp_path):
    deck = tmp_path / "d.dck"
    deck.write_text(
        "[metadata]\nName=T\n\n[Commander]\n1 Atraxa, Praetors' Voice\n\n"
        "[Main]\n1 Sol Ring|C16|1\n1 Cultivate\n1 Sol Ring|LEA|1\n",
        encoding="utf-8",
    )
    names = oracle_store.names_from_deck(deck)
    # Distinct, first-seen order; |SET|CN suffix stripped; dupes collapsed.
    assert names == ["Atraxa, Praetors' Voice", "Sol Ring", "Cultivate"]


def test_card_reference_is_presentation_helper():
    # Public alias points at the existing formatter.
    assert oracle_store.card_reference is scryfall_client.format_card_for_display


# --- CLI ------------------------------------------------------------------

def test_cli_deck_not_found(capsys):
    rc = oracle_store.main(["--deck", "/no/such.dck"])
    assert rc == 2


def test_cli_name_mode_json(cache_dir, monkeypatch, capsys):
    _write_snapshot("Sol Ring", "old")
    monkeypatch.setattr(scryfall_client, "lookup_card",
                        lambda name, cache=True: {"oracle_text": "new"})
    rc = oracle_store.main(["--name", "Sol Ring", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checked"] == 1 and out["changed"] == 1
