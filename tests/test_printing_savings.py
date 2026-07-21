"""Cheaper-printing savings (ManaFoundry parity) — deck_pricing unit tests.

Pure-offline: ``lookup_card`` / ``lookup_card_prints`` are monkeypatched
with multi-printing fakes. Covers the threshold edges ($1 floor, 30%
rule, >$1.00 gate), printing-legality exclusions (memorabilia, gold
border, not_legal), single-printing no-ops, aggregate math, the payload
shape, and the offline circuit breaker.
"""
from __future__ import annotations

import urllib.error

import pytest

from commander_builder.web.deck_pricing import (
    _printing_is_commander_legal,
    _printing_min_usd,
    printing_savings_for_deck_text,
)


# ---------------------------------------------------------------------------
# Fake-building helpers
# ---------------------------------------------------------------------------

def _card(usd, type_line="Instant", set_code="aaa"):
    return {
        "type_line": type_line,
        "set": set_code,
        "prices": {"usd": usd},
    }


def _printing(usd, set_code="bbb", collector="1", set_type="expansion",
              border="black", oversized=False, digital=False,
              commander="legal", usd_foil=None, usd_etched=None):
    return {
        "set": set_code,
        "set_name": set_code.upper(),
        "set_type": set_type,
        "collector_number": collector,
        "border_color": border,
        "oversized": oversized,
        "digital": digital,
        "prices": {"usd": usd, "usd_foil": usd_foil,
                   "usd_etched": usd_etched},
        "legalities": {"commander": commander},
    }


def _deck(*lines, commander=None):
    body = "[Main]\n" + "\n".join(lines) + "\n"
    if commander:
        body += "[Commander]\n" + commander + "\n"
    return body


def _patch(monkeypatch, cards: dict, prints: dict):
    """Point deck_pricing's scryfall imports at in-memory fakes."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: cards.get(name),
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints",
        lambda name, **_: prints.get(name),
    )


# ---------------------------------------------------------------------------
# Threshold edges
# ---------------------------------------------------------------------------

def test_card_at_exactly_one_dollar_is_skipped(monkeypatch):
    # "priced above $1.00" is a strict gate — $1.00 exactly stays out
    # even with a free reprint available.
    _patch(monkeypatch,
           {"Cheapo": _card("1.00")},
           {"Cheapo": [_printing("0.01")]})
    out = printing_savings_for_deck_text(_deck("1 Cheapo"))
    assert out == {"total": 0.0, "count": 0, "suggestions": []}


def test_one_dollar_floor_blocks_small_savings(monkeypatch):
    # $2.00 → $1.20 saves $0.80: over 30% would not matter, the $1
    # absolute floor rules.
    _patch(monkeypatch,
           {"Small": _card("2.00")},
           {"Small": [_printing("1.20")]})
    assert printing_savings_for_deck_text(_deck("1 Small"))["count"] == 0


def test_saving_exactly_at_floor_qualifies(monkeypatch):
    # $2.00 → $1.00 saves exactly $1.00 = max($1, $0.60) → inclusive
    # (spec says "saves >= max(...)").
    _patch(monkeypatch,
           {"Edge": _card("2.00")},
           {"Edge": [_printing("1.00")]})
    out = printing_savings_for_deck_text(_deck("1 Edge"))
    assert out["count"] == 1
    assert out["suggestions"][0]["savings"] == 1.0


def test_thirty_pct_rule_blocks_relative_small_savings(monkeypatch):
    # $10.00 → $8.50 saves $1.50: clears the $1 floor but not the 30%
    # rule (threshold $3.00) — nobody re-buys to shave 15%.
    _patch(monkeypatch,
           {"Pricey": _card("10.00")},
           {"Pricey": [_printing("8.50")]})
    assert printing_savings_for_deck_text(_deck("1 Pricey"))["count"] == 0


def test_saving_exactly_at_thirty_pct_qualifies(monkeypatch):
    # $10.00 → $7.00 saves $3.00 == 30% of current → inclusive edge.
    _patch(monkeypatch,
           {"Pricey": _card("10.00")},
           {"Pricey": [_printing("7.00")]})
    out = printing_savings_for_deck_text(_deck("1 Pricey"))
    assert out["count"] == 1
    assert out["suggestions"][0]["cheapest_price"] == 7.0


# ---------------------------------------------------------------------------
# Printing-legality exclusions
# ---------------------------------------------------------------------------

def test_memorabilia_printing_is_never_suggested(monkeypatch):
    # The World-Championship gold-border copy is the cheapest listing —
    # exactly the false positive the set_type/border filters exist for.
    _patch(monkeypatch,
           {"Staple": _card("20.00", set_code="lea")},
           {"Staple": [
               _printing("0.50", set_code="wc99", set_type="memorabilia"),
               _printing("1.00", set_code="wc00", border="gold"),
               _printing("12.00", set_code="rvr"),
           ]})
    out = printing_savings_for_deck_text(_deck("1 Staple"))
    assert out["count"] == 1
    s = out["suggestions"][0]
    # Skipped past both illegal printings to the real one.
    assert s["cheapest_set"] == "rvr"
    assert s["savings"] == 8.0


def test_not_legal_and_oversized_and_digital_excluded(monkeypatch):
    _patch(monkeypatch,
           {"Card": _card("10.00")},
           {"Card": [
               _printing("0.10", commander="not_legal"),
               _printing("0.20", oversized=True),
               _printing("0.30", digital=True),
           ]})
    # Every printing filtered out → no suggestion at all.
    assert printing_savings_for_deck_text(_deck("1 Card"))["count"] == 0


def test_helper_legality_predicate():
    assert _printing_is_commander_legal(_printing("1.00"))
    assert not _printing_is_commander_legal(
        _printing("1.00", set_type="memorabilia"))
    assert not _printing_is_commander_legal(_printing("1.00", border="gold"))
    assert not _printing_is_commander_legal(
        _printing("1.00", commander="banned"))
    # Missing legality map (old cached shape) → assume legal; the
    # printing-specific checks above are the real gate.
    p = _printing("1.00")
    p["legalities"] = {}
    assert _printing_is_commander_legal(p)


# ---------------------------------------------------------------------------
# No-suggestion cases
# ---------------------------------------------------------------------------

def test_single_printing_card_produces_no_suggestion(monkeypatch):
    # Only printing == the one you own: saving is 0, threshold filters
    # out the "swap it for itself" degenerate.
    _patch(monkeypatch,
           {"Unique": _card("15.00", set_code="one")},
           {"Unique": [_printing("15.00", set_code="one")]})
    assert printing_savings_for_deck_text(_deck("1 Unique"))["count"] == 0


def test_basic_lands_are_skipped(monkeypatch):
    # Even an absurdly priced premium Forest never gets a suggestion.
    _patch(monkeypatch,
           {"Forest": _card("25.00", type_line="Basic Land — Forest")},
           {"Forest": [_printing("0.10")]})
    assert printing_savings_for_deck_text(_deck("30 Forest"))["count"] == 0


def test_unpriced_or_unknown_cards_are_skipped(monkeypatch):
    _patch(monkeypatch,
           {"NoPrice": {"type_line": "Instant", "prices": {"usd": None}}},
           {"NoPrice": [_printing("0.10")]})
    deck = _deck("1 NoPrice", "1 TotallyUnknown")
    assert printing_savings_for_deck_text(deck)["count"] == 0


def test_printings_without_any_price_are_ignored(monkeypatch):
    _patch(monkeypatch,
           {"Card": _card("10.00")},
           {"Card": [_printing(None)]})
    assert printing_savings_for_deck_text(_deck("1 Card"))["count"] == 0


# ---------------------------------------------------------------------------
# Price-field handling
# ---------------------------------------------------------------------------

def test_foil_only_printing_price_counts(monkeypatch):
    # Promo printings often have usd=null + usd_foil set; a foil copy
    # is just as legal, so it must be eligible as the cheap option.
    _patch(monkeypatch,
           {"Promo": _card("10.00")},
           {"Promo": [_printing(None, usd_foil="2.50", set_code="pf")]})
    out = printing_savings_for_deck_text(_deck("1 Promo"))
    assert out["count"] == 1
    assert out["suggestions"][0]["cheapest_price"] == 2.5


def test_min_usd_takes_cheapest_finish():
    assert _printing_min_usd(
        _printing("5.00", usd_foil="3.00", usd_etched="4.00")) == 3.0
    assert _printing_min_usd(_printing(None)) is None


# ---------------------------------------------------------------------------
# Aggregation + payload shape
# ---------------------------------------------------------------------------

def test_aggregate_totals_count_and_ordering(monkeypatch):
    _patch(monkeypatch,
           {"Big": _card("40.00", set_code="big"),
            "Mid": _card("6.00", set_code="mid")},
           {"Big": [_printing("10.00", set_code="cheapbig", collector="9")],
            "Mid": [_printing("2.00", set_code="cheapmid")]})
    out = printing_savings_for_deck_text(_deck("1 Big", "1 Mid"))
    assert out["count"] == 2
    # Sorted biggest-saving first.
    assert [s["card"] for s in out["suggestions"]] == ["Big", "Mid"]
    assert out["suggestions"][0]["savings"] == 30.0
    assert out["suggestions"][1]["savings"] == 4.0
    assert out["total"] == pytest.approx(34.0)


def test_quantity_multiplies_savings(monkeypatch):
    # 4 copies (Relentless-Rats-style) → savings row covers all four.
    _patch(monkeypatch,
           {"Rats": _card("3.00")},
           {"Rats": [_printing("0.50")]})
    out = printing_savings_for_deck_text(_deck("4 Rats"))
    s = out["suggestions"][0]
    assert s["qty"] == 4
    assert s["savings"] == pytest.approx(10.0)   # (3.00 - 0.50) × 4
    assert out["total"] == pytest.approx(10.0)


def test_commander_section_is_included(monkeypatch):
    _patch(monkeypatch,
           {"Cmdr": _card("30.00", type_line="Legendary Creature")},
           {"Cmdr": [_printing("5.00")]})
    out = printing_savings_for_deck_text(_deck("1 Filler", commander="1 Cmdr"))
    assert out["count"] == 1
    assert out["suggestions"][0]["card"] == "Cmdr"


def test_suggestion_payload_shape(monkeypatch):
    _patch(monkeypatch,
           {"Card": _card("10.00", set_code="cur")},
           {"Card": [_printing("2.00", set_code="chp", collector="42")]})
    out = printing_savings_for_deck_text(_deck("1 Card"))
    assert set(out) == {"total", "count", "suggestions"}
    s = out["suggestions"][0]
    assert s == {
        "card": "Card",
        "qty": 1,
        "current_price": 10.0,
        "current_set": "cur",
        "cheapest_price": 2.0,
        "cheapest_set": "chp",
        "cheapest_collector": "42",
        "savings": 8.0,
    }


def test_empty_deck_text(monkeypatch):
    _patch(monkeypatch, {}, {})
    assert printing_savings_for_deck_text("") == {
        "total": 0.0, "count": 0, "suggestions": [],
    }


# ---------------------------------------------------------------------------
# Offline degradation + circuit breaker
# ---------------------------------------------------------------------------

def test_offline_with_nothing_cached_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: _card("10.00"),
    )

    def offline(name, **_):
        raise urllib.error.URLError("no network")
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints", offline,
    )
    out = printing_savings_for_deck_text(_deck("1 A", "1 B", "1 C"))
    assert out == {"total": 0.0, "count": 0, "suggestions": []}


def test_breaker_switches_to_cache_only_after_first_failure(monkeypatch):
    """After one network failure, every remaining lookup must pass
    cache_only=True — a fully-offline dashboard load must not eat a
    connect-timeout per expensive card."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: _card("10.00"),
    )
    calls: list[tuple[str, bool]] = []

    def flaky(name, cache_only=False, **_):
        calls.append((name, cache_only))
        if not cache_only:
            raise urllib.error.URLError("no network")
        # Cache still serves B — a mid-deck outage must not lose
        # already-snapshotted cards.
        if name == "B":
            return [_printing("2.00", set_code="cch")]
        return None
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints", flaky,
    )

    out = printing_savings_for_deck_text(_deck("1 A", "1 B", "1 C"))
    # A: network attempt (fails) + cache-only retry; B and C: cache-only
    # straight away. Exactly one non-cache_only call total.
    assert [c for c in calls if not c[1]] == [("A", False)]
    assert calls == [
        ("A", False), ("A", True), ("B", True), ("C", True),
    ]
    # B's cached printings still produced a suggestion.
    assert [s["card"] for s in out["suggestions"]] == ["B"]
    assert out["total"] == 8.0
