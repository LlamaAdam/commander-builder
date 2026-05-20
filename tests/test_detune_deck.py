"""Tests for scripts/detune_deck.py -- the deck-weakening tool used to
manufacture positive ('kept') FP-002 training examples.

Pure-logic tests (no Forge, no DB). load_game_changers is mocked so the
tests are offline-deterministic and don't depend on the WotC cache.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import detune_deck  # noqa: E402


_DECK = """[metadata]
Name=T
[Commander]
1 Cmdr|S|1
[Main]
1 Sol Ring|C|1
1 Cyclonic Rift|C|2
1 Lightning Bolt|C|3
1 Random Creature|C|4
9 Forest|J|1
9 Island|J|2
"""


def _main_total(text: str) -> int:
    total, in_main = 0, False
    for ln in text.splitlines():
        s = ln.strip()
        if s.lower() == "[main]":
            in_main = True
            continue
        if s.startswith("["):
            in_main = False
            continue
        if in_main and s:
            m = re.match(r"^(\d+)\s", s)
            total += int(m.group(1)) if m else 1
    return total


def test_detune_preserves_card_count(monkeypatch):
    monkeypatch.setattr(detune_deck, "load_game_changers",
                        lambda: {"Sol Ring", "Cyclonic Rift"})
    before = _main_total(_DECK)  # 1+1+1+1+9+9 = 22
    out, removed, added = detune_deck.detune(_DECK, n=2, seed=1)
    assert len(removed) == 2 and added == 2
    assert _main_total(out) == before  # remove N cards, add N basics


def test_detune_removes_game_changers_first(monkeypatch):
    monkeypatch.setattr(detune_deck, "load_game_changers",
                        lambda: {"Sol Ring", "Cyclonic Rift"})
    _out, removed, _added = detune_deck.detune(_DECK, n=2, seed=7)
    # Both game-changers are removed before any non-GC card.
    assert set(removed) == {"Sol Ring", "Cyclonic Rift"}


def test_detune_is_seeded_deterministic(monkeypatch):
    monkeypatch.setattr(detune_deck, "load_game_changers", lambda: set())
    _o1, r1, _ = detune_deck.detune(_DECK, n=2, seed=42)
    _o2, r2, _ = detune_deck.detune(_DECK, n=2, seed=42)
    assert r1 == r2


def test_detune_never_removes_basics(monkeypatch):
    monkeypatch.setattr(detune_deck, "load_game_changers", lambda: set())
    _out, removed, _added = detune_deck.detune(_DECK, n=3, seed=3)
    assert not ({"Forest", "Island"} & set(removed))


def test_detune_raises_without_basics_to_pad(monkeypatch):
    monkeypatch.setattr(detune_deck, "load_game_changers", lambda: set())
    no_basics = "[Main]\n1 Sol Ring|C|1\n1 Lightning Bolt|C|2\n"
    with pytest.raises(ValueError):
        detune_deck.detune(no_basics, n=1, seed=1)
