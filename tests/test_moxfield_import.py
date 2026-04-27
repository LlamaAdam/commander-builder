"""moxfield_import unit tests for offline helpers (no network).

The HTTP paths (`fetch_deck`, `search_decks`) are integration concerns. This
module covers the deterministic helpers: filename sanitation, deck-id parsing,
bracket resolution, .dck rendering, and the uniquify collision logic.
"""
from pathlib import Path

import pytest

from commander_builder.moxfield_import import (
    _uniquify,
    card_line,
    deck_destination,
    parse_deck_id,
    resolve_bracket,
    safe_filename,
    to_dck,
)


def test_parse_deck_id_from_url():
    assert parse_deck_id("https://moxfield.com/decks/abc123XYZ") == "abc123XYZ"
    assert parse_deck_id("https://www.moxfield.com/decks/abc-DEF_456/edit") == "abc-DEF_456"


def test_parse_deck_id_passthrough_for_bare_id():
    assert parse_deck_id("abc123") == "abc123"
    assert parse_deck_id("  abc123  ") == "abc123"


def test_safe_filename_strips_invalid_chars():
    assert safe_filename("Foo: Bar") == "Foo_ Bar"
    assert safe_filename('Foo<>:"/\\|?*Bar') == "Foo_________Bar"


def test_safe_filename_strips_non_ascii():
    # Forge 2.0.12 mangles non-ASCII in filenames on Windows.
    assert safe_filename("Atraxa Infect_Proliferate ϕ ☣") == "Atraxa Infect_Proliferate"


def test_safe_filename_falls_back_to_deck_when_fully_stripped():
    # Empty after stripping non-ASCII falls back to "deck" rather than empty.
    # (`safe_filename("///")` returns "___" — slashes get substituted, not stripped.)
    assert safe_filename("☣☣☣") == "deck"
    assert safe_filename("") == "deck"


def test_safe_filename_collapses_whitespace():
    assert safe_filename("Foo   Bar    Baz") == "Foo Bar Baz"


def test_resolve_bracket_prefers_confirmed():
    # bracket > userBracket > autoBracket
    assert resolve_bracket({"bracket": 3, "userBracket": 4, "autoBracket": 5}) == 3
    assert resolve_bracket({"userBracket": 4, "autoBracket": 5}) == 4
    assert resolve_bracket({"autoBracket": 5}) == 5
    assert resolve_bracket({}) == 0


def test_resolve_bracket_rejects_out_of_range():
    # bracket=0 is "unrated"; treat as missing.
    assert resolve_bracket({"bracket": 0, "userBracket": 3}) == 3
    # Negative values can appear in malformed payloads.
    assert resolve_bracket({"bracket": -1}) == 0
    assert resolve_bracket({"bracket": 99}) == 0


def test_deck_destination_user_prefix_and_bracket_suffix():
    base = Path("/tmp/decks")
    assert (
        deck_destination("My Deck", 3, base=base, is_user=True)
        == base / "[USER] My Deck [B3].dck"
    )
    assert (
        deck_destination("My Deck", 3, base=base, is_user=False)
        == base / "My Deck [B3].dck"
    )


def test_deck_destination_unknown_bracket():
    base = Path("/tmp/decks")
    assert (
        deck_destination("Foo", 0, base=base) == base / "Foo [B?].dck"
    )


def test_card_line_with_full_metadata():
    entry = {"quantity": 4, "card": {"name": "Lightning Bolt", "set": "lea", "cn": "150"}}
    assert card_line(entry) == "4 Lightning Bolt|LEA|150"


def test_card_line_omits_missing_set_and_cn():
    entry = {"quantity": 1, "card": {"name": "Frobnicator"}}
    assert card_line(entry) == "1 Frobnicator"


def test_to_dck_includes_moxfield_metadata():
    deck = {
        "name": "Test Deck",
        "publicId": "abc-XYZ_123",
        "boards": {
            "commanders": {"cards": {"k1": {"quantity": 1, "card": {"name": "Atraxa, Praetors' Voice"}}}},
            "mainboard": {"cards": {"k2": {"quantity": 1, "card": {"name": "Sol Ring", "set": "cmm"}}}},
        },
    }
    text = to_dck(deck)
    assert "Name=Test Deck" in text
    assert "Moxfield=abc-XYZ_123" in text
    assert "[Commander]" in text
    assert "1 Atraxa, Praetors' Voice" in text
    assert "[Main]" in text
    assert "1 Sol Ring|CMM" in text


def test_to_dck_omits_moxfield_when_no_public_id():
    deck = {"name": "X", "boards": {"mainboard": {"cards": {}}}}
    text = to_dck(deck)
    assert "Moxfield=" not in text


def test_uniquify_returns_path_when_free(tmp_path):
    p = tmp_path / "Foo.dck"
    assert _uniquify(p) == p


def test_uniquify_appends_suffix_on_collision(tmp_path):
    p = tmp_path / "Foo.dck"
    p.write_text("first")
    out = _uniquify(p)
    assert out.name == "Foo (2).dck"
    out.write_text("second")
    out2 = _uniquify(p)
    assert out2.name == "Foo (3).dck"


def test_find_top_liked_deck_resolves_card_id_then_searches(monkeypatch):
    """Two-step lookup: card-search → ID, then deck-search by commanderCardId."""
    from commander_builder.moxfield_import import find_top_liked_deck_for_commander

    card_search_response = {"data": [
        {"id": "card-uuid-1", "name": "Hakbal of the Surging Soul"},
        {"id": "card-uuid-2", "name": "Different Card"},
    ]}
    deck_search_response = {"data": [
        {"publicId": "deck-id-1", "commanders": [{"name": "Hakbal of the Surging Soul"}]},
    ]}
    fake_deck_json = {"publicId": "deck-id-1", "name": "Top Likes Hakbal"}

    def fake_get(url):
        if "/cards/search" in url:
            return card_search_response
        if "/decks/search" in url:
            return deck_search_response
        return fake_deck_json   # fetch_deck path

    monkeypatch.setattr("commander_builder.moxfield_import._http_get_json", fake_get)

    result = find_top_liked_deck_for_commander("Hakbal of the Surging Soul")
    assert result is not None
    assert result["publicId"] == "deck-id-1"


def test_find_top_liked_deck_returns_none_when_card_id_unresolved(monkeypatch):
    """If card-search returns no exact match, the function gives up cleanly."""
    from commander_builder.moxfield_import import find_top_liked_deck_for_commander

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json",
        lambda url: {"data": [{"id": "x", "name": "Different Card"}]},
    )
    assert find_top_liked_deck_for_commander("Hakbal of the Surging Soul") is None


def test_find_top_liked_deck_handles_network_error(monkeypatch):
    from commander_builder.moxfield_import import find_top_liked_deck_for_commander
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr("commander_builder.moxfield_import._http_get_json", boom)
    assert find_top_liked_deck_for_commander("Whatever") is None


def test_lookup_moxfield_card_id_finds_exact_match(monkeypatch):
    """The new card-id resolution helper, exercised directly."""
    from commander_builder.moxfield_import import lookup_moxfield_card_id

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json",
        lambda url: {"data": [
            {"id": "uuid-A", "name": "Foo"},
            {"id": "uuid-B", "name": "Hakbal of the Surging Soul"},
        ]},
    )
    assert lookup_moxfield_card_id("Hakbal of the Surging Soul") == "uuid-B"


def test_lookup_moxfield_card_id_returns_none_when_no_exact_match(monkeypatch):
    from commander_builder.moxfield_import import lookup_moxfield_card_id

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json",
        lambda url: {"data": [{"id": "x", "name": "Hakbal Junior"}]},
    )
    # 'Hakbal of the Surging Soul' isn't an exact match for 'Hakbal Junior'.
    assert lookup_moxfield_card_id("Hakbal of the Surging Soul") is None


def test_uniquify_raises_after_99_collisions(tmp_path):
    """Pathological case: pre-create 99 collisions and confirm we refuse to
    silently overwrite. Regression for the QA review fix."""
    p = tmp_path / "Foo.dck"
    p.write_text("orig")
    for n in range(2, 100):
        (tmp_path / f"Foo ({n}).dck").write_text(str(n))
    with pytest.raises(RuntimeError):
        _uniquify(p)
