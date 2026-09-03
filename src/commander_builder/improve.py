"""commander-improve — greedy single-deck improvement loop (FP-012, slice 1).

The bounded first slice of FP-012 (the autonomous deck-improvement
agent). Runs the existing ``commander-auto-curate`` pipeline (advisor →
Claude curator → apply → Forge A/B sim → knowledge_log) on ONE deck for
``--rounds N`` iterations, advancing **greedily**: a round's proposed
deck becomes the base for the next round *only* when the seat-attributed
A/B sim verdict is ``kept`` — the new deck's decisive-game split beat
the old deck with statistical significance (exact two-sided binomial
test at ``_proposer_sim.VERDICT_ALPHA`` over at least
``MIN_DECISIVE_GAMES_FOR_VERDICT`` decisive games; ``--sim-margin``
survives as a back-compat minimum-margin pre-filter on top).
``reverted`` / ``neutral`` / ``inconclusive`` / ``pending`` rounds keep
the current base — the candidate ``.dck`` is left on disk but not built
upon. That's the greedy keep-if-better contract.

Replication (2026-08-17, owner decision)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
An UNATTENDED run no longer advances the base on ONE ``kept``. A first
``kept`` triggers a SECOND independent A/B over the same old-vs-new
pairing, and the base moves only if that run says ``kept`` too. Rationale:
at ``alpha`` 0.05 a truly neutral swap still earns a ``kept`` about 1 run
in 40, and an overnight loop chains its rounds — one false positive
becomes the base every later round is measured against, so the error is
not just recorded, it is *compounded*. Two independent significant runs
in the same direction cut that per-advance false-positive rate to ~1 in
1,600. The cost is honest and stated up front: a confirmed swap costs 2x
sim time. ``--replicate`` / ``--no-replicate`` control it; the default is
ON for the unattended round loop (greedy / ``--search-budget``) and OFF
for the interactive ``--strategy bandit`` explorer (see
``resolve_replicate_default``).

What that gate actually buys, both directions (2026-08-20, R2-D2)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The 1-in-1,600 figure above is real, and it was the only number this
docstring used to state. Here is the other half, computed exactly over
the decisive-count distribution at 45 games with the 20-decisive gate
and the exact two-sided binomial at alpha 0.05, for a GENUINELY good
swap worth +5pp (a 55% decisive win rate):

    P(kept | one 45-game run)      = 3.65%
    P(advance | replication gate)  = 0.13%   per round
    P(>= 1 advance | 10 rounds)    = 1.3%    per overnight run
    likelihood ratio of an advance = 3.0 single-shot -> 9.2 replicated

So the honest positioning, and the one the owner chose (R2-D2, decided
2026-08-20 — reposition and document, no behavior change): this loop is
a FALSE-POSITIVE-PROOF SCREEN, not a background improver. "Nothing
advanced" is the EXPECTED outcome of an overnight run — at ~1.3%
true-positive throughput per 10-round run, it is what you should expect
to see even when the curator is proposing genuinely good swaps. An
advance is therefore rare and worth reading, not a progress bar. And
because LR ≈ 9.2, advances only become majority-TRUE once the curator's
true-hit rate clears ~10%; FP-002 measured curation net-neutral over
37,120 games, so treat a single advance as a lead to investigate rather
than a proven improvement.

Raising ``--sim-games`` to buy power was considered and declined: honest
power costs real hours per round, and decision A1 already repositioned
the Forge sim overall as a deep-dive instrument for questions worth real
game counts. This is A1's argument applied to A2's default.

What this slice deliberately is NOT (still parked under the full FP-012):
no multi-arm-bandit / Bayesian swap selection, no intent learning, no
unbounded convergence — just a fixed-N greedy loop. It *composes* the
auto-curate machinery (one `auto_curate_main` call per round) rather than
reimplementing the pipeline, so every round inherits seat-attributed
sims, color-identity filtering, protected-card handling, bracket-aware
fillers, and knowledge_log rows for free.

Post-fix attribution only: each round's sim uses the seat-attribution
fix (`e8777b6`), so verdicts are trustworthy and the new knowledge_log
rows land post-`--min-id 314`.

Entry point: ``commander-improve --deck <id> --rounds N`` (or pass a
``.dck`` path positionally).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .forge_runner import VENDOR_FORGE
from .intent import (
    Intent,
    intent_protect_cards,
    free_text_bias_slugs,
    learn_intent,
    resolve_preferences,
)
# Imported (not duplicated) so the sub-threshold warning and the
# --sim-games default can never drift from the verdict gate in
# _proposer_sim._verdict_from_ab.
from ._proposer_sim import (
    EXPECTED_DECISIVE_FRACTION,
    MIN_DECISIVE_GAMES_FOR_VERDICT,
    min_sim_games_for_verdict,
)

# Default Commander deck directory — mirrors compare_versions.DECK_DIR /
# doctor.DECK_DIR so ``--deck <id>`` resolves against the same place the
# rest of the toolchain reads decks from.
DEFAULT_DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


@dataclass
class RoundResult:
    """Outcome of a single improve round."""

    round: int
    input_deck: str
    output_deck: Optional[str]
    # kept / reverted / neutral / inconclusive / pending / no-op / error
    verdict: str
    advanced: bool  # did the greedy base move forward this round?
    iteration_id: Optional[int] = None
    win_rate_old: Optional[float] = None
    win_rate_new: Optional[float] = None
    margin: Optional[int] = None
    applied_adds: int = 0
    applied_cuts: int = 0
    error: Optional[str] = None
    # Replication (2026-08-17). ``replicated`` is None when no confirming
    # run was attempted (replication off, or the round wasn't 'kept'),
    # True when the second A/B agreed, False when it disagreed.
    # ``replication_verdict`` is the SECOND run's own verdict in the
    # existing vocabulary -- no new label is invented, so every consumer
    # of ``verdict`` keeps working. When the second run disagrees,
    # ``verdict`` above holds that second verdict (the honest,
    # non-advancing outcome) while this field keeps it legible next to
    # the 'kept' that triggered the re-run.
    replicated: Optional[bool] = None
    replication_verdict: Optional[str] = None
    # FP-016 Phase 1 (2026-08-20). The LLM panel's OPINION of this round's
    # pairing, in the same vocabulary but from a different instrument.
    # None means the judge did not run (flag off, by default) or could not
    # — deliberately NOT folded into ``verdict``, which stays the sim's
    # word alone. Nothing in the loop reads this field to make a decision;
    # it exists to be displayed and to be written to the row.
    judge_verdict: Optional[str] = None


@dataclass
class ImproveResult:
    """Aggregate result of an improve run."""

    deck_id: str
    start_deck: str
    final_deck: str
    rounds_requested: int
    rounds_run: int
    rounds_kept: int
    converged: bool  # stopped early because a round proposed no changes
    history: list[RoundResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _default_round_fn(deck_path: Path, round_no: int, args) -> RoundResult:
    """Run one auto-curate round and project its JSON into a RoundResult.

    Composes ``auto_curate_main`` with ``--run-sim --json`` (capturing
    its stdout, exactly as batch mode's ``_process_one_deck`` does) so
    the round inherits the full pipeline. Never raises — pipeline
    failures land as ``verdict='error'`` so the loop can decide whether
    to stop.

    Intent integration (Slice A)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    When ``args.intent`` is an ``Intent`` object, its ``key_wincons``
    are appended to the ``--protect`` list passed to auto-curate, so
    the curator cannot cut the deck's identity pieces.  The intent's
    ``themes`` are passed as ``--intent-themes`` to auto-curate if
    that flag is supported; auto-curate ignores unknown flags, so this
    is forward-compatible.
    """
    from ._proposer_cli import auto_curate_main

    # Merge intent-derived protect cards with any CLI-specified ones.
    # intent_protect_cards returns [] when args.intent is None/missing.
    intent: Optional[Intent] = getattr(args, "intent", None)
    protect_cards = list(getattr(args, "protect", []) or [])
    protect_cards += intent_protect_cards(intent)

    argv: list[str] = [
        str(deck_path),
        "--bracket", str(args.bracket),
        "--run-sim",
        "--json",
        "--mode", args.mode,
        "--source", args.source,
        "--model", args.model,
        "--sim-games", str(args.sim_games),
        "--sim-margin", str(args.sim_margin),
    ]
    if getattr(args, "no_manabase_rebuild", False):
        argv += ["--no-manabase-rebuild"]
    if args.sim_fillers:
        argv += ["--sim-fillers", args.sim_fillers]
    if args.db_path:
        argv += ["--db-path", args.db_path]
    for card in protect_cards:
        argv += ["--protect", card]
    if args.protect_from:
        argv += ["--protect-from", args.protect_from]
    # Soft-bias: pass the intent's DERIVED themes as --intent-themes so
    # the advisor candidate pool is ranked toward those EDHREC tag pages.
    # Only appended when the slug list is non-empty — the no-themes path
    # must be identical to pre-Slice-A behavior. Slugs the intent's FREE
    # TEXT (`stated` primer / `pilot_preferences`) mentions travel on
    # --free-text-themes instead (R3 F-03, 2026-09-03): as one list, the
    # free-text slugs competed for the advisor's 4-page cap and evicted
    # the tribe page (whose cards feed cut-protection); on their own flag
    # they are fetched as EXTRA pages that rank adds and protect nothing.
    derived_slugs = [s.strip() for s in (getattr(intent, "themes", None)
                                         or []) if s.strip()]
    if derived_slugs:
        argv += ["--intent-themes", ",".join(dict.fromkeys(derived_slugs))]
    free_text_slugs = free_text_bias_slugs(intent)
    if free_text_slugs:
        argv += ["--free-text-themes", ",".join(free_text_slugs)]

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = auto_curate_main(argv)
    except Exception as exc:  # noqa: BLE001 — round isolation
        return RoundResult(
            round=round_no, input_deck=str(deck_path), output_deck=None,
            verdict="error", advanced=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    raw = buf.getvalue().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}

    if rc != 0 or not payload:
        return RoundResult(
            round=round_no, input_deck=str(deck_path), output_deck=None,
            verdict="error", advanced=False,
            error=f"auto-curate returned rc={rc} with no parseable JSON",
        )

    proposal = payload.get("proposal") or {}
    applied_adds = len(proposal.get("applied_adds", []) or [])
    applied_cuts = len(proposal.get("applied_cuts", []) or [])

    sim_report = payload.get("sim_report") or {}
    games = sim_report.get("games") or 0
    wins_a = sim_report.get("wins_a")
    wins_b = sim_report.get("wins_b")
    # CONVENTION DIVERGENCE (documented, not fixed — 2026-07-20): these
    # RoundResult fields share the win_rate_old/new NAMES with the
    # knowledge_log columns but use a different denominator: wins / ALL
    # games (draws and filler wins included), not wins / head-to-head
    # decisive (wins_a + wins_b). They are CLI progress display only —
    # never persisted to knowledge_log (the auto-curate subprocess writes
    # the row itself via _ab_to_iteration_fields, on the one convention) —
    # so they are left as-is rather than silently changing the improve
    # loop's printed/JSON output. Do NOT pool these with the DB columns.
    wr_old = round(wins_a / games, 4) if games and wins_a is not None else None
    wr_new = round(wins_b / games, 4) if games and wins_b is not None else None
    margin = (wins_b - wins_a) if (wins_a is not None and wins_b is not None) else None

    return RoundResult(
        round=round_no,
        input_deck=str(deck_path),
        output_deck=payload.get("output_deck"),
        verdict=payload.get("sim_verdict") or "pending",
        advanced=False,  # the loop sets this when it greedily advances
        iteration_id=payload.get("iteration_id"),
        win_rate_old=wr_old,
        win_rate_new=wr_new,
        margin=margin,
        applied_adds=applied_adds,
        applied_cuts=applied_cuts,
    )


@dataclass
class Replication:
    """Outcome of the confirming SECOND A/B behind a first ``kept``.

    ``verdict`` is that second run's own verdict, drawn from the existing
    kept / reverted / neutral / inconclusive / pending vocabulary --
    deliberately not a new label, because ``knowledge_log`` constrains the
    verdict column to exactly that set and every dashboard/report query
    reads it. A failed replication is therefore recorded as the second
    run's honest verdict PLUS a ``replication_failed`` annotation in the
    notes (``notes`` below), which is the field the schema does leave
    free-text.

    When the confirming sim could not RUN AT ALL (no fillers, crashed
    JVM) the verdict is ``inconclusive``, not ``pending`` (2026-08-20,
    decision R2-D3). ``pending`` means "this row's sim didn't complete",
    and run 1 DID complete -- it is the row's ``sim_report``, with win
    rates. Stamping ``pending`` over it produced a row that contradicted
    its own evidence, and any consumer re-deriving state from the
    verdict alone read it as unmeasured. ``inconclusive`` already means
    exactly what happened: measured, not decided. The
    ``replication_failed`` note carries the detail, and the structured
    ``sim_report[SIM_REPORT_REPLICATION_KEY]`` carries ``ran: False``
    plus the error.
    """

    verdict: str
    confirmed: bool
    notes: str
    margin: Optional[int] = None


def _run_confirm_sim(base_path: Path, candidate_path: Path, args):
    """Run ONE fresh A/B over ``base_path`` vs ``candidate_path``.

    The whole confirm path is deliberately a BARE sim call, not another
    round / bandit pull: a round would re-curate (a different swap, so
    not a replication at all) and, more importantly, its own ``kept``
    would want confirming in turn. Straight-line code here is what makes
    "a replication can never itself recurse" a structural property rather
    than a flag someone has to remember to unset.

    Returns ``(ab_result_or_None, fillers, error_message)``.
    """
    from .forge_runner import run_ab_simulation
    from ._proposer_sim import _pick_filler_decks

    deck_dir = base_path.parent
    if getattr(args, "sim_fillers", None):
        fillers = [f.strip() for f in args.sim_fillers.split(",") if f.strip()]
    else:
        # Fillers are RE-DRAWN (same bracket-matched rule, fresh shuffle)
        # rather than replayed. The pairing under test -- old deck vs new
        # deck -- is what must stay identical for this to be a
        # replication; re-seating the same two opponents would also
        # re-inherit whatever filler matchup helped produce run 1's
        # split, which is the failure mode a confirmation is supposed to
        # catch.
        fillers = _pick_filler_decks(
            deck_dir, exclude_paths=[base_path, candidate_path], count=2,
            target_bracket=args.bracket,
        )
    if len(fillers) < 2:
        return None, fillers, (
            f"need 2 filler decks in {deck_dir} for a 4-player pod, "
            f"found {len(fillers)}"
        )
    ab = run_ab_simulation(
        deck_a_path=base_path, deck_b_path=candidate_path,
        games=args.sim_games, fillers=fillers,
    )
    return ab, fillers, None


def _confirm_sim_completed(ab) -> bool:
    """True only when a confirming A/B actually PLAYED: an ABResult with
    ``status == 'done'``. ``None`` (no fillers) and any other status
    ('failed' = crashed JVM, 'skipped' = no Forge) mean the gate's second
    half never executed (R2-D3; R3 C-02, 2026-09-03). Both replication
    sites — the round writer and the bandit evaluator — test THIS, so
    the two cannot disagree on what "could not run" means."""
    return ab is not None and getattr(ab, "status", None) == "done"


def _confirm_sim_error(ab) -> str:
    """The reason a non-completed confirm sim gives, for notes/records."""
    if ab is None:
        return "no confirming A/B result"
    status = getattr(ab, "status", None) or "unknown"
    err = getattr(ab, "error", None)
    return f"confirm sim status={status!r}" + (f": {err}" if err else "")


def _default_replicate_fn(
    base_path: Path,
    candidate_path: Path,
    args,
    iteration_id: Optional[int] = None,
) -> Replication:
    """Confirm a round's first ``kept`` with a second independent A/B.

    Runs the fresh sim, maps it through the SAME ``_verdict_from_ab``
    machinery every other verdict goes through (significance test +
    decisive gate + ``--sim-margin`` pre-filter), and folds the outcome
    back into the round's knowledge_log row so the persisted history
    reflects what actually happened:

      - confirmed  → verdict stays 'kept'; run 1's note is KEPT and a
        ``replication_confirmed`` line is APPENDED to it with the second
        run's split.
      - disagreed  → the row is rewritten to the SECOND run's verdict
        (the non-advancing one) with a ``replication_failed`` line
        appended, naming both verdicts and the second split. Run 1's
        numbers stay in the row's ``sim_report`` / win-rate columns,
        which is why this writer deliberately doesn't overwrite them:
        it ADDS the confirming evidence rather than replacing the
        measured record. Leaving 'kept' on a row whose deck never
        became the base would tell every later reader -- the dashboard,
        FP-013's high-confidence counter, a future Phase 3 training set
        -- that a swap was adopted when it wasn't.

    In both cases run 2 is also persisted STRUCTURALLY, as
    ``sim_report[SIM_REPORT_REPLICATION_KEY]`` -- its wins, games,
    decisive count and verdict as data, not prose (2026-08-20). Two
    things were wrong before: this writer passed a fresh ``notes``
    string, and ``update_iteration_sim`` overwrites notes, so run 1's
    "A/B sim: old won X, new won Y (N games, margin=M)" line was
    DESTROYED by the confirmation -- the docstring here claimed notes
    "gain a line" when the code replaced them. And run 2's games existed
    nowhere structured, so a confirmed 'kept' row understated the Forge
    games behind it by half and no pooled effort/game-count analysis
    could see the second run at all.

    The verdict parameters both runs were scored under
    (``--sim-margin`` / alpha / decisive floor) are stamped into
    ``sim_report[SIM_REPORT_VERDICT_PARAMS_KEY]`` at the same time, so
    the row says what bar its verdict cleared instead of leaving it to
    be inferred from the code version that wrote it. Run 1's verdict
    used the same margin -- improve passes ``--sim-margin`` straight
    through to the auto-curate subprocess that wrote the row.

    Anything that stops the confirm sim from producing a verdict (no
    fillers, crashed JVM) counts as NOT confirmed: unattended replication
    is a gate, and an unrunnable gate stays shut. The row is labeled
    ``inconclusive`` in that case, NOT ``pending`` -- run 1 completed, so
    'the sim didn't complete' would be a lie about the row's own
    sim_report (2026-08-20, R2-D3; see ``Replication``).

    Knowledge_log failures are non-fatal, matching every other writer in
    this pipeline -- the .dck is on disk and the loop's decision is
    already made; losing the row shouldn't sink the run.
    """
    from ._proposer_sim import (
        MIN_DECISIVE_GAMES_FOR_VERDICT,
        VERDICT_ALPHA,
        _verdict_from_ab,
    )
    from .knowledge_log import (
        SIM_REPORT_REPLICATION_KEY,
        SIM_REPORT_VERDICT_PARAMS_KEY,
        verdict_provenance,
    )

    provenance = verdict_provenance(
        margin=args.sim_margin,
        alpha=VERDICT_ALPHA,
        min_decisive=MIN_DECISIVE_GAMES_FOR_VERDICT,
    )

    ab, _fillers, error = _run_confirm_sim(base_path, candidate_path, args)
    if not _confirm_sim_completed(ab):
        # R2-D3 (2026-08-20): 'inconclusive', not 'pending'. Run 1 is a
        # COMPLETED sim sitting in this row's sim_report; 'pending' means
        # "the sim didn't complete" and would contradict it.
        #
        # R3 C-02 (2026-09-03): this branch now also covers a confirm
        # sim that STARTED and came back 'failed' (JVM crash) or
        # 'skipped' (Forge not locatable) -- the case the docstring
        # above always named. ``run_ab_simulation`` never raises; those
        # outcomes are ABResults whose wins_a/wins_b/games are dataclass
        # DEFAULTS of 0, and routing them through ``_verdict_from_ab``
        # rewrote the completed row to 'pending' beside a structured
        # ``replication`` record of fabricated zeros ("run 2 was a 0-0
        # over 0 games"). Counts are None here, never 0: nothing was
        # measured.
        error = error or _confirm_sim_error(ab)
        rep = Replication(
            verdict="inconclusive", confirmed=False,
            notes=(f"replication_failed: confirming A/B could not run "
                   f"({error}); run 1's measurement stands but the "
                   f"discipline's second half never executed, so no "
                   f"verdict; base NOT advanced"),
        )
        run2: dict = {"ran": False, "error": error, "verdict": rep.verdict,
                      "confirmed": False,
                      "status": getattr(ab, "status", None),
                      "wins_old": None, "wins_new": None, "games": None,
                      "decisive": None, "margin": None}
    else:
        verdict = _verdict_from_ab(ab, margin=args.sim_margin)
        wins_a = getattr(ab, "wins_a", None)
        wins_b = getattr(ab, "wins_b", None)
        margin = (wins_b - wins_a) if (wins_a is not None
                                       and wins_b is not None) else None
        split = (f"old={wins_a} new={wins_b} over "
                 f"{getattr(ab, 'games', 0)} games")
        if verdict == "kept":
            rep = Replication(
                verdict=verdict, confirmed=True, margin=margin,
                notes=(f"replication_confirmed: run 1 kept, run 2 kept "
                       f"({split}); base advanced"),
            )
        else:
            rep = Replication(
                verdict=verdict, confirmed=False, margin=margin,
                notes=(f"replication_failed: run 1 kept, run 2 {verdict} "
                       f"({split}); base NOT advanced -- the second "
                       f"independent A/B did not confirm the first"),
            )
        run2 = {
            "ran": True,
            "verdict": verdict,
            "confirmed": rep.confirmed,
            "wins_old": wins_a,
            "wins_new": wins_b,
            "games": getattr(ab, "games", None),
            "decisive": (
                (wins_a or 0) + (wins_b or 0)
                if (wins_a is not None or wins_b is not None) else None
            ),
            "margin": margin,
            "status": getattr(ab, "status", None),
        }
    # No per-run copy of the provenance: both runs were scored with the
    # same args, and the row carries one authoritative
    # SIM_REPORT_VERDICT_PARAMS_KEY below. Two copies could drift.

    if iteration_id is not None:
        from .knowledge_log import update_iteration_sim
        try:
            update_iteration_sim(
                iteration_id=iteration_id,
                # 'kept' only survives a confirmation; otherwise the row
                # carries the second run's verdict. sim_report / win
                # rates are deliberately NOT overwritten -- run 1's
                # numbers stay the row's measured record. The confirming
                # run is ADDED beside them (note appended, run 2 merged
                # into sim_report under its own key), never on top of
                # them.
                verdict=rep.verdict,
                notes=rep.notes,
                notes_append=True,
                sim_report_merge={
                    SIM_REPORT_REPLICATION_KEY: run2,
                    SIM_REPORT_VERDICT_PARAMS_KEY: provenance,
                },
                db_path=Path(args.db_path) if getattr(args, "db_path", None)
                else None,
            )
        except Exception as exc:  # noqa: BLE001 — history loss, not a crash
            if not getattr(args, "json", False):
                _safe_print(f"[improve] WARN: could not persist replication "
                            f"outcome: {type(exc).__name__}: {exc}", flush=True)
    return rep


def _default_judge_round(
    base_path: Path,
    candidate_path: Path,
    args,
    iteration_id: Optional[int] = None,
) -> Optional[str]:
    """FP-016 Phase 1: run the blinded panel on this round's pairing.

    OBSERVE-ONLY, and structurally so. This function returns a string for
    display and writes two columns; it has no access to the loop's advance
    decision and nothing it returns is consulted by one. The judge does
    not advance decks and does not gate anything (FP-016 §6).

    The pairing is the SAME old-vs-new the sim judged (``base_path`` is
    the round's input deck, ``candidate_path`` its output), and the result
    lands on the SAME row via ``update_iteration_judge`` — which is the
    only reason the Phase 2 agreement table can join the two instruments
    at all (decision D1: both must answer about the same object).

    Runs automatically whenever the flag is on (decision D3): an on-demand
    judge would sample exactly the pairings someone was already curious
    about, which is the one sampling rule guaranteed to poison the
    agreement table.

    Returns the judge verdict, or None when the judge did not run. Raises
    nothing it can help — but the loop wraps the call anyway, so even an
    injected judge that throws cannot sink a round.
    """
    from . import deck_judge

    if not deck_judge.is_enabled():
        return None
    report = deck_judge.judge_pairing(
        base_path, candidate_path,
        intent=getattr(args, "intent", None),
        # Test seam: the improve CLI never sets this, so production always
        # gets the real transport ladder. Threading it through args (rather
        # than a new run_improve_loop parameter) keeps the offline test
        # path identical to the production call path.
        judge_fn=getattr(args, "judge_fn", None),
    )
    if iteration_id is not None:
        from .knowledge_log import update_iteration_judge
        update_iteration_judge(
            iteration_id=iteration_id,
            judge_verdict=report.verdict,
            judge_report=report.to_dict(),
            db_path=Path(args.db_path) if getattr(args, "db_path", None)
            else None,
        )
    return report.verdict


def run_improve_loop(
    deck_path: Path,
    deck_id: str,
    rounds: int,
    args,
    *,
    round_fn: Callable[[Path, int, object], RoundResult] = _default_round_fn,
    replicate_fn: Callable[..., Replication] = _default_replicate_fn,
    judge_round_fn: Callable[..., Optional[str]] = _default_judge_round,
) -> ImproveResult:
    """Greedy keep-if-better loop over ``rounds`` auto-curate rounds.

    The loop is intentionally pure of pipeline detail: it calls
    ``round_fn`` per round (default composes auto-curate) and only ever
    advances the base deck on a ``kept`` verdict. Injecting ``round_fn``
    lets tests drive the loop with scripted verdicts and never touch
    Forge / Anthropic.

    Replication (2026-08-17): when ``args.replicate`` is true, a first
    ``kept`` does not advance on its own -- ``replicate_fn`` runs a
    second independent A/B over the same old-vs-new pairing and the base
    moves only if that run is ``kept`` too (see this module's docstring
    for why, and ``_default_replicate_fn`` for how the disagreement is
    recorded). The gate lives HERE, in the loop, rather than in either
    round_fn, because both round shapes -- the curator round and
    ``improve_search``'s bandit-search round -- reach the base-advance
    decision through this one place. ``args`` without a ``replicate``
    attribute means off, which keeps the library callable (and every
    pre-2026-08-17 caller) single-shot unless the CLI resolved a default.

    Stop conditions:
      - a round errors (verdict='error') → stop, record the round.
      - a round proposed zero changes (applied_adds + applied_cuts == 0)
        → converged; record as 'no-op' and stop (nothing left to try).
      - otherwise run all ``rounds``.
    """
    current = Path(deck_path)
    start = current
    history: list[RoundResult] = []
    kept = 0
    converged = False
    replicate = bool(getattr(args, "replicate", False))

    for r in range(1, rounds + 1):
        rr = round_fn(current, r, args)

        if rr.verdict == "error":
            history.append(rr)
            break

        # Convergence: the curator proposed nothing applicable, so
        # further rounds would just repeat. Mark and stop.
        if rr.applied_adds == 0 and rr.applied_cuts == 0:
            rr.verdict = "no-op"
            history.append(rr)
            converged = True
            break

        # FP-016 Phase 1, observe-only. Deliberately placed BEFORE the
        # advance block and deliberately unable to reach it: the judge sees
        # the same pairing the sim saw, writes its opinion beside the sim
        # verdict, and the loop then makes exactly the decision it would
        # have made with the judge switched off (pinned by a test that runs
        # the same script with the judge on, off, and failing, and compares
        # the advance flags).
        #
        # The try/except is the "a judge failure must never sink the round"
        # guarantee, and it lives HERE rather than inside the judge so it
        # holds for ANY injected judge_round_fn, not just the default one.
        # A failed judge leaves the row's judge columns NULL, which is the
        # honest record: no opinion was formed.
        if rr.output_deck:
            try:
                rr.judge_verdict = judge_round_fn(
                    current, Path(rr.output_deck), args, rr.iteration_id,
                )
            except Exception as exc:  # noqa: BLE001 — opinion loss, not a crash
                if not getattr(args, "json", False):
                    _safe_print(
                        f"[improve] WARN: deck judge failed on round {r} "
                        f"({type(exc).__name__}: {exc}); continuing without "
                        f"an opinion (the sim verdict is unaffected).",
                        flush=True,
                    )

        # Greedy advance: only build on the new deck when it won -- and,
        # under replication, only when a second independent A/B agrees.
        if rr.verdict == "kept" and rr.output_deck:
            advance = True
            if replicate:
                if not getattr(args, "json", False):
                    _safe_print(
                        f"[improve] round {r}: first A/B says 'kept'; "
                        f"running the confirming second A/B before "
                        f"advancing the base...", flush=True,
                    )
                rep = replicate_fn(current, Path(rr.output_deck), args,
                                   rr.iteration_id)
                rr.replicated = rep.confirmed
                rr.replication_verdict = rep.verdict
                advance = rep.confirmed
                if not rep.confirmed:
                    # Report the outcome the EVIDENCE supports: the
                    # second run's verdict, not the unconfirmed 'kept'.
                    # rr.replication_verdict keeps the pair legible.
                    rr.verdict = rep.verdict
                if not getattr(args, "json", False):
                    _safe_print(f"[improve] round {r}: {rep.notes}",
                                flush=True)
            if advance:
                current = Path(rr.output_deck)
                rr.advanced = True
                kept += 1

        history.append(rr)

    return ImproveResult(
        deck_id=deck_id,
        start_deck=str(start),
        final_deck=str(current),
        rounds_requested=rounds,
        rounds_run=len(history),
        rounds_kept=kept,
        converged=converged,
        history=history,
    )


def _safe_print(text: str = "", *, file=None, flush: bool = False) -> None:
    """``print`` that survives consoles that can't encode the text.

    Windows consoles frequently run cp1252 (or cp437), which cannot
    represent characters like ``Δ`` (U+0394) used in the run summary —
    a bare ``print`` there raises UnicodeEncodeError and kills the
    process AFTER all the real work finished. Guard at the print site:
    try the normal print, and on UnicodeEncodeError re-encode with
    ``errors='replace'`` against the stream's own encoding so the
    summary stays readable (unencodable chars become ``?``).

    Deliberately NOT a global stdout reconfiguration (e.g.
    ``sys.stdout.reconfigure``): downstream parsers consume this
    process's stdout, and changing the stream's encoding could alter
    bytes for every other write. Only the fallback path degrades, and
    only for the offending line.
    """
    stream = file if file is not None else sys.stdout
    try:
        print(text, file=stream, flush=flush)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        safe = text.encode(encoding, errors="replace").decode(
            encoding, errors="replace")
        print(safe, file=stream, flush=flush)


def _print_intent(intent: Intent) -> None:
    """Human-readable one-liner for the learned intent."""
    parts = [f"archetype={intent.archetype}"]
    if intent.themes:
        parts.append(f"themes={','.join(intent.themes)}")
    if intent.tribal_type:
        parts.append(f"tribal={intent.tribal_type}")
    if intent.color_identity:
        parts.append(f"colors={''.join(intent.color_identity)}")
    if intent.key_wincons:
        wc_preview = ", ".join(intent.key_wincons[:3])
        if len(intent.key_wincons) > 3:
            wc_preview += f" +{len(intent.key_wincons) - 3} more"
        parts.append(f"wincons=[{wc_preview}]")
    # R3 F-01: say when free text is in play, so an operator can tell a
    # primer-anchored run from a list-only one.
    if intent.stated:
        parts.append(f"primer={len(intent.stated.split())} words")
    if intent.pilot_preferences:
        parts.append(f"preferences={len(intent.pilot_preferences.split())} words")
    _safe_print(f"[improve] intent: {'; '.join(parts)}", flush=True)


def _print_summary(result: ImproveResult) -> None:
    """Human-readable run summary.

    Every line goes through ``_safe_print``: the per-round line contains
    ``Δ`` (U+0394), which cp1252/cp437 Windows consoles cannot encode —
    a bare ``print`` there crashed the whole run at the very end
    (UnicodeEncodeError) after all the Forge/LLM work had completed.
    """
    _safe_print()
    _safe_print(f"Improve run on {result.deck_id}")
    _safe_print(f"  start deck:  {Path(result.start_deck).name}")
    _safe_print(f"  final deck:  {Path(result.final_deck).name}")
    _safe_print(
        f"  rounds:      {result.rounds_run}/{result.rounds_requested} run, "
        f"{result.rounds_kept} kept"
        + ("  (converged — a round proposed no changes)" if result.converged else "")
    )
    _safe_print()
    for rr in result.history:
        marker = "+" if rr.advanced else " "
        wr = ""
        if rr.win_rate_old is not None and rr.win_rate_new is not None:
            wr = f"  old={rr.win_rate_old:.0%} new={rr.win_rate_new:.0%} (Δ{rr.margin:+d})"
        line = (
            f"  [{marker}] round {rr.round}: {rr.verdict}"
            f"  +{rr.applied_adds}/-{rr.applied_cuts}{wr}"
        )
        # Replication is invisible in the verdict alone: a round that
        # reads 'neutral' here may have been a 'kept' the confirming run
        # refused, and the operator deciding whether to trust the run
        # needs to see which. (None = no confirming run was attempted.)
        if rr.replicated is True:
            line += "  [replicated]"
        elif rr.replicated is False:
            line += (f"  [replication FAILED: run 1 kept, run 2 "
                     f"{rr.replication_verdict} -- not advanced]")
        # The judge's opinion is shown NEXT TO the sim verdict, never
        # merged into it: two instruments, two columns, and the operator
        # gets to see when they disagree. Labeled 'opinion' on purpose —
        # see deck_judge.OPINION_CAVEAT.
        if rr.judge_verdict is not None:
            line += f"  [judge opinion: {rr.judge_verdict}]"
        if rr.iteration_id is not None:
            line += f"  iter#{rr.iteration_id}"
        if rr.error:
            line += f"  ERROR: {rr.error}"
        _safe_print(line)
    _safe_print()
    if result.final_deck != result.start_deck:
        _safe_print(f"Best deck: {result.final_deck}")
    else:
        _safe_print("No round improved the deck; base unchanged.")


# ---------------------------------------------------------------------------
# Bandit strategy (FP-012 slice 2) — treat candidate swaps as arms.
# ---------------------------------------------------------------------------

def _build_arms_from_advice(deck_path: Path, bracket: int, source: str) -> list:
    """Run the advisor once and turn its candidate swaps into bandit arms.

    Each arm is a concrete ``(add, cut)`` swap: the i-th proposed add
    paired with a proposed cut (cuts cycled if fewer than adds). Returns
    an empty list when the advisor proposes no adds.
    """
    from .bandit import Arm
    from .improvement_advisor import advise

    report = advise(deck_path=deck_path, bracket=bracket, source=source)
    manifest = report.to_manifest()
    adds = list(manifest.get("added", []) or [])
    cuts = list(manifest.get("removed", []) or [])
    arms: list = []
    for i, add in enumerate(adds):
        cut = cuts[i % len(cuts)] if cuts else None
        key = f"+{add} / -{cut}" if cut else f"+{add}"
        arms.append(Arm(key=key, add=add, cut=cut))
    return arms


def _signed_margin_reward(wins_a: int, wins_b: int) -> Optional[float]:
    """Normalize an A/B sim outcome onto the bandit's O(1) reward scale.

    Returns the signed decisive margin ``(wins_b - wins_a) / decisive``
    ∈ [-1, +1] (decisive = wins_a + wins_b — filler wins and draws
    carry no information about the swap). The raw margin the evaluator
    used to return is O(±20) at 45-game pulls, which dwarfed UCB1's
    ``c·sqrt(ln N / n)`` bonus (c=1.4 assumes O(1) rewards) and
    Thompson's unit ``obs_var`` — collapsing both policies to
    greedy-on-one-noisy-pull. Normalizing HERE, at the improve.py
    boundary, was chosen over rescaling the exploration constant
    because it matches the codebase's existing convention:
    ``improve_search.margin_reward`` already normalizes to
    ``wins_b / decisive`` ∈ [0, 1], the affine sibling of this map
    ((m + 1) / 2 = wins_b / decisive). The signed form is kept for
    this path so reward signs still read as improvement/regression in
    the CLI summary and JSON, as the raw margin's sign did.

    Returns ``None`` when decisive == 0: there is no head-to-head
    signal to score, and fabricating a 0.0 "tie" would launder
    no-signal into break-even evidence (the caller skips instead).
    """
    decisive = (wins_a or 0) + (wins_b or 0)
    if decisive <= 0:
        return None
    return (wins_b - wins_a) / decisive


def _log_bandit_pull(
    state: dict,
    base: Path,
    candidate: Path,
    proposal,
    ab,
    verdict: str,
    args,
    replication: Optional[dict] = None,
) -> Optional[int]:
    """Write ONE knowledge_log row for an ACCEPTED bandit pull.

    2026-08-20, decision R2-D1. ``--strategy bandit`` used to write zero
    knowledge_log rows while running full ``--sim-games`` A/B sims and
    permanently advancing the deck on disk (``state["deck"] =
    candidate``, the file already written by ``apply_proposal_to_deck``).
    That meant: no lineage, no snapshot, no revert path, and total
    invisibility to the FP-013 gate counter -- for one of three shipped
    strategies, while the CLI told the operator "every improve run grows
    this number". Greedy rounds get their rows for free because they
    subprocess ``auto_curate_main``, which writes; this path calls
    ``advise`` / ``apply_proposal_to_deck`` / ``run_ab_simulation``
    directly, so it has to write its own.

    WHERE THE BOUNDARY IS -- only ACCEPTING pulls are logged. A pull is
    an "iteration" exactly when it CHANGES THE DECK. A pull that sims to
    'neutral' / 'reverted' / 'inconclusive', or whose replication run
    refused it, leaves the deck untouched and writes nothing: it is a
    measurement of a swap that was not adopted, and the log's unit is
    the deck STATE, with ``parent_id`` chaining one state to the next.
    Logging non-advancing pulls would put rows in the chain that no
    later row descends from and hand FP-013 a training set dominated by
    rejected single swaps. The cost of the boundary, stated plainly:
    the Forge games spent on rejected pulls are NOT recorded anywhere
    persistent -- they live in the run's CLI/JSON output only. Revisit
    this if the arm statistics ever need to survive a run.

    The row shape deliberately matches the auto-curate writer
    (``_proposer_sim._log_auto_curate_iteration``) so a bandit pull and
    a curate cycle are the same kind of object to every reader:

      audit_manifest  the SINGLE swap in the standard manifest shape
                      (added / removed / rationale + the drop buckets)
      deck_snapshot   the candidate deck text -- what revert restores
      sim_report      the pull's own A/B report, stamped with the
                      verdict provenance (margin / alpha / decisive
                      floor) the other writers now carry, plus the
                      confirming run under SIM_REPORT_REPLICATION_KEY
                      when ``--replicate`` ran one
      verdict         the pull's verdict (an accepted pull is 'kept')
      parent_id       the previous row for this deck, so successive
                      pulls in one run form a chain

    ``measurement_era`` is NOT set here on purpose: ``Iteration.to_row``
    derives it from the row's own ``created_at`` through the single
    ``measurement_era_for`` definition, so this writer stamps the era
    correctly without knowing the boundaries exist.

    Failures are non-fatal and warn on stderr, matching every other
    knowledge_log writer in this pipeline: the .dck is already on disk
    and the pull's decision is already made, so losing the row must not
    sink a multi-hour run. Returns the new row id, or None.
    """
    from ._proposer_sim import (
        MIN_DECISIVE_GAMES_FOR_VERDICT,
        VERDICT_ALPHA,
        _ab_to_iteration_fields,
    )
    from .deck_identity import resolve_deck_id, stable_deck_stem
    from .knowledge_log import (
        SIM_REPORT_REPLICATION_KEY,
        SIM_REPORT_VERDICT_PARAMS_KEY,
        Iteration,
        iterations_for_deck,
        record_iteration,
        verdict_provenance,
    )

    # None, NOT DEFAULT_DB_PATH: knowledge_log resolves a None db_path
    # against the module attribute at CALL time, which is what lets the
    # test suite's autouse isolation fixture redirect it. Freezing the
    # constant here would silently write to the production log.
    db_path = Path(args.db_path) if getattr(args, "db_path", None) else None
    # The fallback is the VERSION-STRIPPED stem (2026-09-03, R3 C-08):
    # the raw ``candidate.stem`` gains a version suffix on every accepted
    # pull, so a publicId-less deck was keyed as a different deck per
    # pull and every per-deck knowledge_log surface saw one-row decks.
    deck_id = resolve_deck_id(
        candidate, fallback=stable_deck_stem(candidate.name),
    )

    # Chain within the run FIRST, then fall back to the deck's history.
    # ``iterations_for_deck`` is enough now that the id is stable across
    # versions, but carrying the id in ``state`` (the same dict that
    # carries the advancing deck) keeps the chain exact within one run
    # even if two runs interleave on the same deck.
    parent_id = state.get("last_iteration_id")
    if parent_id is None:
        prior = iterations_for_deck(deck_id, db_path=db_path)
        parent_id = prior[-1].id if prior else None

    sim_fields = _ab_to_iteration_fields(ab)
    report = dict(sim_fields.get("sim_report") or {})
    report[SIM_REPORT_VERDICT_PARAMS_KEY] = verdict_provenance(
        margin=args.sim_margin,
        alpha=VERDICT_ALPHA,
        min_decisive=MIN_DECISIVE_GAMES_FOR_VERDICT,
    )
    if replication is not None:
        report[SIM_REPORT_REPLICATION_KEY] = replication

    adds = list(proposal.applied_adds)
    cuts = list(proposal.applied_cuts)
    manifest = {
        "added": adds,
        "removed": cuts,
        "rationale": (
            f"bandit pull ({getattr(args, 'bandit_policy', 'ucb1')}): "
            f"{'+' + adds[0] if adds else ''}"
            f"{' / ' if adds and cuts else ''}"
            f"{'-' + cuts[0] if cuts else ''}"
            f" — accepted on a '{verdict}' A/B verdict over "
            f"{getattr(ab, 'games', 0)} pod games"
            + ("; confirmed by a second independent A/B"
               if replication is not None and replication.get("confirmed")
               else "")
        ),
        "source": "bandit-pull",
        # Same refusal buckets the auto-curate manifest carries, so a
        # reader can tell "the pull proposed nothing" from "the guards
        # refused what it proposed" on either writer's rows.
        "dropped_for_bracket": list(proposal.dropped_for_bracket),
        "dropped_for_protection": list(proposal.dropped_for_protection),
        "dropped_for_politics": list(proposal.dropped_for_politics),
        "dropped_for_color_identity": list(proposal.dropped_for_color_identity),
        "dropped_for_balance": list(proposal.dropped_for_balance),
        "dropped_unmatched_cut": list(proposal.dropped_unmatched_cut),
        "dropped_duplicate_add": list(proposal.dropped_duplicate_add),
        "dropped_commander_add": list(proposal.dropped_commander_add),
        "padded_count": proposal.padded_count,
        "padded_breakdown": dict(proposal.padded_breakdown),
        "requested_adds": list(proposal.adds),
        "requested_cuts": list(proposal.cuts),
        "src_deck": base.name,
    }

    it = Iteration(
        deck_id=deck_id,
        deck_name=candidate.stem,
        bracket=args.bracket,
        parent_id=parent_id,
        audit_version="bandit-pull",
        audit_manifest=manifest,
        sim_report=report,
        verdict=verdict,
        verdict_notes=(
            f"bandit pull: A/B sim old won {getattr(ab, 'wins_a', None)}, "
            f"new won {getattr(ab, 'wins_b', None)} over "
            f"{getattr(ab, 'games', 0)} games "
            f"(margin={args.sim_margin})"
            + ("; replication_confirmed"
               if replication is not None and replication.get("confirmed")
               else "")
        ),
        win_rate_old=sim_fields.get("win_rate_old"),
        win_rate_new=sim_fields.get("win_rate_new"),
        margin=sim_fields.get("margin"),
        deck_snapshot=candidate.read_text(encoding="utf-8"),
    )
    iteration_id = record_iteration(it, db_path=db_path)
    state["last_iteration_id"] = iteration_id
    # Also kept as a list so the run summary / JSON can hand the operator
    # the row ids. A revert path nobody can find is not a revert path.
    state.setdefault("logged_iteration_ids", []).append(iteration_id)
    return iteration_id


def _make_swap_evaluator(state: dict, args):
    """Build the real per-arm evaluator: apply one swap to the current
    best deck, A/B-sim it, and return a ``bandit.PullOutcome`` whose
    reward is the NORMALIZED signed decisive margin ∈ [-1, +1] (see
    ``_signed_margin_reward``). The candidate becomes the new base
    (greedy accept) only when ``_proposer_sim._verdict_from_ab`` calls
    the split ``kept`` — a statistically significant win (exact
    two-sided binomial at VERDICT_ALPHA) over at least the standard
    decisive-games gate, with ``--sim-margin`` retained as the
    back-compat minimum-margin pre-filter. A raw one-win margin can no
    longer advance the base (pre-2026-08-16 bug: ``reward >=
    sim_margin`` with the default margin of 1 accepted any 23-22
    coin-flip split).

    ``state`` is a mutable ``{"deck": Path}`` the closure advances (it
    also carries ``"last_iteration_id"`` once a pull has been logged —
    see below). Never raises — but failures are NOT rewards: apply
    errors, missing fillers, incomplete sims, and zero-decisive runs
    return ``PullOutcome.skip(reason)`` (logged to stderr), which
    ``run_bandit`` records via the skip API without touching the arm's
    pull count or mean. The old behavior mapped every failure to a 0.0
    reward, entering crashed sims into the bandit's statistics as
    measured ties.

    Skip reasons produced here are the vocabulary
    ``bandit.SKIP_REASON_CLASSES`` classifies (2026-08-20, R2-D6). Only
    ``swap_dropped_by_legality`` is STRUCTURAL and retires the arm; the
    rest (``apply_failed``, ``fillers_unavailable``, ``sim_*``,
    ``zero_decisive_games``) are TRANSIENT and leave the arm in play.
    Adding a NEW skip reason here means adding it to that table — an
    unclassified reason warns loudly and defaults to transient.

    Knowledge log (2026-08-20, decision R2-D1): an ACCEPTED pull writes
    one knowledge_log row via ``_log_bandit_pull`` — manifest, deck
    snapshot, sim report, verdict, parent chain — because an accepted
    pull permanently advances the deck on disk and used to do so with no
    record at all. Non-advancing pulls write nothing; the boundary and
    its cost are argued in ``_log_bandit_pull``'s docstring.

    Replication (2026-08-17): OFF by default on this path (see
    ``resolve_replicate_default``) but honored when the operator asks for
    it with ``--replicate``. When on, a ``kept`` pull triggers one
    confirming A/B of the same base-vs-candidate pairing and the base
    advances only if that run is ``kept`` too; a disagreeing run leaves
    ``accepted=False`` and reports the SECOND run's verdict, so the arm's
    outcome reflects the non-advance. The confirm run is a bare sim
    (``_run_confirm_sim``) and can never recurse into another pull.

    The pull's REWARD stays run 1's measurement on BOTH branches: one
    pull is one budget unit, and folding a second sim into the mean
    would silently double-weight exactly the arms that reached the gate.
    Reviewed and RE-CONFIRMED 2026-08-20 as decision R2-D4 (recorded in
    the ANALYSIS repo: mtga-advisor's
    ``docs/ollama-analysis/DECISIONS_FOR_REVIEW.md`` — not this repo's
    docs/) against the
    counter-argument that this preserves the winner's curse rather than
    correcting it — run 1 triggered the gate by being extreme, so run 2
    is the unbiased draw and discarding it keeps the arm mean inflated.
    That reading is accepted as a real asymmetry and is documented
    rather than fixed: the owner's budget-unit rationale stands, the
    path is non-default (``--replicate`` is OFF for ``--strategy
    bandit``), and the confirming run is preserved as DATA under
    ``sim_report[SIM_REPORT_REPLICATION_KEY]`` on the logged row, so a
    later re-analysis can pool both runs even though the live arm mean
    does not.
    """
    from .proposer import Proposal, apply_proposal_to_deck
    from .forge_runner import run_ab_simulation
    from ._proposer_sim import _pick_filler_decks, _verdict_from_ab
    from .bandit import PullOutcome

    def _skip(arm, reason: str) -> PullOutcome:
        print(f"[bandit] pull on {arm.key} skipped ({reason}); "
              f"arm statistics unchanged.", file=sys.stderr, flush=True)
        return PullOutcome.skip(reason)

    def evaluate(arm) -> PullOutcome:
        base = state["deck"]
        proposal = Proposal(
            adds=[arm.add] if arm.add else [],
            cuts=[arm.cut] if arm.cut else [],
        )
        try:
            candidate = apply_proposal_to_deck(base, proposal, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            return _skip(arm, f"apply_failed: {type(exc).__name__}: {exc}")
        if not proposal.applied_adds and not proposal.applied_cuts:
            # Pair validation dropped the WHOLE swap (cut not in the
            # decklist / add already present / add is the commander), so
            # the candidate is content-identical to the base and simming
            # it would spend a full Forge budget measuring base-vs-base
            # noise -- booked as this arm's real reward, and ~1 no-op run
            # in 83 even clears the significance gate and "advances" the
            # base to a restamped copy of itself. Same guard
            # improve_search.py's probe evaluator and iteration_loop
            # already had; this path was the one that missed it
            # (2026-08-20). Routine here rather than exotic:
            # _build_arms_from_advice cycles cuts across arms, so once
            # any pull accepts and removes that cut from the base, every
            # sibling arm sharing it becomes a guaranteed no-op.
            return _skip(arm, "swap_dropped_by_legality")
        deck_dir = base.parent
        if args.sim_fillers:
            fillers = [f.strip() for f in args.sim_fillers.split(",") if f.strip()]
        else:
            fillers = _pick_filler_decks(
                deck_dir, exclude_paths=[base, candidate], count=2,
                target_bracket=args.bracket,
            )
        if len(fillers) < 2:
            return _skip(arm, f"fillers_unavailable: need 2 for a "
                              f"4-player pod, found {len(fillers)}")
        ab = run_ab_simulation(
            deck_a_path=base, deck_b_path=candidate,
            games=args.sim_games, fillers=fillers,
        )
        status = getattr(ab, "status", None)
        if status != "done":
            err = getattr(ab, "error", None)
            return _skip(arm, f"sim_{status or 'unknown'}"
                              + (f": {err}" if err else ""))
        reward = _signed_margin_reward(ab.wins_a or 0, ab.wins_b or 0)
        if reward is None:
            return _skip(arm, "zero_decisive_games")
        # Accept through the SAME significance machinery every other
        # verdict path uses (--sim-margin stays a pre-filter inside it).
        verdict = _verdict_from_ab(ab, margin=args.sim_margin)
        accepted = verdict == "kept"
        # Structured record of the confirming run, for the logged row's
        # sim_report. None when no confirm was attempted (replication off
        # or the pull wasn't 'kept') -- distinct from "attempted and
        # failed", which is ``{"ran": False, ...}``.
        replication: Optional[dict] = None
        if accepted and bool(getattr(args, "replicate", False)):
            confirm_ab, _f, err = _run_confirm_sim(base, candidate, args)
            # 'inconclusive' when the confirm couldn't RUN, matching
            # _default_replicate_fn (R2-D3, 2026-08-20): run 1 completed,
            # so 'pending' ("the sim didn't complete") would misdescribe
            # the pull. Either way it isn't 'kept', so the base stays put.
            # R3 C-02 (2026-09-03): "couldn't run" includes a confirm
            # that started and came back failed/skipped — same predicate
            # as the round writer, so a crashed JVM here no longer prints
            # "run 2 pending".
            if _confirm_sim_completed(confirm_ab):
                confirm_verdict = _verdict_from_ab(
                    confirm_ab, margin=args.sim_margin,
                )
            else:
                confirm_verdict = "inconclusive"
                err = err or _confirm_sim_error(confirm_ab)
            if confirm_verdict != "kept":
                # Non-advance, recorded as such: the arm keeps run 1's
                # reward (a real measurement) but reports the second
                # run's verdict, so nothing downstream reads this pull
                # as an adopted swap. NOT logged to knowledge_log -- the
                # deck did not change, so there is no new iteration
                # (R2-D1 boundary, argued in _log_bandit_pull).
                print(f"[bandit] replication failed on {arm.key}: run 1 "
                      f"kept, run 2 {confirm_verdict}"
                      + (f" ({err})" if err else "")
                      + "; base NOT advanced.",
                      file=sys.stderr, flush=True)
                return PullOutcome(reward=reward, accepted=False,
                                   verdict=confirm_verdict)
            replication = {
                "ran": confirm_ab is not None,
                "verdict": confirm_verdict,
                "confirmed": True,
                "wins_old": getattr(confirm_ab, "wins_a", None),
                "wins_new": getattr(confirm_ab, "wins_b", None),
                "games": getattr(confirm_ab, "games", None),
                "status": getattr(confirm_ab, "status", None),
            }
            print(f"[bandit] replication confirmed on {arm.key}: two "
                  f"independent A/Bs both 'kept'; base advanced.",
                  file=sys.stderr, flush=True)
        if accepted:
            state["deck"] = candidate  # advance the base deck
            # R2-D1: the deck just moved on disk, so this pull IS an
            # iteration -- record it. Non-fatal: a knowledge_log failure
            # must not sink a multi-hour run whose .dck is already
            # written and whose decision is already made.
            try:
                _log_bandit_pull(state, base, candidate, proposal, ab,
                                 verdict, args, replication)
            except Exception as exc:  # noqa: BLE001 — history loss, not a crash
                print(f"[bandit] WARN: could not log the accepted pull on "
                      f"{arm.key} to the knowledge log: "
                      f"{type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
        return PullOutcome(reward=reward, accepted=accepted, verdict=verdict)

    return evaluate


def _print_bandit_summary(deck_id: str, result, final_deck: Path,
                          logged_ids: Optional[list] = None) -> None:
    # _safe_print throughout: arm keys embed card names, which can carry
    # characters a cp1252/cp437 console can't encode (same failure mode
    # as the Δ in _print_summary).
    _safe_print()
    skip_note = (f", {result.skipped} skipped"
                 if getattr(result, "skipped", 0) else "")
    _safe_print(f"Bandit improve run on {deck_id} ({result.rounds_run} pulls, "
                f"{result.accepted} accepted{skip_note})")
    _safe_print(f"  best swap:  {result.best_arm_key} (mean reward "
                f"{result.best_arm_mean:+.2f})")
    _safe_print(f"  final deck: {final_deck.name}")
    # R2-D1: accepted pulls advance the deck on disk, so each one is now
    # a knowledge_log row. Print the ids -- a revert path the operator
    # can't find is not a revert path.
    if logged_ids:
        _safe_print(f"  logged:     iteration rows "
                    f"{', '.join('#' + str(i) for i in logged_ids)} "
                    f"(commander-history / commander-revert)")
    elif result.accepted:
        _safe_print("  logged:     none -- the accepted pull(s) could not "
                    "be written to the knowledge log (see the warnings "
                    "above)")
    _safe_print()
    _safe_print("  Arm stats (by mean reward; normalized margin in [-1, 1]):")
    for a in result.arm_stats:
        # R2-D6: "retired" and "skipped" are different facts now. An arm
        # can carry skips and still be live (transient failures only), so
        # printing every skipped arm as dead would misreport the run.
        note = ""
        if a.get("skips"):
            note = (f"  [{a['skips']} skip(s): "
                    f"{a.get('skip_reason') or 'no signal'}"
                    + ("; RETIRED (structural)" if a.get("retired")
                       else "; still live (transient)")
                    + "]")
        if a["pulls"]:
            _safe_print(f"    {a['mean']:+.2f}  ({a['pulls']}x)  "
                        f"{a['key']}{note}")
        elif note:
            _safe_print(f"    no measurement  {a['key']}{note}")


def _run_bandit_strategy(deck_path: Path, deck_id: str, args) -> int:
    """Drive the bandit search: build arms from the advisor, then pull
    swaps via the chosen policy, advancing the base deck on improvement."""
    import random
    from .bandit import make_policy, run_bandit

    arms = _build_arms_from_advice(deck_path, args.bracket, args.source)
    if not arms:
        msg = "no candidate swaps from the advisor; nothing to search."
        print(json.dumps({"error": msg}) if args.json else f"[improve] {msg}",
              flush=True)
        return 0

    policy = make_policy(args.bandit_policy, epsilon=args.epsilon, c=args.ucb_c)
    state = {"deck": deck_path}
    evaluate = _make_swap_evaluator(state, args)
    # accept_threshold is deliberately NOT passed (2026-08-20). The real
    # evaluator returns PullOutcome objects, so acceptance is decided by
    # _verdict_from_ab (significance + decisive gate + --sim-margin
    # pre-filter) inside the evaluator and the threshold is never
    # consulted on this path at all -- it only coerces bare-float
    # evaluators (the back-compat/test path). What used to be here was
    # `accept_threshold=args.sim_margin`: a UNITS ERROR, and the exact
    # class bandit.py's own docstring lectures about. --sim-margin is a
    # raw decisive-game margin (default 1, O(±20) at 45-game pulls) while
    # rewards were normalized to [-1, +1] in 2026-08-16, so the
    # comparison `reward >= 1` meant "accept only a clean sweep", and any
    # raised --sim-margin made acceptance arithmetically impossible.
    # Dropping the kwarg rather than substituting a normalized constant:
    # improve.py has no opinion to express about a path it cannot reach,
    # and inventing one here would put a second, silent acceptance rule
    # beside the significance test that actually governs this strategy.
    result = run_bandit(
        arms, args.rounds, evaluate, policy, rng=random.Random(),
    )

    logged_ids = state.get("logged_iteration_ids") or []
    if args.json:
        out = result.to_dict()
        out["deck_id"] = deck_id
        out["final_deck"] = str(state["deck"])
        # R2-D1: the knowledge_log rows this run created, in pull order.
        # Empty when no pull advanced the deck -- which is the common
        # outcome and NOT an error (see _log_bandit_pull's boundary).
        out["iteration_ids"] = logged_ids
        print(json.dumps(out, indent=2))
    else:
        _print_bandit_summary(deck_id, result, state["deck"], logged_ids)
    return 0


def resolve_replicate_default(args) -> bool:
    """Resolve the tri-state ``--replicate`` / ``--no-replicate`` flag.

    An explicit flag always wins. Left unset (``None``), the default
    keys on the run's SHAPE, because the two entry points differ in
    exactly the way the owner's decision cares about:

      - the round loop (``--strategy greedy``, with or without
        ``--search-budget``) is the UNATTENDED one: it runs for hours,
        CHAINS its rounds -- each advance becomes the base every later
        round is measured against -- and nobody is watching to sanity-
        check a lucky split. A false 'kept' there compounds. Default ON.
      - ``--strategy bandit`` is the interactive single-swap explorer:
        one swap per pull, the operator reading arm stats as they land,
        and UCB1 already RE-PULLS promising arms, so the policy supplies
        its own repeat measurements. Doubling every accepting pull's
        Forge bill to re-litigate a decision the bandit revisits anyway
        is cost without much information. Default OFF.

    Returns the effective boolean; ``improve_main`` writes it back onto
    ``args`` so ``run_improve_loop`` / the evaluator see one resolved
    value rather than re-deriving policy.
    """
    explicit = getattr(args, "replicate", None)
    if explicit is not None:
        return bool(explicit)
    return getattr(args, "strategy", "greedy") != "bandit"


def improve_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``commander-improve``.

    Greedy single-deck improve loop. Resolves the deck (by ``--deck
    <id>`` against the Commander deck dir, or a positional ``.dck``
    path), infers the bracket from the filename when ``--bracket`` is
    omitted, then runs ``--rounds N`` auto-curate rounds keeping only
    rounds whose A/B sim says the new deck won.
    """
    p = argparse.ArgumentParser(
        prog="commander-improve",
        description=(
            "Single-deck improvement loop (FP-012). --strategy greedy "
            "(slice 1, default) runs auto-curate for N rounds, advancing "
            "only on a 'kept' A/B sim verdict. --strategy bandit (slice 2) "
            "treats candidate swaps as arms and learns which move the win "
            "rate via an epsilon-greedy / UCB1 policy."
        ),
    )
    p.add_argument("deck_path", type=Path, nargs="?", default=None,
                   help="Path to the .dck file. Omit when using --deck.")
    p.add_argument("--deck", dest="deck_id", default=None, metavar="ID",
                   help="Deck id (filename stem) resolved against --deck-dir. "
                        "Use this OR a positional path, not both.")
    p.add_argument("--deck-dir", type=Path, default=DEFAULT_DECK_DIR,
                   help=f"Directory --deck ids resolve against "
                        f"(default: {DEFAULT_DECK_DIR}).")
    p.add_argument("--rounds", type=int, default=None,
                   help="Number of improve rounds to attempt (>= 1). The "
                        "loop stops early if a round proposes no changes. "
                        "Required unless --health is given.")
    p.add_argument("--health", action="store_true",
                   help="Report FP-013 gate progress (high-confidence "
                        "curator iterations in the knowledge_log toward "
                        "the 1,000 needed to unpark the project-tuned "
                        "LLM) and exit. Needs no deck or --rounds.")
    p.add_argument("--bracket", type=int, default=None,
                   help="Target bracket (1-5). Default: inferred from the "
                        "deck filename's [B<n>] suffix.")
    # Pass-through curation / sim controls (mirror commander-auto-curate).
    p.add_argument("--mode",
                   choices=["polish", "overhaul", "free", "rebuild", "auto"],
                   default="polish",
                   help="Curation intensity (default polish). 'auto' "
                        "resolves the tier from the deck's health score "
                        "each round (opt-in — escalated budgets multiply "
                        "curator + Forge cost); 'rebuild' is 30+30 with "
                        "an optional Karsten manabase rebuild. Both are "
                        "forwarded to commander-auto-curate per round.")
    p.add_argument("--no-manabase-rebuild", action="store_true",
                   help="Forwarded to commander-auto-curate: skip the "
                        "Karsten manabase-rebuild step in the rebuild "
                        "tier. No effect in other modes.")
    p.add_argument("--source", default="heuristic",
                   choices=["heuristic", "bracket_peers", "claude"],
                   help="Advisor backend (default heuristic).")
    p.add_argument("--model", default="claude-sonnet-4-5",
                   help="Anthropic model id for the curator step.")
    # Default 45, NOT 5 or 25. UNITS: --sim-games is TOTAL 4-player-pod
    # games, but the verdict gate (MIN_DECISIVE_GAMES_FOR_VERDICT = 20)
    # counts DECISIVE games = wins_a + wins_b -- head-to-head wins only.
    # The two filler seats win roughly half the pod games (see
    # EXPECTED_DECISIVE_FRACTION), so the previous default of 25 total
    # yielded only ~12-13 decisive: still below the gate, still ALWAYS
    # 'inconclusive' -- and the greedy loop advances only on 'kept', so
    # improve stayed structurally unable to move the base deck, now at
    # 5x the Forge cost of the old 5-game default. (The old comment
    # blamed "headroom for a few draws" -- wrong drain: filler WINS, not
    # draws, eat the other half of the games.) 45 total -> ~22 expected
    # decisive: clears the 20-decisive gate with headroom for filler
    # variance, and sits in-family with the operator's standard 40-game
    # soak convention.
    p.add_argument("--sim-games", type=int, default=45,
                   help="TOTAL 4-player pod games per A/B sim each "
                        "round (default 45). Verdicts need >= 20 "
                        "DECISIVE games -- games won by the old or new "
                        "deck itself; the 2 filler seats win ~half the "
                        "pod games, so expect decisive ~= total/2 (45 "
                        "total ~= 22 decisive). Below ~40 total the "
                        "verdict is likely 'inconclusive' and the round "
                        "cannot advance the deck. NOTE the runtime "
                        "cost: 45 Forge pod games per round is ~9x the "
                        "old 5-game default -- budget a couple of "
                        "hours per round, not minutes.")
    p.add_argument("--sim-margin", type=int, default=1,
                   help="Minimum |wins_new - wins_old| PRE-FILTER for a "
                        "kept/reverted call (default 1). The verdict "
                        "additionally requires statistical significance "
                        "(exact two-sided binomial test — see "
                        "_proposer_sim._verdict_from_ab); at the default "
                        "of 1 the significance test is strictly stricter, "
                        "so raise this only to demand a LARGER minimum "
                        "effect than significance alone.")
    p.add_argument("--sim-fillers", default=None,
                   help="Comma-separated filler .dck filenames for the pod. "
                        "Default: auto-pick 2 bracket-matched opponents.")
    # Tri-state: None = "use the shape-dependent default" (see
    # resolve_replicate_default). argparse's store_true/store_false pair
    # on one dest is this CLI's existing idiom for an on/off flag whose
    # default isn't a constant (cf. --sim-games' None sentinel in
    # _proposer_cli).
    p.add_argument("--replicate", dest="replicate", action="store_true",
                   default=None,
                   help="Require a SECOND independent A/B (same old-vs-new "
                        "pairing, fresh sim) to also say 'kept' before the "
                        "base deck advances. Default ON for the unattended "
                        "round loop (greedy / --search-budget), OFF for "
                        "--strategy bandit. COST: a confirmed swap runs two "
                        "sims, i.e. ~2x the Forge time of an advancing "
                        "round. Buys a ~40x lower per-advance "
                        "false-positive rate (~1 in 1,600) on a loop that "
                        "compounds its own mistakes. THE OTHER HALF OF THE "
                        "TRADE: it is a screen, not an improver. A "
                        "genuinely good +5pp swap advances with p=0.13%% "
                        "per round, ~1.3%% over a 10-round run, so "
                        "'nothing advanced' is the EXPECTED overnight "
                        "outcome; an advance is a rare lead worth reading, "
                        "not a progress bar.")
    p.add_argument("--no-replicate", dest="replicate", action="store_false",
                   help="Single-shot: one significance-passing 'kept' "
                        "advances the base, no confirming run (the "
                        "pre-2026-08-17 behaviour). This is already the "
                        "default for --strategy bandit.")
    p.add_argument("--db-path", default=None,
                   help="Override the knowledge_log SQLite path.")
    p.add_argument("--protect", action="append", default=[], metavar="CARD",
                   help="Lock a card against cuts. Repeatable.")
    p.add_argument("--protect-from", default=None, metavar="PATH",
                   help="File of card names (one per line) protected against cuts.")
    p.add_argument("--json", action="store_true",
                   help="Emit the run result as JSON instead of a summary.")
    # Strategy: greedy (slice 1, default) curates a full proposal per
    # round and keeps it if better; bandit (slice 2) treats individual
    # candidate swaps as arms and learns which ones move the win rate.
    p.add_argument("--strategy", choices=["greedy", "bandit"], default="greedy",
                   help="Search strategy (default greedy). 'bandit' selects "
                        "individual swaps via a multi-armed-bandit policy. "
                        "KNOWLEDGE LOG: greedy writes one row per round "
                        "(via the auto-curate subprocess) whatever the "
                        "verdict; bandit writes one row per ACCEPTED pull "
                        "only -- a pull that does not advance the deck is "
                        "not an iteration and is left in the run's "
                        "CLI/JSON output, not the log.")
    p.add_argument("--bandit-policy", choices=["epsilon_greedy", "ucb1", "thompson"],
                   default="ucb1",
                   help="Bandit arm-selection policy (default ucb1). Only "
                        "used with --strategy bandit. 'thompson' uses "
                        "Thompson sampling (Gaussian posterior per arm).")
    p.add_argument("--epsilon", type=float, default=0.2,
                   help="Exploration rate for --bandit-policy epsilon_greedy "
                        "(default 0.2).")
    p.add_argument("--ucb-c", type=float, default=1.4,
                   help="Exploration constant for --bandit-policy ucb1 "
                        "(default 1.4). Also used by --search-budget's "
                        "UCB1 policy.")
    # Budget-bounded swap search (FP-012 full slice). 0 = disabled, and
    # the greedy path is then byte-identical to pre-slice behavior
    # (pinned by test: the search module is never even imported).
    p.add_argument("--search-budget", type=int, default=0, metavar="N",
                   help="Total probe sims per round for the UCB1 swap "
                        "search (default 0 = disabled; plain greedy "
                        "rounds). When set, each round builds swap arms "
                        "from the advisor's OFFLINE candidate pool (no "
                        "Claude in the inner loop), spends N single-swap "
                        "A/B sims learning which swaps move the win "
                        "rate, applies the best arm(s) with at least "
                        "--search-min-pulls observations, then runs the "
                        "normal keep-if-better verdict sim. HONEST COST: "
                        "each pull is a FULL A/B sim of --sim-games pod "
                        "games, so a round costs ~(N+1) x sim-games "
                        "Forge games (~(N+1) x 10+ min at the 45-game "
                        "default). Incompatible with --strategy bandit.")
    p.add_argument("--search-min-pulls", type=int, default=2, metavar="K",
                   help="Minimum pulls (independent probe sims) an arm "
                        "needs before it may be applied (default 2 -- a "
                        "single ~22-decisive-game sim has a ~0.1 win-rate "
                        "standard error, so one lucky pull is not "
                        "evidence). Only used with --search-budget.")
    # forge_py screening gate (FP-012 x forge_py). SCREEN, NOT JUDGE:
    # forge_py only decides which arms get Forge time; Forge remains
    # the only verdict engine. Default OFF -> byte-identical rounds.
    p.add_argument("--screen", action="store_true",
                   help="forge_py pre-SCREEN of the --search-budget arm "
                        "pool: before the bandit spends ANY Forge games, "
                        "goldfish each candidate swap in forge_py (cheap "
                        "in-process Python sims, measured at r~0.898 "
                        "rank correlation with real Forge outcomes) and "
                        "prune the weakest arms. SCREEN, NOT JUDGE: "
                        "Forge remains the only verdict engine; the "
                        "screen merely decides which arms deserve Forge "
                        "time. Pruned arms are logged with their screen "
                        "scores; missing/broken forge_py degrades "
                        "loudly to no screening. Also enabled by "
                        "COMMANDER_BUILDER_FORGEPY_SCREEN=1. Requires "
                        "--search-budget.")
    p.add_argument("--screen-keep", type=float, default=0.5, metavar="FRAC",
                   help="Fraction of measured arms the screen keeps "
                        "(default 0.5 = keep the top half), floor of 2 "
                        "arms overall; arms the screen could not "
                        "measure are always kept. Only used with "
                        "--screen.")
    p.add_argument("--screen-games", type=int, default=20, metavar="G",
                   help="forge_py games per candidate during screening "
                        "(default 20). These are cheap Python sims, "
                        "NOT Forge JVM games.")
    # Intent learning (FP-012 Slice A).
    p.add_argument("--learn-intent", dest="learn_intent_path",
                   type=Path, default=None, metavar="DCK",
                   help="Path to a .dck file whose intent (archetype, themes, "
                        "key win-cons, and its <stem>.primer.md sidecar as "
                        "the stated free text) is learned before the improve "
                        "loop starts. The intent's key win-cons are added to "
                        "the protected-card list (auto-protect) and its "
                        "themes serve as a soft bias on candidate adds. "
                        "Intent is advisory: the win-margin objective remains "
                        "primary.")
    # R3 F-01 (2026-09-03): the pilot's own words had no CLI path.
    p.add_argument("--preferences", default=None, metavar="TEXT",
                   help="The pilot's own words about what they like doing "
                        "(free text, read as AFFIRMATIVE keywords — a negated "
                        "mention such as 'no tokens' contributes nothing). "
                        "Implies --learn-intent on the deck being improved "
                        "when --learn-intent is not given.")
    p.add_argument("--preferences-file", default=None, metavar="PATH",
                   help="Read --preferences text from a file instead.")
    args = p.parse_args(argv)

    # --health short-circuits: report the FP-013 gate counter and exit.
    # Surfaced here because this is where the qualifying data gets
    # generated. What actually grows the number (corrected 2026-08-20,
    # R2-P02 / decision R2-D1 -- this comment used to claim "every
    # improve run grows this number", which was flatly false for
    # --strategy bandit, then writing zero rows at all):
    #
    #   --strategy greedy   one row per round, written by the auto-curate
    #                       subprocess. It counts toward the gate once it
    #                       has a decided verdict, a manifest and a
    #                       >= FP013_MIN_GAMES sim report.
    #   --strategy bandit   one row per ACCEPTED pull (_log_bandit_pull).
    #                       Pulls that don't advance the deck write
    #                       nothing, so a run that accepts nothing --
    #                       the common outcome -- grows this by zero.
    #
    # So: runs that CHANGE a deck grow this number. Runs that conclude
    # "no change was justified" mostly don't, and that is the honest
    # reading of both the gate and the loop (see R2-D2 in the module
    # docstring).
    if args.health:
        from .knowledge_log import (
            DEFAULT_DB_PATH,
            FP013_RELABELABLE_ERA,
            fp013_gate_progress,
        )
        db_path = Path(args.db_path) if args.db_path else DEFAULT_DB_PATH
        progress = fp013_gate_progress(db_path=db_path)
        if args.json:
            print(json.dumps(progress), flush=True)
        else:
            print(
                f"High-confidence curator iterations: "
                f"{progress['count']} / {progress['target']} "
                f"({progress['pct']}%) toward FP-013 "
                f"(>= {progress['min_games']}-game decided verdicts "
                f"with an audit manifest, measurement era "
                f">= {progress['min_era']})",
                flush=True,
            )
            # Disclose what the era floor held back, so the headline
            # number never reads as "that's all the history there is".
            if progress["relabelable"]:
                print(
                    f"  + {progress['relabelable']} era-"
                    f"{FP013_RELABELABLE_ERA} rows are recoverable: "
                    f"sound measurement, but their verdicts came from "
                    f"the old margin threshold. Re-scoring their stored "
                    f"sim reports with the current significance test "
                    f"would promote them.",
                    flush=True,
                )
            if progress["excluded_by_era"]:
                print(
                    f"  - {progress['excluded_by_era']} rows excluded: "
                    f"labels from a superseded measurement regime, or "
                    f"unstamped provenance. Archive only.",
                    flush=True,
                )
        return 0

    # Exactly one of {positional path, --deck id} must be supplied.
    if (args.deck_path is None) == (args.deck_id is None):
        print("ERROR: pass either a deck_path positional OR --deck <id>, "
              "not both / neither.", flush=True)
        return 2

    if args.rounds is None:
        print("ERROR: --rounds is required (unless --health).", flush=True)
        return 2

    if args.rounds < 1:
        print(f"ERROR: --rounds must be >= 1, got {args.rounds}", flush=True)
        return 2

    # --search-budget validation, all before any Forge/LLM/deck work.
    if args.search_budget < 0:
        print(f"ERROR: --search-budget must be >= 0, got "
              f"{args.search_budget}", flush=True)
        return 2
    if args.search_budget and args.strategy == "bandit":
        # Two different searches over the same swap space in one run
        # makes no sense: --strategy bandit REPLACES the round loop,
        # --search-budget runs INSIDE it. Refuse rather than pick one.
        print("ERROR: --search-budget runs inside the greedy round loop "
              "and is incompatible with --strategy bandit; drop one.",
              flush=True)
        return 2
    if args.search_budget:
        if args.search_min_pulls < 1:
            print(f"ERROR: --search-min-pulls must be >= 1, got "
                  f"{args.search_min_pulls}", flush=True)
            return 2
        if args.search_budget < args.search_min_pulls:
            # Structurally useless: no arm could ever accumulate
            # min_pulls observations, so every round would burn the
            # whole budget and then apply nothing. Refuse up front
            # instead of wasting hours of Forge time on a no-op.
            print(f"ERROR: --search-budget {args.search_budget} < "
                  f"--search-min-pulls {args.search_min_pulls}: no arm "
                  f"could ever qualify, every round would be a no-op. "
                  f"Raise the budget or lower min-pulls.", flush=True)
            return 2

    # --screen validation, before any Forge/forge_py/deck work.
    if args.screen and not args.search_budget:
        # The screen prunes the bandit's ARM POOL; without a search
        # there is no pool to prune. Refuse rather than silently no-op.
        print("ERROR: --screen is the forge_py pre-filter for the "
              "--search-budget arm pool; pass --search-budget N too.",
              flush=True)
        return 2
    if args.screen:
        if not (0.0 < args.screen_keep <= 1.0):
            print(f"ERROR: --screen-keep must be in (0, 1], got "
                  f"{args.screen_keep}", flush=True)
            return 2
        if args.screen_games < 1:
            print(f"ERROR: --screen-games must be >= 1, got "
                  f"{args.screen_games}", flush=True)
            return 2

    # Resolve the deck to an on-disk .dck.
    from .web._helpers import _bracket_from_filename, _resolve_deck_path
    if args.deck_path is not None:
        deck_path = args.deck_path.resolve()
        if not deck_path.exists():
            print(f"ERROR: deck not found: {deck_path}", flush=True)
            return 2
    else:
        deck_path = _resolve_deck_path(args.deck_dir, args.deck_id, None)
        if deck_path is None:
            print(f"ERROR: deck id {args.deck_id!r} not found under "
                  f"{args.deck_dir}", flush=True)
            return 2

    # Resolve the bracket: explicit flag wins, else infer from filename.
    if args.bracket is None:
        inferred = _bracket_from_filename(deck_path.name)
        if inferred is None:
            print(f"ERROR: --bracket not given and no [B<n>] suffix in "
                  f"{deck_path.name!r}; pass --bracket 1-5.", flush=True)
            return 2
        args.bracket = inferred
    if not (1 <= args.bracket <= 5):
        print(f"ERROR: bracket must be 1-5, got {args.bracket}", flush=True)
        return 2

    deck_id = deck_path.stem

    # Intent learning (Slice A): learn the deck's intent before the loop.
    # Defaults to None when --learn-intent is not supplied.
    args.intent: Optional[Intent] = None
    try:
        preferences = resolve_preferences(
            getattr(args, "preferences", None),
            getattr(args, "preferences_file", None))
    except OSError as exc:
        print(f"ERROR: cannot read --preferences-file: {exc}", flush=True)
        return 2
    if preferences and args.learn_intent_path is None:
        # The pilot's words need an intent to ride on; the deck under
        # improvement is the natural source (R3 F-01).
        args.learn_intent_path = deck_path
    if args.learn_intent_path is not None:
        intent_src = args.learn_intent_path.resolve()
        if not intent_src.exists():
            print(f"ERROR: --learn-intent deck not found: {intent_src}", flush=True)
            return 2
        if not args.json:
            print(f"[improve] learning intent from {intent_src.name} ...", flush=True)
        try:
            args.intent = learn_intent(intent_src, pilot_preferences=preferences)
            if not args.json:
                _print_intent(args.intent)
        except Exception as exc:  # noqa: BLE001 — intent is advisory
            if not args.json:
                print(f"[improve] intent learning failed ({exc}); "
                      "proceeding without intent.", flush=True)
            args.intent = None

    # LOUD up-front warning before any Forge/LLM time is spent, in the
    # RIGHT units: --sim-games is TOTAL pod games, the verdict gate
    # counts DECISIVE games, and the two filler seats win ~half of the
    # pod games -- so the comparison is expected-decisive (sim_games *
    # EXPECTED_DECISIVE_FRACTION) vs the gate. Comparing raw sim_games
    # to the gate (the pre-2026-07-20 bug) let --sim-games 25 pass this
    # check silently while every round still resolved 'inconclusive'
    # (~12-13 decisive < 20). Below the gate the greedy loop -- which
    # advances ONLY on 'kept' -- cannot, in expectation, ever move the
    # base deck forward. On stderr so --json stdout stays
    # machine-parseable and the warning is visible even when stdout is
    # piped.
    expected_decisive = args.sim_games * EXPECTED_DECISIVE_FRACTION
    if expected_decisive < MIN_DECISIVE_GAMES_FOR_VERDICT:
        print(
            f"[improve] WARNING: --sim-games counts TOTAL pod games, "
            f"but the verdict gate counts DECISIVE games (won by the "
            f"old or new deck; the 2 filler seats take ~half). "
            f"{args.sim_games} total pod games ~= "
            f"{int(expected_decisive)} expected decisive, below the "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT}-decisive gate -- every "
            f"round's verdict will likely be 'inconclusive', and "
            f"improve only advances the deck on 'kept', so this run "
            f"will probably just burn Forge/LLM time. A verdict needs "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT} decisive ~= "
            f"{min_sim_games_for_verdict()}+ total games; pass "
            f"--sim-games >= {min_sim_games_for_verdict()} "
            f"(default 45).",
            file=sys.stderr, flush=True,
        )

    # Resolve replication BEFORE any Forge/LLM spend and state the cost
    # implication up front -- an operator who typed neither flag is
    # entitled to know, before the first sim, that an advancing round now
    # runs two of them.
    args.replicate = resolve_replicate_default(args)

    if not args.json:
        if args.replicate:
            print(f"[improve] replication: ON -- a first 'kept' is re-tested "
                  f"with a second independent A/B before the base advances, "
                  f"so a CONFIRMED swap costs 2x sim time "
                  f"(~2 x {args.sim_games} pod games for that round). Pass "
                  f"--no-replicate for single-shot advancing.", flush=True)
            # BOTH numbers, up front (2026-08-20, decision R2-D2). The
            # 1-in-1,600 false-advance rate was the only figure this
            # banner used to state, which sells the gate as an improver.
            # It is a false-positive-proof SCREEN: at the shipped power
            # the true-positive throughput is the other half of the same
            # trade and the operator is entitled to see it before
            # committing a night of Forge time.
            print(
                "[improve] what that buys, both directions: a truly "
                "neutral swap advances ~1 in 1,600 times (the "
                "false-positive floor), and a GENUINELY good +5pp swap "
                "advances with p=0.13% per round -- about 1.3% over a "
                "10-round run. 'Nothing advanced' is the EXPECTED "
                "outcome of an overnight run; an advance is a rare lead "
                "worth investigating (likelihood ratio ~9.2), not proof "
                "and not a progress bar.",
                flush=True,
            )
        else:
            print("[improve] replication: OFF -- a single 'kept' advances "
                  "the base (single-shot). Pass --replicate to require a "
                  "confirming second A/B.", flush=True)
        search_note = (f", search-budget={args.search_budget} pulls/round"
                       if args.search_budget else "")
        if args.search_budget and args.screen:
            search_note += (f", forgepy-screen=on (keep "
                            f"{args.screen_keep:g}, "
                            f"{args.screen_games} py-games/arm)")
        print(f"[improve] {deck_id} (B{args.bracket}) -- strategy={args.strategy}, "
              f"up to {args.rounds} rounds, mode={args.mode}, "
              f"{args.sim_games} games/round{search_note}", flush=True)

    if args.strategy == "bandit":
        return _run_bandit_strategy(deck_path, deck_id, args)

    # Round-fn selection (FP-012 full slice): --search-budget swaps the
    # curator-driven round for the bandit-search round. The import is
    # deliberately INSIDE the branch so the disabled path (budget 0,
    # the default) never even loads the search module — the greedy
    # behavior stays byte-identical, and the test suite pins that by
    # spying that make_search_round_fn is never constructed.
    round_fn = _default_round_fn
    if args.search_budget:
        from .improve_search import make_search_round_fn
        round_fn = make_search_round_fn()

    result = run_improve_loop(deck_path, deck_id, args.rounds, args,
                              round_fn=round_fn)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_summary(result)

    # Exit non-zero only when every round errored (no useful work done).
    if result.history and all(rr.verdict == "error" for rr in result.history):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(improve_main())
