"""FP-014.2 — color-source-aware manabase for the from-scratch assembler.

This is the "hard 20%" the FP-014 plan (docs/future-plans.md) calls out:
going from *"a pile of role-appropriate, in-color spells"* to a manabase that
can actually CAST them on curve. FP-014.1 shipped a deliberate placeholder —
it stripped every land (including the seed's tuned dual/fetch base) and
rebuilt a basics-only pile split by pip weight. This module replaces that
placeholder with real color-source math and nonbasic-land selection.

WHY A SEPARATE MODULE (layering).
=================================
``deck_builder.py`` orchestrates the whole build: seed vs fallback sourcing,
legality (commander/singleton/color-identity), the exactly-99 invariant, and
output rendering. The manabase is a self-contained research problem with its
own load-bearing MTG knowledge (source-count tables, land tiers, a curve
model). Keeping it here lets ``deck_builder`` stay a thin orchestrator and
lets this knowledge be tested — and cited — in isolation. ``deck_builder``
computes the land BUDGET (it owns the 99-card invariant and the nonland
trim); this module decides WHICH lands fill that budget.

THE THREE MODELS (all documented inline, all cite their source).
================================================================
1. Land count from the curve  — ``target_land_count``.
2. Color sources per color    — ``color_source_targets`` (the FULL Karsten
                                per-CMC table, most-demanding-card rule;
                                two-anchor fallback for unresolvable costs).
3. Fill order                 — ``build_manabase`` (keep seed → top-up
                                fixing from the advisor's land tiers → basics).

Everything routes through an injected ``lookup`` so tests run fully offline,
and the whole thing degrades to FP-014.1 basics-only behavior (with a
warning) when card/land data can't be resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .staples import (
    ABU_DUAL_LANDS,
    BOND_LANDS,
    FETCH_LANDS,
    SHOCK_LANDS,
    essential_manabase_for_colors,
    is_basic_land,
    tribal_essential_lands,
)

# ---------------------------------------------------------------------------
# Constants shared with deck_builder's basics split.
# ---------------------------------------------------------------------------

_BASIC_FOR_COLOR: dict[str, str] = {
    "W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest",
}
_COLOR_FOR_BASIC: dict[str, str] = {
    v.lower(): k for k, v in _BASIC_FOR_COLOR.items()
}

_PIP_RE = re.compile(r"\{([^}]+)\}")  # each {...} token in a mana cost.
_WUBRG = "WUBRG"

# Lands that tap for mana of ANY of the deck's colors. We treat each as a
# source for EVERY color in the deck's identity — a coarse but honest read
# for source counting (Cavern / Path / Unclaimed only tap "any color" for
# the tribe's spells, but in a tribal deck that is almost every colored
# spell, so counting them as full sources is the right approximation here).
_ANY_COLOR_LANDS: frozenset[str] = frozenset({
    "command tower", "city of brass", "mana confluence", "reflecting pool",
    "forbidden orchard", "exotic orchard", "grand coliseum",
    "path of ancestry", "cavern of souls", "unclaimed territory",
    "secluded courtyard", "three tree city",
})

# Merged view of the advisor's color-gated land tiers (staples.py). We reuse
# these maps verbatim rather than hand-rolling land→color data so the
# assembler and the improvement-advisor agree on what each land taps for.
_TIERED_LANDS: dict[str, frozenset[str]] = {
    **ABU_DUAL_LANDS, **FETCH_LANDS, **SHOCK_LANDS, **BOND_LANDS,
}


# ===========================================================================
# MODEL 1 — how many lands (from the curve).
# ===========================================================================

# Base land count for a "normal" Commander deck. 38 is the widely-cited EDH
# baseline (e.g. Frank Karsten's Commander manabase guidance and the common
# community rule of thumb) for a deck whose average mana value sits around
# 3.5. Lower curves flood more easily and want fewer lands; higher curves
# want more. We nudge one land per ~0.5 MV away from the 3.5 pivot and clamp
# to the sane 33-40 band nobody sensibly leaves.
BASE_LANDS = 38
_PIVOT_MV = 3.5
_LAND_CLAMP_LO, _LAND_CLAMP_HI = 33, 40
# A published average deck is already community-tuned; trust its land count
# outright when it falls in a plausible band, in preference to our model.
_SEED_TRUST_LO, _SEED_TRUST_HI = 33, 42


def target_land_count(
    avg_mana_value: float, seed_land_count: Optional[int] = None,
) -> int:
    """Target land count for a deck with ``avg_mana_value`` nonland curve.

    RECONCILIATION (curve model vs the seed's own count): when we seed from
    an EDHREC average deck, that deck's land count is a per-commander signal
    tuned by thousands of real lists — strictly better than any formula — so
    we TRUST it whenever it's in a plausible band (33-42). We only fall back
    to the curve model when there's no seed count, or the seed's count is
    implausible (a sparse fallback/fixture with a handful of lands).

    The curve model: ``BASE_LANDS`` (38) shifted by 2 lands per point of MV
    away from the 3.5 pivot, clamped to 33-40. A 2.5-MV deck → 36; a 4.5-MV
    deck → 40. Cited above; this is a documented heuristic, not gospel.
    """
    if seed_land_count and _SEED_TRUST_LO <= seed_land_count <= _SEED_TRUST_HI:
        return seed_land_count
    modelled = round(BASE_LANDS + (avg_mana_value - _PIVOT_MV) * 2)
    return max(_LAND_CLAMP_LO, min(_LAND_CLAMP_HI, modelled))


# ===========================================================================
# MODEL 2 — how many sources per color (Karsten-anchored).
# ===========================================================================

# SOURCE-COUNT MODEL — READ THIS, it is the load-bearing MTG knowledge here.
# ------------------------------------------------------------------------
# The reference standard is Frank Karsten's source-count work. FP-014.2's
# first cut used a two-anchor simplification (kept below as the FALLBACK
# path); this second cut implements his FULL per-CMC table:
#
#   Frank Karsten, "How Many Sources Do You Need to Consistently Cast Your
#   Spells? A 2022 Update" (ChannelFireball, 2022; now hosted on TCGplayer
#   Infinite). The 99-card Commander column.
#
# WHAT THE PUBLISHED NUMBERS MEAN (so future readers can re-verify them):
# Karsten simulates millions of games and asks: for a spell with mana value
# M needing N pips of one color, how many sources of that color does a
# 99-card deck need so that P(>= N sources by turn M, ON CURVE) hits his
# consistency bar — defined as (89 + M)%, i.e. 90% for one-drops rising to
# ~96% for seven-drops (this is the "~90% castability" band; there is no
# separate column per probability — the bar scales with M by design, because
# missing a game-winning 5-drop hurts more than missing a 1-drop). The
# probability is CONDITIONAL on having drawn >= M lands by turn M (colored-
# ratio failures only, not land-count failures), assumes a 41-land 99-card
# deck, a reasonable London mulligan strategy, and — new in the 2022 update —
# Commander's free mulligan (CR 103.4c) and the starting player's turn-one
# draw in multiplayer (CR 800.7). Those two Commander rules are why the
# 1-drop and 2-drop rows are noticeably LOWER than a naive 60→99 scale-up,
# and why C and 1C both land on 19 (more cards seen early).
#
# THE TABLE, keyed on (cmc, pips-of-that-color) — transcribed verbatim from
# the article's summary table, 99-card column:
#
#   cost   (cmc,pips)  sources      cost   (cmc,pips)  sources
#   C        (1,1)       19         CC       (2,2)       30
#   1C       (2,1)       19         1CC      (3,2)       28
#   2C       (3,1)       18         2CC      (4,2)       26
#   3C       (4,1)       16         3CC      (5,2)       23
#   4C       (5,1)       15         4CC      (6,2)       22
#   5C       (6,1)       14         5CC      (7,2)       20
#                                   CCC      (3,3)       36
#   CCCC     (4,4)       39         1CCC     (4,3)       33
#   1CCCC    (5,4)       36         2CCC     (5,3)       30
#                                   3CCC     (6,3)       28
#                                   4CCC     (7,3)       26
#
# CAST TURN = THE CARD'S CMC (the assembler's necessary simplification).
# Karsten's table keys on the turn you INTEND to cast the card; a from-
# scratch assembler has no play pattern to read that from, so we assume
# on-curve casting (turn M for a CMC-M card) — exactly the assumption the
# published table itself is built on. Cards cast off-curve on purpose
# (alternative costs, delve, X spells held late) are modelled at their
# printed CMC; that overstates their demand slightly, which errs safe.
#
# GAPS IN THE PUBLISHED TABLE (marked ⇐carry below): Karsten did not publish
# 6C (CMC-7 single-pip) or 4-pip rows past 1CCCC. For those cells we carry
# the nearest published LOWER-CMC value of the same pip count forward —
# demand falls as cast turn rises (more draws seen), so carrying the earlier
# turn's number can only OVERSTATE the requirement. Conservative, never
# fabricated-low.
#
# These are TARGETS, not hard requirements. In 3+ color decks the sum of
# per-color targets exceeds any legal land count (Karsten notes the same).
# ``build_manabase`` treats unmet targets as priorities and allocates the
# real land budget toward them, accepting lower consistency in high-color
# decks exactly as a human deckbuilder does. That softening is unchanged
# from the first cut.
KARSTEN_99_SOURCES: dict[tuple[int, int], int] = {
    # -- single pip: C / 1C / 2C / 3C / 4C / 5C --------------------------
    (1, 1): 19, (2, 1): 19, (3, 1): 18, (4, 1): 16, (5, 1): 15, (6, 1): 14,
    (7, 1): 14,  # ⇐carry: 6C not published; carry 5C's 14 (see note above).
    # -- double pip: CC / 1CC / 2CC / 3CC / 4CC / 5CC --------------------
    (2, 2): 30, (3, 2): 28, (4, 2): 26, (5, 2): 23, (6, 2): 22, (7, 2): 20,
    # -- triple pip: CCC / 1CCC / 2CCC / 3CCC / 4CCC ---------------------
    (3, 3): 36, (4, 3): 33, (5, 3): 30, (6, 3): 28, (7, 3): 26,
    # -- quadruple pip: CCCC / 1CCCC (all Karsten published) -------------
    (4, 4): 39, (5, 4): 36,
    (6, 4): 36, (7, 4): 36,  # ⇐carry: not published; carry 1CCCC's 36.
}


def karsten_sources(cmc: float, pips: int) -> int:
    """Published source count for a card of ``cmc`` needing ``pips`` of one
    color — with the clamping that maps real cards onto the table's domain:

      * ``cmc`` caps at 7 (Karsten's table stops at seven-drops; past turn
        seven you've seen so many cards the 7-drop row is already generous);
      * ``cmc`` floors at ``pips`` (a real cost always has CMC >= its pip
        count; defensive for X-costs parsed at X=0, e.g. {X}{R}{R} → CMC 2);
      * ``pips`` caps at 4 (the deepest published row — 5+ pip cards are
        vanishingly rare and the 4-pip row is already near the table max).
    """
    p = max(1, min(4, pips))
    m = max(p, min(7, int(round(cmc)) or 1))
    return KARSTEN_99_SOURCES[(m, p)]


# TWO-ANCHOR FALLBACK (the FP-014.2 first-cut model — kept, not deleted).
# ------------------------------------------------------------------------
# Before the per-CMC table above, this module interpolated between two
# anchors from the same Karsten line of work: a single-pip card wants ~14
# sources, a double-pip card ~21, scaled by each color's double-pip
# fraction. That model needs only pip data — no CMC — so it remains the
# documented DEGRADED path for cards/colors whose mana cost can't be
# resolved offline (see ``color_source_targets``): when we can't read a
# card's (cmc, pips) we do NOT fabricate a table entry, we fall back to
# this coarser-but-honest estimate and say so in the summary.
SINGLE_PIP_SOURCES = 14
DOUBLE_PIP_SOURCES = 21


@dataclass
class _PipStats:
    """Colored-pip signal extracted from the nonland spells."""

    # color -> total pips of that color across all spells (the split weight).
    weights: dict[str, int] = field(default_factory=dict)
    # color -> how many spells carry >=1 pip of it.
    cards_with: dict[str, int] = field(default_factory=dict)
    # color -> how many spells carry >=2 pips of it (double-pip intensity).
    cards_double: dict[str, int] = field(default_factory=dict)
    # average mana value across the resolvable spells (drives land count).
    avg_mana_value: float = _PIVOT_MV
    # PER-CMC TABLE DATA (FP-014 second cut). color -> the most demanding
    # card seen for that color: (table sources, card name, cmc, pips).
    # Karsten's rule is "meet the requirement of your most demanding card"
    # (his Rakdos worked example does exactly this: MAX per color, not an
    # average) — so we only need the running max, kept stable on ties
    # (first card seen wins, for deterministic summaries).
    table_demands: dict[str, tuple[int, str, int, int]] = field(
        default_factory=dict,
    )
    # HONESTY COUNTERS for the summary: how many spells were scored off the
    # per-CMC table (mana cost resolved) vs fell back / were skipped because
    # their cost couldn't be resolved offline. A constructed _PipStats with
    # no table data (older tests, degraded paths) leaves table_demands empty
    # and routes every color through the two-anchor fallback.
    table_scored: int = 0
    fallback_scored: int = 0


def _parse_cost(mana_cost: str) -> tuple[dict[str, int], float]:
    """Parse a Scryfall ``mana_cost`` → (per-color pip counts, mana value).

    ``{2}{W}{U}`` → ({W:1, U:1}, 4.0). Hybrid ``{W/U}`` counts a pip for
    BOTH colors (either can pay it — a coarse-but-honest fixing signal, same
    convention FP-014.1 used). Generic ``{2}`` and ``{X}`` add to mana value
    but no colored pip; ``{X}`` is treated as 0 for mana value.
    """
    pips: dict[str, int] = {c: 0 for c in _WUBRG}
    mv = 0.0
    for token in _PIP_RE.findall(mana_cost.upper()):
        if token.isdigit():
            mv += int(token)
            continue
        if token == "X":
            continue  # X is 0 for curve purposes.
        # Colored / hybrid / phyrexian symbol → 1 mana value, and a pip for
        # each WUBRG letter it contains (hybrids hit two colors).
        mv += 1
        for letter in token:
            if letter in _WUBRG:
                pips[letter] += 1
    return {c: n for c, n in pips.items() if n}, mv


def pip_stats(
    names, lookup: Callable[[str], Optional[dict]],
) -> _PipStats:
    """Aggregate the colored-pip + mana-value + per-CMC-demand signal.

    Every spell lands in exactly one of two honesty buckets (surfaced in the
    manabase summary so a degraded build is visible, not silent):

      * TABLE-SCORED — ``lookup`` resolved a mana cost. The card gets a
        Karsten table entry per colored pip group: ``karsten_sources(cmc,
        pips-of-that-color)``, and competes for "most demanding card" of
        each of its colors. A resolved cost with zero colored pips (a {3}
        artifact) is still table-scored — "demands nothing" is a true
        table reading, not a gap.
      * FALLBACK-SCORED — ``lookup`` failed / returned nothing / returned a
        card with no mana cost. No cost means no (cmc, pips) — we DO NOT
        fabricate one. The card is skipped from every per-color max and
        counted, and any color left with pips but no table entries at all
        is scored by the two-anchor fallback in ``color_source_targets``.
    """
    stats = _PipStats(
        weights={c: 0 for c in _WUBRG},
        cards_with={c: 0 for c in _WUBRG},
        cards_double={c: 0 for c in _WUBRG},
    )
    mv_total = 0.0
    mv_n = 0
    for nm in names:
        try:
            card = lookup(nm)
        except Exception:  # noqa: BLE001 — a lookup blip must not crash a build.
            card = None
        cost = (card.get("mana_cost") or "") if card else ""
        if not cost.strip():
            # Unresolvable offline (or a genuinely costless card) — counted,
            # never guessed at. See the fallback bucket note above.
            stats.fallback_scored += 1
            continue
        pips, mv = _parse_cost(cost)
        stats.table_scored += 1
        mv_total += mv
        mv_n += 1
        for color, n in pips.items():
            stats.weights[color] += n
            stats.cards_with[color] += 1
            if n >= 2:
                stats.cards_double[color] += 1
            # Karsten per-CMC entry for THIS card in THIS color. Cast turn
            # = CMC (on-curve assumption — see the table's header comment).
            need = karsten_sources(mv, n)
            best = stats.table_demands.get(color)
            if best is None or need > best[0]:
                stats.table_demands[color] = (need, nm, int(round(mv)), n)
    stats.avg_mana_value = (mv_total / mv_n) if mv_n else _PIVOT_MV
    return stats


def color_source_targets(
    colors: list[str], stats: _PipStats,
) -> dict[str, int]:
    """Desired source count per color — full Karsten per-CMC model.

    THE RULE (Karsten's own): a color's requirement is set by its MOST
    DEMANDING card — the max of the table entries, not an average. His 2022
    article applies it exactly this way in the Rakdos worked example ("check
    the most constraining restrictions for all colors"): if the deck holds
    one Wrath of God, the white count must satisfy Wrath of God, no matter
    how many easy 4C white cards sit alongside it. Averaging would let a
    pile of splashy singles vote down the one card that actually strains
    the mana.

    Per color, in order:
      1. table max  — ``stats.table_demands`` (cards with resolved costs);
      2. two-anchor fallback — the color has pips but NO table entries
         (cost data unresolvable offline, or a hand-built _PipStats):
         interpolate 14→21 by the double-pip fraction, the documented
         first-cut model. Coarser, honest, never fabricated;
      3. zero — a color in the identity no spell actually needs; the
         basics floor in ``build_manabase`` still gives it a token source.

    Targets remain PRIORITIES, not guarantees — the 3+ color softening in
    ``build_manabase`` (largest-remainder over deficits when the sum
    exceeds the land budget) is unchanged from the first cut.
    """
    targets: dict[str, int] = {}
    for c in colors:
        demand = stats.table_demands.get(c)
        if demand is not None:
            targets[c] = demand[0]
            continue
        n_with = stats.cards_with.get(c, 0)
        if n_with <= 0:
            targets[c] = 0
            continue
        frac_double = stats.cards_double.get(c, 0) / n_with
        span = DOUBLE_PIP_SOURCES - SINGLE_PIP_SOURCES
        targets[c] = round(SINGLE_PIP_SOURCES + span * frac_double)
    return targets


# ===========================================================================
# MODEL 3 — the fill (keep seed → top-up fixing → basics).
# ===========================================================================


def land_color_sources(
    name: str,
    identity: set[str],
    lookup: Callable[[str], Optional[dict]],
) -> set[str]:
    """Which of the deck's colors does land ``name`` produce a source for?

    Order of resolution (cheapest / most-authoritative first):
      1. basics — Plains→{W} … Wastes→{} (colorless, no colored source);
      2. any-color fixers (Command Tower, City of Brass, …) → the whole
         identity;
      3. the advisor's color-gated tiers (ABU / fetch / shock / bond) →
         exactly the colors that land spans, intersected with the identity;
      4. anything else — ``lookup`` for ``produced_mana`` then the land's
         own color identity; failing both, no colored source (a colorless
         utility land like Ancient Tomb).

    A dual counts for BOTH its colors (that's the whole point of running it),
    which is why each source set can have more than one letter.
    """
    key = name.strip().lower()
    if key in _COLOR_FOR_BASIC:
        return {_COLOR_FOR_BASIC[key]}
    if is_basic_land(name):  # Wastes / snow basics without a color.
        return set()
    if key in _ANY_COLOR_LANDS:
        return set(identity)
    if key in _TIERED_LANDS:
        spanned = set(_TIERED_LANDS[key])
        return (spanned & identity) if identity else spanned
    try:
        card = lookup(name)
    except Exception:  # noqa: BLE001
        card = None
    if card:
        produced = {
            p.upper() for p in (card.get("produced_mana") or [])
            if isinstance(p, str) and p.upper() in _WUBRG
        }
        if not produced:
            produced = {
                c.upper() for c in (card.get("color_identity") or [])
                if isinstance(c, str) and c.upper() in _WUBRG
            }
        if produced:
            return (produced & identity) if identity else produced
    return set()  # colorless utility land — real, just not a colored source.


def _largest_remainder(
    total: int, weights: dict[str, int], keys: list[str],
) -> dict[str, int]:
    """Distribute ``total`` integer units across ``keys`` in proportion to
    ``weights`` with no drift (the exact-sum largest-remainder method used
    across the codebase). Zero total → all zero; zero weight-sum → even.
    """
    out = {k: 0 for k in keys}
    if total <= 0 or not keys:
        return out
    wsum = sum(max(0, weights.get(k, 0)) for k in keys)
    if wsum <= 0:
        base, rem = divmod(total, len(keys))
        for i, k in enumerate(keys):
            out[k] = base + (1 if i < rem else 0)
        return out
    exact = {k: total * max(0, weights.get(k, 0)) / wsum for k in keys}
    for k in keys:
        out[k] = int(exact[k])
    leftover = total - sum(out.values())
    for k in sorted(keys, key=lambda k: exact[k] - out[k], reverse=True)[:leftover]:
        out[k] += 1
    return out


@dataclass
class ManabaseSummary:
    """Inspectable quality report for an assembled manabase."""

    land_count: int                 # total land cards emitted.
    sources: dict[str, int]         # color -> actual sources produced.
    targets: dict[str, int]         # color -> Karsten-model desired sources.
    fixing_land_count: int          # nonbasic lands (kept seed + topped-up).
    basic_count: int                # basic lands.
    kept_seed_lands: int            # of the fixing lands, how many came from the seed.
    degraded: bool                  # True → fell back toward basics-only.
    # --- FP-014 second cut: per-CMC table inspectability -------------------
    # (defaulted so older call sites/tests constructing the summary by
    # keyword keep working unchanged.)
    # color -> (card name, cmc, pips): the card that SET that color's target
    # under Karsten's most-demanding-card rule. The target value itself is
    # ``targets[color]`` — this names the card responsible for it.
    most_demanding: dict[str, tuple[str, int, int]] = field(
        default_factory=dict,
    )
    spells_table_scored: int = 0    # spells scored off the per-CMC table.
    spells_fallback_scored: int = 0  # spells with unresolvable cost (skipped).
    # colors whose target came from the two-anchor fallback (pips seen but
    # no per-card cost data for that color at all).
    fallback_colors: list[str] = field(default_factory=list)

    def format_lines(self) -> list[str]:
        """Human-readable summary lines for the CLI."""
        srcs = "  ".join(
            f"{c}:{self.sources.get(c, 0)}/{self.targets.get(c, 0)}"
            for c in _WUBRG if c in self.targets
        )
        lines = [
            f"  manabase: {self.land_count} lands "
            f"({self.fixing_land_count} fixing / {self.basic_count} basics"
            f"{f'; {self.kept_seed_lands} kept from seed' if self.kept_seed_lands else ''})",
        ]
        if srcs:
            lines.append(f"  sources (have/target): {srcs}")
        # WHY each target is what it is: name the card that set it (Karsten's
        # most-demanding-card rule made inspectable). "Wrath of God (cmc 4,
        # WW) → 26" reads straight back into the published table.
        demanding = "  ".join(
            f"{c}: {name} (cmc {cmc}, {'C' * pips}) → {self.targets.get(c, 0)}"
            for c in _WUBRG
            if c in self.most_demanding
            for (name, cmc, pips) in [self.most_demanding[c]]
        )
        if demanding:
            lines.append(f"  most demanding: {demanding}")
        if self.spells_table_scored or self.spells_fallback_scored:
            scoring = (
                f"  spell scoring: {self.spells_table_scored} per-CMC table"
                f" / {self.spells_fallback_scored} fallback (cost unresolved)"
            )
            if self.fallback_colors:
                scoring += (
                    f"; two-anchor colors: {','.join(self.fallback_colors)}"
                )
            lines.append(scoring)
        if self.degraded:
            lines.append(
                "  NOTE: land data unavailable — degraded to a basics-only "
                "manabase (FP-014.1 behavior)."
            )
        return lines


@dataclass
class Manabase:
    """The assembled manabase: nonbasic land names + a basics multiset."""

    lands: list[str]                # ordered nonbasic land names (singleton).
    basics: dict[str, int]          # basic land name -> quantity.
    summary: ManabaseSummary

    def total_cards(self) -> int:
        return len(self.lands) + sum(self.basics.values())


def build_manabase(
    colors: list[str],
    nonland_names: list[str],
    kept_seed_lands: list[str],
    land_slots: int,
    *,
    lookup: Callable[[str], Optional[dict]],
    tribe: Optional[str] = None,
    stats: Optional[_PipStats] = None,
    collection=None,
) -> Manabase:
    """Fill exactly ``land_slots`` land cards for a deck of ``colors``.

    FILL ORDER — the coherence win over FP-014.1's discard-everything base:

      (a) KEEP the seed's own nonbasic lands (``kept_seed_lands``). An EDHREC
          average deck's dual/fetch/utility base is already tuned for this
          commander; discarding it (FP-014.1) threw away real fixing. We
          keep every one that fits the land budget.

      (b) TOP UP fixing from the advisor's color-identity-appropriate land
          tiers — ``staples.essential_manabase_for_colors`` (ABU duals,
          fetches, shocks, bond lands, and 3+ color utility fixers) plus
          ``tribal_essential_lands`` for tribal decks. We do NOT hand-roll
          land lists; we reuse the exact tiers the improvement-advisor
          recommends, so a from-scratch build and an advised upgrade agree.
          We add a fixer only while a color is still under target and slots
          remain (reserving one basic slot per color), so mono-color decks
          (no eligible duals) and decks with a rich kept base don't get
          spammed with lands they don't need.

      (c) FILL the rest with basics, allocated to close each color's source
          DEFICIT first (bring every under-target color up), then to spread
          any surplus by pip weight (a red-heavy deck gets the extra
          Mountains). Each land counts toward every color it taps.

    ``colors`` empty (colorless / unresolved identity) → degrade to a
    basics-only base (all Wastes when truly colorless) and flag it; this is
    the FP-014.1 fallback, preserved for graceful failure.

    FP-014.3 HOOK (collection/budget preference): ``collection`` is accepted
    but only threaded, not yet consumed. When implemented, step (b) should
    prefer owned duals over unowned ones and honor a budget flag by passing
    ``budget=True`` to ``essential_manabase_for_colors`` (which already drops
    the $200 ABU duals + fetches). The seam is here so .3 is a local change.
    """
    identity = {c for c in colors if c in _WUBRG}
    if stats is None:
        stats = pip_stats(nonland_names, lookup)

    # --- degrade path: no resolvable colors → basics-only (FP-014.1) -------
    if not identity:
        # Truly colorless (Karn) → Wastes; otherwise nothing to fix.
        basics = {"Wastes": max(0, land_slots)} if land_slots > 0 else {}
        summary = ManabaseSummary(
            land_count=sum(basics.values()), sources={}, targets={},
            fixing_land_count=0, basic_count=sum(basics.values()),
            kept_seed_lands=0, degraded=True,
            spells_table_scored=stats.table_scored,
            spells_fallback_scored=stats.fallback_scored,
        )
        return Manabase(lands=[], basics=basics, summary=summary)

    color_list = [c for c in _WUBRG if c in identity]  # WUBRG-ordered.
    targets = color_source_targets(color_list, stats)

    # (a) KEEP seed lands, singleton-deduped, capped at the land budget.
    lands: list[str] = []
    seen: set[str] = set()
    for nm in kept_seed_lands:
        key = nm.strip().lower()
        if not key or key in seen or is_basic_land(nm):
            continue
        if len(lands) >= land_slots:
            break
        seen.add(key)
        lands.append(nm)
    kept_count = len(lands)

    def _sources_now() -> dict[str, int]:
        got = {c: 0 for c in color_list}
        for land in lands:
            for c in land_color_sources(land, identity, lookup):
                if c in got:
                    got[c] += 1
        return got

    # (b) TOP UP fixing from the advisor's tiers. Reserve one basic slot per
    # color so a color never ends up with zero *basic* fallback.
    # FP-014.3 HOOK: pass budget=... / prefer owned here.
    reserve_for_basics = len(color_list)
    fixing_candidates = [
        nm for nm in essential_manabase_for_colors(identity)
        if nm.strip().lower() not in seen
    ]
    fixing_candidates += [
        nm for nm in tribal_essential_lands(tribe, color_identity=identity)
        if nm.strip().lower() not in seen
    ]
    for cand in fixing_candidates:
        if len(lands) >= land_slots - reserve_for_basics:
            break
        got = _sources_now()
        # Only add a fixer that helps a color still below its target.
        provides = land_color_sources(cand, identity, lookup)
        if not provides:
            continue
        if any(got.get(c, 0) < targets.get(c, 0) for c in provides):
            key = cand.strip().lower()
            seen.add(key)
            lands.append(cand)

    # (c) FILL the remainder with basics.
    basic_slots = land_slots - len(lands)
    got = _sources_now()
    deficits = {c: max(0, targets.get(c, 0) - got.get(c, 0)) for c in color_list}
    basics_by_color = {c: 0 for c in color_list}
    if basic_slots > 0:
        total_deficit = sum(deficits.values())
        if total_deficit >= basic_slots:
            # Can't fully cover — prioritize by deficit size.
            alloc = _largest_remainder(basic_slots, deficits, color_list)
            for c in color_list:
                basics_by_color[c] += alloc[c]
        else:
            # Cover every deficit, then spread the surplus by pip weight so
            # the heavier color gets the extra sources.
            for c in color_list:
                basics_by_color[c] += deficits[c]
            surplus = basic_slots - total_deficit
            alloc = _largest_remainder(surplus, stats.weights, color_list)
            for c in color_list:
                basics_by_color[c] += alloc[c]
        # Floor: any color with zero total sources but real pips steals a
        # slot so it isn't left uncastable.
        got_after = {
            c: got.get(c, 0) + basics_by_color[c] for c in color_list
        }
        for c in color_list:
            if got_after[c] == 0 and stats.cards_with.get(c, 0) > 0:
                donor = max(color_list, key=lambda x: basics_by_color[x])
                if basics_by_color[donor] > 1:
                    basics_by_color[donor] -= 1
                    basics_by_color[c] += 1

    basics = {
        _BASIC_FOR_COLOR[c]: n for c, n in basics_by_color.items() if n > 0
    }

    # --- final source tally + summary --------------------------------------
    final_sources = {c: 0 for c in color_list}
    for land in lands:
        for c in land_color_sources(land, identity, lookup):
            if c in final_sources:
                final_sources[c] += 1
    for c in color_list:
        final_sources[c] += basics_by_color[c]

    basic_total = sum(basics.values())
    # Inspectability (FP-014 second cut): name the card that set each color's
    # target, and say how much of the deck the per-CMC table actually saw —
    # a build with many fallback-scored spells deserves less trust in its
    # targets, and the summary should make that legible, not bury it.
    most_demanding = {
        c: (name, cmc, pips)
        for c, (_need, name, cmc, pips) in stats.table_demands.items()
        if c in identity
    }
    fallback_colors = [
        c for c in color_list
        if stats.cards_with.get(c, 0) > 0 and c not in stats.table_demands
    ]
    summary = ManabaseSummary(
        land_count=len(lands) + basic_total,
        sources=final_sources,
        targets=targets,
        fixing_land_count=len(lands),
        basic_count=basic_total,
        kept_seed_lands=kept_count,
        degraded=False,
        most_demanding=most_demanding,
        spells_table_scored=stats.table_scored,
        spells_fallback_scored=stats.fallback_scored,
        fallback_colors=fallback_colors,
    )
    return Manabase(lands=lands, basics=basics, summary=summary)
