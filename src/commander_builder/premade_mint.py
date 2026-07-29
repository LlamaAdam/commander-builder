"""Batch heuristic v2 minting for `[PREMADE]` decks (FP-002 unblock).

FP-002's margin regression counts unique (base, v2) deck pairs, and the
soak only deepens pairs that already exist — new pairs have to be MINTED.
The freshly pulled `[PREMADE]` library (``premade_import``) supplies the
bases; this module writes each one's curated sibling:

  ``[PREMADE] Foo [B3].dck`` -> ``[PREMADE] Foo v2 [B3].dck``

For every premade base with no versioned file in its lineage
(``moxfield_import._lineage_root`` — same grouping the same-id lineage
classifier uses), it:

  1. Runs the FREE heuristic advisor (``improvement_advisor.advise`` with
     ``source="heuristic"`` — EDHREC aggregate deltas, the exact backend
     ``commander-advise`` defaults to; no LLM, no Anthropic API) at the
     deck's filename-tagged bracket (offline ``bracket_estimator``
     fallback for a `[B?]` tag).
  2. Applies the recommendations through ``proposer.apply_proposal_to_deck``
     — the SAME shared legality path the improve loop uses: balanced
     adds/cuts, per-pair decklist validation, basic-land padding to
     ``100 - commander_count``, the hard mainboard guard, and the
     ``Name= == filename stem`` restamp (``log_parser._normalize``
     equality invariant). The `[metadata]` block passes through, so a
     Moxfield-sourced v2 keeps its base's ``Moxfield=`` id — one lineage,
     no same-id warning.
  3. SKIPS a deck when zero swaps would land (checked via a dry run
     first): a v2 identical to its base contributes nothing to FP-002.

Optionally stages every minted (base, v2) file pair into an inbox
directory (``--stage-inbox``) so ``scripts/sync_machine.ps1`` delivers
them to the soak box. The path is caller-supplied on purpose — no UNC
path is hardcoded here, and tests never touch the network share.

Usage:
  commander-mint-v2
  commander-mint-v2 --stage-inbox "\\\\192.168.4.49\\soak_inbox\\new_decks"
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Callable, Optional

from . import moxfield_import as _mox
from .bracket_estimator import estimate_bracket
from .moxfield_import import DECK_OUT_DIR

# Injectable advisor seam: (deck_path, bracket) -> list of
# SwapRecommendation-shaped objects (only .card / .action are read).
# Tests inject a fake; production uses _default_advise below.
AdviseFn = Callable[[Path, int], list]


def _tagged_bracket(path: Path) -> Optional[int]:
    """Bracket from the filename's ` [B<n>]` tag, or None for `[B?]`/none."""
    m = _mox._BRACKET_TAG_STEM.match(path.stem)
    if not m:
        return None
    digit = m.group("tag").strip()[2:-1]  # " [B3]" -> "3"
    return int(digit) if digit.isdigit() else None


def premade_bases_without_v2(deck_dir: Path = DECK_OUT_DIR) -> list[Path]:
    """`[PREMADE]` base decks whose lineage has no versioned sibling.

    Lineage identity is ``_lineage_root`` (stem minus bracket tag minus
    ` v<N>` token) — the same grouping the same-Moxfield-id classifier
    uses, so a bracket-drift-renamed v2 still counts as covering its
    base. Unversioned files are the bases; any ` v<N>` file in the same
    root marks the pair as already minted."""
    versioned_roots: set[str] = set()
    bases: dict[str, Path] = {}
    for path in sorted(deck_dir.glob("*.dck")):
        if _mox._deck_role(path) != "premade":
            continue
        root, ver = _mox._lineage_root(path.stem)
        if ver is None:
            bases.setdefault(root, path)
        else:
            versioned_roots.add(root)
    return [bases[r] for r in sorted(bases) if r not in versioned_roots]


def _default_advise(deck_path: Path, bracket: int, deck_dir: Path) -> list:
    """The commander-advise heuristic backend — EDHREC aggregate, no LLM."""
    from .improvement_advisor import advise

    report = advise(
        deck_path=deck_path,
        bracket=bracket,
        source="heuristic",
        deck_dir=deck_dir,
        match_dir=deck_dir / "_matches",
    )
    return report.recommendations


def mint_v2_for_premades(
    deck_dir: Path = DECK_OUT_DIR,
    *,
    advise_fn: Optional[AdviseFn] = None,
    stage_dir: Optional[Path] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Mint a heuristic v2 for every `[PREMADE]` base lacking one.

    Per-deck flow: advise (heuristic) -> ``apply_proposal_to_deck``
    dry run (zero-applied-swap decks are SKIPPED — an identical v2 is
    useless to FP-002) -> real write through the same shared legality
    path (balancing, validation, padding, hard guard, Name= restamp).
    A deck that fails to advise or apply is logged and skipped; one bad
    premade never aborts the batch.

    ``stage_dir``, when given, receives a copy of each minted base AND
    its new v2 (the soak inbox hand-off). ``limit`` caps how many bases
    are attempted (smoke runs). Returns one summary row per candidate:
    ``{deck, bracket, swaps, v2, skipped, staged}`` (``v2``/``skipped``
    are mutually exclusive; ``skipped`` carries the why)."""
    from .proposer import Proposal, apply_proposal_to_deck

    bases = premade_bases_without_v2(deck_dir)
    if limit is not None:
        bases = bases[:limit]
    rows: list[dict] = []
    print(f"Minting heuristic v2s for {len(bases)} [PREMADE] base deck(s)...")
    for base in bases:
        bracket = _tagged_bracket(base)
        if bracket is None:
            bracket = estimate_bracket(
                base.read_text(encoding="utf-8"))["estimate"]
        row = {"deck": base.name, "bracket": bracket, "swaps": 0,
               "v2": None, "skipped": None, "staged": False}
        rows.append(row)
        try:
            recs = (advise_fn(base, bracket) if advise_fn is not None
                    else _default_advise(base, bracket, deck_dir))
        except Exception as exc:  # noqa: BLE001 — batch survives one bad deck
            row["skipped"] = f"advise failed: {type(exc).__name__}: {exc}"
            print(f"  SKIP {base.name}: {row['skipped']}")
            continue
        proposal = Proposal(
            adds=[r.card for r in recs if r.action == "add"],
            cuts=[r.card for r in recs if r.action == "cut"],
            rationale=f"heuristic v2 mint at B{bracket}",
            source="premade-mint",
        )
        try:
            # Dry run first: the applied swap set only exists after
            # balancing + per-pair decklist validation, so "zero swaps"
            # can't be judged from the requested lists alone.
            apply_proposal_to_deck(base, proposal, dry_run=True)
            if not proposal.applied_adds and not proposal.applied_cuts:
                row["skipped"] = "zero swaps (v2 would be identical to base)"
                print(f"  SKIP {base.name}: {row['skipped']}")
                continue
            out_path = apply_proposal_to_deck(base, proposal)
        except Exception as exc:  # noqa: BLE001
            row["skipped"] = f"apply failed: {type(exc).__name__}: {exc}"
            print(f"  SKIP {base.name}: {row['skipped']}")
            continue
        row["swaps"] = len(proposal.applied_cuts)
        row["v2"] = out_path.name
        print(f"  Wrote {out_path.name} ({row['swaps']} swap(s), B{bracket})")
        if stage_dir is not None:
            stage_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base, stage_dir / base.name)
            shutil.copy2(out_path, stage_dir / out_path.name)
            row["staged"] = True
            print(f"  Staged {base.name} + {out_path.name} -> {stage_dir}")
    _print_summary(rows)
    return rows


def _print_summary(rows: list[dict]) -> None:
    minted = sum(1 for r in rows if r["v2"])
    print(f"\n=== v2 mint summary ({minted} minted / {len(rows)} candidates) ===")
    if not rows:
        print("  (no [PREMADE] bases without a v2)")
        return
    name_w = max(len(r["deck"]) for r in rows)
    header = f"  {'Deck':<{name_w}}  Bracket  Swaps  Result"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        result = r["v2"] if r["v2"] else f"SKIPPED: {r['skipped']}"
        if r["staged"]:
            result += "  [staged]"
        print(f"  {r['deck']:<{name_w}}  B{r['bracket']}       "
              f"{r['swaps']:<5}  {result}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-mint-v2",
        description=(
            "Mint a heuristic (FREE, no-LLM) v2 for every [PREMADE] base "
            "deck without one — new (base, v2) pairs for the FP-002 gate."
        ),
    )
    p.add_argument(
        "--deck-dir", type=Path, default=DECK_OUT_DIR,
        help="Deck directory to scan and write into (default: the "
             "Forge commander deck dir).",
    )
    p.add_argument(
        "--stage-inbox", type=Path, default=None, metavar="PATH",
        help="Also copy each minted base + v2 into this directory "
             "(e.g. the soak inbox share consumed by sync_machine.ps1). "
             "Off by default; the path is never hardcoded.",
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Mint at most this many decks (smoke runs).",
    )
    args = p.parse_args(argv)
    rows = mint_v2_for_premades(
        deck_dir=args.deck_dir,
        stage_dir=args.stage_inbox,
        limit=args.limit,
    )
    minted = any(r["v2"] for r in rows)
    errored = any(
        r["skipped"] and not r["skipped"].startswith("zero swaps")
        for r in rows
    )
    # Nothing minted AND something errored = the batch failed; an empty
    # candidate list or all-zero-swap skips is a legitimate no-op.
    if rows and not minted and errored:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
