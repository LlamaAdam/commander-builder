"""Self-test for the curated real-oracle fixture.

The fixture in ``tests/fixtures/real_oracles.py`` exists to catch
classifier regressions against byte-exact Scryfall data — but only
if every entry actually exercises a classifier path. This module
checks that:

1. Every fixture's oracle text classifies to a non-trivial role
   (NOT ``"other"`` or ``"threat"`` — those are fallback buckets
   that indicate the classifier didn't engage).
2. The fixture's expected role matches what the classifier returns.

If a future pattern rewrite breaks against real Scryfall data, the
broken card's test in test_staples.py or test_deck_dashboard.py
will fail AND this self-test will catch any drift from the curated
expected role.

Run order: this file is independent of the rest of the suite — it
only depends on ``commander_builder.staples`` + the fixture itself.
"""

from __future__ import annotations

import pytest

from commander_builder.staples import classify_role, classify_role_extended
from tests.fixtures.real_oracles import ORACLES, oracle


# Expected role per card. Update when adding a new fixture entry.
# Pinned here (not in the fixture module) so the fixture stays
# focused on holding the Scryfall data — the assertion of what role
# each card SHOULD be lives in the test module that enforces it.
EXPECTED_ROLE: dict[str, str] = {
    "Coalition Victory":   "win_condition",
    "Craterhoof Behemoth": "win_condition",
    "Crux of Fate":        "wipe",
    "Cyclonic Rift":       "wipe",
    "Damnation":           "wipe",          # control value
    "Mystical Tutor":      "tutor",
    "Sylvan Library":      "draw",
    "Three Visits":        "ramp",
    "Toxic Deluge":        "wipe",
    "Wrath of God":        "wipe",          # control value
}


def test_every_fixture_has_expected_role():
    """Tripwire: any new fixture entry must declare its expected
    classification role above. Catches the case where someone adds
    a card to the fixture but forgets to wire it into the assertion
    map — that fixture entry would silently provide no signal."""
    missing = set(ORACLES) - set(EXPECTED_ROLE)
    assert not missing, (
        f"Fixture entries without EXPECTED_ROLE entries: {sorted(missing)}. "
        f"Add them to EXPECTED_ROLE in test_real_oracle_fixture.py."
    )
    stale = set(EXPECTED_ROLE) - set(ORACLES)
    assert not stale, (
        f"EXPECTED_ROLE entries with no matching fixture: {sorted(stale)}. "
        f"Add them to ORACLES in tests/fixtures/real_oracles.py."
    )


@pytest.mark.parametrize("card_name", sorted(ORACLES.keys()))
def test_classify_role_extended_matches_expected(card_name: str):
    """Every fixture card classifies to its declared expected role
    via the canonical entry point. This is the regression net — if
    a regex rewrite breaks any real card, the matching parametrized
    test fails by name in CI.
    """
    o = oracle(card_name)
    expected = EXPECTED_ROLE[card_name]
    got = classify_role_extended(o["oracle_text"], o["type_line"])
    assert got == expected, (
        f"{card_name!r} should classify as {expected!r} but got {got!r}. "
        f"Check whether the pattern in staples.py / "
        f"_WIN_CONDITION_PATTERNS still matches the real Scryfall "
        f"oracle text in tests/fixtures/real_oracles.py."
    )


def test_oracle_helper_raises_helpful_keyerror_for_missing_card():
    """The helper's error message should tell the developer how to
    fix the problem (add the card to the fixture, don't synthesize)."""
    with pytest.raises(KeyError, match="No real-oracle fixture"):
        oracle("Some Card That Doesn't Exist")


def test_fixture_classify_role_base_taxonomy_consistent():
    """Spot-check: cards that should be in the BASE taxonomy
    (ramp/draw/wipe/tutor) classify the same way via the unwrapped
    ``classify_role`` as via ``classify_role_extended``. Cards in
    the extended-only buckets (win_condition / land_payoff) are
    allowed to differ — that's the whole point of the extended
    wrapper.
    """
    base_only_roles = {"ramp", "draw", "wipe", "tutor", "removal",
                       "protection", "finisher"}
    for card_name in ORACLES:
        expected = EXPECTED_ROLE[card_name]
        if expected not in base_only_roles:
            continue
        o = oracle(card_name)
        base_role = classify_role(o["oracle_text"], o["type_line"])
        assert base_role == expected, (
            f"{card_name!r}: base classify_role returned {base_role!r}, "
            f"expected {expected!r}. Extended wrapper shouldn't be "
            f"hiding a base-classifier bug."
        )
