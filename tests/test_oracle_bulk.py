"""Tests for the Scryfall bulk-data snapshot path + per-card 429 backoff.

Everything is offline: the bulk index / bulk payload / per-card fetches
are injected callables or monkeypatched module seams, and
``scryfall_client.CACHE_DIR`` is redirected to a tmp dir (the bulk dir
derives from it at call time).
"""
from __future__ import annotations

import email.message
import io
import json
import os
import time
import urllib.error
from pathlib import Path

import pytest

from commander_builder import oracle_store, scryfall_client


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "oracle_snapshots"
    d.mkdir()
    monkeypatch.setattr(scryfall_client, "CACHE_DIR", d)
    return d


def http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", hdrs, None)


SOL_RING = {
    "name": "Sol Ring",
    "oracle_text": "{T}: Add {C}{C}.",
    "type_line": "Artifact",
    "color_identity": [],
    "cmc": 1.0,
}
DELVER = {
    "name": "Delver of Secrets // Insectile Aberration",
    "layout": "transform",
    "type_line": "Creature — Human Wizard // Creature — Human Insect",
    "color_identity": ["U"],
    "card_faces": [
        {"name": "Delver of Secrets", "oracle_text": "At the beginning..."},
        {"name": "Insectile Aberration", "oracle_text": "Flying"},
    ],
}
CULTIVATE = {
    "name": "Cultivate",
    "oracle_text": "Search your library...",
    "type_line": "Sorcery",
    "color_identity": ["G"],
}


def _write_bulk(tmp_path, cards, filename="bulk.json") -> Path:
    p = tmp_path / filename
    p.write_text(json.dumps(cards), encoding="utf-8")
    return p


# --- download_bulk_oracle ---------------------------------------------------

def _index_payload(uri="https://data.scryfall.io/oracle-cards.json"):
    return {
        "data": [
            {"type": "default_cards", "download_uri": "https://x/other"},
            {"type": "oracle_cards", "download_uri": uri},
        ],
    }


def test_download_writes_dated_file_and_streams_payload(cache_dir):
    payload = json.dumps([SOL_RING]).encode("utf-8")
    fetched_urls = []

    def fetch_json(url):
        fetched_urls.append(url)
        return _index_payload()

    opened = []

    def open_stream(uri):
        opened.append(uri)
        return io.BytesIO(payload)

    dest = oracle_store.download_bulk_oracle(
        fetch_json=fetch_json, open_stream=open_stream)
    assert fetched_urls == [oracle_store.BULK_INDEX_URL]
    assert opened == ["https://data.scryfall.io/oracle-cards.json"]
    assert dest.parent == oracle_store.bulk_data_dir()
    stamp = time.strftime("%Y%m%d")
    assert dest.name == f"oracle-cards-{stamp}.json"
    assert dest.read_bytes() == payload
    # The .part temp file was renamed away, not left behind.
    assert list(dest.parent.glob("*.part")) == []


def test_download_skips_when_fresh_copy_exists(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    existing = d / "oracle-cards-20990101.json"
    existing.write_text("[]", encoding="utf-8")

    def boom(url):  # pragma: no cover - must not be called
        raise AssertionError("network touched despite fresh local copy")

    dest = oracle_store.download_bulk_oracle(
        fetch_json=boom, open_stream=boom)
    assert dest == existing


def test_download_redownloads_when_local_copy_stale(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    stale = d / "oracle-cards-20200101.json"
    stale.write_text("[]", encoding="utf-8")
    old = time.time() - (oracle_store.BULK_FRESH_DAYS + 1) * 86400
    os.utime(stale, (old, old))

    dest = oracle_store.download_bulk_oracle(
        fetch_json=lambda url: _index_payload(),
        open_stream=lambda uri: io.BytesIO(b"[]"),
    )
    assert dest != stale
    assert dest.name == f"oracle-cards-{time.strftime('%Y%m%d')}.json"


def test_download_force_overrides_freshness(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    fresh = d / "oracle-cards-20990101.json"
    fresh.write_text("[]", encoding="utf-8")
    calls = []

    dest = oracle_store.download_bulk_oracle(
        force=True,
        fetch_json=lambda url: calls.append(url) or _index_payload(),
        open_stream=lambda uri: io.BytesIO(b"[1]"),
    )
    assert calls == [oracle_store.BULK_INDEX_URL]
    assert dest.read_text(encoding="utf-8") == "[1]"


def test_download_errors_when_no_oracle_cards_entry(cache_dir):
    with pytest.raises(RuntimeError, match="oracle_cards"):
        oracle_store.download_bulk_oracle(
            fetch_json=lambda url: {"data": [{"type": "rulings"}]},
            open_stream=lambda uri: io.BytesIO(b"[]"),
        )


# --- write_snapshots_from_bulk ----------------------------------------------

def test_write_snapshots_targets_only(cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [SOL_RING, CULTIVATE, DELVER])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Sol Ring"], bulk_path=bulk)
    assert summary == {"written": 1, "missing": [], "targets": 1}
    # Snapshot readable through the existing lookup path, offline.
    card = scryfall_client.lookup_card("Sol Ring", cache_only=True)
    assert card is not None and card["oracle_text"] == "{T}: Add {C}{C}."
    # Non-target cards were NOT written.
    assert scryfall_client.lookup_card("Cultivate", cache_only=True) is None
    assert len(list(cache_dir.glob("*.json"))) == 1


def test_write_snapshots_dfc_front_face_lookup_hits(cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [DELVER])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Delver of Secrets"], bulk_path=bulk)
    assert summary["written"] == 1 and summary["missing"] == []
    # Front-face lookup returns the FULL card object, exactly like a
    # /cards/named?exact=Delver+of+Secrets response would have cached.
    card = scryfall_client.lookup_card("Delver of Secrets", cache_only=True)
    assert card is not None
    assert card["name"] == "Delver of Secrets // Insectile Aberration"
    assert card["card_faces"][0]["name"] == "Delver of Secrets"


def test_write_snapshots_full_dfc_name_also_resolves(cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [DELVER])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Delver of Secrets // Insectile Aberration"], bulk_path=bulk)
    assert summary["written"] == 1
    card = scryfall_client.lookup_card(
        "Delver of Secrets // Insectile Aberration", cache_only=True)
    assert card is not None


def test_write_snapshots_reports_missing(cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [SOL_RING])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Sol Ring", "Not A Real Card"], bulk_path=bulk)
    assert summary["written"] == 1
    assert summary["missing"] == ["Not A Real Card"]
    assert summary["targets"] == 2


def test_write_snapshots_case_insensitive_targets(cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [SOL_RING])
    summary = oracle_store.write_snapshots_from_bulk(
        ["sol ring"], bulk_path=bulk)
    assert summary["written"] == 1 and summary["missing"] == []


def test_write_snapshots_everything_writes_all_plus_face_alias(
        cache_dir, tmp_path):
    bulk = _write_bulk(tmp_path, [SOL_RING, DELVER])
    summary = oracle_store.write_snapshots_from_bulk(
        None, bulk_path=bulk, everything=True)
    # Sol Ring + full DFC name + DFC front-face alias = 3 files.
    assert summary["written"] == 3
    assert scryfall_client.lookup_card("Sol Ring", cache_only=True)
    assert scryfall_client.lookup_card("Delver of Secrets", cache_only=True)
    assert scryfall_client.lookup_card(
        "Delver of Secrets // Insectile Aberration", cache_only=True)


def test_bulk_name_index_full_name_beats_face_name():
    real = {"name": "Bala Ged Recovery // Bala Ged Sanctuary",
            "card_faces": [{"name": "Bala Ged Recovery"},
                           {"name": "Bala Ged Sanctuary"}]}
    impostor = {"name": "Impostor",
                "card_faces": [{"name": "Bala Ged Recovery // Bala Ged Sanctuary"}]}
    index = oracle_store._bulk_name_index([impostor, real])
    key = "bala ged recovery // bala ged sanctuary"
    assert index[key] is real  # full name wins over a face-name collision
    assert index["bala ged recovery"] is real


# --- names_from_deck_dir ----------------------------------------------------

def _write_deck(path: Path, cards: list[str]) -> None:
    lines = ["[metadata]", "Name=T", "", "[Main]"]
    lines += [f"1 {c}" for c in cards]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_names_from_deck_dir_dedupes_across_decks(tmp_path):
    _write_deck(tmp_path / "a.dck", ["Sol Ring", "Cultivate"])
    _write_deck(tmp_path / "b.dck", ["sol ring", "Delver of Secrets"])
    names = oracle_store.names_from_deck_dir(tmp_path)
    assert names == ["Sol Ring", "Cultivate", "Delver of Secrets"]


# --- per-card 429 backoff ---------------------------------------------------

def test_retry_honors_retry_after(cache_dir):
    sleeps = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(429, retry_after="3")
        return {"ok": True}

    out = oracle_store._call_with_retry(fn, "X", sleep=sleeps.append)
    assert out == {"ok": True}
    assert sleeps == [3.0]


def test_retry_clamps_absurd_retry_after(cache_dir):
    from commander_builder.edhrec_client import MAX_RETRY_AFTER_SEC
    sleeps = []
    state = {"n": 0}

    def fn():
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(429, retry_after="900")
        return {}

    oracle_store._call_with_retry(fn, "X", sleep=sleeps.append)
    assert sleeps == [MAX_RETRY_AFTER_SEC]


def test_retry_exponential_fallback_and_bounded_budget(cache_dir):
    sleeps = []
    calls = []

    def fn():
        calls.append(1)
        raise http_error(429)  # no Retry-After -> exponential fallback

    with pytest.raises(urllib.error.HTTPError):
        oracle_store._call_with_retry(fn, "X", sleep=sleeps.append)
    assert len(calls) == oracle_store.MAX_RETRIES + 1
    assert sleeps == [oracle_store.RETRY_BASE_DELAY_SEC * (2 ** a)
                      for a in range(oracle_store.MAX_RETRIES)]


def test_retry_non_retryable_code_raises_immediately(cache_dir):
    calls = []

    def fn():
        calls.append(1)
        raise http_error(403)

    with pytest.raises(urllib.error.HTTPError):
        oracle_store._call_with_retry(fn, "X", sleep=lambda s: None)
    assert calls == [1]  # deterministic 4xx: no retries


def _snapshot(name: str, oracle: str) -> None:
    p = scryfall_client._cache_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"name": name, "oracle_text": oracle}),
                 encoding="utf-8")


def test_bulk_refresh_survives_persistent_429(cache_dir, monkeypatch, capsys):
    _snapshot("A", "old-a")
    _snapshot("B", "old-b")

    def rate_limited(name, cache=True):
        if name == "A":
            raise http_error(429, retry_after="0")
        return {"name": name, "oracle_text": "old-b"}

    monkeypatch.setattr(scryfall_client, "lookup_card", rate_limited)
    summary = oracle_store.bulk_refresh(["A", "B"], sleep=lambda s: None)
    # A degraded loudly; the run CONTINUED and still checked B.
    assert summary["checked"] == 2
    assert summary["errors"] == 1
    by_name = {r["name"]: r for r in summary["results"]}
    assert by_name["A"]["status"] == "http_error"
    assert by_name["A"]["error"] == "HTTP 429"
    assert by_name["B"]["status"] == "ok"
    err = capsys.readouterr().err
    assert "giving up on 'A'" in err and "HTTP 429" in err


def test_bulk_refresh_recovers_after_transient_429(cache_dir, monkeypatch):
    _snapshot("A", "old-a")
    state = {"n": 0}

    def flaky(name, cache=True):
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(429, retry_after="0")
        return {"name": name, "oracle_text": "new-a"}

    monkeypatch.setattr(scryfall_client, "lookup_card", flaky)
    summary = oracle_store.bulk_refresh(["A"], sleep=lambda s: None)
    assert summary["errors"] == 0
    assert summary["changed"] == 1
    assert summary["results"][0]["status"] == "ok"


# --- loud cards-dir fallback ------------------------------------------------

def test_fallback_logs_where_snapshots_live(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MTG_CARDS_DIR", raising=False)
    monkeypatch.setattr(scryfall_client, "_CANONICAL_CARDS_DIR",
                        tmp_path / "definitely_absent")
    resolved = scryfall_client._resolve_cards_dir()
    assert resolved == scryfall_client.REPO_ROOT / ".cache"
    err = capsys.readouterr().err
    assert "falling back" in err
    assert str(resolved / "scryfall") in err


def test_no_fallback_log_when_env_override_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MTG_CARDS_DIR", str(tmp_path))
    assert scryfall_client._resolve_cards_dir() == tmp_path
    assert capsys.readouterr().err == ""


def test_no_fallback_log_when_canonical_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MTG_CARDS_DIR", raising=False)
    canonical = tmp_path / "mtg_cards"
    canonical.mkdir()
    monkeypatch.setattr(scryfall_client, "_CANONICAL_CARDS_DIR", canonical)
    assert scryfall_client._resolve_cards_dir() == canonical
    assert capsys.readouterr().err == ""


# --- CLI --------------------------------------------------------------------

def _stub_bulk_download(monkeypatch, tmp_path, cards):
    bulk = _write_bulk(tmp_path, cards, filename="stub-bulk.json")
    calls = []

    def fake_download(*, force=False):
        calls.append(force)
        return bulk

    monkeypatch.setattr(oracle_store, "download_bulk_oracle", fake_download)
    return bulk, calls


def test_cli_from_bulk_deck(cache_dir, tmp_path, monkeypatch, capsys):
    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING, CULTIVATE, DELVER])
    deck = tmp_path / "d.dck"
    deck.write_text(
        "[metadata]\nName=T\n\n[Main]\n1 Sol Ring\n1 Delver of Secrets\n",
        encoding="utf-8",
    )
    rc = oracle_store.main(["--from-bulk", "--deck", str(deck)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Wrote 2 snapshot(s)" in out
    assert scryfall_client.lookup_card("Sol Ring", cache_only=True)
    assert scryfall_client.lookup_card("Delver of Secrets", cache_only=True)
    # Deck didn't name Cultivate -> not written.
    assert scryfall_client.lookup_card("Cultivate", cache_only=True) is None


def test_cli_from_bulk_all_walks_deck_dir(cache_dir, tmp_path, monkeypatch,
                                          capsys):
    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING, CULTIVATE])
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    _write_deck(deck_dir / "a.dck", ["Sol Ring"])
    _write_deck(deck_dir / "b.dck", ["Cultivate", "Ghost Card"])
    rc = oracle_store.main(
        ["--from-bulk", "--all", "--deck-dir", str(deck_dir), "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] == 2
    assert summary["missing"] == ["Ghost Card"]


def test_cli_from_bulk_force_flag_passes_through(cache_dir, tmp_path,
                                                 monkeypatch, capsys):
    _bulk, calls = _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING])
    rc = oracle_store.main(
        ["--from-bulk", "--force-bulk", "--name", "Sol Ring"])
    assert rc == 0
    assert calls == [True]


def test_cli_from_bulk_missing_deck_dir_errors(cache_dir, tmp_path,
                                               monkeypatch, capsys):
    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING])
    rc = oracle_store.main(
        ["--from-bulk", "--all", "--deck-dir", str(tmp_path / "nope")])
    assert rc == 2
    assert "deck dir not found" in capsys.readouterr().out


def test_cli_everything_requires_from_bulk(capsys):
    rc = oracle_store.main(["--everything"])
    assert rc == 2
    assert "--from-bulk" in capsys.readouterr().out


def test_cli_from_bulk_everything(cache_dir, tmp_path, monkeypatch, capsys):
    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING, DELVER])
    rc = oracle_store.main(["--from-bulk", "--everything", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] == 3  # 2 cards + 1 front-face alias


def test_cli_from_bulk_download_failure_degrades_to_rc2(
        cache_dir, tmp_path, monkeypatch, capsys):
    def boom(*, force=False):
        raise urllib.error.URLError("no route to scryfall")

    monkeypatch.setattr(oracle_store, "download_bulk_oracle", boom)
    rc = oracle_store.main(["--from-bulk", "--name", "Sol Ring"])
    assert rc == 2
    assert "bulk download failed" in capsys.readouterr().out
