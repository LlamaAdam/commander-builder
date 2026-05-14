"""Curated byte-exact Scryfall oracle text fixtures.

## Why this exists

The 2026-05-14 chrome-audit follow-up caught **nine** classifier
bugs in ``staples.classify_role`` / ``classify_role_extended``
that all passed the existing unit tests. The unit tests used
hand-written synthetic oracle text that happened to match the
overly-permissive regex patterns; real Scryfall data exposed the
gaps:

- ``Cyclonic Rift`` — oracle has ``\\n`` between target clause and
  ``Overload {`` paragraph; original regex used ``.*`` (not
  ``[\\s\\S]``) and didn't cross newlines.
- ``Crux of Fate`` — real text is ``Destroy all Dragon creatures``
  (typed all-sweep), not ``Destroy each Dragon`` the test assumed.
- ``Coalition Victory`` — uses ``You win the game`` idiom that
  wasn't in ``_WIN_CONDITION_PATTERNS`` at all.
- ``Three Visits`` — ``Search your library for a Forest card``;
  pattern required literal word "land".
- ``Sylvan Library`` — ``draw two additional cards`` template; the
  "additional" qualifier broke the literal-word-order pattern.
- ``Toxic Deluge`` — ``All creatures get -X/-X``; no existing
  pattern matched the mass-shrink wipe shape.
- ``Mystical Tutor`` — ``instant or sorcery card``; flat
  alternation didn't tolerate the OR templating.
- ``Craterhoof Behemoth`` — real oracle says ``gain trample and
  get +X/+X``, OPPOSITE word order from the original pattern.
- Multiple cards had similar issues that the synthetic-text test
  fixtures hid.

## The rule

When writing a classification test, **always source the oracle
text from this module** rather than hand-writing a synthetic
approximation. Every value here was copy-pasted directly from a
live Scryfall API response (`https://api.scryfall.com/cards/named`),
including the ``\\n`` paragraph breaks and the typographic dashes.

## How to add a new card

1. Look up the card at ``https://scryfall.com/search?q=!"<name>"`` and
   copy the oracle text exactly as displayed.
2. Add it below in alphabetical-by-card-name order with a short
   comment explaining what role-classifier behavior the fixture
   pins.
3. Reference it in your test via
   ``from tests.fixtures.real_oracles import ORACLES``
   then ``ORACLES["Card Name"]``.

Do NOT paraphrase, normalize, or "clean up" the text. Real Scryfall
data has em-dashes, bullet glyphs, and trailing newlines that
matter — the classifier must handle them as Scryfall ships them.
"""

from __future__ import annotations


# Card name → ``{"oracle_text": str, "type_line": str}`` dict, sourced
# verbatim from Scryfall. Keys sorted alphabetically by card name.
ORACLES: dict[str, dict[str, str]] = {
    # Coalition Victory — uses "You win the game" idiom which the
    # original ``_WIN_CONDITION_PATTERNS`` didn't cover. Pinned in
    # commit b2ff2b9.
    "Coalition Victory": {
        "oracle_text": (
            "You win the game if you control a land of each basic "
            "land type and a creature of each color."
        ),
        "type_line": "Sorcery",
    },

    # Craterhoof Behemoth — "gain trample and get +X/+X" is the
    # OPPOSITE word order from the original
    # ``_WIN_CONDITION_PATTERNS`` entry ("get +N/+N and gain
    # trample"). Pinned in commit 085c256.
    "Craterhoof Behemoth": {
        "oracle_text": (
            "Haste\n"
            "When this creature enters, creatures you control gain "
            "trample and get +X/+X until end of turn, where X is the "
            "number of creatures you control."
        ),
        "type_line": "Creature — Beast",
    },

    # Crux of Fate — typed all-sweep ("destroy all Dragon creatures"),
    # not the "each <type>" idiom the original test fixture assumed.
    # Multi-paragraph with em-dash bullets. Pinned in commit b2ff2b9.
    "Crux of Fate": {
        "oracle_text": (
            "Choose one —\n"
            "• Destroy all Dragon creatures.\n"
            "• Destroy all non-Dragon creatures."
        ),
        "type_line": "Sorcery",
    },

    # Cyclonic Rift — the canonical overload bounce wipe. Real
    # Scryfall oracle has ``\n`` between the target clause and the
    # ``Overload {`` paragraph; the original regex used ``.*`` which
    # Python's ``re.search`` doesn't cross newlines without DOTALL.
    # Pinned in commit b2ff2b9.
    "Cyclonic Rift": {
        "oracle_text": (
            "Return target nonland permanent you don't control to "
            "its owner's hand.\n"
            "Overload {6}{U} (You may cast this spell for its "
            "overload cost. If you do, change \"target\" in its "
            "text to \"each.\")"
        ),
        "type_line": "Instant",
    },

    # Damnation — basic destroy-all template. Already classified
    # correctly before the audit but kept here as a control value
    # for the multi-paragraph parser.
    "Damnation": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
    },

    # Mystical Tutor — "instant or sorcery card" OR-templating
    # pattern; flat alternation in the original tutor regex didn't
    # match. Pinned in commit 085c256.
    "Mystical Tutor": {
        "oracle_text": (
            "Search your library for an instant or sorcery card, "
            "reveal it, then shuffle and put that card on top of "
            "your library."
        ),
        "type_line": "Instant",
    },

    # Sylvan Library — "draw two additional cards"; the "additional"
    # qualifier between number and "cards" broke the literal-pattern
    # match. Pinned in commit 085c256.
    "Sylvan Library": {
        "oracle_text": (
            "At the beginning of your draw step, you may draw two "
            "additional cards. If you do, choose two cards in your "
            "hand drawn this turn. For each of those cards, pay 4 "
            "life or put the card on top of your library."
        ),
        "type_line": "Enchantment",
    },

    # Three Visits — "Search your library for a Forest card" (basic
    # land type rather than the literal word "land"). Original ramp
    # pattern required ``\bland\b``. Pinned in commit 085c256.
    "Three Visits": {
        "oracle_text": (
            "Search your library for a Forest card, put it onto the "
            "battlefield, then shuffle."
        ),
        "type_line": "Sorcery",
    },

    # Toxic Deluge — "All creatures get -X/-X" mass-shrink wipe; no
    # existing pattern matched the shape. Pinned in commit 085c256.
    "Toxic Deluge": {
        "oracle_text": (
            "As an additional cost to cast this spell, pay X life.\n"
            "All creatures get -X/-X until end of turn."
        ),
        "type_line": "Sorcery",
    },

    # Wrath of God — baseline destroy-all template. Used as a
    # control value to confirm the standard pattern still works
    # after the multi-paragraph parser tweaks.
    "Wrath of God": {
        "oracle_text": "Destroy all creatures. They can't be regenerated.",
        "type_line": "Sorcery",
    },
}


def oracle(name: str) -> dict[str, str]:
    """Convenience accessor: returns ``{"oracle_text", "type_line"}``
    for ``name``. Raises ``KeyError`` with a helpful message when the
    card hasn't been added to the fixture yet.
    """
    if name not in ORACLES:
        raise KeyError(
            f"No real-oracle fixture for {name!r}. Add it to "
            f"tests/fixtures/real_oracles.py — do NOT synthesize "
            f"oracle text in tests; copy verbatim from Scryfall."
        )
    return ORACLES[name]
