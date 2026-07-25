"""Opening-hand / mulligan / commander-on-curve consistency math.

WHY THIS MODULE EXISTS
======================
The project can already answer "does this deck WIN?" — that's Forge,
``compare_versions``, and the soak pipeline. It could not answer the
cheaper and logically prior question: **"does this deck FUNCTION?"**
Nothing in the tree did hypergeometric or opening-hand analysis
(grepped 2026-07-24: genuinely absent).

That gap matters more in Commander than anywhere else. Commander is a
100-card SINGLETON format: you cannot run four copies of your best
card, so raw card quality converges across decks and **consistency**
— do you hit land drops, can you cast your commander on curve, do
your colors show up — is what actually separates a good deck from a
bad one. A 30-land deck and a 38-land deck can hold identical card
lists and play like different games.

``docs/architecture.md`` (Step 5.6 note) already flagged the value:
a *"pre-execute consistency check (mulligan rate, commander-turn) has
independent value before committing to a full sim."* It was never
built. This is that check.

DESIGN CONTRACT
===============
* **Offline.** Every card touch routes through an injected ``lookup``
  (defaults to ``deck_health._lookup_card_safe``, i.e. the disk-cached
  Scryfall client). Tests pass a stub and nothing reaches the network.
* **Deterministic.** Seeded ``random.Random(seed)`` only — never the
  global ``random``. Same seed + same deck ⇒ byte-identical dict. This
  feeds a regression dataset (FP-002), so a drifting number would be
  indistinguishable from a real deck change.
* **Fast.** Milliseconds-to-seconds, not minutes. Only the prefix of
  the library that a trial can actually see is shuffled (partial
  Fisher-Yates), and the closed-form layer needs no trials at all.
* **Pure stdlib.** ``math.comb`` + ``random`` + ``statistics``-free
  arithmetic. numpy / scipy / sklearn are NOT installed on the soak
  boxes (same constraint ``scripts/margin_analysis.py`` documents).
* **Fail-quiet, never fabricate.** A Scryfall outage returns ``None``,
  never a plausible-looking 0.0. See the outage contract below.

TWO LAYERS
==========
1. **Closed form** (``hypergeom_pmf`` / ``hypergeom_at_least``) —
   exact rational arithmetic, no simulation, no deck lookup. "What are
   the odds of seeing at least one of my two tutors in the opening 7?"
2. **Seeded Monte Carlo** (``opening_hand_stats``) — plays the actual
   decklist out: London mulligans, land drops turn by turn, colored
   sources, commander castability. Simulation (not closed form) is
   required here because mulligan policy, land SEQUENCING, and colored
   sources are path-dependent — a hypergeometric can tell you how many
   lands you drew, not whether they were the right ones in time.

OUTAGE CONTRACT (mirrors ``deck_health``)
=========================================
``opening_hand_stats`` returns ``None`` when the deck is empty or when
MORE than half the deck's card lines fail to resolve
(``if lines and failed_lines * 2 > lines: return None``) — the exact
majority-failure guard ``deck_health._mana_health_signal`` /
``compute_spell_density`` use. Below that threshold, unresolved cards
stay in the library as non-land, uncastable "unknown" cards: the deck
size stays the true printed size (99 cards is a structural fact, not a
lookup result) and the bias runs PESSIMISTIC — an unresolved card can
only make the reported numbers worse, never invent a success. The
count is surfaced as ``lookup_failures`` so a caller can annotate.
``p_commander_on_curve`` is independently ``None`` when the commander
itself can't be resolved: no mana cost means no on-curve turn, and
guessing one would be the fabricated number the contract forbids.

LAND DETECTION — REUSED, NOT REIMPLEMENTED
==========================================
Front-face type line decides, exactly as
``deck_health._mana_health_signal`` does ("Sorcery // Land" MDFCs are
spells you sometimes play as lands, so the front face decides), with
``staples.is_basic_land`` as the no-round-trip fast path and
``deck_health._MDFC_LANDS`` as the curated spell-front MDFC list.
``staples.is_land`` is the same rule but binds the module-level
``lookup_card`` directly, so it cannot be pointed at a test stub —
this module needs the injected-lookup form, and resolves to the same
Scryfall data in production.

ONE DELIBERATE DIVERGENCE from ``_mana_health_signal``: that signal
weights a spell-front MDFC as **0.5** lands, which is the right
DECK-CONSTRUCTION weighting (across 99 cards you cast some of them as
spells). In a SPECIFIC opening hand there is no half a land — you
either need the drop or you don't, and if you need it the MDFC makes
it. So the simulation counts a spell-front MDFC as a full land, and
the returned dict also carries ``effective_land_count`` under
``_mana_health_signal``'s 0.5 convention so the two modules'
land counts can be reconciled at a glance.

Colored sources come from ``deck_builder_manabase.land_color_sources``
and pips from its ``_parse_cost`` — the Karsten-model helpers already
in the tree. Nothing about mana is re-derived here.

MODELLING ASSUMPTIONS (all deliberate, all conservative)
========================================================
* **No free mulligan.** London: draw 7, bottom N. Most Commander
  playgroups do NOT use the free mulligan, and Wizards' Commander
  rules don't grant one; assuming one would flatter every deck.
* **Lands only — ramp is NOT modelled.** Mana rocks and dorks would
  only ever make ``p_commander_on_curve`` go UP, so the reported
  number is an honest FLOOR, not an estimate. A deck that clears the
  bar on lands alone genuinely clears it.
* **One land drop per turn**, no extra-land effects, no fetch/bounce
  timing.
* **On the play by default** (``CONVENTION``) — the harsher read.
  Both conventions are always returned (``on_play`` / ``on_draw``
  sub-dicts, evaluated on the SAME shuffles so the pair is directly
  comparable), because at a 4-player table you are on the draw three
  seats out of four and the difference is a full extra card by turn 3.

Public API: ``hypergeom_pmf``, ``hypergeom_at_least``,
``opening_hand_stats``, ``format_consistency_report``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from . import dck_utils
from .deck_builder_manabase import _parse_cost, land_color_sources
from .staples import is_basic_land

__all__ = [
    "CONVENTION",
    "DEFAULT_TRIALS",
    "KEEPABLE_LAND_MAX",
    "KEEPABLE_LAND_MIN",
    "MAX_MULLIGANS",
    "MDFC_LAND_WEIGHT",
    "OPENING_HAND_SIZE",
    "format_consistency_report",
    "hypergeom_at_least",
    "hypergeom_pmf",
    "opening_hand_stats",
]


# ---------------------------------------------------------------------------
# Tunable constants -- every threshold in this module is named here.
# ---------------------------------------------------------------------------

#: Cards drawn for an opening hand. London always draws 7 regardless of
#: mulligan depth; the bottoming is what shrinks the kept hand.
OPENING_HAND_SIZE = 7

#: "Keepable" band, in LANDS, for a 7-card opener. A 7-card Commander
#: hand with 0-1 lands cannot reliably make its turn-3 drop and a hand
#: with 6-7 lands has no game; both are ships. 2-5 is the band the
#: mulligan literature and every Commander primer converge on, and it
#: brackets the 3-land "textbook keep" symmetrically. Deliberately
#: land-count-only: a spell-quality keep rule would need card
#: evaluation this module does not have and could not defend.
KEEPABLE_LAND_MIN = 2
KEEPABLE_LAND_MAX = 5

#: Land count the bottoming policy steers toward when it has a choice.
PREFERRED_KEEP_LANDS = 3

#: Mulligans taken before keeping whatever the 7 shows. 2 ⇒ the worst
#: kept hand is 5 cards. Beyond that, in a singleton 99 with no free
#: mulligan, digging costs more than the bad keep.
MAX_MULLIGANS = 2

#: Land-in-play checkpoints reported as ``p_<n>_lands_by_t<t>``.
LAND_CHECKPOINTS: tuple[tuple[int, int], ...] = ((3, 3), (5, 5))

#: Turn at which color screw is assessed -- the first turn a Commander
#: deck is expected to actually deploy something colored.
COLOR_SCREW_TURN = 3

#: ``_mana_health_signal``'s spell-front-MDFC weighting, reused only
#: for the reported ``effective_land_count`` cross-reference (the
#: simulation itself counts an MDFC as a full land -- see the module
#: docstring's "ONE DELIBERATE DIVERGENCE").
MDFC_LAND_WEIGHT = 0.5

#: Default trial count. 10k puts the Monte-Carlo standard error near
#: 0.5pp at p=0.5 (sqrt(.25/10000)), well below the effect sizes that
#: matter for a deck-construction decision.
DEFAULT_TRIALS = 10_000

#: Which sub-dict the top-level convenience keys alias.
CONVENTION = "on_play"


# ---------------------------------------------------------------------------
# Layer 1 -- closed-form hypergeometric (exact, no simulation)
# ---------------------------------------------------------------------------

def hypergeom_pmf(
    population: int, successes: int, draws: int, k: int,
) -> Optional[float]:
    """P(EXACTLY ``k`` successes) drawing ``draws`` from ``population``.

    The urn is the library: ``population`` cards of which ``successes``
    are the thing you want, ``draws`` cards seen. Sampling without
    replacement -- the whole reason a 99-card singleton deck needs this
    and not a binomial.

    Returns ``None`` for incoherent parameters (negatives, or more
    successes than the population holds). That is the module's
    fail-quiet contract applied to arithmetic: "you asked something
    unanswerable" must not come back as ``0.0``, which is a perfectly
    valid probability and would be silently believed.

    Impossible-but-coherent asks return a real ``0.0`` (e.g. k greater
    than the successes in the deck). ``draws`` above ``population`` is
    clamped to ``population`` -- you cannot draw more cards than exist,
    and "draw the whole deck" is the honest reading.

    PRECISION: the binomials a 99-card deck produces are large
    (``comb(99, 7)`` ~ 1.6e10, and intermediate products far larger),
    so numerator and denominator are accumulated as EXACT Python ints
    and divided exactly once, at the end. ``int.__truediv__`` rounds
    the true rational correctly regardless of operand magnitude, so no
    intermediate float ever exists to lose bits or overflow. Building
    the sum out of float pmf terms instead would accumulate error term
    by term, which is exactly the trap the docstring's caller is here
    to avoid.
    """
    if population < 0 or successes < 0 or draws < 0:
        return None
    if successes > population:
        return None
    draws = min(draws, population)
    if k < 0 or k > draws or k > successes:
        return 0.0
    failures = population - successes
    if draws - k > failures:
        return 0.0  # not enough non-successes to fill the rest of the draw
    numerator = math.comb(successes, k) * math.comb(failures, draws - k)
    denominator = math.comb(population, draws)  # >= 1: draws <= population
    return numerator / denominator


def hypergeom_at_least(
    population: int, successes: int, draws: int, k: int,
) -> Optional[float]:
    """P(AT LEAST ``k`` successes) drawing ``draws`` from ``population``.

    The workhorse: "what are the odds I open at least one of my two
    ramp spells", "at least 2 lands in 7 of 99".

    Degenerate cases, all real answers rather than errors:
      * ``k <= 0``  -> 1.0 (you always draw at least zero of anything);
      * ``successes == 0`` -> 1.0 for k<=0, else 0.0 (nothing to hit);
      * ``draws > population`` -> clamped to drawing the whole library;
      * ``k`` above ``min(successes, draws)`` -> 0.0 (unreachable).
    Incoherent parameters (negatives, successes > population) return
    ``None``, same contract as ``hypergeom_pmf``.

    Sums the SHORTER tail (complementing when that is the low end) so
    the exact-integer term count stays small, then clamps into [0, 1]
    to absorb the final division's last-bit rounding -- a returned
    1.0000000000000002 would be a correctness bug for a caller
    formatting percentages or asserting a bound.
    """
    if population < 0 or successes < 0 or draws < 0:
        return None
    if successes > population:
        return None
    draws = min(draws, population)
    if k <= 0:
        return 1.0
    max_hits = min(successes, draws)
    if k > max_hits:
        return 0.0
    # Sum whichever tail has fewer terms; both are exact.
    upper_terms = max_hits - k + 1
    lower_terms = k
    if upper_terms <= lower_terms:
        total = 0.0
        for i in range(k, max_hits + 1):
            term = hypergeom_pmf(population, successes, draws, i)
            if term is None:
                return None
            total += term
    else:
        total = 1.0
        for i in range(0, k):
            term = hypergeom_pmf(population, successes, draws, i)
            if term is None:
                return None
            total -= term
    return min(1.0, max(0.0, total))


# ---------------------------------------------------------------------------
# Layer 2 -- deck model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Card:
    """One physical card in the simulated 99.

    ``is_land`` is land-CAPABLE (front-face land or curated spell-front
    MDFC): can this card be put onto the battlefield as a land drop?
    ``is_true_land`` is the strict front-face reading, kept separately
    so the reported land counts can be reconciled with
    ``deck_health._mana_health_signal``'s 0.5-weighted view.
    ``sources`` is the set of the deck's colors this card taps for when
    played as a land (empty for spells and colorless utility lands).
    ``pips``/``mv`` describe the card as a SPELL (empty/0.0 for lands
    and for cards whose cost could not be resolved).
    """
    name: str
    is_land: bool
    is_true_land: bool
    sources: frozenset
    pips: tuple  # ((color, count), ...) -- tuple so the card stays frozen
    mv: float
    resolved: bool


def _default_lookup(name: str) -> Optional[dict]:
    """Production lookup: ``deck_health._lookup_card_safe``, the same
    try/except-wrapped disk-cached Scryfall client the deck-health
    signals use. Imported lazily so this module stays importable (and
    testable) without touching the Scryfall layer at all."""
    from .deck_health import _lookup_card_safe
    return _lookup_card_safe(name)


def _front_type_line(card: dict) -> str:
    """Front-face type line, lowercased -- ``_mana_health_signal``'s
    exact rule, including its MDFC/split fallback to per-face data."""
    type_line = card.get("type_line") or ""
    if not type_line:
        faces = card.get("card_faces") or []
        if faces:
            type_line = " // ".join(
                (f or {}).get("type_line") or "" for f in faces
            )
    return type_line.split("//")[0].lower()


def _front_mana_cost(card: dict) -> str:
    """Front-face mana cost. Same per-face fallback shape as the type
    line: an MDFC/adventure carries its cost only on ``card_faces``,
    and the FRONT face is the one you cast."""
    cost = card.get("mana_cost") or ""
    if cost.strip():
        return cost
    faces = card.get("card_faces") or []
    if faces:
        return (faces[0] or {}).get("mana_cost") or ""
    return ""


def _card_identity(card: Optional[dict]) -> set:
    """WUBRG color identity of a resolved card ({} when unresolvable)."""
    if not card:
        return set()
    return {
        c.upper() for c in (card.get("color_identity") or [])
        if isinstance(c, str) and c.upper() in "WUBRG"
    }


def _make_lookup_cache(
    lookup: Optional[Callable[[str], Optional[dict]]],
) -> Callable[[str], Optional[dict]]:
    """Memoize ``lookup`` per unique name and swallow per-card blips.

    A 99-card deck with 36 Mountains must not make 36 round-trips, and
    one card's exception must not poison the whole computation (same
    reasoning as ``deck_health._lookup_card_safe``). ``None`` results
    are cached too -- a miss is a miss, retrying it 36 times is 36
    timeouts during an actual outage.
    """
    fn = lookup or _default_lookup
    cache: dict[str, Optional[dict]] = {}

    def _cached(name: str) -> Optional[dict]:
        key = name.strip().lower()
        if key in cache:
            return cache[key]
        try:
            card = fn(name)
        except Exception:  # noqa: BLE001 -- caller survives nulls
            card = None
        cache[key] = card
        return card

    return _cached


def _build_deck(
    deck_text: str,
    identity: set,
    lookup: Callable[[str], Optional[dict]],
) -> Optional[dict]:
    """Classify [Main] into the simulated library.

    Returns ``{"cards": [_Card, ...], "lines": int, "failed_lines": int,
    "lands": int, "mdfc_lands": int}`` -- or ``None`` on the outage
    contract (empty deck, or MORE than half the card lines unresolved).

    An unresolved card is NOT dropped from the library: the deck keeps
    its true printed size and the card simulates as a non-land,
    uncastable unknown. See the module docstring -- the bias is
    deliberately pessimistic.
    """
    cards: list[_Card] = []
    lines = 0
    failed_lines = 0
    lands = 0
    mdfc_lands = 0
    from .deck_health import _MDFC_LANDS  # curated spell-front MDFC list

    for qty, name in dck_utils.iter_main_cards(deck_text):
        lines += 1
        if qty <= 0:
            continue
        basic = is_basic_land(name)          # fast path, no round-trip
        card = None if basic else lookup(name)
        if not basic and card is None:
            failed_lines += 1
            cards.extend([_Card(name, False, False, frozenset(), (), 0.0,
                                False)] * qty)
            continue
        front = "land" if basic else _front_type_line(card or {})
        is_true_land = "land" in front
        is_mdfc_land = (not is_true_land) and name.lower() in _MDFC_LANDS
        if is_true_land or is_mdfc_land:
            sources = frozenset(land_color_sources(name, identity, lookup))
            entry = _Card(name, True, is_true_land, sources, (), 0.0, True)
            if is_true_land:
                lands += qty
            else:
                mdfc_lands += qty
        else:
            pips, mv = _parse_cost(_front_mana_cost(card or {}))
            entry = _Card(
                name, False, False, frozenset(),
                tuple(sorted(pips.items())), mv, True,
            )
        cards.extend([entry] * qty)

    if not lines:
        return None  # empty deck -> nothing to measure
    if failed_lines * 2 > lines:
        return None  # outage contract: majority of lookups failed
    return {
        "cards": cards,
        "lines": lines,
        "failed_lines": failed_lines,
        "lands": lands,
        "mdfc_lands": mdfc_lands,
    }


# ---------------------------------------------------------------------------
# Layer 2 -- mana feasibility
# ---------------------------------------------------------------------------

def _pips_payable(pips: Iterable, land_sources: list) -> bool:
    """Can these lands in play pay these COLORED pips simultaneously?

    Not a per-color count check: a dual land pays one pip, not one pip
    of each of its colors, so ``{W}{U}`` off a single Hallowed Fountain
    is NOT castable. That makes this a bipartite matching -- pips on
    one side, untapped lands on the other, an edge where the land taps
    for that color -- and the question is whether a PERFECT matching of
    the pips exists. Kuhn's augmenting-path algorithm, pure stdlib;
    the graph is tiny (<=8 pips, <=10 lands) so the simple form is
    faster than anything cleverer.

    Generic pips are NOT modelled here -- the caller pairs this with a
    ``lands_in_play >= mana_value`` check, which is exactly what
    generic costs require.
    """
    demands: list[str] = []
    for color, count in pips:
        demands.extend([color] * count)
    if not demands:
        return True
    if len(demands) > len(land_sources):
        return False
    assigned = [-1] * len(land_sources)  # land index -> demand index

    def _augment(demand_idx: int, seen: set) -> bool:
        for li, sources in enumerate(land_sources):
            if li in seen or demands[demand_idx] not in sources:
                continue
            seen.add(li)
            if assigned[li] < 0 or _augment(assigned[li], seen):
                assigned[li] = demand_idx
                return True
        return False

    for d in range(len(demands)):
        if not _augment(d, set()):
            return False
    return True


# ---------------------------------------------------------------------------
# Layer 2 -- mulligan policy
# ---------------------------------------------------------------------------

def _keep_land_range(lands: int, depth: int, hand_size: int) -> tuple:
    """Land counts reachable in the KEPT hand at London depth ``depth``.

    You draw 7 and bottom ``depth`` cards of your choosing, so the kept
    hand can hold anywhere from ``lands - depth`` to ``lands`` lands
    (floored/capped by the hand size). Returns ``(lo, hi)``.
    """
    lo = max(0, lands - depth)
    hi = min(lands, hand_size)
    return lo, hi


def _keepable(lands: int, depth: int, hand_size: int) -> bool:
    """Is a 7-card draw a keep at London depth ``depth``?

    THE RULE: the hand is a keep iff, after bottoming ``depth`` cards
    optimally, the LANDS IN THE KEPT HAND can land inside
    ``[KEEPABLE_LAND_MIN, min(KEEPABLE_LAND_MAX, hand_size)]``. At
    depth 0 that reduces to the headline definition -- **a 7-card
    opener is keepable iff it holds 2 to 5 lands** -- and at deeper
    mulligans the upper bound tightens with the shrinking hand while
    bottoming lets you shed flood (a 6-land 7 IS a keep at depth 1,
    because you bottom one of them).
    """
    if hand_size <= 0:
        return False
    lo, hi = _keep_land_range(lands, depth, hand_size)
    band_lo = KEEPABLE_LAND_MIN
    band_hi = min(KEEPABLE_LAND_MAX, hand_size)
    return max(lo, band_lo) <= min(hi, band_hi)


def _bottom_hand(opener: list, depth: int) -> list:
    """Apply the London bottoming to a drawn 7 -> the kept hand.

    Steers the kept land count to ``PREFERRED_KEEP_LANDS`` inside
    whatever is reachable (falling back to the closest reachable count
    when the keepable band is out of reach -- a forced keep at
    ``MAX_MULLIGANS`` still has to bottom SOMETHING). Non-land
    bottoming sheds the most expensive spells first, keeping the cheap
    early plays, which is what the turn-3 checks are about.

    Deterministic: ties break on the card's index in the drawn hand, so
    the same shuffle always yields the same kept hand.
    """
    hand_size = len(opener) - depth
    if hand_size <= 0:
        return []
    if depth <= 0:
        return list(opener)
    land_idx = [i for i, c in enumerate(opener) if c.is_land]
    spell_idx = [i for i, c in enumerate(opener) if not c.is_land]
    lands = len(land_idx)
    lo, hi = _keep_land_range(lands, depth, hand_size)
    band_lo, band_hi = KEEPABLE_LAND_MIN, min(KEEPABLE_LAND_MAX, hand_size)
    in_band_lo, in_band_hi = max(lo, band_lo), min(hi, band_hi)
    if in_band_lo <= in_band_hi:
        target = min(max(PREFERRED_KEEP_LANDS, in_band_lo), in_band_hi)
    else:
        target = min(max(PREFERRED_KEEP_LANDS, lo), hi)
    # Cheapest spells first; index breaks ties for determinism.
    spell_idx.sort(key=lambda i: (opener[i].mv, i))
    keep = set(land_idx[:target]) | set(spell_idx[:hand_size - target])
    return [c for i, c in enumerate(opener) if i in keep]


# ---------------------------------------------------------------------------
# Layer 2 -- turn-by-turn play-out
# ---------------------------------------------------------------------------

def _choose_land(hand: list, produced: set) -> Optional[int]:
    """Index of the land to play this turn, or ``None`` if landless.

    Greedy color-first sequencing: play whichever land adds the most
    NEW colors to what is already in play (a dual/any-color land beats
    a duplicate basic), ties breaking on hand position. This is a
    proxy for how a human sequences lands, and being greedy makes the
    color metrics an upper-ish bound on colored availability while the
    no-ramp assumption makes the turn metrics a lower bound -- the two
    are documented separately rather than netted out.
    """
    best_idx = None
    best_gain = -1
    for i, card in enumerate(hand):
        if not card.is_land:
            continue
        gain = len(card.sources - produced)
        if gain > best_gain:
            best_gain, best_idx = gain, i
    return best_idx


def _play_out(
    hand: list, library: list, *, on_play: bool, max_turn: int,
    commander_turn: Optional[int], commander_pips: tuple,
) -> dict:
    """Play one kept hand out to ``max_turn``, one land drop per turn.

    ``on_play`` skips the turn-1 draw (the actual rules difference
    between the two conventions -- everything else is identical, which
    is why both are evaluated off the same shuffle).

    Returns the per-trial booleans/counters the aggregator averages.
    """
    hand = list(hand)
    lands_in_play: list = []          # source sets of lands on the battlefield
    produced: set = set()
    drawn = 0
    result = {
        "lands_by_turn": {},
        "commander_on_curve": None,
        "color_screw": False,
    }
    for turn in range(1, max_turn + 1):
        if (turn > 1 or not on_play) and drawn < len(library):
            hand.append(library[drawn])
            drawn += 1
        idx = _choose_land(hand, produced)
        if idx is not None:
            card = hand.pop(idx)
            lands_in_play.append(card.sources)
            produced |= card.sources
        result["lands_by_turn"][turn] = len(lands_in_play)
        if turn == COLOR_SCREW_TURN:
            result["color_screw"] = _is_color_screwed(hand, lands_in_play)
        if commander_turn is not None and turn == commander_turn:
            # The commander is ALWAYS available from the command zone --
            # this is a mana question, never a draw question. That
            # distinction is the whole point of the metric.
            result["commander_on_curve"] = (
                len(lands_in_play) >= commander_turn
                and _pips_payable(commander_pips, lands_in_play)
            )
    return result


def _is_color_screwed(hand: list, lands_in_play: list) -> bool:
    """Colored-mana screw at ``COLOR_SCREW_TURN``: enough lands, wrong
    colors.

    Requires ALL of:
      1. every land drop made (``len(lands) >= COLOR_SCREW_TURN``) --
         otherwise you are mana SCREWED, a different failure the land
         checkpoints already report, and double-counting it here would
         make the two metrics correlate for the wrong reason;
      2. at least one colored spell in hand cheap enough to cast on
         mana count alone;
      3. NONE of those spells castable given the lands' colors.

    Reported as an unconditional probability over all trials (not
    conditioned on 1+2), so it reads as "how often does this deck have
    mana and still do nothing".
    """
    if len(lands_in_play) < COLOR_SCREW_TURN:
        return False
    affordable = [
        c for c in hand
        if not c.is_land and c.pips and c.mv <= len(lands_in_play)
    ]
    if not affordable:
        return False
    return not any(_pips_payable(c.pips, lands_in_play) for c in affordable)


def _draw_prefix(deck: list, rng: random.Random, count: int) -> list:
    """Deal the top ``count`` cards of a freshly shuffled ``deck``.

    Partial Fisher-Yates: only the prefix a trial can actually see gets
    randomized, which is uniform over that prefix and skips ~90% of the
    swaps a full 99-card shuffle would do. Uses the injected
    ``rng`` -- ``random.Random(seed)``, never the global ``random`` --
    so the whole run is reproducible.
    """
    cards = list(deck)
    n = len(cards)
    count = min(count, n)
    for i in range(count):
        j = rng.randrange(i, n)
        cards[i], cards[j] = cards[j], cards[i]
    return cards[:count]


# ---------------------------------------------------------------------------
# Layer 2 -- public entry
# ---------------------------------------------------------------------------

def _resolve_commander(
    deck_text: str, commander: Optional[str],
    lookup: Callable[[str], Optional[dict]],
) -> tuple:
    """``(name, card, identity, mana_value, pips)`` for the commander.

    Falls back to the deck's ``[Commander]`` section when no name is
    passed. Any part that can't be resolved comes back ``None`` rather
    than guessed -- an unknown commander must not silently become a
    generic 3-drop.
    """
    name = commander
    if not name:
        names = dck_utils.section_card_names(deck_text, "Commander")
        name = names[0] if names else None
    if not name:
        return None, None, set(), None, ()
    card = lookup(name)
    if not card:
        return name, None, set(), None, ()
    pips, mv = _parse_cost(_front_mana_cost(card))
    identity = _card_identity(card)
    return name, card, identity, mv, tuple(sorted(pips.items()))


def opening_hand_stats(
    deck_text: str,
    *,
    commander: Optional[str] = None,
    trials: int = DEFAULT_TRIALS,
    seed: int = 0,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> Optional[dict]:
    """Seeded Monte-Carlo consistency profile of ``deck_text``.

    The pre-execute check ``docs/architecture.md`` asked for: how often
    this deck keeps its opener, hits its land drops, casts its
    commander on curve, and stalls on colors -- in milliseconds,
    offline, before anyone commits a machine to a Forge sim.

    ``commander`` defaults to the deck's ``[Commander]`` section.
    ``lookup`` defaults to the disk-cached Scryfall client; tests pass
    a stub. ``seed`` fixes everything: same seed + same deck ⇒
    identical dict, which is what makes this safe to store in the
    FP-002 regression dataset.

    Returns ``None`` when the deck is empty or a MAJORITY of card lines
    fail to resolve (the ``deck_health`` outage contract -- an
    unavailable metric is ``None``, never a fabricated 0.0), else::

        {
          "trials": int, "seed": int, "commander": str | None,
          "commander_mana_value": float | None,
          "deck_size": int,            # cards simulated (true printed size)
          "land_count": int,           # strict front-face lands
          "mdfc_land_count": int,      # spell-front MDFCs (played as lands)
          "effective_land_count": float,   # deck_health's 0.5 weighting
          "lookup_failures": int,
          "p_keepable_7": float,       # 2-5 lands in the opening 7
          "mulligan_rate": float,      # P(at least one London mulligan)
          "avg_lands_in_7": float,
          "avg_opening_hand_size": float,
          "convention": "on_play",
          "on_play": {...}, "on_draw": {...},   # both, same shuffles
          # aliases of the CONVENTION sub-dict, for callers that want
          # one number rather than a pair:
          "p_3_lands_by_t3": float, "p_5_lands_by_t5": float,
          "p_commander_on_curve": float | None, "p_color_screw": float,
        }

    Each ``on_play`` / ``on_draw`` sub-dict holds
    ``p_3_lands_by_t3``, ``p_5_lands_by_t5``, ``avg_lands_by_t3``,
    ``avg_lands_by_t5``, ``p_commander_on_curve`` and
    ``p_color_screw``.

    Metric definitions, in one place:
      * **keepable 7** -- 2 to 5 lands among the opening 7
        (``KEEPABLE_LAND_MIN`` / ``KEEPABLE_LAND_MAX``). Land-count
        only; see ``_keepable``.
      * **mulligan_rate** -- P(the first 7 is not a keep), i.e. exactly
        ``1 - p_keepable_7`` under this policy. Reported separately
        because it is the number a player recognizes;
        ``avg_opening_hand_size`` is the non-redundant companion (how
        many cards you actually start with after the London process).
      * **p_N_lands_by_tT** -- N lands ON THE BATTLEFIELD by turn T,
        one drop per turn, from the KEPT hand plus natural draws. Not
        "N lands seen": drawing your third land on turn 5 does not
        retroactively make the turn-3 drop.
      * **p_commander_on_curve** -- P(enough lands AND the right colors
        on turn == the commander's mana value). The commander is always
        in the command zone, so this never asks whether you drew it.
        ``None`` when the commander can't be resolved.
      * **p_color_screw** -- see ``_is_color_screwed``.
    """
    lookup_fn = _make_lookup_cache(lookup)
    cmd_name, _cmd_card, identity, cmd_mv, cmd_pips = _resolve_commander(
        deck_text, commander, lookup_fn,
    )
    built = _build_deck(deck_text, identity, lookup_fn)
    if built is None:
        return None  # empty deck or majority-lookup-failure outage
    deck = built["cards"]
    deck_size = len(deck)
    if deck_size <= 0 or trials <= 0:
        return None

    commander_turn = None
    if cmd_mv is not None:
        # Mana value 0-1 commanders are "on curve" on turn 1.
        commander_turn = max(1, int(math.ceil(cmd_mv)))
    max_turn = max(
        [t for _n, t in LAND_CHECKPOINTS] + [COLOR_SCREW_TURN]
        + ([commander_turn] if commander_turn else [])
    )
    needed = OPENING_HAND_SIZE + max_turn + 1

    rng = random.Random(seed)
    keepable_7 = 0
    lands_in_7_total = 0
    hand_size_total = 0
    tallies = {
        conv: {
            "lands_by_turn": {t: 0 for _n, t in LAND_CHECKPOINTS},
            "hits": {n: 0 for n, _t in LAND_CHECKPOINTS},
            "commander": 0,
            "commander_trials": 0,
            "screw": 0,
        }
        for conv in ("on_play", "on_draw")
    }

    for _ in range(trials):
        kept_hand: list = []
        library: list = []
        for depth in range(MAX_MULLIGANS + 1):
            prefix = _draw_prefix(deck, rng, needed)
            opener = prefix[:OPENING_HAND_SIZE]
            lands = sum(1 for c in opener if c.is_land)
            if depth == 0:
                lands_in_7_total += lands
                if _keepable(lands, 0, len(opener)):
                    keepable_7 += 1
            hand_size = max(0, len(opener) - depth)
            if _keepable(lands, depth, hand_size) or depth == MAX_MULLIGANS:
                kept_hand = _bottom_hand(opener, depth)
                library = prefix[OPENING_HAND_SIZE:]
                break
        hand_size_total += len(kept_hand)
        for conv in ("on_play", "on_draw"):
            out = _play_out(
                kept_hand, library,
                on_play=(conv == "on_play"), max_turn=max_turn,
                commander_turn=commander_turn, commander_pips=cmd_pips,
            )
            tally = tallies[conv]
            for n, t in LAND_CHECKPOINTS:
                got = out["lands_by_turn"].get(t, 0)
                tally["lands_by_turn"][t] += got
                if got >= n:
                    tally["hits"][n] += 1
            if out["commander_on_curve"] is not None:
                tally["commander_trials"] += 1
                if out["commander_on_curve"]:
                    tally["commander"] += 1
            if out["color_screw"]:
                tally["screw"] += 1

    def _conv_dict(conv: str) -> dict:
        tally = tallies[conv]
        out = {}
        for n, t in LAND_CHECKPOINTS:
            out[f"p_{n}_lands_by_t{t}"] = tally["hits"][n] / trials
            out[f"avg_lands_by_t{t}"] = tally["lands_by_turn"][t] / trials
        out["p_commander_on_curve"] = (
            tally["commander"] / tally["commander_trials"]
            if tally["commander_trials"] else None  # unresolved -> unavailable
        )
        out["p_color_screw"] = tally["screw"] / trials
        return out

    on_play = _conv_dict("on_play")
    on_draw = _conv_dict("on_draw")
    p_keepable = keepable_7 / trials
    stats = {
        "trials": trials,
        "seed": seed,
        "commander": cmd_name,
        "commander_mana_value": cmd_mv,
        "commander_turn": commander_turn,
        "deck_size": deck_size,
        "land_count": built["lands"],
        "mdfc_land_count": built["mdfc_lands"],
        "effective_land_count": (
            built["lands"] + MDFC_LAND_WEIGHT * built["mdfc_lands"]
        ),
        "lookup_failures": built["failed_lines"],
        "p_keepable_7": p_keepable,
        "mulligan_rate": 1.0 - p_keepable,
        "avg_lands_in_7": lands_in_7_total / trials,
        "avg_opening_hand_size": hand_size_total / trials,
        "convention": CONVENTION,
        "on_play": on_play,
        "on_draw": on_draw,
    }
    # Top-level aliases at the documented default convention.
    stats.update(
        {k: v for k, v in stats[CONVENTION].items() if k.startswith("p_")}
    )
    return stats


# ---------------------------------------------------------------------------
# Formatter -- CLI / report rendering
# ---------------------------------------------------------------------------

def _pct(value: Optional[float]) -> str:
    """Percent, or the explicit unavailable marker. The whole point of
    the None contract is that the UI can say "unavailable" instead of
    printing a confident 0%."""
    return "unavailable" if value is None else f"{value:.1%}"


def format_consistency_report(stats: Optional[dict]) -> str:
    """Render ``opening_hand_stats`` output as a text block.

    Mirrors ``doctor.format_text`` / ``meta_test.format_report_text``:
    a banner, aligned rows, and a plain-language verdict. ``None``
    (the outage contract) renders as one honest unavailable line rather
    than an empty table of zeroes.
    """
    if not stats:
        return (
            "Consistency: unavailable — the deck is empty or card data "
            "could not be resolved for a majority of its lines."
        )
    lines = []
    lines.append("=" * 60)
    lines.append(" Consistency check (closed-form + seeded simulation)")
    lines.append("=" * 60)
    cmd = stats.get("commander") or "unknown"
    mv = stats.get("commander_mana_value")
    mv_note = f"  (mana value {mv:g})" if mv is not None else ""
    lines.append(f"Commander: {cmd}{mv_note}")
    lines.append(
        f"Deck: {stats['deck_size']} cards, {stats['land_count']} lands"
        + (
            f" + {stats['mdfc_land_count']} MDFC "
            f"(= {stats['effective_land_count']:g} effective)"
            if stats.get("mdfc_land_count") else ""
        )
    )
    lines.append(
        f"Trials: {stats['trials']} (seed {stats['seed']}, deterministic)"
    )
    if stats.get("lookup_failures"):
        lines.append(
            f"  note: {stats['lookup_failures']} card line(s) unresolved — "
            "simulated as non-land spells, so these numbers read low."
        )
    lines.append("")
    keep_label = (
        f"Keepable 7 ({KEEPABLE_LAND_MIN}-{KEEPABLE_LAND_MAX} lands)"
    )
    lines.append(f"  {keep_label:<28s}{_pct(stats['p_keepable_7']):>12s}")
    lines.append(
        f"  {'Mulligan rate':<28s}{_pct(stats['mulligan_rate']):>12s}"
    )
    lines.append(
        f"  {'Avg lands in opening 7':<28s}{stats['avg_lands_in_7']:>12.2f}"
    )
    lines.append(
        f"  {'Avg starting hand size':<28s}"
        f"{stats['avg_opening_hand_size']:>12.2f}"
    )
    lines.append("")
    lines.append(f"  {'':28s}{'on the play':>14s}{'on the draw':>14s}")
    rows = [("p_3_lands_by_t3", "3 lands by turn 3"),
            ("p_5_lands_by_t5", "5 lands by turn 5"),
            ("p_commander_on_curve", "Commander on curve"),
            ("p_color_screw", "Color screw by turn %d" % COLOR_SCREW_TURN)]
    for key, label in rows:
        lines.append(
            f"  {label:<28s}{_pct(stats['on_play'].get(key)):>14s}"
            f"{_pct(stats['on_draw'].get(key)):>14s}"
        )
    lines.append("")
    quoted = "on the play" if CONVENTION == "on_play" else "on the draw"
    lines.append(
        f"Headline figures are quoted {quoted} (the harsher read); ramp is "
        "not modelled, so the commander and land figures are floors."
    )
    return "\n".join(lines)
