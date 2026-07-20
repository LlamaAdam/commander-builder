"""Deck-health signals that feed the audit panel's tile row.

These are the higher-ROI deck-construction signals the existing
advisor doesn't surface, identified during the 2026-05-15
deck-building gap analysis:

  - MDFC count (modal double-faced lands like Boseiju Who Endures,
    Bala Ged Recovery -- effectively reduce land count by playing as
    a spell sometimes)
  - Spell density (non-permanent / total ratio -- the spellslinger
    archetype detector; high density nudges Storm/Magecraft/Prowess
    payoffs into scope)
  - Mana sink count (X-spells + uncapped activated abilities -- the
    "what do I do with 12 mana on turn 9?" signal; without these a
    deck flood-outs in long games)
  - Wincon-specific protection (Silence / Veil of Summer / Grand
    Abolisher / Defense Grid / Allosaurus Shepherd -- protects a
    combo turn specifically, distinct from generic hexproof which
    the advisor already counts)
  - Self-mill enablement (Stitcher's Supplier / Satyr Wayfinder /
    Buried Alive / Hermit Druid -- the graveyard-FUEL side, distinct
    from the graveyard-PAYOFF side the theme detector already finds)

Single public entry: ``compute_deck_health(deck_text)``. Returns one
dict the ``/api/audit`` endpoint inlines under ``deck_health``. The
audit-panel UI renders four/five tiles from that dict.

Architecture:

  - Hardcoded sets for cards where named-membership is the right
    signal (MDFCs, wincon protection, self-mill enablers). These
    lists are short, stable across sets, and avoid per-card Scryfall
    round-trips for the common cases.
  - Scryfall lookup (already disk-cached via ``scryfall_client``) for
    the signals that need type-line data (spell density, X-cost
    detection). Graceful fallback when Scryfall is unreachable: the
    signal returns None instead of a misleading zero.

The hardcoded lists are deliberately conservative. False negatives
(missing a card that should be included) are better than false
positives (wrongly flagging a card) because the UI surfaces specific
card names from each list, and a wrong inclusion is visible.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Named-card detection lists
# ---------------------------------------------------------------------------
#
# All lookups are case-folded so the .dck file's casing doesn't matter.
# Entries are stored with canonical casing for display.


# Modal double-faced lands -- the front face is a spell, the back face
# is a basic-ish land. A deck with many of these effectively plays
# more lands AND more spells than the printed deck list suggests, so
# typical 36-38 land guidance can drop to 32-34 without flood risk.
#
# Curated from MTG sets that introduced MDFCs (Zendikar Rising 2020 +
# Kamigawa Neon Dynasty 2022 + later). Conservative: only includes
# MDFCs that see real Commander play.
_MDFC_LANDS = frozenset(c.lower() for c in [
    # Zendikar Rising — Pathways and Modal Land Cycle
    "Sea Gate Restoration",       # back: Sea Gate, Reborn
    "Emeria's Call",              # back: Emeria, Shattered Skyclave
    "Glasspool Mimic",            # back: Glasspool Shore
    "Shatterskull Smashing",      # back: Shatterskull, the Hammer Pass
    "Turntimber Symbiosis",       # back: Turntimber, Serpentine Wood
    "Valakut Awakening",          # back: Valakut Stoneforge
    "Kazandu Mammoth",            # back: Kazandu Valley
    "Felidar Retreat",            # not MDFC, skip
    "Agadeem's Awakening",        # back: Agadeem, the Undercrypt
    "Bala Ged Recovery",          # back: Bala Ged Sanctuary
    "Cleansing Wildfire",         # not MDFC
    "Hagra Mauling",              # back: Hagra Broodpit
    "Khalni Ambush",              # back: Khalni Territory
    "Malakir Rebirth",            # back: Malakir Mire
    "Murasa Rootgrazer",          # not MDFC
    "Ondu Inversion",              # back: Ondu Skyruins
    "Pelakka Predation",           # back: Pelakka Caverns
    "Silundi Vision",              # back: Silundi Isle
    "Skyclave Cleric",             # back: Skyclave Basilica
    "Spikefield Hazard",           # back: Spikefield Cave
    "Tangled Florahedron",         # back: Tangled Vale
    "Vastwood Fortification",      # back: Vastwood Thicket
    "Branchloft Pathway",         # Pathway lands (Pathways are MDFCs)
    "Brightclimb Pathway",
    "Clearwater Pathway",
    "Cragcrown Pathway",
    "Hengegate Pathway",
    "Needleverge Pathway",
    "Riverglide Pathway",
    "Barkchannel Pathway",
    "Blightstep Pathway",
    "Darkbore Pathway",
    # Kamigawa Neon Dynasty -- Channel lands
    "Boseiju, Who Endures",
    "Eiganjo, Seat of the Empire",
    "Otawara, Soaring City",
    "Sokenzan, Crucible of Defiance",
    "Takenuma, Abandoned Mire",
    # Phyrexia: All Will Be One -- single MDFC cycle
    "Mishra's Foundry",            # not MDFC; skip
    # Dominaria United -- "Lair" cycle (Karoo MDFCs)
    "Plaza of Heroes",             # not MDFC
    # Brothers' War (Mishra MDFCs)
    "Argoth, Sanctum of Nature",
    "Urza's Sylex",                # spell, not MDFC
    "Mishra, Lost to Phyrexia",    # spell, not MDFC
    # The Lord of the Rings: Tales of Middle-earth
    "Minas Tirith",                # not MDFC
    # Wilds of Eldraine -- Faceless
    # Murders at Karlov Manor (no major MDFCs)
])
# Filter out the entries I left as "not MDFC; skip" placeholders.
# They're tagged in the comment but easier to enumerate negatively.
# (Keeping the comment + entry shape so a future maintainer can see
# the curation reasoning rather than wondering why X is missing.)
_MDFC_LANDS = frozenset(name for name in _MDFC_LANDS if name not in {
    # NOTE: "skyclave cleric" must NOT be filtered here — it IS a ZNR
    # MDFC (back face: Skyclave Basilica, per the entry comment above);
    # it was wrongly listed among the not-MDFC placeholders.
    "felidar retreat", "cleansing wildfire", "murasa rootgrazer",
    "mishra's foundry", "plaza of heroes",
    "urza's sylex", "mishra, lost to phyrexia", "minas tirith",
    "glasspool mimic",  # mimic IS MDFC, but back face is land --
                         # leave it since real Commander play uses it
})
# Add Glasspool Mimic back (curation note was wrong above).
_MDFC_LANDS = _MDFC_LANDS | frozenset({"glasspool mimic"})


# Wincon-specific protection -- cards that prevent interaction
# during a combo turn or stop opponents from breaking up your
# wincon. Distinct from generic "hexproof on my creatures" (which
# the advisor already counts as "Protection").
_WINCON_PROTECTION = frozenset(c.lower() for c in [
    # Silence-style "opponents can't cast"
    "Silence",
    "Orim's Chant",
    "Abeyance",
    "Angel's Grace",
    "Teferi's Protection",   # phase out everything
    "Veil of Summer",
    "Autumn's Veil",
    "Grand Abolisher",
    "City of Solitude",
    "Dosan the Falling Leaf",
    "Defense Grid",
    "Conqueror's Flail",
    "Vexing Shusher",
    "Allosaurus Shepherd",
    "Cavern of Souls",       # creature-type spells can't be countered
    "Boseiju, Who Shelters All",
    "Dauthi Voidwalker",     # exiles tops to prevent reactive draws
    "Carpet of Flowers",     # niche -- skip for now
    # Modern protection staples for combo turns
    "Pact of Negation",       # free counter for combo turns
    "Force of Will",          # free counter
    "Force of Negation",      # free counter
    "Mindbreak Trap",         # free counter on storm/stack
    "Bind",                   # split-second redirect
    "Spell Pierce",           # mid-stack disruption
    "Flusterstorm",           # stack protection on storm turns
    "Spell Snare",            # cheap stack disruption
])
_WINCON_PROTECTION = _WINCON_PROTECTION - {"carpet of flowers"}


# Self-mill enablers -- cards that put cards from YOUR library into
# YOUR graveyard. Distinct from graveyard PAYOFFS (Living Death,
# reanimation spells) which the theme detector already finds.
#
# Curated to cards that exist primarily for self-mill, not generic
# "mill X target player" which usually targets opponents.
_SELF_MILL_ENABLERS = frozenset(c.lower() for c in [
    # Repeatable self-mill engines
    "Stitcher's Supplier",
    "Satyr Wayfinder",
    "Mesmeric Orb",
    "Hermit Druid",
    "Underrealm Lich",
    "The Gitrog Monster",
    "Sidisi, Brood Tyrant",
    "Splinterfright",
    "Wonder",                # cycle/discard payoff; skip
    "Stinkweed Imp",
    "Golgari Grave-Troll",
    "Life from the Loam",
    "Lord of Extinction",   # not enabler; payoff
    "Cephalid Coliseum",
    "Glimpse the Unthinkable",  # actually mills opp typically
    "Forgotten Creation",
    "Underworld Connections",   # wrong card; skip
    "Insolent Neonate",
    "Tasigur's Cruelty",
    "Boneyard Wurm",            # payoff not enabler
    "Grisly Salvage",
    "Mulch",
    "Drown in the Loch",        # wrong card; skip
    "Buried Alive",
    "Entomb",
    "Liliana of the Veil",      # not really self-mill; skip
    "Lazav, Dimir Mastermind",  # payoff
    "Splendid Reclamation",     # ramp payoff for self-mill
    "Crucible of Worlds",       # graveyard recursion, not enabler
    "Ramunap Excavator",
    "Mind Funeral",             # opp-mill
    "Glimpse of Tomorrow",      # not self-mill
    "Altar of Dementia",        # actually mills opponent typically
    "Tortured Existence",
    "Survival of the Fittest",  # discard, not mill, but adjacent
    "Buried in the Garden",
])
# Strip the entries I added then marked as not-self-mill.
_SELF_MILL_ENABLERS = _SELF_MILL_ENABLERS - {
    "wonder", "lord of extinction", "underworld connections",
    "boneyard wurm", "drown in the loch", "liliana of the veil",
    "lazav, dimir mastermind", "crucible of worlds", "mind funeral",
    "glimpse of tomorrow", "altar of dementia", "glimpse the unthinkable",
}


# ---------------------------------------------------------------------------
# Deck-text parsing
# ---------------------------------------------------------------------------

_MAIN_LINE = re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")


def _iter_main_cards(deck_text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(qty, card_name)`` tuples from the [Main] section.

    Iterates lines in deck order. ``qty`` is the integer prefix.
    ``card_name`` is the name with edition tail stripped, casing
    preserved from the file. Skips section headers, metadata, and
    blank lines. Same parsing convention as the rest of the project
    (see web/_helpers.py's ``_apply_swaps_to_dck``).
    """
    in_main = False
    for raw in deck_text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            in_main = s.lower() == "[main]"
            continue
        if not in_main:
            continue
        m = _MAIN_LINE.match(s)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        name = m.group(2).strip()
        if not name:
            continue
        yield qty, name


# ---------------------------------------------------------------------------
# Named-card signals (MDFC / wincon protection / self-mill)
# ---------------------------------------------------------------------------

def _count_named_matches(
    deck_text: str, known_set: frozenset[str],
) -> tuple[int, list[str]]:
    """Walk the [Main] section and count cards matching a known set.

    Returns ``(total_quantity, matched_names)`` where:
      - total_quantity sums quantities across all matching lines.
      - matched_names is the deck-order list of canonical card names
        that matched (one entry per matching LINE, not per copy --
        the UI shows distinct cards).
    """
    total = 0
    names: list[str] = []
    seen_lower: set[str] = set()
    for qty, name in _iter_main_cards(deck_text):
        key = name.lower()
        if key in known_set:
            total += qty
            if key not in seen_lower:
                seen_lower.add(key)
                names.append(name)
    return total, names


def count_mdfc_lands(deck_text: str) -> dict:
    """Count modal double-faced lands in the [Main] section.

    Returns ``{"count": int, "cards": [str, ...]}``. The UI tile
    surfaces 'count' and the tooltip lists 'cards'. A B4 deck with
    6+ MDFCs effectively runs 2-3 fewer lands than the printed
    count suggests.
    """
    count, names = _count_named_matches(deck_text, _MDFC_LANDS)
    return {"count": count, "cards": names}


def count_wincon_protection(deck_text: str) -> dict:
    """Count wincon-specific protection cards (Silence / Veil of
    Summer / Grand Abolisher / Defense Grid / Allosaurus Shepherd /
    Pact of Negation / Force of Will, etc.).

    Distinct from generic hexproof / ward / counterspell density
    (which the advisor's existing 'Protection' bucket counts).
    Wincon-protection answers the specific question: "does this
    deck have a card it can hold up on its combo turn that lets the
    spells resolve uninterrupted?" A B4 combo deck without any of
    these is brittle to interaction.
    """
    count, names = _count_named_matches(deck_text, _WINCON_PROTECTION)
    return {"count": count, "cards": names}


def count_self_mill_enablers(deck_text: str) -> dict:
    """Count cards that put your own library into your graveyard.

    The advisor's theme detector already flags graveyard-payoff
    decks (Living Death, reanimation spells, dredge payoffs). What's
    missing is the FUEL side -- does this deck have ways to put cards
    into the graveyard at speed? A reanimator without self-mill is
    a Buried-Alive-or-bust deck; with Stitcher's Supplier + Satyr
    Wayfinder + Mesmeric Orb + Hermit Druid it's a real reanimator
    shell.
    """
    count, names = _count_named_matches(deck_text, _SELF_MILL_ENABLERS)
    return {"count": count, "cards": names}


# ---------------------------------------------------------------------------
# Scryfall-typed signals (spell density, mana sinks)
# ---------------------------------------------------------------------------

def _lookup_card_safe(name: str):
    """Wrap scryfall_client.lookup_card with a try/except so a network
    blip on one card doesn't poison the whole deck-health computation.
    Returns the card dict or None."""
    try:
        from .scryfall_client import lookup_card
        return lookup_card(name)
    except Exception:  # noqa: BLE001 -- caller can survive nulls
        return None


def compute_spell_density(deck_text: str) -> Optional[dict]:
    """Ratio of non-permanent (instant + sorcery) to total main cards.

    Returns ``{
        "non_permanent_count": int,
        "total_main_count": int,
        "ratio": float | None,
        "lookup_failures": int,
    }`` — or ``None`` when Scryfall lookups failed for MORE than half
    the deck's card lines. That's the module docstring's outage
    contract: "Scryfall unreachable → the signal returns None instead
    of a misleading zero." Before this guard an all-lookups-fail
    outage produced ``ratio == 0.0`` ("0% spells") on a perfectly
    healthy deck — indistinguishable from a genuinely spell-free deck
    and rendered with warn styling in the UI.

    Below the outage threshold, ``ratio`` is computed from the cards
    Scryfall COULD classify (failed lookups drop out of both numerator
    and denominator — an unknown card must not count as "permanent"),
    and ``lookup_failures`` carries the number of card lines that
    missed so the UI can annotate the tile. ``total_main_count`` stays
    the full printed deck size. ``ratio`` is None when nothing could
    be classified or the deck is empty (defensive).

    Spellslinger archetypes typically run 20-30%+ non-permanents.
    The advisor's theme detector flags spellslinger from add-pool
    composition; this metric measures whether the user's deck
    actually has the spell density to back it up.
    """
    non_perm = 0
    total = 0            # full printed quantity across [Main]
    classified_qty = 0   # quantity backed by a successful lookup
    lines = 0            # lookup attempts (one per deck line)
    failed_lines = 0     # lookups that returned None (miss or outage)
    for qty, name in _iter_main_cards(deck_text):
        total += qty
        lines += 1
        card = _lookup_card_safe(name)
        if card is None:
            failed_lines += 1
            continue
        classified_qty += qty
        type_line = (card.get("type_line") or "").lower()
        # "Instant" and "Sorcery" are non-permanent. Lands, creatures,
        # artifacts, enchantments, planeswalkers, battles, tribals
        # all become permanents on resolution.
        if "instant" in type_line or "sorcery" in type_line:
            non_perm += qty
    # Outage detection: a majority of lookups failing means Scryfall is
    # effectively unreachable (a single typo'd/custom card can't trip
    # this). Half-or-fewer misses are tolerable noise; MORE than half
    # means any computed ratio would be dominated by guesswork.
    if lines and failed_lines * 2 > lines:
        return None
    return {
        "non_permanent_count": non_perm,
        "total_main_count": total,
        "ratio": (non_perm / classified_qty) if classified_qty > 0 else None,
        "lookup_failures": failed_lines,
    }


# Mana-cost regex: matches `{X}` symbol in a card's mana_cost field.
# Scryfall uses curly-brace notation: `{X}{R}` for Lightning Bolt-style
# X spells, `{2}{R}{R}` for fixed-cost. We're looking for the literal
# `{X}` token to identify variable-cost spells.
_X_COST_RE = re.compile(r"\{X\}")

# Pure-mana activated-ability cost: an activation whose cost is one or
# more mana symbols (digits, color letters, hybrid slash, Phyrexian,
# snow) followed by a colon, with NO ``{T}`` / ``{Q}`` and no non-mana
# cost segment. Catches Walking Ballista's ``{4}: ...``, Spikeshot
# Goblin's ``{R}: ...``, Inkmoth Nexus's ``{1}: ...``, etc.
_MANA_SINK_ACTIVATION_RE = re.compile(
    r"\{[0-9XWUBRGCPS/]+\}(?:\s*,\s*\{[0-9XWUBRGCPS/]+\})*\s*:",
)

# Used by the self-untap-loop heuristic to detect any mana symbol in
# the cost segment of an activated ability (including ``{N}, {T}:``).
_ANY_MANA_SYMBOL_RE = re.compile(r"\{[0-9XWUBRGCPS/]+\}")


def _has_self_untap_loop(card_name: str, oracle_text: str) -> bool:
    """Staff-of-Domination pattern: at least one activated ability has
    mana in its cost AND the oracle text contains ``Untap <self_name>``.
    The self-untap recycles the tap, so arbitrary mana can be poured
    into the prior activations over a single turn.

    The substring check on the literal card name keeps the heuristic
    narrow: ``Untap target creature`` and similar generic effects
    don't match.
    """
    if not card_name or not oracle_text:
        return False
    if f"Untap {card_name}" not in oracle_text:
        return False
    for line in oracle_text.split("\n"):
        if ":" not in line:
            continue
        cost = line.split(":", 1)[0]
        if _ANY_MANA_SYMBOL_RE.search(cost):
            return True
    return False


def count_mana_sinks(deck_text: str) -> Optional[dict]:
    """Count cards that can repeatedly consume mana for value.

    Three heuristics, OR'd per card:

    1. ``{X}`` in mana_cost — X-spells (Genesis Wave, Comet Storm,
       Walking Ballista, Hangarback Walker, Profane Command,
       Pull from Tomorrow, etc.). The dominant "what do I do with
       12 mana on turn 9" category.
    2. Pure-mana activated ability — oracle text contains
       ``{cost}: ...`` where the cost is mana-only (no ``{T}``).
       Catches Spikeshot Goblin (``{R}:``), Inkmoth Nexus (``{1}:``),
       Walking Ballista's ``{4}:`` add-counter ability, etc. (X-cost
       creatures hit both #1 and #2; deduped via ``seen_lower``.)
    3. Self-untap loop — ``Untap <self_name>`` clause plus at least
       one activated ability with mana in its cost. Catches Staff of
       Domination's loop (every ability is ``{N}, {T}:`` but the
       self-untap recycles the tap).

    Returns ``{"count": int, "cards": [str, ...], "lookup_failures":
    int}`` — or ``None`` when Scryfall lookups failed for MORE than
    half the deck's card lines. Same outage contract (and same
    majority threshold) as ``compute_spell_density``: an outage used
    to yield ``{"count": 0}``, which the UI rendered as a warn-flavored
    "no mana sinks" on decks that simply couldn't be classified.
    Below the threshold the count comes from the cards that DID
    resolve, with ``lookup_failures`` noting how many lines missed.
    """
    count = 0
    names: list[str] = []
    seen_lower: set[str] = set()
    lines = 0            # lookup attempts (one per deck line)
    failed_lines = 0     # lookups that returned None (miss or outage)
    for qty, name in _iter_main_cards(deck_text):
        lines += 1
        card = _lookup_card_safe(name)
        if card is None:
            failed_lines += 1
            continue
        # mana_cost is the printed cost. card_faces[0].mana_cost for
        # MDFCs; we check both to catch the front face of MDFC X-spells.
        mana_cost = card.get("mana_cost") or ""
        if not mana_cost:
            faces = card.get("card_faces") or []
            if faces:
                mana_cost = (faces[0] or {}).get("mana_cost") or ""
        # oracle_text similarly may live on either the top level (most
        # cards) or split across ``card_faces`` (MDFCs, split, adventure).
        oracle_text = card.get("oracle_text") or ""
        if not oracle_text:
            faces = card.get("card_faces") or []
            if faces:
                oracle_text = "\n".join(
                    (f or {}).get("oracle_text") or "" for f in faces
                )
        card_name = card.get("name") or name
        is_sink = bool(
            _X_COST_RE.search(mana_cost)
            or _MANA_SINK_ACTIVATION_RE.search(oracle_text)
            or _has_self_untap_loop(card_name, oracle_text)
        )
        if is_sink:
            count += qty
            key = name.lower()
            if key not in seen_lower:
                seen_lower.add(key)
                names.append(name)
    # Outage detection — mirrors compute_spell_density: majority of
    # lookups failing means "can't classify this deck", not "this deck
    # has zero mana sinks". Returning None lets the UI say
    # "unavailable" instead of scolding a healthy deck.
    if lines and failed_lines * 2 > lines:
        return None
    return {"count": count, "cards": names, "lookup_failures": failed_lines}


# ---------------------------------------------------------------------------
# Aggregator -- the single public entry the audit route calls
# ---------------------------------------------------------------------------

def compute_deck_health(deck_text: str) -> dict:
    """Compute all deck-health signals for the audit panel tile row.

    Returns a single dict the ``/api/audit`` endpoint inlines under
    ``deck_health``. The UI renders one tile per top-level key.

    Performance note: this walks the deck text once per signal and
    Scryfall-looks-up each unique card for the type-based signals.
    The scryfall_client is already disk-cached so subsequent audits
    of the same deck are near-instant; the first run on a fresh
    deck takes a few seconds for the lookups to populate.

    Any individual signal that fails (e.g. Scryfall outage) returns
    its empty/null shape so the rest of the panel still renders. For
    the Scryfall-typed signals (``spell_density``, ``mana_sinks``)
    that null shape is literally ``None`` — a majority-of-lookups-fail
    outage must NOT masquerade as "0% spells" / "0 sinks" (the module
    docstring's contract). The UI renders None as an explicit
    "unavailable" tile.
    """
    return {
        "mdfc": count_mdfc_lands(deck_text),
        "spell_density": compute_spell_density(deck_text),
        "mana_sinks": count_mana_sinks(deck_text),
        "wincon_protection": count_wincon_protection(deck_text),
        "self_mill": count_self_mill_enablers(deck_text),
        # Role target ratios (F2): flag roles below the gold-standard
        # template minimums (ramp/draw/removal/wipe/protection). The
        # complement of the saturation guard, which flags excess.
        "role_targets": _role_targets_signal(deck_text),
    }


def _role_targets_signal(deck_text: str) -> dict:
    """Deck-health signal: role counts vs ROLE_TARGETS minimums. Degrades
    to an empty shape on any failure so the rest of the panel renders."""
    try:
        from .staples import role_target_report
        names = [name for _qty, name in _iter_main_cards(deck_text)]
        return role_target_report(names)
    except Exception:  # noqa: BLE001
        return {"roles": {}, "under_built": []}
