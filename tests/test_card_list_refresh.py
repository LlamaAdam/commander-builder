"""Tests for the card-list refresh helpers used by
``scripts/refresh_card_lists.py``.

The helpers in ``_card_list_refresh.py`` are pure (or take an
injectable ``http_get``) so they can be tested without touching the
Scryfall network. Tests cover three things:

1. ``diff_card_lists`` — set arithmetic with case-folding.
2. ``parse_mdfc_lands_from_response`` — Scryfall search response →
   set of qualifying card names.
3. ``fetch_mdfc_lands`` — pagination loop, exit conditions, safety cap.
"""
from __future__ import annotations

import pytest

from commander_builder._card_list_refresh import (
    diff_card_lists,
    fetch_mdfc_lands,
    parse_mdfc_lands_from_response,
)


# ---------------------------------------------------------------------------
# diff_card_lists
# ---------------------------------------------------------------------------

def test_diff_card_lists_basic_overlap():
    """Cards in both stay 'kept'; current-only stays 'stale';
    fresh-only stays 'candidates'."""
    result = diff_card_lists(
        current=["Sol Ring", "Cultivate", "Removed Card"],
        fresh=["sol ring", "cultivate", "New Card"],
    )
    assert result["kept"] == ["cultivate", "sol ring"]
    assert result["stale"] == ["removed card"]
    assert result["candidates"] == ["new card"]


def test_diff_card_lists_case_insensitive():
    """Both sides are lowercased before set arithmetic so 'Sol Ring'
    in the curated list matches 'sol ring' from Scryfall."""
    result = diff_card_lists(current=["SOL RING"], fresh=["sol ring"])
    assert result["kept"] == ["sol ring"]
    assert result["stale"] == []
    assert result["candidates"] == []


def test_diff_card_lists_handles_empty_inputs():
    """Empty current → everything fresh is a candidate. Empty fresh →
    everything current is stale. Both empty → all empty."""
    only_fresh = diff_card_lists(current=[], fresh=["a", "b"])
    assert only_fresh["candidates"] == ["a", "b"]
    assert only_fresh["stale"] == []

    only_current = diff_card_lists(current=["a", "b"], fresh=[])
    assert only_current["stale"] == ["a", "b"]
    assert only_current["candidates"] == []

    both_empty = diff_card_lists(current=[], fresh=[])
    assert both_empty == {"stale": [], "candidates": [], "kept": []}


def test_diff_card_lists_filters_empty_strings():
    """Empty / falsy entries on either side don't leak into the diff."""
    result = diff_card_lists(current=["Sol Ring", "", None], fresh=["sol ring"])
    assert result["kept"] == ["sol ring"]
    assert "" not in result["stale"]


def test_diff_card_lists_output_sorted_alphabetically():
    """Stable sort makes the diff output reviewable line-by-line."""
    result = diff_card_lists(
        current=["zeta", "alpha"], fresh=["mu", "beta"],
    )
    assert result["stale"] == ["alpha", "zeta"]
    assert result["candidates"] == ["beta", "mu"]


# ---------------------------------------------------------------------------
# parse_mdfc_lands_from_response
# ---------------------------------------------------------------------------

def _mdfc_card(name: str, faces: list[dict]) -> dict:
    return {
        "object": "card",
        "name": name,
        "layout": "modal_dfc",
        "card_faces": faces,
    }


def test_parse_mdfc_extracts_spell_back_land():
    """Classic MDFC: front face is a spell, back face is a land
    (e.g. Sea Gate Restoration // Sea Gate, Reborn)."""
    payload = {
        "data": [
            _mdfc_card(
                "Sea Gate Restoration // Sea Gate, Reborn",
                [
                    {"type_line": "Sorcery"},
                    {"type_line": "Land"},
                ],
            ),
        ],
    }
    assert parse_mdfc_lands_from_response(payload) == {
        "sea gate restoration",
    }


def test_parse_mdfc_extracts_pathway_land_both_faces():
    """Pathways (both faces are Land) qualify — they're MDFCs that
    affect the mana base size, which is what ``_MDFC_LANDS`` cares
    about."""
    payload = {
        "data": [
            _mdfc_card(
                "Branchloft Pathway // Boulderloft Pathway",
                [
                    {"type_line": "Land"},
                    {"type_line": "Land"},
                ],
            ),
        ],
    }
    assert parse_mdfc_lands_from_response(payload) == {
        "branchloft pathway",
    }


def test_parse_mdfc_skips_spell_spell_modal_cards():
    """Spell+spell MDFCs (no land on either face) don't qualify
    for ``_MDFC_LANDS`` — they're not lands."""
    payload = {
        "data": [
            _mdfc_card(
                "Hypothetical Spell // Hypothetical Other Spell",
                [
                    {"type_line": "Instant"},
                    {"type_line": "Sorcery"},
                ],
            ),
        ],
    }
    assert parse_mdfc_lands_from_response(payload) == set()


def test_parse_mdfc_skips_non_modal_dfc_layout():
    """Non-modal-DFC layouts (transform, adventure, split, etc.) don't
    qualify even if a face is a Land."""
    payload = {
        "data": [
            {
                "name": "Search for Azcanta // Azcanta, the Sunken Ruin",
                "layout": "transform",
                "card_faces": [
                    {"type_line": "Legendary Enchantment"},
                    {"type_line": "Legendary Land"},
                ],
            },
        ],
    }
    assert parse_mdfc_lands_from_response(payload) == set()


def test_parse_mdfc_tolerates_missing_fields():
    """Defensive: empty payload, missing faces, missing layout — all
    yield an empty result without raising."""
    assert parse_mdfc_lands_from_response({}) == set()
    assert parse_mdfc_lands_from_response({"data": []}) == set()
    assert parse_mdfc_lands_from_response(
        {"data": [{"name": "X", "layout": "modal_dfc"}]}
    ) == set()
    assert parse_mdfc_lands_from_response(
        {"data": [{"layout": "modal_dfc",
                   "card_faces": [{"type_line": "Land"}]}]}
    ) == set()  # missing top-level name


def test_parse_mdfc_handles_compound_type_lines():
    """``Legendary Land — Mountain`` should still match the
    case-insensitive ``land`` substring."""
    payload = {
        "data": [
            _mdfc_card(
                "Test Card // Test Land",
                [
                    {"type_line": "Sorcery"},
                    {"type_line": "Legendary Land — Mountain"},
                ],
            ),
        ],
    }
    assert parse_mdfc_lands_from_response(payload) == {"test card"}


# ---------------------------------------------------------------------------
# fetch_mdfc_lands (pagination loop)
# ---------------------------------------------------------------------------

def test_fetch_mdfc_lands_single_page():
    """When the first response has ``has_more=False``, the loop exits
    after one call."""
    calls = []

    def _http(url):
        calls.append(url)
        return {
            "data": [
                _mdfc_card("A // A Land",
                           [{"type_line": "Instant"}, {"type_line": "Land"}]),
            ],
            "has_more": False,
        }

    result = fetch_mdfc_lands(http_get=_http)
    assert result == {"a"}
    assert len(calls) == 1


def test_fetch_mdfc_lands_follows_pagination():
    """Multi-page Scryfall results: follow ``next_page`` until
    ``has_more`` flips."""
    responses = iter([
        {
            "data": [_mdfc_card(
                "A", [{"type_line": "Sorcery"}, {"type_line": "Land"}])],
            "has_more": True,
            "next_page": "page2",
        },
        {
            "data": [_mdfc_card(
                "B", [{"type_line": "Sorcery"}, {"type_line": "Land"}])],
            "has_more": True,
            "next_page": "page3",
        },
        {
            "data": [_mdfc_card(
                "C", [{"type_line": "Sorcery"}, {"type_line": "Land"}])],
            "has_more": False,
        },
    ])
    urls = []

    def _http(url):
        urls.append(url)
        return next(responses)

    result = fetch_mdfc_lands(http_get=_http)
    assert result == {"a", "b", "c"}
    assert urls == [
        "https://api.scryfall.com/cards/search?q=layout:modal_dfc",
        "page2",
        "page3",
    ]


def test_fetch_mdfc_lands_safety_cap_breaks_infinite_loop():
    """A malformed response that keeps reporting ``has_more=True``
    without a useful ``next_page`` shouldn't spin forever — the
    50-page cap kicks in."""
    def _http(url):
        return {
            "data": [],
            "has_more": True,
            "next_page": "https://example.invalid/loop",
        }

    # No raise, no hang.
    result = fetch_mdfc_lands(http_get=_http)
    assert result == set()


def test_fetch_mdfc_lands_stops_on_missing_next_page():
    """Defensive: if Scryfall sends ``has_more=True`` but omits
    ``next_page`` (shouldn't happen but guard anyway), the loop exits."""
    calls = [0]

    def _http(url):
        calls[0] += 1
        return {"data": [], "has_more": True}  # next_page missing

    result = fetch_mdfc_lands(http_get=_http)
    assert result == set()
    # First call returns has_more=True without next_page → second
    # iteration sees ``url=None`` from .get() and exits.
    assert calls[0] == 1
