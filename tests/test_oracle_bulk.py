"""Tests for the Scryfall bulk-data snapshot path + per-card 429 backoff.

Everything is offline: the bulk index / bulk payload / per-card fetches
are injected callables or monkeypatched module seams, and
``scryfall_client.CACHE_DIR`` is redirected to a tmp dir (the bulk dir
derives from it at call time).
"""
from __future__ import annotations

import email.message
import gzip
import http.client
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


def _gz_jsonl(cards) -> bytes:
    """Cards as gzip-compressed JSONL — the live bulk format (2026-07)."""
    lines = "\n".join(json.dumps(c) for c in cards) + "\n"
    return gzip.compress(lines.encode("utf-8"))


def _write_bulk_jsonl_gz(tmp_path, cards, filename="bulk.jsonl.gz") -> Path:
    p = tmp_path / filename
    p.write_bytes(_gz_jsonl(cards))
    return p


# --- download_bulk_oracle ---------------------------------------------------

_JSONL_URI = "https://data.scryfall.io/oracle-cards/oracle-cards-20260729090224.jsonl.gz"


def _index_payload(uri=_JSONL_URI):
    """The LIVE index shape (verified 2026-07-29): jsonl_download_uri
    only — entries no longer carry download_uri."""
    return {
        "data": [
            {"object": "bulk_data", "type": "default_cards",
             "jsonl_download_uri": "https://x/other.jsonl.gz"},
            {"object": "bulk_data", "type": "oracle_cards",
             "updated_at": "2026-07-29T09:02:24.821+00:00",
             "jsonl_download_uri": uri,
             "compressed_size": 24332018},
        ],
    }


def _legacy_index_payload(uri="https://data.scryfall.io/oracle-cards.json"):
    """The pre-2026-07 index shape: plain-JSON-array download_uri."""
    return {
        "data": [
            {"type": "default_cards", "download_uri": "https://x/other"},
            {"type": "oracle_cards", "download_uri": uri},
        ],
    }


def test_download_live_index_shape_writes_dated_jsonl_gz(cache_dir):
    payload = _gz_jsonl([SOL_RING])
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
    assert opened == [_JSONL_URI]
    assert dest.parent == oracle_store.bulk_data_dir()
    stamp = time.strftime("%Y%m%d")
    assert dest.name == f"oracle-cards-{stamp}.jsonl.gz"
    assert dest.read_bytes() == payload  # stored compressed, byte-for-byte
    # The .part temp file was renamed away, not left behind.
    assert list(dest.parent.glob("*.part")) == []


def test_download_legacy_index_shape_falls_back_to_plain_json(cache_dir):
    payload = json.dumps([SOL_RING]).encode("utf-8")
    opened = []

    def open_stream(uri):
        opened.append(uri)
        return io.BytesIO(payload)

    dest = oracle_store.download_bulk_oracle(
        fetch_json=lambda url: _legacy_index_payload(),
        open_stream=open_stream)
    assert opened == ["https://data.scryfall.io/oracle-cards.json"]
    assert dest.name == f"oracle-cards-{time.strftime('%Y%m%d')}.json"
    assert dest.read_bytes() == payload


def test_download_prefers_jsonl_over_legacy_when_both_present(cache_dir):
    index = {
        "data": [{
            "type": "oracle_cards",
            "jsonl_download_uri": "https://x/oc.jsonl.gz",
            "download_uri": "https://x/oc.json",
        }],
    }
    opened = []
    dest = oracle_store.download_bulk_oracle(
        fetch_json=lambda url: index,
        open_stream=lambda uri: opened.append(uri) or io.BytesIO(b"x"),
    )
    assert opened == ["https://x/oc.jsonl.gz"]
    assert dest.name.endswith(".jsonl.gz")


@pytest.mark.parametrize("filename", [
    "oracle-cards-20990101.jsonl.gz",
    "oracle-cards-20990101.json",  # legacy copies stay reusable
])
def test_download_skips_when_fresh_copy_exists(cache_dir, filename):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    existing = d / filename
    existing.write_bytes(b"whatever")

    def boom(url):  # pragma: no cover - must not be called
        raise AssertionError("network touched despite fresh local copy")

    dest = oracle_store.download_bulk_oracle(
        fetch_json=boom, open_stream=boom)
    assert dest == existing


def test_download_ignores_leftover_part_files_for_freshness(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    (d / "oracle-cards-20990101.jsonl.gz.part").write_bytes(b"partial")

    dest = oracle_store.download_bulk_oracle(
        fetch_json=lambda url: _index_payload(),
        open_stream=lambda uri: io.BytesIO(_gz_jsonl([])),
    )
    assert dest.name == f"oracle-cards-{time.strftime('%Y%m%d')}.jsonl.gz"


def test_scan_sweeps_stale_part_files_keeps_recent_ones(cache_dir):
    """Interrupted downloads leave *.part files behind; the freshness
    scan cleans up stale ones. A RECENT .part may belong to a download
    in progress in another process and is left alone."""
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    real = d / "oracle-cards-20990101.jsonl.gz"
    real.write_bytes(b"whatever")
    stale_part = d / "oracle-cards-20200101.jsonl.gz.part"
    stale_part.write_bytes(b"partial")
    old = time.time() - oracle_store._PART_STALE_SEC - 60
    os.utime(stale_part, (old, old))
    live_part = d / "oracle-cards-20990102.jsonl.gz.part"
    live_part.write_bytes(b"streaming")

    found = oracle_store.find_fresh_bulk_file()
    assert found == real
    assert not stale_part.exists()   # swept
    assert live_part.exists()        # in-progress download untouched


def test_download_redownloads_when_local_copy_stale(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    stale = d / "oracle-cards-20200101.jsonl.gz"
    stale.write_bytes(b"old")
    old = time.time() - (oracle_store.BULK_FRESH_DAYS + 1) * 86400
    os.utime(stale, (old, old))

    dest = oracle_store.download_bulk_oracle(
        fetch_json=lambda url: _index_payload(),
        open_stream=lambda uri: io.BytesIO(_gz_jsonl([])),
    )
    assert dest != stale
    assert dest.name == f"oracle-cards-{time.strftime('%Y%m%d')}.jsonl.gz"


def test_download_force_overrides_freshness(cache_dir):
    d = oracle_store.bulk_data_dir()
    d.mkdir(parents=True)
    fresh = d / "oracle-cards-20990101.jsonl.gz"
    fresh.write_bytes(b"old-fresh")
    calls = []
    body = _gz_jsonl([SOL_RING])

    dest = oracle_store.download_bulk_oracle(
        force=True,
        fetch_json=lambda url: calls.append(url) or _index_payload(),
        open_stream=lambda uri: io.BytesIO(body),
    )
    assert calls == [oracle_store.BULK_INDEX_URL]
    assert dest.read_bytes() == body


def test_download_errors_when_no_oracle_cards_entry(cache_dir):
    with pytest.raises(RuntimeError, match="oracle_cards"):
        oracle_store.download_bulk_oracle(
            fetch_json=lambda url: {"data": [{"type": "rulings"}]},
            open_stream=lambda uri: io.BytesIO(b"[]"),
        )


def test_download_errors_when_entry_has_no_uri_at_all(cache_dir):
    with pytest.raises(RuntimeError, match="oracle_cards"):
        oracle_store.download_bulk_oracle(
            fetch_json=lambda url: {"data": [{"type": "oracle_cards"}]},
            open_stream=lambda uri: io.BytesIO(b"[]"),
        )


# --- _load_bulk_cards -------------------------------------------------------

def test_load_bulk_cards_jsonl_gz(tmp_path):
    p = _write_bulk_jsonl_gz(tmp_path, [SOL_RING, DELVER])
    cards = oracle_store._load_bulk_cards(p)
    assert [c["name"] for c in cards] == [SOL_RING["name"], DELVER["name"]]


def test_load_bulk_cards_jsonl_gz_skips_blank_lines(tmp_path):
    raw = json.dumps(SOL_RING) + "\n\n" + json.dumps(CULTIVATE) + "\n"
    p = tmp_path / "b.jsonl.gz"
    p.write_bytes(gzip.compress(raw.encode("utf-8")))
    cards = oracle_store._load_bulk_cards(p)
    assert len(cards) == 2


def test_load_bulk_cards_plain_json_array(tmp_path):
    p = _write_bulk(tmp_path, [SOL_RING])
    assert oracle_store._load_bulk_cards(p) == [SOL_RING]


def test_load_bulk_cards_plain_json_non_array_raises(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a JSON array"):
        oracle_store._load_bulk_cards(p)


def test_load_bulk_cards_garbage_gz_raises_corrupt_error(tmp_path):
    p = tmp_path / "b.jsonl.gz"
    p.write_bytes(b"this is not gzip data at all")
    with pytest.raises(oracle_store.BulkFileCorruptError):
        oracle_store._load_bulk_cards(p)


def test_load_bulk_cards_truncated_gz_raises_corrupt_error(tmp_path):
    whole = _gz_jsonl([SOL_RING, CULTIVATE, DELVER])
    p = tmp_path / "b.jsonl.gz"
    p.write_bytes(whole[: len(whole) // 2])  # killed download / bad disk
    with pytest.raises(oracle_store.BulkFileCorruptError):
        oracle_store._load_bulk_cards(p)


def test_load_bulk_cards_bad_json_raises_corrupt_error(tmp_path):
    p = tmp_path / "b.json"
    p.write_text('[{"name": "Sol R', encoding="utf-8")  # truncated JSON
    with pytest.raises(oracle_store.BulkFileCorruptError):
        oracle_store._load_bulk_cards(p)


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


def test_write_snapshots_from_jsonl_gz_bulk(cache_dir, tmp_path):
    bulk = _write_bulk_jsonl_gz(tmp_path, [SOL_RING, CULTIVATE])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Sol Ring"], bulk_path=bulk)
    assert summary == {"written": 1, "missing": [], "targets": 1}
    card = scryfall_client.lookup_card("Sol Ring", cache_only=True)
    assert card is not None and card["oracle_text"] == "{T}: Add {C}{C}."


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


def test_write_snapshots_diacritic_insensitive_targets(cache_dir, tmp_path):
    """Deck files spell it Lim-Dul's Vault; Scryfall's canonical name has
    the û. The exact-named API resolves that, so the bulk index must too
    (live-run regression, 2026-07-29)."""
    vault = {"name": "Lim-Dûl's Vault", "oracle_text": "..."}
    bulk = _write_bulk(tmp_path, [vault])
    summary = oracle_store.write_snapshots_from_bulk(
        ["Lim-Dul's Vault"], bulk_path=bulk)
    assert summary["written"] == 1 and summary["missing"] == []
    card = scryfall_client.lookup_card("Lim-Dul's Vault", cache_only=True)
    assert card is not None and card["name"] == "Lim-Dûl's Vault"


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


def test_write_snapshots_everything_writes_folded_alias(cache_dir, tmp_path):
    """--everything must leave the store as usable as a names-path build:
    deck files spell it Lim-Dul's Vault (folded slug lim_dul_s_vault),
    but the canonical name slugs to lim_d_l_s_vault — without the folded
    alias those decks still miss offline after a full build."""
    vault = {"name": "Lim-Dûl's Vault", "oracle_text": "..."}
    bulk = _write_bulk(tmp_path, [vault])
    summary = oracle_store.write_snapshots_from_bulk(
        None, bulk_path=bulk, everything=True)
    # Canonical slug + folded alias slug = 2 files.
    assert summary["written"] == 2
    card = scryfall_client.lookup_card("Lim-Dul's Vault", cache_only=True)
    assert card is not None and card["name"] == "Lim-Dûl's Vault"
    assert scryfall_client.lookup_card("Lim-Dûl's Vault", cache_only=True)


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


def test_snapshot_targets_from_foil_marked_deck_lines(cache_dir, tmp_path):
    """A deck line with Forge's trailing ``+`` foil marker must target the
    CANONICAL name in the bulk build — live-run regression: 44 foil-marked
    names were reported "not in bulk data" and never got snapshots."""
    bulk = _write_bulk(tmp_path, [SOL_RING, CULTIVATE])
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    (deck_dir / "a.dck").write_text(
        "[metadata]\nName=T\n\n[Main]\n1 Sol Ring+|C21|263\n1 Cultivate+\n",
        encoding="utf-8",
    )
    names = oracle_store.names_from_deck_dir(deck_dir)
    assert names == ["Sol Ring", "Cultivate"]  # canonical, no "+"
    summary = oracle_store.write_snapshots_from_bulk(names, bulk_path=bulk)
    assert summary == {"written": 2, "missing": [], "targets": 2}
    assert scryfall_client.lookup_card("Sol Ring", cache_only=True)
    assert scryfall_client.lookup_card("Cultivate", cache_only=True)


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


@pytest.mark.parametrize("exc", [
    urllib.error.URLError("dns blip"),          # below-HTTP network failure
    ConnectionResetError("peer reset"),         # plain OSError subclass
    http.client.HTTPException("bad status line"),
])
def test_bulk_refresh_survives_network_error_mid_pass(
        cache_dir, monkeypatch, capsys, exc):
    """A URLError (DNS blip, connection reset) is NOT an HTTPError — before
    the fix it propagated straight through bulk_refresh's per-card
    containment and killed a long --all --write pass mid-run."""
    _snapshot("A", "old-a")
    _snapshot("B", "old-b")

    def flaky_network(name, cache=True):
        if name == "A":
            raise exc
        return {"name": name, "oracle_text": "old-b"}

    monkeypatch.setattr(scryfall_client, "lookup_card", flaky_network)
    summary = oracle_store.bulk_refresh(["A", "B"], sleep=lambda s: None)
    # A degraded loudly; the run CONTINUED and still checked B.
    assert summary["checked"] == 2
    assert summary["errors"] == 1
    by_name = {r["name"]: r for r in summary["results"]}
    assert by_name["A"]["status"] == "network_error"
    assert type(exc).__name__ in by_name["A"]["error"]
    assert by_name["B"]["status"] == "ok"
    err = capsys.readouterr().err
    assert "giving up on 'A'" in err and "continuing" in err


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

def _stub_bulk_download(monkeypatch, tmp_path, cards, jsonl=False):
    if jsonl:
        bulk = _write_bulk_jsonl_gz(tmp_path, cards,
                                    filename="stub-bulk.jsonl.gz")
    else:
        bulk = _write_bulk(tmp_path, cards, filename="stub-bulk.json")
    calls = []

    def fake_download(*, force=False):
        calls.append(force)
        return bulk

    monkeypatch.setattr(oracle_store, "download_bulk_oracle", fake_download)
    return bulk, calls


def test_cli_from_bulk_deck(cache_dir, tmp_path, monkeypatch, capsys):
    # jsonl=True: exercises the live .jsonl.gz format end-to-end.
    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING, CULTIVATE, DELVER],
                        jsonl=True)
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


def test_cli_from_bulk_all_defaults_to_forge_deck_dir(
        cache_dir, tmp_path, monkeypatch, capsys):
    """Without --deck-dir, --from-bulk --all targets the FORGE deck dir
    (run_match.DECK_DIR) — the dir the rest of the CLI tooling uses —
    not the desktop app's Documents library."""
    from commander_builder import run_match

    _stub_bulk_download(monkeypatch, tmp_path, [SOL_RING])
    forge_decks = tmp_path / "forge_decks"
    forge_decks.mkdir()
    _write_deck(forge_decks / "a.dck", ["Sol Ring"])
    monkeypatch.setattr(run_match, "DECK_DIR", forge_decks)
    rc = oracle_store.main(["--from-bulk", "--all", "--json"])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] == 1 and summary["missing"] == []


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


def test_cli_from_bulk_corrupt_cached_file_quarantined_rc2(
        cache_dir, tmp_path, monkeypatch, capsys):
    """A corrupt/truncated cached bulk file must NOT traceback (or keep
    being reused for a week): friendly message, file renamed *.corrupt
    so the next run re-downloads, nonzero exit."""
    bulk = tmp_path / "oracle-cards-20260801.jsonl.gz"
    bulk.write_bytes(b"garbage, not gzip")
    monkeypatch.setattr(oracle_store, "download_bulk_oracle",
                        lambda *, force=False: bulk)
    rc = oracle_store.main(["--from-bulk", "--name", "Sol Ring"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "corrupt or truncated" in out
    assert "Re-run" in out
    assert not bulk.exists()  # quarantined away from the freshness scan
    assert (tmp_path / "oracle-cards-20260801.jsonl.gz.corrupt").exists()
