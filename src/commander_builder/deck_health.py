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
  - Opening-hand consistency (2026-08 wiring of ``consistency.py`` --
    keepable-opener / mulligan-rate / land-drop / commander-on-curve
    probabilities from that module's seeded Monte Carlo). ADDITIVE
    ONLY, by decision: the signal is REPORTED alongside the others but
    deliberately NOT folded into ``compute_health_grade`` -- adding a
    component (or reweighting an existing one) would silently re-grade
    every deck the day the wiring landed, which is exactly the kind of
    unattributable drift the grade's pinned calibration tests exist to
    prevent. If it ever earns a grade weight, that goes in as its own
    reviewed, test-repinned change.

Single public entry: ``compute_deck_health(deck_text)``. Returns one
dict the ``/api/audit`` endpoint inlines under ``deck_health``. The
audit-panel UI renders one tile per top-level key.

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

from . import dck_utils
from .deck_builder_manabase import LAND_COUNT_BAND


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

# Kept for backwards compatibility; canonical copy lives in dck_utils.
_MAIN_LINE = dck_utils.CARD_LINE_RE


def _iter_main_cards(deck_text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(qty, card_name)`` tuples from the [Main] section.

    Iterates lines in deck order. ``qty`` is the integer prefix.
    ``card_name`` is the name with edition tail stripped, casing
    preserved from the file. Skips section headers, metadata, and
    blank lines. Same parsing convention as the rest of the project
    (see web/_helpers.py's ``_apply_swaps_to_dck``).

    Thin wrapper over ``dck_utils.iter_main_cards``.
    """
    return dck_utils.iter_main_cards(deck_text)


def _iter_commander_cards(deck_text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(qty, card_name)`` tuples from the [Commander] section.

    WHY THIS EXISTS AT ALL: until 2026-07 this module read ``[Main]`` and
    nothing else — every signal, every grade component, every reason
    string. In Commander that is a structural blind spot, because the
    commander is the one card you are guaranteed to have in every game:
    a 5-mana commander with 8 ramp graded identically to a 2-mana one,
    and a deck whose commander IS the draw engine (Edric, Tatyova) was
    still scolded for a ``draw`` deficit it did not have.

    WHY NOT ``deck_library_analyzer.iter_deck_cards``: that walker is the
    canonical ``[Commander]`` + ``[Main]`` reader and the manabase report
    uses it precisely because pip math wants both sections merged. The
    health module needs them SEPARATED — the commander is credited
    differently from a mainboard card, so merging would destroy exactly
    the distinction being added. Same ``dck_utils`` primitives, one
    section over.
    """
    for line in dck_utils.iter_section_lines(deck_text, "Commander"):
        parsed = dck_utils.parse_card_line(line)
        if parsed is None:
            continue
        qty, name = parsed
        if name:
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
# Consistency signal -- opening-hand math from consistency.py (2026-08)
# ---------------------------------------------------------------------------

#: Trial count for the consistency signal's seeded Monte Carlo. Smaller
#: than ``consistency.DEFAULT_TRIALS`` (10k) because this runs inside a
#: synchronous audit request: 2000 trials puts the standard error near
#: 1.1pp at p=0.5, already below what a percent-formatted tile can
#: display, at a fifth of the CPU cost.
_CONSISTENCY_TRIALS = 2_000

#: Fixed seed so the tile is deterministic: same deck text ⇒ identical
#: numbers on every audit. The consistency module's own determinism
#: contract (``random.Random(seed)`` only), honored at this call site.
_CONSISTENCY_SEED = 0


def consistency_signal(deck_text: str) -> Optional[dict]:
    """Deck-health signal: opening-hand consistency via ``consistency.py``.

    A compact projection of ``consistency.opening_hand_stats`` -- the
    keepable-opener / mulligan / land-drop / commander-on-curve numbers
    the audit panel's tile renders, quoted at that module's documented
    default convention (on the play, the harsher read). The commander
    comes from the deck's own ``[Commander]`` section; card data routes
    through the same disk-cached ``_lookup_card_safe`` path as every
    other Scryfall-typed signal here (it is ``opening_hand_stats``'s
    default lookup).

    Returns ``None`` under the standard outage contract (empty deck, or
    a MAJORITY of card lines unresolved -- ``opening_hand_stats`` applies
    the same majority-failure guard as ``compute_spell_density``), and
    on any unexpected exception: an unavailable signal must degrade to
    ``None``, never a fabricated 0.0 and never a traceback out of the
    audit path. Below the outage threshold ``lookup_failures`` rides
    along so the UI can annotate a pessimistically-biased reading.

    ``p_commander_on_curve`` is independently ``None`` inside an
    otherwise-available signal when the commander itself can't be
    resolved (that module's per-metric contract).

    ADDITIVE ONLY: reported alongside the other signals, deliberately
    NOT an input to ``compute_health_grade`` -- see the module
    docstring's consistency bullet for the re-grading rationale.
    """
    try:
        from .consistency import opening_hand_stats
        stats = opening_hand_stats(
            deck_text, trials=_CONSISTENCY_TRIALS, seed=_CONSISTENCY_SEED,
        )
    except Exception:  # noqa: BLE001 -- degrade, never break the panel
        return None
    if stats is None:
        return None
    return {
        "p_keepable_7": stats["p_keepable_7"],
        "mulligan_rate": stats["mulligan_rate"],
        "avg_lands_in_7": stats["avg_lands_in_7"],
        "p_3_lands_by_t3": stats["p_3_lands_by_t3"],
        "p_5_lands_by_t5": stats["p_5_lands_by_t5"],
        "p_commander_on_curve": stats["p_commander_on_curve"],
        "p_color_screw": stats["p_color_screw"],
        "convention": stats["convention"],
        "trials": stats["trials"],
        "seed": stats["seed"],
        "lookup_failures": stats["lookup_failures"],
    }


def _consistency_targets_signal(
    deck_text: str, consistency: Optional[dict],
) -> Optional[dict]:
    """Deck-health signal: primer-derived consistency floors (FP-019.2).

    Thin degrade wrapper over
    ``consistency_targets.evaluate_consistency_targets`` — the module
    named after the table it grades against. ``consistency`` is the
    projection ``consistency_signal`` already computed this audit; the
    evaluator reads two of its numbers and computes the closed-form
    checks itself through the same ``_lookup_card_safe`` path.

    ``None`` on any failure: an unavailable signal must degrade, never
    fabricate and never traceback out of the audit path.
    """
    try:
        from . import consistency_targets as ct
        return ct.evaluate_consistency_targets(
            deck_text, consistency=consistency,
        )
    except Exception:  # noqa: BLE001 -- degrade, never break the panel
        return None


def _nonbo_signal(deck_text: str) -> Optional[list]:
    """Deck-health signal: §14 nonbo lint findings (FP-019.5).

    Thin degrade wrapper over ``nonbo_lint.lint_deck_text``, which
    resolves cards through this module's ``_lookup_card_safe`` by
    default. An empty list is a real "no conflicts found"; ``None`` is
    the wrapper's own outage shape (unexpected exception only — the
    linter itself degrades per-card).
    """
    try:
        from . import nonbo_lint as nl
        return nl.lint_deck_text(deck_text)
    except Exception:  # noqa: BLE001 -- degrade, never break the panel
        return None


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
    # Computed once: the projection feeds its own tile AND the FP-019.2
    # targets tile, which grades two of its numbers against the
    # primer-derived floors without paying for a second Monte Carlo.
    consistency = consistency_signal(deck_text)
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
        # Opening-hand consistency (consistency.py, wired 2026-08).
        # Schema-additive: a new reported key, NOT a grade input --
        # compute_health_grade must keep ignoring it (see module
        # docstring). None under the outage contract.
        "consistency": consistency,
        # Primer-derived consistency floors (FP-019.2, consistency_targets
        # module). Same doctrine as the consistency tile: reported,
        # NEVER a grade input. None under the outage contract.
        "consistency_targets": _consistency_targets_signal(
            deck_text, consistency),
        # Nonbo lint (FP-019.5, nonbo_lint module): §14 self-conflict
        # pairs. A list (possibly empty) of fired rules; None only on
        # unexpected failure. Reported only — never a grade input.
        "nonbos": _nonbo_signal(deck_text),
    }


def _role_targets_signal(deck_text: str) -> dict:
    """Deck-health signal: role counts vs ROLE_TARGETS minimums. Degrades
    to an empty shape on any failure so the rest of the panel renders.

    The ``[Commander]`` section is threaded through so a commander that
    FILLS a role shrinks that role's effective target (see
    ``staples.COMMANDER_ROLE_CREDIT`` for the reasoning and the size of
    the credit). A deck file with no readable command zone passes an
    empty list and gets the unmodified targets — the pre-2026-07
    behavior, unchanged.
    """
    try:
        from .staples import role_target_report
        names = [name for _qty, name in _iter_main_cards(deck_text)]
        commanders = [name for _qty, name in _iter_commander_cards(deck_text)]
        return role_target_report(names, commanders)
    except Exception:  # noqa: BLE001
        return {"roles": {}, "under_built": []}


# ---------------------------------------------------------------------------
# Health grade -- ManaFoundry-parity letter grade over the signals above
# ---------------------------------------------------------------------------
#
# ``compute_health_grade`` compresses the panel's individual signals into
# ONE at-a-glance letter (A..F) with the top reasons the deck lost
# points. It lives here (not in the route layer) because this module
# owns the signals; the grade is nothing but their aggregation.

# Component weights -- THE single source of truth for how much each
# component contributes to the 0-100 score. Why these numbers:
#
#   role_deficits (0.40)        -- the broadest construction measure we
#       have: ramp/draw/removal/wipe/protection/finisher counts vs the
#       gold-standard template minimums (staples.ROLE_TARGETS). A deck
#       missing its engine roles misfires every game, so this is the
#       heaviest component. UNCHANGED at 0.40 across the 2026-07
#       rebalance: the two new components were funded out of the other
#       two so this one's calibration (a deck missing ramp AND draw
#       lands in C, see _GRADE_BANDS) survives intact.
#   mana_health (0.22)          -- mana quality, now TWO halves averaged
#       (see _score_mana_health): the land COUNT vs the healthy
#       Commander band (MDFC-adjusted), and the per-color SOURCE counts
#       vs Frank Karsten's targets. Before 2026-07 only the count half
#       existed, so a Command Tower and a Wastes scored identically and
#       a three-color deck with 37 Mountains graded a perfect 100 on
#       mana. Trimmed 0.25 -> 0.22 to help fund commander_alignment;
#       the component now measures strictly more than it used to.
#   construction_signals (0.28) -- the tile row's objectively
#       "warn"-able signals: mana sinks + wincon protection. These are
#       the two tiles deck_health_ui.js is willing to paint warn-red
#       when absent. The other tiles (MDFC, spell density, self-mill)
#       are deliberately EXCLUDED from the grade: the UI never assigns
#       them worse than "muted" because their absence is
#       archetype-dependent (a stax deck doesn't want self-mill; a
#       creature deck doesn't want 25% instants), so counting them
#       would punish healthy decks for not being spellslinger/
#       graveyard decks. Trimmed 0.35 -> 0.28: these are two curated
#       name-list counts, and it was hard to defend them outweighing
#       all of mana quality once mana quality became real.
#   commander_alignment (0.10)  -- does the deck's ramp match what its
#       COMMANDER costs. New in 2026-07 and deliberately the smallest
#       weight: it is one focused question (an expensive commander you
#       cast on turn 8 loses to one you cast on turn 4), not a broad
#       survey, and it is the newest / least-validated model here. It
#       is a separate component rather than a role_deficits tweak
#       because it must be able to go UNAVAILABLE on its own -- a deck
#       file with no readable [Commander] section still deserves a
#       grade for everything else.
#
# Weights sum to 1.0 (pinned by a test). When a component's underlying
# signal is unavailable (the Scryfall-outage None contract), that
# component is EXCLUDED and the remaining weights are renormalized --
# an outage must never read as an unhealthy deck.
_GRADE_WEIGHTS: dict[str, float] = {
    "role_deficits": 0.40,
    "mana_health": 0.22,
    "construction_signals": 0.28,
    "commander_alignment": 0.10,
}

# Flavor -> sub-score mapping for the construction signals. These reuse
# the EXACT count cutoffs deck_health_ui.js applies to the mana-sink and
# wincon-protection tiles (>=3 good, >=1 neutral, 0 warn) rather than
# inventing parallel thresholds. The 100/70/30 spacing is chosen so a
# component that is all-good sits in the A band, all-neutral lands
# around C, and all-warn drags hard toward F.
_FLAVOR_SCORES: dict[str, int] = {"good": 100, "neutral": 70, "warn": 30}

# Healthy effective-land band for a Commander deck. The module docstring
# (MDFC section) documents the underlying guidance: 36-38 lands is the
# classic default, and an MDFC-heavy deck legitimately drops to 32-34
# because each spell-front MDFC is "half a land". Counting MDFC spell
# fronts at 0.5 lands each and accepting the band covers both shapes.
# Each effective land outside the band costs ``_LAND_BAND_PENALTY``
# points (linear, floored at 0) -- steep enough that a 27-land greed
# manabase scores under 30.
#
# 2026-08 reconciliation: the band IS the builder's clamp band,
# imported from ``deck_builder_manabase.LAND_COUNT_BAND`` (33-40).
# Before sharing, this module graded against a hand-kept (33, 38)
# while ``deck_builder_manabase`` clamped its own builds to 33-40 --
# so a 40-land high-curve build the app itself assembled was docked
# ~24 points on the land half of mana_health. One constant, one
# owner (the builder, whose land-count model defines "sane"), zero
# drift. NOTE: seed-trusted builds may still carry up to 42 lands
# (the builder trusts a community-tuned seed count in 33-42); 41-42
# is charged the mild linear penalty, which is intentional -- the
# band covers what OUR model would choose, the seed trust is a
# deliberate exception.
_LAND_BAND: tuple[int, int] = LAND_COUNT_BAND
_LAND_BAND_PENALTY: float = 12.0

# Commander-cost -> ramp expectation (the ``commander_alignment``
# component's whole model).
#
# The pivot is 3.5 mana, the same pivot deck_builder_manabase's curve
# model uses for land counts, and the baseline expectation at the pivot
# is exactly staples.ROLE_TARGETS["ramp"] (10) -- a 3.5-MV commander is
# the "normal" deck the published template ratios describe, so at the
# pivot this component agrees with role_deficits by construction and
# only adds information away from it. The slope (1.5 ramp pieces per
# mana) is gentler than the land model's 2-per-mana because ramp
# competes with spells for the same 99 slots while lands have their own
# budget.
#
# The band caps the demand at 12. Past that the marginal ramp piece is
# worse than a spell, and the advisor's redundancy guard
# (ROLE_SATURATION_THRESHOLDS["ramp"] == 10) already refuses adds well
# before then -- the two numbers answer different questions (how much
# does this commander DEMAND vs is one MORE piece worth a slot), and 12
# keeps the gap between them to the couple of pieces a genuine 7-drop
# deck really does run. The 6 floor stops a 1-MV commander from making
# ramp look optional.
_COMMANDER_RAMP_PIVOT_MV: float = 3.5
_COMMANDER_RAMP_PER_MV: float = 1.5
_COMMANDER_RAMP_BAND: tuple[float, float] = (6.0, 12.0)

# Letter bands over the weighted 0-100 score. Tuned so that a deck
# missing BOTH engine roles (ramp and draw at zero) with everything
# else perfect computes to ~77 and lands in C -- "playable but the
# engine is missing" -- rather than B. Descending order matters.
#   >= 90  A   template-clean deck
#   >= 80  B   minor gaps
#   >= 65  C   real construction problems
#   >= 50  D   multiple core failures
#   else   F   fundamentally unbuilt
_GRADE_BANDS: tuple[tuple[int, str], ...] = (
    (90, "A"), (80, "B"), (65, "C"), (50, "D"),
)


def _mana_health_signal(deck_text: str) -> Optional[dict]:
    """Land-count signal feeding the grade's ``mana_health`` component.

    Walks [Main] once, classifying each card by its FRONT-face type
    line ("Sorcery // Land" MDFCs are spells you sometimes play as
    lands, so the front face decides). Cards whose front face is a
    land count fully; cards in the curated MDFC list whose front face
    is a SPELL count as half a land (``effective_lands``), matching
    the module docstring's "6+ MDFCs ~= 2-3 fewer lands" guidance.

    Same Scryfall-outage contract as compute_spell_density /
    count_mana_sinks: MORE than half the lookups failing returns
    ``None`` (signal unavailable), never a fabricated low land count.
    An empty deck is also ``None`` -- there is nothing to grade.
    """
    lands = 0
    mdfc_spell_fronts = 0
    lines = 0
    failed_lines = 0
    for qty, name in _iter_main_cards(deck_text):
        lines += 1
        card = _lookup_card_safe(name)
        if card is None:
            failed_lines += 1
            continue
        type_line = card.get("type_line") or ""
        if not type_line:
            # MDFC/split layouts sometimes carry type only per-face.
            faces = card.get("card_faces") or []
            if faces:
                type_line = " // ".join(
                    (f or {}).get("type_line") or "" for f in faces
                )
        front = type_line.split("//")[0].lower()
        if "land" in front:
            lands += qty
        elif name.lower() in _MDFC_LANDS:
            # Spell-front MDFC (Bala Ged Recovery class): half a land.
            mdfc_spell_fronts += qty
    if not lines:
        return None  # empty deck -> nothing to measure
    if failed_lines * 2 > lines:
        return None  # outage contract: majority of lookups failed
    return {
        "lands": lands,
        "mdfc_spell_fronts": mdfc_spell_fronts,
        "effective_lands": lands + 0.5 * mdfc_spell_fronts,
        "lookup_failures": failed_lines,
    }


def _manabase_signal(deck_text: str) -> Optional[dict]:
    """Karsten color-source signal feeding the ``mana_health`` component.

    Delegates wholesale to ``deck_builder_manabase.manabase_report``,
    which runs the assembler's own ``pip_stats`` ->
    ``color_source_targets`` -> ``land_color_sources`` pipeline over the
    existing deck. No mana math lives here; this is a fail-quiet adapter
    (an unexpected exception inside the report must not cost the deck its
    whole grade, same contract as every other signal in this module).

    ``None`` when the report is unavailable (outage / unparseable deck)
    OR when the deck has no colored requirements at all -- a colorless
    Karn deck has no Karsten target to miss, and scoring "no
    requirements" as a perfect 100 would hand every colorless deck free
    points it did not earn.
    """
    try:
        from .deck_builder_manabase import manabase_report
        report = manabase_report(deck_text, lookup=_lookup_card_safe)
    except Exception:  # noqa: BLE001
        return None
    if not report or not report.get("per_color"):
        return None
    return report


def _score_manabase(manabase: Optional[dict]) -> Optional[float]:
    """0-100 score from the ``manabase_report`` shape (None -> None).

    Target-weighted exactly like ``_score_role_deficits``: one source
    short of a 30-source triple-pip requirement is a smaller failure
    than one short of a 14-source splash, and dividing summed deficits
    by summed targets says so without a second tuning knob.
    """
    if not manabase:
        return None
    total_target = int(manabase.get("total_target") or 0)
    if total_target <= 0:
        return None  # colors with no demand -> nothing measured.
    total_deficit = int(manabase.get("total_deficit") or 0)
    return 100.0 * (1.0 - min(1.0, total_deficit / total_target))


def _commander_mana_value(deck_text: str) -> Optional[float]:
    """Mana value of the most expensive commander, or None.

    PARTNERS: the MAX rather than the sum. Each half of a partner pair
    is cast on its own, so the deck's hardest cast is what its ramp has
    to reach; summing would demand a manabase for a card that does not
    exist.

    Prefers Scryfall's structured ``cmc``; falls back to parsing
    ``mana_cost`` through the manabase module's existing cost parser
    rather than adding a second mana-cost regex to this file. Returns
    None when no commander line resolves -- the component then reads as
    unavailable rather than assuming a cheap commander.
    """
    best: Optional[float] = None
    for _qty, name in _iter_commander_cards(deck_text):
        card = _lookup_card_safe(name)
        if not card:
            continue
        mv = card.get("cmc")
        if not isinstance(mv, (int, float)):
            cost = card.get("mana_cost") or ""
            if not cost.strip():
                # No cmc AND no printed cost: the card resolved but its
                # cost didn't. Skip rather than read it as a free
                # commander -- a fabricated 0 would hand the deck a
                # perfect alignment score it never earned.
                continue
            try:
                from .deck_builder_manabase import _parse_cost
                _pips, mv = _parse_cost(cost)
            except Exception:  # noqa: BLE001
                continue
        mv = float(mv)
        if best is None or mv > best:
            best = mv
    return best


def _expected_ramp_for_commander(commander_mv: float) -> float:
    """Ramp pieces a ``commander_mv``-cost commander wants. See the
    ``_COMMANDER_RAMP_*`` constants for the model and its bounds."""
    modelled = (
        _COMMANDER_RAMP_PER_MV * (commander_mv - _COMMANDER_RAMP_PIVOT_MV)
    )
    from .staples import ROLE_TARGETS
    lo, hi = _COMMANDER_RAMP_BAND
    return max(lo, min(hi, ROLE_TARGETS.get("ramp", 10) + modelled))


def _score_commander_alignment(
    commander_mv: Optional[float], role_targets: Optional[dict],
) -> tuple[Optional[float], Optional[dict]]:
    """0-100 commander-cost-vs-ramp alignment + the detail for reasons.

    Returns ``(score, detail)`` where detail carries ``commander_mv`` /
    ``ramp`` / ``expected_ramp`` for the reason string, or
    ``(None, None)`` when either input is unavailable: no resolvable
    commander (headless deck file, Scryfall outage) or a degraded
    role-target signal with no ramp count. Never a fabricated zero --
    "we could not read your commander" is not "your ramp is broken".

    The ramp count is READ FROM the role-target signal rather than
    recomputed, so the two components can never disagree about how much
    ramp the deck has. Note the count is the deck's own ramp only: the
    commander's role credit adjusts TARGETS, not counts, so a ramp
    commander cannot pay for its own ramp requirement here.
    """
    if commander_mv is None:
        return None, None
    roles = (role_targets or {}).get("roles") or {}
    ramp = roles.get("ramp")
    if not isinstance(ramp, dict):
        return None, None
    count = int(ramp.get("count", 0) or 0)
    expected = _expected_ramp_for_commander(commander_mv)
    if expected <= 0:  # pragma: no cover - band floor is 6
        return None, None
    detail = {
        "commander_mv": commander_mv,
        "ramp": count,
        "expected_ramp": expected,
    }
    return 100.0 * min(1.0, count / expected), detail


def _score_role_deficits(role_targets: Optional[dict]) -> Optional[float]:
    """0-100 score from the role_target_report shape.

    Target-weighted: score = 100 * (1 - total_deficit / total_target),
    so the big-target engine roles (ramp 10, draw 10) dominate the
    small ones (wipe 3) in proportion to how much the template says
    they matter. Reuses ROLE_TARGETS via the report -- no parallel
    thresholds. ``None`` (unavailable) when the roles dict is empty,
    which is _role_targets_signal's documented degraded shape.
    """
    roles = (role_targets or {}).get("roles") or {}
    if not roles:
        return None
    total_target = sum(int(v.get("target", 0) or 0) for v in roles.values())
    if total_target <= 0:
        return None
    total_deficit = sum(int(v.get("deficit", 0) or 0) for v in roles.values())
    return 100.0 * (1.0 - min(1.0, total_deficit / total_target))


def _score_land_band(mana: Optional[dict]) -> Optional[float]:
    """0-100 score from the ``_mana_health_signal`` shape (None -> None).

    100 inside the ``_LAND_BAND`` effective-land band; linear penalty
    of ``_LAND_BAND_PENALTY`` per effective land outside; floored at 0.
    """
    if not mana:
        return None
    eff = float(mana.get("effective_lands") or 0.0)
    lo, hi = _LAND_BAND
    if lo <= eff <= hi:
        return 100.0
    distance = (lo - eff) if eff < lo else (eff - hi)
    return max(0.0, 100.0 - _LAND_BAND_PENALTY * distance)


def _score_mana_health(
    mana: Optional[dict], manabase: Optional[dict] = None,
) -> Optional[float]:
    """The ``mana_health`` component: how many lands, and are they the
    RIGHT lands.

    Two independent halves, averaged over whichever are available:

      * land count  -- ``_score_land_band`` over the MDFC-adjusted
        effective land count;
      * color sources -- ``_score_manabase`` over the Karsten per-color
        targets (``manabase_report``).

    Equal weight, and no new tuning constants: this is the same
    mean-of-available-sub-scores shape ``construction_signals`` already
    uses, and there is no evidence that would justify calling one half
    more important than the other. Averaging (rather than adding a third
    top-level component) keeps mana ONE line in the grade breakdown,
    which is how the UI and every reader think about it.

    Either half may be missing without costing the deck the component:
    a colorless deck has no color targets, and a deck whose land types
    can't be resolved still has a Karsten reading if its spells resolve.
    ``None`` only when BOTH halves are unavailable -- never a fabricated
    zero.
    """
    subs = [
        s for s in (_score_land_band(mana), _score_manabase(manabase))
        if s is not None
    ]
    if not subs:
        return None
    return sum(subs) / len(subs)


def _count_flavor_score(count: int) -> int:
    """Map a tile count to a sub-score via the UI's flavor cutoffs
    (>=3 good, >=1 neutral, 0 warn) -- deck_health_ui.js applies these
    to both the mana-sink and wincon-protection tiles."""
    if count >= 3:
        return _FLAVOR_SCORES["good"]
    if count >= 1:
        return _FLAVOR_SCORES["neutral"]
    return _FLAVOR_SCORES["warn"]


def compute_health_grade(
    deck_text: str, health: Optional[dict] = None,
) -> dict:
    """Compress the deck-health signals into one letter grade.

    Returns::

        {
          "grade": "A" | "B" | "C" | "D" | "F" | "N/A",
          "score": int 0-100 | None,      # None only when grade == N/A
          "reasons": [str, ...],          # top <=3 worst contributors
          "components": {
            name: {"score": int|None, "weight": float, "available": bool},
          },
        }

    ``health`` is an optional precomputed ``compute_deck_health`` dict
    (the audit route already has one -- passing it avoids a second
    Scryfall walk for those signals). When omitted it is computed here.

    THE COMMANDER-AWARE SIGNALS (2026-07) are computed HERE rather than
    in ``compute_deck_health``: that function's return shape is the
    ``/api/audit`` tile-row contract (one tile per key, pinned by tests
    in both layers), and the commander/Karsten readings are grade
    inputs, not tiles. They walk the deck a second time through the
    same disk-cached ``lookup_card``, which is the same trade
    ``_mana_health_signal`` has always made.

    Unavailability rules (MUST stay consistent with the Scryfall-outage
    None contract established in 6f89c6c):

      - A component whose underlying signal is unavailable is EXCLUDED
        from the denominator; the remaining components' weights are
        renormalized so the score stays 0-100.
      - If EVERY component is unavailable (empty deck, or a degraded
        health payload during a total outage), the grade is 'N/A' --
        never 'F'. An outage is not a deck-construction failure.

    The reasons list is an exact decomposition of the points lost:
    each candidate's severity is the number of (renormalized-)weighted
    points it drags off the final score, so sorting by severity puts
    the genuinely worst contributor first.
    """
    weights = _GRADE_WEIGHTS

    # N/A skeleton shared by both all-unavailable exits below.
    def _na() -> dict:
        return {
            "grade": "N/A",
            "score": None,
            "reasons": [],
            "components": {
                name: {"score": None, "weight": w, "available": False}
                for name, w in weights.items()
            },
        }

    # No parseable [Main] cards -> nothing to grade. This also covers
    # garbage input handed to the route defensively.
    if not any(True for _ in _iter_main_cards(deck_text)):
        return _na()

    if health is None:
        health = compute_deck_health(deck_text)

    # --- component: role_deficits ------------------------------------
    role_targets = health.get("role_targets")
    role_score = _score_role_deficits(role_targets)

    # --- component: mana_health --------------------------------------
    # Two halves: the land-count walk (this module) and the Karsten
    # per-color source report (the assembler's own math, pointed at an
    # existing deck). Each carries its own availability.
    mana = _mana_health_signal(deck_text)
    manabase = _manabase_signal(deck_text)
    land_score = _score_land_band(mana)
    manabase_score = _score_manabase(manabase)
    mana_score = _score_mana_health(mana, manabase)

    # --- component: commander_alignment ------------------------------
    commander_mv = _commander_mana_value(deck_text)
    commander_score, commander_detail = _score_commander_alignment(
        commander_mv, role_targets,
    )

    # --- component: construction_signals -----------------------------
    # Sub-signals scored via the UI's flavor cutoffs. mana_sinks can be
    # None (outage contract); wincon_protection is a named-list count
    # and only goes missing when the health payload itself is degraded.
    subs: list[tuple[str, int, int]] = []  # (label, count, sub_score)
    sinks = health.get("mana_sinks")
    if isinstance(sinks, dict):
        c = int(sinks.get("count", 0) or 0)
        subs.append(("mana_sinks", c, _count_flavor_score(c)))
    wincon = health.get("wincon_protection")
    if isinstance(wincon, dict):
        c = int(wincon.get("count", 0) or 0)
        subs.append(("wincon_protection", c, _count_flavor_score(c)))
    construction_score: Optional[float] = (
        sum(s for _n, _c, s in subs) / len(subs) if subs else None
    )

    component_scores: dict[str, Optional[float]] = {
        "role_deficits": role_score,
        "mana_health": mana_score,
        "construction_signals": construction_score,
        "commander_alignment": commander_score,
    }

    # Reweight over the AVAILABLE components only (outage exclusion).
    available_w = sum(
        weights[name] for name, s in component_scores.items() if s is not None
    )
    if available_w <= 0:
        return _na()
    eff_w = {
        name: (weights[name] / available_w if s is not None else 0.0)
        for name, s in component_scores.items()
    }

    score = round(sum(
        eff_w[name] * s
        for name, s in component_scores.items()
        if s is not None
    ))

    grade = "F"
    for cutoff, letter in _GRADE_BANDS:
        if score >= cutoff:
            grade = letter
            break

    # --- reasons: exact decomposition of the points lost --------------
    # Each candidate carries (severity, text) where severity == the
    # renormalized-weighted points that piece removed from the score.
    candidates: list[tuple[float, str]] = []
    if role_score is not None:
        roles = (role_targets or {}).get("roles") or {}
        total_target = sum(
            int(v.get("target", 0) or 0) for v in roles.values()
        ) or 1
        for role, v in roles.items():
            deficit = int(v.get("deficit", 0) or 0)
            if deficit <= 0:
                continue
            severity = eff_w["role_deficits"] * 100.0 * deficit / total_target
            candidates.append((
                severity,
                f"{role.capitalize()} {v.get('count', 0)}/{v.get('target', 0)}"
                f" — {deficit} below target",
            ))
    # mana_health decomposes into its two halves, each charged the share
    # of the component it actually dragged down (same shape as the
    # construction sub-signals below) -- a deck that is short on lands
    # AND short on blue sources should see both, not one merged line.
    mana_halves = sum(
        1 for s in (land_score, manabase_score) if s is not None
    )
    if land_score is not None and land_score < 100.0 and mana:
        lo, hi = _LAND_BAND
        eff = mana["effective_lands"]
        side = "below" if eff < lo else "above"
        candidates.append((
            eff_w["mana_health"] * (100.0 - land_score) / mana_halves,
            f"{mana['lands']} lands ({eff:g} effective with MDFCs) — "
            f"{side} the {lo}-{hi} healthy band",
        ))
    if manabase_score is not None and manabase_score < 100.0 and manabase:
        under = manabase.get("under_served") or []
        worst = under[0] if under else None
        entry = (manabase.get("per_color") or {}).get(worst) or {}
        demanding = entry.get("most_demanding") or {}
        # Name the card that SET the target (Karsten's most-demanding-card
        # rule made legible) so the fix is obvious: "add black sources" is
        # advice, "your BB four-drop wants 26" is a reason.
        driver = (
            f", set by {demanding['card']} "
            f"({worst * int(demanding.get('pips') or 1)} at "
            f"{demanding.get('cmc')} mana)"
            if demanding.get("card") else ""
        )
        candidates.append((
            eff_w["mana_health"] * (100.0 - manabase_score) / mana_halves,
            f"{worst} sources {entry.get('sources', 0)}/"
            f"{entry.get('target', 0)} — {entry.get('deficit', 0)} short of "
            f"the Karsten target{driver}",
        ))
    if (commander_score is not None and commander_score < 100.0
            and commander_detail):
        candidates.append((
            eff_w["commander_alignment"] * (100.0 - commander_score),
            f"{commander_detail['commander_mv']:g}-mana commander with "
            f"{commander_detail['ramp']} ramp — an expensive commander "
            f"wants ~{commander_detail['expected_ramp']:g}",
        ))
    if construction_score is not None and subs:
        sub_reason = {
            "mana_sinks": lambda c: (
                "No mana sinks — risks flooding out in long games"
                if c == 0 else f"Only {c} mana sink(s) — 3+ recommended"
            ),
            "wincon_protection": lambda c: (
                "No wincon protection (Silence / Veil of Summer class)"
                if c == 0
                else f"Only {c} wincon-protection card(s) — 3+ recommended"
            ),
        }
        for name, c, s in subs:
            if s >= 100:
                continue
            candidates.append((
                eff_w["construction_signals"] * (100.0 - s) / len(subs),
                sub_reason[name](c),
            ))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    reasons = [text for _sev, text in candidates[:3]]

    return {
        "grade": grade,
        "score": int(score),
        "reasons": reasons,
        "components": {
            name: {
                "score": (round(s) if s is not None else None),
                "weight": weights[name],
                "available": s is not None,
            }
            for name, s in component_scores.items()
        },
    }
