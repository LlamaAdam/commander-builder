"""Tests for ``dck_meta`` — the two ``[metadata]`` invariants.

``Name=`` (filename↔win-attribution) has been exercised indirectly for a
long time by the deck writers that call it (test_snapshot_deck,
test_moxfield_import, test_web_app). This file is the direct home for the
``BracketUnverified=`` marker added 2026-08-20, plus the ``Name=``
behaviors the marker rides next to and must not disturb.
"""
from __future__ import annotations

from commander_builder import dck_meta, dck_utils


DECK = (
    "[metadata]\n"
    "Name=[USER] Gamma [B3]\n"
    "\n"
    "[Commander]\n"
    "1 Test Cmdr\n"
    "\n"
    "[Main]\n"
    "60 Forest\n"
    "39 Cultivate\n"
)


# ---------------------------------------------------------------------------
# read / set / clear round trip
# ---------------------------------------------------------------------------

def test_absent_marker_reads_none():
    assert dck_meta.read_bracket_unverified(DECK) is None
    assert dck_meta.read_bracket_unverified("") is None
    assert dck_meta.read_bracket_unverified(None) is None


def test_set_then_read_round_trips_the_bracket():
    out = dck_meta.set_bracket_unverified(DECK, 3)
    assert "BracketUnverified=3" in out
    assert dck_meta.read_bracket_unverified(out) == 3


def test_set_is_idempotent():
    """The editor round-trips the file through a textarea, so an already
    marked deck is re-marked on every save. Two markers would make
    "cleared" depend on which line a reader stopped at."""
    once = dck_meta.set_bracket_unverified(DECK, 3)
    twice = dck_meta.set_bracket_unverified(once, 3)
    assert twice.count("BracketUnverified=") == 1
    assert twice == once


def test_set_overwrites_a_different_bracket():
    out = dck_meta.set_bracket_unverified(
        dck_meta.set_bracket_unverified(DECK, 3), 4,
    )
    assert out.count("BracketUnverified=") == 1
    assert dck_meta.read_bracket_unverified(out) == 4


def test_clear_restores_the_original_text():
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    assert dck_meta.clear_bracket_unverified(marked) == DECK


def test_clear_removes_every_marker_line():
    """Hand-edited files (or a racing writer) can hold more than one."""
    doubled = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\nBracketUnverified=3\nBracketUnverified=4\n",
    )
    cleared = dck_meta.clear_bracket_unverified(doubled)
    assert "BracketUnverified" not in cleared
    assert cleared == DECK


def test_clear_is_a_no_op_without_a_marker():
    assert dck_meta.clear_bracket_unverified(DECK) == DECK


# ---------------------------------------------------------------------------
# Placement + parsing rules (the "Forge must still load this" contract)
# ---------------------------------------------------------------------------

def test_marker_lands_inside_the_metadata_block():
    """Placement rule shared with
    ``moxfield_import._insert_metadata_lines``: after the last metadata
    line, before the first card-section header. Outside the block the
    module's own reader would not see it."""
    out = dck_meta.set_bracket_unverified(DECK, 3)
    lines = out.splitlines()
    assert lines[0] == "[metadata]"
    assert lines[2] == "BracketUnverified=3"
    assert lines.index("BracketUnverified=3") < lines.index("[Commander]")


def test_marker_is_not_a_mainboard_change():
    """THE structural requirement: the PUT route decides "did the
    mainboard change?" with ``main_card_quantities``, and it does that on
    text it is about to stamp the marker into. If the marker registered
    as a card, setting it would make the next save look like an edit."""
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    assert (
        dck_utils.main_card_quantities(marked)
        == dck_utils.main_card_quantities(DECK)
    )
    assert dck_utils.count_main_cards(marked) == 99


def test_marker_does_not_disturb_other_metadata():
    """``Protect=`` / ``PoliticsGuard=`` / ``Moxfield=`` pass through
    byte-identical — the marker is additive, not a rewrite."""
    rich = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\n"
        "Moxfield=abc123\n"
        "Protect=Krenko, Mob Boss\n"
        "PoliticsGuard=off\n",
    )
    out = dck_meta.set_bracket_unverified(rich, 3)
    for line in ("Moxfield=abc123", "Protect=Krenko, Mob Boss",
                 "PoliticsGuard=off"):
        assert line in out
    assert dck_meta.clear_bracket_unverified(out) == rich


def test_name_stamp_and_marker_compose():
    """``rewrite_name`` runs first in the PUT path; the marker must not
    confuse it, and vice versa."""
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    renamed = dck_meta.rewrite_name(marked, "[USER] Delta [B3]")
    assert "Name=[USER] Delta [B3]" in renamed
    assert dck_meta.read_bracket_unverified(renamed) == 3


def test_key_is_case_insensitive():
    """Same rule ``read_protected_cards`` / ``politics_guard_enabled``
    enforce, so there is one [metadata] syntax to learn."""
    hand_typed = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\nbracketunverified=2\n",
    )
    assert dck_meta.read_bracket_unverified(hand_typed) == 2
    assert "bracketunverified" not in (
        dck_meta.clear_bracket_unverified(hand_typed)
    )


def test_marker_outside_the_metadata_block_is_ignored():
    """Only ``[metadata]`` is consulted — a line under [Main] is a deck
    comment, not a directive."""
    stray = DECK.replace("[Main]\n", "[Main]\nBracketUnverified=3\n")
    assert dck_meta.read_bracket_unverified(stray) is None


def test_unparseable_value_reads_as_absent():
    """The only use of the number is comparison against the filename's
    [B<n>] tag; a value that cannot be compared cannot distinguish a live
    marker from one a rename left behind."""
    for bad in ("", "yes", "0", "6", "3.5"):
        text = DECK.replace(
            "Name=[USER] Gamma [B3]\n",
            f"Name=[USER] Gamma [B3]\nBracketUnverified={bad}\n",
        )
        assert dck_meta.read_bracket_unverified(text) is None, bad


def test_deck_without_metadata_header_gets_one():
    """Mirrors ``rewrite_name``: a header-less file would otherwise get a
    directive nothing reads."""
    bare = "[Commander]\n1 Test Cmdr\n\n[Main]\n1 Forest\n"
    out = dck_meta.set_bracket_unverified(bare, 5)
    assert out.startswith("[metadata]\nBracketUnverified=5\n")
    assert dck_meta.read_bracket_unverified(out) == 5
