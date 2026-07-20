"""Forge A/B simulation helpers used by ``proposer.auto_curate_main``
and the knowledge_log row writer.

These hooks close the feedback loop on auto-curate: the iteration row
initially lands with ``verdict='pending'`` and the post-apply A/B sim
fills in the empirical result (kept / reverted / neutral) plus the
detailed metrics ``update_iteration_sim`` persists.

Public symbols:

  ``_DEFAULT_SIM_MARGIN``         — minimum delta to call kept/reverted.
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

import sys
from pathlib import Path
from typing import Optional


# Minimum margin (wins_b - wins_a) for an A/B run to be called a kept
# vs reverted vs neutral outcome. Default 1 means a 3-2 result is
# 'kept' rather than 'neutral' -- on a 5-game sim that's a meaningful
# signal even though it's noisy. The CLI can tune via --sim-margin.
_DEFAULT_SIM_MARGIN = 1

# Below this many DECISIVE games (wins_a + wins_b, draws excluded), an A/B
# result is too noisy to call: the win-rate standard error is ~0.5/sqrt(N)
# (N=10 -> +/-0.16, N=20 -> +/-0.11), which swamps the ~0.01-0.05 effect a
# curator swap actually has. Below the threshold the verdict is 'inconclusive'
# rather than a confident kept/reverted that a single-game flip could invert.
MIN_DECISIVE_GAMES_FOR_VERDICT = 20


def _verdict_from_ab(ab_result, *, margin: int = _DEFAULT_SIM_MARGIN,
                     min_decisive: int = MIN_DECISIVE_GAMES_FOR_VERDICT) -> str:
    """Map an ``ABResult`` to a verdict label.

    Returns one of 'kept' / 'reverted' / 'neutral' / 'inconclusive' / 'pending':

      'kept'         -- new deck won at least ``margin`` more games than old
      'reverted'     -- old deck won at least ``margin`` more games than new
      'neutral'      -- difference within margin at a TRUSTWORTHY sample size
                        (e.g. 21-20 over 41 decisive games)
      'inconclusive' -- fewer than ``min_decisive`` decisive games, so the
                        result is below the noise floor regardless of margin
                        (a 3-2 at 5 games is a coin flip, not a tie)
      'pending'      -- sim didn't complete (status='skipped' or 'failed')

    The split between 'neutral' and 'inconclusive' matters: 'neutral' is a
    real near-tie we can trust; 'inconclusive' is "not enough games to say."
    Gating low-N runs to 'inconclusive' stops a noise verdict from being
    recorded as authoritative.
    """
    status = getattr(ab_result, "status", None)
    if status != "done":
        return "pending"
    wins_a = ab_result.wins_a or 0
    wins_b = ab_result.wins_b or 0
    decisive = wins_a + wins_b
    if decisive < min_decisive:
        return "inconclusive"
    delta = wins_b - wins_a
    if delta >= margin:
        return "kept"
    if delta <= -margin:
        return "reverted"
    return "neutral"


def _ab_to_iteration_fields(ab_result) -> dict:
    """Extract win_rate_old / win_rate_new / margin / sim_report from
    an ABResult into the shape ``update_iteration_sim`` expects.

    Win rates are computed as wins/total_games (ignoring draws since
    Forge's AI rarely produces them). When the sim was skipped or
    games=0, win rates are None -- caller passes None to
    ``update_iteration_sim`` which preserves existing column values.
    """
    fields: dict = {
        "sim_report": ab_result.to_dict() if hasattr(ab_result, "to_dict") else None,
    }
    total = getattr(ab_result, "games", 0) or 0
    if total > 0:
        wins_a = getattr(ab_result, "wins_a", 0) or 0
        wins_b = getattr(ab_result, "wins_b", 0) or 0
        fields["win_rate_old"] = round(wins_a / total, 4)
        fields["win_rate_new"] = round(wins_b / total, 4)
        fields["margin"] = wins_b - wins_a
    return fields


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
        work; the opponent pool is everything WITHOUT the prefix).
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
        if not p.name.startswith("[USER]")
        and not p.name.startswith("[CONTROL]")  # never use a calibration deck as filler
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

    # LOUD sub-threshold warning: with fewer total games than the
    # min-decisive gate, the verdict is STRUCTURALLY 'inconclusive' --
    # even a clean sweep can't reach MIN_DECISIVE_GAMES_FOR_VERDICT
    # decisive games, so kept/reverted/neutral are unreachable and the
    # Forge time is spent on a verdict that can never resolve. Printed
    # on stderr deliberately: --json mode keeps stdout machine-parseable,
    # and commander-improve captures auto-curate's stdout per round --
    # stderr is the only channel that reaches the operator in all three
    # invocation modes.
    if args.sim_games < MIN_DECISIVE_GAMES_FOR_VERDICT:
        print(
            f"[sim] WARNING: --sim-games {args.sim_games} < "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT} (MIN_DECISIVE_GAMES_FOR_"
            f"VERDICT): at most {args.sim_games} decisive games are "
            f"possible, so the verdict will ALWAYS be 'inconclusive' -- "
            f"never kept/reverted/neutral. Pass --sim-games >= "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT} (draws don't count as "
            f"decisive, so add headroom) for a verdict that can resolve.",
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
