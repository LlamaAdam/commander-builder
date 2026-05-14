"""Test fixtures shared across the suite.

Currently contains:

- ``real_oracles`` — curated byte-exact Scryfall oracle text for
  cards we use in classifier tests. Use these instead of
  hand-written synthetic strings — the 2026-05-14 audit caught 9
  bugs that all passed synthetic-text tests but failed against real
  Scryfall data.
"""
