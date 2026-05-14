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
from typing import Optional

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

# Universal mana fixers — colorless mana cost, produce any color.
# Earn their slot in 3+ color decks where the consistency upside
# outweighs the life loss / opponent-token cost. Below 3 colors,
# Temple Garden / Bayou are strictly better than City of Brass.
UTILITY_FIXING_LANDS: tuple[str, ...] = (
    "City of Brass",       # any color, 1 damage to self when tapped
    "Mana Confluence",     # any color, 1 life when used
    "Reflecting Pool",     # color any other land in play produces
    "Forbidden Orchard",   # any color, opp gets a 1/1 spirit
)


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


def utility_fixing_lands(color_identity) -> list[str]:
    """Universal-fixer lands worth slotting in 3+ color decks.

    Returns ``[]`` for mono- and 2-color decks because cheaper /
    cleaner alternatives exist there (Temple Garden vs City of Brass
    for a Selesnya deck). At 3+ colors, the consistency win from
    "any color, always" outweighs the life loss or token-gift cost
    of each.

    Order is the static order in ``UTILITY_FIXING_LANDS``: most-
    universally-played first (City of Brass > Mana Confluence >
    Reflecting Pool > Forbidden Orchard).
    """
    if not color_identity:
        return []
    identity = {c.upper() for c in color_identity if isinstance(c, str)}
    if len(identity) < 3:
        return []
    return list(UTILITY_FIXING_LANDS)


def essential_manabase_for_colors(
    color_identity, budget: bool = False,
) -> list[str]:
    """Return canonical card names for the manabase essentials whose
    color identity is fully contained in ``color_identity``.

    ``color_identity`` is a set / iterable of WUBRG letters
    (case-insensitive). Includes ABU duals, fetch lands, bond lands,
    shock lands, and (for 3+ color decks) universal utility fixers.
    A 2-color land is included only when BOTH of its colors are
    inside the deck's identity — a mono-red deck won't see Stomping
    Ground (RG) because the G slot is wasted.

    ``budget=True`` strips the $200+ ABU duals AND the $25-60 fetch
    lands — leaving shock lands ($10-30), bond lands ($5-20), and
    utility fixers ($5-30) as the realistic budget manabase. Use
    when the user opted out of the most expensive cards via the
    audit panel's budget toggle.

    Empty identity (colorless commander) → empty list. The caller
    can still surface colorless utility lands (Cavern of Souls,
    Strip Mine, etc.) separately via tribal / utility helpers.

    Order: duals → fetches → shocks → bond lands → universal fixers
    (when 3+ colors). Within each tier, alphabetical. Budget mode
    skips the first two tiers but preserves order within the rest.
    """
    if not color_identity:
        return []
    identity = {c.upper() for c in color_identity if isinstance(c, str)}

    tiers: tuple[dict[str, frozenset[str]], ...]
    if budget:
        # Drop ABU duals + fetches — the two expensive tiers. Shocks,
        # bonds, and utility fixers stay.
        tiers = (SHOCK_LANDS, BOND_LANDS)
    else:
        tiers = (ABU_DUAL_LANDS, FETCH_LANDS, SHOCK_LANDS, BOND_LANDS)

    out: list[str] = []
    for source in tiers:
        tier = sorted(
            (name for name, colors in source.items() if colors <= identity),
            key=str.lower,
        )
        # Render display-cased names (title-case respects existing
        # convention like "Misty Rainforest", "Sea of Clouds").
        # Cards in the static map use simple word casing.
        out.extend(_titlecase_card_name(name) for name in tier)
    # Universal fixers — only for 3+ color decks (see
    # utility_fixing_lands docstring for the rationale). City of
    # Brass / Mana Confluence are $10-30, well under the budget
    # threshold, so we include them in both modes.
    out.extend(utility_fixing_lands(identity))
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


# --- Tribal essentials (Cavern of Souls etc.) -----------------------------
#
# Canonical Magic creature types that appear often enough as tribal
# archetypes to be worth detecting from a commander's oracle text.
# Order matters loosely — we check in this order and return the first
# match, so put the more-specific types before generic ones (e.g.
# "Soldier" matches a lot of cards but it's rarely the *primary*
# tribe; check more flavorful types first).
_CANONICAL_TRIBAL_TYPES: tuple[str, ...] = (
    "Dragon", "Sliver", "Elf", "Goblin", "Vampire", "Zombie", "Merfolk",
    "Angel", "Demon", "Beast", "Wizard", "Knight", "Spirit", "Ninja",
    "Pirate", "Dinosaur", "Faerie", "Eldrazi", "Werewolf", "Cat",
    "Bird", "Hydra", "Treefolk", "Giant", "Minotaur", "Druid",
    "Warrior", "Soldier", "Human",
)


def detect_tribal_type(oracle_text: str, type_line: str = "") -> Optional[str]:
    """Best-effort guess at a commander's primary tribal type.

    Looks for creature-type mentions in the oracle text (e.g. "Dragon
    spells you cast cost 1 less", "Create a Goblin creature token")
    and returns the **most-mentioned** canonical tribe. Frequency
    wins over canonical-list order — Lathliss mentioning "Spirit"
    twice and "Dragon" once would return Dragon (the more meaningful
    tribal signal there), but a synthetic oracle with "Spirit"
    twice and "Dragon" once returns Spirit because the text
    emphasizes Spirit more.

    Ties break by canonical-list order (more-played tribes first)
    so the result is deterministic. None when no canonical tribal
    type is mentioned at all.

    Not exhaustive — covers the ~30 most-played tribes. Misses for
    obscure tribal commanders (Frog, Otter, etc.) gracefully return
    None, which the caller treats as "this isn't a tribal deck."
    """
    if not oracle_text:
        return None
    import re
    # Count occurrences of each canonical tribe. Singular + plural
    # both count at word boundaries (so "Dragons" hits Dragon).
    counts: list[tuple[str, int]] = []
    for tribe in _CANONICAL_TRIBAL_TYPES:
        pattern = rf"\b{re.escape(tribe)}s?\b"
        n = len(re.findall(pattern, oracle_text))
        if n > 0:
            counts.append((tribe, n))
    if not counts:
        return None
    # Most-frequent wins; ties break by canonical order (the
    # original list order is preserved in ``counts``, so a stable
    # sort by -count keeps canonical-earlier first on ties).
    counts.sort(key=lambda pair: -pair[1])
    return counts[0][0]


# Lands every tribal deck wants regardless of color identity. All are
# colorless mana cost so they fit any deck.
_TRIBAL_LANDS = (
    "Cavern of Souls",      # mana of any color + uncounterable for the tribe
    "Path of Ancestry",     # filter mana + scry 1 on tribe entry
    "Secluded Courtyard",   # tribe-typed mana
    "Unclaimed Territory",  # tribe-typed mana
)


# Theme-detection patterns. Each theme has a list of regex
# patterns (matched against card oracle text, case-insensitive)
# and a minimum count required to flag the deck. Mapped to EDHREC
# tag slugs so the advisor can pull /tags/<slug> for additional
# add candidates + cut-protection.
#
# Tuning rationale: thresholds reflect "the deck obviously cares
# about this" — at least 8 cards minimum for most themes,
# stricter (10-12) for broad themes that incidentally appear in
# many decks (Lifegain, Counters). Below threshold the deck is
# probably a goodstuff toolbox, not a themed build.
_THEME_PATTERNS: list[tuple[str, str, list[str], int]] = [
    # (theme_name_for_logging, edhrec_tag_slug, [oracle_regex...], min_count)
    (
        "Tokens", "tokens",
        [
            # Match anything between "create" and "token(s)" — token
            # blurbs include power/toughness ("1/1"), slashes,
            # numbers, colors, creature types. ``.+?`` with the
            # non-greedy modifier is broad-but-bounded; the
            # closing "token" anchors the match.
            r"creates? .+? tokens?",
            r"create [\w\s]+ tokens?",  # fallback for simple "Create N tokens"
        ],
        8,
    ),
    (
        "Spellslinger", "spellslinger",
        [
            r"whenever you cast (?:a|an|your)",
            r"prowess",
            r"copy target instant or sorcery",
            r"instant and sorcery spells you cast cost",
        ],
        8,
    ),
    (
        "Aristocrats", "sacrifice",
        [
            r"sacrifice (?:a|another) creature",
            r"whenever (?:a creature|another creature|this creature) (?:you control )?dies",
            r"creatures? you control dies?",
        ],
        8,
    ),
    (
        "+1/+1 Counters", "plus-1-plus-1-counters",
        [
            r"\+1/\+1 counter",
            r"enters? with a \+1/\+1",
            r"put a \+1/\+1 counter",
        ],
        10,
    ),
    (
        "Landfall", "landfall",
        [
            r"landfall",
            r"whenever a land enters",
            r"whenever you play a land",
            r"for each land you control",
        ],
        7,
    ),
    (
        "Lifegain", "lifegain",
        [
            r"you gain \w+ life",
            r"whenever you gain life",
            r"if you gained life this turn",
            r"lifelink",
        ],
        10,
    ),
    (
        "Reanimator", "reanimator",
        [
            r"return target creature card from (?:your |a )?graveyard",
            r"return [\w\s]+ from your graveyard to the battlefield",
            r"put target creature card from a graveyard onto the battlefield",
        ],
        6,
    ),
    (
        "Equipment", "equipment",
        [
            r"\bequip\s*\{",
            r"equipped creature",
            r"attach to target creature",
        ],
        8,
    ),
    (
        "Artifacts", "artifacts",
        [
            r"whenever (?:an? |another )?artifact (?:enters|you control)",
            r"artifacts? (?:you control )?(?:enters?|cost)",
            r"metalcraft",
            r"affinity for artifacts",
        ],
        10,
    ),
    (
        "Enchantress", "enchantress",
        [
            r"whenever you cast an enchantment",
            r"whenever an enchantment enters",
            r"constellation",
        ],
        6,
    ),
]


def detect_themes(deck_oracles: list[tuple[str, str]]) -> list[str]:
    """Scan the deck's card oracle texts and return EDHREC tag
    slugs for any themes that meet their threshold count.

    ``deck_oracles`` is a list of ``(card_name, oracle_text)``
    tuples. Each pattern matches against the lowercased oracle;
    a card counts toward a theme if ANY of the theme's patterns
    match anywhere in its text. Cards can count for multiple
    themes (a token-producer with a +1/+1 effect counts toward
    both).

    Returns up to 3 theme slugs sorted by signal strength (most
    matches first). 3-slug cap keeps the audit's
    ``/tags/<slug>`` fetch count bounded — each tag page is a
    ~1-2s HTTP round-trip even with caching.

    Used by the advisor to pull theme-specific tag pages for
    non-tribal themed decks (Spellslinger, Tokens, Aristocrats,
    etc.). Tribal detection is separate (``detect_tribal_type``).
    """
    import re as _re
    counts: dict[str, tuple[str, int]] = {}  # slug → (name, count)
    for name, oracle in deck_oracles:
        if not oracle:
            continue
        text = oracle.lower()
        for theme_name, slug, patterns, _threshold in _THEME_PATTERNS:
            if any(_re.search(p, text, _re.IGNORECASE) for p in patterns):
                _name, prev = counts.get(slug, (theme_name, 0))
                counts[slug] = (theme_name, prev + 1)
    # Filter to themes that cleared their min-count threshold.
    qualifying: list[tuple[str, int]] = []
    for theme_name, slug, _patterns, min_count in _THEME_PATTERNS:
        _n, count = counts.get(slug, (theme_name, 0))
        if count >= min_count:
            qualifying.append((slug, count))
    qualifying.sort(key=lambda kv: -kv[1])
    return [slug for slug, _ in qualifying[:3]]


def tribal_essential_lands(tribe: Optional[str]) -> list[str]:
    """Return the canonical tribal-utility lands for ``tribe``.

    Returns an empty list when ``tribe`` is None (deck isn't tribal).
    All four lands are colorless mana cost, so they fit any color
    identity — the manabase recommender appends them on top of the
    color-gated ABU duals / fetches / shocks / bond lands.
    """
    if not tribe:
        return []
    return list(_TRIBAL_LANDS)


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
        # "Search your library for a Forest card" / "Plains card" etc.
        # — Three Visits / Nature's Lore / Land Tax style. The basic
        # land type names act as "land" synonyms in the templating.
        # Live audit 2026-05-13 caught Three Visits returning "other"
        # because the original pattern required the literal word
        # "land", which Three Visits replaces with "Forest".
        (r"search your library[^.]{0,80}\b(?:forest|island|plains|swamp|mountain)\b card", None, 80),
        (r"add\s+\{[wubrgc]\}", None, 50),  # mana producers, colored or colorless
        (r"(?:put|return)[^.]+land card[^.]+(?:onto the battlefield|to the battlefield)", None, 80),
        (r"create a treasure token", None, 40),
    ]),
    ("draw", [
        (r"draw (?:a card|two cards|three cards|\d cards|x cards|cards equal)", None, 70),
        # "Draw two additional cards" / "draw an additional card" —
        # Sylvan Library / Howling Mine style. The "additional"
        # qualifier between the number and "cards" broke the literal
        # word-order pattern above; this looser one catches the
        # idiom. Live audit 2026-05-13 caught Sylvan Library
        # returning "other".
        (r"draw \w+ additional cards?", None, 70),
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
        # "Destroy all <type> creatures" — Crux of Fate's real
        # Scryfall oracle reads "Destroy all Dragon creatures" /
        # "Destroy all non-Dragon creatures" (typed all-sweep, not
        # the "each" idiom). Live-browser audit 2026-05-13 caught
        # this: the dashboard reported wipe=0 on the Ur-Dragon deck
        # despite Crux of Fate being present. ``\S+`` covers
        # "Dragon" + "non-Dragon" + any hyphenated subtype.
        (r"destroy all \S+ creatures", None, 90),
        (r"exile all (?:creatures|permanents)", None, 90),
        (r"return all .* to (?:its|their) owners' hands", None, 80),
        (r"deals \d+ damage to each (?:creature|player)", None, 75),
        # "Destroy each <typed> creature" / "destroy each <subtype>" —
        # In Garruk's Wake, Dusk // Dawn, etc. The "each <typed>"
        # idiom is modern templating for board-scoped removal.
        # Score above single-target removal (70-75) but at the same
        # tier as the "destroy all" wipe.
        (r"destroy each (?:creature|non-?\w+ creature|\w+)", None, 90),
        (r"exile each (?:creature|non-?\w+ creature|\w+)", None, 90),
        # Overload bounce wipes — Cyclonic Rift et al. Scryfall's
        # real oracle text puts the overload paragraph AFTER a
        # newline (``\n``) following the target clause, so the
        # regex must cross newlines. Using ``[\s\S]*`` (any char
        # incl. newline) instead of ``.*``, which Python's
        # ``re.search`` doesn't cross-line by default.
        #
        # Live-browser audit 2026-05-13 caught the original miss:
        # ``classify_role_extended('Cyclonic Rift oracle text')``
        # returned ``"other"`` because the test fixtures had
        # synthesized the oracle on one line. The real Scryfall
        # text reads:
        #   "Return target nonland permanent you don't control to
        #    its owner's hand.\nOverload {6}{U} (...)"
        #
        # Order matters: the target clause comes BEFORE the
        # overload paragraph in printed templating, so the regex
        # threads target-clause → overload-mention.
        (r"return target[\s\S]*?to (?:its|their) owner(?:'s|s')? hands?[\s\S]*overload\s*\{", None, 85),
        (r"destroy target (?:creature|permanent|artifact|enchantment)[\s\S]*overload\s*\{", None, 85),
        (r"exile target (?:creature|permanent|artifact|enchantment)[\s\S]*overload\s*\{", None, 85),
        # Symmetric: when the overload appears earlier (unlikely
        # but possible in re-formatted oracle text), still catch it.
        (r"return each[\s\S]*?(?:you don't control|nonland permanent)[\s\S]*?to (?:its|their) owner(?:'s|s')? hands?", None, 85),
        # -X/-X mass-shrink wipes — Toxic Deluge, Crippling Fear,
        # Languish (which uses literal -2/-2 and is already covered
        # by the digit form). The "all creatures get -X/-X" template
        # is the modern catch-all. Pattern allows digits OR variables
        # so it matches both "all creatures get -2/-2" (Languish) and
        # "all creatures get -X/-X" (Toxic Deluge).
        # Live audit 2026-05-13 caught Toxic Deluge returning "other".
        (r"all creatures get -[\dxn]+/-[\dxn]+", None, 80),
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
        # "Search your library for an instant or sorcery card" —
        # Mystical Tutor style, where the type list is OR'd. Generic
        # version: "an? <type-word>(?: or <type-word>)* card". Live
        # audit 2026-05-13 caught Mystical Tutor returning "other".
        (r"search your library for an? (?:creature|artifact|enchantment|instant|sorcery|planeswalker|legendary|\w+)(?: or (?:creature|artifact|enchantment|instant|sorcery|planeswalker|legendary|\w+))+\s+card", None, 80),
    ]),
    ("finisher", [
        (r"each opponent loses \d+ life", None, 60),
        (r"target opponent loses the game", None, 95),
        (r"each opponent's life total becomes", None, 70),
        (r"deals damage equal to .* to each opponent", None, 60),
        (r"infect", None, 40),
    ]),
]


# --- Extended taxonomy (land_payoff / win_condition) ----------------------
#
# Two role buckets that the base ``classify_role`` taxonomy doesn't
# cover. They were originally defined in ``deck_dashboard`` because the
# UI's Categories panel surfaces them, but the advisor also wants to
# tag recommendations consistently — when the dashboard reports
# "win_condition=1" the advisor's evidence pill on the same card
# should read "win_condition", not the base ``finisher`` it would
# otherwise return. ``classify_role_extended`` (below) is the
# canonical entry point both callers should use; ``deck_dashboard``
# re-exports for backward compat with existing test imports.

_LAND_PAYOFF_PATTERNS = [
    re.compile(r"whenever a land enters", re.IGNORECASE),
    re.compile(r"whenever .*land .* enters", re.IGNORECASE),
    re.compile(r"landfall", re.IGNORECASE),
    re.compile(r"for each land you control", re.IGNORECASE),
    re.compile(r"whenever you play a land", re.IGNORECASE),
]

_WIN_CONDITION_PATTERNS = [
    re.compile(r"target opponent loses the game", re.IGNORECASE),
    # "You win the game" — Coalition Victory, Approach of the
    # Second Sun (second cast), Test of Endurance, Felidar
    # Sovereign, etc. Live-browser audit 2026-05-13 caught the
    # original miss: Coalition Victory was returning "other" for
    # the Ur-Dragon deck despite being listed as a Game Changer.
    re.compile(r"you win the game", re.IGNORECASE),
    re.compile(r"each opponent loses \d+ life", re.IGNORECASE),
    re.compile(r"deals damage equal to .* to each opponent", re.IGNORECASE),
    re.compile(r"each opponent's life total becomes", re.IGNORECASE),
    re.compile(r"infect", re.IGNORECASE),
    re.compile(r"poison counter", re.IGNORECASE),
    # Big-trample finishers — Craterhoof Behemoth, Pathbreaker
    # Ibex, Overwhelming Stampede, End-Raze Forerunners. Two
    # orderings: "get +N/+N and gain trample" OR "gain trample
    # and get +N/+N". Live audit 2026-05-13 caught Craterhoof
    # returning "threat" because its actual oracle says "gain
    # trample and get +X/+X" — opposite order from the original
    # pattern. The ``[\dxn]`` character class accepts both
    # literal-digit pumps and X-based pumps.
    re.compile(
        r"creatures you control "
        r"(?:get \+[\dxn]+/\+[\dxn]+ and gain trample"
        r"|gain trample and get \+[\dxn]+/\+[\dxn]+)",
        re.IGNORECASE,
    ),
    re.compile(r"craterhoof", re.IGNORECASE),
]


def classify_role_extended(oracle_text: str, type_line: str = "") -> str:
    """Expanded role taxonomy. Tries the new (UI-relevant) categories
    first; falls back to the base ``classify_role`` taxonomy.

    Returns one of: ``land_payoff``, ``win_condition``, or whatever
    ``classify_role`` returns (ramp / draw / removal / wipe /
    protection / tutor / finisher / threat / land / other).

    This is the canonical role classifier — both the dashboard's
    Categories panel and the advisor's per-recommendation role tag
    should route through here so the two surfaces never disagree
    about what bucket a card belongs in.
    """
    text = (oracle_text or "").lower()
    if any(p.search(text) for p in _LAND_PAYOFF_PATTERNS):
        return "land_payoff"
    if any(p.search(text) for p in _WIN_CONDITION_PATTERNS):
        return "win_condition"
    return classify_role(oracle_text, type_line)


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

# Tuned-deck saturation points per role. The advisor uses these to
# drop adds whose role bucket is already full in the user's deck.
#
# Recalibrated 2026-05-13 (Phase A gap #4): the original values were
# padded UP to compensate for the role classifier's under-counting
# (e.g. Cyclonic Rift mis-classified as "other" rather than wipe;
# Crux of Fate ditto). With the wipe-pattern fixes + classify_role
# consolidation now in (1.1 / 3.2), the classifier counts are
# accurate, so the thresholds can drop back to the EDH tuned-deck
# norms documented in STAPLES.md:
#
#   ramp: 8-10 standard, 12+ is bloat → threshold 10
#   draw: 8-10 standard, 12+ is bloat → threshold 9
#   removal: 6-8 standard             → threshold 8
#   wipe: 2-4 standard                → threshold 4
#   protection: 3-5 standard          → threshold 5
#   tutor: 1-4 standard (heavy decks
#     legitimately go higher)         → threshold 5
#   finisher: 1-2 specific lose-the-game effects (Coalition
#     Victory, Approach of the Second Sun). NOTE: post-consolidation
#     this is DISTINCT from ``win_condition`` (Craterhoof,
#     Insurrection, infect-pumps) which has its own bucket and
#     never saturates — wincon cards are too heterogeneous to
#     pattern-match a saturation curve.  → threshold 3
#
# Roles not listed here NEVER saturate — see ``is_role_saturated``.
# ``threat``/``land``/``other``/``win_condition``/``land_payoff``
# are deliberately excluded because they don't pattern-match the
# "too many of these" failure mode.
ROLE_SATURATION_THRESHOLDS: dict[str, int] = {
    "ramp": 10,
    "draw": 9,
    "removal": 8,
    "wipe": 4,
    "protection": 5,
    "tutor": 5,
    "finisher": 3,
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
