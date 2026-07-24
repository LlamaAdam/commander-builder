"""Budget-bounded UCB1 swap search inside the improve loop (FP-012 full slice).

The greedy improve loop (slice 1) accepts whatever the Claude curator
proposes each round and keeps the whole proposal only if the A/B sim
says the new deck won. That couples two very different questions —
"which swaps are good?" (the curator's judgment call) and "is the deck
better?" (the sim's empirical verdict) — and spends the entire sim
budget answering only the second one. Slice 2's ``--strategy bandit``
went the other way: pure bandit, no round machinery, no keep-if-better
contract.

This module is the missing middle: a **candidate-level bandit search
that lives INSIDE an improve round**. Each round:

1. Build swap-candidate ARMS from the advisor's already-filtered pool
   (heuristic / bracket-peers / EDHREC-sourced — see "Why Claude is
   excluded" below). One arm = one concrete (cut X, add Y) swap.
2. Spend ``--search-budget`` sim pulls on a UCB1 bandit over those
   arms. Pulling an arm = apply that ONE swap to the round's base deck,
   run ONE A/B sim of the configured ``--sim-games``, and convert the
   decisive margin into a [0,1] reward.
3. After the budget is exhausted, the best arm(s) by empirical mean
   (with at least ``--search-min-pulls`` observations each) form the
   round's applied proposal — written through the exact same
   ``apply_proposal_to_deck`` path as every other proposal, so every
   legality guard (pair validation, protection, balancing, padding,
   the 99-mainboard hard guard) still applies.
4. The round then proceeds through the existing keep-if-better verdict
   machinery unchanged: one more A/B sim of base-vs-applied, verdict
   via ``_verdict_from_ab``, knowledge_log row updated via
   ``update_iteration_sim``, and ``run_improve_loop`` advances the base
   only on 'kept'.

Why Claude is excluded from the search inner loop
-------------------------------------------------
The whole point of the bandit is to replace a judgment call with
empirical evidence: reward comes from Forge games, not from a model's
opinion. Putting the Claude curator inside the pull loop would (a) cost
one LLM call per pull for a decision the sim immediately re-litigates,
(b) make pulls non-reproducible (same arm, different curator mood), and
(c) re-import exactly the subjectivity the search exists to remove. The
FP-001 measurements point the same way: the engine's empirical signal,
not LLM judgment, is the binding resource. So arms are drawn ONLY from
the advisor's offline candidate sources (``heuristic`` = EDHREC
inclusion/synergy stats, ``bracket_peers`` = other tuned local builds);
a ``--source claude`` request is coerced to ``heuristic`` for the
search with a loud stderr note.

Honest cost model
-----------------
Each pull costs ONE full A/B sim ≈ ``--sim-games`` Forge pod games
(~10+ minutes at the 45-game default), plus one more sim for the
round's final verdict. A round with ``--search-budget 8`` therefore
burns ≈ 9 × sim_games pod games. The budget flag exists precisely so
that cost is a deliberate, bounded choice.

UCB1 math (why this policy)
---------------------------
UCB1 pulls the arm maximizing ``mean + c·sqrt(ln N / n_arm)`` where N is
the total pulls so far and n_arm the arm's own pulls. The second term is
an exploration bonus that shrinks as an arm accumulates evidence, so
noisy single-sim rewards can't permanently lock the search onto a
lucky-but-mediocre swap — under-sampled arms keep getting revisited
until their confidence interval separates from the leader's. UCB1's
regret bound assumes rewards in [0,1], which is WHY the decisive margin
is mapped there (see ``margin_reward``). The exploration constant ``c``
(CLI ``--ucb-c``, default 1.4 ≈ sqrt(2), the classic UCB1 choice) trades
exploration (higher c) against exploitation (lower c). Selection is
delegated to ``bandit.UCB1`` so the formula lives in exactly one place.

Everything here threads the injectable seams (``arm_builder``,
``sim_fn``) so tests never touch Forge, Anthropic, or the network.

Empirical shakedown status: NOT yet validated against live Forge — the
gauntlet soak owns the CPU. See docs/future-plans.md (FP-012).
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .bandit import Arm, UCB1, update_arm
from .improve import RoundResult

# An arm's mean reward must STRICTLY exceed this to be applied.
# margin_reward maps a decisive margin of exactly 0 (new deck won the
# same number of decisive games as the old) to 0.5 — the break-even
# point. Applying a swap whose best evidence says "coin flip" would
# spend a deck change (and the round's verdict sim) on noise, so
# winners must be empirically BETTER than break-even, not merely tied.
BREAK_EVEN_REWARD = 0.5

# Hard cap on how many winning swaps a single search round applies.
# Deliberately small and NOT a CLI flag: the pulls measured each swap
# INDIVIDUALLY against the base deck, so the combined proposal's effect
# is extrapolated, not measured — stacking many "individually good"
# swaps compounds that extrapolation error (and interaction effects are
# exactly what an independent-arm bandit cannot see; that's the parked
# GP-BO slice B2). Three keeps the applied set attributable while still
# letting a productive round move more than one card.
MAX_APPLIED_SWAPS = 3

# Default for --search-min-pulls. One pull of a 45-game sim has a
# win-rate standard error around 0.1 on ~22 decisive games — a single
# lucky pull can look great. Requiring 2+ pulls before an arm may win
# means every applied swap survived at least two independent sims.
DEFAULT_MIN_PULLS = 2


@dataclass
class SearchArm(Arm):
    """A bandit arm with a liveness flag.

    ``dead`` arms are excluded from further selection AND from the
    winner set. An arm dies when a pull produced no usable signal:
    the probe deck failed the legality apply path, the sim didn't
    complete, or the sim finished with ZERO decisive games (every game
    went to a filler seat or a draw — the head-to-head pair never won,
    so there is no margin to learn from). Killing rather than retrying
    is the conservative choice: we only apply swaps whose evidence is
    consistently interpretable, and a dead arm frees its share of the
    budget for arms that ARE producing signal.
    """

    dead: bool = False
    death_reason: Optional[str] = None


@dataclass
class PullRecord:
    """One budget unit spent."""

    pull: int
    arm_key: str
    reward: Optional[float]  # None = no usable signal (arm marked dead)
    no_signal: bool


@dataclass
class SearchResult:
    """Outcome of one budget-bounded swap search."""

    budget: int
    pulls_used: int
    chosen: list[dict] = field(default_factory=list)
    arm_stats: list[dict] = field(default_factory=list)
    history: list[PullRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def margin_reward(wins_a: int, wins_b: int) -> Optional[float]:
    """Map an A/B sim's decisive margin onto UCB1's required [0,1] scale.

    Decisive semantics (post-69ab9a5): decisive = wins_a + wins_b, the
    games the head-to-head pair actually won — filler wins and draws
    carry no information about the swap and are excluded.

    The signed relative margin ``m = (wins_b - wins_a) / decisive``
    lives in [-1, +1]. UCB1's regret analysis assumes bounded [0,1]
    rewards, so we apply the affine map ``(m + 1) / 2`` — which
    algebraically collapses to ``wins_b / decisive``, the new deck's
    decisive-share win rate:

        (m + 1)/2 = ((wins_b - wins_a)/d + (wins_b + wins_a)/d) / 2
                  = wins_b / d

    So reward 0.5 = break-even, 1.0 = the new deck won every decisive
    game, 0.0 = it lost every one.

    Returns None when decisive == 0: there is no margin to map, and
    fabricating a 0.5 "tie" would launder no-signal into break-even
    evidence. The caller marks the arm instead (no reward update).
    """
    decisive = (wins_a or 0) + (wins_b or 0)
    if decisive <= 0:
        return None
    return wins_b / decisive


def build_swap_arms(
    deck_path: Path,
    bracket: int,
    source: str,
    *,
    protected: tuple = (),
    max_arms: Optional[int] = None,
    intent_themes: Optional[list[str]] = None,
    advise_fn: Optional[Callable] = None,
) -> list[SearchArm]:
    """Build the arm pool from the advisor's offline candidate sources.

    Reuses the SAME candidate generation auto-curate feeds the curator
    (``improvement_advisor.advise`` → AdviceReport manifest), but stops
    BEFORE the Claude curation step — the bandit replaces the curator's
    add/cut pairing judgment with measured rewards. ``source="claude"``
    is coerced to ``"heuristic"`` (see module docstring: Claude stays
    out of the search inner loop) with a stderr note so the operator
    knows their flag was overridden rather than silently honored.

    Pairing: the i-th candidate add is paired with the (i mod n_cuts)-th
    candidate cut — the same convention slice 2's ``--strategy bandit``
    established. The advisor already ranks both lists, so early arms
    pair its strongest add with its strongest cut.

    Legality pre-filters applied HERE (cheap, saves wasted pulls):
      - cuts matching ``protected`` (case-insensitive) never become
        arms — the apply path would strip them anyway, turning the
        pull into a base-vs-base sim that burns a full Forge budget
        to measure nothing.
    Everything else (singleton rule, commander adds, color identity of
    what actually lands, the 99-main guard) is enforced by the shared
    ``apply_proposal_to_deck`` path at pull time and again at final
    apply time.

    ``max_arms`` caps the pool (advisor order = ranked order). The
    caller sizes it to the budget: an arm the budget can never pull
    ``min_pulls`` times can never be applied, so carrying it only
    dilutes cold-start coverage.
    """
    if advise_fn is None:
        from .improvement_advisor import advise as advise_fn  # type: ignore

    effective_source = source
    if source == "claude":
        print(
            "[search] --source claude is not used inside the bandit "
            "search (the curator stays out of the inner loop; rewards "
            "come from sims, not model judgment). Using the heuristic "
            "advisor's candidate pool instead.",
            file=sys.stderr, flush=True,
        )
        effective_source = "heuristic"

    report = advise_fn(deck_path=deck_path, bracket=bracket,
                       source=effective_source)
    manifest = report.to_manifest()
    adds = list(manifest.get("added", []) or [])
    cuts = list(manifest.get("removed", []) or [])

    protected_lower = {p.lower() for p in protected}
    cuts = [c for c in cuts if c.lower() not in protected_lower]

    arms: list[SearchArm] = []
    for i, add in enumerate(adds):
        cut = cuts[i % len(cuts)] if cuts else None
        key = f"+{add} / -{cut}" if cut else f"+{add}"
        arms.append(SearchArm(key=key, add=add, cut=cut))
        if max_arms is not None and len(arms) >= max_arms:
            break
    return arms


def run_swap_search(
    arms: list[SearchArm],
    budget: int,
    evaluate: Callable[[SearchArm], Optional[float]],
    *,
    ucb_c: float = 1.4,
    min_pulls: int = DEFAULT_MIN_PULLS,
    max_swaps: int = MAX_APPLIED_SWAPS,
) -> SearchResult:
    """Spend up to ``budget`` pulls on a UCB1 bandit over ``arms``.

    ``evaluate(arm)`` runs ONE probe sim of that single swap against the
    current base deck and returns the [0,1] reward, or None when the
    pull produced no usable signal (apply failure / sim failure / zero
    decisive games). A None marks the arm dead: no reward update, no
    further pulls, ineligible to win. Every ``evaluate`` call consumes
    one budget unit regardless of outcome — the loop cannot see whether
    Forge actually ran inside the callable, and honest accounting beats
    a budget that silently stretches on failures.

    Selection is delegated to ``bandit.UCB1`` over the LIVE arms only,
    so the UCB formula (cold-start each untried arm once in list order,
    then argmax of mean + c·sqrt(ln N / n)) is single-sourced. N is the
    pull total across live arms; a dead arm's earlier pulls drop out of
    N, which slightly RAISES the exploration bonus for survivors — the
    right direction when part of the pool turned out to be noise.

    Winner selection after the budget: live arms with at least
    ``min_pulls`` observations AND mean strictly above break-even
    (see BREAK_EVEN_REWARD), best mean first (arm key breaks ties
    deterministically), capped at ``max_swaps`` (see MAX_APPLIED_SWAPS).
    """
    if budget < 0:
        raise ValueError(f"budget must be >= 0, got {budget}")

    policy = UCB1(c=ucb_c)
    # UCB1.select takes an rng for interface parity with the stochastic
    # policies but never draws from it — selection here is fully
    # deterministic (cold-start in list order, then the argmax).
    rng = random.Random(0)

    history: list[PullRecord] = []
    for pull_no in range(1, budget + 1):
        live = [a for a in arms if not a.dead]
        if not live:
            break  # every arm died — spending more budget buys nothing
        arm = policy.select(live, rng)
        reward = evaluate(arm)
        if reward is None:
            # No usable signal: mark, don't update. Folding a fake
            # reward in would poison the mean; re-pulling a swap that
            # just produced garbage wastes budget better spent on arms
            # that ARE measurable.
            arm.dead = True
            if not arm.death_reason:
                arm.death_reason = "no_signal"
            history.append(PullRecord(pull=pull_no, arm_key=arm.key,
                                      reward=None, no_signal=True))
        else:
            update_arm(arm, reward)
            history.append(PullRecord(pull=pull_no, arm_key=arm.key,
                                      reward=round(reward, 4),
                                      no_signal=False))

    winners = sorted(
        (a for a in arms
         if not a.dead and a.pulls >= min_pulls and a.mean > BREAK_EVEN_REWARD),
        key=lambda a: (-a.mean, a.key),
    )[:max_swaps]

    arm_stats = sorted(
        ({"key": a.key, "add": a.add, "cut": a.cut, "pulls": a.pulls,
          "mean": round(a.mean, 4), "dead": a.dead,
          "death_reason": a.death_reason} for a in arms),
        key=lambda d: (-d["mean"], d["key"]),
    )
    return SearchResult(
        budget=budget,
        pulls_used=len(history),
        chosen=[{"key": a.key, "add": a.add, "cut": a.cut,
                 "pulls": a.pulls, "mean": round(a.mean, 4)}
                for a in winners],
        arm_stats=arm_stats,
        history=history,
    )


def _resolve_protected(deck_path: Path, args) -> list[str]:
    """Union the protect sources the greedy round honors, order-
    preserving with case-insensitive dedup: deck [metadata] Protect=
    entries, --protect flags, --protect-from file, and the learned
    intent's key wincons. Building this list BEFORE the search means
    protected cuts never even become arms (see build_swap_arms)."""
    from .intent import intent_protect_cards
    from .web._helpers import read_protected_cards

    combined: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = (name or "").strip()
        if n and n.lower() not in seen:
            seen.add(n.lower())
            combined.append(n)

    try:
        for c in read_protected_cards(deck_path.read_text(encoding="utf-8")):
            _add(c)
    except OSError:
        pass
    for c in (getattr(args, "protect", []) or []):
        _add(c)
    protect_from = getattr(args, "protect_from", None)
    if protect_from:
        pf = Path(protect_from)
        if pf.exists():
            for line in pf.read_text(encoding="utf-8").splitlines():
                _add(line)
    for c in intent_protect_cards(getattr(args, "intent", None)):
        _add(c)
    return combined


def make_search_round_fn(
    *,
    arm_builder: Callable = build_swap_arms,
    sim_fn: Optional[Callable] = None,
):
    """Build a ``round_fn`` for ``run_improve_loop`` that replaces the
    Claude curator's proposal with the bandit's chosen swap set.

    The returned callable has the exact ``(deck_path, round_no, args)
    -> RoundResult`` contract of ``_default_round_fn``, so the greedy
    loop's keep-if-better advance / no-op convergence / error stop
    logic applies UNCHANGED — the search only decides WHAT the round
    proposes, never whether the round's outcome advances the base.

    ``arm_builder`` and ``sim_fn`` are injectable so tests drive the
    whole round with scripted candidates and sim outcomes and never
    touch Forge. ``sim_fn`` must match ``forge_batch.run_ab_simulation``
    's shape: ``(deck_a_path, deck_b_path, games=..., fillers=...) ->
    ABResult``; the default resolves lazily so importing this module
    stays cheap.
    """

    def round_fn(deck_path: Path, round_no: int, args) -> RoundResult:
        from .proposer import Proposal, apply_proposal_to_deck, \
            _bump_version_filename
        from ._proposer_sim import (
            _ab_to_iteration_fields,
            _log_auto_curate_iteration,
            _pick_filler_decks,
            _verdict_from_ab,
        )

        def _error(msg: str) -> RoundResult:
            return RoundResult(
                round=round_no, input_deck=str(deck_path), output_deck=None,
                verdict="error", advanced=False, error=msg,
            )

        _sim = sim_fn
        if _sim is None:
            from .forge_batch import run_ab_simulation as _sim

        budget = int(getattr(args, "search_budget", 0) or 0)
        min_pulls = int(getattr(args, "search_min_pulls", DEFAULT_MIN_PULLS))
        ucb_c = float(getattr(args, "ucb_c", 1.4))

        # --- 1. Arms from the advisor's offline candidate pool. -------
        protected = _resolve_protected(deck_path, args)
        # Pool sizing: an arm the budget can never pull min_pulls times
        # can never be applied, so a pool wider than budget//min_pulls
        # only dilutes cold-start coverage (UCB1 must try every arm
        # once before it can exploit anything).
        max_arms = max(1, budget // max(1, min_pulls))
        try:
            arms = arm_builder(
                deck_path, args.bracket, args.source,
                protected=tuple(protected), max_arms=max_arms,
            )
        except Exception as exc:  # noqa: BLE001 — round isolation
            return _error(f"arm building failed: {type(exc).__name__}: {exc}")
        if not arms:
            # Zero candidates: return a zero-change round so
            # run_improve_loop's convergence logic (applied 0/0 ->
            # 'no-op', stop) handles it exactly like a curator that
            # proposed nothing.
            return RoundResult(
                round=round_no, input_deck=str(deck_path), output_deck=None,
                verdict="neutral", advanced=False,
                applied_adds=0, applied_cuts=0,
            )

        # --- 2. Fillers, picked ONCE per round. ------------------------
        # Deliberate: every pull (and the final verdict sim) faces the
        # SAME two opponents, so per-arm rewards differ only by the swap
        # under test, not by filler luck-of-the-draw. Re-picking per
        # pull would add filler-asymmetry variance to exactly the signal
        # the bandit is trying to separate from noise.
        deck_dir = deck_path.parent
        probe_path = deck_dir / _bump_version_filename(deck_path.name)
        if getattr(args, "sim_fillers", None):
            fillers = [f.strip() for f in args.sim_fillers.split(",")
                       if f.strip()]
        else:
            fillers = _pick_filler_decks(
                deck_dir, exclude_paths=[deck_path, probe_path], count=2,
                target_bracket=args.bracket,
            )
        if len(fillers) < 2:
            return _error(
                f"need 2+ filler decks in {deck_dir} for a 4-player pod; "
                f"found {len(fillers)}"
            )

        # --- 3. The pull evaluator: one swap, one sim, one reward. ----
        # Probe decks are written through apply_proposal_to_deck so the
        # SAME legality pipeline that guards real proposals guards the
        # probes (a probe Forge would reject is a wasted sim). All
        # probes share one on-disk path (the bumped-version name derived
        # from the base deck) and each pull overwrites the last — the
        # final applied proposal overwrites it once more at step 4.
        probe_existed_before = probe_path.exists()
        probe_written = {"flag": False}

        def evaluate(arm: SearchArm) -> Optional[float]:
            probe_proposal = Proposal(
                adds=[arm.add] if arm.add else [],
                cuts=[arm.cut] if arm.cut else [],
                rationale=f"bandit probe: {arm.key}",
                source="bandit-search-probe",
            )
            try:
                probe = apply_proposal_to_deck(
                    deck_path, probe_proposal, dry_run=False,
                )
            except Exception:  # noqa: BLE001 — swap can't legally apply
                arm.death_reason = "apply_failed"
                return None
            probe_written["flag"] = True
            if not probe_proposal.applied_adds and not probe_proposal.applied_cuts:
                # Pair validation dropped the whole swap (cut not in
                # decklist / add already present / add is the
                # commander): the probe is byte-equivalent to the base,
                # so simming it would spend a full Forge budget to
                # measure base-vs-base noise.
                arm.death_reason = "swap_dropped_by_legality"
                return None
            ab = _sim(
                deck_a_path=deck_path, deck_b_path=probe,
                games=args.sim_games, fillers=fillers,
            )
            if getattr(ab, "status", None) != "done":
                arm.death_reason = "sim_" + str(getattr(ab, "status", "unknown"))
                return None
            # Zero decisive games -> None -> the arm is marked, no
            # reward update (see margin_reward's docstring).
            return margin_reward(ab.wins_a or 0, ab.wins_b or 0)

        # --- 4. Run the search, then apply the winners. ----------------
        search = run_swap_search(
            arms, budget, evaluate,
            ucb_c=ucb_c, min_pulls=min_pulls, max_swaps=MAX_APPLIED_SWAPS,
        )

        if not search.chosen:
            # Nothing beat break-even with enough evidence. Clean up the
            # last probe (it's a discarded experiment, not a proposal —
            # leaving it would look like a curated v2) unless the path
            # pre-existed this round, and report a zero-change round so
            # the loop converges rather than re-running an identical
            # search.
            if probe_written["flag"] and not probe_existed_before:
                try:
                    probe_path.unlink()
                except OSError:
                    pass
            if not getattr(args, "json", False):
                print(f"[search] round {round_no}: {search.pulls_used} pulls, "
                      f"no swap beat break-even with >= {min_pulls} pulls; "
                      f"no changes applied.", flush=True)
            return RoundResult(
                round=round_no, input_deck=str(deck_path), output_deck=None,
                verdict="neutral", advanced=False,
                applied_adds=0, applied_cuts=0,
            )

        proposal = Proposal(
            adds=[w["add"] for w in search.chosen if w["add"]],
            cuts=[w["cut"] for w in search.chosen if w["cut"]],
            rationale=(
                "UCB1 swap search: "
                + "; ".join(f"{w['key']} (mean {w['mean']:+.2f}, "
                            f"{w['pulls']} pulls)" for w in search.chosen)
                + f" — {search.pulls_used}/{budget} pulls used"
            ),
            source="bandit-search",
        )
        try:
            # The shared apply path = ALL the legality guards: protected
            # cuts stripped (defense-in-depth), pair validation,
            # add/cut balancing, basic-land padding, and the refuse-to-
            # write-a-non-legal-mainboard hard guard.
            out_path = apply_proposal_to_deck(deck_path, proposal,
                                              dry_run=False)
        except Exception as exc:  # noqa: BLE001
            return _error(f"apply failed: {type(exc).__name__}: {exc}")

        applied_adds = len(proposal.applied_adds)
        applied_cuts = len(proposal.applied_cuts)
        if applied_adds == 0 and applied_cuts == 0:
            # Every winning swap was pair-dropped at apply time (can
            # happen if the deck changed since the arms were built).
            # Zero-change round -> loop converges.
            return RoundResult(
                round=round_no, input_deck=str(deck_path),
                output_deck=str(out_path), verdict="neutral", advanced=False,
                applied_adds=0, applied_cuts=0,
            )

        # --- 5. Existing keep-if-better verdict machinery, unchanged. --
        # Log the iteration row (verdict='pending'), run the SAME
        # base-vs-candidate A/B sim the greedy round runs, map it
        # through _verdict_from_ab, persist via update_iteration_sim.
        # The combined proposal gets its own verdict sim because the
        # pulls measured each swap INDIVIDUALLY — the combination's
        # effect is unmeasured until this sim, and only this verdict
        # (not the pull rewards) may advance the greedy base.
        iteration_id: Optional[int] = None
        db_path = Path(args.db_path) if getattr(args, "db_path", None) else None
        try:
            iteration_id = _log_auto_curate_iteration(
                src_deck_path=deck_path, new_deck_path=out_path,
                bracket=args.bracket, proposal=proposal, db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001 — the .dck is on disk;
            # losing the log row shouldn't kill the round.
            if not getattr(args, "json", False):
                print(f"[search] WARN: knowledge_log write failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)

        ab = _sim(
            deck_a_path=deck_path, deck_b_path=out_path,
            games=args.sim_games, fillers=fillers,
        )
        verdict = _verdict_from_ab(ab, margin=args.sim_margin)

        if iteration_id is not None:
            from .knowledge_log import update_iteration_sim
            sim_fields = _ab_to_iteration_fields(ab)
            try:
                update_iteration_sim(
                    iteration_id=iteration_id,
                    verdict=verdict,
                    notes=(f"bandit-search round: {applied_adds} adds / "
                           f"{applied_cuts} cuts; verdict sim old="
                           f"{ab.wins_a} new={ab.wins_b} ({ab.games} games, "
                           f"margin={args.sim_margin})"),
                    db_path=db_path,
                    **sim_fields,
                )
            except Exception as exc:  # noqa: BLE001
                if not getattr(args, "json", False):
                    print(f"[search] WARN: could not persist sim result: "
                          f"{type(exc).__name__}: {exc}", flush=True)

        games = getattr(ab, "games", 0) or 0
        wins_a = getattr(ab, "wins_a", None)
        wins_b = getattr(ab, "wins_b", None)
        # Same DISPLAY convention (and the same documented divergence
        # from the knowledge_log columns) as _default_round_fn: these
        # are wins / ALL games, CLI progress display only, never pooled
        # with the DB's decisive-denominator columns.
        wr_old = round(wins_a / games, 4) if games and wins_a is not None else None
        wr_new = round(wins_b / games, 4) if games and wins_b is not None else None
        margin = (wins_b - wins_a) if (wins_a is not None and wins_b is not None) else None

        return RoundResult(
            round=round_no,
            input_deck=str(deck_path),
            output_deck=str(out_path),
            verdict=verdict if getattr(ab, "status", None) == "done" else "pending",
            advanced=False,  # run_improve_loop decides, exactly as before
            iteration_id=iteration_id,
            win_rate_old=wr_old,
            win_rate_new=wr_new,
            margin=margin,
            applied_adds=applied_adds,
            applied_cuts=applied_cuts,
        )

    return round_fn
