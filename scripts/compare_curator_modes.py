#!/usr/bin/env python
"""Compare commander-auto-curate's three modes on the same deck.

Runs the SAME deck through ``--mode polish``, ``--mode overhaul``,
and ``--mode free`` (in that order) and prints a side-by-side
comparison of the curator's outputs. Use this to verify that:

  - The mode hint actually changes Claude's behavior — polish should
    propose fewer / more conservative swaps than overhaul.
  - The "caps are ceilings, not targets" rule is respected — overhaul
    should NOT fill 15/15 just because it can.
  - The protection list (``[metadata] Protect=`` entries) is honored
    by all three modes consistently.

Cost: ~3× a single curator call. With Sonnet 4.5 at ~3-5k tokens
per call (deck + advisor candidates + system prompt + rationale),
expect ~$0.20-$0.50 per full A/B run. Use Haiku 4.5 via
``--model claude-haiku-4-5`` for ~3× cheaper.

Output: a side-by-side table + per-mode adds/cuts/rationale +
set-difference analysis showing which adds are unique to each mode.

Side effects: NONE. No .dck files written, no knowledge_log rows
recorded. The script calls ``auto_propose()`` directly and stops
before ``apply_proposal_to_deck`` / ``record_iteration`` would
fire. Safe to re-run as many times as your API budget allows.

Usage:
    python scripts/compare_curator_modes.py <deck-path> --bracket N
    python scripts/compare_curator_modes.py <deck-path> --bracket 4 --model claude-haiku-4-5
    python scripts/compare_curator_modes.py <deck-path> --bracket 3 --out report.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from io import StringIO
from pathlib import Path

# Make `commander_builder` importable when running from repo root.
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from commander_builder.improvement_advisor import advise
from commander_builder.proposer import Proposal, auto_propose
from commander_builder.web._helpers import read_protected_cards


# Default caps per mode — mirrors auto_curate_main's _MODE_CAPS. We
# duplicate the values here rather than importing because the script
# wants to override them via per-mode CLI flags, which the CLI's
# preset isn't structured to expose.
_MODE_DEFAULT_CAPS = {
    "polish":   (5,   5),
    "overhaul": (15,  15),
    "free":     (999, 999),
}


def run_comparison(
    deck_path: Path,
    bracket: int,
    *,
    model: str,
    caps_override: dict[str, tuple[int, int]] | None = None,
    out_stream=sys.stdout,
) -> dict[str, Proposal]:
    """Run the curator three times — one per mode — and return the
    three Proposals keyed by mode. Prints a comparison report to
    ``out_stream`` as it goes.
    """
    caps = dict(_MODE_DEFAULT_CAPS)
    if caps_override:
        caps.update(caps_override)

    def _print(*args, **kwargs):
        print(*args, file=out_stream, flush=True, **kwargs)

    _print(f"[advisor] Running on {deck_path.name} at B{bracket}...")
    report = advise(deck_path=deck_path, bracket=bracket)
    advice = report.to_manifest()
    n_adds = len(advice.get("added") or [])
    n_cuts = len(advice.get("removed") or [])
    _print(f"  -> advisor produced {n_adds} candidate adds, {n_cuts} candidate cuts")

    deck_text = deck_path.read_text(encoding="utf-8")
    protected = read_protected_cards(deck_text)
    if protected:
        _print(f"  -> {len(protected)} protected cards: {', '.join(protected)}")
    _print()

    results: dict[str, Proposal] = {}
    for mode in ("polish", "overhaul", "free"):
        max_adds, max_cuts = caps[mode]
        _print(f"[{mode}] curator call (caps {max_adds}/{max_cuts})...")
        proposal = auto_propose(
            deck_path=deck_path,
            bracket=bracket,
            advice_report=advice,
            max_adds=max_adds,
            max_cuts=max_cuts,
            model=model,
            protected_cards=protected,
            mode=mode,
        )
        _print(f"  -> {len(proposal.adds)} adds, {len(proposal.cuts)} cuts proposed")
        results[mode] = proposal
    _print()

    _render_report(deck_path, bracket, model, results, out_stream)
    return results


def _render_report(
    deck_path: Path,
    bracket: int,
    model: str,
    results: dict[str, Proposal],
    out_stream,
) -> None:
    def _p(*a, **kw):
        print(*a, file=out_stream, **kw)

    _p("=" * 80)
    _p(f"COMPARISON: {deck_path.name} at B{bracket}")
    _p(f"Model: {model}")
    _p("=" * 80)
    _p()

    # Summary table — the headline diff. If polish and overhaul produce
    # similar counts, either the deck is well-tuned or the mode hint
    # isn't landing strongly enough.
    _p(f"{'MODE':<10} {'ADDS':>5} {'CUTS':>5} {'DROPPED_BRACKET':>16} "
       f"{'DROPPED_PROTECT':>16}")
    _p("-" * 80)
    for mode in ("polish", "overhaul", "free"):
        prop = results[mode]
        _p(f"{mode:<10} {len(prop.adds):>5} {len(prop.cuts):>5} "
           f"{len(prop.dropped_for_bracket):>16} "
           f"{len(prop.dropped_for_protection):>16}")
    _p()

    # Per-mode adds.
    _p("ADDS")
    _p("-" * 80)
    for mode in ("polish", "overhaul", "free"):
        prop = results[mode]
        if prop.adds:
            _p(f"  [{mode}] ({len(prop.adds)}):")
            for c in prop.adds:
                _p(f"    + {c}")
        else:
            _p(f"  [{mode}]: (none)")
    _p()

    # Per-mode cuts.
    _p("CUTS")
    _p("-" * 80)
    for mode in ("polish", "overhaul", "free"):
        prop = results[mode]
        if prop.cuts:
            _p(f"  [{mode}] ({len(prop.cuts)}):")
            for c in prop.cuts:
                _p(f"    - {c}")
        else:
            _p(f"  [{mode}]: (none)")
    _p()

    # Rationales.
    _p("RATIONALES")
    _p("-" * 80)
    for mode in ("polish", "overhaul", "free"):
        prop = results[mode]
        _p(f"  [{mode}]: {prop.rationale or '(empty)'}")
    _p()

    # Set-difference analysis: how much do the three modes agree?
    # High overlap suggests the deck's needs are clear; low overlap
    # suggests the mode hint is genuinely steering Claude.
    add_sets = {
        m: {c.lower() for c in results[m].adds}
        for m in ("polish", "overhaul", "free")
    }
    cut_sets = {
        m: {c.lower() for c in results[m].cuts}
        for m in ("polish", "overhaul", "free")
    }

    def _intersect_count(sets, a, b):
        return len(sets[a] & sets[b])

    def _unique_to(sets, target):
        others = set()
        for m, s in sets.items():
            if m != target:
                others |= s
        return sets[target] - others

    _p("ADD-SET ANALYSIS")
    _p("-" * 80)
    _p(f"  polish intersect overhaul: {_intersect_count(add_sets, 'polish', 'overhaul')}")
    _p(f"  polish intersect free:     {_intersect_count(add_sets, 'polish', 'free')}")
    _p(f"  overhaul intersect free:   {_intersect_count(add_sets, 'overhaul', 'free')}")
    for mode in ("polish", "overhaul", "free"):
        u = _unique_to(add_sets, mode)
        if u:
            _p(f"  unique to {mode}:   {sorted(u)}")
    _p()

    _p("CUT-SET ANALYSIS")
    _p("-" * 80)
    _p(f"  polish intersect overhaul: {_intersect_count(cut_sets, 'polish', 'overhaul')}")
    _p(f"  polish intersect free:     {_intersect_count(cut_sets, 'polish', 'free')}")
    _p(f"  overhaul intersect free:   {_intersect_count(cut_sets, 'overhaul', 'free')}")
    for mode in ("polish", "overhaul", "free"):
        u = _unique_to(cut_sets, mode)
        if u:
            _p(f"  unique to {mode}:   {sorted(u)}")
    _p()

    # Health checks the user should glance at.
    _p("HEALTH CHECKS")
    _p("-" * 80)
    polish_count = len(results["polish"].adds)
    overhaul_count = len(results["overhaul"].adds)
    free_count = len(results["free"].adds)
    if overhaul_count == 15 and free_count == 15:
        _p("  WARN: Both overhaul + free returned exactly 15 adds. Claude may be "
           "FILLING the cap rather than picking based on need. The 'caps are "
           "ceilings, not targets' rule may not be landing.")
    elif overhaul_count <= polish_count:
        _p(f"  WARN: Overhaul ({overhaul_count}) returned no more than polish "
           f"({polish_count}). Either the deck is tight or the mode hint isn't "
           f"steering Claude.")
    else:
        _p(f"  OK: Overhaul ({overhaul_count}) > polish ({polish_count}) — the "
           f"mode hint is landing.")

    if all(len(results[m].adds) == 0 for m in ("polish", "overhaul", "free")):
        _p("  OK: All three modes returned zero adds — Claude reads this deck "
           "as already well-tuned at this bracket. (Or the curator gave up; "
           "check the rationales.)")
    _p()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="compare_curator_modes",
        description=(
            "Smoke-test commander-auto-curate's --mode preset by running "
            "the same deck through polish, overhaul, and free in one shot. "
            "Prints a side-by-side comparison. Costs ~$0.20-$0.50 per run."
        ),
    )
    p.add_argument("deck", type=Path, help="Path to the .dck file.")
    p.add_argument("--bracket", type=int, required=True,
                   help="Target bracket (1-5).")
    p.add_argument("--model", default="claude-sonnet-4-5",
                   help="Anthropic model id (default sonnet-4.5; use "
                        "claude-haiku-4-5 for ~3× cheaper).")
    p.add_argument("--out", type=Path, default=None,
                   help="Write the report to this file in addition to "
                        "stdout. Useful for archival comparison.")
    args = p.parse_args(argv)

    # Load external credentials so users who configured the key in
    # ~/.commander-builder/credentials don't need to set the env var
    # explicitly. Shell env still wins if both are set.
    from commander_builder._secrets import load_credentials
    load_credentials(quiet=True)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print(
            "ERROR: ANTHROPIC_API_KEY required. Run `commander-config init` "
            "to create the credentials file, or set the env var directly.",
        )
        return 2
    # Resolve to absolute path BEFORE passing to advise() — the advisor
    # treats relative paths as deck_dir-relative, which doubles the
    # prefix when the user passes a path already inside vendor/forge/.
    args.deck = args.deck.resolve()
    if not args.deck.exists():
        print(f"ERROR: deck not found: {args.deck}")
        return 2
    if not (1 <= args.bracket <= 5):
        print(f"ERROR: bracket must be 1-5, got {args.bracket}")
        return 2

    # If --out was passed, tee the output to both stdout and the file.
    if args.out:
        buf = StringIO()

        class _Tee:
            def write(self, s):
                buf.write(s)
                sys.stdout.write(s)
            def flush(self):
                sys.stdout.flush()

        out_stream = _Tee()
        run_comparison(args.deck, args.bracket, model=args.model,
                       out_stream=out_stream)
        args.out.write_text(buf.getvalue(), encoding="utf-8")
        print(f"\n[report saved to {args.out}]")
    else:
        run_comparison(args.deck, args.bracket, model=args.model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
