"""Moxfield → Forge `.dck` importer.

Phase 1B preview: pulls a Moxfield deck (or a search of decks) via the public API
and writes Forge `.dck` files into `vendor/forge/userdata/decks/commander/`.

Decks are tagged by Commander bracket (1-5) via filename suffix:
  - `Deck Name [B3].dck` — bracket immediately visible in Forge's deck picker
  - Flat layout because Forge sim mode in 2.0.12 does NOT recurse subfolders
    (verified empirically — decks under `bracket-N/` are invisible to sim)

Single-deck usage:

    python -m commander_builder.moxfield_import https://moxfield.com/decks/<id>
    python -m commander_builder.moxfield_import <id>

Bulk-by-bracket usage:

    python -m commander_builder.moxfield_import --bracket 3 --count 4
    python -m commander_builder.moxfield_import --bracket 4 --count 3 --bracket 5 --count 2
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DECK_OUT_DIR = REPO_ROOT / "vendor" / "forge" / "userdata" / "decks" / "commander"
API_BASE = "https://api2.moxfield.com/v3/decks/all"
SEARCH_BASE = "https://api2.moxfield.com/v2/decks/search"
USER_AGENT = "commander-builder/0.1 (+https://github.com/LlamaAdam/commander-builder)"

# Forge `.dck` filenames must avoid characters that the Windows filesystem
# rejects. Trailing dots/spaces also break things. Keep this conservative.
INVALID_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def parse_deck_id(url_or_id: str) -> str:
    """Accept either a full Moxfield URL or a bare deck id."""
    m = re.search(r"/decks/([A-Za-z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_deck(deck_id: str) -> dict:
    return _http_get_json(f"{API_BASE}/{deck_id}")


def search_decks(
    bracket: int,
    page_size: int = 20,
    sort_type: str = "updated",
    page: int = 1,
) -> list[dict]:
    """Return search results for commander decks at the given bracket.

    Moxfield's `bracket` filter is loose — it surfaces near-bracket decks too —
    so callers must re-check each fetched deck's actual bracket. Uses
    `sortType=updated`; with `views`, Moxfield returns historic high-traffic
    decks regardless of the requested bracket.
    """
    params = {
        "pageNumber": str(page),
        "pageSize": str(page_size),
        "sortType": sort_type,
        "sortDirection": "descending",
        "fmt": "commander",
        "bracket": str(bracket),
    }
    url = f"{SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    payload = _http_get_json(url)
    return payload.get("data", [])


def card_line(entry: dict) -> str:
    """Render one Forge `.dck` card line: `<qty> <Name>|<SET>|<CN>`.

    Set + collector number are pipe-separated suffixes Forge uses to pick the
    exact printing. They're optional — Forge falls back to any printing if the
    set code is unknown — so we include them when available.
    """
    qty = entry.get("quantity", 1)
    card = entry.get("card", {})
    name = card.get("name", "<UNKNOWN>")
    set_code = (card.get("set") or "").upper()
    cn = card.get("cn") or ""
    parts = [name]
    if set_code:
        parts.append(set_code)
    if cn:
        parts.append(str(cn))
    return f"{qty} {'|'.join(parts)}"


def to_dck(deck_json: dict) -> str:
    """Convert a Moxfield deck JSON to Forge `.dck` text."""
    name = deck_json.get("name", "Untitled")
    boards = deck_json.get("boards", {})

    commanders = list(boards.get("commanders", {}).get("cards", {}).values())
    main = list(boards.get("mainboard", {}).get("cards", {}).values())

    lines: list[str] = []
    lines.append("[metadata]")
    lines.append(f"Name={name}")
    if commanders:
        lines.append("[Commander]")
        for c in commanders:
            lines.append(card_line(c))
    lines.append("[Main]")
    for c in main:
        lines.append(card_line(c))
    return "\n".join(lines) + "\n"


def safe_filename(name: str) -> str:
    cleaned = INVALID_FN.sub("_", name).strip().rstrip(".")
    return cleaned or "deck"


def resolve_bracket(deck_json: dict) -> int:
    """Pick the most authoritative bracket from a Moxfield deck JSON.

    `bracket` is Moxfield's confirmed bracket. `userBracket` is owner-claimed.
    `autoBracket` is the algorithm's guess. Prefer them in that order; default
    to 0 (unknown) if none are set.
    """
    for key in ("bracket", "userBracket", "autoBracket"):
        v = deck_json.get(key)
        if isinstance(v, int) and 1 <= v <= 5:
            return v
    return 0


def deck_destination(deck_name: str, bracket: int, base: Path = DECK_OUT_DIR) -> Path:
    """Compute the on-disk path for an imported deck.

    Flat layout: all decks land directly in `commander/`. Bracket is encoded in
    the filename suffix `[B<n>]` so it shows up in Forge's deck picker. Subfolders
    were tried first, but Forge 2.0.12 sim mode doesn't recurse them.
    """
    suffix = f" [B{bracket}]" if bracket else " [B?]"
    return base / f"{safe_filename(deck_name)}{suffix}.dck"


def import_deck(url_or_id: str, out_dir: Path = DECK_OUT_DIR) -> Path:
    deck_id = parse_deck_id(url_or_id)
    print(f"Fetching {deck_id} from Moxfield...")
    deck_json = fetch_deck(deck_id)

    fmt = (deck_json.get("format") or "").lower()
    if fmt and fmt != "commander":
        print(f"  WARN: deck format is '{fmt}', not 'commander'. Importing anyway.")

    bracket = resolve_bracket(deck_json)
    dck = to_dck(deck_json)
    out_path = deck_destination(deck_json.get("name", deck_id), bracket, out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dck, encoding="utf-8")

    boards = deck_json.get("boards", {})
    cmdr_count = sum(c.get("quantity", 0) for c in boards.get("commanders", {}).get("cards", {}).values())
    main_count = sum(c.get("quantity", 0) for c in boards.get("mainboard", {}).get("cards", {}).values())
    print(f"  Wrote {out_path.relative_to(out_dir)} ({cmdr_count} commander + {main_count} main, bracket {bracket or '?'})")
    return out_path


def import_by_bracket(
    bracket: int,
    count: int,
    out_dir: Path = DECK_OUT_DIR,
    max_pages: int = 5,
) -> list[Path]:
    """Pull `count` decks that ACTUALLY match the requested bracket.

    Moxfield's search filter returns near-bracket decks too, so we re-check each
    deck's bracket after fetching and discard mismatches. Pages through results
    until `count` strict matches are imported or `max_pages` is exhausted.
    """
    print(f"Searching Moxfield for bracket-{bracket} commander decks...")
    written: list[Path] = []
    seen: set[str] = set()
    page = 1
    while len(written) < count and page <= max_pages:
        results = search_decks(bracket, page_size=20, page=page)
        if not results:
            break
        for entry in results:
            if len(written) >= count:
                break
            pid = entry.get("publicId")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            try:
                deck_json = fetch_deck(pid)
            except Exception as exc:
                print(f"  ERROR fetching {pid}: {type(exc).__name__}: {exc}")
                continue
            actual = resolve_bracket(deck_json)
            if actual != bracket:
                # Skip near-bracket result; don't pollute the requested bucket.
                continue
            try:
                path = _write_deck(deck_json, actual, out_dir)
                written.append(path)
            except Exception as exc:
                print(f"  ERROR writing {pid}: {type(exc).__name__}: {exc}")
        page += 1
    if len(written) < count:
        print(f"  WARN: only got {len(written)} of {count} requested for bracket {bracket}.")
    return written


def _write_deck(deck_json: dict, bracket: int, out_dir: Path) -> Path:
    """Write a fetched deck JSON to disk and report counts."""
    fmt = (deck_json.get("format") or "").lower()
    if fmt and fmt != "commander":
        print(f"  WARN: deck format is '{fmt}', not 'commander'. Importing anyway.")
    dck = to_dck(deck_json)
    out_path = deck_destination(deck_json.get("name", "deck"), bracket, out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dck, encoding="utf-8")
    boards = deck_json.get("boards", {})
    cmdr_count = sum(c.get("quantity", 0) for c in boards.get("commanders", {}).get("cards", {}).values())
    main_count = sum(c.get("quantity", 0) for c in boards.get("mainboard", {}).get("cards", {}).values())
    print(f"  Wrote {out_path.relative_to(out_dir)} ({cmdr_count} commander + {main_count} main, bracket {bracket or '?'})")
    return out_path


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="moxfield_import",
        description="Import Moxfield Commander decks into Forge's userdata, organized by bracket.",
    )
    p.add_argument(
        "decks",
        nargs="*",
        help="Moxfield URLs or deck IDs to import individually.",
    )
    p.add_argument(
        "--bracket",
        action="append",
        type=int,
        default=[],
        help="Bracket level (1-5) to bulk-import. Repeat with --count to pull from multiple brackets.",
    )
    p.add_argument(
        "--count",
        action="append",
        type=int,
        default=[],
        help="How many decks to pull per --bracket (parallel-positional with --bracket).",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    if not args.decks and not args.bracket:
        _build_argparser().print_help()
        return 2

    failures = 0

    for url_or_id in args.decks:
        try:
            import_deck(url_or_id)
        except Exception as exc:
            print(f"  ERROR importing {url_or_id}: {type(exc).__name__}: {exc}")
            failures += 1

    if args.bracket:
        # Pair --bracket with --count positionally; fall back to 3 if no count given.
        counts: Iterable[int] = args.count if args.count else [3] * len(args.bracket)
        # Pad counts if shorter than brackets
        if len(args.count) < len(args.bracket):
            counts = list(args.count) + [3] * (len(args.bracket) - len(args.count))
        for bracket, count in zip(args.bracket, counts):
            if not 1 <= bracket <= 5:
                print(f"  ERROR: bracket {bracket} out of range (1-5). Skipping.")
                failures += 1
                continue
            import_by_bracket(bracket, count)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
