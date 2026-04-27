"""Universal staples and card-role categorization.

Two related concerns surface enough across the advisor pipeline that they
deserve a dedicated module:

1. **Universal staples** — cards that are in 80%+ of decks regardless of
   commander (Sol Ring, Arcane Signet, Command Tower, basic lands, etc.).
   These show up at the top of every EDHREC inclusion list, but
   recommending them as adds is noise — every deck already has them, and
   if it doesn't that's a deck-author choice (cube, budget, theme).
   Excluding them from the must-add list lets the rest of the
   recommendation surface signal.

2. **Card roles** — given a card's oracle text + type line, classify it
   into one of: ``ramp``, ``draw``, ``removal``, ``wipe``, ``protection``,
   ``tutor``, ``finisher``, ``threat``, ``land``, ``other``. Mirrors
   ``forge_py.card_tagger`` deliberately (we may consolidate later); for
   now we keep an independent local copy because the two projects share
   no code.

Both functions are pure (no I/O, no network) and idempotent. Tests can
exercise them with synthetic ``oracle_text`` strings.
"""

from __future__ import annotations

import re

# Cards that show up in well over 50% of all decks regardless of commander.
# Recommending these as adds is noise. Cutting them is also rarely correct.
# Lowercase canonical form. Reviewed against EDHREC's "all decks" 2026 leaderboard.
UNIVERSAL_STAPLES_LC: frozenset[str] = frozenset({
    # Mana fixing — colorless artifacts / lands every deck wants
    "sol ring",
    "arcane signet",
    "command tower",
    "exotic orchard",
    "fellwar stone",
    "mind stone",
    "thought vessel",
    "skullclamp",
    # Color-indistinguishable card filtering
    "swiftfoot boots",
    "lightning greaves",
    # Generic answers
    "reliquary tower",
    "rogue's passage",
    "myriad landscape",
    "evolving wilds",
    "terramorphic expanse",
    "path of ancestry",
    "ash barrens",
})

# Basic lands by all known names.
BASIC_LANDS_LC: frozenset[str] = frozenset({
    "plains", "island", "swamp", "mountain", "forest", "wastes",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest", "snow-covered wastes",
})


def is_universal_staple(card_name: str) -> bool:
    """Return True if ``card_name`` is on the universal-staples list.
    Case-insensitive."""
    return card_name.lower().strip() in UNIVERSAL_STAPLES_LC


def is_basic_land(card_name: str) -> bool:
    return card_name.lower().strip() in BASIC_LANDS_LC


# --- Role classification --------------------------------------------------

# Each role has a list of (regex, expected_in_type_line, score) tuples.
# Highest-scoring matched role wins. Multiple roles may apply but we
# return the strongest one for category-style display.

_ROLE_PATTERNS: list[tuple[str, list[tuple[str, str | None, int]]]] = [
    # Order roughly matches priority when multiple apply.
    ("land", [
        (r"\bland\b", "land", 100),
    ]),
    ("ramp", [
        # Matches fetches and tutors-for-land in any phrasing. Once
        # "your library" + a "land" mention appear in the same clause,
        # it's a ramp/fetch effect.
        (r"search your library[^.]{0,80}\bland\b", None, 80),
        (r"add\s+\{[wubrgc]\}", None, 50),  # mana producers, colored or colorless
        (r"(?:put|return)[^.]+land card[^.]+(?:onto the battlefield|to the battlefield)", None, 80),
        (r"create a treasure token", None, 40),
    ]),
    ("draw", [
        (r"draw (?:a card|two cards|three cards|\d cards|x cards|cards equal)", None, 70),
        (r"investigate", None, 40),
        (r"scry \d+", None, 30),
        (r"\bcantrip", None, 60),
    ]),
    ("removal", [
        (r"destroy target", None, 70),
        (r"exile target", None, 75),
        (r"return target (?:creature|permanent|nonland) (?:to its owner's hand|to your hand)", None, 50),
        (r"target creature gets -\d+/-\d+", None, 40),
        (r"deals \d+ damage to (?:any target|target)", None, 50),
        (r"counter target spell", None, 65),
    ]),
    ("wipe", [
        (r"destroy all (?:creatures|nonland|nonland permanents|permanents)", None, 90),
        (r"exile all (?:creatures|permanents)", None, 90),
        (r"return all .* to (?:its|their) owners' hands", None, 80),
        (r"deals \d+ damage to each (?:creature|player)", None, 75),
    ]),
    ("protection", [
        (r"hexproof", None, 50),
        (r"indestructible", None, 50),
        (r"protection from", None, 60),
        (r"shroud", None, 50),
        (r"can't be the target of", None, 50),
    ]),
    ("tutor", [
        (r"search your library for a (?:card|creature|artifact|enchantment|instant|sorcery|planeswalker|legendary)", None, 80),
    ]),
    ("finisher", [
        (r"each opponent loses \d+ life", None, 60),
        (r"target opponent loses the game", None, 95),
        (r"each opponent's life total becomes", None, 70),
        (r"deals damage equal to .* to each opponent", None, 60),
        (r"infect", None, 40),
    ]),
]


def classify_role(oracle_text: str, type_line: str = "") -> str:
    """Return the strongest-matching role for a card given its oracle text +
    type line. Returns ``"other"`` if nothing matches.

    The role taxonomy is intentionally coarse — granular enough to group
    advisor recommendations, not so fine that it becomes a card-text
    interpreter."""
    text = (oracle_text or "").lower()
    types = (type_line or "").lower()

    # Land takes priority: anything with "Land" in its type line is a land.
    if "land" in types:
        # Distinguish ramp lands (fetches, MDFC ramp, etc.) from generic mana lands
        if "search your library" in text:
            return "ramp"
        return "land"

    # Creature with no other strong signal — call it a threat.
    has_creature_type = "creature" in types

    best_role = "other"
    best_score = 0
    for role, patterns in _ROLE_PATTERNS:
        if role == "land":  # already handled above
            continue
        for pattern, type_req, score in patterns:
            if type_req and type_req not in types:
                continue
            if re.search(pattern, text):
                if score > best_score:
                    best_score = score
                    best_role = role

    if best_role == "other" and has_creature_type:
        return "threat"
    return best_role


# --- Frequency labels (for "in N of M references") -----------------------

def render_frequency_label(count: int, total: int) -> str:
    """Render ``count``-of-``total`` reference frequency as a human label.

    Used by the advisor when synthesizing must-add lists from multiple
    reference decks. Higher confidence labels read first.
    """
    if total <= 0:
        return ""
    if count == total and total >= 3:
        return f"unanimous ({count}/{total} refs)"
    if count >= total - 1 and total >= 3:
        return f"near-unanimous ({count}/{total} refs)"
    if count * 2 >= total and total >= 2:
        return f"majority ({count}/{total} refs)"
    if count >= 1:
        return f"minority ({count}/{total} refs)"
    return ""


def confidence_tier(count: int, total: int) -> int:
    """Bucket reference-frequency into 0..3 for sortable confidence.
    0 = absent, 1 = minority, 2 = majority, 3 = unanimous-ish."""
    if total <= 0 or count <= 0:
        return 0
    if count == total and total >= 3:
        return 3
    if count * 2 >= total:
        return 2
    return 1
