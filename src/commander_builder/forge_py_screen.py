"""forge_py screening gate — a SCREEN, never a JUDGE (FP-012 x forge_py).

The contract, stated once and enforced everywhere in this module:

    forge_py decides which candidates DESERVE Forge games.
    Forge decides which deck is BETTER. Only Forge.

forge_py is the Python-native goldfish/pod simulator living in the
companion repo (invoked through ``forge_py_correlation.run_forge_py_ab``
— the SAME lazy-import / deck-parse / result-shape path the FP-001
correlation harness uses, so there is exactly one way this codebase
talks to forge_py). Its measured agreement with real Forge outcomes is
r ~= 0.898 rank correlation (FP-001 measurement; the paired corpus
behind that number is owned by ``forge_py_correlation.py``). 0.898 is
plenty to rank a candidate pool and nowhere near enough to render a
verdict — which is why nothing in this module may ever feed a
keep/kept/advance decision. It only prunes the ARM POOL the FP-012
bandit will spend real Forge games on.

Behavioral rules (all pinned by tests):

- Default OFF. Screening runs only under ``commander-improve --screen``
  or ``COMMANDER_BUILDER_FORGEPY_SCREEN=1``; with both unset the
  improve loop is byte-identical to pre-screen behavior.
- Unmeasurable candidates are always KEPT. A candidate that could not
  be staged (apply failed / swap dropped by legality), errored inside
  forge_py, or produced zero decisive games gave the screen no
  measurement — and a screen may only condemn what it measured. The
  bandit's own evaluate path will kill truly-broken arms at pull time,
  exactly as it does today.
- Missing/broken forge_py degrades LOUDLY to "no screening": all
  candidates kept, one stderr note. The screen must never block the
  improve loop.
- Every pruned candidate is logged with its screen score — no silent
  drops (house convention).
- The kept pool never shrinks below ``min_kept`` (default 2) arms.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .forge_py_correlation import ForgePyABResult, run_forge_py_ab

# Env var that force-enables screening (equivalent to --screen).
SCREEN_ENV_FLAG = "COMMANDER_BUILDER_FORGEPY_SCREEN"

# Keep the top half by default. The screen's job is to stop the bandit
# cold-starting on arms forge_py can already tell are weak — not to
# hand-pick a winner (that would be judging).
DEFAULT_KEEP_FRACTION = 0.5

# Never screen the pool below this many arms: a 1-arm "search" isn't a
# search, and UCB1's whole value is comparing alternatives with real
# Forge evidence.
MIN_KEPT = 2

# forge_py games per candidate. These are cheap in-process Python sims
# (seconds), NOT Forge JVM games — 20 gives a stable enough decisive
# share to rank on without meaningfully delaying the round.
DEFAULT_SCREEN_GAMES = 20

# Fixed seed base: same pool + same decks -> same screen verdicts.
DEFAULT_SCREEN_SEED = 0


def screening_enabled(args=None) -> bool:
    """--screen flag OR the env var. Default OFF — with both unset the
    bandit round must behave byte-identically (pinned by test)."""
    if args is not None and getattr(args, "screen", False):
        return True
    return os.environ.get(SCREEN_ENV_FLAG, "") == "1"


@dataclass
class CandidateScore:
    """One candidate's screen measurement.

    ``score`` is the candidate deck's decisive-share win rate against
    the base deck in forge_py (``new_wins / (old_wins + new_wins)``) —
    the same [0,1] mapping the bandit's ``margin_reward`` uses for
    Forge sims, so the two scales read the same way. ``None`` = the
    screen could not measure this candidate (see ``error``); such
    candidates are always kept.
    """

    key: str
    score: Optional[float]
    games: int
    error: Optional[str] = None


@dataclass
class ScreenReport:
    """Outcome of one screening pass.

    ``screened=False`` means the screen stood down entirely (forge_py
    unavailable, nothing measurable, or the pool was already at/below
    the min-kept floor): ``kept_keys`` then lists every candidate and
    ``pruned`` is empty. That is the loud-degrade path — never an
    exception, never a blocked loop.
    """

    screened: bool
    reason: Optional[str] = None
    scores: list[CandidateScore] = field(default_factory=list)
    kept_keys: list[str] = field(default_factory=list)
    pruned: list[CandidateScore] = field(default_factory=list)


def _default_runner(base_path: Path, cand_path: Path,
                    games: int, seed: int) -> ForgePyABResult:
    """The ONE sanctioned forge_py invocation path: the correlation
    harness's ``run_forge_py_ab`` (lazy import, parse_dck staging,
    ForgePyABResult shape). base = 'old', candidate = 'new'."""
    return run_forge_py_ab(base_path, cand_path, games,
                           mode="1v1", seed_base=seed)


def screen_candidates(
    base_path: Path,
    candidates: list[tuple[str, Optional[Path]]],
    *,
    keep_fraction: float = DEFAULT_KEEP_FRACTION,
    min_kept: int = MIN_KEPT,
    games: int = DEFAULT_SCREEN_GAMES,
    seed: int = DEFAULT_SCREEN_SEED,
    runner: Optional[Callable[[Path, Path, int, int], ForgePyABResult]] = None,
    log=None,
) -> ScreenReport:
    """Score each ``(key, staged_deck_path)`` candidate against
    ``base_path`` in forge_py and prune the bottom of the ranking.

    ``path=None`` marks a candidate the caller could not stage — it is
    scored as unmeasurable and therefore kept. ``runner`` is the
    injectable forge_py seam (tests pass a fake; production uses
    ``_default_runner``); its contract is
    ``(base_path, cand_path, games, seed) -> ForgePyABResult``.

    Pruning math: rank the MEASURED candidates by score (desc, key as
    deterministic tie-break) and keep the top
    ``ceil(n_measured * keep_fraction)`` — raised as needed so that
    measured-kept + unmeasured (always kept) >= ``min_kept``. Pools of
    ``min_kept`` or fewer skip the sims entirely: nothing could be
    pruned, so no screen time is spent.
    """
    if not (0.0 < keep_fraction <= 1.0):
        raise ValueError(
            f"keep_fraction must be in (0, 1], got {keep_fraction}")
    if games < 1:
        raise ValueError(f"games must be >= 1, got {games}")
    if min_kept < 1:
        raise ValueError(f"min_kept must be >= 1, got {min_kept}")
    out = log if log is not None else sys.stderr

    def _say(msg: str) -> None:
        print(msg, file=out, flush=True)

    n = len(candidates)
    all_keys = [k for k, _ in candidates]
    if n <= min_kept:
        # Nothing could legally be pruned — don't spend screen sims.
        reason = (f"pool of {n} candidate(s) is at/below the "
                  f"min-kept floor ({min_kept}); nothing to prune")
        _say(f"[screen] forge_py screening skipped: {reason}.")
        return ScreenReport(screened=False, reason=reason,
                            kept_keys=all_keys)

    scores: list[CandidateScore] = []
    active_runner = runner if runner is not None else _default_runner
    for key, path in candidates:
        if path is None:
            scores.append(CandidateScore(
                key=key, score=None, games=0,
                error="not stageable (apply failed or swap dropped "
                      "by legality)"))
            continue
        try:
            res = active_runner(base_path, path, games, seed)
        except Exception as exc:  # noqa: BLE001 — screen must not raise
            scores.append(CandidateScore(
                key=key, score=None, games=0,
                error=f"runner raised {type(exc).__name__}: {exc}"))
            continue
        if res.error:
            scores.append(CandidateScore(
                key=key, score=None, games=res.total_games,
                error=res.error))
            continue
        decisive = (res.old_wins or 0) + (res.new_wins or 0)
        if decisive <= 0:
            # Same rule as margin_reward: no decisive games = no
            # measurement, and fabricating a 0.5 would launder
            # no-signal into a rankable score.
            scores.append(CandidateScore(
                key=key, score=None, games=res.total_games,
                error="zero decisive games"))
            continue
        scores.append(CandidateScore(
            key=key, score=res.new_wins / decisive,
            games=res.total_games))

    measured = [s for s in scores if s.score is not None]
    unmeasured = [s for s in scores if s.score is None]
    if not measured:
        # forge_py missing/broken (or every candidate unmeasurable):
        # stand down LOUDLY, keep everything — Forge will judge them
        # all, exactly as if screening were off.
        first_err = next((s.error for s in scores if s.error), "no scores")
        reason = f"no candidate could be measured ({first_err})"
        _say(f"[screen] forge_py screening unavailable — {reason}; "
             f"keeping all {n} candidates unscreened. Forge remains "
             f"the judge either way.")
        return ScreenReport(screened=False, reason=reason,
                            scores=scores, kept_keys=all_keys)

    ranked = sorted(measured, key=lambda s: (-s.score, s.key))
    kept_measured_n = max(
        math.ceil(len(measured) * keep_fraction),
        min_kept - len(unmeasured),
    )
    kept_measured_n = min(max(kept_measured_n, 1), len(measured))
    kept_measured = set(s.key for s in ranked[:kept_measured_n])
    pruned = ranked[kept_measured_n:]
    kept_keys = [s.key for s in scores
                 if s.score is None or s.key in kept_measured]

    _say(f"[screen] forge_py screened {n} candidates "
         f"({games} forge_py games each, keep-frac {keep_fraction}, "
         f"min-kept {min_kept}): kept {len(kept_keys)}, "
         f"pruned {len(pruned)}. forge_py is a SCREEN, not a judge — "
         f"Forge still renders every verdict.")
    for s in scores:
        if s.score is None:
            _say(f"[screen]   kept   {s.key!r} — unmeasured "
                 f"({s.error}); a screen only condemns what it "
                 f"measured.")
        elif s.key in kept_measured:
            _say(f"[screen]   kept   {s.key!r} score={s.score:.3f}")
    for s in pruned:  # no silent drops — house convention
        _say(f"[screen]   PRUNED {s.key!r} score={s.score:.3f}")

    return ScreenReport(screened=True, scores=scores,
                        kept_keys=kept_keys, pruned=list(pruned))


def screen_arms_for_search(
    deck_path: Path,
    arms: list,
    args,
    *,
    runner: Optional[Callable] = None,
    apply_fn: Optional[Callable] = None,
    log=None,
) -> list:
    """FP-012 hook: screen the bandit's swap-arm pool before ANY Forge
    games are spent, returning the kept arms (original objects, original
    advisor-ranked order preserved).

    Each arm's single swap is staged into a throwaway temp copy of the
    base deck through the SAME ``apply_proposal_to_deck`` legality path
    a real pull uses, then goldfished against the base via the
    correlation harness's forge_py invocation. Arms that fail to stage
    are kept unmeasured (the bandit's evaluate path handles them at
    pull time, exactly as today).

    ``runner`` / ``apply_fn`` are injectable seams so tests never touch
    a real forge_py install. Reads ``args.screen_keep`` /
    ``args.screen_games`` with the module defaults.

    Never raises past its caller's needs: on any internal surprise the
    caller (improve_search) degrades to the unscreened pool.
    """
    from .proposer import Proposal
    if apply_fn is None:
        from .proposer import apply_proposal_to_deck as apply_fn

    keep_fraction = float(getattr(args, "screen_keep",
                                  DEFAULT_KEEP_FRACTION))
    games = int(getattr(args, "screen_games", DEFAULT_SCREEN_GAMES))

    with tempfile.TemporaryDirectory(prefix="cb_forgepy_screen_") as td:
        staged: list[tuple[str, Optional[Path]]] = []
        for i, arm in enumerate(arms):
            base_i = Path(td) / f"screen{i:03d} {deck_path.name}"
            try:
                shutil.copy2(deck_path, base_i)
                prop = Proposal(
                    adds=[arm.add] if arm.add else [],
                    cuts=[arm.cut] if arm.cut else [],
                    rationale=f"forge_py screen probe: {arm.key}",
                    source="forgepy-screen-probe",
                )
                cand = apply_fn(base_i, prop, dry_run=False)
            except Exception:  # noqa: BLE001 — unmeasurable, kept
                staged.append((arm.key, None))
                continue
            if not prop.applied_adds and not prop.applied_cuts:
                staged.append((arm.key, None))
                continue
            staged.append((arm.key, cand))

        # Score INSIDE the tempdir context — the staged decks live there.
        report = screen_candidates(
            deck_path, staged,
            keep_fraction=keep_fraction, min_kept=MIN_KEPT,
            games=games, seed=DEFAULT_SCREEN_SEED,
            runner=runner, log=log,
        )

    kept = set(report.kept_keys)
    kept_arms = [a for a in arms if a.key in kept]
    # Defensive: a screen may narrow the pool, never empty it.
    return kept_arms if kept_arms else list(arms)
