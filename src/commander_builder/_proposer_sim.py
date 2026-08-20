"""Forge A/B simulation helpers used by ``proposer.auto_curate_main``
and the knowledge_log row writer.

These hooks close the feedback loop on auto-curate: the iteration row
initially lands with ``verdict='pending'`` and the post-apply A/B sim
fills in the empirical result (kept / reverted / neutral) plus the
detailed metrics ``update_iteration_sim`` persists.

Public symbols:

  ``_DEFAULT_SIM_MARGIN``         — legacy minimum-delta pre-filter for
                                    kept/reverted (see VERDICT_ALPHA).
  ``VERDICT_ALPHA``               — two-sided binomial significance level
                                    the kept/reverted call must clear.
  ``EXPECTED_DECISIVE_FRACTION``  — expected decisive share of TOTAL pod
                                    games (filler seats win the rest).
  ``min_sim_games_for_verdict()`` — smallest --sim-games whose expected
                                    decisive count reaches the verdict gate.
  ``_verdict_from_ab(ab_result)`` — pure ABResult → verdict mapping.
  ``_ab_to_iteration_fields(...)``— project ABResult into the
                                    update_iteration_sim kwargs shape.
  ``_pick_filler_decks(...)``     — bracket-matched opponent pool
                                    selection for the 4-player pod.
  ``_run_sim_and_record(...)``    — orchestrator that calls run_ab_
                                    simulation and persists the result.
  ``_log_auto_curate_iteration(...)`` — write the initial pending
                                    iteration row to knowledge_log.

Split out of ``proposer.py`` on 2026-05-16 (Tier-3 refactor) to bring
the orchestrator under the 800-line guideline. Re-exported from
``proposer`` for back-compat with existing imports.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Optional

# Canonical decisive-games floor, defined next to the binomial test it
# gates (analyst owns the verdict statistics; this module already imports
# ``binomial_two_sided_p`` from there — same direction, no cycle).
# Re-exported under its historical name: ``improve`` and callers import
# ``_proposer_sim.MIN_DECISIVE_GAMES_FOR_VERDICT``.
from .analyst import MIN_DECISIVE_GAMES_FOR_VERDICT  # noqa: F401


# Minimum absolute margin (|wins_b - wins_a|) for an A/B run to even be
# CONSIDERED kept/reverted. Historical knob (the CLI tunes it via
# --sim-margin) kept for backward compatibility, now a coarse pre-filter:
# since 2026-08-14 the actual kept/reverted call additionally requires
# the split to be statistically significant (exact two-sided binomial
# test vs p=0.5 at VERDICT_ALPHA — see _verdict_from_ab). At the default
# margin=1 the significance test is strictly stricter, so the knob only
# matters when a caller raises it ABOVE what significance demands.
_DEFAULT_SIM_MARGIN = 1

# Two-sided significance level for the kept/reverted call. A fixed
# absolute margin is game-count-invariant — under the null (a truly
# neutral swap) pair-decisive wins split ~Binomial(n, 0.5), so with 20
# decisive games P(|new - old| >= 4) ~= 0.50: half of all neutral swaps
# would earn a confident verdict. The exact binomial test scales the bar
# with the decisive count instead (at n=20 significance needs a 15-5
# split; at n=40, 27-13 — exact tails give p(26,40)=0.081, p(27,40)=0.039).
VERDICT_ALPHA = 0.05

# MIN_DECISIVE_GAMES_FOR_VERDICT (imported from ``analyst`` above): below
# that many DECISIVE games (wins_a + wins_b, draws excluded), an A/B
# result is too noisy to call: the win-rate standard error is ~0.5/sqrt(N)
# (N=10 -> +/-0.16, N=20 -> +/-0.11), which swamps the ~0.01-0.05 effect a
# curator swap actually has. Below the threshold the verdict is 'inconclusive'
# rather than a confident kept/reverted that a single-game flip could invert.

# Unit converter between --sim-games and the gate above. --sim-games is
# TOTAL 4-player-pod games, but the gate counts DECISIVE games -- games
# won by the head-to-head pair (old deck or new deck), because only
# those move wins_a/wins_b. The pod seats FOUR decks: old, new, and two
# bracket-matched fillers (_pick_filler_decks deliberately picks
# competitive opponents). With four evenly matched seats each wins ~1/4
# of the games, so the pair together takes ~2/4 = 0.5 of the total and
# the two filler seats absorb the other half (true turn-cap draws are a
# rounding error next to that -- filler wins, not draws, are the drain).
# No recorded soak run in this repo measures the pair share more
# precisely, so we use the seat-symmetry estimate 0.5. Comparing
# sim_games directly against MIN_DECISIVE_GAMES_FOR_VERDICT is a UNITS
# error: 25 total games looks comfortably above the 20-decisive gate
# but actually yields only ~12-13 decisive -- structurally
# 'inconclusive' in expectation.
EXPECTED_DECISIVE_FRACTION = 0.5


def min_sim_games_for_verdict() -> int:
    """Smallest --sim-games (TOTAL pod games) whose EXPECTED decisive
    count (total * EXPECTED_DECISIVE_FRACTION) reaches the
    MIN_DECISIVE_GAMES_FOR_VERDICT gate. Currently ceil(20 / 0.5) = 40.

    This is an expectation, not a guarantee -- an unlucky filler streak
    can still leave a 40-game run under 20 decisive -- so callers that
    want headroom should budget a few games above this floor (improve's
    default is 45).
    """
    return math.ceil(MIN_DECISIVE_GAMES_FOR_VERDICT / EXPECTED_DECISIVE_FRACTION)


def _verdict_from_ab(ab_result, *, margin: int = _DEFAULT_SIM_MARGIN,
                     min_decisive: int = MIN_DECISIVE_GAMES_FOR_VERDICT,
                     alpha: float = VERDICT_ALPHA) -> str:
    """Map an ``ABResult`` to a verdict label.

    Returns one of 'kept' / 'reverted' / 'neutral' / 'inconclusive' / 'pending':

      'kept'         -- new deck won more AND the split is statistically
                        significant (exact two-sided binomial test vs
                        p=0.5, p < ``alpha``) AND |delta| >= ``margin``
      'reverted'     -- same standard with old deck ahead
      'neutral'      -- difference within binomial noise at a TRUSTWORTHY
                        sample size (e.g. 21-20 over 41 decisive games,
                        or 12-8 over 20 — p ~= 0.5, a coin does that half
                        the time)
      'inconclusive' -- fewer than ``min_decisive`` decisive games, so the
                        result is below the noise floor regardless of margin
                        (a 3-2 at 5 games is a coin flip, not a tie)
      'pending'      -- sim didn't complete (status='skipped' or 'failed')

    The split between 'neutral' and 'inconclusive' matters: 'neutral' is a
    real near-tie we can trust; 'inconclusive' is "not enough games to say."
    Gating low-N runs to 'inconclusive' stops a noise verdict from being
    recorded as authoritative.

    2026-08-14 -- significance requirement added. The old rule was
    ``|delta| >= margin`` alone, which is game-count-invariant: at the
    default margin=1 ANY non-tied split over 20+ decisive games earned a
    confident kept/reverted. Now the split must also clear an exact
    binomial test (``analyst.binomial_two_sided_p``) at ``alpha``.
    ``margin`` is retained as a backward-compatible pre-filter for
    callers (--sim-margin) that want a LARGER minimum effect than
    significance alone demands; at its default of 1 it is a no-op
    relative to the test.
    """
    from .analyst import binomial_two_sided_p

    status = getattr(ab_result, "status", None)
    if status != "done":
        return "pending"
    wins_a = ab_result.wins_a or 0
    wins_b = ab_result.wins_b or 0
    decisive = wins_a + wins_b
    if decisive < min_decisive:
        return "inconclusive"
    delta = wins_b - wins_a
    if abs(delta) < margin:
        return "neutral"
    if binomial_two_sided_p(wins_b, decisive) >= alpha:
        return "neutral"
    return "kept" if delta > 0 else "reverted"


def _ab_to_iteration_fields(ab_result) -> dict:
    """Extract win_rate_old / win_rate_new / margin / sim_report from
    an ABResult into the shape ``update_iteration_sim`` expects.

    Win-rate convention (2026-07-19, see knowledge_log's schema docstring):
    wins / DECISIVE games, where decisive = wins_a + wins_b -- the same
    denominator ``_verdict_from_ab`` gates on. The old wins/games denominator
    counted filler-won and unresolved-draw games the head-to-head pair can
    never "win", deflating both rates relative to the other knowledge_log
    writers (iteration_loop, save_iteration) and making the columns
    incomparable across runs.

    When decisive == 0 (all games drew or went to fillers -- or the sim was
    skipped with games=0) the win_rate keys are OMITTED: the caller passes
    the fields straight into ``update_iteration_sim``, which leaves absent
    columns untouched, so a fresh 'pending' row keeps its NULL win rates
    rather than recording a fabricated 0.0/0.0.
    """
    from .knowledge_log import decisive_win_rate

    fields: dict = {
        "sim_report": ab_result.to_dict() if hasattr(ab_result, "to_dict") else None,
    }
    total = getattr(ab_result, "games", 0) or 0
    wins_a = getattr(ab_result, "wins_a", 0) or 0
    wins_b = getattr(ab_result, "wins_b", 0) or 0
    if total > 0:
        # Margin is defined whenever the sim actually ran (0 is a real
        # observation there), independent of whether any game was decisive.
        fields["margin"] = wins_b - wins_a
    decisive = wins_a + wins_b
    if decisive > 0:
        fields["win_rate_old"] = decisive_win_rate(wins_a, decisive)
        fields["win_rate_new"] = decisive_win_rate(wins_b, decisive)
    return fields


#: Filename prefixes that are never filler-eligible. One tuple so the picker
#: and the "why did I get zero fillers?" census (R2-P22, 2026-08-20) can
#: never drift apart — the census exists to EXPLAIN this exact list, and a
#: second hand-maintained copy would eventually explain the wrong one.
#: Rationale per prefix lives in ``_pick_filler_decks``' docstring.
_FILLER_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "[USER]", "[CONTROL]", "[PREMADE]", "[REF]",
)


def _filler_exclusion_census(
    deck_dir: Path, exclude_paths: "list[Path]",
) -> dict:
    """Why the filler pool is what it is: ``{total, pair, eligible,
    excluded, by_prefix}``.

    ``exclude_paths`` (the v_n / v_n+1 decks being compared) is checked
    FIRST and counted as ``pair``, never as a prefix exclusion: those two
    files are the subject of the sim, and blaming their ``[USER]`` prefix
    for the empty pool would explain the wrong thing to the operator.

    Round-2 review 2026-08-20 (R2-P22). Adding ``[REF]`` to the exclusion
    list (2026-08-17) can zero out the filler pool on a box that never ran a
    bracket harvest — a deck dir of imports plus meta-test references is a
    realistic non-harvest setup, and it regressed from "ran the sim" to
    "Sim skipped", with a message that named only the count. An operator
    staring at a directory full of .dck files was told, in effect, that the
    files they can see do not exist.

    Called ONLY on the failure path, so the extra directory walk costs
    nothing in the normal case.
    """
    by_prefix: dict[str, int] = {}
    exclude_set = {p.name for p in exclude_paths}
    total = eligible = pair = 0
    for p in deck_dir.glob("*.dck"):
        total += 1
        if p.name in exclude_set:
            pair += 1
            continue
        matched = next(
            (pre for pre in _FILLER_EXCLUDED_PREFIXES
             if p.name.startswith(pre)),
            None,
        )
        if matched is not None:
            by_prefix[matched] = by_prefix.get(matched, 0) + 1
        else:
            eligible += 1
    return {
        "total": total,
        "pair": pair,
        "eligible": eligible,
        "excluded": sum(by_prefix.values()),
        "by_prefix": by_prefix,
    }


def _pick_filler_decks(
    deck_dir: Path,
    exclude_paths: list[Path],
    *,
    count: int = 2,
    target_bracket: Optional[int] = None,
    rng=None,
) -> list[str]:
    """Pick ``count`` opponent-pool deck filenames from ``deck_dir``.

    Bracket-aware ranking: when ``target_bracket`` is given, prefers
    fillers matching that bracket first, then adjacent brackets
    (delta=1), then delta=2, etc. A B4 user deck A/B'd against a B5
    cEDH filler + B2 casual filler produces NOISE-dominated verdicts:
    the cEDH crushes everything (both v_n and v_n+1 lose to it
    equally), the casual gets rolled (both v_n and v_n+1 beat it
    equally), and the v_n vs v_n+1 delta -- the signal we actually
    want -- drowns in filler asymmetry.

    With same-bracket fillers the games are competitive enough that
    the choice of v_n vs v_n+1 in seat-1 is the dominant variable.

    Auto-pick rules:
      - Skip any file under ``exclude_paths`` (the v_n + v_n+1 decks
        being compared -- pitting the new deck against the old deck's
        identical copy in the filler slots would be self-defeating).
      - Skip ``[USER]`` prefixed decks (those are the user's own
        work; the opponent pool is everything WITHOUT the prefix),
        ``[CONTROL]`` calibration decks, ``[PREMADE]``
        popularity-ranked imports, and -- since 2026-08-17 --
        ``[REF]`` meta-test references. ``[REF]`` decks are Moxfield
        top-likes: the SAME popularity bias ``[PREMADE]`` is excluded
        for, so seating them as fillers made that exclusion arbitrary
        rather than principled. The asymmetry now survives only where
        it is earned: ``[REF]`` stays a pool CANDIDATE in
        ``pool_curator._list_bracket_candidates`` (a real playable
        community build, worth RANKING) but is no longer filler-
        eligible, because a filler seat is never ranked -- its
        strength silently sets the A/B baseline instead.
      - When ``target_bracket`` is given, group candidates by
        |bracket_of_candidate - target_bracket| and walk the buckets
        from delta=0 up. Each bucket is shuffled via ``rng`` for
        variety within a tier.
      - Fillers with NO ``[B<N>]`` suffix (unparseable bracket) land
        in a final fallback bucket at delta=infinity -- used only
        if every parseable filler bucket can't fill ``count``.

    Returns the chosen filenames. Returns an empty list if fewer
    than ``count`` candidates exist total -- the caller surfaces
    "no fillers" and skips the sim with verdict='pending'.
    """
    import random as _random
    from .web._helpers import _bracket_from_filename
    if rng is None:
        rng = _random.Random()
    exclude_set = {p.name for p in exclude_paths}
    candidates = [
        p.name for p in deck_dir.glob("*.dck")
        if not p.name.startswith(_FILLER_EXCLUDED_PREFIXES)
        and p.name not in exclude_set
    ]
    if not candidates:
        return []

    # Bucket by bracket-distance to target. Files without a parseable
    # bracket land in their own bucket at the end of the priority list
    # so they're only used when nothing better is available.
    if target_bracket is None:
        # No target -- single bucket, alpha-sorted then shuffled. This
        # matches the pre-bracket-aware behavior for callers that don't
        # care.
        sorted_pool = sorted(candidates)
        rng.shuffle(sorted_pool)
        if len(sorted_pool) < count:
            return []
        return sorted_pool[:count]

    buckets: dict[int, list[str]] = {}
    unparseable: list[str] = []
    for name in sorted(candidates):
        b = _bracket_from_filename(name)
        if b is None:
            unparseable.append(name)
        else:
            buckets.setdefault(abs(b - target_bracket), []).append(name)

    picks: list[str] = []
    for delta in sorted(buckets.keys()):
        bucket = list(buckets[delta])
        rng.shuffle(bucket)
        for name in bucket:
            picks.append(name)
            if len(picks) >= count:
                return picks[:count]
    # Fall back to unparseable bracket only when everything else is
    # exhausted. Shuffled for variety.
    rng.shuffle(unparseable)
    for name in unparseable:
        picks.append(name)
        if len(picks) >= count:
            return picks[:count]
    if len(picks) < count:
        return []
    return picks[:count]


def _run_sim_and_record(
    args,
    out_path: Path,
    iteration_id: int,
    db_path: Optional[Path],
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Execute the Forge A/B sim and persist results to knowledge_log.

    Returns ``(sim_result_dict, error_str, verdict)``:
      - sim_result_dict: ``ABResult.to_dict()`` on success, or the
        partial result on a runtime failure
      - error_str: human-readable error if the sim couldn't complete,
        else None
      - verdict: 'kept'/'reverted'/'neutral' on success, 'pending' on
        a sim that was skipped or failed

    Never raises. All failure modes (Forge missing, fillers
    unavailable, runner crash) land as printed warnings + a
    ``verdict='pending'`` outcome so the iteration row stays
    consistent.
    """
    from .forge_runner import run_ab_simulation
    from .knowledge_log import update_iteration_sim

    # Resolve filler decks. Default: auto-pick 2 from the opponent
    # pool in the user's deck dir. Override: explicit --sim-fillers
    # comma-separated list (filenames relative to deck_dir).
    deck_dir = out_path.parent
    if args.sim_fillers:
        filler_names = [f.strip() for f in args.sim_fillers.split(",")
                        if f.strip()]
    else:
        filler_names = _pick_filler_decks(
            deck_dir,
            exclude_paths=[args.deck_path, out_path],
            count=2,
            # Bracket-match the fillers to the user's deck. A B4 vs B4
            # filler pod is competitive; B4 vs (B5 cEDH + B2 casual) is
            # filler-asymmetry-dominated and yields junk verdicts.
            target_bracket=args.bracket,
        )
    if len(filler_names) < 2:
        msg = (
            f"[sim] Need 2+ filler decks in {deck_dir} for a 4-player "
            f"Commander pod; found {len(filler_names)}. Sim skipped."
        )
        # Say WHY, when the reason is the prefix exclusion rather than an
        # empty directory (R2-P22, 2026-08-20). "found 0" in a directory
        # full of .dck files reads as a bug in the tool; naming the rule
        # and the two remedies makes it an actionable state. Only added
        # for auto-picked fillers — an explicit --sim-fillers list that
        # came up short is a typo, not a policy question.
        if not args.sim_fillers:
            census = _filler_exclusion_census(
                deck_dir, [args.deck_path, out_path],
            )
            if census["excluded"] and not census["eligible"]:
                breakdown = ", ".join(
                    f"{pre} x{n}"
                    for pre, n in sorted(census["by_prefix"].items())
                )
                msg += (
                    f" All {census['excluded']} of the other .dck files "
                    f"there are prefix-excluded from filler duty "
                    f"({breakdown}): [USER] decks are yours, [CONTROL] "
                    f"are calibration decks, and [PREMADE]/[REF] are "
                    f"popularity-ranked imports whose strength would "
                    f"silently set the A/B baseline. Fix it by harvesting "
                    f"an opponent pool (`commander-import --harvest "
                    f"{args.bracket}`) or by "
                    f"naming seats explicitly with --sim-fillers "
                    f"\"<file1.dck>,<file2.dck>\" (which bypasses the "
                    f"exclusion)."
                )
        if not args.json:
            print(msg, flush=True)
        # Still write 'pending' verdict explicitly so the row's state
        # is unambiguous (vs leaving the auto-curate default).
        try:
            update_iteration_sim(
                iteration_id=iteration_id,
                verdict="pending",
                notes=msg,
                db_path=db_path if db_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            if not args.json:
                print(f"[sim] WARN: could not persist pending verdict: "
                      f"{type(exc).__name__}: {exc}", flush=True)
        return None, msg, "pending"

    # LOUD sub-threshold warning, in the RIGHT units: --sim-games is
    # TOTAL pod games but the verdict gate counts DECISIVE games, and
    # the two filler seats win ~half the pod games (see
    # EXPECTED_DECISIVE_FRACTION). So the comparison must be
    # expected-decisive (sim_games * fraction) vs the gate -- comparing
    # raw sim_games to the gate (the pre-2026-07-20 bug) let 25 total
    # games pass silently while every verdict still landed
    # 'inconclusive' (~12-13 decisive < 20). Below the gate the Forge
    # time is spent on a verdict that -- in expectation -- can't
    # resolve. Printed on stderr deliberately: --json mode keeps stdout
    # machine-parseable, and commander-improve captures auto-curate's
    # stdout per round -- stderr is the only channel that reaches the
    # operator in all three invocation modes.
    expected_decisive = args.sim_games * EXPECTED_DECISIVE_FRACTION
    if expected_decisive < MIN_DECISIVE_GAMES_FOR_VERDICT:
        print(
            f"[sim] WARNING: --sim-games counts TOTAL 4-player pod games, "
            f"but the verdict gate counts DECISIVE games (won by the old "
            f"or new deck; the 2 filler seats take ~half the wins). "
            f"{args.sim_games} total pod games ~= "
            f"{int(expected_decisive)} expected decisive, below the "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT}-decisive gate "
            f"(MIN_DECISIVE_GAMES_FOR_VERDICT) -- expect 'inconclusive', "
            f"not kept/reverted/neutral. A verdict needs "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT} decisive ~= "
            f"{min_sim_games_for_verdict()}+ total games.",
            file=sys.stderr, flush=True,
        )
    if not args.json:
        print(f"[4/4] Running Forge A/B sim ({args.sim_games} games, "
              f"fillers={filler_names})...", flush=True)
    ab_result = run_ab_simulation(
        deck_a_path=args.deck_path,
        deck_b_path=out_path,
        games=args.sim_games,
        fillers=filler_names,
    )

    sim_payload = ab_result.to_dict()
    verdict = _verdict_from_ab(ab_result, margin=args.sim_margin)
    sim_fields = _ab_to_iteration_fields(ab_result)

    # Post-sim honesty: the pre-sim warning above is an ESTIMATE
    # (expected fraction 0.5); this reports the MEASURED outcome. When
    # the sim completed but produced fewer decisive games than the gate,
    # say exactly how many decisives the run actually got and what
    # total-games budget a verdict needs -- "got 11 decisive of 25
    # games" teaches the operator the real total->decisive conversion
    # for their pod, instead of leaving them to wonder why a
    # 25-games-looks-plenty run came back 'inconclusive'. Same stderr
    # channel as the pre-sim warning, for the same three-invocation-mode
    # reasons.
    if ab_result.status == "done":
        actual_decisive = (ab_result.wins_a or 0) + (ab_result.wins_b or 0)
        if actual_decisive < MIN_DECISIVE_GAMES_FOR_VERDICT:
            print(
                f"[sim] got {actual_decisive} decisive of "
                f"{ab_result.games} total games (filler seats/draws took "
                f"the rest); a verdict needs "
                f"{MIN_DECISIVE_GAMES_FOR_VERDICT} decisive ~= "
                f"{min_sim_games_for_verdict()}+ total games.",
                file=sys.stderr, flush=True,
            )

    # Build a human-readable note that captures the sim status + result
    # for the iteration row's verdict_notes column. Future analysts /
    # the dashboard tooltip use this to explain "why kept?"
    status = ab_result.status
    if status == "done":
        note = (
            f"A/B sim: old won {ab_result.wins_a}, new won "
            f"{ab_result.wins_b}, neutral={max(0, ab_result.games - ab_result.wins_a - ab_result.wins_b)} "
            f"({ab_result.games} games, margin={args.sim_margin})"
        )
    elif status == "skipped":
        note = f"A/B sim skipped: {ab_result.error or 'unknown reason'}"
    elif status == "failed":
        note = f"A/B sim failed: {ab_result.error or 'unknown error'}"
    else:
        note = f"A/B sim ended with unexpected status={status!r}"

    try:
        update_iteration_sim(
            iteration_id=iteration_id,
            verdict=verdict,
            notes=note,
            db_path=db_path if db_path else None,
            **sim_fields,
        )
    except Exception as exc:  # noqa: BLE001
        # Don't lose the sim result if the DB update fails -- return
        # the payload so the CLI summary + JSON still surface it.
        if not args.json:
            print(f"[sim] WARN: could not persist sim result: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        return sim_payload, f"{type(exc).__name__}: {exc}", verdict

    return sim_payload, None, verdict


def _log_auto_curate_iteration(
    src_deck_path: Path,
    new_deck_path: Path,
    bracket: int,
    proposal,  # forward-ref Proposal — imported lazily to avoid cycle
    db_path: Optional[Path] = None,
) -> int:
    """Persist a 'pending' Iteration row recording this auto-curate run.

    Reads the moxfield publicId out of the new .dck (falls back to the
    filename stem). Hooks the new row's parent_id to the most recent
    prior iteration of the same deck so the iteration chain stays
    threaded -- important for the upcoming knowledge_log graph view.

    Verdict is 'pending' -- we haven't actually played the new deck yet.
    Phase 2's analyst path (or a follow-up Forge sim) updates verdict
    + sim_report once results land.
    """
    from .iteration_loop import resolve_deck_id
    from .knowledge_log import (
        DEFAULT_DB_PATH,
        Iteration,
        iterations_for_deck,
        record_iteration,
    )

    effective_db = db_path or DEFAULT_DB_PATH

    deck_id = resolve_deck_id(new_deck_path, fallback=new_deck_path.stem)
    deck_name = new_deck_path.stem

    # Thread the iteration chain: find the latest existing iteration for
    # this deck_id and set it as parent. If none exists, parent_id stays
    # None (this becomes v1 in the log).
    prior = iterations_for_deck(deck_id, db_path=effective_db)
    parent_id = prior[-1].id if prior else None

    deck_snapshot = new_deck_path.read_text(encoding="utf-8")
    # Record what ACTUALLY LANDED in the .dck -- these are the changes
    # that produced the new deck snapshot. ``requested_*`` fields
    # preserve Claude's intent for analysis (which adds did the curator
    # want but balancing dropped?) without conflating the two.
    audit_manifest = {
        "added": list(proposal.applied_adds),
        "removed": list(proposal.applied_cuts),
        "rationale": proposal.rationale,
        "source": proposal.source,
        "dropped_for_bracket": list(proposal.dropped_for_bracket),
        "dropped_for_protection": list(proposal.dropped_for_protection),
        # Politics-shielded cuts (decision C2 / R2-P09, 2026-08-20).
        # Persisted beside the other refusal buckets so a later reader
        # can tell "the curator proposed no cuts" apart from "the guard
        # refused the cuts the curator proposed".
        "dropped_for_politics": list(proposal.dropped_for_politics),
        "dropped_for_color_identity": list(proposal.dropped_for_color_identity),
        "dropped_for_balance": list(proposal.dropped_for_balance),
        # Pair-drops from apply-time decklist validation — each entry
        # is {"cut": ..., "add": ...}. Persisted so iteration analysis
        # can spot proposals the LLM built against a stale/imagined
        # decklist (high pair-drop counts = curator quality signal).
        "dropped_unmatched_cut": list(proposal.dropped_unmatched_cut),
        "dropped_duplicate_add": list(proposal.dropped_duplicate_add),
        "dropped_commander_add": list(proposal.dropped_commander_add),
        "padded_count": proposal.padded_count,
        "padded_breakdown": dict(proposal.padded_breakdown),
        "requested_adds": list(proposal.adds),
        "requested_cuts": list(proposal.cuts),
        "src_deck": src_deck_path.name,
    }

    it = Iteration(
        deck_id=deck_id,
        deck_name=deck_name,
        bracket=bracket,
        parent_id=parent_id,
        audit_version="claude-auto",
        audit_manifest=audit_manifest,
        verdict="pending",
        deck_snapshot=deck_snapshot,
    )
    return record_iteration(it, db_path=effective_db)
