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


# --- find_top_liked_decks_for_commander (multi-deck variant) ---------------
# This is the top-N fetcher that the bracket-peers advisor mode needs:
# given a commander, return up to N highest-liked decks at the requested
# bracket. The singular variant returns 1; this one supports N so the
# frequency-across-references math has signal.

def test_find_top_liked_decks_returns_top_n_when_search_has_more(monkeypatch):
    """When the search returns >N hits, we fetch the first N (likes desc)."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    card_search_response = {"data": [
        {"id": "card-uuid-1", "name": "Hakbal of the Surging Soul"},
    ]}
    # 7 deck results, all matching the commander; we ask for top 5.
    deck_search_response = {"data": [
        {"publicId": f"deck-{i}",
         "commanders": [{"name": "Hakbal of the Surging Soul"}]}
        for i in range(1, 8)
    ]}
    fetched_pids: list[str] = []

    def fake_get(url):
        if "/cards/search" in url:
            return card_search_response
        if "/decks/search" in url:
            return deck_search_response
        # fetch_deck path — URL ends with /{publicId}.
        # Tag each deck with the requested bracket so the new
        # re-verification step (Phase A gap #1) doesn't drop them.
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        fetched_pids.append(pid)
        return {"publicId": pid, "name": f"Hakbal #{pid}", "bracket": 3}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander(
        "Hakbal of the Surging Soul", bracket=3, n=5,
    )
    assert len(out) == 5
    # First five publicIds, in the order Moxfield returned them.
    assert [d["publicId"] for d in out] == ["deck-1", "deck-2", "deck-3", "deck-4", "deck-5"]
    assert fetched_pids == ["deck-1", "deck-2", "deck-3", "deck-4", "deck-5"]


def test_find_top_liked_decks_returns_fewer_when_search_returns_few(monkeypatch):
    """If the search only finds 2 decks and we asked for 5, return 2."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal of the Surging Soul"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "a", "commanders": [{"name": "Hakbal of the Surging Soul"}]},
                {"publicId": "b", "commanders": [{"name": "Hakbal of the Surging Soul"}]},
            ]}
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        return {"publicId": pid, "name": f"Deck {pid}"}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal of the Surging Soul", n=5)
    assert len(out) == 2


def test_find_top_liked_decks_empty_when_card_id_unresolved(monkeypatch):
    """No exact card match → empty list, never raises."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander
    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json",
        lambda url: {"data": [{"id": "x", "name": "Different Card"}]},
    )
    assert find_top_liked_decks_for_commander("Hakbal") == []


def test_find_top_liked_decks_empty_on_network_error(monkeypatch):
    """Network failure during search → empty list (caller falls back)."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander
    def boom(url):
        raise OSError("network down")
    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", boom,
    )
    assert find_top_liked_decks_for_commander("Whatever") == []


def test_find_top_liked_decks_skips_failed_fetches(monkeypatch):
    """If one publicId's fetch_deck fails, the others still come back."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "good-1", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "broken", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "good-2", "commanders": [{"name": "Hakbal"}]},
            ]}
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        if pid == "broken":
            raise OSError("403 on this one")
        return {"publicId": pid, "name": pid}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal", n=3)
    # The middle fetch failed; the other two still returned.
    assert [d["publicId"] for d in out] == ["good-1", "good-2"]


def test_find_top_liked_decks_passes_bracket_to_search(monkeypatch):
    """The bracket filter actually lands in the search URL params."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander
    seen_search_url = {}

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            seen_search_url["url"] = url
            return {"data": []}
        return {"publicId": url.rsplit("/", 1)[-1]}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    find_top_liked_decks_for_commander("Hakbal", bracket=4, n=3)
    assert "bracket=4" in seen_search_url["url"]
    # Sort by likes desc so the highest-engagement decks come first.
    assert "sortColumn=likes" in seen_search_url["url"]
    assert "sortDirection=descending" in seen_search_url["url"]


def test_find_top_liked_decks_filters_out_wrong_bracket(monkeypatch):
    """Regression for Phase A gap #1: Moxfield's `bracket` search filter
    is loose — it returns near-bracket decks too. Without a client-side
    re-check, a B4 audit could read recommendations from B3/B5 decks
    mixed in, diluting the signal we're trying to capture. Re-verify
    each returned deck via resolve_bracket() and drop mismatches."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "b4-a", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "b3",   "commanders": [{"name": "Hakbal"}]},  # wrong bracket
                {"publicId": "b4-b", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "b5",   "commanders": [{"name": "Hakbal"}]},  # wrong bracket
                {"publicId": "b4-c", "commanders": [{"name": "Hakbal"}]},
            ]}
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        # Tag each fetched deck with a bracket consistent with its id.
        bracket = (
            4 if pid.startswith("b4")
            else 3 if pid == "b3"
            else 5 if pid == "b5"
            else 0
        )
        return {"publicId": pid, "name": pid, "bracket": bracket}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal", bracket=4, n=5)
    pids = [d["publicId"] for d in out]
    # Only the B4 decks survived the re-verification step.
    assert pids == ["b4-a", "b4-b", "b4-c"]


def test_find_top_liked_decks_skips_verification_when_bracket_none(monkeypatch):
    """When no bracket filter was passed (caller doesn't care), don't
    re-verify — accept whatever Moxfield returned."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "a", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "b", "commanders": [{"name": "Hakbal"}]},
            ]}
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        return {"publicId": pid, "name": pid}  # no bracket field

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal", bracket=None, n=5)
    # Both accepted — no filter requested.
    assert len(out) == 2


def test_find_top_liked_decks_drops_unverifiable_bracket_when_filtering(
    monkeypatch,
):
    """Decks with no bracket fields at all (resolve_bracket returns 0)
    must drop when a bracket filter is active — we can't confirm they
    match what the user asked for."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "good", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "no-bracket", "commanders": [{"name": "Hakbal"}]},
            ]}
        pid = url.rstrip("/").rsplit("/", 1)[-1]
        if pid == "good":
            return {"publicId": pid, "name": pid, "bracket": 4}
        return {"publicId": pid, "name": pid}  # no bracket at all

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal", bracket=4, n=5)
    pids = [d["publicId"] for d in out]
    assert pids == ["good"]


def test_find_top_liked_decks_dedupes_duplicate_public_ids(monkeypatch):
    """Defensive: if Moxfield returns the same deck twice (paging glitch),
    don't double-count it in the references."""
    from commander_builder.moxfield_import import find_top_liked_decks_for_commander

    def fake_get(url):
        if "/cards/search" in url:
            return {"data": [{"id": "c1", "name": "Hakbal"}]}
        if "/decks/search" in url:
            return {"data": [
                {"publicId": "abc", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "abc", "commanders": [{"name": "Hakbal"}]},
                {"publicId": "xyz", "commanders": [{"name": "Hakbal"}]},
            ]}
        pid = url.rsplit("/", 1)[-1]
        return {"publicId": pid, "name": pid}

    monkeypatch.setattr(
        "commander_builder.moxfield_import._http_get_json", fake_get,
    )
    out = find_top_liked_decks_for_commander("Hakbal", n=5)
    pids = [d["publicId"] for d in out]
    assert pids == ["abc", "xyz"]  # second "abc" dropped


# --- same-id re-import overwrite + name-collision uniquify -----------------
# Regression tests for the adversarial-review import-workflow fixes:
#   1. Re-importing the SAME Moxfield deck must overwrite in place (the
#      documented audit-cycle re-pull semantics), preserving local-only
#      metadata (Protect=).
#   2. A DIFFERENT deck whose name sanitizes to the same filename must get
#      a uniquified name that KEEPS the ` [B<n>].dck` suffix shape, so it
#      stays visible to every bracket-suffix filter.

def _deck_json(name="My Deck", pid="pid-1", bracket=3, main_card="Sol Ring"):
    """Minimal Moxfield-shape payload for the import paths."""
    return {
        "name": name,
        "publicId": pid,
        "bracket": bracket,
        "format": "commander",
        "boards": {
            "commanders": {"cards": {"c": {
                "quantity": 1, "card": {"name": "Atraxa, Praetors' Voice"}}}},
            "mainboard": {"cards": {"m": {
                "quantity": 1, "card": {"name": main_card}}}},
        },
    }


def test_reimport_same_id_overwrites_in_place(tmp_path, monkeypatch):
    """Audit-cycle step 4: re-pulling the same deck must update the SAME
    file — no '(2)' copy, content refreshed. (The old behavior uniquified,
    so the v2 snapshot copied the untouched v1 file and the A/B compared
    a deck against itself.)"""
    from commander_builder import moxfield_import as mi

    decks = {"pid-1": _deck_json(main_card="Sol Ring")}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    assert p1.name == "[USER] My Deck [B3].dck"
    assert "Sol Ring" in p1.read_text(encoding="utf-8")

    # The audit edited the deck on Moxfield; re-pull it.
    decks["pid-1"] = _deck_json(main_card="Arcane Signet")
    p2 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)

    assert p2 == p1
    text = p1.read_text(encoding="utf-8")
    assert "Arcane Signet" in text
    assert "Sol Ring" not in text
    # No numbered duplicate appeared.
    assert sorted(p.name for p in tmp_path.glob("*.dck")) == [p1.name]


def test_reimport_preserves_local_protect_lines(tmp_path, monkeypatch):
    """User-authored [metadata] Protect= lines are local-only (never in the
    Moxfield payload) — a same-id overwrite must carry them over, and they
    must remain parseable by the real reader."""
    from commander_builder import moxfield_import as mi
    from commander_builder.web._helpers import read_protected_cards

    decks = {"pid-1": _deck_json()}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    # User locks two pet cards in the .dck metadata.
    text = p1.read_text(encoding="utf-8")
    text = text.replace(
        "Moxfield=pid-1",
        "Moxfield=pid-1\nProtect=Sol Ring\nProtect=Krenko, Mob Boss",
    )
    p1.write_text(text, encoding="utf-8")

    decks["pid-1"] = _deck_json(main_card="Arcane Signet")
    p2 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)

    assert p2 == p1
    merged = p1.read_text(encoding="utf-8")
    assert "Arcane Signet" in merged  # fresh content landed
    # Protect= survived AND still parses through the real reader.
    assert read_protected_cards(merged) == ["Sol Ring", "Krenko, Mob Boss"]


def test_import_different_deck_with_colliding_name_uniquifies(tmp_path, monkeypatch):
    """Two DIFFERENT Moxfield decks whose names sanitize to the same
    filename: the second import must land under a uniquified name that
    still ends in ` [B<n>].dck` — verified against the ACTUAL bracket
    filters (status._count_decks, _existing_moxfield_ids)."""
    from commander_builder import moxfield_import as mi
    from commander_builder.status import _count_decks

    decks = {
        "pid-1": _deck_json(pid="pid-1"),
        "pid-2": _deck_json(pid="pid-2", main_card="Counterspell"),
    }
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    p2 = mi.import_deck("pid-2", out_dir=tmp_path, is_user=True)

    assert p1 != p2
    # Counter BEFORE the bracket tag, keeping the [B3].dck suffix shape.
    assert p2.name == "[USER] My Deck (2) [B3].dck"
    assert p1.read_text(encoding="utf-8") != p2.read_text(encoding="utf-8")

    # Both decks visible to the real bracket-suffix consumers.
    counts = _count_decks(tmp_path)
    assert counts[3]["user"] == 2
    assert mi._existing_moxfield_ids(tmp_path, 3) == {"pid-1", "pid-2"}


def test_uniquify_inserts_counter_before_bracket_tag(tmp_path):
    """_uniquify keeps the ` [B<n>].dck` suffix intact; the counter goes
    before the bracket tag. (` [B?]` is covered by the regex too, but a
    literal `?` is not a legal Windows filename, so no on-disk case here.)"""
    p = tmp_path / "[USER] My Deck [B3].dck"
    p.write_text("x")
    out = _uniquify(p)
    assert out.name == "[USER] My Deck (2) [B3].dck"
    out.write_text("y")
    assert _uniquify(p).name == "[USER] My Deck (3) [B3].dck"


def test_write_deck_same_id_skips_different_id_uniquifies(tmp_path):
    """Harvest write path (_write_deck): same recorded Moxfield id → skip
    (correct pool dedupe); DIFFERENT id under the same sanitized name →
    written under a uniquified name instead of being silently dropped."""
    from commander_builder.moxfield_import import _write_deck

    first = _write_deck(_deck_json(pid="pid-1"), 3, tmp_path)
    assert first is not None and first.name == "My Deck [B3].dck"

    # Re-harvest of the SAME deck: dedupe-skip, file untouched.
    before = first.read_text(encoding="utf-8")
    assert _write_deck(_deck_json(pid="pid-1", main_card="Brainstorm"), 3, tmp_path) is None
    assert first.read_text(encoding="utf-8") == before

    # A DIFFERENT deck colliding on the name: must be written, uniquified.
    other = _write_deck(_deck_json(pid="pid-2", main_card="Counterspell"), 3, tmp_path)
    assert other is not None
    assert other.name == "My Deck (2) [B3].dck"


def test_write_deck_still_skips_pre_metadata_files(tmp_path):
    """A name-owning file WITHOUT Moxfield= metadata is unidentifiable —
    keep the conservative legacy skip (don't overwrite, don't duplicate)."""
    from commander_builder.moxfield_import import _write_deck

    legacy = tmp_path / "My Deck [B3].dck"
    legacy.write_text("[metadata]\nName=My Deck\n[Main]\n1 Sol Ring\n", encoding="utf-8")
    assert _write_deck(_deck_json(pid="pid-1"), 3, tmp_path) is None
    assert "Sol Ring" in legacy.read_text(encoding="utf-8")
    assert sorted(p.name for p in tmp_path.glob("*.dck")) == ["My Deck [B3].dck"]


# --- Name=-from-final-filename-stem stamping -------------------------------
# Regression tests for the non-ASCII / ':' deck-name break: to_dck stamps the
# RAW Moxfield name into Name=, but safe_filename strips non-ASCII and
# substitutes ':' etc., so "Chatterfang: Squirrel Tribal 🐿" landed under a
# filename whose stem no longer normalized to its own Name= — invisible to
# every name-keyed consumer (compare_versions pod aggregation, pool_curator
# matching, and Forge's own picker). Writers now stamp Name= with the FINAL
# filename stem (dck_meta.stamp_name_preserving_display), keeping the pretty
# name in DisplayName= for the status CLI.

_UGLY_NAME = "Chatterfang: Squirrel Tribal \U0001f43f"


def test_stamp_name_preserving_display_rules():
    """Unit pin for the shared stamping helper (dck_meta): Name= becomes the
    stem; the old pretty name moves to DisplayName=; other metadata passes
    through untouched; degenerate inputs behave sanely."""
    from commander_builder.dck_meta import stamp_name_preserving_display

    # Pretty name → stem + DisplayName; Moxfield=/Protect= untouched.
    src = (
        "[metadata]\nName=Pretty: Name \U0001f43f\nMoxfield=abc\n"
        "Protect=Sol Ring\n[Main]\n1 Sol Ring\n"
    )
    out = stamp_name_preserving_display(src, "[USER] Pretty_ Name [B3]")
    assert "Name=[USER] Pretty_ Name [B3]\n" in out
    assert "DisplayName=Pretty: Name \U0001f43f\n" in out
    assert "Moxfield=abc" in out and "Protect=Sol Ring" in out

    # Name already equals the stem (re-stamp): no DisplayName churn.
    out2 = stamp_name_preserving_display(out.replace(
        "DisplayName=Pretty: Name \U0001f43f\n", ""), "[USER] Pretty_ Name [B3]")
    assert "DisplayName=" not in out2

    # Existing DisplayName wins — never duplicated or clobbered.
    out3 = stamp_name_preserving_display(out, "[USER] Renamed [B3]")
    assert out3.count("DisplayName=") == 1
    assert "DisplayName=Pretty: Name \U0001f43f" in out3
    assert "Name=[USER] Renamed [B3]\n" in out3

    # No Name= at all (bare paste): synthesized from the stem, nothing to
    # preserve.
    out4 = stamp_name_preserving_display("[Main]\n1 Sol Ring\n", "Stem")
    assert "Name=Stem" in out4 and "DisplayName=" not in out4


def test_rewrite_name_replaces_empty_name_line():
    """(dck_meta hardening) An EMPTY 'Name=' line is still the Name= line.
    The old ^Name=.+$ regex skipped it, so rewrite_name concluded "no
    Name=" and synthesized a second one under [metadata] — leaving a
    duplicate whose winner is Forge-parser-dependent. Exactly one Name=
    must remain, holding the new value; neighbors pass through."""
    import re as _re
    from commander_builder.dck_meta import rewrite_name

    src = "[metadata]\nName=\nMoxfield=abc\n[Main]\n1 Sol Ring\n"
    out = rewrite_name(src, "Stem")
    assert _re.findall(r"^Name=.*$", out, _re.MULTILINE) == ["Name=Stem"]
    assert "Moxfield=abc" in out and "1 Sol Ring" in out


def test_import_deck_stamps_name_from_final_filename_stem(tmp_path, monkeypatch):
    """(a) Non-ASCII + ':' deck name: the written file's Name= must equal
    its own filename stem, and log_parser._normalize must agree whether it
    starts from the filename or from the Name= field."""
    from commander_builder import moxfield_import as mi
    from commander_builder.log_parser import _normalize

    decks = {"pid-1": _deck_json(name=_UGLY_NAME, pid="pid-1")}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    # ':' → '_', emoji stripped, [USER]/[B3] wrapping from deck_destination.
    assert p.name == "[USER] Chatterfang_ Squirrel Tribal [B3].dck"

    text = p.read_text(encoding="utf-8")
    import re as _re
    name_val = _re.search(r"^Name=(.+)$", text, _re.MULTILINE).group(1)
    assert name_val == p.stem
    # The invariant every name-keyed pipeline relies on.
    assert _normalize(p.name) == _normalize(name_val)
    # Pretty name preserved for display surfaces.
    assert f"DisplayName={_UGLY_NAME}" in text


def test_reimport_nonascii_name_still_classified_same(tmp_path, monkeypatch):
    """(b) The stamp must not disturb Moxfield= metadata: a re-import of the
    same publicId is still classified 'same' — overwrite in place, no
    numbered duplicate."""
    from commander_builder import moxfield_import as mi

    decks = {"pid-1": _deck_json(name=_UGLY_NAME, pid="pid-1")}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    assert "Moxfield=pid-1" in p1.read_text(encoding="utf-8")

    decks["pid-1"] = _deck_json(
        name=_UGLY_NAME, pid="pid-1", main_card="Arcane Signet",
    )
    p2 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)

    assert p2 == p1  # 'same' verdict: overwrote in place
    text = p1.read_text(encoding="utf-8")
    assert "Moxfield=pid-1" in text  # id metadata intact after stamping
    assert "Arcane Signet" in text
    assert sorted(q.name for q in tmp_path.glob("*.dck")) == [p1.name]


def test_reimport_preserves_protect_and_stamped_name(tmp_path, monkeypatch):
    """The 6ccf3f0 overwrite path (merge Protect=) composes with the stamp:
    the merged re-import keeps Protect= AND ends up Name=stem again."""
    from commander_builder import moxfield_import as mi
    from commander_builder.web._helpers import read_protected_cards

    decks = {"pid-1": _deck_json(name=_UGLY_NAME, pid="pid-1")}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    text = p1.read_text(encoding="utf-8")
    p1.write_text(
        text.replace("Moxfield=pid-1", "Moxfield=pid-1\nProtect=Sol Ring"),
        encoding="utf-8",
    )

    decks["pid-1"] = _deck_json(
        name=_UGLY_NAME, pid="pid-1", main_card="Arcane Signet",
    )
    p2 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    assert p2 == p1
    merged = p1.read_text(encoding="utf-8")
    assert read_protected_cards(merged) == ["Sol Ring"]
    import re as _re
    assert _re.search(r"^Name=(.+)$", merged, _re.MULTILINE).group(1) == p1.stem


def test_reimport_preserves_locally_edited_displayname(tmp_path, monkeypatch):
    """dck_meta's documented contract: user edits to DisplayName= survive
    re-imports. The mechanism is two-part and ORDER-dependent —
    _merge_local_metadata carries the local line into the fresh render, then
    stamp_name_preserving_display's "existing DisplayName wins" rule sees it
    and never synthesizes a competitor — so the result must be the LOCAL
    edit, exactly once, with Name= still stamped from the stem."""
    from commander_builder import moxfield_import as mi
    import re as _re

    decks = {"pid-1": _deck_json(name=_UGLY_NAME, pid="pid-1")}
    monkeypatch.setattr(mi, "fetch_deck", lambda pid: decks[pid])

    p1 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    text = p1.read_text(encoding="utf-8")
    assert f"DisplayName={_UGLY_NAME}" in text  # stamp wrote the pretty name

    # User hand-edits the display name locally.
    p1.write_text(
        text.replace(f"DisplayName={_UGLY_NAME}", "DisplayName=Squirrel Storm"),
        encoding="utf-8",
    )

    # Deck changes upstream; re-pull the same publicId (overwrite in place).
    decks["pid-1"] = _deck_json(
        name=_UGLY_NAME, pid="pid-1", main_card="Arcane Signet",
    )
    p2 = mi.import_deck("pid-1", out_dir=tmp_path, is_user=True)
    assert p2 == p1

    merged = p1.read_text(encoding="utf-8")
    assert "Arcane Signet" in merged  # fresh content landed
    # The local edit won — and there is exactly ONE DisplayName= line (a
    # duplicate would make the honored value ordering luck).
    assert (_re.findall(r"^DisplayName=(.*)$", merged, _re.MULTILINE)
            == ["Squirrel Storm"])
    # Name= is still stamped from the final filename stem.
    assert _re.search(r"^Name=(.+)$", merged, _re.MULTILINE).group(1) == p1.stem


def test_write_deck_stamps_name_including_uniquify_counter(tmp_path):
    """Harvest path: the stamp uses the FINAL stem — after _uniquify — so a
    collision-renamed deck's Name= carries the '(2)' counter Forge reports."""
    from commander_builder.moxfield_import import _write_deck
    import re as _re

    first = _write_deck(_deck_json(name=_UGLY_NAME, pid="pid-1"), 3, tmp_path)
    assert first is not None
    text1 = first.read_text(encoding="utf-8")
    assert _re.search(r"^Name=(.+)$", text1, _re.MULTILINE).group(1) == first.stem

    # A DIFFERENT deck colliding on the sanitized name gets uniquified —
    # and its Name= must match the uniquified stem, not the original.
    other = _write_deck(
        _deck_json(name=_UGLY_NAME, pid="pid-2", main_card="Counterspell"),
        3, tmp_path,
    )
    assert other is not None and "(2)" in other.stem
    text2 = other.read_text(encoding="utf-8")
    assert _re.search(r"^Name=(.+)$", text2, _re.MULTILINE).group(1) == other.stem


def test_bulk_import_stamps_name_from_stem(tmp_path, monkeypatch):
    """Bulk path writes the same stamped shape as import_deck."""
    from commander_builder import moxfield_import as mi
    import re as _re

    monkeypatch.setattr(
        mi, "fetch_deck",
        lambda pid: _deck_json(name=_UGLY_NAME, pid=pid),
    )
    result = mi.bulk_import(["pid-1"], out_dir=tmp_path, is_user=True)
    assert result.success_count == 1
    p = Path(result.successes[0]["path"])
    text = p.read_text(encoding="utf-8")
    assert _re.search(r"^Name=(.+)$", text, _re.MULTILINE).group(1) == p.stem
    assert f"DisplayName={_UGLY_NAME}" in text
    assert "Moxfield=pid-1" in text


def test_harvest_loop_sleeps_on_fetch_failure(tmp_path, monkeypatch):
    """Politeness regression: the per-fetch sleep must fire on the FAILURE
    path too — previously a fetch exception skipped straight to the next
    entry with zero delay, hammering Moxfield exactly when it was erroring."""
    from commander_builder import moxfield_import as mi

    sleeps: list[float] = []
    monkeypatch.setattr(mi.time, "sleep", lambda s: sleeps.append(s))

    def fake_search(bracket, page_size=20, page=1, sort_type="updated", since_iso=None):
        if page == 1:
            return [{"publicId": "bad-1"}, {"publicId": "good-1"}]
        return []

    def fake_fetch(pid):
        if pid == "bad-1":
            raise OSError("simulated 429")
        return _deck_json(name=f"Deck {pid}", pid=pid)

    monkeypatch.setattr(mi, "search_decks", fake_search)
    monkeypatch.setattr(mi, "fetch_deck", fake_fetch)

    written = mi.import_by_bracket(3, count=1, out_dir=tmp_path)
    assert len(written) == 1
    # One sleep per fetch ATTEMPT: the failed bad-1 fetch AND the good one.
    assert len(sleeps) == 2
