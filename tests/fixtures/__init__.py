"""Test fixtures shared across the suite.

Currently contains:

- ``real_oracles`` — curated byte-exact Scryfall oracle text for
  cards we use in classifier tests. Use these instead of
  hand-written synthetic strings — the 2026-05-14 audit caught 9
  bugs that all passed synthetic-text tests but failed against real
  Scryfall data.
- ``archidekt_deck_shape.json`` — a REAL Archidekt detail response
  (deck 24864897, fetched 2026-08-20), trimmed to the keys the
  adapter reads plus 9 byte-verbatim card entries. Same lesson as
  ``real_oracles``, one API layer up: R2-P18 found
  ``archidekt_client``'s adapter pinned only by shapes the test file
  invented. Entries are evidence — extend the fixture from a new
  capture (``_captures/README.md``), never by hand.
- ``archidekt_deck_mdfc_shape.json`` — a SECOND real detail response
  (deck 5273595, fetched 2026-08-27), 6 byte-verbatim entries, added
  because the first deck contained no double-faced card. It pins the
  ``"Front // Back"`` name + populated ``faces`` shape, an MDFC
  COMMANDER, and the front-face ``.dck`` form Forge needs — and
  capturing it found a real bug (see ``archidekt_client._entry_name``).
  The two are a matched pair: single-faced baseline, double-faced
  contrast. Neither is a place to hand-add a card.
- ``hazel_primer.md`` — that deck's owner-written primer
  (``description``, verbatim), kept as the stated-intent test case for
  the FP-016 deck judge.
"""
