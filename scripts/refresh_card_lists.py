#!/usr/bin/env python
"""Diff the hardcoded card lists in ``deck_health.py`` against current
Scryfall data and surface review candidates.

The lists in ``deck_health._MDFC_LANDS``, ``_WINCON_PROTECTION``, and
``_SELF_MILL_ENABLERS`` are curated by hand. They're short, stable
across sets, and deliberately conservative — false negatives (missing
a card that should be on the list) are preferred over false positives
because the UI surfaces named cards from each list and a wrong
inclusion is visible.

This script doesn't auto-edit the lists. It prints two reports per
category::

    stale       — names in the hardcoded list that are no longer on
                  Scryfall under the relevant filter (typos, renames,
                  removed sets, mis-curation).
    candidates  — names on Scryfall that match the filter but aren't
                  in our list yet. Maintainer reviews these and adds
                  the ones that survive the curation rule.

Side effects: NONE. Read-only against Scryfall + ``deck_health``.

Usage:
    python scripts/refresh_card_lists.py                # all categories
    python scripts/refresh_card_lists.py --only mdfc    # MDFC only
    python scripts/refresh_card_lists.py --json         # machine-readable

Cost: 2-3 Scryfall API calls (paginated search). Cached on Scryfall's
side; rate-limited per their terms (~10 req/s).

Coverage:
    mdfc          ``layout:modal_dfc`` AND at least one face is a Land.
    wincon        NOT automatable. Prints the current list with a
                  reminder to review manually (criteria: prevents
                  interaction during a combo turn).
    self-mill     NOT automatable. Prints the current list with a
                  reminder to review manually (criteria: puts cards
                  from YOUR library into YOUR graveyard, not generic
                  opponent-mill cards).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the package importable when run as ``python scripts/...``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from commander_builder._card_list_refresh import (  # noqa: E402
    diff_card_lists,
    fetch_mdfc_lands,
)
from commander_builder.deck_health import (  # noqa: E402
    _MDFC_LANDS,
    _SELF_MILL_ENABLERS,
    _WINCON_PROTECTION,
)


def _print_diff(label: str, diff: dict) -> None:
    print(f"\n=== {label} ===")
    if diff["stale"]:
        print(f"\n  Stale (in list but not in fresh data, {len(diff['stale'])}):")
        for name in diff["stale"]:
            print(f"    - {name}")
    else:
        print("\n  Stale: (none)")
    if diff["candidates"]:
        print(
            f"\n  Candidates "
            f"(in fresh data but not in list, {len(diff['candidates'])}):"
        )
        for name in diff["candidates"]:
            print(f"    + {name}")
    else:
        print("\n  Candidates: (none)")
    print(f"\n  Kept (in both): {len(diff['kept'])}")


def _refresh_mdfc(as_json: bool) -> dict:
    fresh = fetch_mdfc_lands()
    diff = diff_card_lists(current=_MDFC_LANDS, fresh=fresh)
    if not as_json:
        _print_diff("_MDFC_LANDS", diff)
    return diff


def _manual_only(label: str, current: frozenset[str], as_json: bool) -> dict:
    """Categories that can't be cleanly automated — wincon protection
    and self-mill enablers have curation nuance (combo-turn intent,
    opponent vs self mill semantics) that's easier to express via
    human review of the existing list than via regex."""
    diff = {
        "stale": [],
        "candidates": [],
        "kept": sorted(current),
        "note": "Manual curation only. No automated Scryfall query for this list.",
    }
    if not as_json:
        print(f"\n=== {label} ===")
        print(f"\n  {diff['note']}")
        print(f"  Current list size: {len(diff['kept'])}")
    return diff


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        choices=["mdfc", "wincon", "self-mill"],
        help="Refresh only the named category (default: all).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    args = p.parse_args(argv)

    reports: dict[str, dict] = {}
    categories = (
        [args.only] if args.only else ["mdfc", "wincon", "self-mill"]
    )

    for cat in categories:
        if cat == "mdfc":
            reports["mdfc"] = _refresh_mdfc(args.json)
        elif cat == "wincon":
            reports["wincon"] = _manual_only(
                "_WINCON_PROTECTION", _WINCON_PROTECTION, args.json,
            )
        elif cat == "self-mill":
            reports["self-mill"] = _manual_only(
                "_SELF_MILL_ENABLERS", _SELF_MILL_ENABLERS, args.json,
            )

    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
