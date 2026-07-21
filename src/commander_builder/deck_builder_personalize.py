"""FP-014.3 — personalization stages for the from-scratch assembler.

FP-014.1 built a legal 99 (``deck_builder``); FP-014.2 gave it a real
manabase (``deck_builder_manabase``). Both produce a deck whose card CHOICES
are borrowed wholesale from EDHREC's community aggregate — the same shell
every builder of this commander would get. This module makes the shell
*yours*: three independent, individually-toggleable passes applied AFTER the
base assembly, each preserving the deck's three hard invariants
(exactly-99 mainboard, color-identity legality, singleton).

WHY A SEPARATE MODULE (same layering call as deck_builder_manabase).
====================================================================
``deck_builder`` is the orchestrator: it owns sourcing, the exactly-99
budget, and rendering. Each personalization stage is a self-contained
transform over the nonland spell list with its own load-bearing knowledge
(lift math, the bracket rules, ownership). Keeping them here lets
``deck_builder`` stay thin and lets each stage be unit-tested with injected
fakes, no network.

THE ONE INVARIANT TRICK THAT KEEPS THIS SAFE.
=============================================
Every stage only ever performs LIKE-FOR-LIKE SWAPS on the *nonland spell*
list: remove exactly one card, add exactly one card, net zero. It never
touches the manabase (lands + basics are already tuned and their count is
what pins the deck at 99). So:

  * exactly-99 is preserved by construction — a net-zero edit to the
    nonland list cannot change ``len(nonlands) + land_slots``;
  * singleton is preserved because every incoming card is checked against
    the full set of committed keys (``reserved_keys`` + the current
    nonlands) before it goes in;
  * color-identity is preserved because every incoming card must pass the
    injected ``ci_ok`` gate (the deck's ``enforce_color_identity``).

``deck_builder`` still re-validates the rendered deck after the stages run
(``count_main_cards`` + a singleton/color assertion) — belt and suspenders,
because an invariant a stage *claims* to keep is not one the emitted file is
allowed to break.

THE THREE STAGES, IN THE ORDER deck_builder RUNS THEM.
======================================================
1. LIFT SWAPS   — trade marginal seed cards for higher-synergy in-corpus
                  cards that pair with the commander + shell (lift_analysis).
2. BRACKET STEER— nudge the deck's estimated power toward the requested
                  bracket, WITHIN that bracket's Game-Changer cap
                  (bracket_estimator).
3. COLLECTION   — among near-equivalent slots, prefer cards the user owns
   BIAS           (collection), and report a buy-list of what's still
                  unowned.

Why THAT order (deck_builder documents it too, this is the rationale):

  * Lift first — it reshapes WHICH cards are in the deck for synergy; doing
    it first means the later stages estimate/bias over the settled list.
  * Steer second — power level is read off the whole (post-lift) list, so
    estimate once the synergy shell has stopped moving.
  * Collection last — it is the least disruptive (it only trades
    near-equivalents) and it must run AFTER steering so it can be told to
    NEVER trade away a power card the steer stage deliberately placed
    (``protect``); doing it last also means the buy-list reflects the final
    99.
"""

from __future__ import annotations

from typing import Callable, Optional

from . import lift_analysis
from .collection import name_key, owns

# GC CAPS PER BRACKET — verified against bracket_estimator's HARD FLOORS,
# not invented here:
#   * ``n_gc >= 1`` floors the deck at B3  → B1/B2 may hold ZERO Game
#     Changers (cap 0);
#   * ``n_gc >= 4`` floors the deck at B4  → B3's max is 3 (cap 3);
#   * B4/B5 are unbounded.
# The steer stage NEVER adds a Game Changer that would push the count past
# the cap for the *target* bracket — that would move the estimate the wrong
# way (a fourth GC floors a would-be B3 up to B4). See ``steer_bracket``.
GC_CAP_BY_BRACKET: dict[int, int] = {1: 0, 2: 0, 3: 3, 4: 10 ** 9, 5: 10 ** 9}

DEFAULT_MAX_LIFT_SWAPS = 6      # bounded so a build stays close to its seed.
DEFAULT_MAX_STEER_ITERS = 4     # bounded convergence; re-estimate each loop.


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def synergy_scorer(
    matrix: Optional[dict], bracket: Optional[int], deck_keys: set[str],
) -> Callable[[str], float]:
    """Build a "how well does this card connect to the deck" scorer.

    Score = sum over the deck's cards of ``max(0, lift - 1)`` for every pair
    that clears the support floor and beats chance (lift > 1). It rewards a
    card that pairs above-chance with MANY of the deck's cards — exactly the
    "fits the fabric" signal ``lift_candidates`` ranks on, reused here to
    rank the deck's OWN cards so we can find the marginal (low-synergy) ones
    to trade away. A card absent from the corpus scores 0 (no signal), which
    is the correct read: we have no evidence it belongs.

    Returns a callable taking a ``name_key`` (or raw name — folded here).
    Degrades to an all-zero scorer when there's no usable matrix.
    """
    if not matrix or matrix.get("too_small"):
        return lambda _name: 0.0
    # ``_section_for`` picks the bracket band sub-matrix when it exists, else
    # the overall matrix — the same population lift_candidates scores on.
    section, _band = lift_analysis._section_for(matrix, bracket)

    def score(name: str) -> float:
        key = name_key(name)
        total = 0.0
        for dk in deck_keys:
            if dk == key:
                continue
            lv = lift_analysis.lift_value(section, key, dk)
            if lv and lv > 1.0:
                total += lv - 1.0
        return total

    return score


# ---------------------------------------------------------------------------
# STAGE 1 — lift-driven swaps
# ---------------------------------------------------------------------------


def lift_swaps(
    nonlands: list[str],
    *,
    commander: str,
    bracket: Optional[int],
    matrix: Optional[dict],
    reserved_keys: set[str],
    role_of: Callable[[str], str],
    ci_ok: Callable[[str], bool],
    max_swaps: int = DEFAULT_MAX_LIFT_SWAPS,
) -> tuple[list[str], list[str], Optional[str]]:
    """Trade marginal seed cards for higher-synergy in-corpus candidates.

    Returns ``(new_nonlands, swap_notes, skipped_reason)``. When the corpus
    is unavailable or below ``lift_analysis``'s floor we return the input
    unchanged and a human ``skipped_reason`` (never an exception) — a
    from-scratch build with a thin corpus must still succeed.

    THE LIKE-FOR-LIKE ROLE CONSTRAINT (read this — it is why role targets
    don't degrade). ``deck_builder`` sizes the shell toward ``ROLE_TARGETS``
    (ramp/draw/removal/wipe/protection counts). A naive "drop the worst card,
    add the best lift pick" would happily trade a removal spell for a draw
    spell and quietly break those counts. So a lift swap is only made when
    the incoming candidate shares a ROLE with some card already in the deck:
    we remove a same-role card and add the candidate, leaving every role's
    count exactly where the assembler put it. A candidate whose role has no
    counterpart in the deck is simply skipped (we would rather not run it
    than distort the role balance).

    Selection, per candidate (candidates come pre-ranked by aggregate lift):
      * must pass color identity (``ci_ok``) and be a genuine singleton
        (its key is not already committed);
      * we pick, among same-role nonlands not yet swapped, the one with the
        LOWEST deck-synergy (the marginal card that pairs with nothing);
      * we only swap when the candidate connects to the deck BETTER than that
        marginal card (``synergy(cand) > synergy(out)``) — never a downgrade.
    Bounded by ``max_swaps`` so the deck stays recognizably its seed.
    """
    if matrix is None:
        return nonlands, [], "no corpus available for lift analysis"
    if matrix.get("too_small"):
        n = matrix.get("n_decks", 0)
        return (
            nonlands, [],
            f"corpus too small ({n} harvested decks; "
            f"need {lift_analysis.MIN_CORPUS_DECKS})",
        )

    deck_keys = {name_key(commander)} | {name_key(n) for n in nonlands}
    # Over-fetch: color/role/singleton filtering below drops some, and we
    # want enough survivors to reach ``max_swaps``.
    candidates = lift_analysis.lift_candidates(
        matrix, deck_keys, bracket=bracket, limit=max(max_swaps * 4, 20),
    )
    if not candidates:
        return nonlands, [], "no candidate cleared the lift bar"

    score = synergy_scorer(matrix, bracket, deck_keys)
    working = list(nonlands)
    swapped_out: set[str] = set()   # keys removed — never trade one twice.
    notes: list[str] = []

    for cand in candidates:
        if len(notes) >= max_swaps:
            break
        cand_name = cand["card"]
        cand_key = cand["key"]
        # Singleton: not the commander, not a land/basic, not already run,
        # and not the target of an earlier swap this pass.
        live_keys = {name_key(n) for n in working}
        if cand_key in reserved_keys or cand_key in live_keys:
            continue
        if not ci_ok(cand_name):
            continue
        cand_role = role_of(cand_name)
        # Same-role, still-present, not-yet-swapped trade targets.
        same_role = [
            n for n in working
            if name_key(n) not in swapped_out and role_of(n) == cand_role
        ]
        if not same_role:
            # No like-for-like slot → skip rather than distort role counts.
            continue
        out_name = min(same_role, key=lambda n: score(n))
        if score(cand_name) <= score(out_name):
            continue  # candidate connects no better than the card it'd oust.
        idx = working.index(out_name)
        working[idx] = cand_name
        swapped_out.add(name_key(out_name))
        notes.append(
            f"swapped {out_name} for {cand_name} — {cand['rationale']}"
        )

    return working, notes, None


# ---------------------------------------------------------------------------
# STAGE 2 — bracket steering
# ---------------------------------------------------------------------------


def _pick_marginal_nonpower(
    working: list[str],
    is_power: Callable[[str], bool],
) -> Optional[str]:
    """The lowest-priority NON-power card to sacrifice for a power add.

    Nonlands are stored in EDHREC/lift priority order, so the tail is the
    least important. We skip power cards (a GC/fast-mana card we may have
    just added, or one from the seed) so raising the bracket never
    cannibalizes the very power we're trying to accumulate.
    """
    for nm in reversed(working):
        if not is_power(nm):
            return nm
    return None


def steer_bracket(
    nonlands: list[str],
    *,
    target: int,
    render_fn: Callable[[list[str]], str],
    estimate_fn: Callable[[str], dict],
    is_game_changer: Callable[[str], bool],
    is_fast_mana: Callable[[str], bool],
    candidate_pool: list[str],
    reserved_keys: set[str],
    ci_ok: Callable[[str], bool],
    gc_cap: int,
    max_iters: int = DEFAULT_MAX_STEER_ITERS,
) -> tuple[list[str], list[str], Optional[int]]:
    """Nudge the deck's estimated bracket toward ``target``, in-cap.

    Returns ``(new_nonlands, notes, final_estimate)``. Loops at most
    ``max_iters`` times, re-running ``estimate_fn`` after every swap so the
    decision is always made against the CURRENT list (never a stale one).

    UNDER target → ADD POWER. Walk ``candidate_pool`` (corpus/commander-page
    sourced, so the additions actually belong to this commander) for an
    in-color power card (Game Changer or fast mana). THE CAP BOUND: if the
    candidate is a Game Changer we only add it while the running GC count is
    below ``gc_cap`` — the bracket_estimator-derived limit (0 for B1/B2, 3
    for B3, unbounded for B4/B5). A fourth GC in a B3 deck would floor it to
    B4, the exact over-shoot we're steering away from. The added card
    displaces the lowest-priority non-power card, keeping the 99 intact.

    OVER target → SOFTEN. Remove the highest-power card in the deck (Game
    Changers first — they carry the hard floor) and replace it with a benign
    (non-power) in-color card from the pool, lowering the GC count / weighted
    score. Each removal re-runs the estimate.

    Either direction stops early when nothing actionable remains (no in-cap
    power to add / no power to trim / no benign filler) — the honest "budget
    exhausted" outcome, reported in the notes.
    """
    is_power = lambda nm: is_game_changer(nm) or is_fast_mana(nm)  # noqa: E731
    working = list(nonlands)
    notes: list[str] = []
    final_est: Optional[int] = None

    def live_keys() -> set[str]:
        return {name_key(n) for n in working}

    for _ in range(max_iters):
        est = estimate_fn(render_fn(working)).get("estimate")
        final_est = est
        if est is None or est == target:
            break

        if est < target:
            n_gc = sum(1 for n in working if is_game_changer(n))
            added = False
            keys = live_keys()
            for cand in candidate_pool:
                ck = name_key(cand)
                if ck in reserved_keys or ck in keys:
                    continue
                if not ci_ok(cand):
                    continue
                cand_gc = is_game_changer(cand)
                if not (cand_gc or is_fast_mana(cand)):
                    continue  # only power raises the bracket.
                if cand_gc and n_gc >= gc_cap:
                    continue  # CAP BOUND — see docstring.
                out = _pick_marginal_nonpower(working, is_power)
                if out is None:
                    break
                working[working.index(out)] = cand
                notes.append(
                    f"added {cand} (power) over {out} to raise estimate "
                    f"toward B{target}"
                )
                added = True
                break
            if not added:
                notes.append(
                    f"estimate B{est} below target B{target}; no in-cap "
                    f"power card available to add"
                )
                break
        else:  # est > target — soften.
            power_in = [n for n in working if is_game_changer(n)]
            power_in += [n for n in working if is_fast_mana(n)
                         and not is_game_changer(n)]
            if not power_in:
                notes.append(
                    f"estimate B{est} above target B{target}; no power "
                    f"cards to trim"
                )
                break
            keys = live_keys()
            filler = next(
                (c for c in candidate_pool
                 if name_key(c) not in reserved_keys
                 and name_key(c) not in keys
                 and not is_power(c) and ci_ok(c)),
                None,
            )
            if filler is None:
                notes.append(
                    f"estimate B{est} above target B{target}; no benign "
                    f"filler available to swap in"
                )
                break
            out = power_in[0]
            working[working.index(out)] = filler
            notes.append(
                f"removed {out} (power) for {filler} to lower estimate "
                f"toward B{target}"
            )

    # Final estimate reflects the LAST edit (the loop may have broken before
    # re-estimating after its final swap).
    final_est = estimate_fn(render_fn(working)).get("estimate")
    notes.append(f"final bracket estimate B{final_est} vs target B{target}")
    return working, notes, final_est


# ---------------------------------------------------------------------------
# STAGE 3 — collection bias
# ---------------------------------------------------------------------------


def apply_collection_bias(
    nonlands: list[str],
    *,
    collection: Optional[frozenset[str]],
    owned_pool: list[str],
    ci_ok: Callable[[str], bool],
    role_of: Callable[[str], str],
    mv_of: Callable[[str], Optional[float]],
    quality_of: Callable[[str], float],
    reserved_keys: set[str],
    protect: Optional[Callable[[str], bool]] = None,
    tolerance: float = 0.0,
    max_cost_gap: float = 1.0,
) -> tuple[list[str], list[str]]:
    """Prefer owned cards for INTERCHANGEABLE slots — near-equivalents only.

    Returns ``(new_nonlands, swap_notes)``. For every UNOWNED nonland we look
    for an owned card that is a genuine near-equivalent and swap it in.

    "Near-equivalent" is deliberately strict so we never trade quality for
    ownership (the explicit non-goal — "do NOT swap in strictly worse cards
    just because they're owned"):
      * SAME ROLE (a removal spell only substitutes for a removal spell);
      * SIMILAR COST (mana value within ``max_cost_gap`` — a 2-mana rock is
        not an "equivalent" for a 6-mana bomb even if both read as ramp);
      * NOT A DOWNGRADE (``quality_of(owned) >= quality_of(current) -
        tolerance`` — with the default lift-synergy quality, an owned card
        the corpus has never seen (score 0) will not displace a card that
        actually pairs with the deck).
    Colour identity and singleton are enforced on the incoming owned card.

    ``protect`` marks cards the caller must keep (the steer stage's power
    adds): we never trade those away, or collection bias would silently
    re-open the bracket gap steering just closed.
    """
    if collection is None or not owned_pool:
        return nonlands, []

    working = list(nonlands)
    used_owned: set[str] = set()
    notes: list[str] = []
    live = {name_key(n) for n in working}

    for idx, current in enumerate(working):
        if owns(collection, current):
            continue  # already owned — nothing to bias.
        if protect and protect(current):
            continue  # a deliberately-placed power card; hands off.
        role = role_of(current)
        q_cur = quality_of(current)
        mv_cur = mv_of(current)
        for owned in owned_pool:
            ok = name_key(owned)
            if ok in reserved_keys or ok in live or ok in used_owned:
                continue
            if not owns(collection, owned):
                continue  # pool entry the user doesn't actually own.
            if not ci_ok(owned):
                continue
            if role_of(owned) != role:
                continue  # not the same slot.
            mv_owned = mv_of(owned)
            if (mv_cur is not None and mv_owned is not None
                    and abs(mv_owned - mv_cur) > max_cost_gap):
                continue  # cost gap too wide to call them interchangeable.
            if quality_of(owned) < q_cur - tolerance:
                continue  # strictly worse — refuse the downgrade.
            # Swap the owned near-equivalent in.
            working[idx] = owned
            used_owned.add(ok)
            live.discard(name_key(current))
            live.add(ok)
            notes.append(
                f"owned-bias: {owned} (owned) replaces {current} — same "
                f"role ({role}), near-equivalent cost"
            )
            break

    return working, notes
