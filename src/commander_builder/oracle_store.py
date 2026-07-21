"""Oracle-text-first card-reference store (FP-009).

A thin public surface over the substrate that already exists in
``scryfall_client`` — the per-card snapshot cache at
``mtg_cards/oracle_snapshots/<slug>.json``. This module deliberately does
**not** introduce a second datastore; it leans on the Scryfall client and
adds the three pieces FP-009 was missing:

  1. **Presentation helper** — ``card_reference(name)`` (a stable public
     alias for ``scryfall_client.format_card_for_display``) renders a
     card to the plain-text block used identically in CLI output, web
     panels, and LLM prompts. Oracle text is authoritative; images are
     decorative.
  2. **Errata-diff tooling** — ``check_errata(name)`` compares the cached
     snapshot's oracle text against a fresh (un-cached) Scryfall fetch and
     reports whether WotC re-worded the card since we last snapshotted it.
  3. **Bulk-refresh CLI** — ``bulk_refresh(...)`` / ``main()`` walk a set
     of cards (a deck, an explicit list, or the whole snapshot store),
     report drift, and optionally rewrite stale snapshots
     (``commander-oracle-refresh``).

Network calls go through ``scryfall_client`` functions so they're easy to
stub in tests; nothing here talks to Scryfall directly.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator, Optional

from . import scryfall_client

# Re-export the presentation helper under a stable, intention-revealing
# name. Callers wanting "render this card for a human/LLM" use this; they
# shouldn't need to know it lives in scryfall_client.
from .scryfall_client import format_card_for_display as card_reference  # noqa: F401


def iter_cached_names() -> Iterator[str]:
    """Yield the canonical card name of every snapshot in the store.

    Reads each ``oracle_snapshots/*.json`` and yields its ``name`` field
    (falling back to the file stem). Corrupt / unreadable snapshots are
    skipped silently — a single bad file shouldn't abort a bulk pass.
    """
    cache_dir = scryfall_client.CACHE_DIR
    if not cache_dir.exists():
        return
    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name") if isinstance(data, dict) else None
        yield name or path.stem


def snapshot_age_days(name: str) -> Optional[float]:
    """Age in days of ``name``'s cached snapshot, or ``None`` if uncached."""
    path = scryfall_client._cache_path(name)
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 86400.0


def names_from_deck(deck_path: Path) -> list[str]:
    """Distinct card names (Commander + Main) from a ``.dck`` file, in
    first-seen order. Reuses the library analyzer's line parser so the
    ``|SET|CN`` suffix is handled consistently."""
    from .deck_library_analyzer import iter_deck_cards

    text = Path(deck_path).read_text(encoding="utf-8")
    seen: set[str] = set()
    out: list[str] = []
    for _qty, name in iter_deck_cards(text):
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def check_errata(name: str) -> dict:
    """Compare the cached snapshot's oracle text against current Scryfall.

    Returns a dict with ``status`` one of:
      - ``"not_cached"`` — no snapshot to compare against.
      - ``"corrupt"``    — snapshot exists but won't parse.
      - ``"upstream_404"`` — Scryfall no longer resolves the name.
      - ``"ok"``         — compared; ``changed`` says whether it drifted.
    On ``ok`` the dict also carries ``before`` / ``after`` oracle text.
    Never raises for the missing/corrupt/404 cases — they're data.
    """
    path = scryfall_client._cache_path(name)
    if not path.exists():
        return {"name": name, "status": "not_cached", "changed": False}
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"name": name, "status": "corrupt", "changed": False}

    before = (cached.get("oracle_text") or "").strip()
    # cache=False fetches fresh AND does not overwrite the snapshot — so a
    # read-only errata check never mutates the store.
    current = scryfall_client.lookup_card(name, cache=False)
    if current is None:
        return {"name": name, "status": "upstream_404", "changed": False,
                "before": before}
    after = (current.get("oracle_text") or "").strip()
    return {"name": name, "status": "ok", "changed": before != after,
            "before": before, "after": after}


def bulk_refresh(
    names: Optional[list[str]] = None,
    *,
    write: bool = False,
    stale_days: Optional[float] = None,
) -> dict:
    """Check (and optionally rewrite) oracle snapshots for ``names``.

    ``names=None`` walks the entire snapshot store. ``stale_days`` skips
    snapshots younger than that age (cheap incremental refresh).
    ``write=True`` rewrites the snapshot for any card whose oracle text
    drifted; the default is a read-only report.

    Returns a summary ``{checked, changed, refreshed, skipped, errors,
    results}`` where ``results`` is the per-card ``check_errata`` dict
    (annotated with ``refreshed`` when written). Never raises.
    """
    if names is None:
        names = list(iter_cached_names())

    results: list[dict] = []
    changed = refreshed = errors = skipped = 0

    for name in names:
        if stale_days is not None:
            age = snapshot_age_days(name)
            if age is not None and age < stale_days:
                results.append({"name": name, "status": "skipped_fresh",
                                "changed": False, "age_days": round(age, 1)})
                skipped += 1
                continue

        res = check_errata(name)
        if res["status"] in ("not_cached", "corrupt", "upstream_404"):
            errors += 1
        if res.get("changed"):
            changed += 1
            if write:
                try:
                    scryfall_client.refresh_card(name)
                    res["refreshed"] = True
                    refreshed += 1
                except Exception as exc:  # noqa: BLE001
                    res["refreshed"] = False
                    res["error"] = f"{type(exc).__name__}: {exc}"
        results.append(res)

    return {
        "checked": len(names),
        "changed": changed,
        "refreshed": refreshed,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }


def main(argv: Optional[list[str]] = None) -> int:
    """``commander-oracle-refresh`` — report (and optionally rewrite)
    oracle-snapshot drift for a deck, an explicit card list, or the whole
    store."""
    import argparse

    p = argparse.ArgumentParser(
        prog="commander-oracle-refresh",
        description=(
            "Detect oracle-text drift (WotC errata) between cached "
            "snapshots and current Scryfall, and optionally rewrite stale "
            "snapshots. Read-only by default — pass --write to persist."
        ),
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--deck", type=Path, metavar="PATH",
                     help="Refresh the cards in this .dck file.")
    src.add_argument("--name", action="append", default=[], metavar="CARD",
                     help="Refresh a specific card. Repeatable.")
    src.add_argument("--all", action="store_true",
                     help="Walk the entire snapshot store (slow; one "
                          "Scryfall request per cached card).")
    p.add_argument("--write", action="store_true",
                   help="Rewrite the snapshot for any drifted card "
                        "(default: report only).")
    p.add_argument("--stale-days", type=float, default=None, metavar="N",
                   help="Skip snapshots younger than N days.")
    p.add_argument("--json", action="store_true",
                   help="Emit the summary as JSON.")
    args = p.parse_args(argv)

    if args.deck is not None:
        if not args.deck.exists():
            print(f"ERROR: deck not found: {args.deck}", flush=True)
            return 2
        names: Optional[list[str]] = names_from_deck(args.deck)
    elif args.name:
        names = args.name
    else:  # --all
        names = None

    summary = bulk_refresh(names, write=args.write, stale_days=args.stale_days)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    verb = "rewrote" if args.write else "would rewrite"
    print(f"Checked {summary['checked']} card(s): "
          f"{summary['changed']} drifted, {verb} {summary['refreshed']}, "
          f"{summary['skipped']} skipped (fresh), {summary['errors']} error(s).")
    for res in summary["results"]:
        if res.get("changed"):
            flag = "✎ rewrote" if res.get("refreshed") else "≠ drifted"
            print(f"  {flag}: {res['name']}")
        elif res["status"] not in ("ok", "skipped_fresh"):
            print(f"  ! {res['status']}: {res['name']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
