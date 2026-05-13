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
from collections import Counter

# Imported at module level (despite being only used inside
# ``count_deck_roles``) so tests can monkeypatch
# ``commander_builder.staples.lookup_card`` to inject synthetic card
# data without needing to know it lives in ``scryfall_client``.
from .scryfall_client import lookup_card

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


# --- Manabase essentials (the "your deck should have these" lands) ------
#
# User feedback (2026-05-13): "tribal decks should have cavern of souls.
# All decks should have dual lands and bond lands and fetch lands." The
# advisor's heuristic + bracket_peers paths recommend lands only when
# they happen to appear in references/EDHREC. This curated set is a
# deterministic safety net: any color-identity-appropriate essential
# that the deck doesn't already own surfaces as a recommended add,
# regardless of what the references happen to include this week.
#
# Each map: lowercase-card-name → frozenset of WUBRG letters the land
# spans. We treat color identity inclusively — Bountiful Promenade (GW)
# only fits a deck that has BOTH G and W in its identity. A monocolor
# deck won't see two-color cards in its essentials list because the
# additional color is wasted there.

ABU_DUAL_LANDS: dict[str, frozenset[str]] = {
    "bayou": frozenset({"B", "G"}),
    "badlands": frozenset({"B", "R"}),
    "plateau": frozenset({"R", "W"}),
    "scrubland": frozenset({"B", "W"}),
    "savannah": frozenset({"G", "W"}),
    "taiga": frozenset({"G", "R"}),
    "tundra": frozenset({"U", "W"}),
    "tropical island": frozenset({"G", "U"}),
    "underground sea": frozenset({"B", "U"}),
    "volcanic island": frozenset({"R", "U"}),
}

# Onslaught + Zendikar fetches. Each fetches one of two basic-land
# types, so we encode the two colors those basic types produce.
FETCH_LANDS: dict[str, frozenset[str]] = {
    "arid mesa": frozenset({"R", "W"}),
    "bloodstained mire": frozenset({"B", "R"}),
    "flooded strand": frozenset({"U", "W"}),
    "marsh flats": frozenset({"B", "W"}),
    "misty rainforest": frozenset({"G", "U"}),
    "polluted delta": frozenset({"B", "U"}),
    "scalding tarn": frozenset({"R", "U"}),
    "verdant catacombs": frozenset({"B", "G"}),
    "windswept heath": frozenset({"G", "W"}),
    "wooded foothills": frozenset({"G", "R"}),
}

# Battlebond + Commander Legends bond lands ("untapped if an opponent
# controls an untapped creature" — perfect for multiplayer pods).
BOND_LANDS: dict[str, frozenset[str]] = {
    "bountiful promenade": frozenset({"G", "W"}),
    "luxury suite": frozenset({"B", "R"}),
    "morphic pool": frozenset({"B", "U"}),
    "sea of clouds": frozenset({"U", "W"}),
    "spectator seating": frozenset({"R", "W"}),
    "spire garden": frozenset({"G", "R"}),
    "training center": frozenset({"U", "R"}),
    "rejuvenating springs": frozenset({"G", "U"}),
    "undergrowth stadium": frozenset({"B", "G"}),
    "vault of champions": frozenset({"B", "W"}),
}

# Ravnica shock lands — 2-color "pay 2 life or it enters tapped" duals.
# Cheaper than ABU duals but still archetype-defining in tuned decks.
SHOCK_LANDS: dict[str, frozenset[str]] = {
    "godless shrine": frozenset({"B", "W"}),
    "blood crypt": frozenset({"B", "R"}),
    "overgrown tomb": frozenset({"B", "G"}),
    "watery grave": frozenset({"B", "U"}),
    "stomping ground": frozenset({"G", "R"}),
    "temple garden": frozenset({"G", "W"}),
    "breeding pool": frozenset({"G", "U"}),
    "sacred foundry": frozenset({"R", "W"}),
    "steam vents": frozenset({"R", "U"}),
    "hallowed fountain": frozenset({"U", "W"}),
}


def essential_manabase_for_colors(color_identity) -> list[str]:
    """Return canonical card names for the manabase essentials whose
    color identity is fully contained in ``color_identity``.

    ``color_identity`` is a set / iterable of WUBRG letters
    (case-insensitive). Includes ABU duals, fetch lands, bond lands,
    and shock lands. A 2-color land is included only when BOTH of its
    colors are inside the deck's identity — a mono-red deck won't see
    Stomping Ground (RG) because the G slot is wasted.

    Empty identity (colorless commander) → empty list. The caller
    can still surface colorless utility lands (Cavern of Souls,
    Strip Mine, etc.) separately via tribal / utility helpers.

    Order: duals → fetches → shocks → bond lands. Within each tier,
    alphabetical. Keeps the recommendation surface predictable.
    """
    if not color_identity:
        return []
    identity = {c.upper() for c in color_identity if isinstance(c, str)}

    out: list[str] = []
    for source in (ABU_DUAL_LANDS, FETCH_LANDS, SHOCK_LANDS, BOND_LANDS):
        tier = sorted(
            (name for name, colors in source.items() if colors <= identity),
            key=str.lower,
        )
        # Render display-cased names (title-case respects existing
        # convention like "Misty Rainforest", "Sea of Clouds").
        # Cards in the static map use simple word casing.
        out.extend(_titlecase_card_name(name) for name in tier)
    return out


def _titlecase_card_name(lowercase_name: str) -> str:
    """Reverse the lowercase-key convention used in the manabase maps.

    'underground sea' → 'Underground Sea'; 'sea of clouds' →
    'Sea of Clouds' (the 'of' stays lowercased to match Scryfall's
    canonical capitalization for that specific land's name).
    """
    # Words that stay lowercase except when first.
    minor_words = {"of", "the", "in", "on", "and"}
    parts = lowercase_name.split()
    out = []
    for i, part in enumerate(parts):
        if i > 0 and part in minor_words:
            out.append(part)
        else:
            out.append(part.capitalize())
    return " ".join(out)


def is_land(card_name: str) -> bool:
    """Catch any land — basic, dual, fetch, shock, MDFC, utility, etc.

    Manabase decisions are deliberate; the advisor's cut path uses this
    to skip lands so it never recommends pulling a $200 ABU dual
    (Savannah, 2026-05-13 Ur-Dragon audit) just because reference
    decks happened to substitute a different mana base configuration.

    Basics short-circuit through the static frozenset (no Scryfall
    round-trip). Everything else falls back to a type_line check via
    ``lookup_card``. Lookup failures return False — over-protecting
    an unknown nonland is the worse mistake.
    """
    if is_basic_land(card_name):
        return True
    try:
        card = lookup_card(card_name)
    except Exception:
        return False
    if not card:
        return False
    type_line = (card.get("type_line") or "").lower()
    return "land" in type_line


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


# --- Role saturation thresholds (the advisor's redundancy guard) ---------

# Tuned-deck saturation points per role. The advisor uses these to drop
# adds whose role bucket is already full in the user's deck — the
# Ur-Dragon B4 audit (2026-05-13) recommended 5 ramp/cost-reducer adds
# to a deck already running 12+ ramp pieces, which empirically lost
# the A/B sim. These numbers are conservative (high side) so the guard
# only fires on genuinely-saturated buckets, not borderline ones.
#
# Roles not listed here NEVER saturate — see ``is_role_saturated``.
# ``threat``/``land``/``other`` are deliberately excluded because they
# don't pattern-match the "too many of these" failure mode.
ROLE_SATURATION_THRESHOLDS: dict[str, int] = {
    "ramp": 12,        # 8-10 is standard; 12+ is bloat
    "draw": 12,        # similar shape to ramp
    "removal": 10,     # 6-8 standard
    "wipe": 6,         # 2-4 standard; 6 is the upper bound on most decks
    "protection": 7,   # 3-5 standard
    "tutor": 8,        # 1-4 standard; tutor-heavy decks go higher
    "finisher": 14,    # finisher-tribal decks (dragons!) legitimately run many
}


def is_role_saturated(role: str, count: int) -> bool:
    """True when ``count`` cards of ``role`` already in the deck exceeds
    the threshold for that role. Roles with no configured threshold
    never saturate — a typo in a role string would otherwise silently
    drop every add."""
    threshold = ROLE_SATURATION_THRESHOLDS.get(role)
    if threshold is None:
        return False
    return count >= threshold


# --- Count roles in a deck (for the saturation guard) --------------------

# Imported lazily inside the helper because ``staples`` is already
# imported by modules that don't want a scryfall round-trip surface
# (forge_runner, web app at boot, etc.). Lazy import keeps the
# top-level module dependency graph small.


def count_deck_roles(card_names) -> "dict[str, int]":
    """Resolve each card name via Scryfall + ``classify_role`` and return
    a Counter of role → count.

    Defensive against missing lookups and Scryfall exceptions: unknown
    cards bucket into ``"other"`` rather than crashing the count. The
    advisor reads this to decide whether a role bucket is already
    saturated.

    Cache pressure: each unique card name triggers at most one
    ``lookup_card`` call (which is itself disk-cached). On a 99-card
    deck the cost is ~99 dict lookups + maybe a handful of Scryfall
    misses; both are cheap.
    """
    out: Counter = Counter()
    for name in card_names:
        try:
            card = lookup_card(name)
        except Exception:
            out["other"] += 1
            continue
        if not card:
            out["other"] += 1
            continue
        role = classify_role(
            card.get("oracle_text", "") or "",
            card.get("type_line", "") or "",
        )
        out[role] += 1
    return out
