#!/usr/bin/env python
"""Diff the hardcoded card lists in ``deck_health.py`` and
``bracket_estimator.py`` against fresh data and surface review
candidates.

The lists in ``deck_health._MDFC_LANDS``, ``_WINCON_PROTECTION``,
``_SELF_MILL_ENABLERS``, ``bracket_estimator._EXTRA_TURN_CARDS`` and
``bracket_estimator._MLD_CARDS`` are curated by hand. They're short,
stable across sets, and deliberately conservative — false negatives
(missing a card that should be on the list) are preferred over false
positives because the UI surfaces named cards from each list and a
wrong inclusion is visible.

This script doesn't auto-edit the lists. It prints two reports per
category::

    stale       — names in the hardcoded list that fresh data doesn't
                  match under the relevant filter (typos, renames,
                  removed sets, mis-curation — or, for the snapshot-
                  backed categories, a snapshot store that simply
                  hasn't cached the card yet).
    candidates  — names in fresh data that match the filter but aren't
                  in our list yet. Maintainer reviews these and adds
                  the ones that survive the curation rule.

Side effects: NONE. Read-only against Scryfall / the local snapshot
store + the hardcoded lists.

Usage:
    python scripts/refresh_card_lists.py                    # all categories
    python scripts/refresh_card_lists.py --only mdfc        # MDFC only
    python scripts/refresh_card_lists.py --only extra-turns # extra turns only
    python scripts/refresh_card_lists.py --json             # machine-readable

Cost: 2-3 Scryfall API calls (paginated search) for the API-backed
categories; ZERO network for the snapshot-backed ones (they scan the
local oracle-snapshot store — populate/refresh it first via
``commander-oracle-refresh --from-bulk --everything``).

Coverage:
    mdfc          ``layout:modal_dfc`` AND at least one face is a Land.
                  (Scryfall search API.)
    wincon        NOT automatable. Prints the current list with a
                  reminder to review manually (criteria: prevents
                  interaction during a combo turn).
    self-mill     Scryfall search + oracle-text post-filter (puts cards
                  from YOUR library into YOUR graveyard, not generic
                  opponent-mill cards).
    extra-turns   Local snapshot store: oracle text grants an extra
                  turn ("take(s) an/two/X extra turn(s)"). Backs
                  ``bracket_estimator._EXTRA_TURN_CARDS``.
    mld           Local snapshot store: mass-land-denial oracle shapes
                  (destroy/exile/return ALL lands, symmetric multi-land
                  sacrifice, choose-and-sacrifice-the-rest). Backs
                  ``bracket_estimator._MLD_CARDS``. Tutors stay
                  manual-only on purpose: oracle text can't cleanly
                  separate tutors from fetches/ramp.
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
    extra_turn_names_from_snapshots,
    fetch_mdfc_lands,
    fetch_self_mill_candidates,
    iter_snapshot_cards,
    mld_names_from_snapshots,
)
from commander_builder.bracket_estimator import (  # noqa: E402
    _EXTRA_TURN_CARDS,
    _MLD_CARDS,
)
from commander_builder.deck_health import (  # noqa: E402
    _MDFC_LANDS,
    _SELF_MILL_ENABLERS,
    _WINCON_PROTECTION,
)


def _print_diff(label: str, diff: dict) -> None:
    print(f"\n=== {label} ===")
    if diff.get("note"):
        print(f"\n  NOTE: {diff['note']}")
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


def _refresh_self_mill(as_json: bool) -> dict:
    """Scryfall query for self-mill candidates + post-filter to
    exclude opponent-mill. The filter is conservative: a noisy
    candidate list (maintainer reviews) beats missing genuine
    self-mill cards. AGENT_BACKLOG #010."""
    fresh = fetch_self_mill_candidates()
    diff = diff_card_lists(current=_SELF_MILL_ENABLERS, fresh=fresh)
    if not as_json:
        _print_diff("_SELF_MILL_ENABLERS", diff)
    return diff


def _snapshot_store_empty() -> bool:
    """True when the local oracle-snapshot store has no cards — the
    diff would then report the ENTIRE hardcoded list as stale, which is
    a statement about the store, not the list."""
    return next(iter_snapshot_cards(), None) is None


_EMPTY_STORE_NOTE = (
    "The local oracle-snapshot store is empty — every hardcoded name "
    "reads as stale. Populate it first: "
    "commander-oracle-refresh --from-bulk --everything"
)


def _refresh_snapshot_backed(
    label: str,
    current: frozenset[str],
    fresh_fn,
    as_json: bool,
) -> dict:
    """Shared driver for the snapshot-store categories (extra-turns,
    mld). Zero network: scans the local oracle snapshots."""
    if _snapshot_store_empty():
        diff = {
            "stale": [],
            "candidates": [],
            "kept": sorted(current),
            "note": _EMPTY_STORE_NOTE,
        }
    else:
        diff = diff_card_lists(current=current, fresh=fresh_fn())
    if not as_json:
        _print_diff(label, diff)
    return diff


def _manual_only(label: str, current: frozenset[str], as_json: bool) -> dict:
    """Categories that can't be cleanly automated — wincon protection
    has curation nuance (combo-turn intent) that's easier to express
    via human review of the existing list than via regex."""
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


_ALL_CATEGORIES = ["mdfc", "wincon", "self-mill", "extra-turns", "mld"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--only",
        choices=_ALL_CATEGORIES,
        help="Refresh only the named category (default: all).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    args = p.parse_args(argv)

    reports: dict[str, dict] = {}
    categories = [args.only] if args.only else list(_ALL_CATEGORIES)

    for cat in categories:
        if cat == "mdfc":
            reports["mdfc"] = _refresh_mdfc(args.json)
        elif cat == "wincon":
            reports["wincon"] = _manual_only(
                "_WINCON_PROTECTION", _WINCON_PROTECTION, args.json,
            )
        elif cat == "self-mill":
            reports["self-mill"] = _refresh_self_mill(args.json)
        elif cat == "extra-turns":
            reports["extra-turns"] = _refresh_snapshot_backed(
                "_EXTRA_TURN_CARDS (bracket_estimator)",
                _EXTRA_TURN_CARDS,
                extra_turn_names_from_snapshots,
                args.json,
            )
        elif cat == "mld":
            reports["mld"] = _refresh_snapshot_backed(
                "_MLD_CARDS (bracket_estimator)",
                _MLD_CARDS,
                mld_names_from_snapshots,
                args.json,
            )

    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    sys.exit(main())
