"""Command-line entry point commander-auto-curate.

End-to-end unattended pipeline: advisor -> Claude curator -> apply ->
optional Forge A/B sim -> knowledge_log row. Split out of proposer
on 2026-05-16 (Tier-3 refactor); re-exported from proposer for
back-compat.

Batch mode (added 2026-05-19, AGENT_BACKLOG #011): passing ``--batch
<glob>`` resolves the glob to multiple .dck files and runs the
single-deck pipeline over each in sequence. JSON output is forced
on per deck and the run aggregates everything into one NDJSON stream
(one JSON object per line). Already-versioned decks are skipped
unless ``--force`` is passed so a re-run picks up where the previous
batch left off without redoing API spend.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path
from typing import Optional

from .proposer import (
    auto_propose,
    apply_proposal_to_deck,
)
from ._proposer_sim import (
    _log_auto_curate_iteration,
    _run_sim_and_record,
)


def auto_curate_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``commander-auto-curate``.

    End-to-end unattended pipeline:
      1. Run the improvement advisor on the deck (sync EDHREC fetch).
      2. Hand the AdviceReport to ``auto_propose()`` so Claude curates
         it down to a small, applicable proposal.
      3. Apply the proposal to a new versioned .dck file (or print it
         under ``--dry-run`` and leave disk untouched).
      4. Print a summary the user can scan when the overnight batch
         lands.

    Exits non-zero with a clear message on missing key, missing SDK,
    or unparseable Claude response so a batch driver can skip the
    deck rather than misinterpret silent zero-changes as success.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="commander-auto-curate",
        description=(
            "Run advisor -> Claude curator -> apply, all in one go. "
            "Designed for unattended overnight batch refinement."
        ),
    )
    # ``deck_path`` is optional so ``--batch <glob>`` can substitute
    # for a single positional path. Exactly one of the two MUST be
    # supplied; validated below after argparse.
    p.add_argument("deck_path", type=Path, nargs="?", default=None,
                   help="Path to the .dck file to audit. Omit when "
                        "using --batch.")
    p.add_argument("--bracket", type=int, required=True,
                   help="Target bracket (1-5). Drives game-changer enforcement.")
    p.add_argument("--batch", default=None, metavar="GLOB",
                   help="Run the pipeline over every .dck file matching "
                        "the glob (e.g. 'vendor/forge/userdata/decks/"
                        "commander/[USER]*[B4].dck'). Forces --json on; "
                        "emits one JSON object per line on stdout, plus "
                        "a final summary record under key 'batch_summary'. "
                        "Already-versioned decks (those whose 'v<N+1>' "
                        "sibling exists) are skipped unless --force is "
                        "passed -- safe to re-run an interrupted batch "
                        "without redoing API spend. Per-deck failures "
                        "are caught and recorded; one bad deck doesn't "
                        "abort the rest.")
    p.add_argument("--force", action="store_true",
                   help="In --batch mode, re-curate decks that already "
                        "have a versioned sibling. Default: skip them "
                        "so an interrupted batch resumes cleanly.")
    p.add_argument("--parallelism", type=int, default=1,
                   help="Batch-mode worker count (default 1 = sequential). "
                        "When > 1, runs that many decks through the "
                        "pipeline concurrently via a ThreadPoolExecutor. "
                        "Each worker spawns its own Forge JVM for "
                        "--run-sim — verified safe via the 2026-05-19 "
                        "feasibility spike (see scripts/_spike_concurrent_"
                        "forge.py): two JVMs co-exist in the same Forge "
                        "install cwd with no lock contention. A 2-deck "
                        "batch ran in 41%% less wall time than sequential "
                        "in the spike. Anthropic curator calls also run "
                        "in parallel; their rate-limit is high enough "
                        "that 2-4 workers is comfortable. Per-thread "
                        "stdout is serialized so the NDJSON stream "
                        "stays parseable.")
    p.add_argument(
        "--mode", choices=["polish", "overhaul", "free"], default="polish",
        help=(
            "Curation intensity preset (default 'polish'). "
            "polish=5 adds + 5 cuts (safe for unattended overnight runs). "
            "overhaul=15 + 15 (deliberate major revision). "
            "free=unbounded (trust Claude to pick the right count). "
            "Override individual caps with --max-adds / --max-cuts."
        ),
    )
    # Defaults are None so we can tell whether the user passed an
    # explicit cap (which overrides the mode preset) or left it at
    # the mode's recommended value. argparse-of-the-classics: a
    # sentinel beats reading sys.argv directly.
    p.add_argument("--max-adds", type=int, default=None,
                   help="Hard cap on returned adds. Overrides --mode's "
                        "add cap when set. Default: preset value for "
                        "the active --mode (polish=5, overhaul=15, "
                        "free=999).")
    p.add_argument("--max-cuts", type=int, default=None,
                   help="Hard cap on returned cuts. Same override "
                        "semantics as --max-adds.")
    p.add_argument("--source", default="heuristic",
                   choices=["heuristic", "bracket_peers", "claude"],
                   help="Advisor backend (default heuristic).")
    p.add_argument("--model", default="claude-sonnet-4-5",
                   help="Anthropic model id for the curator step.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the proposal but don't write the new .dck.")
    p.add_argument("--json", action="store_true",
                   help="Emit the proposal as JSON on stdout instead "
                        "of human-readable summary.")
    p.add_argument("--no-log", action="store_true",
                   help="Skip writing the iteration to knowledge_log "
                        "(default: persist a pending iteration row).")
    p.add_argument("--db-path",
                   help="Override the knowledge_log SQLite path "
                        "(default: vendor/knowledge_log.sqlite).")
    p.add_argument("--protect", action="append", default=[],
                   metavar="CARD",
                   help="Lock a card against cuts. Repeatable. Unioned "
                        "with [metadata] Protect= entries in the .dck "
                        "and any --protect-from file.")
    p.add_argument("--protect-from", default=None, metavar="PATH",
                   help="Path to a file with one card name per line, "
                        "all protected against cuts. Unioned with "
                        "--protect and [metadata] Protect=.")
    p.add_argument("--run-sim", action="store_true",
                   help="After applying the proposal, run a Forge A/B "
                        "head-to-head between the old and new deck and "
                        "record the verdict (kept/reverted/neutral) in "
                        "the knowledge_log. Closes the loop -- the "
                        "iteration row's verdict reflects empirical sim "
                        "results, not a permanent 'pending'. Skipped "
                        "automatically under --dry-run or --no-log "
                        "(no row to update).")
    p.add_argument("--sim-games", type=int, default=5,
                   help="Games per A/B sim (default 5). The harness "
                        "alternates seat order, so total games = 2 * "
                        "this number isn't quite right -- it's exactly "
                        "this number, half with old in seat 1.")
    p.add_argument("--sim-fillers", default=None,
                   help="Comma-separated filenames (relative to "
                        "deck_dir) of filler decks for the 4-player "
                        "Commander pod. Default: auto-pick 2 from the "
                        "opponent pool (non-[USER] .dck files in "
                        "deck_dir).")
    p.add_argument("--sim-margin", type=int, default=_DEFAULT_SIM_MARGIN,
                   help="Minimum (wins_new - wins_old) margin to call "
                        "'kept'. Mirrored for 'reverted'. Within "
                        "margin = neutral. Default 1.")
    # Intent-theme bias (FP-012 Slice A soft-bias finish).
    # Comma-separated EDHREC tag-page slugs that the deck's intent
    # identified as its themes (e.g. "tokens,aristocrats").  When
    # provided, these slugs are prepended to the tag-page fetch list
    # inside the advisor so candidates sourced from those archetype
    # pages are ranked first.  Empty / absent = no bias (identical
    # behavior to today).  The flag is additive: auto-detected themes
    # from oracle-text scanning still run after these.
    p.add_argument("--intent-themes", default=None, metavar="SLUGS",
                   help="Comma-separated EDHREC tag-page slugs from the "
                        "deck's learned intent (e.g. 'tokens,aristocrats'). "
                        "Soft-biases candidate adds toward those archetypes. "
                        "Empty / absent = no bias.")
    args = p.parse_args(argv)

    # Exactly one of {deck_path, --batch} must be provided. Reject
    # both-set and neither-set up front so the user gets a clean
    # error instead of a confused single-deck attempt with batch
    # flags hanging around (or vice versa).
    if (args.deck_path is None) == (args.batch is None):
        print(
            "ERROR: pass either a deck_path positional OR --batch <glob>, "
            "not both / neither.",
            flush=True,
        )
        return 2

    # Load the external credentials file (~/.commander-builder/credentials
    # or $COMMANDER_BUILDER_CREDENTIALS) BEFORE any code reads
    # os.environ["ANTHROPIC_API_KEY"]. Shell env wins if both are set,
    # so production deployments using container secrets are unaffected.
    # Silent when no file exists (some users prefer shell-only); a hint
    # surfaces to stderr only if they later hit a "key missing" error.
    from ._secrets import load_credentials
    load_credentials(quiet=True)

    # Bracket validation applies to both modes (single + batch).
    if not (1 <= args.bracket <= 5):
        print(f"ERROR: bracket must be 1-5, got {args.bracket}", flush=True)
        return 2

    # Batch mode short-circuits here: dispatch to the loop and exit.
    # The loop calls back into auto_curate_main for each resolved deck
    # so the per-deck pipeline is unchanged.
    if args.batch is not None:
        return _run_batch(args, p, argv or sys.argv[1:])

    # Resolve to absolute path BEFORE handing to the advisor. The
    # advisor treats relative paths as deck_dir-relative and prepends
    # its own deck_dir, which double-prefixes when the user passes a
    # path already inside vendor/forge/userdata/decks/commander/.
    # Same fix as scripts/compare_curator_modes.py — keeps both paths
    # consistent.
    args.deck_path = args.deck_path.resolve()
    if not args.deck_path.exists():
        print(f"ERROR: deck not found: {args.deck_path}", flush=True)
        return 2

    # Resolve effective caps from the mode preset + any explicit
    # overrides. The preset is the discoverable default ("I want a
    # polish run / overhaul / let Claude decide") and the explicit
    # flags are the fine-tune for users who want a specific number.
    _MODE_CAPS = {
        "polish":   (5,   5),    # conservative; safe for unattended
        "overhaul": (15,  15),   # deliberate major revision
        "free":     (999, 999),  # effectively unbounded
    }
    preset_adds, preset_cuts = _MODE_CAPS[args.mode]
    effective_max_adds = args.max_adds if args.max_adds is not None else preset_adds
    effective_max_cuts = args.max_cuts if args.max_cuts is not None else preset_cuts
    if effective_max_adds < 0 or effective_max_cuts < 0:
        print(
            f"ERROR: --max-adds / --max-cuts must be non-negative, "
            f"got adds={effective_max_adds} cuts={effective_max_cuts}",
            flush=True,
        )
        return 2
    if not args.json:
        if (args.max_adds is None and args.max_cuts is None):
            print(
                f"      mode={args.mode!r} -> up to {effective_max_adds} "
                f"adds and {effective_max_cuts} cuts",
                flush=True,
            )
        else:
            print(
                f"      mode={args.mode!r} + overrides -> "
                f"max adds={effective_max_adds}, max cuts={effective_max_cuts}",
                flush=True,
            )

    # Step 1: advisor. Imported lazily so the CLI startup stays cheap when
    # the user only wanted --help.
    from .improvement_advisor import advise
    if not args.json:
        print(f"[1/3] Running advisor on {args.deck_path.name} (B{args.bracket})...",
              flush=True)
    # Parse --intent-themes into a slug list (comma-separated, stripped).
    # Empty string / absent flag both produce an empty list so no-bias
    # path is identical to the pre-FP-012-slice-A behavior.
    _raw_intent_themes = args.intent_themes or ""
    _intent_themes: list[str] = [
        s.strip() for s in _raw_intent_themes.split(",") if s.strip()
    ]
    report = advise(
        deck_path=args.deck_path,
        bracket=args.bracket,
        source=args.source,
        intent_themes=_intent_themes if _intent_themes else None,
    )
    advice_dict = report.to_manifest()
    candidate_add_count = len(advice_dict.get("added", []))
    candidate_cut_count = len(advice_dict.get("removed", []))
    if not args.json:
        print(f"      advisor produced {candidate_add_count} candidate adds, "
              f"{candidate_cut_count} candidate cuts", flush=True)

    # Resolve the protected-cards set from all three sources:
    #   - [metadata] Protect= entries in the .dck (persistent, per-deck)
    #   - --protect CLI flag (repeatable, ad-hoc override)
    #   - --protect-from <file> (bulk reusable list)
    # Order-preserving union so the prompt + summary read in a stable
    # order; case-insensitive dedup.
    from .web._helpers import read_protected_cards
    protected_combined: list[str] = []
    seen_lower: set[str] = set()
    def _add_protected(name: str) -> None:
        n = name.strip()
        if not n:
            return
        key = n.lower()
        if key in seen_lower:
            return
        seen_lower.add(key)
        protected_combined.append(n)

    deck_text_for_protect = args.deck_path.read_text(encoding="utf-8")
    for c in read_protected_cards(deck_text_for_protect):
        _add_protected(c)
    for c in args.protect:
        _add_protected(c)
    if args.protect_from:
        pf = Path(args.protect_from)
        if not pf.exists():
            print(f"ERROR: --protect-from file not found: {pf}", flush=True)
            return 2
        for line in pf.read_text(encoding="utf-8").splitlines():
            _add_protected(line)

    if not args.json and protected_combined:
        print(f"      {len(protected_combined)} protected cards locked "
              f"against cuts", flush=True)

    # Step 2: curator.
    if not args.json:
        print(f"[2/3] Curating via {args.model}...", flush=True)
    try:
        proposal = auto_propose(
            deck_path=args.deck_path,
            bracket=args.bracket,
            advice_report=advice_dict,
            max_adds=effective_max_adds,
            max_cuts=effective_max_cuts,
            model=args.model,
            protected_cards=protected_combined,
            mode=args.mode,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 3

    # Step 3: apply (or dry-run).
    if not args.json:
        verb = "would write" if args.dry_run else "writing"
        print(f"[3/3] {verb} new .dck...", flush=True)
    out_path = apply_proposal_to_deck(
        args.deck_path, proposal, dry_run=args.dry_run,
    )

    # Step 3b: persist iteration to knowledge_log. Skipped on dry-run
    # (no actual deck to point at) and on --no-log (opt-out). Failures
    # to write the log are NON-fatal -- the .dck is already on disk;
    # the user shouldn't lose that work because of a knowledge_log
    # quirk. Surface the failure in the summary instead.
    iteration_id: Optional[int] = None
    log_error: Optional[str] = None
    if not args.dry_run and not args.no_log:
        try:
            iteration_id = _log_auto_curate_iteration(
                src_deck_path=args.deck_path,
                new_deck_path=out_path,
                bracket=args.bracket,
                proposal=proposal,
                db_path=Path(args.db_path) if args.db_path else None,
            )
        except Exception as exc:  # noqa: BLE001
            log_error = f"{type(exc).__name__}: {exc}"

    # Step 4: optional Forge A/B sim. Closes the loop -- the iteration
    # row we just wrote has verdict='pending'; this fills it in with
    # the actual head-to-head result. Skipped under --dry-run (no
    # row exists), --no-log (we'd have nowhere to write), and the
    # bare default (opt-in via --run-sim). When the user opts in,
    # this can take 5-15 minutes depending on game count.
    sim_result_payload = None
    sim_error: Optional[str] = None
    sim_verdict: Optional[str] = None
    if (args.run_sim
            and not args.dry_run
            and not args.no_log
            and iteration_id is not None):
        sim_result_payload, sim_error, sim_verdict = _run_sim_and_record(
            args=args,
            out_path=out_path,
            iteration_id=iteration_id,
            db_path=Path(args.db_path) if args.db_path else None,
        )
    elif args.run_sim and args.dry_run and not args.json:
        print("[sim] --run-sim ignored under --dry-run (no deck on disk "
              "to test).", flush=True)
    elif args.run_sim and args.no_log and not args.json:
        print("[sim] --run-sim ignored under --no-log (no iteration row "
              "to update with the result).", flush=True)

    if args.json:
        print(json.dumps({
            "input_deck": str(args.deck_path),
            "output_deck": str(out_path),
            "dry_run": args.dry_run,
            "mode": args.mode,
            "max_adds": effective_max_adds,
            "max_cuts": effective_max_cuts,
            "proposal": proposal.to_dict(),
            "iteration_id": iteration_id,
            "log_error": log_error,
            # Sim block. ``sim_run=True`` only when --run-sim AND the
            # sim actually executed (not dry-run, not no-log, has
            # fillers). verdict is the kept/reverted/neutral/pending
            # outcome; sim_report carries the ABResult dict for
            # downstream analysis.
            "sim_run": sim_result_payload is not None or sim_error is not None,
            "sim_verdict": sim_verdict,
            "sim_report": sim_result_payload,
            "sim_error": sim_error,
        }, indent=2))
        return 0

    print()
    # Surface what Claude REQUESTED vs what actually LANDED. The two
    # can differ when adds and cuts are unbalanced -- apply_proposal_
    # to_deck slices both to min() so the deck stays the right size.
    print(f"Adds requested ({len(proposal.adds)}) -> applied ({len(proposal.applied_adds)}):")
    for c in proposal.applied_adds:
        print(f"  + {c}")
    print(f"Cuts requested ({len(proposal.cuts)}) -> applied ({len(proposal.applied_cuts)}):")
    for c in proposal.applied_cuts:
        print(f"  - {c}")
    if proposal.dropped_for_bracket:
        print(f"Dropped for B{args.bracket} (game-changers): "
              f"{len(proposal.dropped_for_bracket)}")
        for c in proposal.dropped_for_bracket:
            print(f"  ! {c}")
    if proposal.dropped_for_protection:
        print(f"Dropped because protected (user-locked): "
              f"{len(proposal.dropped_for_protection)}")
        for c in proposal.dropped_for_protection:
            print(f"  [LOCKED] {c}")
    if proposal.dropped_for_color_identity:
        print(f"Dropped for color identity (off-color): "
              f"{len(proposal.dropped_for_color_identity)}")
        for c in proposal.dropped_for_color_identity:
            print(f"  ! {c}")
    if proposal.dropped_for_balance:
        print(f"Dropped to keep deck size legal "
              f"(adds/cuts unbalanced): {len(proposal.dropped_for_balance)}")
        for c in proposal.dropped_for_balance:
            print(f"  ~ {c}")
    # Pair-drops from apply-time decklist validation. Each entry is a
    # {"cut": ..., "add": ...} dict — the pair was dropped as a unit so
    # the deck stays at a legal 99 mainboard (see apply_proposal_to_deck).
    if proposal.dropped_unmatched_cut:
        print(f"Dropped swap pairs (cut not found in decklist): "
              f"{len(proposal.dropped_unmatched_cut)}")
        for pair in proposal.dropped_unmatched_cut:
            print(f"  ~ -{pair['cut']} / +{pair['add']}")
    if proposal.dropped_duplicate_add:
        print(f"Dropped swap pairs (add already in deck, singleton rule): "
              f"{len(proposal.dropped_duplicate_add)}")
        for pair in proposal.dropped_duplicate_add:
            print(f"  ~ -{pair['cut']} / +{pair['add']}")
    if proposal.dropped_commander_add:
        print(f"Dropped swap pairs (add is the commander): "
              f"{len(proposal.dropped_commander_add)}")
        for pair in proposal.dropped_commander_add:
            print(f"  ~ -{pair['cut']} / +{pair['add']}")
    if proposal.padded_count:
        breakdown_str = ", ".join(
            f"{n}x {b}" for b, n in proposal.padded_breakdown.items()
        )
        print(f"Padded with basics: +{proposal.padded_count} ({breakdown_str})")
    print()
    print(f"Rationale: {proposal.rationale}")
    print()
    if args.dry_run:
        print(f"DRY RUN -- would have written: {out_path}")
    else:
        print(f"Wrote: {out_path}")
        if iteration_id is not None:
            initial_verdict = sim_verdict or "pending"
            print(f"Logged iteration #{iteration_id} ({initial_verdict})")
        elif args.no_log:
            print("(skipped knowledge_log per --no-log)")
        elif log_error:
            # Non-fatal: deck is on disk, history just lost this row.
            print(f"WARN: knowledge_log write failed: {log_error}")

        # Sim summary block. Only print when the sim actually ran or
        # was attempted -- silence when --run-sim wasn't passed at all.
        if sim_result_payload is not None or sim_error is not None:
            print()
            if sim_error:
                print(f"A/B sim: {sim_error}")
            else:
                wa = sim_result_payload.get("wins_a", 0)
                wb = sim_result_payload.get("wins_b", 0)
                games = sim_result_payload.get("games", 0)
                status = sim_result_payload.get("status", "?")
                avg_a = sim_result_payload.get("avg_turns_a", 0)
                avg_b = sim_result_payload.get("avg_turns_b", 0)
                print(f"A/B sim ({status}): old={wa} wins, new={wb} wins "
                      f"({games} games)")
                if avg_a or avg_b:
                    print(f"  avg-turns-to-win: old={avg_a}, new={avg_b}")
                if sim_verdict:
                    print(f"  verdict: {sim_verdict}")
    return 0


# ---------------------------------------------------------------------------
# Batch mode (AGENT_BACKLOG #011) -- iterate over a glob of decks
# ---------------------------------------------------------------------------

def _already_versioned(deck_path: Path) -> bool:
    """Return True when ``<deck_path>``'s next-version sibling already
    exists on disk (e.g. ``v2 [B4].dck`` exists alongside ``[B4].dck``).

    Used by batch mode to skip decks that a prior run already curated,
    so a re-invocation of the same ``--batch`` command resumes cleanly
    instead of redoing API spend on already-done decks. The user can
    force re-curation with ``--force``.
    """
    from .proposer import _bump_version_filename
    next_name = _bump_version_filename(deck_path.name)
    return (deck_path.parent / next_name).exists()


def _resolve_batch_glob(pattern: str) -> list[Path]:
    """Expand a batch glob into a sorted list of .dck paths.

    Uses ``glob.glob`` with ``recursive=True`` so users can pass
    ``**/*.dck``-style patterns. Filters to .dck files only (the
    pipeline can't audit anything else) and resolves to absolute
    paths up front so the per-deck argv is unambiguous.

    **Bracket-literal handling**: this project's deck filenames use
    ``[USER]`` and ``[B<N>]`` markers liberally, which collide with
    glob's character-class syntax (``[USER]`` would match a single
    char from {U,S,E,R}, not the literal string). We split the
    pattern on ``*``/``?`` wildcards, escape literal segments via
    ``glob.escape`` so brackets become ``[[]USER[]]`` (glob's
    documented literal-bracket form), then rejoin. The wildcards
    are preserved because they're isolated in odd-indexed split
    pieces.
    """
    import re as _re
    parts = _re.split(r"([*?])", pattern)
    escaped = "".join(
        glob.escape(part) if i % 2 == 0 else part
        for i, part in enumerate(parts)
    )
    matched = [
        Path(p).resolve() for p in glob.glob(escaped, recursive=True)
        if p.lower().endswith(".dck")
    ]
    matched.sort()
    return matched


def _value_taking_flags(parser) -> frozenset[str]:
    """Return every option string of ``parser`` that consumes a
    following value token (``--flag value`` form).

    Derived from ``parser._actions`` instead of a hardcoded list so a
    newly added flag can never silently rot the batch argv rewriter
    (the original hardcode-free motivation: ``_build_per_deck_argv``
    must know which flags take values to avoid mistaking a flag's
    VALUE for the deck positional). ``_actions`` is argparse's
    stable-in-practice internal — every action added via
    ``add_argument`` lands there with its ``option_strings`` and
    ``nargs`` intact.

    ``nargs == 0`` covers store_true/store_false/count/help — flags
    that consume NO value. Everything else (nargs None = exactly one
    value, and any explicit nargs) consumes at least the next token,
    which is all the rewriter needs to know.
    """
    flags: set[str] = set()
    for action in parser._actions:
        if not action.option_strings:
            continue  # positional (deck_path) — not a flag
        if action.nargs == 0:
            continue  # store_true-style: no value token follows
        flags.update(action.option_strings)
    return frozenset(flags)


def _build_per_deck_argv(
    deck_path: Path, batch_argv: list[str], value_flags: frozenset[str],
) -> list[str]:
    """Construct the argv for a single-deck recursive call by stripping
    batch-only flags from the batch invocation and substituting the
    resolved deck path.

    Strips: ``--batch <glob>`` (and its value), ``--force``. Leaves
    every other flag intact (--bracket, --mode, --run-sim, --max-adds,
    --max-cuts, --source, --model, --dry-run, --no-log, --db-path,
    --protect*, --sim-*).

    ``value_flags`` (from :func:`_value_taking_flags`) makes the walk
    flag-AWARE: when a flag that takes a value appears in the
    ``--flag value`` two-token form, the value token is copied through
    verbatim rather than being considered as a positional candidate.
    Without this, flag values that happen to end in ``.dck`` — e.g.
    ``--sim-fillers "PodA.dck,PodB.dck"`` (the documented value shape)
    or ``--protect-from list.dck`` — were stripped as "the positional
    deck token", leaving a dangling flag that argparse then fed the
    NEXT unrelated token (or errored on).
    """
    out: list[str] = []
    i = 0
    saw_positional = False
    while i < len(batch_argv):
        tok = batch_argv[i]
        if tok == "--batch":
            i += 2  # skip the flag + its value
            continue
        if tok.startswith("--batch="):
            i += 1
            continue
        if tok == "--force":
            i += 1
            continue
        if tok.startswith("-") and tok != "-":
            # Flag token. The '--flag=value' one-token form carries its
            # value inline, so copying the single token is always safe.
            # The '--flag value' two-token form must copy BOTH tokens
            # here so the value is never examined by the positional
            # check below (a value ending in .dck is NOT the deck).
            out.append(tok)
            if "=" not in tok and tok in value_flags and i + 1 < len(batch_argv):
                out.append(batch_argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        # Non-flag token: the batch invocation's own positional? In
        # batch mode the user shouldn't have passed one (we validated
        # above), but defensively skip the first .dck-looking token.
        if not saw_positional and tok.lower().endswith(".dck"):
            saw_positional = True
            i += 1
            continue
        out.append(tok)
        i += 1
    # Prepend the resolved per-deck path as the positional.
    return [str(deck_path), *out]


# Thread-local stdout router for batch-mode parallel dispatch.
# ``contextlib.redirect_stdout`` patches the PROCESS-GLOBAL sys.stdout
# which races catastrophically across worker threads (workers stomp
# each other's writes, NDJSON output gets corrupted). We swap sys.stdout
# once at the top of _run_batch with this proxy that dispatches each
# ``write`` / ``flush`` to a per-thread buffer if one is set,
# otherwise to the original stdout. Workers set the buffer just
# before calling auto_curate_main and clear it after — same shape as
# redirect_stdout but thread-safe.
_BATCH_THREAD_LOCAL = __import__("threading").local()


class _ThreadLocalStdoutProxy:
    """sys.stdout proxy that dispatches to a per-thread buffer when set.

    ``attr`` names the thread-local slot to consult, so the same class
    doubles as the sys.stderr proxy (slot "errbuf") — parallel workers
    need stderr captured per-thread too, because argparse writes its
    parse-error message to stderr before raising SystemExit and the
    per-deck failure record wants that message.
    """

    def __init__(self, default, attr: str = "buf"):
        self._default = default
        self._attr = attr

    def _target(self):
        return getattr(_BATCH_THREAD_LOCAL, self._attr, None) or self._default

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


def _process_one_deck(
    deck_path: Path, batch_argv: list[str], force: bool,
    value_flags: frozenset[str],
) -> dict:
    """Run the per-deck pipeline once and return the NDJSON record.

    Capture strategy depends on whether a per-thread buffer is in
    scope (set by the parallel dispatcher) — falls back to in-process
    ``contextlib.redirect_stdout`` for the sequential path where
    sys.stdout isn't proxied. Both paths produce the same record
    shape, so callers don't care which was used.

    ``value_flags`` comes from :func:`_value_taking_flags` on the batch
    parser and is threaded through to :func:`_build_per_deck_argv` so
    the argv rewrite is flag-aware.

    Returns one of three record shapes:
      ``{deck, status: 'skipped', reason}`` — already-versioned and
        --force not passed.
      ``{deck, status: 'ok'|'failed', rc, result}`` — pipeline ran;
        ``result`` is the per-deck JSON payload parsed back from stdout.
      ``{deck, status: 'error', rc: None, exception}`` — pipeline
        raised; one bad deck doesn't kill the rest of the batch.
    """
    import contextlib as _ctx
    import io as _io
    import sys as _sys

    if not force and _already_versioned(deck_path):
        return {
            "deck": str(deck_path),
            "status": "skipped",
            "reason": "already-versioned (use --force to re-curate)",
        }

    per_deck_argv = _build_per_deck_argv(deck_path, batch_argv, value_flags)
    buf = _io.StringIO()
    # stderr is captured alongside stdout so that when argparse rejects
    # a per-deck argv (it prints the error to stderr, then raises
    # SystemExit) the failure record can carry the actual message.
    errbuf = _io.StringIO()
    is_thread_local_stdout = isinstance(_sys.stdout, _ThreadLocalStdoutProxy)
    try:
        if is_thread_local_stdout:
            # Parallel path: set this thread's per-thread buffers so
            # the proxies route writes here. ``auto_curate_main``'s
            # print() calls land in ``buf``, isolated from sibling
            # workers' writes; stderr writes land in ``errbuf``.
            _BATCH_THREAD_LOCAL.buf = buf
            _BATCH_THREAD_LOCAL.errbuf = errbuf
            try:
                rc = auto_curate_main(per_deck_argv)
            finally:
                _BATCH_THREAD_LOCAL.buf = None
                _BATCH_THREAD_LOCAL.errbuf = None
        else:
            # Sequential path: redirect_* is safe (single thread).
            with _ctx.redirect_stdout(buf), _ctx.redirect_stderr(errbuf):
                rc = auto_curate_main(per_deck_argv)
    except SystemExit as exc:
        # argparse errors exit via SystemExit, which inherits
        # BaseException — the ``except Exception`` isolation clause
        # below never sees it, so pre-fix one deck's parse error
        # aborted the entire overnight batch. Convert it to a recorded
        # per-deck failure like any other error. Deliberately narrow:
        # KeyboardInterrupt (also BaseException) must still propagate
        # so Ctrl-C actually stops the batch.
        err_text = errbuf.getvalue().strip()
        return {
            "deck": str(deck_path),
            "status": "error",
            "rc": None,
            "exception": (
                f"SystemExit(code={exc.code})"
                + (f": {err_text}" if err_text else "")
            ),
        }
    except Exception as exc:  # noqa: BLE001 -- per-deck isolation
        return {
            "deck": str(deck_path),
            "status": "error",
            "rc": None,
            "exception": f"{type(exc).__name__}: {exc}",
        }
    finally:
        # Replay captured stderr onto the real stream so warnings the
        # pipeline emitted stay visible — the capture exists only so a
        # SystemExit record can include argparse's message, not to
        # swallow diagnostics. By the time this runs, the sequential
        # redirect has exited and the parallel thread-local slot is
        # cleared, so ``_sys.stderr`` resolves to the real stream.
        _captured_err = errbuf.getvalue()
        if _captured_err:
            _sys.stderr.write(_captured_err)
            _sys.stderr.flush()

    raw = buf.getvalue().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw_stdout": raw}
    return {
        "deck": str(deck_path),
        "status": "ok" if rc == 0 else "failed",
        "rc": rc,
        "result": payload,
    }


def _run_batch(args, parser, batch_argv: list[str]) -> int:
    """Iterate ``args.batch`` glob, run auto_curate_main per deck.

    JSON output is forced on so the run produces an NDJSON stream
    (one JSON object per line) plus a final ``batch_summary`` record.
    Per-deck failures are caught and recorded; one bad deck doesn't
    abort the rest.

    ``args.parallelism > 1`` dispatches per-deck work to a
    ThreadPoolExecutor. The 2026-05-19 feasibility spike
    (scripts/_spike_concurrent_forge.py) confirmed two Forge JVMs
    co-exist in the same install cwd with zero lock contention, so no
    cwd isolation is required. Per-thread stdout writes are
    serialized through a Lock so the NDJSON stream stays parseable
    even when records complete concurrently. Record order in the
    output reflects completion order, NOT glob order — downstream
    consumers should treat the stream as a multiset, not a sequence.

    Returns 0 when at least one deck succeeded (or all skipped);
    2 only when EVERY processed deck failed.
    """
    paths = _resolve_batch_glob(args.batch)
    if not paths:
        print(json.dumps({
            "batch_summary": {
                "glob": args.batch,
                "matched": 0,
                "error": "no .dck files matched",
            },
        }))
        return 2

    # Force --json on for the per-deck calls so we capture structured
    # output. If the user already passed --json, this is a no-op.
    if "--json" not in batch_argv:
        batch_argv = [*batch_argv, "--json"]

    parallelism = max(1, int(getattr(args, "parallelism", 1) or 1))

    # UX hint: when --batch resolves to >1 deck AND --run-sim is on AND
    # parallelism is the default 1, suggest bumping. Sequential Forge
    # sims dominate wall time on multi-deck overnight runs; the spike
    # showed 2-way parallelism cuts ~41% wall time with no contention.
    # One-line stderr note; no behavior change.
    if (
        len(paths) > 1
        and getattr(args, "run_sim", False)
        and parallelism == 1
    ):
        print(
            f"[batch] tip: {len(paths)} decks queued with --run-sim and "
            f"--parallelism 1. Pass --parallelism 2 (or higher) to halve "
            f"wall time — Forge JVMs co-exist safely (verified via "
            f"2026-05-19 feasibility spike).",
            file=sys.stderr,
            flush=True,
        )

    # Derive the value-taking flag set ONCE from the live parser (the
    # same object that parsed the batch argv) so the per-deck argv
    # rewrite can't mistake a flag's value for the deck positional.
    # Deriving here — rather than hardcoding flag names inside
    # _build_per_deck_argv — means a future add_argument() call is
    # automatically covered.
    value_flags = _value_taking_flags(parser)

    n_skipped = 0
    n_failed = 0
    n_succeeded = 0

    def _tally(record: dict) -> None:
        nonlocal n_skipped, n_failed, n_succeeded
        status = record.get("status")
        if status == "skipped":
            n_skipped += 1
        elif status == "ok":
            n_succeeded += 1
        else:  # failed, error
            n_failed += 1

    if parallelism == 1:
        # Sequential fast path — preserves the pre-2026-05-19 NDJSON
        # ordering contract (records emit in glob order) for users
        # who haven't opted into parallelism.
        for deck_path in paths:
            record = _process_one_deck(
                deck_path, batch_argv, args.force, value_flags,
            )
            _tally(record)
            print(json.dumps(record), flush=True)
    else:
        import concurrent.futures as _cf
        import sys as _sys
        import threading as _threading

        emit_lock = _threading.Lock()
        # Capture the REAL stdout (before the proxy swap) so the
        # batch coordinator can emit NDJSON records that bypass the
        # per-thread routing. Without this, our own _emit() writes
        # would land in a worker's thread-local buffer if a worker
        # accidentally ran on the coordinator thread.
        real_stdout = _sys.stdout
        real_stderr = _sys.stderr

        def _emit(record: dict) -> None:
            with emit_lock:
                real_stdout.write(json.dumps(record) + "\n")
                real_stdout.flush()

        # Install the thread-local stdout/stderr proxies for the
        # duration of the pool. Workers will set _BATCH_THREAD_LOCAL
        # .buf / .errbuf inside _process_one_deck so their
        # auto_curate_main writes land in per-thread buffers instead
        # of corrupting each other. stderr is proxied for the same
        # reason stdout is, plus one more: argparse writes its
        # parse-error message to stderr right before raising
        # SystemExit, and the per-deck failure record includes that
        # message — only possible when the worker's stderr is
        # per-thread capturable.
        _sys.stdout = _ThreadLocalStdoutProxy(real_stdout)
        _sys.stderr = _ThreadLocalStdoutProxy(real_stderr, attr="errbuf")
        try:
            # ThreadPoolExecutor over the deck paths. Workers block on
            # the Forge subprocess (when --run-sim is on) and on the
            # Anthropic API (when the curator runs); both are IO-bound
            # so threads are the right tool. max_workers is capped at
            # len(paths) to avoid spinning up idle workers for tiny
            # batches.
            with _cf.ThreadPoolExecutor(
                max_workers=min(parallelism, len(paths)),
            ) as pool:
                futures = [
                    pool.submit(
                        _process_one_deck, deck_path, batch_argv, args.force,
                        value_flags,
                    )
                    for deck_path in paths
                ]
                for fut in _cf.as_completed(futures):
                    record = fut.result()
                    _tally(record)
                    _emit(record)
        finally:
            _sys.stdout = real_stdout
            _sys.stderr = real_stderr

    summary = {
        "batch_summary": {
            "glob": args.batch,
            "matched": len(paths),
            "succeeded": n_succeeded,
            "skipped": n_skipped,
            "failed": n_failed,
            "parallelism": parallelism,
        },
    }
    print(json.dumps(summary), flush=True)

    # Non-zero exit iff EVERYTHING failed (with no successes); a
    # mixed batch returns 0 since the per-deck records carry the
    # individual failure info.
    if n_failed > 0 and n_succeeded == 0:
        return 2
    return 0


# ---------------------------------------------------------------------------
# Forge A/B-sim integration -- closes the loop the project's mission
# statement rests on. The advisor + curator produce a proposed deck;
# without empirically validating that the new deck wins MORE games than
# the old one, the recommendations are just untested suggestions. This
# block wires ``commander-auto-curate --run-sim`` into ``run_ab_sim
# ulation`` so each iteration row lands with a real verdict + win rates
# attached, not a permanent ``verdict='pending'``.


# A/B sim + knowledge_log writer helpers live in ``_proposer_sim`` so
# the orchestrator stays focused. Re-exported here for back-compat
# with existing tests that import _verdict_from_ab, _pick_filler_decks,
# etc. directly from ``commander_builder.proposer``.
from ._proposer_sim import (  # noqa: E402
    _DEFAULT_SIM_MARGIN,
    _ab_to_iteration_fields,
    _log_auto_curate_iteration,
    _pick_filler_decks,
    _run_sim_and_record,
    _verdict_from_ab,
)
