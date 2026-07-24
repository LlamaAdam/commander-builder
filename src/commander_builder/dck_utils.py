"""Canonical primitives for parsing Forge ``.dck`` deck files.

A Forge ``.dck`` file is an INI-like text format::

    [metadata]
    Name=My Deck
    [Commander]
    1 Atraxa, Praetors' Voice|CMM|1
    [Main]
    1 Sol Ring|C21|263
    12 Forest
    ...

Card lines are ``<qty> <Name>[|SET[|CN]]``. Section headers are matched
case-insensitively. A section runs until the next ``[...]`` header (or EOF).

Historically this parsing was reimplemented in ~10 modules with two slightly
different line regexes. Both are preserved here verbatim because they diverge
on degenerate inputs (a "name" that begins with ``|``):

* ``CARD_LINE_RE`` -- ``^(\\d+)\\s+([^|]+?)(\\s*\\|.*)?$``
  The quantity-summing convention (knowledge_log, deck_health,
  web/routes_audit). Rejects ``1 |SET|cn`` outright and yields an empty
  name for ``1   |x``.

* ``NAME_LINE_RE`` -- ``^\\d+\\s+(.+?)(?:\\|.*)?$``
  The name-collecting convention (intent, archetype, meta_test,
  improvement_advisor, scryfall_client). Matches ``1 |SET|cn`` with the
  bogus name ``|SET``.

On every realistic line the two agree. New code should prefer
``parse_card_line`` / ``iter_main_cards``; the ``*_names`` helpers exist to
reproduce the legacy name-collecting call sites exactly.
"""
from __future__ import annotations

import re
from typing import Iterator, Optional

# A legal Commander deck is exactly 100 cards TOTAL: mainboard plus
# command zone. The mainboard target is therefore NOT a constant 99 —
# it is ``100 - <number of commanders>``: 99 for a single commander,
# 98 for a partner pair (two lines in [Commander]). Code that hardcodes
# 99 corrupts partner decks (real incident: a Pako/Haldan deck padded
# from its legal 98 main to 99, writing a 101-card deck).
COMMANDER_DECK_SIZE = 100

# Quantity-summing convention: qty, base name (no "|"), optional |SET|CN tail.
CARD_LINE_RE = re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")

# Legacy name-collecting convention: qty discarded, non-greedy name capture.
NAME_LINE_RE = re.compile(r"^\d+\s+(.+?)(?:\|.*)?$")


def iter_section_lines(deck_text: str, section: str) -> Iterator[str]:
    """Yield the stripped, non-empty raw lines inside ``[section]``.

    ``section`` is the bare section name (e.g. ``"Main"``); the header
    match is case-insensitive (``[MAIN]`` == ``[main]``). Any other
    ``[...]`` header line ends the section. Header lines themselves are
    never yielded. Handles ``None``/empty text by yielding nothing.
    """
    if not deck_text:
        return
    wanted = f"[{section.lower()}]"
    in_section = False
    for raw in deck_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped.lower() == wanted
            continue
        if in_section:
            yield stripped


def parse_card_line(line: str) -> Optional[tuple[int, str]]:
    """Parse one card line into ``(quantity, base_name)``.

    Uses ``CARD_LINE_RE``: the base name is everything between the
    quantity and the first ``|`` (the ``|SET|CN`` printing suffix is
    stripped), with surrounding whitespace removed.

    Returns ``None`` for non-card lines (no leading quantity, metadata
    ``Key=Value`` lines, headers, ...). Note the returned name can be
    ``""`` for the degenerate line ``"1   |x"`` -- callers that build
    name maps should skip empty names (``count_main_cards`` deliberately
    still counts such lines, matching the historical behavior).
    """
    m = CARD_LINE_RE.match(line.strip())
    if not m:
        return None
    try:
        qty = int(m.group(1))
    except (TypeError, ValueError):  # pragma: no cover - \d+ always ints
        qty = 1
    return qty, m.group(2).strip()


def iter_main_cards(deck_text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(qty, name)`` for each card line in [Main], in deck order.

    Skips non-card lines and empty-name degenerates. Duplicate names are
    yielded separately (no merging). Mirrors deck_health._iter_main_cards.
    """
    for line in iter_section_lines(deck_text, "Main"):
        parsed = parse_card_line(line)
        if parsed is None:
            continue
        qty, name = parsed
        if name:
            yield qty, name


def count_main_cards(deck_text: Optional[str]) -> int:
    """Sum the quantity prefixes of every parseable [Main] card line.

    Returns 0 for ``None`` / empty text. Lines that match ``CARD_LINE_RE``
    but strip to an empty name still count (historical behavior of
    knowledge_log._count_main_cards / routes_audit._count_main_lines).
    """
    if not deck_text:
        return 0
    total = 0
    for line in iter_section_lines(deck_text, "Main"):
        parsed = parse_card_line(line)
        if parsed is not None:
            total += parsed[0]
    return total


def count_commander_cards(deck_text: Optional[str]) -> int:
    """Sum the quantity prefixes of every parseable [Commander] card line.

    1 for a normal deck, 2 for a partner / Background pair, 0 for a
    text fragment with no [Commander] section. Same parsing rules as
    ``count_main_cards`` (``CARD_LINE_RE``), just a different section.
    """
    if not deck_text:
        return 0
    total = 0
    for line in iter_section_lines(deck_text, "Commander"):
        parsed = parse_card_line(line)
        if parsed is not None:
            total += parsed[0]
    return total


def main_target(deck_text: Optional[str]) -> int:
    """The LEGAL mainboard size for ``deck_text``: ``100 - commanders``.

    99 for a single commander, 98 for a partner pair. When the text
    carries no [Commander] lines (a fragment, or a paste that hasn't
    been sectioned yet) we keep the historical assumption of a single
    commander and return 99 — every pre-partner-fix caller behaved
    exactly this way, and a fragment gives us nothing better to go on.
    """
    n = count_commander_cards(deck_text)
    if n <= 0:
        return COMMANDER_DECK_SIZE - 1
    return COMMANDER_DECK_SIZE - n


def main_card_quantities(deck_text: Optional[str]) -> dict[str, int]:
    """Fold the [Main] section into ``{base_name: total_quantity}``.

    Quantities for repeated names are summed; names keep file casing but
    lose the ``|SET|CN`` suffix. Empty-name degenerates are skipped.
    Returns ``{}`` for ``None`` / empty text. Mirrors
    knowledge_log._parse_main_cards.
    """
    cards: dict[str, int] = {}
    if not deck_text:
        return cards
    for qty, name in iter_main_cards(deck_text):
        cards[name] = cards.get(name, 0) + qty
    return cards


def section_card_names(deck_text: str, section: str) -> list[str]:
    """Collect card names (qty / ``|SET|CN`` stripped) from ``[section]``.

    Uses the legacy ``NAME_LINE_RE`` convention so existing call sites
    (intent, archetype, meta_test, improvement_advisor, scryfall_client)
    keep byte-identical behavior. Duplicates are kept in deck order.
    """
    names: list[str] = []
    for line in iter_section_lines(deck_text, section):
        m = NAME_LINE_RE.match(line)
        if m:
            names.append(m.group(1).strip())
    return names


def main_card_names(deck_text: str) -> list[str]:
    """``section_card_names`` specialized to the [Main] section."""
    return section_card_names(deck_text, "Main")
