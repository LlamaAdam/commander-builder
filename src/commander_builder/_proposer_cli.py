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
    report = advise(
        deck_path=args.deck_path,
        bracket=args.bracket,
        source=args.source,
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


def _build_per_deck_argv(deck_path: Path, batch_argv: list[str]) -> list[str]:
    """Construct the argv for a single-deck recursive call by stripping
    batch-only flags from the batch invocation and substituting the
    resolved deck path.

    Strips: ``--batch <glob>`` (and its value), ``--force``. Leaves
    every other flag intact (--bracket, --mode, --run-sim, --max-adds,
    --max-cuts, --source, --model, --dry-run, --no-log, --db-path,
    --protect*, --sim-*).
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
        # Existing positional? In batch mode the user shouldn't have
        # passed one (we validated above), but defensively skip it.
        if not tok.startswith("-") and not saw_positional and tok.lower().endswith(".dck"):
            saw_positional = True
            i += 1
            continue
        out.append(tok)
        i += 1
    # Prepend the resolved per-deck path as the positional.
    return [str(deck_path), *out]


def _run_batch(args, parser, batch_argv: list[str]) -> int:
    """Iterate ``args.batch`` glob, run auto_curate_main per deck.

    JSON output is forced on so the run produces an NDJSON stream
    (one JSON object per line) plus a final ``batch_summary`` record.
    Per-deck failures are caught and recorded; one bad deck doesn't
    abort the rest.

    Returns 0 when every deck succeeded (or was skipped); 2 when at
    least one failed AND no successes; otherwise 0 with the failures
    captured in the summary.
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

    results: list[dict] = []
    n_skipped = 0
    n_failed = 0
    n_succeeded = 0
    # Force --json on for the per-deck calls so we capture structured
    # output. If the user already passed --json, this is a no-op.
    if "--json" not in batch_argv:
        batch_argv = [*batch_argv, "--json"]

    for deck_path in paths:
        if not args.force and _already_versioned(deck_path):
            record = {
                "deck": str(deck_path),
                "status": "skipped",
                "reason": "already-versioned (use --force to re-curate)",
            }
            results.append(record)
            print(json.dumps(record), flush=True)
            n_skipped += 1
            continue

        per_deck_argv = _build_per_deck_argv(deck_path, batch_argv)
        # Capture stdout from the recursive call so we can echo the
        # per-deck JSON object directly into the NDJSON stream while
        # also recording it in the summary list.
        import io as _io
        import contextlib as _ctx
        buf = _io.StringIO()
        try:
            with _ctx.redirect_stdout(buf):
                rc = auto_curate_main(per_deck_argv)
        except Exception as exc:  # noqa: BLE001 -- per-deck isolation
            record = {
                "deck": str(deck_path),
                "status": "error",
                "rc": None,
                "exception": f"{type(exc).__name__}: {exc}",
            }
            results.append(record)
            print(json.dumps(record), flush=True)
            n_failed += 1
            continue

        # The single-deck path emits one JSON object on success.
        # Parse it back so we can re-emit cleanly into the stream
        # (and attach our own status fields).
        raw = buf.getvalue().strip()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw_stdout": raw}
        record = {
            "deck": str(deck_path),
            "status": "ok" if rc == 0 else "failed",
            "rc": rc,
            "result": payload,
        }
        results.append(record)
        print(json.dumps(record), flush=True)
        if rc == 0:
            n_succeeded += 1
        else:
            n_failed += 1

    summary = {
        "batch_summary": {
            "glob": args.batch,
            "matched": len(paths),
            "succeeded": n_succeeded,
            "skipped": n_skipped,
            "failed": n_failed,
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
