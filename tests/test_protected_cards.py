"""Tests for the per-deck protected-cards reader.

``[metadata] Protect=`` entries in a .dck file lock specific cards
against curator cuts. The reader lives in ``web/_helpers.py`` so
both the web ``/api/audit`` endpoint and the ``commander-auto-curate``
CLI can share the parse — no two sources of truth.

Pinned invariants:

1. **One card per Protect= line**, comma is literal. This way the most
   common case — protecting your commander — works naturally without
   quoting:

       Protect=Krenko, Mob Boss      → 1 card: "Krenko, Mob Boss"
       Protect=Jaya Ballard, Task Mage → 1 card

2. Compact form uses double quotes:

       Protect="Sol Ring", "Counterspell"  → 2 cards

   Quoted and bare lines can mix in the same .dck.

3. Order-preserving + case-insensitive dedup. The UI renders protected
   cards in the order the user wrote them.

4. Only [metadata] section is consulted.

5. Whitespace trimmed; empty entries dropped.
"""
from __future__ import annotations

from commander_builder.web._helpers import read_protected_cards


def test_returns_empty_when_no_protect_lines():
    """A standard .dck with no Protect= entries → empty list."""
    deck = (
        "[metadata]\nName=Test\nMoxfield=abc\n"
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Sol Ring\n"
    )
    assert read_protected_cards(deck) == []


def test_one_card_per_line_is_the_canonical_form():
    """Each Protect= line is a single card. Comma in the value is
    literal — this is the whole point of the rule, so commanders
    (almost always comma-named) work without quoting."""
    deck = (
        "[metadata]\n"
        "Protect=Krenko, Mob Boss\n"
        "Protect=Sol Ring\n"
        "Protect=Jaya Ballard, Task Mage\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == [
        "Krenko, Mob Boss", "Sol Ring", "Jaya Ballard, Task Mage",
    ]


def test_quoted_compact_form_splits_into_multiple_cards():
    """For no-comma names a user can compact a list on one line by
    quoting each entry. Quotes mark a card boundary; commas between
    quoted chunks are ignored."""
    deck = (
        "[metadata]\n"
        'Protect="Sol Ring", "Counterspell", "Lightning Bolt"\n'
        "[Main]\n"
    )
    assert read_protected_cards(deck) == [
        "Sol Ring", "Counterspell", "Lightning Bolt",
    ]


def test_quoted_form_handles_comma_named_cards():
    """The quoted compact form preserves commas inside each entry,
    so multi-card lines work for comma-named cards too."""
    deck = (
        "[metadata]\n"
        'Protect="Krenko, Mob Boss", "Goblin Lackey"\n'
        "[Main]\n"
    )
    assert read_protected_cards(deck) == [
        "Krenko, Mob Boss", "Goblin Lackey",
    ]


def test_bare_and_quoted_lines_can_mix():
    """A real .dck might have a mix of styles (e.g. a single line
    quoted, plus a few bare-form lines). Both forms parse on the
    same deck."""
    deck = (
        "[metadata]\n"
        "Protect=Krenko, Mob Boss\n"
        'Protect="Sol Ring", "Brainstorm"\n'
        "Protect=Jaya Ballard, Task Mage\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == [
        "Krenko, Mob Boss", "Sol Ring", "Brainstorm",
        "Jaya Ballard, Task Mage",
    ]


def test_duplicates_collapse_case_insensitive():
    """Same card listed twice (or with different casing) collapses
    to one entry. Order matches first appearance."""
    deck = (
        "[metadata]\n"
        "Protect=Sol Ring\n"
        "Protect=Cultivate\n"
        "Protect=SOL RING\n"
        "Protect=cultivate\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == ["Sol Ring", "Cultivate"]


def test_preserves_casing_from_first_entry():
    """First spelling wins on dedup."""
    deck = (
        "[metadata]\n"
        "Protect=sol ring\n"
        "Protect=SOL RING\n"
        "Protect=Sol Ring\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == ["sol ring"]


def test_ignores_protect_outside_metadata_section():
    """Only [metadata] section is consulted."""
    deck = (
        "[metadata]\nName=Test\n"
        "[Main]\n"
        "Protect=Sol Ring\n"  # in [Main], ignored
        "1 Lightning Bolt\n"
    )
    assert read_protected_cards(deck) == []


def test_handles_empty_protect_value():
    """A bare ``Protect=`` with no value is a no-op."""
    deck = (
        "[metadata]\nProtect=\nProtect=Sol Ring\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == ["Sol Ring"]


def test_handles_empty_quoted_entries():
    """Trailing empty quoted chunks dropped silently."""
    deck = (
        "[metadata]\n"
        'Protect="Sol Ring", "", "Cultivate", ""\n'
        "[Main]\n"
    )
    assert read_protected_cards(deck) == ["Sol Ring", "Cultivate"]


def test_whitespace_around_bare_entries_is_trimmed():
    """Casual hand-edits land entries with surrounding spaces. Trim."""
    deck = (
        "[metadata]\nProtect=   Krenko, Mob Boss   \n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == ["Krenko, Mob Boss"]


def test_protect_key_is_case_insensitive():
    """``PROTECT=``, ``protect=``, ``Protect=`` all work."""
    deck = (
        "[metadata]\n"
        "PROTECT=Sol Ring\n"
        "protect=Cultivate\n"
        "Protect=Counterspell\n"
        "[Main]\n"
    )
    assert read_protected_cards(deck) == [
        "Sol Ring", "Cultivate", "Counterspell",
    ]


def test_empty_deck_text_returns_empty():
    """Defensive: empty input doesn't crash."""
    assert read_protected_cards("") == []


def test_real_world_commander_protection():
    """Smoke: the typical 'protect my commander + a few favorites'
    .dck shape works end-to-end. Single-line entries for comma-named
    cards (no quoting needed); compact quoted line for the rest."""
    deck = (
        "[metadata]\nName=Goblin\nMoxfield=abc\n"
        "Protect=Krenko, Mob Boss\n"
        'Protect="Goblin Lackey", "Skirk Prospector"\n'
        "Protect=Purphoros, God of the Forge\n"
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Sol Ring\n1 Goblin Lackey\n"
    )
    assert read_protected_cards(deck) == [
        "Krenko, Mob Boss",
        "Goblin Lackey",
        "Skirk Prospector",
        "Purphoros, God of the Forge",
    ]
