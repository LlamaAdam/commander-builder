"""Tests for edhtop16_client (FP-017) — fully offline via injected fetch_json.

No live network anywhere in this file: every call passes a stub
``fetch_json(url, payload)`` and the cache is redirected to tmp_path.
"""

from __future__ import annotations

import email.message
import json
import time
import urllib.error

import pytest

import commander_builder.edhtop16_client as et
from commander_builder.edhtop16_client import (
    CEDH_BRACKET,
    CommanderStats,
    EdhTop16Error,
    TournamentEntry,
    card_presence,
    fetch_card_stats,
    fetch_commander_entries,
    fetch_top_commanders,
    fetch_top_decklists,
    load_cached_card_stats,
    main,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own cache dir — never the repo's."""
    monkeypatch.setattr(et, "_cache_dir", lambda: tmp_path / "edhtop16")
    return tmp_path / "edhtop16"


# ---------------------------------------------------------------------------
# Payload builders (shapes copied from the live probe, 2026-08-05)
# ---------------------------------------------------------------------------

def commander_node(name="Kinnan, Bonder Prodigy", color="UG", count=3421,
                   conv=0.2668, wr=0.2108, cuts=913, share=0.0319):
    return {
        "name": name,
        "colorId": color,
        "stats": {"count": count, "conversionRate": conv, "winRate": wr,
                  "topCuts": cuts, "metaShare": share},
    }


def entry_node(standing=1, wins=5, losses=0, draws=3, wr=0.625,
               cards=("Sol Ring", "Mana Crypt"), top_cut=16, size=125,
               name="CCS Invitational", tid="tid-1", date="2026-05-01"):
    return {
        "standing": standing, "wins": wins, "losses": losses, "draws": draws,
        "winRate": wr,
        "decklist": "https://topdeck.gg/deck/x/y",
        "maindeck": [{"name": c} for c in cards],
        "tournament": {"name": name, "TID": tid, "size": size,
                       "tournamentDate": date, "topCut": top_cut},
    }


def conn(nodes):
    return {"edges": [{"node": n} for n in nodes]}


def make_fetcher(response, record=None):
    """Stub seam. ``response`` is a dict or a callable(payload) -> dict."""
    def fetch(url, payload):
        if record is not None:
            record.append((url, payload))
        return response(payload) if callable(response) else response
    fetch.calls = record if record is not None else []
    return fetch


# ---------------------------------------------------------------------------
# Parse correctness
# ---------------------------------------------------------------------------

def test_top_commanders_parses_stats():
    fetch = make_fetcher({"data": {"commanders": conn([
        commander_node(), commander_node(name="Sami, Wildcat Captain",
                                         color="WR", count=45, conv=0.3777)])}})
    rows = fetch_top_commanders(n=2, fetch_json=fetch)
    assert [r.name for r in rows] == ["Kinnan, Bonder Prodigy",
                                      "Sami, Wildcat Captain"]
    assert rows[0].color_id == "UG"
    assert rows[0].entries == 3421
    assert rows[0].top_cuts == 913
    assert rows[0].conversion_rate == pytest.approx(0.2668)
    assert rows[0].win_rate == pytest.approx(0.2108)
    assert rows[0].meta_share == pytest.approx(0.0319)


def test_top_commanders_sends_the_required_stats_filter():
    # The live API rejects a bare `stats` — the filters arg is REQUIRED.
    calls = []
    fetch = make_fetcher({"data": {"commanders": conn([commander_node()])}},
                         record=calls)
    fetch_top_commanders(n=1, fetch_json=fetch)
    q = calls[0][1]["query"]
    assert "stats(filters: {timePeriod: $tp})" in q
    assert calls[0][1]["variables"]["sortBy"] == "CONVERSION"
    assert calls[0][0] == et.ENDPOINT


def test_top_commanders_skips_nameless_nodes_not_the_source():
    fetch = make_fetcher({"data": {"commanders": conn(
        [{"name": "", "stats": {}}, commander_node()])}})
    assert [r.name for r in fetch_top_commanders(n=5, fetch_json=fetch)] == [
        "Kinnan, Bonder Prodigy"]


def test_top_commanders_rejects_bad_enum_values():
    with pytest.raises(ValueError):
        fetch_top_commanders(sort_by="NOPE")
    with pytest.raises(ValueError):
        fetch_top_commanders(time_period="YESTERDAY")


def test_top_commanders_zero_n_makes_no_request():
    fetch = make_fetcher(lambda p: pytest.fail("must not fetch"))
    assert fetch_top_commanders(n=0, fetch_json=fetch) == []


def test_entries_parses_stats_and_decklists():
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([
            entry_node(cards=("Sol Ring", "Mana Crypt", "Force of Will")),
            entry_node(standing=9, wins=4, wr=0.5, cards=("Sol Ring",)),
        ]))}})
    stats, entries = fetch_commander_entries("Kinnan, Bonder Prodigy",
                                             fetch_json=fetch)
    assert isinstance(stats, CommanderStats) and stats.entries == 3421
    assert len(entries) == 2
    assert entries[0].maindeck == ("Sol Ring", "Mana Crypt", "Force of Will")
    assert entries[0].standing == 1
    assert entries[0].wins == 5 and entries[0].draws == 3
    assert entries[0].tournament_size == 125
    assert entries[0].tournament_date == "2026-05-01"
    assert entries[0].decklist_url.startswith("https://topdeck.gg/")


def test_entries_query_carries_both_required_filter_fields():
    # EntriesFilter REQUIRES timePeriod AND minEventSize (live-verified).
    calls = []
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}}, record=calls)
    fetch_commander_entries("X", n=3, min_event_size=64, fetch_json=fetch)
    v = calls[0][1]["variables"]
    assert v["minEventSize"] == 64 and v["tp"] == "SIX_MONTHS"
    assert v["name"] == "X" and v["n"] == 3
    assert "minEventSize: $minEventSize" in calls[0][1]["query"]


def test_entry_without_a_decklist_is_dropped_not_counted_as_empty():
    # A recorded finish with an unpublished list would otherwise deflate
    # every presence rate by sitting in the denominator as a 0-card deck.
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([
            entry_node(cards=()), entry_node(cards=("Sol Ring",))]))}})
    _stats, entries = fetch_commander_entries("X", fetch_json=fetch)
    assert len(entries) == 1


def test_made_top_cut_reads_false_when_unknown():
    assert TournamentEntry(standing=3, top_cut=16).made_top_cut is True
    assert TournamentEntry(standing=30, top_cut=16).made_top_cut is False
    assert TournamentEntry(standing=None, top_cut=16).made_top_cut is False
    assert TournamentEntry(standing=1, top_cut=None).made_top_cut is False


def test_unknown_commander_degrades_to_empty(capsys):
    # The live API's actual shape: HTTP 200 + errors + data.commander null.
    fetch = make_fetcher({"errors": [{"message": "Commander not found"}],
                          "data": {"commander": None}})
    stats, entries = fetch_commander_entries("Nope", fetch_json=fetch)
    assert (stats, entries) == (None, [])
    assert "unavailable" in capsys.readouterr().err


def test_graphql_errors_raise_edhtop16_error():
    with pytest.raises(EdhTop16Error):
        et._graphql("{x}", {},
                    fetch_json=make_fetcher({"errors": ["boom"]}))
    with pytest.raises(EdhTop16Error):
        et._graphql("{x}", {}, fetch_json=make_fetcher({"noData": 1}))


# ---------------------------------------------------------------------------
# NO-CACHE-ON-EMPTY (the hard-won convention)
# ---------------------------------------------------------------------------

def test_empty_commander_list_is_not_cached(isolated_cache, capsys):
    fetch = make_fetcher({"data": {"commanders": conn([])}})
    assert fetch_top_commanders(n=5, fetch_json=fetch) == []
    assert "WARNING" in capsys.readouterr().err
    assert not list(isolated_cache.glob("*.json"))


def test_empty_entries_are_not_cached(isolated_cache, capsys):
    # Stats present but zero decklists: caching that would serve a source
    # that LOOKS present and contributes nothing for a full TTL.
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([]))}})
    stats, entries = fetch_commander_entries("Kinnan", fetch_json=fetch)
    assert entries == [] and stats is not None
    assert "WARNING" in capsys.readouterr().err
    assert not list(isolated_cache.glob("*.json"))


def test_non_empty_result_is_cached_and_reused(isolated_cache):
    calls = []
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}}, record=calls)
    fetch_commander_entries("Kinnan", fetch_json=fetch)
    assert len(calls) == 1
    assert list(isolated_cache.glob("*.json"))

    def boom(url, payload):
        pytest.fail("second call must be served from cache")

    stats, entries = fetch_commander_entries("Kinnan", fetch_json=boom)
    assert stats.name == "Kinnan, Bonder Prodigy"
    assert entries[0].maindeck == ("Sol Ring", "Mana Crypt")


def test_cache_can_be_disabled(isolated_cache):
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}})
    fetch_commander_entries("Kinnan", cache=False, fetch_json=fetch)
    assert not list(isolated_cache.glob("*.json"))


def _age_cache(cache_dir, hours):
    """Backdate the cache files. Explicit clock movement, never a sleep —
    and never `ttl_hours=0`, which races filesystem timestamp
    granularity (a file written this instant can look 0.0h old or a hair
    in the future)."""
    import os
    old = time.time() - hours * 3600
    for f in cache_dir.iterdir():
        os.utime(f, (old, old))


def test_stale_cache_is_refetched(isolated_cache):
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}})
    fetch_commander_entries("Kinnan", fetch_json=fetch)
    _age_cache(isolated_cache, et.CACHE_TTL_HOURS + 1)
    calls = []
    fetch2 = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}}, record=calls)
    fetch_commander_entries("Kinnan", fetch_json=fetch2)
    assert len(calls) == 1


def test_corrupt_cache_refetches(isolated_cache):
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}})
    fetch_commander_entries("Kinnan", fetch_json=fetch)
    for p in isolated_cache.glob("*.json"):
        p.write_text("{not json", encoding="utf-8")
    calls = []
    fetch2 = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node()]))}}, record=calls)
    _s, entries = fetch_commander_entries("Kinnan", fetch_json=fetch2)
    assert len(calls) == 1 and entries


def test_round_trip_through_cache_preserves_records(isolated_cache):
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([entry_node(standing=4, top_cut=16)]))}})
    fetch_commander_entries("Kinnan", fetch_json=fetch)
    raw = json.loads(next(isolated_cache.glob("*.json")).read_text("utf-8"))
    e = TournamentEntry.from_dict(raw["entries"][0])
    assert e.made_top_cut is True and e.tournament_id == "tid-1"
    assert CommanderStats.from_dict(raw["stats"]).top_cuts == 913


# ---------------------------------------------------------------------------
# 429 retry / backoff  (PR #40 pattern)
# ---------------------------------------------------------------------------

def http_error(code, retry_after=None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "err", hdrs, None)


def test_429_retried_honoring_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(et.time, "sleep", sleeps.append)
    state = {"n": 0}

    def fetch(url, payload):
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(429, retry_after="7")
        return {"data": {"commanders": conn([commander_node()])}}

    assert len(fetch_top_commanders(n=1, fetch_json=fetch)) == 1
    assert sleeps == [7.0]  # server hint honored, not the exp curve


def test_retry_after_is_clamped(monkeypatch):
    from commander_builder.edhrec_client import MAX_RETRY_AFTER_SEC
    sleeps = []
    monkeypatch.setattr(et.time, "sleep", sleeps.append)
    state = {"n": 0}

    def fetch(url, payload):
        state["n"] += 1
        if state["n"] == 1:
            raise http_error(503, retry_after="9999")
        return {"data": {"commanders": conn([commander_node()])}}

    fetch_top_commanders(n=1, fetch_json=fetch)
    assert sleeps == [MAX_RETRY_AFTER_SEC]


def test_429_exhausted_degrades_to_empty(monkeypatch, capsys):
    sleeps = []
    monkeypatch.setattr(et.time, "sleep", sleeps.append)
    calls = []

    def fetch(url, payload):
        calls.append(url)
        raise http_error(429)

    assert fetch_top_commanders(n=1, fetch_json=fetch) == []
    assert len(calls) == et.MAX_RETRIES + 1
    assert sleeps == [et.RETRY_BASE_DELAY_SEC * (2 ** a)
                      for a in range(et.MAX_RETRIES)]
    assert "degrading" in capsys.readouterr().err


def test_deterministic_4xx_not_retried(monkeypatch):
    monkeypatch.setattr(
        et.time, "sleep",
        lambda s: pytest.fail("must not back off on a deterministic 4xx"))
    calls = []

    def fetch(url, payload):
        calls.append(url)
        raise http_error(404)

    assert fetch_commander_entries("X", fetch_json=fetch) == (None, [])
    assert len(calls) == 1


def test_network_level_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(
        et.time, "sleep",
        lambda s: pytest.fail("a dead network must not multiply into sleeps"))
    calls = []

    def fetch(url, payload):
        calls.append(url)
        raise urllib.error.URLError("no route to host")

    assert fetch_top_commanders(n=1, fetch_json=fetch) == []
    assert len(calls) == 1


def test_transient_failure_is_never_cached(isolated_cache, monkeypatch):
    monkeypatch.setattr(et.time, "sleep", lambda s: None)

    def fetch(url, payload):
        raise http_error(503)

    fetch_commander_entries("Kinnan", fetch_json=fetch)
    assert not list(isolated_cache.glob("*.json"))


# ---------------------------------------------------------------------------
# card_presence (pure)
# ---------------------------------------------------------------------------

def ent(cards, wr=0.5, standing=1, top_cut=16):
    return TournamentEntry(standing=standing, win_rate=wr, top_cut=top_cut,
                           maindeck=tuple(cards))


def test_card_presence_counts_and_rates():
    entries = [ent(["Sol Ring", "Mana Crypt"], wr=0.6),
               ent(["Sol Ring"], wr=0.4)]
    stats = card_presence(entries, min_entries=1)
    assert stats["sol ring"].entries == 2
    assert stats["sol ring"].presence == pytest.approx(1.0)
    assert stats["sol ring"].mean_entry_win_rate == pytest.approx(0.5)
    assert stats["mana crypt"].presence == pytest.approx(0.5)
    assert stats["mana crypt"].mean_entry_win_rate == pytest.approx(0.6)
    assert stats["sol ring"].name == "Sol Ring"  # display name preserved


def test_card_presence_dedupes_within_one_entry():
    stats = card_presence([ent(["Sol Ring", "sol ring"])], min_entries=1)
    assert stats["sol ring"].entries == 1
    assert stats["sol ring"].presence == pytest.approx(1.0)


def test_card_presence_top_cut_split():
    entries = [ent(["Sol Ring"], standing=1, top_cut=16),
               ent(["Sol Ring", "Wild Growth"], standing=40, top_cut=16)]
    stats = card_presence(entries, min_entries=1)
    assert stats["sol ring"].top_cut_presence == pytest.approx(1.0)
    assert stats["wild growth"].top_cut_presence == pytest.approx(0.0)


def test_card_presence_top_cut_presence_none_when_no_top_cuts():
    stats = card_presence([ent(["Sol Ring"], standing=None, top_cut=None)],
                          min_entries=1)
    assert stats["sol ring"].top_cut_presence is None


def test_card_presence_withheld_below_the_floor():
    # "We don't know" must never render as "nobody plays it".
    entries = [ent(["Sol Ring"]) for _ in range(et.MIN_ENTRIES_FOR_PRESENCE - 1)]
    assert card_presence(entries) == {}
    entries.append(ent(["Sol Ring"]))
    assert card_presence(entries)["sol ring"].total_entries == \
        et.MIN_ENTRIES_FOR_PRESENCE


def test_card_presence_of_nothing_is_empty():
    assert card_presence([]) == {}
    assert card_presence(None) == {}


def test_fetch_card_stats_end_to_end():
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn(
            [entry_node(cards=("Sol Ring", "Mana Crypt")) for _ in range(4)]
            + [entry_node(cards=("Sol Ring",)) for _ in range(4)]))}})
    stats = fetch_card_stats("Kinnan", fetch_json=fetch)
    assert stats["sol ring"].presence == pytest.approx(1.0)
    assert stats["mana crypt"].presence == pytest.approx(0.5)


def test_load_cached_card_stats_never_fetches(isolated_cache, monkeypatch):
    monkeypatch.setattr(et, "_http_post_json", lambda u, p: pytest.fail(
        "the cache-only entry must never touch the network"))
    assert load_cached_card_stats("Kinnan") == {}
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn(
            [entry_node(cards=("Sol Ring",)) for _ in range(8)]))}})
    fetch_commander_entries("Kinnan", fetch_json=fetch)
    assert load_cached_card_stats("Kinnan")["sol ring"].entries == 8


# ---------------------------------------------------------------------------
# Bracket-5 gate (client side)
# ---------------------------------------------------------------------------

def test_top_decklists_refuses_non_bracket5_without_a_request():
    fetch = make_fetcher(lambda p: pytest.fail("must not fetch off-bracket"))
    for bracket in (None, 1, 2, 3, 4, 6, "5"):
        assert fetch_top_decklists("Kinnan", bracket=bracket,
                                   fetch_json=fetch) == []


def test_top_decklists_returns_lists_at_bracket_5():
    fetch = make_fetcher({"data": {"commander": dict(
        commander_node(), entries=conn([
            entry_node(cards=("Sol Ring", "Mana Crypt")),
            entry_node(cards=("Sol Ring",))]))}})
    out = fetch_top_decklists("Kinnan", bracket=CEDH_BRACKET, fetch_json=fetch)
    assert out == [["Sol Ring", "Mana Crypt"], ["Sol Ring"]]


def test_cedh_bracket_constant_is_five():
    assert CEDH_BRACKET == 5


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_prints_scope_note_and_cards(monkeypatch, capsys):
    monkeypatch.setattr(et, "_http_post_json", lambda u, p: {
        "data": {"commander": dict(commander_node(), entries=conn(
            [entry_node(cards=("Sol Ring", "Mana Crypt"))
             for _ in range(8)]))}})
    assert main(["Kinnan, Bonder Prodigy", "--no-cache"]) == 0
    out = capsys.readouterr().out
    assert "NOT a validated predictor" in out
    assert "BRACKET-5" in out
    assert "Sol Ring" in out


def test_cli_json_mode_carries_the_scope_note(monkeypatch, capsys):
    monkeypatch.setattr(et, "_http_post_json", lambda u, p: {
        "data": {"commanders": conn([commander_node()])}})
    assert main(["--json", "--no-cache"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "scope_note" in payload and "BRACKET-5" in payload["scope_note"]
    assert payload["commanders"][0]["name"] == "Kinnan, Bonder Prodigy"


def test_cli_reports_failure_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(et, "_http_post_json",
                        lambda u, p: {"data": {"commanders": conn([])}})
    assert main(["--no-cache"]) == 1
