"""FP-014.1 — build-from-scratch deck assembly (commander → legal 99).

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

WHAT IS DELIBERATELY DEFERRED.
==============================
  * FP-014.2 (real manabase): the manabase HERE is basics-only. We strip
    every land the seed carried (including its real dual/fetch/shock base)
    and rebuild the land slots from basic lands alone, distributed across
    the commander's colors by the pip weight of the nonland spells. Proper
    color-source math — how many sources of each color a curve actually
    needs, which nonbasic lands to run — is the genuinely hard research
    step and is out of scope for this slice. See ``_distribute_basics_by_pips``.

  * FP-014.3 (owned-card preference): when a collection is supplied we apply
    only a MINIMAL owned-bias (keep owned cards first when we have to trim).
    Full owned-aware fill/substitution is future work.

The assembler reuses the shipped, tested substrate rather than re-deriving
it: ``enforce_color_identity`` for legality, ``count_main_cards`` +
``_pad_main_to_99`` for the exactly-99 guard, ``dck_meta.rewrite_name`` for
the name-stamp invariant, and ``moxfield_import``'s filename/section
conventions so the dashboard/improve loop accept the output unchanged.

Fetchers and resolvers are injectable so tests run fully offline.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import dck_meta
from ._proposer_filters import enforce_color_identity
from .collection import load_collection, name_key, owns
from .dck_utils import count_main_cards
from .edhrec_client import fetch_average_deck, fetch_commander_page
from .moxfield_import import DECK_OUT_DIR, safe_filename
from .scryfall_client import lookup_card, normalize_color_identity
from .staples import ROLE_TARGETS, is_basic_land
from .web.deck_text_ops import _pad_main_to_99

# Commander decks are exactly 100 cards: 1 commander in the command zone +
# 99 in the mainboard. Every path below funnels to this invariant.
MAIN_SIZE = 99

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
        ``deck_text_ops._pad_main_to_99`` already use, so the counts always
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

    # ---- 3. LEGALITY: drop commander + all lands, hold singleton ---------
    # Every land (basic AND nonbasic) is dropped here; the manabase is
    # rebuilt basics-only in step 4. Discarding the seed's real dual/fetch
    # base is the deliberate FP-014.1 simplification (FP-014.2 keeps/selects
    # nonbasics with color-source math).
    cmdr_key = name_key(commander)
    seen: set[str] = set()
    nonlands: list[str] = []
    for nm in raw_names:
        if not nm or not nm.strip():
            continue
        k = name_key(nm)
        if k == cmdr_key:
            continue  # commander lives in the command zone, not [Main].
        if _is_land(nm, lookup):
            continue
        if k in seen:
            continue  # singleton: no duplicate nonbasics.
        seen.add(k)
        nonlands.append(nm)

    # Color-identity legality over the nonland picks. ``ci is None`` makes
    # this a pass-through (can't verify → don't strip everything) — the
    # advisor degrades the same way; warn so it's not silent.
    nonlands, dropped_off_color = enforce_color_identity(nonlands, ci)
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

    # ---- 4. MANABASE (basics-only) + exactly-99 sizing -------------------
    land_target = seed_land_count if seed_land_count else DEFAULT_LAND_TARGET
    nonland_target = MAIN_SIZE - land_target
    if len(nonlands) > nonland_target:
        # More spells than the land target leaves room for — trim the tail
        # (lowest EDHREC priority / least-owned) so lands + spells == 99.
        nonlands = nonlands[:nonland_target]
    basics_needed = MAIN_SIZE - len(nonlands)

    # Colors to spread basics across: the commander's identity. When identity
    # is unresolved or colorless, fall back to the colors that actually
    # appear in the nonland spells' costs (so a mono-pip pile still gets the
    # right basic even without a resolved CI).
    weights = _pip_weights(nonlands, lookup)
    ci_colors = [c for c in "WUBRG" if ci and c in ci]
    if not ci_colors:
        ci_colors = [c for c in "WUBRG" if weights.get(c, 0) > 0]
    basics = _distribute_basics_by_pips(ci_colors, weights, basics_needed)

    # ---- 5. OUTPUT + INVARIANT -------------------------------------------
    display_name = name or f"{commander} Build"
    # Stem the dashboard/improve loop and the win-attribution pipeline key
    # on: "[USER] <name> [B<n>]". Name= is stamped to match it (dck_meta).
    stem = f"[USER] {safe_filename(display_name)} [B{bracket}]"
    text = _render_dck(commander, nonlands, basics)
    text = dck_meta.rewrite_name(text, stem)

    # Guarantee exactly 99. The pip split already sums to ``basics_needed``
    # so we should be exact; ``_pad_main_to_99`` is the belt-and-suspenders
    # backstop (reuses the shipped guard). Overshoot is a real bug — raise
    # rather than emit an illegal deck.
    main = count_main_cards(text)
    if main < MAIN_SIZE:
        text, _added, _breakdown = _pad_main_to_99(text, main)
        main = count_main_cards(text)
    if main != MAIN_SIZE:
        raise RuntimeError(
            f"assembler produced {main} main cards for {commander!r} "
            f"(expected {MAIN_SIZE}); refusing to emit an illegal deck"
        )

    land_count = sum(basics.values())
    return BuildResult(
        text=text,
        name=display_name,
        stem=stem,
        colors=(ci if ci is not None else "?"),
        nonland_count=len(nonlands),
        land_count=land_count,
        source=source,
        dropped_off_color=dropped_off_color,
    )


def _render_dck(
    commander: str, nonlands: list[str], basics: dict[str, int],
) -> str:
    """Render the Forge ``.dck`` text: [metadata]/[Commander]/[Main].

    Matches ``moxfield_import.to_dck``'s section layout so the dashboard and
    improve loop accept the file unchanged. Card lines are name-only (no
    ``|SET|CN`` edition tail) — Forge falls back to any printing, and
    resolving an exact printing per card would mean a Scryfall round-trip
    for all 99. The ``Name=`` here is a placeholder; the caller stamps the
    real stem via ``dck_meta.rewrite_name``.
    """
    lines = ["[metadata]", "Name=", "[Commander]", f"1 {commander}", "[Main]"]
    lines.extend(f"1 {nm}" for nm in nonlands)
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
) -> str:
    """Build a legal 99-card Commander deck for ``commander`` and return the
    Forge ``.dck`` TEXT (the caller decides where to write it).

    Thin wrapper over ``_assemble`` — see that function and the module
    docstring for the full contract and the honest scope notes.
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
                        help="Path to a collection file for a minimal "
                             "owned-card bias (FP-014.3 = full preference).")
    args = parser.parse_args(argv)

    if args.bracket not in (1, 2, 3, 4, 5):
        parser.error(f"--bracket must be 1-5, got {args.bracket}")

    collection_path = Path(args.collection) if args.collection else None
    try:
        result = _assemble(
            args.commander,
            args.bracket,
            collection_path,
        )
    except ValueError as exc:
        # Clean, user-facing message (e.g. "cannot build: no EDHREC data
        # for <commander>") — never a stacktrace.
        print(str(exc))
        return 1

    deck_dir = Path(args.deck_dir) if args.deck_dir else DECK_OUT_DIR
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
    if result.dropped_off_color:
        print(f"  dropped off-color: {', '.join(result.dropped_off_color)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
