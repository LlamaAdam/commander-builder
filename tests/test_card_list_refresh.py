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
    fetch_self_mill_candidates,
    parse_mdfc_lands_from_response,
    parse_self_mill_from_response,
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


# ---------------------------------------------------------------------------
# parse_self_mill_from_response (AGENT_BACKLOG #010)
# ---------------------------------------------------------------------------

def _card(name: str, oracle: str, faces: list[dict] | None = None) -> dict:
    out = {"name": name, "oracle_text": oracle}
    if faces is not None:
        out["card_faces"] = faces
        out["oracle_text"] = ""
    return out


def test_parse_self_mill_finds_motion_pattern():
    """Hermit Druid / Satyr Wayfinder style: "reveal cards from your
    library ... put rest into your graveyard". Catches the canonical
    self-mill enabler shape."""
    payload = {"data": [_card(
        "Satyr Wayfinder",
        "When Satyr Wayfinder enters, reveal the top four cards of "
        "your library. You may put a land card from among them into "
        "your hand. Put the rest into your graveyard.",
    )]}
    assert parse_self_mill_from_response(payload) == {"satyr wayfinder"}


def test_parse_self_mill_finds_explicit_mill_with_you():
    """Stitcher's Supplier shape: "mill three cards" attached to a
    self trigger (no opponent targeting)."""
    payload = {"data": [_card(
        "Stitcher's Supplier",
        "When Stitcher's Supplier enters or dies, mill three cards.",
    )]}
    assert parse_self_mill_from_response(payload) == {"stitcher's supplier"}


def test_parse_self_mill_excludes_opponent_targeted_mill():
    """Mind Funeral / Glimpse the Unthinkable target opponents.
    Must not appear as self-mill candidates even though the oracle
    has ``mill`` and ``library`` in it."""
    payload = {"data": [_card(
        "Mind Funeral",
        "Target opponent mills cards from the top of their library "
        "until four lands are milled this way.",
    )]}
    assert parse_self_mill_from_response(payload) == set()


def test_parse_self_mill_excludes_target_player_phrasing():
    """``target player`` is also opponent-targeting (the target
    chooses)."""
    payload = {"data": [_card(
        "Some Mill Spell",
        "Target player mills 10 cards from their library.",
    )]}
    assert parse_self_mill_from_response(payload) == set()


def test_parse_self_mill_excludes_each_opponent_mill():
    """``each opponent`` mass-targets all opponents (Maddening Cacophony,
    similar). Not a self-mill enabler."""
    payload = {"data": [_card(
        "Maddening Cacophony",
        "Each opponent mills half their library, rounded up.",
    )]}
    assert parse_self_mill_from_response(payload) == set()


def test_parse_self_mill_excludes_each_player_symmetric_mill():
    """Symmetrical mill (everyone mills, like Mesmeric Trance-ish
    effects). Not a SELF-mill enabler — designed as a sideways
    attack."""
    payload = {"data": [_card(
        "Symmetrical Mill Card",
        "At the beginning of your upkeep, each player mills 2 cards.",
    )]}
    assert parse_self_mill_from_response(payload) == set()


def test_parse_self_mill_skips_cards_without_mill_motion():
    """Random card with ``your library`` (e.g. tutor) but no mill
    motion shouldn't show up."""
    payload = {"data": [_card(
        "Demonic Tutor",
        "Search your library for a card, put that card into your "
        "hand, then shuffle.",
    )]}
    assert parse_self_mill_from_response(payload) == set()


def test_parse_self_mill_handles_dfc_faces():
    """DFCs have empty top-level oracle_text; walks face oracles."""
    payload = {"data": [_card(
        "Some DFC // Other Face",
        "",
        faces=[
            {"oracle_text": (
                "Reveal cards from the top of your library until "
                "a creature card is revealed. Put it into your hand "
                "and the rest into your graveyard."
            )},
            {"oracle_text": "{T}: Add {G}."},
        ],
    )]}
    assert parse_self_mill_from_response(payload) == {"some dfc"}


def test_parse_self_mill_strips_dfc_back_face_from_name():
    """Front-face-only naming convention same as parse_mdfc_lands."""
    payload = {"data": [_card(
        "Foo // Bar",
        "Reveal cards from your library and put the rest into your "
        "graveyard.",
    )]}
    assert parse_self_mill_from_response(payload) == {"foo"}


def test_fetch_self_mill_candidates_paginates(monkeypatch):
    """Follows ``has_more`` + ``next_page`` like ``fetch_mdfc_lands``."""
    responses = iter([
        {"data": [_card("Stitcher's Supplier", "mill three cards. You mill.")],
         "has_more": True, "next_page": "page2"},
        {"data": [_card("Hermit Druid",
                        "Reveal cards from your library and put the rest into your graveyard.")],
         "has_more": False},
    ])

    def _http(url):
        return next(responses)

    out = fetch_self_mill_candidates(http_get=_http)
    assert "stitcher's supplier" in out
    assert "hermit druid" in out


# ---------------------------------------------------------------------------
# Local-snapshot scans (bracket_estimator's extra-turn + MLD lists)
# ---------------------------------------------------------------------------

import json as _json

from commander_builder._card_list_refresh import (  # noqa: E402
    card_grants_extra_turn,
    card_matches_mass_land_denial,
    extra_turn_names_from_snapshots,
    iter_snapshot_cards,
    mld_names_from_snapshots,
)


def test_extra_turn_predicate_matches_the_canonical_wordings():
    """'take an extra turn' (imperative), 'takes an extra turn'
    (targeted), and 'takes two extra turns' (Time Stretch) all match."""
    assert card_grants_extra_turn(_card(
        "Alrund's Epiphany",
        "Draw two cards... Take an extra turn after this one.",
    ))
    assert card_grants_extra_turn(_card(
        "Time Warp", "Target player takes an extra turn after this one.",
    ))
    assert card_grants_extra_turn(_card(
        "Time Stretch",
        "Target player takes two extra turns after this one.",
    ))


def test_extra_turn_predicate_ignores_prevention_wording():
    """Cards that PREVENT extra turns talk about 'begin an extra turn'
    / 'take extra turns', never 'take(s) an extra turn' — they must
    not read as extra-turn spells."""
    assert not card_grants_extra_turn(_card(
        "Stranglehold",
        "Your opponents can't search libraries. If an opponent would "
        "begin an extra turn, that player skips that turn instead.",
    ))
    assert not card_grants_extra_turn(_card(
        "Demonic Tutor",
        "Search your library for a card, put that card into your hand.",
    ))


def test_extra_turn_predicate_walks_dfc_faces():
    assert card_grants_extra_turn(_card(
        "Some Front // Time Back", "",
        faces=[
            {"oracle_text": "{T}: Add {U}."},
            {"oracle_text": "Take an extra turn after this one."},
        ],
    ))


def test_mld_predicate_matches_the_hardcoded_list_shapes():
    """Every oracle shape in bracket_estimator._MLD_CARDS must match:
    destroy-all / exile-all / return-all, symmetric numbered sacrifice,
    choose-and-sacrifice-the-rest, and destroy-N-lands."""
    shapes = {
        "Armageddon": "Destroy all lands.",
        "Obliterate": (
            "Obliterate can't be countered.\nDestroy all artifacts, "
            "creatures, and lands. They can't be regenerated."
        ),
        "Decree of Annihilation": (
            "Exile all artifacts, creatures, and lands from the "
            "battlefield, all cards from all graveyards, and all cards "
            "from all hands."
        ),
        "Sunder": "Return all lands to their owners' hands.",
        "Wildfire": (
            "Each player sacrifices four lands. Wildfire deals 4 damage "
            "to each creature."
        ),
        "Impending Disaster": (
            "At the beginning of your upkeep, if there are five or more "
            "lands on the battlefield, sacrifice Impending Disaster and "
            "each player sacrifices all lands they control."
        ),
        "Death Cloud": (
            "Each player loses X life, discards X cards, sacrifices X "
            "creatures, then sacrifices X lands."
        ),
        "Cataclysm": (
            "Each player chooses from among the permanents they control "
            "an artifact, a creature, an enchantment, and a land, then "
            "sacrifices the rest."
        ),
        "Global Ruin": (
            "Each player chooses a land they control of each basic land "
            "type, then sacrifices the rest of their lands."
        ),
        "Burning of Xinye": (
            "You destroy four lands you control, then target opponent "
            "destroys four lands they control. Then Burning of Xinye "
            "deals 4 damage to each creature."
        ),
    }
    for name, oracle in shapes.items():
        assert card_matches_mass_land_denial(_card(name, oracle)), name


def test_mld_predicate_excludes_targeted_and_single_land_effects():
    """Single-target land destruction, one-land symmetric edicts, and
    subtype wipes ('all Islands') are NOT mass land denial."""
    assert not card_matches_mass_land_denial(_card(
        "Strip Mine", "{T}, Sacrifice Strip Mine: Destroy target land.",
    ))
    assert not card_matches_mass_land_denial(_card(
        "Smallpox",
        "Each player loses 1 life, discards a card, sacrifices a "
        "creature, then sacrifices a land.",
    ))
    assert not card_matches_mass_land_denial(_card(
        "Boil", "Destroy all Islands.",
    ))
    assert not card_matches_mass_land_denial(_card(
        "Divination", "Draw two cards.",
    ))


def test_iter_snapshot_cards_walks_store_and_skips_corrupt(tmp_path):
    (tmp_path / "good.json").write_text(
        _json.dumps({"name": "Good Card", "oracle_text": "x"}),
        encoding="utf-8",
    )
    (tmp_path / "corrupt.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    cards = list(iter_snapshot_cards(tmp_path))
    assert cards == [{"name": "Good Card", "oracle_text": "x"}]


def test_iter_snapshot_cards_missing_dir_yields_nothing(tmp_path):
    assert list(iter_snapshot_cards(tmp_path / "nope")) == []


def test_iter_snapshot_cards_defaults_to_scryfall_cache_dir(
    tmp_path, monkeypatch,
):
    import commander_builder.scryfall_client as sc
    monkeypatch.setattr(sc, "CACHE_DIR", tmp_path)
    (tmp_path / "a.json").write_text(
        _json.dumps({"name": "A"}), encoding="utf-8",
    )
    assert [c["name"] for c in iter_snapshot_cards()] == ["A"]


def test_snapshot_set_builders_lowercase_and_reduce_to_front_face():
    cards = [
        _card("Time Warp",
              "Target player takes an extra turn after this one."),
        _card("Modal Turn // Back Land", "Take an extra turn after this one."),
        _card("Armageddon", "Destroy all lands."),
        _card("Divination", "Draw two cards."),
        # Alias duplicate (the store writes folded-slug copies).
        _card("Time Warp",
              "Target player takes an extra turn after this one."),
    ]
    assert extra_turn_names_from_snapshots(cards) == {
        "time warp", "modal turn",
    }
    assert mld_names_from_snapshots(cards) == {"armageddon"}


# ---------------------------------------------------------------------------
# scripts/refresh_card_lists.py wiring for the snapshot-backed categories
# ---------------------------------------------------------------------------

import sys as _sys
from pathlib import Path as _Path

# scripts/ isn't a package and isn't on sys.path by default; same import
# pattern as tests/test_merge_soak.py.
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "scripts"))

import refresh_card_lists as _refresh_cli  # noqa: E402


def test_cli_extra_turns_category_diffs_against_snapshot_store(
    tmp_path, monkeypatch, capsys,
):
    import commander_builder.scryfall_client as sc
    store = tmp_path / "oracle_snapshots"
    store.mkdir()
    monkeypatch.setattr(sc, "CACHE_DIR", store)
    (store / "time_warp.json").write_text(_json.dumps({
        "name": "Time Warp",
        "oracle_text": "Target player takes an extra turn after this one.",
    }), encoding="utf-8")
    (store / "new_turn_spell.json").write_text(_json.dumps({
        "name": "Brand New Turn Spell",
        "oracle_text": "Take an extra turn after this one.",
    }), encoding="utf-8")

    assert _refresh_cli.main(["--only", "extra-turns", "--json"]) == 0
    report = _json.loads(capsys.readouterr().out)["extra-turns"]
    # A snapshot matching the filter but absent from the list surfaces
    # as a candidate; the list entry the tiny store DOES hold is kept;
    # everything else in the hardcoded list reads stale (tiny store).
    assert "brand new turn spell" in report["candidates"]
    assert "time warp" in report["kept"]
    assert "time stretch" in report["stale"]


def test_cli_mld_category_diffs_against_snapshot_store(
    tmp_path, monkeypatch, capsys,
):
    import commander_builder.scryfall_client as sc
    store = tmp_path / "oracle_snapshots"
    store.mkdir()
    monkeypatch.setattr(sc, "CACHE_DIR", store)
    (store / "armageddon.json").write_text(_json.dumps({
        "name": "Armageddon", "oracle_text": "Destroy all lands.",
    }), encoding="utf-8")

    assert _refresh_cli.main(["--only", "mld", "--json"]) == 0
    report = _json.loads(capsys.readouterr().out)["mld"]
    assert "armageddon" in report["kept"]
    assert "sunder" in report["stale"]  # not in the tiny store


def test_cli_snapshot_categories_note_an_empty_store_instead_of_stale_spam(
    tmp_path, monkeypatch, capsys,
):
    """An empty snapshot store is a statement about the store, not the
    hardcoded lists — the CLI must say so rather than reporting every
    curated name as stale."""
    import commander_builder.scryfall_client as sc
    monkeypatch.setattr(sc, "CACHE_DIR", tmp_path / "empty")
    assert _refresh_cli.main(["--only", "extra-turns", "--json"]) == 0
    report = _json.loads(capsys.readouterr().out)["extra-turns"]
    assert report["stale"] == []
    assert report["candidates"] == []
    assert "commander-oracle-refresh" in report["note"]
    assert report["kept"]  # the current list rides along for review
