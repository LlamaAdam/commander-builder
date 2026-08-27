"""Tests for archidekt_client — fully offline via injected fetch_json.

The adapter half of this file (``to_deck_json`` / ``extract_mainboard`` /
commander detection / quantities) runs against a REAL captured response —
``tests/fixtures/archidekt_deck_shape.json``, trimmed from a live pull of
deck 24864897 on 2026-08-20. R2-P18's finding was that those functions
were pinned only by shapes this file invented, which is exactly the setup
in which a wrong assumption about the API passes forever. The synthetic
helpers below survive only for paths a healthy capture cannot contain:
malformed entries, missing names, drift in ``edhBracket``.

A SECOND capture — ``tests/fixtures/archidekt_deck_mdfc_shape.json``,
deck 5273595, 2026-08-27 — closes the double-faced gap the first one
pinned as open, and vindicated the method: it caught the adapter emitting
``"Front // Back"`` into ``.dck`` files Forge names by the front face.
"""

from __future__ import annotations

import email.message
import json
import urllib.error
from pathlib import Path

import pytest

import commander_builder.archidekt_client as ac
from commander_builder.archidekt_client import (
    DEFAULT_N,
    extract_mainboard,
    fetch_top_decks,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
REAL_DECK_JSON = FIXTURE_DIR / "archidekt_deck_shape.json"
MDFC_DECK_JSON = FIXTURE_DIR / "archidekt_deck_mdfc_shape.json"
PRIMER_MD = FIXTURE_DIR / "hazel_primer.md"


@pytest.fixture
def real_deck():
    """The trimmed real capture, reloaded per test (tests may mutate it)."""
    return json.loads(REAL_DECK_JSON.read_text(encoding="utf-8"))


@pytest.fixture
def mdfc_deck():
    """The second real capture — the double-faced half of the pair."""
    return json.loads(MDFC_DECK_JSON.read_text(encoding="utf-8"))


def card(name, quantity=1, categories=()):
    return {"quantity": quantity, "categories": list(categories),
            "card": {"oracleCard": {"name": name}}}


def detail(cards, categories=()):
    return {"cards": cards, "categories": list(categories)}


def search_hit(deck_id, size=100, edh_bracket=None):
    return {"id": deck_id, "size": size, "edhBracket": edh_bracket}


# ---------------------------------------------------------------------------
# extract_mainboard — SYNTHETIC EDGE PATHS ONLY.
#
# The happy path (names, commander exclusion, includedInDeck) is asserted
# against the real capture further down; what is left here is the set of
# shapes a healthy deck response does not contain, so no capture can pin
# them: mixed-case category names, zero quantities, malformed entries,
# entries with no categories at all.
# ---------------------------------------------------------------------------

def test_mainboard_category_matching_is_case_insensitive():
    d = detail(
        [card("Wish", categories=["MAYBEBOARD"])],
        categories=[{"name": "maybeboard", "includedInDeck": False}],
    )
    assert extract_mainboard(d) == []


def test_mainboard_skips_zero_quantity_and_malformed():
    d = detail([card("Ghost", quantity=0), "not-a-dict",
                {"quantity": 1, "card": {}}, card("Real")])
    assert extract_mainboard(d) == ["Real"]


def test_mainboard_empty_categories_counts():
    assert extract_mainboard(detail([card("Sol Ring", categories=[])])) \
        == ["Sol Ring"]


# ---------------------------------------------------------------------------
# fetch_top_decks
# ---------------------------------------------------------------------------

def make_fetcher(search_results, details, fail_ids=()):
    calls = []

    def fetch(url):
        calls.append(url)
        if "/decks/v3/" in url:
            return {"count": len(search_results),
                    "results": search_results}
        deck_id = int(url.rstrip("/").rsplit("/", 1)[1])
        if deck_id in fail_ids:
            raise OSError("boom")
        return details[deck_id]

    fetch.calls = calls
    return fetch


def test_fetch_top_decks_returns_name_lists():
    fetch = make_fetcher(
        [search_hit(1), search_hit(2)],
        {1: detail([card("A"), card("B")]), 2: detail([card("C")])},
    )
    out = fetch_top_decks("Krenko, Mob Boss", n=2, fetch_json=fetch)
    assert out == [["A", "B"], ["C"]]
    assert "commanderName=Krenko" in fetch.calls[0]


def test_fetch_top_decks_soft_bracket_filter():
    fetch = make_fetcher(
        [search_hit(1, edh_bracket=5), search_hit(2, edh_bracket=3),
         search_hit(3, edh_bracket=None)],
        {2: detail([card("B")]), 3: detail([card("C")])},
    )
    out = fetch_top_decks("X", bracket=3, n=5, fetch_json=fetch)
    # bracket-5 deck skipped without a fetch; null bracket passes.
    assert out == [["B"], ["C"]]
    assert not any("/decks/1/" in u for u in fetch.calls)


def test_fetch_top_decks_skips_partial_decks_and_failures():
    fetch = make_fetcher(
        [search_hit(1, size=30), search_hit(2), search_hit(3)],
        {3: detail([card("C")])},
        fail_ids={2},
    )
    out = fetch_top_decks("X", n=5, fetch_json=fetch)
    assert out == [["C"]]


def test_fetch_top_decks_stops_at_n():
    fetch = make_fetcher(
        [search_hit(i) for i in range(1, 6)],
        {i: detail([card(f"C{i}")]) for i in range(1, 6)},
    )
    out = fetch_top_decks("X", n=2, fetch_json=fetch)
    assert len(out) == 2
    # 1 search + exactly 2 detail fetches — no wasted requests.
    assert len(fetch.calls) == 3


def test_fetch_top_decks_search_failure_returns_empty():
    def fetch(url):
        raise OSError("down")
    assert fetch_top_decks("X", n=3, fetch_json=fetch) == []


def test_fetch_top_decks_n_zero_no_requests():
    fetch = make_fetcher([], {})
    assert fetch_top_decks("X", n=0, fetch_json=fetch) == []
    assert fetch.calls == []


def test_default_n_is_modest():
    # The corpus build's request budget — a deliberate cap, pinned so a
    # future edit can't silently 4x the cold-build fetch count.
    assert DEFAULT_N <= 30


def test_bad_edh_bracket_drops_hit_not_source():
    # API drift: a non-numeric edhBracket must cost that hit only —
    # before the guard it raised ValueError and sank the whole source.
    fetch = make_fetcher(
        [search_hit(1, edh_bracket="not-a-number"),
         search_hit(2, edh_bracket=3)],
        {2: detail([card("B")])},
    )
    out = fetch_top_decks("X", bracket=3, n=5, fetch_json=fetch)
    assert out == [["B"]]
    assert not any("/decks/1/" in u for u in fetch.calls)


def test_bad_edh_bracket_ignored_when_no_bracket_filter():
    # Without a bracket filter the field is never consulted, so drift
    # there must not cost the hit.
    fetch = make_fetcher([search_hit(1, edh_bracket="junk")],
                         {1: detail([card("A")])})
    assert fetch_top_decks("X", n=5, fetch_json=fetch) == [["A"]]


# ---------------------------------------------------------------------------
# 429 / transient retry
# ---------------------------------------------------------------------------

def http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", hdrs, None)


def test_search_429_retried_honoring_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ac.time, "sleep", sleeps.append)
    state = {"n": 0}

    def fetch(url):
        if "/decks/v3/" in url:
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(429, retry_after="7")
            return {"count": 1, "results": [search_hit(1)]}
        return detail([card("A")])

    out = fetch_top_decks("X", n=1, fetch_json=fetch)
    assert out == [["A"]]
    assert sleeps == [7.0]  # server hint honored, not the exp curve


def test_429_exhausted_returns_empty_source(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ac.time, "sleep", sleeps.append)
    calls = []

    def fetch(url):
        calls.append(url)
        raise http_error(429)

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == []
    assert len(calls) == ac.MAX_RETRIES + 1
    # No Retry-After header -> exponential fallback, base * 2^attempt.
    assert sleeps == [ac.RETRY_BASE_DELAY_SEC * (2 ** a)
                      for a in range(ac.MAX_RETRIES)]


def test_detail_429_retried_per_deck(monkeypatch):
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)
    state = {"n": 0}

    def fetch(url):
        if "/decks/v3/" in url:
            return {"count": 1, "results": [search_hit(1)]}
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(503)
        return detail([card("A")])

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == [["A"]]


def test_deterministic_4xx_not_retried(monkeypatch):
    monkeypatch.setattr(
        ac.time, "sleep",
        lambda s: pytest.fail("must not back off on a deterministic 4xx"))
    calls = []

    def fetch(url):
        calls.append(url)
        raise http_error(404)

    assert fetch_top_decks("X", n=1, fetch_json=fetch) == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Single-deck import lane (decision C3) — the Moxfield fallback client
# ---------------------------------------------------------------------------

def _detail_card(name, quantity=1, categories=(), set_code="cmr", cn="123"):
    """An Archidekt detail-JSON card entry, including printing fields."""
    return {
        "quantity": quantity,
        "categories": list(categories),
        "card": {
            "oracleCard": {"name": name},
            "edition": {"editioncode": set_code},
            "collectorNumber": cn,
        },
    }


def _deck_detail(**over):
    d = {
        "id": 1234567,
        "name": "Krenko Goblins",
        "edhBracket": 3,
        "cards": [
            _detail_card("Krenko, Mob Boss", categories=["Commander"]),
            _detail_card("Sol Ring", set_code="c21", cn="7"),
            _detail_card("Mountain", quantity=27, set_code="", cn=""),
            _detail_card("Wishlist Card", categories=["Maybeboard"]),
        ],
        "categories": [
            {"name": "Commander", "includedInDeck": True},
            {"name": "Maybeboard", "includedInDeck": False},
        ],
    }
    d.update(over)
    return d


def test_is_archidekt_url_matches_host_only():
    assert ac.is_archidekt_url("https://archidekt.com/decks/1234567/krenko")
    assert ac.is_archidekt_url("https://www.ARCHIDEKT.com/decks/1")
    # A bare id is NOT claimed: that is exactly what a Moxfield call site
    # passes, and source selection is the caller's decision.
    assert not ac.is_archidekt_url("1234567")
    assert not ac.is_archidekt_url("https://moxfield.com/decks/abc123")
    assert not ac.is_archidekt_url("")


def test_parse_deck_id_from_url_and_bare_id():
    assert ac.parse_deck_id(
        "https://archidekt.com/decks/1234567/krenko-goblins") == "1234567"
    assert ac.parse_deck_id("https://archidekt.com/decks/1234567") == "1234567"
    assert ac.parse_deck_id("1234567") == "1234567"
    assert ac.parse_deck_id("  1234567  ") == "1234567"


def test_fetch_deck_hits_the_detail_endpoint():
    seen = []

    def fake_get(url):
        seen.append(url)
        return _deck_detail()

    out = ac.fetch_deck("https://archidekt.com/decks/1234567/krenko",
                        fetch_json=fake_get)
    assert seen == [f"{ac.BASE}/decks/1234567/"]
    assert out["name"] == "Krenko Goblins"


def test_fetch_deck_raises_unlike_the_corpus_functions():
    """Single-deck import has no redundancy to degrade into: swallowing
    the error would write an empty .dck and call it success."""
    def boom(url):
        raise urllib.error.URLError("dns")

    with pytest.raises(urllib.error.URLError):
        ac.fetch_deck("1234567", fetch_json=boom)


def test_fetch_deck_still_retries_transient_codes():
    calls = {"n": 0}
    hdrs = email.message.Message()

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 503, "busy", hdrs, None)
        return _deck_detail()

    out = ac.fetch_deck("1234567", fetch_json=flaky)
    assert calls["n"] == 2 and out["name"] == "Krenko Goblins"


@pytest.mark.parametrize("value,expected", [
    (3, 3), ("4", 4), (None, 0), ("weird", 0), (0, 0), (9, 0),
])
def test_deck_bracket_normalizes_to_the_moxfield_range(value, expected):
    """0 is the same 'unknown' resolve_bracket returns, so an unbracketed
    Archidekt deck lands as ``[B?]`` exactly like an unbracketed Moxfield
    one — most Archidekt decks leave edhBracket null."""
    assert ac.deck_bracket({"edhBracket": value}) == expected


def test_to_deck_json_defaults_a_missing_name():
    """Synthetic on purpose: the real capture has a name, and a nameless
    deck is a drift/error path no healthy response supplies."""
    out = ac.to_deck_json(_deck_detail(name=""))
    assert out["name"] == "Untitled"


# ---------------------------------------------------------------------------
# THE REAL SHAPE (R2-P18, 2026-08-20) — archidekt.com/api/decks/24864897/
#
# Every assertion below reads the trimmed capture of a real, public,
# owner-provided deck. Card entries in it are byte-for-byte verbatim, so
# these tests fail the day Archidekt changes the contract rather than the
# day someone edits a hand-written stub.
# ---------------------------------------------------------------------------

def _names(board: dict) -> list[str]:
    return [e["card"]["name"] for e in board["cards"].values()]


def test_real_fixture_is_a_capture_not_a_construction(real_deck):
    """Guard the fixture itself: it is evidence, and evidence that gets
    'helpfully' extended with invented entries stops being evidence."""
    prov = real_deck["_provenance"]
    assert "24864897" in prov["source"]
    assert prov["fetched"].startswith("2026-08-20")
    entries = real_deck["cards"]
    assert len(entries) == 9
    # This deck is the SINGLE-FACED baseline of the fixture pair: every
    # oracleCard here has an empty faces list and a one-face name. It
    # used to carry a ``known_gap`` note because that left the MDFC shape
    # unpinned; the gap was closed on 2026-08-27 by a second real capture
    # (deck 5273595), never by inventing an entry here.
    assert all(e["card"]["oracleCard"]["faces"] == [] for e in entries)
    assert not any("//" in e["card"]["oracleCard"]["name"] for e in entries)
    assert "known_gap" not in prov
    assert "archidekt_deck_mdfc_shape.json" in prov["closed_gap"]


def test_real_commander_is_marked_by_the_card_level_category(real_deck):
    """How a commander is ACTUALLY marked: no top-level ``commander``
    field, no flag on the entry — the card entry carries the category
    string ``"Commander"``, matching the deck's one ``isPremier`` category.
    """
    assert "commander" not in real_deck  # not a top-level field
    assert "commanders" not in real_deck

    hazel = next(e for e in real_deck["cards"]
                 if e["card"]["oracleCard"]["name"]
                 == "Hazel of the Rootbloom")
    assert hazel["categories"] == ["Commander"]
    premier = [c["name"] for c in real_deck["categories"] if c["isPremier"]]
    assert premier == ["Commander"]

    out = ac.to_deck_json(real_deck)
    assert _names(out["boards"]["commanders"]) == ["Hazel of the Rootbloom"]
    # ...and it is NOT also in the 99.
    assert "Hazel of the Rootbloom" not in _names(out["boards"]["mainboard"])
    assert "Hazel of the Rootbloom" not in extract_mainboard(real_deck)


def test_real_board_membership_follows_the_flag_not_the_name(real_deck):
    """The disproved assumption, pinned. This deck's ``Sideboard``
    category carries ``includedInDeck: true`` while ``Maybeboard`` carries
    false — so ``includedInDeck`` is per-deck user state and any
    name-based rule would import someone's sideboard as maindeck."""
    flags = {c["name"]: c["includedInDeck"] for c in real_deck["categories"]}
    assert flags["Maybeboard"] is False
    assert flags["Sideboard"] is True  # NOT what the old docstring claimed
    assert ac._excluded_categories(real_deck) == {"maybeboard"}

    main = _names(ac.to_deck_json(real_deck)["boards"]["mainboard"])
    assert "Bitterblossom" not in main
    assert "Torment of Hailfire" not in main


def test_real_maybeboard_entries_stack_categories(real_deck):
    """A maybeboarded card keeps its user category too
    (``["Maybeboard", "Tokens"]``), so exclusion has to test EVERY
    category on the entry — matching only the first would import it."""
    bitter = next(e for e in real_deck["cards"]
                  if e["card"]["oracleCard"]["name"] == "Bitterblossom")
    assert bitter["categories"] == ["Maybeboard", "Tokens"]
    assert bitter["categories"][0] != bitter["categories"][-1]
    assert "Bitterblossom" not in extract_mainboard(real_deck)


def test_real_quantities_survive_into_the_import(real_deck):
    """Real multi-copy entries: 11 Forest / 9 Swamp. Collapsing a stacked
    basic to 1x would silently rewrite the manabase."""
    by_name = {e["card"]["name"]: e for e in
               ac.to_deck_json(real_deck)["boards"]["mainboard"]
               ["cards"].values()}
    assert by_name["Forest"]["quantity"] == 11
    assert by_name["Swamp"]["quantity"] == 9
    assert by_name["Forest"]["card"]["set"] == "hob"
    assert by_name["Forest"]["card"]["cn"] == "193"
    # Everything else in this deck is a singleton.
    assert sorted(e["quantity"] for e in by_name.values()) == \
        [1, 1, 1, 1, 9, 11]


def test_real_printing_fields_pass_through_including_non_numeric_cn(
        real_deck):
    """``collectorNumber`` is a STRING and is not always numeric: The List
    printings come back as ``M20-193`` under ``editioncode: "plst"``.
    Coercing it to int (or dropping it) would lose the printing."""
    by_name = {e["card"]["name"]: e for e in
               ac.to_deck_json(real_deck)["boards"]["mainboard"]
               ["cards"].values()}
    assert by_name["Shared Summons"]["card"]["set"] == "plst"
    assert by_name["Shared Summons"]["card"]["cn"] == "M20-193"
    assert by_name["Prosperous Innkeeper"]["card"]["set"] == "blc"
    assert by_name["Prosperous Innkeeper"]["card"]["cn"] == "121"


def test_real_non_normal_layout_imports_like_any_other_card(real_deck):
    """The capture's one ``layout: "class"`` entry (an enchantment Class
    card). It carries a single ``oracleCard.name`` and ``faces: []``, so
    the adapter needs no layout special-case — pinned here so a future
    'handle weird layouts' patch has to prove it doesn't break this one."""
    ninja = next(e for e in real_deck["cards"]
                 if e["card"]["oracleCard"]["layout"] != "normal")
    assert ninja["card"]["oracleCard"]["layout"] == "class"
    assert ninja["card"]["oracleCard"]["name"] == "Ninja Teen"

    by_name = {e["card"]["name"]: e for e in
               ac.to_deck_json(real_deck)["boards"]["mainboard"]
               ["cards"].values()}
    assert by_name["Ninja Teen"]["card"]["set"] == "tmt"
    assert by_name["Ninja Teen"]["card"]["cn"] == "67"
    assert "Ninja Teen" in extract_mainboard(real_deck)


def test_real_deck_leaves_edh_bracket_null(real_deck):
    """The common real case (this deck included): ``edhBracket`` is null,
    so no ``bracket`` key at all and ``resolve_bracket`` falls through to
    its own unknown default — the deck files as ``[B?]``."""
    assert real_deck["edhBracket"] is None
    assert ac.deck_bracket(real_deck) == 0
    assert "bracket" not in ac.to_deck_json(real_deck)


def test_real_import_never_claims_a_moxfield_public_id(real_deck):
    """An Archidekt id is not a Moxfield publicId; stamping one into the
    ``Moxfield=`` line would poison the re-import dedupe index."""
    out = ac.to_deck_json(real_deck)
    assert "publicId" not in out
    assert str(real_deck["id"]) not in json.dumps(out)
    assert out["name"] == "Hazel demands Sacrifice"
    assert out["format"] == "commander"


def test_real_corpus_and_importer_agree_on_membership(real_deck):
    """One walk, one notion of 'part of the deck' — the corpus reader and
    the importer must never read the same URL differently."""
    imported = _names(ac.to_deck_json(real_deck)["boards"]["mainboard"])
    assert extract_mainboard(real_deck) == imported
    assert imported == [
        "Prosperous Innkeeper", "Nadier's Nightblade", "Shared Summons",
        "Forest", "Swamp", "Ninja Teen",
    ]


def test_real_extract_mainboard_is_one_name_per_entry_not_per_copy(
        real_deck):
    """Documented, deliberate: the corpus folds each list into a set, so
    names are not quantity-expanded. In the FULL capture that is 81 names
    for 99 cards — a caller that needs deck SIZE must not count this."""
    names = extract_mainboard(real_deck)
    assert len(names) == 6
    assert len(set(names)) == 6
    total_copies = sum(
        e["quantity"] for e in
        ac.to_deck_json(real_deck)["boards"]["mainboard"]["cards"].values())
    assert total_copies == 24 and total_copies != len(names)


def test_real_deck_renders_a_forge_dck_end_to_end(real_deck):
    """The point of the adapter: the real payload has to come out the far
    end of ``moxfield_import.to_dck`` as a loadable deck file."""
    from commander_builder.moxfield_import import to_dck

    dck = to_dck(ac.to_deck_json(real_deck)).splitlines()
    assert dck[:2] == ["[metadata]", "Name=Hazel demands Sacrifice"]
    assert dck.index("[Commander]") < dck.index("[Main]")
    assert dck[dck.index("[Commander]") + 1] == \
        "1 Hazel of the Rootbloom|BLC|2"
    main = dck[dck.index("[Main]") + 1:]
    assert "11 Forest|HOB|193" in main
    assert "1 Shared Summons|PLST|M20-193" in main
    assert "1 Ninja Teen|TMT|67" in main
    assert len(main) == 6


def test_real_capture_carries_the_skipped_card_data_flag(real_deck):
    """This deck's data was complete — the flag exists and is false. It is
    the API's own statement that a 200 can omit card data; see
    ``fetch_deck``'s guard."""
    assert real_deck["intentionallySkippedCardData"] is False
    assert real_deck["customCards"] == []


def test_fetch_deck_refuses_a_response_with_card_data_skipped(real_deck):
    """R2-P18: a 200 with ``intentionallySkippedCardData`` would leave
    every entry nameless, and the importer would write an EMPTY .dck and
    report success. The single-deck lane raises instead."""
    skipped = dict(real_deck, intentionallySkippedCardData=True, cards=[])
    with pytest.raises(ValueError, match="intentionallySkippedCardData"):
        ac.fetch_deck("24864897", fetch_json=lambda url: skipped)

    # ...and the healthy capture still passes straight through.
    out = ac.fetch_deck("24864897", fetch_json=lambda url: real_deck)
    assert out["id"] == 24864897


def test_to_deck_json_warns_about_entries_it_cannot_name(real_deck,
                                                        capsys):
    """Nameless entries are unrenderable and get dropped; dropping them
    SILENTLY turns 'we lost cards' into a deck that merely looks short."""
    real_deck["cards"][0]["card"].pop("oracleCard")
    out = ac.to_deck_json(real_deck)
    assert len(out["boards"]["mainboard"]["cards"]) == 5
    err = capsys.readouterr().err
    assert "1 of 7 in-deck entries have no oracleCard name" in err


def test_primer_holds_the_same_captures_description_verbatim():
    """FP-016 Phase 1 keeps the deck's stated intent as a test case. Two
    things pinned: the primer really is THIS capture's description (the
    fixture's truncated copy is a prefix of it), and Archidekt's
    ``description`` is a Quill Delta JSON string, not prose — an intent
    reader that treats the field as text will read JSON punctuation."""
    primer = PRIMER_MD.read_text(encoding="utf-8")
    assert "24864897" in primer and "2026-08-20" in primer

    verbatim = primer.split("```json\n", 1)[1].split("\n```", 1)[0]
    delta = json.loads(verbatim)
    assert isinstance(delta["ops"], list) and delta["ops"]
    text = "".join(op["insert"] for op in delta["ops"])
    assert "Squirreled Away" in text

    fixture = json.loads(REAL_DECK_JSON.read_text(encoding="utf-8"))
    truncated = fixture["description"]
    assert "TRUNCATED" in truncated
    assert verbatim.startswith(truncated.split(" …[TRUNCATED", 1)[0])
    assert fixture["hasPrimer"] is True


# ---------------------------------------------------------------------------
# DOUBLE-FACED CARDS (R2-P18 follow-up, 2026-08-27) —
# archidekt.com/api/decks/5273595/ ("From Cute to Brute - Secret Lair
# Drop", 100 entries: 53 normal / 25 transform / 22 modal_dfc).
#
# The gap the 2026-08-20 capture pinned as OPEN, now closed with real
# data instead of a guess. Every entry read below is byte-for-byte
# verbatim from that response, so these tests fail the day Archidekt
# changes the DFC contract — which is the whole point, because the
# capture immediately found a live bug in ``_entry_name``.
# ---------------------------------------------------------------------------

def _oracle(deck: dict, name_startswith: str) -> dict:
    return next(e["card"]["oracleCard"] for e in deck["cards"]
                if e["card"]["oracleCard"]["name"].startswith(name_startswith))


def test_mdfc_fixture_is_a_capture_not_a_construction(mdfc_deck):
    """Guard the second fixture the same way as the first: it is
    evidence. Note what it must NOT be extended with by hand — the
    provenance names the deck, the fetch date and the entry count."""
    prov = mdfc_deck["_provenance"]
    assert "5273595" in prov["source"]
    assert prov["fetched"].startswith("2026-08-27")
    assert "VERBATIM" in prov["trimmed"]
    assert len(mdfc_deck["cards"]) == 6
    assert mdfc_deck["id"] == 5273595
    assert mdfc_deck["intentionallySkippedCardData"] is False
    assert mdfc_deck["customCards"] == []


def test_mdfc_name_is_the_joined_front_back_string(mdfc_deck):
    """THE SHAPE, pinned. A double-faced card's ``oracleCard.name`` is
    ``"Front // Back"`` — Archidekt does NOT hand back the front face as
    the name and the back somewhere else. Both DFC layouts in the deck
    behave identically."""
    esika = _oracle(mdfc_deck, "Esika")
    assert esika["layout"] == "modal_dfc"
    assert esika["name"] == "Esika, God of the Tree // The Prismatic Bridge"

    avacyn = _oracle(mdfc_deck, "Archangel Avacyn")
    assert avacyn["layout"] == "transform"
    assert avacyn["name"] == "Archangel Avacyn // Avacyn, the Purifier"

    layouts = {e["card"]["oracleCard"]["layout"] for e in mdfc_deck["cards"]}
    assert layouts == {"modal_dfc", "transform", "normal"}


def test_mdfc_faces_is_a_two_element_list_front_first(mdfc_deck):
    """``oracleCard.faces`` is populated for a DFC — 2 entries, front
    first, each a full face object with its own name/manaCost/types. That
    is what makes a face-based reduction possible at all; splitting the
    joined name on ``"//"`` is a fallback, not the mechanism."""
    bala = _oracle(mdfc_deck, "Bala Ged Recovery")
    assert [f["name"] for f in bala["faces"]] == [
        "Bala Ged Recovery", "Bala Ged Sanctuary"]
    assert bala["faces"][0]["types"] == ["Sorcery"]     # spell front...
    assert bala["faces"][1]["types"] == ["Land"]        # ...land back
    assert bala["faces"][0]["manaCost"] == "{2}{G}"

    # ...and the SAME response still carries faces: [] for plain cards.
    # The discriminator is the per-ENTRY layout, not the deck.
    sol_ring = _oracle(mdfc_deck, "Sol Ring")
    assert sol_ring["layout"] == "normal" and sol_ring["faces"] == []
    assert "//" not in sol_ring["name"]


def test_mdfc_commander_is_detected_by_category_not_by_layout(mdfc_deck):
    """This deck's commander is ITSELF a modal DFC (Esika, God of the
    Tree // The Prismatic Bridge): one entry that is both
    ``categories: ["Commander"]`` and ``layout: "modal_dfc"``. Commander
    detection is category-driven, so a two-faced commander must land in
    ``[Commander]`` — and, being a card line like any other, must carry
    the front-face name."""
    esika = next(e for e in mdfc_deck["cards"]
                 if e["card"]["oracleCard"]["name"].startswith("Esika"))
    assert esika["categories"] == ["Commander"]
    assert esika["card"]["oracleCard"]["layout"] == "modal_dfc"
    premier = [c["name"] for c in mdfc_deck["categories"] if c["isPremier"]]
    assert premier == ["Commander"]

    out = ac.to_deck_json(mdfc_deck)
    assert _names(out["boards"]["commanders"]) == ["Esika, God of the Tree"]
    # ...and it is not also in the 99.
    assert not any(n.startswith("Esika")
                   for n in _names(out["boards"]["mainboard"]))
    assert not any(n.startswith("Esika") for n in extract_mainboard(mdfc_deck))


def test_dfc_entries_adapt_to_the_front_face_name(mdfc_deck):
    """THE BUG THIS CAPTURE FOUND. ``_entry_name`` used to return
    ``oracleCard.name`` verbatim, so a DFC imported as
    ``"Bala Ged Recovery // Bala Ged Sanctuary"``. Forge — and every
    ``.dck`` reader in this project — names a DFC by its FRONT FACE:
    ``forge_cards_loader`` keeps a front-face-slug index precisely
    because ".dck files reference cards by front-face name only", and
    ``deck_health._MDFC_LANDS`` is keyed on the lowercase front face.

    Both layouts reduce; a ``normal`` entry is untouched."""
    names = extract_mainboard(mdfc_deck)
    assert names == [
        "Archangel Avacyn",     # transform
        "Bala Ged Recovery",    # modal_dfc, spell front
        "Branchloft Pathway",   # modal_dfc, land front
        "Sol Ring",             # normal — unchanged
        "Westvale Abbey",       # transform, LAND front face
    ]
    assert not any("//" in n for n in names)
    # The reduction is the FACE, not a prefix of the joined string: each
    # name equals faces[0].name exactly.
    for entry in mdfc_deck["cards"]:
        oracle = entry["card"]["oracleCard"]
        if oracle["faces"]:
            assert ac._entry_name(entry) == oracle["faces"][0]["name"]


def test_dfc_front_face_names_are_what_deck_health_counts(mdfc_deck):
    """Why the front face is not cosmetic: ``deck_health._MDFC_LANDS``
    (read through ``consistency`` and ``card_score.is_mdfc_land``) is a
    set of lowercase FRONT-face names matched against names parsed out of
    ``.dck`` lines. Pre-fix, this deck's modal lands scored ZERO."""
    from commander_builder.deck_health import _MDFC_LANDS

    names = extract_mainboard(mdfc_deck)
    assert "bala ged recovery" in _MDFC_LANDS
    assert "branchloft pathway" in _MDFC_LANDS
    matched = [n for n in names if n.lower() in _MDFC_LANDS]
    assert matched == ["Bala Ged Recovery", "Branchloft Pathway"]
    # The joined form — what the adapter used to emit — matches nothing.
    joined = [e["card"]["oracleCard"]["name"] for e in mdfc_deck["cards"]]
    assert not [n for n in joined if n.lower() in _MDFC_LANDS]


def test_mdfc_deck_renders_a_forge_dck_end_to_end(mdfc_deck):
    """The ``.dck`` emission form for DFC names, pinned end to end: front
    face plus the ``|SET|CN`` printing suffix, and NO ``//`` anywhere in
    the file — including on the ``[Commander]`` line, whose card is an
    MDFC in this deck."""
    from commander_builder.moxfield_import import to_dck

    dck = to_dck(ac.to_deck_json(mdfc_deck)).splitlines()
    assert dck[:2] == ["[metadata]",
                       "Name=From Cute to Brute - Secret Lair Drop"]
    assert dck[dck.index("[Commander]") + 1] == \
        "1 Esika, God of the Tree|SLD|1155"
    main = dck[dck.index("[Main]") + 1:]
    assert main == [
        "1 Archangel Avacyn|SLD|1156",
        "1 Bala Ged Recovery|PLST|ZNR-180",
        "1 Branchloft Pathway|PLST|ZNR-258",
        "1 Sol Ring|PLST|CMD-261",
        "1 Westvale Abbey|SLD|1159",
    ]
    assert not any("//" in line for line in dck)

    # dck_utils parses those lines back to the same names — the printing
    # suffix strips, the name survives whole.
    from commander_builder import dck_utils
    text = "\n".join(dck) + "\n"
    assert dck_utils.section_card_names(text, "Commander") == \
        ["Esika, God of the Tree"]
    assert dck_utils.section_card_names(text, "Main") == extract_mainboard(
        mdfc_deck)


def test_mdfc_capture_reconfirms_the_board_membership_flag(mdfc_deck):
    """Independent confirmation from a SECOND deck of the finding the
    first capture made: ``Sideboard`` carries ``includedInDeck: true``
    and ``Maybeboard`` false, so board membership is the flag, never the
    category's name. Two unrelated decks, same user state."""
    flags = {c["name"]: c["includedInDeck"] for c in mdfc_deck["categories"]}
    assert flags["Sideboard"] is True and flags["Maybeboard"] is False
    assert ac._excluded_categories(mdfc_deck) == {"maybeboard"}


def test_mdfc_corpus_and_importer_still_agree_on_membership(mdfc_deck):
    """One walk, one notion of 'part of the deck' — reasserted on the
    double-faced deck, since ``_entry_name`` is the shared seam that the
    front-face fix changed."""
    imported = _names(ac.to_deck_json(mdfc_deck)["boards"]["mainboard"])
    assert extract_mainboard(mdfc_deck) == imported


def test_two_faced_layout_with_empty_faces_falls_back_to_the_split():
    """SYNTHETIC drift path, and deliberately so: no healthy capture
    contains ``layout: "modal_dfc"`` with ``faces: []``. If Archidekt
    ever stops populating faces, a front face split off the joined name
    still resolves in Forge; the full string does not."""
    entry = {"quantity": 1, "categories": [], "card": {"oracleCard": {
        "name": "Bala Ged Recovery // Bala Ged Sanctuary",
        "layout": "modal_dfc", "faces": []}}}
    assert ac._entry_name(entry) == "Bala Ged Recovery"


def test_unpinned_two_faced_layouts_pass_through_untouched():
    """Scope guard. ``//`` is overloaded — EDHREC joins partner PAIRS
    with it and split/adventure cards carry it inside ONE card's real
    name — so the reduction is keyed on the layouts the capture proves,
    not on the separator. A layout no capture has shown keeps its name
    whole rather than being guessed at."""
    def one(layout, name, faces=()):
        return {"quantity": 1, "categories": [], "card": {"oracleCard": {
            "name": name, "layout": layout, "faces": list(faces)}}}

    split = one("split", "Fire // Ice",
                [{"name": "Fire"}, {"name": "Ice"}])
    assert ac._entry_name(split) == "Fire // Ice"
    adventure = one("adventure", "Bonecrusher Giant",
                    [{"name": "Bonecrusher Giant"}, {"name": "Stomp"}])
    assert ac._entry_name(adventure) == "Bonecrusher Giant"
    assert ac._TWO_FACED_LAYOUTS == {"transform", "modal_dfc"}


# ---------------------------------------------------------------------------
# FP-018.1 (2026-08-27) — `description` passes through the adapter RAW
# ---------------------------------------------------------------------------

def test_to_deck_json_passes_the_description_through_raw(real_deck):
    """The client stays a client: `to_deck_json` hands the Quill Delta
    string through verbatim (rendering is `primer`'s job, at the one
    place that decides whether a sidecar gets written)."""
    out = ac.to_deck_json(real_deck)
    assert out["description"] == real_deck["description"]
    # It really is the raw field, not a render.
    assert out["description"].startswith('{"ops"')


def test_to_deck_json_omits_an_empty_description(real_deck):
    """No description key when the deck has none, so consumers can key
    on plain truthiness (mirrors the bracket key's convention)."""
    for empty in (None, ""):
        deck = dict(real_deck, description=empty)
        assert "description" not in ac.to_deck_json(deck)
    deck = dict(real_deck)
    del deck["description"]
    assert "description" not in ac.to_deck_json(deck)
