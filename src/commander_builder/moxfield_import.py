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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DECK_OUT_DIR = REPO_ROOT / "vendor" / "forge" / "userdata" / "decks" / "commander"
API_BASE = "https://api2.moxfield.com/v3/decks/all"
SEARCH_BASE = "https://api2.moxfield.com/v2/decks/search"
USER_AGENT = "commander-builder/0.1 (+https://github.com/LlamaAdam/commander-builder)"

# Be polite to Moxfield — sequential fetches with a small sleep avoid 429s.
FETCH_SLEEP_SEC = 1.0

# Default harvest recipe per bracket: ~60 decks across three discovery axes.
# Categories overlap (a salty deck is often well-liked); the dedupe-by-publicId
# pass means a multi-category match counts once and we backfill from the next
# category. 60-day window keeps "recent" pulls relevant to current meta.
#
# `views` was tried initially but underperformed badly (~1-5/12 per bracket):
# the intersection of "high-view" and "updated last 60 days" is near-empty
# because high-view decks are historic. Replaced with `created` for fresh
# brews + the more generous `likes` weight.
#
# Sized to give the curator's round-robin enough variety: 12 candidates ÷ 60
# pool ≈ 5x oversample, so qualifier picks can prefer high-pilotability decks
# without exhausting the bracket. .dck files are ~1-2KB; storage is irrelevant.
HARVEST_RECIPE: list[tuple[str, int, Optional[int]]] = [
    ("likes",   27, None),  # well-liked, all-time
    ("created", 18, 60),    # recently created brews (last 60 days)
    ("updated", 15, 60),    # recently updated (active maintenance)
]

# Forge `.dck` filenames must avoid characters that the Windows filesystem
# rejects. Trailing dots/spaces also break things. Keep this conservative.
INVALID_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Forge 2.0.12 mangles non-ASCII characters in deck filenames when launched on
# Windows: emoji/symbols get replaced with `?` in stdout, then Forge fails to
# locate the deck on disk (`No deck found ... ?? ...`). Stick to ASCII.
NON_ASCII = re.compile(r"[^\x00-\x7f]")


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
    since_iso: Optional[str] = None,
) -> list[dict]:
    """Return search results for commander decks at the given bracket.

    Moxfield's `bracket` filter is loose — it surfaces near-bracket decks too —
    so callers must re-check each fetched deck's actual bracket. The historic
    `sortType=views` quirk is partially controlled by passing `since_iso`,
    which Moxfield accepts as `lastUpdatedAtUtcStart`; client-side filtering
    catches anything the server ignores.
    """
    params = {
        "pageNumber": str(page),
        "pageSize": str(page_size),
        "sortType": sort_type,
        "sortDirection": "descending",
        "fmt": "commander",
        "bracket": str(bracket),
    }
    if since_iso:
        # Moxfield's date-window param name varies by endpoint version. We send
        # what's most likely to be honored; the importer also enforces the
        # window client-side after fetching the deck JSON, so a missed param
        # here just means more wasted fetches, not bad data.
        params["lastUpdatedAtUtcStart"] = since_iso
    url = f"{SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    payload = _http_get_json(url)
    return payload.get("data", [])


CARD_SEARCH_BASE = "https://api2.moxfield.com/v2/cards/search"


def lookup_moxfield_card_id(card_name: str) -> Optional[str]:
    """Resolve a card name to Moxfield's internal card ID.

    Moxfield's deck-search API expects `commanderCardId`, not the card name.
    We first hit the card-search endpoint for an exact-name lookup, then
    return the matching card's id. None if no exact match (Moxfield's
    search is fuzzy, so we filter)."""
    try:
        resp = _http_get_json(
            f"{CARD_SEARCH_BASE}?q={urllib.parse.quote(card_name)}&limit=10"
        )
    except Exception as exc:  # noqa: BLE001
        # Surface the failure unconditionally — a silent None here
        # propagates as "no bracket peers found" and the operator never
        # learns Moxfield was unreachable / rate-limiting.
        print(
            f"WARN: Moxfield card-search failed for {card_name!r} "
            f"({type(exc).__name__}: {exc}); skipping peer lookup.",
            flush=True,
        )
        return None
    target = card_name.lower().strip()
    for card in resp.get("data", []) or []:
        if (card.get("name") or "").lower() == target:
            return card.get("id")
    return None


def find_top_liked_deck_for_commander(
    commander_name: str,
    bracket: Optional[int] = None,
    page_size: int = 10,
    verbose: bool = False,
) -> Optional[dict]:
    """Find the most-liked Moxfield deck whose primary commander matches
    `commander_name`. Used by `commander-meta-test` to auto-pull a reference
    build without making the user paste a URL.

    Strategy:
      1. Resolve the commander name → Moxfield card ID via card-search.
      2. Search decks filtered by `commanderCardId` + sort by likes desc.
      3. Confirm the top hit's commander matches (defensive — sometimes the
         search returns near-matches even with the ID filter).
      4. Fetch the full deck JSON via the existing `fetch_deck`.

    Returns None on any failure. `verbose=True` prints each step's outcome
    so users can diagnose why it didn't find a deck."""
    card_id = lookup_moxfield_card_id(commander_name)
    if not card_id:
        if verbose:
            print(f"  [moxfield] could not resolve card ID for {commander_name!r}.",
                  flush=True)
        return None
    if verbose:
        print(f"  [moxfield] commander card ID: {card_id}", flush=True)

    params = {
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumn": "likes",
        "sortDirection": "descending",
        "fmt": "commander",
        "commanderCardId": card_id,
    }
    if bracket and 1 <= bracket <= 5:
        params["bracket"] = str(bracket)
    url = f"{SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    try:
        payload = _http_get_json(url)
    except Exception as exc:
        if verbose:
            print(f"  [moxfield] search HTTP failed: {type(exc).__name__}: {exc}",
                  flush=True)
        return None
    results = payload.get("data", []) or []
    if verbose:
        print(f"  [moxfield] search returned {len(results)} deck(s).", flush=True)

    target_lower = commander_name.lower()
    for entry in results:
        commanders = entry.get("commanders", []) or []
        names = [(c.get("name") or "").lower() for c in commanders]
        if target_lower in names or any(target_lower in n for n in names):
            pid = entry.get("publicId") or entry.get("id")
            if not pid:
                continue
            try:
                return fetch_deck(pid)
            except Exception as exc:
                if verbose:
                    print(f"  [moxfield] fetch_deck({pid}) failed: {exc}", flush=True)
                continue

    # Even without a strict commander-name match, the top result by likes
    # was filtered by commanderCardId so it's almost certainly correct.
    # Fall back to the top result.
    if results:
        pid = results[0].get("publicId") or results[0].get("id")
        if pid:
            try:
                return fetch_deck(pid)
            except Exception as exc:
                # Match the verbose-logging shape of the strict-match
                # loop above — without this, the fallback path was the
                # only silent failure in the whole resolver.
                if verbose:
                    print(
                        f"  [moxfield] fallback fetch_deck({pid}) failed: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
    return None


def find_top_liked_decks_for_commander(
    commander_name: str,
    bracket: Optional[int] = None,
    n: int = 5,
    verbose: bool = False,
) -> list[dict]:
    """Find up to ``n`` highest-liked Moxfield decks for ``commander_name``.

    Multi-deck variant of ``find_top_liked_deck_for_commander``. Used by
    the bracket-peers advisor mode in ``improvement_advisor`` to source
    the references whose card overlap drives the recommendation.

    Strategy mirrors the singular variant:
      1. Resolve commander → Moxfield card ID via card-search.
      2. Search decks filtered by ``commanderCardId`` + sort by likes
         desc, optionally narrowed to a bracket.
      3. Walk the result list, fetching each deck's full JSON until we
         have ``n`` distinct decks. Skip duplicate ``publicId`` (paging
         glitches occasionally return the same deck twice) and individual
         fetch failures (one 4xx shouldn't kill the whole set).

    Returns ``[]`` on any unrecoverable failure (no card-ID, search
    error, all fetches failed). Callers fall back to a sparser source.
    """
    if n <= 0:
        return []
    card_id = lookup_moxfield_card_id(commander_name)
    if not card_id:
        if verbose:
            print(f"  [moxfield] could not resolve card ID for {commander_name!r}.",
                  flush=True)
        return []

    params = {
        "pageNumber": "1",
        # Pull a bigger search window than ``n`` so duplicates and
        # commander-mismatches don't shrink the result below the goal.
        "pageSize": str(max(n * 3, 15)),
        "sortColumn": "likes",
        "sortDirection": "descending",
        "fmt": "commander",
        "commanderCardId": card_id,
    }
    if bracket and 1 <= bracket <= 5:
        params["bracket"] = str(bracket)
    url = f"{SEARCH_BASE}?{urllib.parse.urlencode(params)}"
    try:
        payload = _http_get_json(url)
    except Exception as exc:
        if verbose:
            print(f"  [moxfield] search HTTP failed: {type(exc).__name__}: {exc}",
                  flush=True)
        return []
    results = payload.get("data", []) or []
    if verbose:
        print(f"  [moxfield] search returned {len(results)} candidate(s).",
              flush=True)

    decks: list[dict] = []
    seen_ids: set[str] = set()
    target_lower = commander_name.lower()
    for entry in results:
        if len(decks) >= n:
            break
        pid = entry.get("publicId") or entry.get("id")
        if not pid or pid in seen_ids:
            continue
        # Best-effort commander match. If the search already filtered by
        # commanderCardId, a near-miss here is rare; when it does happen
        # the deck is probably still close enough, but we prefer strict
        # matches first.
        commanders = entry.get("commanders", []) or []
        names = [(c.get("name") or "").lower() for c in commanders]
        if names and not (target_lower in names
                          or any(target_lower in n2 for n2 in names)):
            continue
        try:
            deck_json = fetch_deck(pid)
        except Exception as exc:
            if verbose:
                print(f"  [moxfield] fetch_deck({pid}) failed: {exc}",
                      flush=True)
            continue
        # Bracket re-verification. Moxfield's search-side `bracket`
        # filter is documented as loose — it surfaces near-bracket
        # decks too — so a B4 audit could otherwise read recs from
        # B3/B5 decks mixed in, defeating the whole point of sourcing
        # from same-bracket builds. When a bracket filter is active,
        # check resolve_bracket() (which prefers Moxfield's confirmed
        # `bracket` field, then userBracket, then autoBracket) and
        # drop mismatches.
        if bracket and 1 <= bracket <= 5:
            actual = resolve_bracket(deck_json)
            if actual != bracket:
                if verbose:
                    print(
                        f"  [moxfield] {pid} bracket={actual} mismatched "
                        f"requested {bracket}; dropping.",
                        flush=True,
                    )
                continue
        seen_ids.add(pid)
        decks.append(deck_json)
    return decks


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
    public_id = deck_json.get("publicId", "")
    boards = deck_json.get("boards", {})

    commanders = list(boards.get("commanders", {}).get("cards", {}).values())
    main = list(boards.get("mainboard", {}).get("cards", {}).values())

    lines: list[str] = []
    lines.append("[metadata]")
    lines.append(f"Name={name}")
    # `Moxfield=<publicId>` lets re-harvests dedupe against on-disk decks without
    # an external index file. Forge tolerates unknown metadata keys (verified —
    # decks with Moxfield= load identically to decks without).
    if public_id:
        lines.append(f"Moxfield={public_id}")
    if commanders:
        lines.append("[Commander]")
        for c in commanders:
            lines.append(card_line(c))
    lines.append("[Main]")
    for c in main:
        lines.append(card_line(c))
    return "\n".join(lines) + "\n"


_MOXFIELD_META = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)


def _existing_moxfield_ids(out_dir: Path, bracket: int) -> set[str]:
    """Scan on-disk decks at this bracket and return their stored Moxfield
    publicIds. Decks imported before the publicId-in-metadata patch land are
    invisible to this scan — caller still gets best-effort dedupe via the
    skip-if-destination-exists check inside `_write_deck`.

    Note: globbing `*[B<n>].dck` doesn't work — pathlib treats the brackets as
    a character class. Glob `*.dck` and filter by suffix instead."""
    suffix = f" [B{bracket}].dck"
    out: set[str] = set()
    for path in out_dir.glob("*.dck"):
        if not path.name.endswith(suffix):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _MOXFIELD_META.search(text)
        if m:
            out.add(m.group(1).strip())
    return out


def safe_filename(name: str) -> str:
    cleaned = INVALID_FN.sub("_", name)
    cleaned = NON_ASCII.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
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


def deck_destination(
    deck_name: str,
    bracket: int,
    base: Path = DECK_OUT_DIR,
    is_user: bool = False,
) -> Path:
    """Compute the on-disk path for an imported deck.

    Flat layout: all decks land directly in `commander/`. Bracket is encoded in
    the filename suffix `[B<n>]`, and user-owned test decks get a `[USER] `
    prefix so the orchestrator can distinguish them from the opponent pool.
    Subfolders were tried first, but Forge 2.0.12 sim mode doesn't recurse them.
    """
    bracket_suffix = f" [B{bracket}]" if bracket else " [B?]"
    prefix = "[USER] " if is_user else ""
    return base / f"{prefix}{safe_filename(deck_name)}{bracket_suffix}.dck"


def _uniquify(path: Path) -> Path:
    """Return `path` if free, else append ` (2)`, ` (3)`, ... before the suffix.

    Sanitization (NON_ASCII strip, INVALID_FN substitution) can collapse two
    distinct deck names onto the same filename — e.g. "Blue Farm 🐮" and
    "Blue Farm" both flatten to `Blue Farm [B5].dck`. Without this, the second
    write silently overwrites the first."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    for n in range(2, 100):
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    # Pathological: 99 collisions on the same sanitized name. Refuse rather
    # than silently overwrite — this is exactly the bug `_uniquify` exists to
    # prevent.
    raise RuntimeError(f"_uniquify exhausted suffixes for {path}")


def import_deck(
    url_or_id: str,
    out_dir: Path = DECK_OUT_DIR,
    is_user: bool = False,
) -> Path:
    deck_id = parse_deck_id(url_or_id)
    print(f"Fetching {deck_id} from Moxfield...")
    deck_json = fetch_deck(deck_id)

    fmt = (deck_json.get("format") or "").lower()
    if fmt and fmt != "commander":
        print(f"  WARN: deck format is '{fmt}', not 'commander'. Importing anyway.")

    bracket = resolve_bracket(deck_json)
    dck = to_dck(deck_json)
    out_path = deck_destination(deck_json.get("name", deck_id), bracket, out_dir, is_user)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path = _uniquify(out_path)
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
    max_pages: int = 8,
    sort_type: str = "updated",
    since_days: Optional[int] = None,
    seen: Optional[set[str]] = None,
) -> list[Path]:
    """Pull `count` decks that ACTUALLY match the requested bracket.

    Moxfield's search filter returns near-bracket decks too, so we re-check each
    deck's bracket after fetching and discard mismatches. Pages through results
    until `count` strict matches are imported or `max_pages` is exhausted.

    `seen` is the deduplication set across categories — pass the same set into
    multiple calls to avoid duplicate writes when running the harvest recipe.
    `since_days` enforces a recency window both server-side (best-effort) and
    client-side via the deck's `lastUpdatedAtUtc` field.
    """
    cutoff: Optional[datetime] = None
    since_iso: Optional[str] = None
    if since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        since_iso = cutoff.isoformat().replace("+00:00", "Z")

    window_label = f"last_{since_days}d" if since_days else "all_time"
    print(f"Searching Moxfield for B{bracket} [{sort_type}, {window_label}]...")
    written: list[Path] = []
    if seen is None:
        seen = set()
    page = 1
    while len(written) < count and page <= max_pages:
        try:
            results = search_decks(
                bracket,
                page_size=20,
                page=page,
                sort_type=sort_type,
                since_iso=since_iso,
            )
        except Exception as exc:
            # Network blips during search would otherwise abort the whole
            # multi-bracket harvest. Log + try the next page; if the network is
            # truly down, max_pages will run out and we move on cleanly.
            print(f"  ERROR searching B{bracket} page {page}: {type(exc).__name__}: {exc}")
            page += 1
            continue
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
                time.sleep(FETCH_SLEEP_SEC)
            except Exception as exc:
                print(f"  ERROR fetching {pid}: {type(exc).__name__}: {exc}")
                continue
            if cutoff and not _within_window(deck_json, cutoff):
                continue
            actual = resolve_bracket(deck_json)
            if actual != bracket:
                # Skip near-bracket result; don't pollute the requested bucket.
                continue
            try:
                path = _write_deck(deck_json, actual, out_dir)
                if path is not None:
                    written.append(path)
            except Exception as exc:
                print(f"  ERROR writing {pid}: {type(exc).__name__}: {exc}")
        page += 1
    if len(written) < count:
        print(f"  WARN: only got {len(written)} of {count} requested for B{bracket} [{sort_type}].")
    return written


def _within_window(deck_json: dict, cutoff: datetime) -> bool:
    """True if the deck's last update is at or after `cutoff`. Defaults to True
    if the timestamp can't be parsed — better to keep a possibly-old deck than
    discard one due to a parsing edge case."""
    ts = deck_json.get("lastUpdatedAtUtc") or deck_json.get("createdAtUtc")
    if not ts:
        return True
    try:
        deck_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    return deck_ts >= cutoff


def harvest_bracket(
    bracket: int,
    out_dir: Path = DECK_OUT_DIR,
    recipe: list[tuple[str, int, Optional[int]]] = HARVEST_RECIPE,
) -> list[Path]:
    """Run the multi-axis harvest recipe for one bracket.

    Categories share a single `seen` set so a deck that qualifies under multiple
    axes (e.g. well-liked AND recently viewed) only writes once and the slot is
    backfilled from the next category. This keeps duplicate fetches minimal.
    """
    print(f"\n=== Harvesting B{bracket}: {sum(c for _, c, _ in recipe)}-deck mix ===")
    seen: set[str] = _existing_moxfield_ids(out_dir, bracket)
    if seen:
        print(f"  ({len(seen)} already on disk with Moxfield metadata, skipping those)")
    all_written: list[Path] = []
    for sort_type, count, since_days in recipe:
        written = import_by_bracket(
            bracket, count, out_dir,
            sort_type=sort_type,
            since_days=since_days,
            seen=seen,
        )
        all_written.extend(written)
    print(f"=== B{bracket} done: {len(all_written)} unique decks ===\n")
    return all_written


def _write_deck(deck_json: dict, bracket: int, out_dir: Path) -> Optional[Path]:
    """Write a fetched deck JSON to disk and report counts.

    Returns None if a deck with this exact destination filename already exists
    (treated as a dup of a pre-publicId-metadata deck). Returns the new path
    otherwise. `_uniquify` only fires on genuine name collisions across
    different decks — not on re-fetches of the same deck."""
    fmt = (deck_json.get("format") or "").lower()
    if fmt and fmt != "commander":
        print(f"  WARN: deck format is '{fmt}', not 'commander'. Importing anyway.")
    dck = to_dck(deck_json)
    out_path = deck_destination(deck_json.get("name", "deck"), bracket, out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        # Existing deck without Moxfield= metadata. Skip rather than
        # re-write, since we can't tell if it's actually the same deck.
        # The skip is safe because the user already has a deck under that name
        # at that bracket — close enough for the curator's purposes.
        print(f"  SKIP {out_path.name} (already on disk)")
        return None
    out_path = _uniquify(out_path)
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
        "--user",
        action="store_true",
        help="Mark imported decks as the user's test deck (filename gets a [USER] prefix).",
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
    p.add_argument(
        "--sort",
        action="append",
        default=[],
        choices=["likes", "views", "updated", "created"],
        help="Sort axis per --bracket (parallel-positional, default 'updated').",
    )
    p.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Restrict --bracket pulls to decks updated within the last N days.",
    )
    p.add_argument(
        "--harvest",
        action="append",
        type=int,
        default=[],
        help="Run the full mixed-recipe harvest for the given bracket. Repeatable.",
    )
    return p


# ---------------------------------------------------------------------------
# Bulk import — multi-URL ingest with politeness + dedup + per-URL outcomes
# ---------------------------------------------------------------------------
#
# The single-deck import_deck path is fine for one-at-a-time use, but a user
# pasting a textarea of Moxfield URLs (or piping a curated list through a
# batch driver) needs:
#
#   - Polite serial fetches with FETCH_SLEEP_SEC between requests so Moxfield
#     doesn't rate-limit us mid-batch.
#   - Per-URL outcome tracking so the UI can show "5 succeeded, 2 duplicates,
#     1 failed (404)" instead of fail-fast on the first error.
#   - In-batch dedup so the same URL pasted twice writes once.
#   - On-disk dedup so re-pasting a URL whose .dck is already present is a
#     no-op, not a numbered duplicate ([USER] Foo (2) [B3].dck).
#
# Output is BulkImportResult, JSON-serializable via to_dict().


from dataclasses import dataclass, field


@dataclass
class BulkImportResult:
    """Aggregate outcome of a bulk_import() call.

    Three buckets — successes / duplicates / failures — each holding
    per-URL dicts the UI iterates to render a result table.
    """
    successes: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.success_count + self.duplicate_count + self.failure_count

    @property
    def success_count(self) -> int:
        return len(self.successes)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicates)

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    def to_dict(self) -> dict:
        return {
            "successes": list(self.successes),
            "duplicates": list(self.duplicates),
            "failures": list(self.failures),
            "total": self.total,
            "success_count": self.success_count,
            "duplicate_count": self.duplicate_count,
            "failure_count": self.failure_count,
        }


def bulk_import(
    urls: list[str],
    out_dir: Path = DECK_OUT_DIR,
    is_user: bool = True,
    *,
    sleep_sec: float = FETCH_SLEEP_SEC,
) -> BulkImportResult:
    """Import multiple Moxfield decks with polite rate-limiting + dedup.

    Each URL is processed sequentially. After every fetch (except the last)
    we sleep ``sleep_sec`` to avoid hammering Moxfield. Per-URL outcomes
    land in one of three buckets in the returned ``BulkImportResult``:

      successes — wrote a new .dck. dict carries {url, deck_id, path}.
      duplicates — .dck already on disk (or a prior URL in this batch
        already imported it). dict carries {url, deck_id, existing_path,
        reason}.
      failures — fetch error, parse error, etc. dict carries {url, error}.

    Blank / whitespace-only URLs are silently dropped. Defaults to
    is_user=True since the typical bulk-import use case is the user
    populating their own deck folder from a Moxfield reading list.
    """
    result = BulkImportResult()
    # Track deck_ids we've already imported within this batch so duplicates
    # WITHIN the input list are surfaced (not silently dropped).
    seen_in_batch: set[str] = set()

    valid_urls = [u.strip() for u in urls if u and u.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, url in enumerate(valid_urls):
        try:
            deck_id = parse_deck_id(url)
            if deck_id in seen_in_batch:
                result.duplicates.append({
                    "url": url,
                    "deck_id": deck_id,
                    "reason": "duplicate URL within this batch",
                })
                continue

            deck_json = fetch_deck(deck_id)
            # Politeness: sleep AFTER the fetch (so we wait BEFORE the next
            # request) but not after the last one in the batch.
            if idx < len(valid_urls) - 1:
                time.sleep(sleep_sec)

            bracket = resolve_bracket(deck_json)
            dest = deck_destination(
                deck_json.get("name", deck_id),
                bracket, out_dir, is_user=is_user,
            )
            if dest.exists():
                result.duplicates.append({
                    "url": url,
                    "deck_id": deck_id,
                    "existing_path": str(dest),
                    "reason": "file already on disk",
                })
                seen_in_batch.add(deck_id)
                continue

            dck = to_dck(deck_json)
            dest.write_text(dck, encoding="utf-8")
            seen_in_batch.add(deck_id)
            result.successes.append({
                "url": url,
                "deck_id": deck_id,
                "path": str(dest),
            })
        except Exception as exc:  # noqa: BLE001 — partial batch shouldn't crash
            result.failures.append({
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return result


def bulk_main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``commander-bulk-import <urls.txt>``.

    Reads one URL per line from a file argument or stdin. Prints a
    human-readable summary by default; ``--json`` swaps to a machine-
    readable payload a batch driver can pipe into jq.

    Exit codes:
      0  at least one URL succeeded (or all were duplicates — nothing failed)
      1  every URL failed (network down, all 404s, etc.)
      2  invocation error (input file missing)
    """
    import sys

    p = argparse.ArgumentParser(
        prog="commander-bulk-import",
        description=(
            "Import many Moxfield decks at once with polite rate-limiting "
            "and dedup. Reads URLs/ids one per line."
        ),
    )
    p.add_argument(
        "input", nargs="?",
        help="Path to a file with one URL per line. Omit to read from stdin.",
    )
    p.add_argument(
        "--out-dir", default=str(DECK_OUT_DIR),
        help="Destination directory for the .dck files.",
    )
    p.add_argument(
        "--opponent", action="store_true",
        help="Write decks as opponent-pool (no [USER] prefix). "
             "Default: [USER]-prefixed (the typical bulk-import use case).",
    )
    p.add_argument(
        "--sleep-sec", type=float, default=FETCH_SLEEP_SEC,
        help="Seconds to wait between Moxfield fetches (default 1.0).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit BulkImportResult as JSON on stdout.",
    )
    args = p.parse_args(argv)

    if args.input:
        in_path = Path(args.input)
        if not in_path.exists():
            print(f"ERROR: input file not found: {in_path}")
            return 2
        urls = in_path.read_text(encoding="utf-8").splitlines()
    else:
        urls = sys.stdin.read().splitlines()

    out_dir = Path(args.out_dir)
    result = bulk_import(
        urls, out_dir=out_dir,
        is_user=not args.opponent,
        sleep_sec=args.sleep_sec,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(
            f"Successes: {result.success_count} succeeded, "
            f"{result.duplicate_count} duplicates, "
            f"{result.failure_count} failed",
        )
        if result.successes:
            print()
            print("Imported:")
            for s in result.successes:
                print(f"  ✓ {s['deck_id']:24}  {Path(s['path']).name}")
        if result.duplicates:
            print()
            print("Skipped (duplicates):")
            for d in result.duplicates:
                print(f"  - {d['deck_id']:24}  {d.get('reason', '')}")
        if result.failures:
            print()
            print("Failures:")
            for f in result.failures:
                print(f"  ✗ {f['url']}  {f['error']}")

    # Exit non-zero only if EVERY URL failed (and there was at least one).
    if result.total > 0 and result.failure_count == result.total:
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)

    if not args.decks and not args.bracket and not args.harvest:
        _build_argparser().print_help()
        return 2

    failures = 0

    for url_or_id in args.decks:
        try:
            import_deck(url_or_id, is_user=args.user)
        except Exception as exc:
            print(f"  ERROR importing {url_or_id}: {type(exc).__name__}: {exc}")
            failures += 1

    if args.bracket:
        # Pair --bracket with --count and --sort positionally; pad with defaults.
        counts = list(args.count) + [3] * max(0, len(args.bracket) - len(args.count))
        sorts = list(args.sort) + ["updated"] * max(0, len(args.bracket) - len(args.sort))
        for bracket, count, sort_type in zip(args.bracket, counts, sorts):
            if not 1 <= bracket <= 5:
                print(f"  ERROR: bracket {bracket} out of range (1-5). Skipping.")
                failures += 1
                continue
            import_by_bracket(
                bracket, count,
                sort_type=sort_type,
                since_days=args.since_days,
            )

    for bracket in args.harvest:
        if not 1 <= bracket <= 5:
            print(f"  ERROR: harvest bracket {bracket} out of range (1-5). Skipping.")
            failures += 1
            continue
        harvest_bracket(bracket)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
