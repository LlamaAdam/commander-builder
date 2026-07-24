"""FP-014 — build-from-scratch deck assembly (commander → legal 99).

commander-builder is deliberately an *iteration engine* everywhere else: it
improves an EXISTING deck. This module is the first vertical slice of the
opposite direction (FP-014, docs/future-plans.md) — take a commander + a
target bracket and emit a complete, legal Commander deck: exactly 99 main +
1 commander, in-color, singleton.

WHERE THE COHERENCE COMES FROM (read this before trusting the output).
==============================================================
This first cut does NOT invent synergy. Its coherence is *borrowed* from
EDHREC's community aggregate:

  * Preferred path — ``edhrec_client.fetch_average_deck`` returns EDHREC's
    auto-generated "average deck" for the commander+bracket: a real,
    already-coherent ~99 assembled from thousands of published lists. We
    take its nonland cards verbatim as the deck's spine. THAT aggregate is
    the coherence — not anything computed here.

  * Fallback path — when no average deck is published (true for many
    commanders), we assemble a shell from the commander PAGE's top +
    high-synergy cards, filling toward ``staples.ROLE_TARGETS`` counts.
    This is a defensible pile, NOT a coherent deck — exactly the
    "legal-but-mediocre" first cut the FP-014 plan calls out. The
    downstream ``commander-improve`` loop is where that pile gets measured.

WHAT THE MANABASE DOES (FP-014.2 — the "hard 20%").
===================================================
  The manabase is color-source-aware (``deck_builder_manabase.py``). It
  KEEPS the seed average deck's own dual/fetch/utility lands (they're tuned
  for this commander), tops up fixing from the improvement-advisor's land
  tiers, and fills the rest with basics sized to hit a per-color SOURCE
  target derived from the spells' pip requirements (a Karsten-anchored
  model — see that module). The land COUNT comes from the curve, reconciled
  against the seed's own count. When card/land data can't be resolved the
  manabase degrades to a basics-only base (the FP-014.1 behavior).

WHAT IS DELIBERATELY DEFERRED.
==============================
  * FP-014.3 (owned-card preference): when a collection is supplied we apply
    only a MINIMAL owned-bias (keep owned cards first when we have to trim).
    Full owned-aware fill/substitution is future work.

The assembler reuses the shipped, tested substrate rather than re-deriving
it: ``enforce_color_identity`` for legality, ``count_main_cards`` +
``_pad_main_to_target`` for the main-size guard, ``dck_meta.rewrite_name`` for
the name-stamp invariant, and ``moxfield_import``'s filename/section
conventions so the dashboard/improve loop accept the output unchanged.

Fetchers and resolvers are injectable so tests run fully offline.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import dck_meta, deck_builder_personalize as personalize, lift_analysis
from ._proposer_filters import enforce_color_identity
from .bracket_estimator import estimate_bracket
from .collection import load_collection, name_key, owns, parse_collection_lines
from .dck_utils import COMMANDER_DECK_SIZE, count_main_cards
from .deck_builder_manabase import (
    ManabaseSummary,
    _parse_cost,
    build_manabase,
    pip_stats,
    target_land_count,
)
from .edhrec_client import fetch_average_deck, fetch_commander_page
from .moxfield_import import DECK_OUT_DIR, safe_filename
from .scryfall_client import lookup_card, normalize_color_identity
from .staples import (
    ROLE_TARGETS,
    classify_role_extended,
    detect_tribal_type,
    is_basic_land,
)
from .web.deck_text_ops import _pad_main_to_target

# Commander decks are exactly 100 cards TOTAL: the command zone plus the
# mainboard, i.e. the mainboard target is ``100 - commander_count`` — 99
# for a single commander, 98 for a partner pair. This builder currently
# only assembles SINGLE-commander decks (``build_deck`` takes one
# commander name; partner-pair building is future work), so the derived
# MAIN_SIZE below is 99 — but it is deliberately written as the
# subtraction so the invariant stays correct when partner building
# arrives (bump _N_COMMANDERS from the pair input, everything downstream
# follows).
_N_COMMANDERS = 1  # single-commander builds only; partners are future work.
MAIN_SIZE = COMMANDER_DECK_SIZE - _N_COMMANDERS

# Land-count target when we can't read one off a seed. 37 is the midpoint of
# the 36-38 band the FP-014 plan cites for a "normal" two/three-color deck.
# When we seed from an average deck we instead MATCH that deck's own land
# count (see ``_count_lands``), which is a better per-commander signal than
# any fixed number.
DEFAULT_LAND_TARGET = 37

# WUBRG pip letter → the basic land that produces it. Colorless decks get
# Wastes (see ``_distribute_basics_by_pips``).
_BASIC_FOR_COLOR: dict[str, str] = {
    "W": "Plains",
    "U": "Island",
    "B": "Swamp",
    "R": "Mountain",
    "G": "Forest",
}

# Pull WUBRG symbols out of a Scryfall ``mana_cost`` string like
# ``{2}{W}{U}`` or the hybrid ``{W/U}``. A hybrid pip counts for BOTH of its
# colors — a coarse-but-honest weight for the basics split.
_PIP_RE = re.compile(r"[WUBRG]")


@dataclass
class BuildResult:
    """Everything ``main`` needs to report — text plus the provenance the
    ``.dck`` itself doesn't carry (which path built it, what got dropped)."""

    text: str
    name: str
    stem: str
    colors: str  # WUBRG string, "" colorless, or "?" when unresolved
    nonland_count: int
    land_count: int
    source: str  # "average-deck seed" | "commander-page fallback"
    dropped_off_color: list[str]
    manabase: ManabaseSummary  # per-color sources vs target (FP-014.2)

    # ---- FP-014.3 personalization provenance -----------------------------
    # Each is empty/None when its stage was disabled or made no change, so a
    # plain FP-014.1/2 build reports exactly as before. ``field`` defaults
    # keep the dataclass constructible without the new args (older callers
    # and the tests that build BuildResult by hand).
    bracket_target: int = 0             # the requested bracket.
    bracket_estimate: Optional[int] = None  # steer's final estimate (or None).
    lift_swaps: list[str] = field(default_factory=list)   # rationale strings.
    lift_skipped: Optional[str] = None  # why the lift stage was skipped.
    steer_notes: list[str] = field(default_factory=list)  # steer actions.
    owned_swaps: list[str] = field(default_factory=list)  # owned-bias trades.
    buy_list: list[str] = field(default_factory=list)     # still-unowned cards.


# --------------------------------------------------------------------------
# Card classification helpers (all go through the injected ``lookup`` so a
# test can drive them from a canned card dict without any network).
# --------------------------------------------------------------------------


def _resolve_ci_via_lookup(
    commander: str, lookup: Callable[[str], Optional[dict]],
) -> Optional[str]:
    """Resolve a commander's color identity to a WUBRG string.

    Returns ``""`` for a genuinely colorless commander (Scryfall found the
    card, its identity is empty) and ``None`` when the card can't be
    resolved at all (typo, custom card, Scryfall outage). The two are
    distinct downstream: ``enforce_color_identity`` treats ``""`` as
    colorless-only and ``None`` as "skip the filter", mirroring how the
    advisor degrades (improvement_advisor.color_identity_for_commander).
    """
    try:
        card = lookup(commander)
    except Exception:  # noqa: BLE001 — a Scryfall blip must not crash a build.
        return None
    if not card:
        return None
    return normalize_color_identity(card.get("color_identity") or [])


def _is_land(name: str, lookup: Callable[[str], Optional[dict]]) -> bool:
    """True for any land — basic or nonbasic. Mirrors ``staples.is_land``
    but routes through the injected ``lookup`` so tests stay offline.

    Basics short-circuit before any lookup. Unknown cards resolve to False:
    over-keeping a mystery card as a nonland is the safer failure than
    silently dropping it into the (discarded) land pile.
    """
    if is_basic_land(name):
        return True
    try:
        card = lookup(name)
    except Exception:  # noqa: BLE001
        return False
    if not card:
        return False
    return "land" in (card.get("type_line") or "").lower()


def _pip_weights(
    names, lookup: Callable[[str], Optional[dict]],
) -> dict[str, int]:
    """Sum WUBRG pips across ``names`` → ``{color: pip_count}``.

    The weight the basics split reads: a deck whose nonland spells are
    two-thirds red pips wants roughly two-thirds Mountains. Cards Scryfall
    can't resolve contribute nothing (no cost = no pip signal), which is
    fine — they're a minority and the split degrades gracefully.
    """
    weights: dict[str, int] = {c: 0 for c in "WUBRG"}
    for nm in names:
        try:
            card = lookup(nm)
        except Exception:  # noqa: BLE001
            continue
        if not card:
            continue
        cost = card.get("mana_cost") or ""
        for letter in _PIP_RE.findall(cost.upper()):
            weights[letter] += 1
    return weights


def _count_lands(names, lookup: Callable[[str], Optional[dict]]) -> int:
    """How many of ``names`` are lands (basic or nonbasic)."""
    return sum(1 for nm in names if _is_land(nm, lookup))


def _distribute_basics_by_pips(
    colors: list[str], weights: dict[str, int], total: int,
) -> dict[str, int]:
    """Split ``total`` basic-land slots across ``colors`` by pip weight.

    THIS IS THE FP-014.1 "SIMPLE MANABASE". It is basics-only on purpose —
    real color-source counting and nonbasic-land selection are FP-014.2. The
    algorithm:

      * colorless deck (no colors) → all ``Wastes`` (legal in any identity);
      * otherwise give each color a floor of 1 basic (when we have enough
        slots) so no color in the identity is left with zero sources, then
        hand out the remainder in proportion to pip weight using the
        largest-remainder method — the same exact-sum distribution
        ``edhrec_client.AverageDeck.to_moxfield_shape`` and
        ``deck_text_ops._pad_main_to_target`` already use, so the counts always
        sum to ``total`` with no drift.

    Returns ``{basic_land_name: quantity}`` (zero-quantity entries omitted).
    """
    if total <= 0:
        return {}
    if not colors:
        # Colorless identity (e.g. Karn) — Wastes is the only legal basic.
        return {"Wastes": total}

    counts: dict[str, int] = {c: 0 for c in colors}
    remaining = total
    # Floor of one source per color, budget permitting.
    if total >= len(colors):
        for c in colors:
            counts[c] = 1
            remaining -= 1

    if remaining > 0:
        wsum = sum(weights.get(c, 0) for c in colors)
        if wsum <= 0:
            # No pip signal at all (e.g. an all-colorless-cost shell): even
            # split, remainder to the earliest colors in WUBRG order.
            base, rem = divmod(remaining, len(colors))
            for i, c in enumerate(colors):
                counts[c] += base + (1 if i < rem else 0)
        else:
            exact = {c: remaining * weights.get(c, 0) / wsum for c in colors}
            floored = {c: int(exact[c]) for c in colors}
            leftover = remaining - sum(floored.values())
            # Largest fractional parts collect the rounding leftover.
            for c in sorted(colors, key=lambda c: exact[c] - floored[c],
                            reverse=True)[:leftover]:
                floored[c] += 1
            for c in colors:
                counts[c] += floored[c]

    return {
        _BASIC_FOR_COLOR[c]: n for c, n in counts.items() if n > 0
    }


# --------------------------------------------------------------------------
# Seed / fallback card sourcing
# --------------------------------------------------------------------------


def _page_has_cards(page) -> bool:
    """True when a commander page carries at least one usable card."""
    return bool(
        page.top_cards
        or page.high_synergy_cards
        or page.new_cards
        or page.category_lists
    )


def _fallback_candidates(page) -> list[str]:
    """Ordered nonland-candidate names from a commander PAGE (no average deck).

    Priority: most-included cards first (``top_cards``), then the commander's
    signature ``high_synergy_cards``, then the per-category sections. That
    ordering biases the eventual trim toward cards the community actually
    runs. Role balancing toward ``ROLE_TARGETS`` happens implicitly — the
    top-cards list for any real commander already spans ramp/draw/removal —
    and finer per-role quotas are left to the improve-loop rather than
    hand-tuned here (the FP-014 "hard 20%"). ``ROLE_TARGETS`` is imported so
    the intent is greppable and the nonland budget below is sized against it.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    buckets = [page.top_cards, page.high_synergy_cards]
    buckets.extend(page.category_lists.values())
    for bucket in buckets:
        for entry in bucket:
            k = entry.name.strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            ordered.append(entry.name)
    return ordered


# --------------------------------------------------------------------------
# Core assembler
# --------------------------------------------------------------------------


def _assemble(
    commander: str,
    bracket: int,
    collection_path: Optional[Path] = None,
    *,
    fetch_avg: Optional[Callable] = None,
    fetch_page: Optional[Callable] = None,
    resolve_ci: Optional[Callable[[str], Optional[str]]] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    name: Optional[str] = None,
    # ---- FP-014.3 personalization toggles + injectables ------------------
    # Each stage is on by default (personalization is the point of a
    # from-scratch build); a caller/CLI flag turns it off. The remaining
    # kwargs are injectable seams so tests drive every stage from canned
    # data with no network — they default to the real corpus/estimator/
    # game-changer entry points, resolved at CALL time like the fetchers.
    enable_lift: bool = True,
    enable_steer: bool = True,
    owned_bias: bool = True,
    deck_dir: Optional[Path] = None,
    lift_matrix: Optional[dict] = None,
    estimate_fn: Optional[Callable[[str], dict]] = None,
    is_game_changer: Optional[Callable[[str], bool]] = None,
    is_fast_mana: Optional[Callable[[str], bool]] = None,
    power_pool: Optional[list[str]] = None,
    owned_names: Optional[list[str]] = None,
) -> BuildResult:
    """Assemble a legal 99 for ``commander`` at ``bracket``. See module docs.

    The fetchers/resolvers are injectable so tests run fully offline; they
    default to the real EDHREC/Scryfall entry points. Defaults resolve at
    CALL time (not def time) so ``main`` — which passes none of them — still
    honors a test's ``monkeypatch.setattr`` on the module-level names (the
    same def-time-binding hazard the conftest documents for DEFAULT_DB_PATH).

    Returns a ``BuildResult`` (text + provenance). ``build_deck`` is the
    text-only public wrapper; ``main`` uses the full result for its summary.
    Raises ``ValueError`` for bad input or when EDHREC has no data at all.
    """
    if not commander or not commander.strip():
        raise ValueError("commander is required")
    if bracket not in (1, 2, 3, 4, 5):
        raise ValueError(f"bracket must be an integer 1-5, got {bracket!r}")
    commander = commander.strip()
    if fetch_avg is None:
        fetch_avg = fetch_average_deck
    if fetch_page is None:
        fetch_page = fetch_commander_page
    if lookup is None:
        lookup = lookup_card
    if resolve_ci is None:
        resolve_ci = lambda nm: _resolve_ci_via_lookup(nm, lookup)  # noqa: E731

    ci = resolve_ci(commander)  # WUBRG string, "" colorless, or None.

    # ---- 1. SEED from the EDHREC average deck (the coherence source) -----
    avg = None
    try:
        avg = fetch_avg(commander, bracket)
    except Exception:  # noqa: BLE001 — a fetch failure just means no seed.
        avg = None

    seed_land_count: Optional[int] = None
    if avg is not None and getattr(avg, "cards", None):
        source = "average-deck seed"
        # The average deck IS the coherence for this first cut — take its
        # cardlist verbatim as the base (commander + lands stripped below).
        raw_names = [c.name for c in avg.cards]
        # Match the seed's own land ratio rather than a fixed target.
        seed_land_count = _count_lands(
            [n for n in raw_names if name_key(n) != name_key(commander)],
            lookup,
        )
    else:
        # ---- 2. FALLBACK — no published average deck for this commander --
        page = None
        try:
            page = fetch_page(commander)
        except Exception:  # noqa: BLE001
            page = None
        if page is None or not _page_has_cards(page):
            # Neither source available: a clean, caller-printable error —
            # never a stacktrace to the user.
            raise ValueError(
                f"cannot build: no EDHREC data for {commander}"
            )
        source = "commander-page fallback"
        raw_names = _fallback_candidates(page)

    # ---- 3. LEGALITY: split commander / seed lands / spells, singleton ----
    # FP-014.2: unlike FP-014.1 (which discarded every land), we KEEP the
    # seed's own NONBASIC lands — an average deck's dual/fetch/utility base
    # is tuned for this commander and is real fixing. Basic lands are still
    # dropped and recomputed (their counts are what we tune to hit the source
    # targets). We only keep lands on the seed path; the commander-page
    # fallback has no tuned base to preserve, so its lands are dropped (they
    # get rebuilt from the tiers + basics like FP-014.1).
    keep_seed_lands = source == "average-deck seed"
    cmdr_key = name_key(commander)
    seen: set[str] = set()
    nonlands: list[str] = []
    seed_lands: list[str] = []
    for nm in raw_names:
        if not nm or not nm.strip():
            continue
        k = name_key(nm)
        if k == cmdr_key:
            continue  # commander lives in the command zone, not [Main].
        if _is_land(nm, lookup):
            if keep_seed_lands and not is_basic_land(nm) and k not in seen:
                seen.add(k)
                seed_lands.append(nm)  # KEEP tuned nonbasic fixing.
            continue  # basics + fallback lands are rebuilt in step 4.
        if k in seen:
            continue  # singleton: no duplicate nonbasics.
        seen.add(k)
        nonlands.append(nm)

    # Color-identity legality over BOTH the spells and the kept lands. ``ci
    # is None`` makes this a pass-through (can't verify → don't strip
    # everything) — the advisor degrades the same way; warn so it's not
    # silent. Colorless lands (Command Tower) pass every identity.
    nonlands, dropped_off_color = enforce_color_identity(nonlands, ci)
    seed_lands, dropped_lands = enforce_color_identity(seed_lands, ci)
    dropped_off_color = dropped_off_color + dropped_lands
    if ci is None:
        print(
            f"[build] WARNING: could not resolve color identity for "
            f"{commander!r} — skipping the color-identity filter "
            f"(cards may be off-color).",
            flush=True,
        )

    # ---- 3b. COLLECTION: minimal owned-bias (FP-014.3 = full preference)--
    # When a collection is registered, keep owned cards first so that if we
    # have to trim the nonland list to hit the land target, the cards we
    # drop are the ones the user doesn't own. Stable sort preserves EDHREC
    # priority within each group. Basics always count as owned. This is the
    # ONLY owned-awareness in this slice — real owned-aware fill is deferred.
    coll = load_collection(collection_path) if collection_path else None
    if coll is not None:
        nonlands.sort(key=lambda nm: 0 if owns(coll, nm) else 1)

    # ---- 4. MANABASE (color-source-aware, FP-014.2) + exactly-99 sizing --
    # The land BUDGET and the 99-card invariant live HERE (deck_builder owns
    # them); WHICH lands fill the budget lives in deck_builder_manabase.
    #
    # Land count: from the curve model, reconciled against the seed's own
    # count (the seed wins when it's plausible — it's community-tuned). See
    # ``target_land_count``. We compute pip/curve stats once and reuse them
    # for both the count and the per-color source targets.
    stats = pip_stats(nonlands, lookup)
    land_target = target_land_count(stats.avg_mana_value, seed_land_count)

    # Trim spells to the nonland budget, then read the ACTUAL land count off
    # what's left (when a seed has few spells, the spare slots become lands —
    # exactly the FP-014.1 behavior). Never trim below the kept lands: the
    # tuned base is not up for negotiation, so widen the land budget if the
    # kept lands alone already exceed it.
    nonland_target = MAIN_SIZE - land_target
    if len(nonlands) > nonland_target:
        # More spells than the budget leaves room for — trim the tail
        # (lowest EDHREC priority / least-owned) so lands + spells == 99.
        nonlands = nonlands[:nonland_target]
    land_slots = MAIN_SIZE - len(nonlands)
    if land_slots < len(seed_lands):
        # Kept base is bigger than the budget — keep it all, shed spells.
        land_slots = len(seed_lands)
        nonlands = nonlands[:MAIN_SIZE - land_slots]
        land_slots = MAIN_SIZE - len(nonlands)

    # Colors to fix for: the commander's identity. When identity is
    # unresolved or colorless, fall back to the colors that actually appear
    # in the spells' costs (so a mono-pip pile still gets the right basics).
    ci_colors = [c for c in "WUBRG" if ci and c in ci]
    if not ci_colors:
        ci_colors = [c for c in "WUBRG" if stats.weights.get(c, 0) > 0]

    # Tribal utility lands (Cavern of Souls etc.) apply when the commander's
    # oracle text reads tribal — the same detector the advisor uses. Best-
    # effort: no oracle text → no tribe → no tribal lands.
    tribe = None
    try:
        cmdr_card = lookup(commander)
        if cmdr_card:
            tribe = detect_tribal_type(
                cmdr_card.get("oracle_text") or "",
                cmdr_card.get("type_line") or "",
            )
    except Exception:  # noqa: BLE001 — tribal detection is a nicety, not load-bearing.
        tribe = None

    manabase = build_manabase(
        ci_colors, nonlands, seed_lands, land_slots,
        lookup=lookup, tribe=tribe, stats=stats,
        collection=coll,  # FP-014.3 hook — accepted, not yet consumed.
    )

    # ---- 4b. PERSONALIZATION (FP-014.3) ----------------------------------
    # Three independent, individually-toggleable passes over the NONLAND
    # spell list, applied in the order lift → steer → collection (rationale
    # in deck_builder_personalize's module docstring). Each is a net-zero
    # swap engine (remove one, add one) that never touches the manabase, so
    # the exactly-99 budget computed above survives untouched; singleton and
    # color-identity are re-checked on every incoming card via the closures
    # below. The whole block is wrapped stage-by-stage so a personalization
    # failure degrades to "no personalization", never a failed build.
    (
        nonlands,
        lift_swap_notes,
        lift_skipped,
        steer_notes,
        owned_swaps,
        bracket_estimate,
        buy_list,
    ) = _personalize(
        commander, bracket, nonlands, manabase, ci, lookup, cmdr_key,
        coll=coll,
        collection_path=collection_path,
        enable_lift=enable_lift,
        enable_steer=enable_steer,
        owned_bias=owned_bias,
        deck_dir=deck_dir,
        lift_matrix=lift_matrix,
        estimate_fn=estimate_fn,
        is_game_changer=is_game_changer,
        is_fast_mana=is_fast_mana,
        power_pool=power_pool,
        owned_names=owned_names,
    )

    # ---- 5. OUTPUT + INVARIANT -------------------------------------------
    display_name = name or f"{commander} Build"
    # Stem the dashboard/improve loop and the win-attribution pipeline key
    # on: "[USER] <name> [B<n>]". Name= is stamped to match it (dck_meta).
    stem = f"[USER] {safe_filename(display_name)} [B{bracket}]"
    text = _render_dck(commander, nonlands, manabase.lands, manabase.basics)
    text = dck_meta.rewrite_name(text, stem)

    # Guarantee exactly MAIN_SIZE. The manabase sums to ``land_slots`` and
    # ``land_slots + len(nonlands) == MAIN_SIZE`` by construction, so we
    # should be exact; ``_pad_main_to_target`` is the belt-and-suspenders
    # backstop (reuses the shipped guard — it reads its target off the
    # rendered text's own [Commander] section, which matches MAIN_SIZE for
    # the single-commander decks this builder emits). Overshoot is a real
    # bug — raise rather than emit an illegal deck.
    main = count_main_cards(text)
    if main < MAIN_SIZE:
        text, _added, _breakdown = _pad_main_to_target(text, main)
        main = count_main_cards(text)
    if main != MAIN_SIZE:
        raise RuntimeError(
            f"assembler produced {main} main cards for {commander!r} "
            f"(expected {MAIN_SIZE}); refusing to emit an illegal deck"
        )

    return BuildResult(
        text=text,
        name=display_name,
        stem=stem,
        colors=(ci if ci is not None else "?"),
        nonland_count=len(nonlands),
        land_count=manabase.summary.land_count,
        source=source,
        dropped_off_color=dropped_off_color,
        manabase=manabase.summary,
        bracket_target=bracket,
        bracket_estimate=bracket_estimate,
        lift_swaps=lift_swap_notes,
        lift_skipped=lift_skipped,
        steer_notes=steer_notes,
        owned_swaps=owned_swaps,
        buy_list=buy_list,
    )


def _revalidate_swaps(prev, new, reserved_keys, ci_ok):
    """Re-check the three invariants after a personalization stage.

    A stage is *supposed* to make net-zero, in-color, singleton swaps — but
    "supposed to" is not "the emitted file may break it". This guard proves
    the post-stage list still (a) has the same length (exactly-99 budget
    intact), (b) is a genuine singleton (no key repeated, none colliding with
    a committed land/basic/commander), and (c) is entirely in color identity.
    If any check fails we DISCARD the stage's output and keep the pre-stage
    list — a personalization bug degrades to "no personalization", it never
    emits an illegal deck. Returns ``(list_to_use, ok)``.
    """
    if len(new) != len(prev):
        return prev, False
    seen: set[str] = set()
    for nm in new:
        k = name_key(nm)
        if k in reserved_keys or k in seen:
            return prev, False
        seen.add(k)
        if not ci_ok(nm):
            return prev, False
    return new, True


def _personalize(
    commander, bracket, nonlands, manabase, ci, lookup, cmdr_key,
    *, coll, collection_path, enable_lift, enable_steer, owned_bias,
    deck_dir, lift_matrix, estimate_fn, is_game_changer, is_fast_mana,
    power_pool, owned_names,
):
    """Run the FP-014.3 stages over ``nonlands``; return the provenance.

    Returns ``(nonlands, lift_notes, lift_skipped, steer_notes, owned_swaps,
    bracket_estimate, buy_list)``. Every stage is wrapped so its failure is
    contained, and every stage's output is re-validated by
    ``_revalidate_swaps`` before it's accepted.
    """
    lift_notes: list[str] = []
    lift_skipped: Optional[str] = None
    steer_notes: list[str] = []
    owned_swaps: list[str] = []
    bracket_estimate: Optional[int] = None
    buy_list: list[str] = []

    # Keys a swap candidate must never collide with: the commander and every
    # land/basic already committed (personalization only trades nonlands).
    reserved: set[str] = {cmdr_key}
    reserved |= {name_key(land) for land in manabase.lands}
    reserved |= {name_key(b) for b in manabase.basics}

    # --- shared closures (all route through the injected ``lookup``) -------
    _role_cache: dict[str, str] = {}

    def role_of(nm: str) -> str:
        k = name_key(nm)
        if k not in _role_cache:
            try:
                card = lookup(nm) or {}
            except Exception:  # noqa: BLE001
                card = {}
            _role_cache[k] = classify_role_extended(
                card.get("oracle_text", "") or "",
                card.get("type_line", "") or "",
            )
        return _role_cache[k]

    def ci_ok(nm: str) -> bool:
        # ci is None → identity unresolved → enforce_color_identity passes
        # everything through (same degrade as the base assembler).
        if ci is None:
            return True
        kept, _dropped = enforce_color_identity([nm], ci)
        return bool(kept)

    def mv_of(nm: str) -> Optional[float]:
        try:
            card = lookup(nm)
        except Exception:  # noqa: BLE001
            return None
        if not card:
            return None
        _pips, mv = _parse_cost(card.get("mana_cost") or "")
        return mv

    # Resolve the lift matrix once (both lift + steer read it). None when
    # personalization is off / no corpus is available.
    matrix = lift_matrix
    if matrix is None and deck_dir is not None and (enable_lift or enable_steer):
        try:
            matrix = lift_analysis.load_or_build_matrix(Path(deck_dir))
        except Exception:  # noqa: BLE001 — a corpus read blip disables lift.
            matrix = None

    # quality = lift deck-synergy over the ORIGINAL shell (built before any
    # swaps so all stages score against the same baseline fabric).
    base_deck_keys = {cmdr_key} | {name_key(n) for n in nonlands}
    quality_of = personalize.synergy_scorer(matrix, bracket, base_deck_keys)

    # --- STAGE 1: LIFT SWAPS ----------------------------------------------
    if enable_lift:
        try:
            new, lift_notes, lift_skipped = personalize.lift_swaps(
                nonlands, commander=commander, bracket=bracket, matrix=matrix,
                reserved_keys=reserved, role_of=role_of, ci_ok=ci_ok,
            )
            nonlands, ok = _revalidate_swaps(nonlands, new, reserved, ci_ok)
            if not ok:
                lift_notes = []
                lift_skipped = "lift swaps discarded (invariant re-check)"
        except Exception:  # noqa: BLE001
            lift_skipped = "lift stage error"

    # --- STAGE 2: BRACKET STEERING ----------------------------------------
    if enable_steer:
        try:
            _estimate = estimate_fn or (
                lambda t: estimate_bracket(t, declared=bracket)
            )
            _is_gc = is_game_changer or _default_is_game_changer()
            _is_fm = is_fast_mana or _default_is_fast_mana()
            pool = power_pool
            if pool is None:
                # Corpus-sourced power candidates: the lift picks for this
                # commander+shell that happen to be power cards belong to
                # THIS deck (unlike a generic "all Game Changers" list).
                pool = _default_power_pool(matrix, commander, nonlands, bracket)

            def render_fn(nl):
                return _render_dck(
                    commander, nl, manabase.lands, manabase.basics,
                )

            new, steer_notes, bracket_estimate = personalize.steer_bracket(
                nonlands, target=bracket, render_fn=render_fn,
                estimate_fn=_estimate, is_game_changer=_is_gc,
                is_fast_mana=_is_fm, candidate_pool=pool,
                reserved_keys=reserved, ci_ok=ci_ok,
                gc_cap=personalize.GC_CAP_BY_BRACKET.get(bracket, 10 ** 9),
            )
            nonlands, ok = _revalidate_swaps(nonlands, new, reserved, ci_ok)
            if not ok:
                steer_notes = ["bracket steer discarded (invariant re-check)"]
        except Exception:  # noqa: BLE001
            steer_notes = []

    # --- STAGE 3: COLLECTION BIAS -----------------------------------------
    if coll is not None and owned_bias:
        try:
            names = owned_names
            if names is None and collection_path is not None:
                try:
                    names = parse_collection_lines(
                        Path(collection_path).read_text(encoding="utf-8")
                    )
                except OSError:
                    names = []
            names = names or []
            _is_gc = is_game_changer or _default_is_game_changer()
            _is_fm = is_fast_mana or _default_is_fast_mana()
            # Never trade away a power card the steer stage placed — that
            # would silently re-open the bracket gap.
            protect = lambda nm: _is_gc(nm) or _is_fm(nm)  # noqa: E731
            new, owned_swaps = personalize.apply_collection_bias(
                nonlands, collection=coll, owned_pool=names, ci_ok=ci_ok,
                role_of=role_of, mv_of=mv_of, quality_of=quality_of,
                reserved_keys=reserved, protect=protect,
            )
            nonlands, ok = _revalidate_swaps(nonlands, new, reserved, ci_ok)
            if not ok:
                owned_swaps = []
        except Exception:  # noqa: BLE001
            owned_swaps = []

    # --- BUY-LIST: still-unowned cards in the FINAL 99 --------------------
    # Only meaningful when a collection is registered AND owned-bias is on
    # (the buy-list is the collection feature's report; --no-owned-bias
    # turns the whole feature, buy-list included, off). Basics are always
    # "owned" (owns() short-circuits them), so they never appear.
    if coll is not None and owned_bias:
        unowned = [n for n in nonlands if not owns(coll, n)]
        unowned += [land for land in manabase.lands if not owns(coll, land)]
        buy_list = sorted(dict.fromkeys(unowned))

    return (
        nonlands, lift_notes, lift_skipped, steer_notes, owned_swaps,
        bracket_estimate, buy_list,
    )


def _default_is_game_changer() -> Callable[[str], bool]:
    """Production Game-Changer predicate: name is on the official GC list
    (offline fallback on any failure — a GC-list blip must not crash steer).
    """
    try:
        from .game_changers import load_game_changers
        gc = {g.lower() for g in load_game_changers()}
    except Exception:  # noqa: BLE001
        gc = set()
    return lambda nm: name_key(nm) in gc


def _default_is_fast_mana() -> Callable[[str], bool]:
    """Production fast-mana predicate: reuse bracket_estimator's own
    fast-mana list so 'power' means the same thing the estimator scores."""
    try:
        from .bracket_estimator import _FAST_MANA_CARDS
        fm = set(_FAST_MANA_CARDS)
    except Exception:  # noqa: BLE001
        fm = set()
    return lambda nm: name_key(nm) in fm


def _default_power_pool(matrix, commander, nonlands, bracket) -> list[str]:
    """Corpus-sourced candidate power cards for the steer stage.

    The lift candidates for this commander+shell are, by construction, cards
    the harvested community pairs with THIS deck — so when some of them are
    Game Changers / fast mana they're commander-appropriate power to add
    (far better than a blind 'every GC in-color' list). Empty when there's
    no usable corpus: without a source we can only soften, never add — the
    honest limit, reported in the steer notes.
    """
    if not matrix or matrix.get("too_small"):
        return []
    deck_keys = {name_key(commander)} | {name_key(n) for n in nonlands}
    try:
        cands = lift_analysis.lift_candidates(
            matrix, deck_keys, bracket=bracket, limit=30,
        )
    except Exception:  # noqa: BLE001
        return []
    return [c["card"] for c in cands]


def _render_dck(
    commander: str,
    nonlands: list[str],
    lands: list[str],
    basics: dict[str, int],
) -> str:
    """Render the Forge ``.dck`` text: [metadata]/[Commander]/[Main].

    Matches ``moxfield_import.to_dck``'s section layout so the dashboard and
    improve loop accept the file unchanged. ``lands`` are the singleton
    nonbasic lands (kept-from-seed + topped-up fixing); ``basics`` is the
    basic-land multiset. Card lines are name-only (no ``|SET|CN`` edition
    tail) — Forge falls back to any printing, and resolving an exact printing
    per card would mean a Scryfall round-trip for all 99. The ``Name=`` here
    is a placeholder; the caller stamps the real stem via
    ``dck_meta.rewrite_name``.
    """
    lines = ["[metadata]", "Name=", "[Commander]", f"1 {commander}", "[Main]"]
    lines.extend(f"1 {nm}" for nm in nonlands)
    lines.extend(f"1 {nm}" for nm in lands)  # singleton nonbasic lands.
    lines.extend(f"{qty} {basic}" for basic, qty in basics.items() if qty > 0)
    return "\n".join(lines) + "\n"


def build_deck(
    commander: str,
    bracket: int,
    collection_path: Optional[Path] = None,
    *,
    fetch_avg: Optional[Callable] = None,
    fetch_page: Optional[Callable] = None,
    resolve_ci: Optional[Callable[[str], Optional[str]]] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    name: Optional[str] = None,
    enable_lift: bool = True,
    enable_steer: bool = True,
    owned_bias: bool = True,
    deck_dir: Optional[Path] = None,
    **personalize_kwargs,
) -> str:
    """Build a legal 99-card Commander deck for ``commander`` and return the
    Forge ``.dck`` TEXT (the caller decides where to write it).

    Thin wrapper over ``_assemble`` — see that function and the module
    docstring for the full contract and the honest scope notes. The FP-014.3
    toggles + injectable seams pass straight through (``**personalize_kwargs``
    forwards the lift-matrix / estimator / power-pool test hooks).
    """
    return _assemble(
        commander,
        bracket,
        collection_path,
        fetch_avg=fetch_avg,
        fetch_page=fetch_page,
        resolve_ci=resolve_ci,
        lookup=lookup,
        name=name,
        enable_lift=enable_lift,
        enable_steer=enable_steer,
        owned_bias=owned_bias,
        deck_dir=deck_dir,
        **personalize_kwargs,
    ).text


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv=None) -> int:
    """``commander-build`` — assemble a from-scratch deck for a commander.

    Writes ``[USER] <name> [B<n>].dck`` into the deck dir and prints the
    path plus a one-line summary. EDHREC-unavailable errors print cleanly
    (no stacktrace).
    """
    parser = argparse.ArgumentParser(
        prog="commander-build",
        description=(
            "Build a legal 99-card Commander deck from a commander + target "
            "bracket. Coherence is seeded from EDHREC's average deck (see "
            "FP-014); the manabase is basics-only in this first cut."
        ),
    )
    parser.add_argument("--commander", required=True,
                        help="Commander card name (e.g. \"Krenko, Mob Boss\").")
    parser.add_argument("--bracket", type=int, default=3,
                        help="Target power bracket 1-5 (default 3 = Upgraded).")
    parser.add_argument("--deck-dir", "--out", dest="deck_dir", default=None,
                        help="Directory to write the .dck into "
                             "(default: the project's commander deck dir).")
    parser.add_argument("--collection", default=None,
                        help="Path to a collection file. Enables owned-card "
                             "bias (FP-014.3) unless --no-owned-bias is set.")
    # FP-014.3 personalization toggles — each stage is ON by default (it is
    # the point of a from-scratch build); these turn a stage off.
    parser.add_argument("--no-lift", dest="enable_lift", action="store_false",
                        help="Disable lift-driven synergy swaps (FP-014.3).")
    parser.add_argument("--no-steer", dest="enable_steer", action="store_false",
                        help="Disable bracket steering (FP-014.3).")
    parser.add_argument("--no-owned-bias", dest="owned_bias",
                        action="store_false",
                        help="Disable owned-card bias even when a "
                             "--collection is given (FP-014.3).")
    parser.set_defaults(enable_lift=True, enable_steer=True, owned_bias=True)
    # FP-014.4 HAND-OFF (the validation moat): after building, optionally
    # hand the fresh .dck straight to commander-improve so the from-scratch
    # pile gets MEASURED (Forge A/B sims) and tuned. Gated behind explicit
    # opt-in because it costs real Forge wall-time AND Anthropic tokens — a
    # from-scratch build must never silently trigger that. ``--improve`` with
    # no value runs a sensible default number of rounds; ``--improve N`` sets
    # the round budget.
    parser.add_argument(
        "--improve", dest="improve_rounds", nargs="?", type=int,
        const=3, default=None, metavar="ROUNDS",
        help="After building, hand the deck to commander-improve for ROUNDS "
             "greedy Forge-sim tuning rounds (default 3). Costs Forge time + "
             "Anthropic tokens — opt-in only.",
    )
    args = parser.parse_args(argv)

    if args.bracket not in (1, 2, 3, 4, 5):
        parser.error(f"--bracket must be 1-5, got {args.bracket}")

    collection_path = Path(args.collection) if args.collection else None
    deck_dir = Path(args.deck_dir) if args.deck_dir else DECK_OUT_DIR
    try:
        result = _assemble(
            args.commander,
            args.bracket,
            collection_path,
            # The corpus lift matrix is read from the deck dir the build
            # writes into — the same pool the harvester/improve loop use.
            deck_dir=deck_dir,
            enable_lift=args.enable_lift,
            enable_steer=args.enable_steer,
            owned_bias=args.owned_bias,
        )
    except ValueError as exc:
        # Clean, user-facing message (e.g. "cannot build: no EDHREC data
        # for <commander>") — never a stacktrace.
        print(str(exc))
        return 1

    deck_dir.mkdir(parents=True, exist_ok=True)
    out_path = deck_dir / f"{result.stem}.dck"
    out_path.write_text(result.text, encoding="utf-8")

    colors = result.colors or "colorless"
    print(f"Wrote {out_path}")
    print(
        f"  name: {result.name}   colors: {colors}   "
        f"nonland: {result.nonland_count}   land: {result.land_count}   "
        f"source: {result.source}"
    )
    # FP-014.2 manabase quality: per-color sources vs target so the manabase
    # is inspectable at a glance (not just a land count).
    for line in result.manabase.format_lines():
        print(line)
    if result.dropped_off_color:
        print(f"  dropped off-color: {', '.join(result.dropped_off_color)}")

    # ---- FP-014.3 personalization summary --------------------------------
    if result.lift_swaps:
        print(f"  lift swaps ({len(result.lift_swaps)}):")
        for note in result.lift_swaps:
            print(f"    - {note}")
    elif result.lift_skipped:
        print(f"  lift swaps: skipped — {result.lift_skipped}")
    if result.bracket_estimate is not None:
        verdict = (
            "meets target" if result.bracket_estimate == result.bracket_target
            else ("below target" if result.bracket_estimate <
                  result.bracket_target else "above target")
        )
        print(
            f"  bracket: estimate B{result.bracket_estimate} vs "
            f"target B{result.bracket_target} ({verdict})"
        )
        for note in result.steer_notes:
            print(f"    - {note}")
    if result.owned_swaps:
        print(f"  owned-bias swaps ({len(result.owned_swaps)}):")
        for note in result.owned_swaps:
            print(f"    - {note}")
    if collection_path is not None and args.owned_bias:
        print(f"  buy-list (still unowned): {len(result.buy_list)} card(s)")

    # ---- FP-014.4 HAND-OFF to commander-improve (explicit opt-in) --------
    # We DELEGATE to improve_main rather than re-implement the loop: it owns
    # deck resolution, bracket inference, the sub-threshold sim-games warning,
    # intent learning, and the greedy keep-if-better contract. Passing the
    # freshly-written stem + the same deck-dir + bracket means improve reads
    # exactly the file we just wrote (Name= stamped, dashboard-loadable). Its
    # exit code is returned so a failed tuning run is visible to the caller.
    if args.improve_rounds is not None:
        print(
            f"\n[build] handing {result.stem!r} to commander-improve for "
            f"{args.improve_rounds} round(s) (Forge + Anthropic time)...",
            flush=True,
        )
        from .improve import improve_main
        return improve_main([
            "--deck", result.stem,
            "--deck-dir", str(deck_dir),
            "--bracket", str(args.bracket),
            "--rounds", str(args.improve_rounds),
        ])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
