"""Price-snapshot freshness for the pricing payload (P19).

``deck_pricing`` renders dollar figures out of ``lookup_card``
snapshots, and that store has NO TTL — a snapshot written six months
ago is served forever, so a stale ``$3.20`` is pixel-identical to one
fetched a minute ago. Legality already solved this class of bug by
reporting ``data_age_days`` next to its verdict; pricing shipped
without any equivalent signal.

These tests pin that the pricing payload now carries the SAME reading
— age of the oldest snapshot behind the quoted cards, plus a stale
flag past ``deck_legality.STALE_SNAPSHOT_DAYS`` — computed by REUSING
``deck_legality.snapshot_staleness`` / ``oracle_store.snapshot_age_days``
rather than a second private implementation that could drift.

Offline-only: ``lookup_card`` / ``lookup_card_prints`` are fakes and
snapshot ages are injected at ``oracle_store.snapshot_age_days``, so
nothing touches Scryfall or the real snapshot store.

(Cheaper-printing suggestion logic itself — thresholds, printing
legality, the offline breaker — is covered by test_printing_savings.py.)
"""
from __future__ import annotations

import pytest

from commander_builder.deck_legality import STALE_SNAPSHOT_DAYS
from commander_builder.web.deck_pricing import (
    price_snapshot_staleness,
    printing_savings_for_deck_text,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _deck(*lines: str) -> str:
    return "[Main]\n" + "\n".join(lines) + "\n"


def _patch_prices(monkeypatch):
    """One expensive card with a much cheaper legal printing, so the
    payload always has a suggestion to hang a staleness reading on."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {
            "type_line": "Instant", "set": "cur", "prices": {"usd": "10.00"},
        },
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints",
        lambda name, **_: [{
            "set": "chp", "set_type": "expansion", "collector_number": "42",
            "border_color": "black", "oversized": False, "digital": False,
            "prices": {"usd": "2.00"},
            "legalities": {"commander": "legal"},
        }],
    )


def _patch_ages(monkeypatch, ages: dict[str, float | None]):
    """Inject per-card snapshot ages at the shared source of truth."""
    monkeypatch.setattr(
        "commander_builder.oracle_store.snapshot_age_days",
        lambda name: ages.get(name),
    )


# ---------------------------------------------------------------------------
# price_snapshot_staleness
# ---------------------------------------------------------------------------

def test_reports_the_oldest_snapshot_age(monkeypatch):
    """The oldest reading is the honest one — a single ancient snapshot
    is exactly where a stale price hides, same rule legality uses."""
    _patch_ages(monkeypatch, {"A": 1.0, "B": 90.0, "C": 30.0})
    age, stale = price_snapshot_staleness(["A", "B", "C"])
    assert age == 90.0
    assert stale is True


def test_fresh_snapshots_are_not_stale(monkeypatch):
    _patch_ages(monkeypatch, {"A": 2.0, "B": 3.0})
    assert price_snapshot_staleness(["A", "B"]) == (3.0, False)


def test_threshold_is_the_shared_legality_constant(monkeypatch):
    """The 45-day line is IMPORTED, not re-declared — patching the
    legality constant must move the pricing verdict with it. If this
    ever fails, the two staleness readings have forked and a user can
    see 'legality 60 days old' beside fresh-looking prices."""
    _patch_ages(monkeypatch, {"A": 20.0})
    assert price_snapshot_staleness(["A"])[1] is False
    monkeypatch.setattr(
        "commander_builder.deck_legality.STALE_SNAPSHOT_DAYS", 10.0,
    )
    assert price_snapshot_staleness(["A"])[1] is True


def test_exactly_at_the_threshold_is_stale(monkeypatch):
    _patch_ages(monkeypatch, {"A": STALE_SNAPSHOT_DAYS})
    assert price_snapshot_staleness(["A"]) == (STALE_SNAPSHOT_DAYS, True)
    _patch_ages(monkeypatch, {"A": STALE_SNAPSHOT_DAYS - 0.1})
    assert price_snapshot_staleness(["A"])[1] is False


def test_unknown_age_is_not_stale(monkeypatch):
    """Nothing on disk (cold store, hermetic test) → unknown age. That
    is not evidence of staleness, so it must not raise a flag."""
    _patch_ages(monkeypatch, {})
    assert price_snapshot_staleness(["A", "B"]) == (None, False)


def test_never_raises_when_the_store_misbehaves(monkeypatch):
    """Freshness is a bonus on a payload that has to render offline."""
    def boom(name):
        raise OSError("snapshot store unreadable")
    monkeypatch.setattr(
        "commander_builder.oracle_store.snapshot_age_days", boom,
    )
    assert price_snapshot_staleness(["A"]) == (None, False)


# ---------------------------------------------------------------------------
# printing_savings_for_deck_text payload
# ---------------------------------------------------------------------------

def test_payload_carries_age_and_stale_flag(monkeypatch):
    _patch_prices(monkeypatch)
    _patch_ages(monkeypatch, {"Pricey": 100.0})
    out = printing_savings_for_deck_text(_deck("1 Pricey"))
    assert out["count"] == 1
    assert out["price_data_age_days"] == 100.0
    assert out["price_data_stale"] is True


def test_payload_flags_fresh_prices_as_not_stale(monkeypatch):
    _patch_prices(monkeypatch)
    _patch_ages(monkeypatch, {"Pricey": 3.0})
    out = printing_savings_for_deck_text(_deck("1 Pricey"))
    assert out["price_data_age_days"] == 3.0
    assert out["price_data_stale"] is False


def test_payload_ages_the_oldest_quoted_card(monkeypatch):
    """The reading covers every card whose price the payload quotes,
    not just the first one."""
    _patch_prices(monkeypatch)
    _patch_ages(monkeypatch, {"Alpha": 1.0, "Bravo": 77.0})
    out = printing_savings_for_deck_text(_deck("1 Alpha", "1 Bravo"))
    assert {s["card"] for s in out["suggestions"]} == {"Alpha", "Bravo"}
    assert out["price_data_age_days"] == 77.0
    assert out["price_data_stale"] is True


def test_new_keys_are_purely_additive(monkeypatch):
    """The historical three keys keep their exact meaning — a client
    that ignores freshness reads the same numbers it always did."""
    _patch_prices(monkeypatch)
    _patch_ages(monkeypatch, {"Pricey": 100.0})
    out = printing_savings_for_deck_text(_deck("2 Pricey"))
    assert out["total"] == 16.0
    assert out["count"] == 1
    assert out["suggestions"][0]["card"] == "Pricey"
    assert out["suggestions"][0]["savings"] == 16.0


def test_uncached_prices_report_unknown_age(monkeypatch):
    _patch_prices(monkeypatch)
    _patch_ages(monkeypatch, {})
    out = printing_savings_for_deck_text(_deck("1 Pricey"))
    assert out["price_data_age_days"] is None
    assert out["price_data_stale"] is False


def test_no_suggestions_reports_unknown_age_not_a_fresh_one(monkeypatch):
    """Freshness describes the prices in ``suggestions``. With none
    quoted, no snapshot was consulted — the payload must say "unknown"
    rather than fabricate a fresh-looking zero."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, **_: {
            "type_line": "Instant", "set": "cur", "prices": {"usd": "10.00"},
        },
    )
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card_prints",
        lambda name, **_: None,
    )
    _patch_ages(monkeypatch, {"Pricey": 100.0})
    assert printing_savings_for_deck_text(_deck("1 Pricey")) == {
        "total": 0.0, "count": 0, "suggestions": [],
        "price_data_age_days": None, "price_data_stale": False,
    }


def test_payload_survives_a_broken_snapshot_store(monkeypatch):
    """A freshness failure must never cost the user their savings
    list — the dashboard tile renders either way."""
    _patch_prices(monkeypatch)

    def boom(name):
        raise OSError("snapshot store unreadable")
    monkeypatch.setattr(
        "commander_builder.oracle_store.snapshot_age_days", boom,
    )
    out = printing_savings_for_deck_text(_deck("1 Pricey"))
    assert out["count"] == 1
    assert out["price_data_age_days"] is None
    assert out["price_data_stale"] is False
