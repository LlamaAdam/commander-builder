"""Tests for deck-health role-target ratios (F2)."""
from __future__ import annotations

import pytest

from commander_builder import staples, deck_health
from commander_builder.staples import ROLE_TARGETS, role_target_report


def test_role_target_report_flags_deficits(monkeypatch):
    # ramp 5/10, draw 12/10, removal 8/8, wipe 0/3, protection 0/4
    monkeypatch.setattr(staples, "count_deck_roles",
                        lambda names: {"ramp": 5, "draw": 12, "removal": 8, "wipe": 0})
    r = role_target_report(["x"])
    assert r["roles"]["ramp"]["deficit"] == 5
    assert r["roles"]["draw"]["deficit"] == 0      # over target → no deficit
    assert r["roles"]["removal"]["deficit"] == 0   # exactly at target
    assert r["roles"]["wipe"]["deficit"] == 3
    assert r["roles"]["protection"]["deficit"] == 4
    # under_built sorted worst-deficit first: ramp(5), protection(4), wipe(3)
    assert r["under_built"] == ["ramp", "protection", "wipe"]


def test_role_target_report_well_built(monkeypatch):
    monkeypatch.setattr(staples, "count_deck_roles",
                        lambda names: {"ramp": 11, "draw": 11, "removal": 9,
                                       "wipe": 4, "protection": 5})
    r = role_target_report(["x"])
    assert r["under_built"] == []
    assert all(v["deficit"] == 0 for v in r["roles"].values())


def test_role_targets_cover_expected_roles():
    assert set(ROLE_TARGETS) == {"ramp", "draw", "removal", "wipe", "protection"}


def test_deck_health_signal_wires_in(monkeypatch):
    monkeypatch.setattr("commander_builder.staples.count_deck_roles",
                        lambda names: {"removal": 3})
    sig = deck_health._role_targets_signal(
        "[Main]\n1 Swords to Plowshares\n1 Path to Exile\n")
    assert sig["roles"]["removal"]["deficit"] == 5  # 3 vs target 8
    assert "removal" in sig["under_built"]


def test_deck_health_signal_degrades_on_error(monkeypatch):
    def boom(names):
        raise RuntimeError("scryfall down")
    monkeypatch.setattr("commander_builder.staples.count_deck_roles", boom)
    sig = deck_health._role_targets_signal("[Main]\n1 Sol Ring\n")
    assert sig == {"roles": {}, "under_built": []}
