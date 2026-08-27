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
- ``hazel_primer.md`` — that deck's owner-written primer
  (``description``, verbatim), kept as the stated-intent test case for
  the FP-016 deck judge.
"""
