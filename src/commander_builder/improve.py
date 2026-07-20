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
from ._proposer_sim import MIN_DECISIVE_GAMES_FOR_VERDICT

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
    p.add_argument("--rounds", type=int, required=True,
                   help="Number of improve rounds to attempt (>= 1). The "
                        "loop stops early if a round proposes no changes.")
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
    # Default 25, NOT 5: verdicts below MIN_DECISIVE_GAMES_FOR_VERDICT
    # (=20) decisive games are gated to 'inconclusive', and the greedy
    # loop only advances on 'kept' -- so a 5-game default made improve
    # structurally unable to ever advance the base deck while still
    # burning Forge + LLM time every round. 25 leaves headroom for a
    # few draws and still clears the 20-decisive gate.
    p.add_argument("--sim-games", type=int, default=25,
                   help="Games per A/B sim each round (default 25). "
                        "Verdicts need >= 20 decisive (non-draw) games "
                        "to resolve to kept/reverted/neutral; fewer is "
                        "recorded 'inconclusive' and the round cannot "
                        "advance the deck. NOTE the runtime cost: 25 "
                        "Forge pod games per round is roughly 5x the "
                        "old 5-game default -- budget on the order of "
                        "an hour per round, not minutes.")
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
                        "(default 1.4).")
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

    # Exactly one of {positional path, --deck id} must be supplied.
    if (args.deck_path is None) == (args.deck_id is None):
        print("ERROR: pass either a deck_path positional OR --deck <id>, "
              "not both / neither.", flush=True)
        return 2

    if args.rounds < 1:
        print(f"ERROR: --rounds must be >= 1, got {args.rounds}", flush=True)
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

    # LOUD up-front warning before any Forge/LLM time is spent: below
    # the min-decisive gate every round's verdict is structurally
    # 'inconclusive', and the greedy loop advances ONLY on 'kept', so
    # the whole run cannot ever move the base deck forward. On stderr
    # so --json stdout stays machine-parseable and the warning is
    # visible even when stdout is piped.
    if args.sim_games < MIN_DECISIVE_GAMES_FOR_VERDICT:
        print(
            f"[improve] WARNING: --sim-games {args.sim_games} < "
            f"{MIN_DECISIVE_GAMES_FOR_VERDICT} (MIN_DECISIVE_GAMES_FOR_"
            f"VERDICT): every round's verdict will be 'inconclusive', "
            f"and improve only advances the deck on 'kept' -- this run "
            f"CANNOT improve the deck, it will only burn Forge/LLM "
            f"time. Pass --sim-games >= {MIN_DECISIVE_GAMES_FOR_VERDICT} "
            f"(default 25) to let verdicts resolve.",
            file=sys.stderr, flush=True,
        )

    if not args.json:
        print(f"[improve] {deck_id} (B{args.bracket}) -- strategy={args.strategy}, "
              f"up to {args.rounds} rounds, mode={args.mode}, "
              f"{args.sim_games} games/round", flush=True)

    if args.strategy == "bandit":
        return _run_bandit_strategy(deck_path, deck_id, args)

    result = run_improve_loop(deck_path, deck_id, args.rounds, args)

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
