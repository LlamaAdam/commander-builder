"""Tests for ``moxfield_import.bulk_import`` — the multi-URL ingest path.

The single-deck flow (``import_deck``) already exists. ``bulk_import``
wraps it for batch ingestion: take a list of URLs/ids, import each
with polite rate-limiting between requests, dedupe against decks
already on disk, return a structured result so a UI / batch driver
can present per-URL outcomes.

Live HTTP is mocked at the ``fetch_deck`` boundary. Every test asserts
the call ordering + sleep ordering matches the politeness contract
(serial, with a small inter-request sleep) so we don't regress and
start hammering Moxfield.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder.moxfield_import import (
    BulkImportResult,
    bulk_import,
)


def _stub_deck_json(name: str, public_id: str) -> dict:
    """Minimal Moxfield-shape JSON the importer accepts."""
    return {
        "publicId": public_id,
        "name": name,
        "format": "commander",
        "boards": {
            "commanders": {
                "cards": {
                    "c1": {
                        "quantity": 1,
                        "card": {
                            "name": "Test Commander",
                            "set": "CMR",
                            "cn": "1",
                            "type_line": "Legendary Creature",
                        },
                    },
                },
            },
            "mainboard": {
                "cards": {
                    "m1": {
                        "quantity": 1,
                        "card": {
                            "name": "Sol Ring", "set": "CLB",
                            "cn": "871", "type_line": "Artifact",
                        },
                    },
                },
            },
        },
        "bracket": 3,
    }


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

def test_bulk_import_result_round_trips_through_json():
    """BulkImportResult.to_dict() round-trips so a Flask endpoint can
    return it without bespoke encoders."""
    result = BulkImportResult(
        successes=[
            {"url": "https://moxfield.com/decks/abc",
             "deck_id": "abc",
             "path": "/x/[USER] Foo [B3].dck"},
        ],
        duplicates=[
            {"url": "https://moxfield.com/decks/dup",
             "deck_id": "dup",
             "existing_path": "/x/[USER] Dup [B3].dck"},
        ],
        failures=[
            {"url": "https://moxfield.com/decks/bad",
             "error": "RuntimeError: 404"},
        ],
    )
    blob = json.dumps(result.to_dict())
    parsed = json.loads(blob)
    assert len(parsed["successes"]) == 1
    assert len(parsed["duplicates"]) == 1
    assert len(parsed["failures"]) == 1
    assert parsed["total"] == 3
    assert parsed["success_count"] == 1
    assert parsed["duplicate_count"] == 1
    assert parsed["failure_count"] == 1


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_bulk_import_writes_each_url_to_disk(tmp_path, monkeypatch):
    """Three URLs → three .dck files, all flagged as successes."""
    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    # Skip the sleep — tests should be fast. We assert on the sleep
    # CALLS separately to pin the politeness contract.
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep",
        lambda s: sleep_calls.append(s),
    )

    urls = [
        "https://moxfield.com/decks/abc1",
        "https://moxfield.com/decks/abc2",
        "https://moxfield.com/decks/abc3",
    ]
    result = bulk_import(urls, out_dir=tmp_path)

    assert result.success_count == 3
    assert result.failure_count == 0
    assert result.duplicate_count == 0
    # One .dck per URL on disk.
    written = list(tmp_path.glob("*.dck"))
    assert len(written) == 3


def test_bulk_import_sleeps_between_requests_politeness_contract(
    tmp_path, monkeypatch,
):
    """The single-deck path uses FETCH_SLEEP_SEC between sequential
    fetches. Bulk MUST do the same so a 20-URL batch doesn't hammer
    Moxfield. Pinned by counting sleep calls — we expect N-1 sleeps
    for N URLs (no sleep after the last)."""
    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep",
        lambda s: sleep_calls.append(s),
    )

    urls = ["https://moxfield.com/decks/a",
            "https://moxfield.com/decks/b",
            "https://moxfield.com/decks/c"]
    bulk_import(urls, out_dir=tmp_path)

    # N-1 sleeps for N successful fetches. Sleep duration is the
    # module-level FETCH_SLEEP_SEC; we don't pin the exact number,
    # only that we WAITED between requests.
    assert len(sleep_calls) >= len(urls) - 1
    assert all(s > 0 for s in sleep_calls)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_bulk_import_skips_existing_files_as_duplicates(
    tmp_path, monkeypatch,
):
    """If a .dck for the URL's deck name+bracket already exists on
    disk, the URL is reported as a duplicate (NOT a success and NOT
    a failure). The existing file is not overwritten."""
    def fake_fetch(deck_id):
        return _stub_deck_json("MyDeck", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    # Pre-seed the deck directory with what the importer WOULD write.
    existing = tmp_path / "[USER] MyDeck [B3].dck"
    existing.write_text("SEEDED CONTENT", encoding="utf-8")

    urls = ["https://moxfield.com/decks/abc"]
    result = bulk_import(urls, out_dir=tmp_path, is_user=True)

    assert result.duplicate_count == 1
    assert result.success_count == 0
    # File NOT overwritten.
    assert existing.read_text(encoding="utf-8") == "SEEDED CONTENT"


def test_bulk_import_deduplicates_within_same_batch(tmp_path, monkeypatch):
    """If the same URL appears twice in one input list, only the
    first import lands; the duplicate is reported separately so the
    user sees what was de-duped without thinking it failed."""
    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls = [
        "https://moxfield.com/decks/same",
        "https://moxfield.com/decks/same",
    ]
    result = bulk_import(urls, out_dir=tmp_path)
    assert result.success_count == 1
    assert result.duplicate_count == 1


def test_bulk_import_same_id_across_runs_is_duplicate(tmp_path, monkeypatch):
    """Re-running bulk_import over a URL already imported (same recorded
    Moxfield= publicId on disk) reports a duplicate — correct dedupe, file
    untouched."""
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda deck_id: _stub_deck_json("MyDeck", deck_id),
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls = ["https://moxfield.com/decks/abc"]
    r1 = bulk_import(urls, out_dir=tmp_path, is_user=True)
    assert r1.success_count == 1
    written = Path(r1.successes[0]["path"])
    before = written.read_text(encoding="utf-8")

    r2 = bulk_import(urls, out_dir=tmp_path, is_user=True)
    assert r2.success_count == 0
    assert r2.duplicate_count == 1
    assert r2.duplicates[0]["reason"] == "same Moxfield deck already on disk"
    assert written.read_text(encoding="utf-8") == before
    # No numbered copy appeared either.
    assert len(list(tmp_path.glob("*.dck"))) == 1


def test_bulk_import_different_deck_name_collision_uniquified(
    tmp_path, monkeypatch,
):
    """Two DIFFERENT decks whose names sanitize to the same filename: the
    second must be IMPORTED under a uniquified name (old code misreported
    it as 'file already on disk' and silently skipped it). The uniquified
    name keeps the ` [B<n>].dck` suffix shape the bracket filters key on."""
    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck",
        lambda deck_id: _stub_deck_json("MyDeck", deck_id),
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls = [
        "https://moxfield.com/decks/id-one",
        "https://moxfield.com/decks/id-two",  # different deck, same name
    ]
    result = bulk_import(urls, out_dir=tmp_path, is_user=True)

    assert result.success_count == 2
    assert result.duplicate_count == 0
    names = sorted(p.name for p in tmp_path.glob("*.dck"))
    assert names == [
        "[USER] MyDeck (2) [B3].dck",
        "[USER] MyDeck [B3].dck",
    ]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_bulk_import_captures_fetch_errors_per_url(tmp_path, monkeypatch):
    """A failed fetch on one URL must not abort the whole batch. The
    failure is captured in the result; the remaining URLs continue."""
    def flaky_fetch(deck_id):
        if deck_id == "bad":
            raise RuntimeError("Moxfield 500")
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", flaky_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls = [
        "https://moxfield.com/decks/good1",
        "https://moxfield.com/decks/bad",
        "https://moxfield.com/decks/good2",
    ]
    result = bulk_import(urls, out_dir=tmp_path)

    assert result.success_count == 2
    assert result.failure_count == 1
    bad = result.failures[0]
    assert "Moxfield 500" in bad["error"]
    assert bad["url"].endswith("/bad")


def test_bulk_import_handles_empty_input(tmp_path):
    """No URLs → empty result, no crash. Defensive guard for callers
    that pass an unfiltered list from a textarea."""
    result = bulk_import([], out_dir=tmp_path)
    assert result.success_count == 0
    assert result.failure_count == 0
    assert result.duplicate_count == 0


def test_bulk_import_skips_blank_lines(tmp_path, monkeypatch):
    """A textarea-pasted list often contains blank lines between URLs
    (especially when copied from a chat / notes app). Strip + skip
    blanks before fetching."""
    fetches: list[str] = []

    def fake_fetch(deck_id):
        fetches.append(deck_id)
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls = [
        "https://moxfield.com/decks/a",
        "",
        "   ",
        "https://moxfield.com/decks/b",
        "",
    ]
    result = bulk_import(urls, out_dir=tmp_path)
    assert result.success_count == 2
    assert fetches == ["a", "b"]


# ---------------------------------------------------------------------------
# CLI entry point smoke
# ---------------------------------------------------------------------------

def test_bulk_main_reads_urls_from_file(tmp_path, monkeypatch, capsys):
    """`commander-bulk-import urls.txt` reads one URL per line from
    the file and prints a summary table."""
    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://moxfield.com/decks/a\n"
        "https://moxfield.com/decks/b\n",
        encoding="utf-8",
    )

    from commander_builder.moxfield_import import bulk_main
    rc = bulk_main([str(urls_file), "--out-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 succeeded" in out or "Successes: 2" in out


def test_bulk_main_returns_nonzero_when_all_fail(
    tmp_path, monkeypatch, capsys,
):
    """If every URL fails, the CLI exits non-zero so a batch driver
    can detect the systemic failure (network down, etc.)."""
    def always_fail(deck_id):
        raise RuntimeError("network is out")

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", always_fail,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://moxfield.com/decks/x\n", encoding="utf-8",
    )

    from commander_builder.moxfield_import import bulk_main
    rc = bulk_main([str(urls_file), "--out-dir", str(tmp_path)])
    assert rc != 0


def test_bulk_main_json_mode_emits_structured_output(
    tmp_path, monkeypatch, capsys,
):
    """--json swaps the human summary for machine-readable JSON the
    Flask endpoint / batch driver can pipe into jq."""
    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(
        "https://moxfield.com/decks/q\n", encoding="utf-8",
    )

    from commander_builder.moxfield_import import bulk_main
    rc = bulk_main([
        str(urls_file), "--out-dir", str(tmp_path), "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["success_count"] == 1


# ---------------------------------------------------------------------------
# Flask /api/bulk_import endpoint
# ---------------------------------------------------------------------------

flask = pytest.importorskip("flask")


@pytest.fixture
def bulk_client(tmp_path, monkeypatch):
    """Flask test client with a deck_dir under tmp_path and Moxfield
    fetches mocked at the boundary."""
    from commander_builder.web.app import create_app

    def fake_fetch(deck_id):
        return _stub_deck_json(f"Deck {deck_id}", deck_id)

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", fake_fetch,
    )
    monkeypatch.setattr(
        "commander_builder.moxfield_import.time.sleep", lambda s: None,
    )

    decks = tmp_path / "decks"
    decks.mkdir()
    app = create_app(deck_dir=decks)
    app.config["TESTING"] = True
    return app.test_client(), decks


def test_bulk_import_endpoint_returns_per_url_result(bulk_client):
    """Happy path: 3 URLs in, 3 successes out + per-URL paths."""
    client, decks = bulk_client
    resp = client.post(
        "/api/bulk_import",
        json={"urls": [
            "https://moxfield.com/decks/a",
            "https://moxfield.com/decks/b",
            "https://moxfield.com/decks/c",
        ]},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["success_count"] == 3
    assert body["failure_count"] == 0
    assert body["duplicate_count"] == 0
    assert len(list(decks.glob("*.dck"))) == 3


def test_bulk_import_endpoint_rejects_non_list_urls(bulk_client):
    """Body validation: urls must be a list. A string or null gets 400
    so the client can correct the request shape."""
    client, _ = bulk_client
    resp = client.post(
        "/api/bulk_import",
        json={"urls": "https://moxfield.com/decks/a"},
    )
    assert resp.status_code == 400


def test_bulk_import_endpoint_caps_at_max_urls(bulk_client):
    """Hard cap protects the worker from a multi-thousand-URL paste.
    50 is the current cap; bumping requires a code change so we don't
    accidentally serialize a 30-minute fetch behind one request."""
    client, _ = bulk_client
    resp = client.post(
        "/api/bulk_import",
        json={"urls": [f"https://moxfield.com/decks/x{i}" for i in range(51)]},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert "too many" in body["error"]
    assert body["received"] == 51


def test_bulk_import_endpoint_502_when_all_fail(bulk_client, monkeypatch):
    """If every URL fails, signal it via 502 so the UI can warn about
    network. Partial failures stay 200 — the per-URL breakdown carries
    the bad news."""
    client, _ = bulk_client

    def always_fail(deck_id):
        raise RuntimeError("network is out")

    monkeypatch.setattr(
        "commander_builder.moxfield_import.fetch_deck", always_fail,
    )

    resp = client.post(
        "/api/bulk_import",
        json={"urls": ["https://moxfield.com/decks/x"]},
    )
    assert resp.status_code == 502
    assert resp.get_json()["failure_count"] == 1
