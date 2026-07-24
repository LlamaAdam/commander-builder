"""commander-improve — greedy single-deck improvement loop (FP-012, slice 1).

The bounded first slice of FP-012 (the autonomous deck-improvement
agent). Runs the existing ``commander-auto-curate`` pipeline (advisor →
Claude curator → apply → Forge A/B sim → knowledge_log) on ONE deck for
``--rounds N`` iterations, advancing **greedily**: a round's proposed
deck becomes the base for the next round *only* when the seat-attributed
A/B sim verdict is ``kept`` (the new deck won by the configured margin).
``reverted`` / ``neutral`` / ``pending`` rounds keep the current base —
the candidate ``.dck`` is left on disk but not built upon. That's the
greedy keep-if-better contract.

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
from .intent import Intent, intent_protect_cards, learn_intent
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
    verdict: str  # kept / reverted / neutral / pending / no-op / error
    advanced: bool  # did the greedy base move forward this round?
    iteration_id: Optional[int] = None
    win_rate_old: Optional[float] = None
    win_rate_new: Optional[float] = None
    margin: Optional[int] = None
    applied_adds: int = 0
    applied_cuts: int = 0
    error: Optional[str] = None


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
    if args.sim_fillers:
        argv += ["--sim-fillers", args.sim_fillers]
    if args.db_path:
        argv += ["--db-path", args.db_path]
    for card in protect_cards:
        argv += ["--protect", card]
    if args.protect_from:
        argv += ["--protect-from", args.protect_from]
    # Soft-bias: pass the intent's themes as --intent-themes so the
    # advisor candidate pool is ranked toward those EDHREC tag pages.
    # Only appended when themes are non-empty — the no-themes path
    # must be identical to pre-Slice-A behavior.
    if intent is not None and intent.themes:
        argv += ["--intent-themes", ",".join(intent.themes)]

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


def run_improve_loop(
    deck_path: Path,
    deck_id: str,
    rounds: int,
    args,
    *,
    round_fn: Callable[[Path, int, object], RoundResult] = _default_round_fn,
) -> ImproveResult:
    """Greedy keep-if-better loop over ``rounds`` auto-curate rounds.

    The loop is intentionally pure of pipeline detail: it calls
    ``round_fn`` per round (default composes auto-curate) and only ever
    advances the base deck on a ``kept`` verdict. Injecting ``round_fn``
    lets tests drive the loop with scripted verdicts and never touch
    Forge / Anthropic.

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

        # Greedy advance: only build on the new deck when it won.
        if rr.verdict == "kept" and rr.output_deck:
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
    print(f"[improve] intent: {'; '.join(parts)}", flush=True)


def _print_summary(result: ImproveResult) -> None:
    """Human-readable run summary."""
    print()
    print(f"Improve run on {result.deck_id}")
    print(f"  start deck:  {Path(result.start_deck).name}")
    print(f"  final deck:  {Path(result.final_deck).name}")
    print(
        f"  rounds:      {result.rounds_run}/{result.rounds_requested} run, "
        f"{result.rounds_kept} kept"
        + ("  (converged — a round proposed no changes)" if result.converged else "")
    )
    print()
    for rr in result.history:
        marker = "+" if rr.advanced else " "
        wr = ""
        if rr.win_rate_old is not None and rr.win_rate_new is not None:
            wr = f"  old={rr.win_rate_old:.0%} new={rr.win_rate_new:.0%} (Δ{rr.margin:+d})"
        line = (
            f"  [{marker}] round {rr.round}: {rr.verdict}"
            f"  +{rr.applied_adds}/-{rr.applied_cuts}{wr}"
        )
        if rr.iteration_id is not None:
            line += f"  iter#{rr.iteration_id}"
        if rr.error:
            line += f"  ERROR: {rr.error}"
        print(line)
    print()
    if result.final_deck != result.start_deck:
        print(f"Best deck: {result.final_deck}")
    else:
        print("No round improved the deck; base unchanged.")


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


def _make_swap_evaluator(state: dict, args):
    """Build the real per-arm evaluator: apply one swap to the current
    best deck, A/B-sim it, and return the seat-attributed win margin as
    the reward. On a positive margin the candidate becomes the new base
    (greedy accept), so later pulls build on improvements.

    ``state`` is a mutable ``{"deck": Path}`` the closure advances. Never
    raises — sim/filler failures map to a 0.0 reward.
    """
    from .proposer import Proposal, apply_proposal_to_deck
    from .forge_runner import run_ab_simulation
    from ._proposer_sim import _pick_filler_decks

    def evaluate(arm) -> float:
        base = state["deck"]
        proposal = Proposal(
            adds=[arm.add] if arm.add else [],
            cuts=[arm.cut] if arm.cut else [],
        )
        try:
            candidate = apply_proposal_to_deck(base, proposal, dry_run=False)
        except Exception:  # noqa: BLE001
            return 0.0
        deck_dir = base.parent
        if args.sim_fillers:
            fillers = [f.strip() for f in args.sim_fillers.split(",") if f.strip()]
        else:
            fillers = _pick_filler_decks(
                deck_dir, exclude_paths=[base, candidate], count=2,
                target_bracket=args.bracket,
            )
        if len(fillers) < 2:
            return 0.0
        ab = run_ab_simulation(
            deck_a_path=base, deck_b_path=candidate,
            games=args.sim_games, fillers=fillers,
        )
        if getattr(ab, "status", None) != "done":
            return 0.0
        reward = float((ab.wins_b or 0) - (ab.wins_a or 0))
        if reward >= args.sim_margin:
            state["deck"] = candidate  # advance the base deck
        return reward

    return evaluate


def _print_bandit_summary(deck_id: str, result, final_deck: Path) -> None:
    print()
    print(f"Bandit improve run on {deck_id} ({result.rounds_run} pulls, "
          f"{result.accepted} accepted)")
    print(f"  best swap:  {result.best_arm_key} (mean reward "
          f"{result.best_arm_mean:+.2f})")
    print(f"  final deck: {final_deck.name}")
    print()
    print("  Arm stats (by mean reward):")
    for a in result.arm_stats:
        if a["pulls"]:
            print(f"    {a['mean']:+.2f}  ({a['pulls']}x)  {a['key']}")


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
    result = run_bandit(
        arms, args.rounds, evaluate, policy,
        accept_threshold=args.sim_margin, rng=random.Random(),
    )

    if args.json:
        out = result.to_dict()
        out["deck_id"] = deck_id
        out["final_deck"] = str(state["deck"])
        print(json.dumps(out, indent=2))
    else:
        _print_bandit_summary(deck_id, result, state["deck"])
    return 0


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
    p.add_argument("--mode", choices=["polish", "overhaul", "free"],
                   default="polish", help="Curation intensity (default polish).")
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
                   help="Min (wins_new - wins_old) margin to call 'kept' "
                        "(default 1). Within margin = neutral.")
    p.add_argument("--sim-fillers", default=None,
                   help="Comma-separated filler .dck filenames for the pod. "
                        "Default: auto-pick 2 bracket-matched opponents.")
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
                        "individual swaps via a multi-armed-bandit policy.")
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
    # Intent learning (FP-012 Slice A).
    p.add_argument("--learn-intent", dest="learn_intent_path",
                   type=Path, default=None, metavar="DCK",
                   help="Path to a .dck file whose intent (archetype, themes, "
                        "key win-cons) is learned before the improve loop "
                        "starts. The intent's key win-cons are added to the "
                        "protected-card list (auto-protect) and its themes "
                        "serve as a soft bias on candidate adds. Intent is "
                        "advisory: the win-margin objective remains primary.")
    args = p.parse_args(argv)

    # --health short-circuits: report the FP-013 gate counter and exit.
    # Every improve run grows this number, so surfacing it here keeps the
    # gate visible exactly where the data gets generated.
    if args.health:
        from .knowledge_log import DEFAULT_DB_PATH, fp013_gate_progress
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
                f"with an audit manifest)",
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
    if args.learn_intent_path is not None:
        intent_src = args.learn_intent_path.resolve()
        if not intent_src.exists():
            print(f"ERROR: --learn-intent deck not found: {intent_src}", flush=True)
            return 2
        if not args.json:
            print(f"[improve] learning intent from {intent_src.name} ...", flush=True)
        try:
            args.intent = learn_intent(intent_src)
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

    if not args.json:
        search_note = (f", search-budget={args.search_budget} pulls/round"
                       if args.search_budget else "")
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
