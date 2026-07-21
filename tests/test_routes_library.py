"""Tests for FP-007 slice 2 -- /api/library cross-deck card search.

Covers:
- Basic hit: a card present in one or more decks is found.
- No match: a card not in any deck returns an empty list.
- Multiple decks: correct count returned when several decks run the card.
- Case-insensitive: query and deck content are folded for matching.
- Edition-tail stripped: "1 Sol Ring|CLB|871" matches "Sol Ring".
- Commander section included: cards in [Commander] are found.
- Missing card param: 400 error.
- Blank card param: 400 error.
"""
from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from commander_builder.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_deck(deck_dir: Path, name: str, commander: str, main: list) -> None:
    lines = ["[Commander]", f"1 {commander}", "[Main]"]
    lines.extend(f"1 {c}" for c in main)
    (deck_dir / f"{name}.dck").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def deck_dir(tmp_path):
    d = tmp_path / "decks"
    d.mkdir()
    _write_deck(d, "[USER] Alpha [B3]", "Krenko, Mob Boss",
                ["Sol Ring", "Lightning Bolt", "Goblin Recruiter"])
    _write_deck(d, "[USER] Beta [B3]",  "Atraxa, Praetors' Voice",
                ["Sol Ring|CLB|871", "Counterspell", "Rhystic Study"])
    _write_deck(d, "[USER] Gamma [B3]", "Sisay, Weatherlight Captain",
                ["Jhoira, Weatherlight Captain", "Exploration"])
    return d


@pytest.fixture
def client(deck_dir):
    app = create_app(deck_dir=deck_dir)
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_library_finds_card_in_single_deck(client):
    body = client.get("/api/library?card=Lightning+Bolt").get_json()
    assert body["card"] == "Lightning Bolt"
    assert body["decks"] == ["[USER] Alpha [B3]"]
    assert body["count"] == 1


def test_library_finds_card_in_multiple_decks(client):
    body = client.get("/api/library?card=Sol+Ring").get_json()
    assert body["count"] == 2
    assert "[USER] Alpha [B3]" in body["decks"]
    assert "[USER] Beta [B3]" in body["decks"]
    # Results are sorted.
    assert body["decks"] == sorted(body["decks"])


def test_library_card_not_in_any_deck(client):
    body = client.get("/api/library?card=Mox+Diamond").get_json()
    assert body["count"] == 0
    assert body["decks"] == []


def test_library_case_insensitive_query(client):
    # "sol ring" (lowercase) must match "Sol Ring" in deck files.
    body = client.get("/api/library?card=sol+ring").get_json()
    assert body["count"] == 2


def test_library_edition_tail_stripped(client):
    # Beta has "Sol Ring|CLB|871" -- the |SET|CN tail must be stripped.
    body = client.get("/api/library?card=Sol+Ring").get_json()
    assert "[USER] Beta [B3]" in body["decks"]


def test_library_finds_commander_section_card(client):
    # "Krenko, Mob Boss" is in [Commander] of Alpha.
    body = client.get("/api/library?card=Krenko%2C+Mob+Boss").get_json()
    assert body["count"] == 1
    assert "[USER] Alpha [B3]" in body["decks"]


def test_library_missing_card_param_returns_400(client):
    resp = client.get("/api/library")
    assert resp.status_code == 400


def test_library_blank_card_param_returns_400(client):
    resp = client.get("/api/library?card=")
    assert resp.status_code == 400


def test_library_response_shape(client):
    body = client.get("/api/library?card=Exploration").get_json()
    assert "card" in body
    assert "decks" in body
    assert "count" in body
    assert isinstance(body["decks"], list)
    assert isinstance(body["count"], int)


# ---------------------------------------------------------------------------
# Polish (FP-007): empty / error state shapes the JS UI renders against
# ---------------------------------------------------------------------------

def test_library_zero_count_when_card_absent(client):
    """count=0 + empty decks list gives the JS its 'No decks run this card.'
    empty-state branch (renderLibraryResults checks !decks.length)."""
    body = client.get("/api/library?card=Black+Lotus").get_json()
    assert body["count"] == 0
    assert body["decks"] == []
    # card field must echo the query so the header renders correctly.
    assert body["card"] == "Black Lotus"


def test_library_400_body_has_error_field(client):
    """400 response includes an 'error' key so the JS can surface a
    friendly message rather than a raw status code string."""
    body = client.get("/api/library?card=").get_json()
    assert "error" in body
