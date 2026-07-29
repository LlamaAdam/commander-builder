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
  4. **Bulk-data snapshot path** — ``--from-bulk`` populates snapshots
     from Scryfall's ``oracle_cards`` bulk export: one ~150MB
     rate-limit-exempt GET instead of one request per card. Built for
     the cold-store case (11,721 missing snapshots) where the per-card
     path is both slow and 429-prone.

Network calls go through ``scryfall_client`` functions so they're easy to
stub in tests; nothing here talks to Scryfall directly.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, Optional

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


# --- Per-card retry (house pattern, PRs #40/#41) ---------------------------
#
# Scryfall rate-limits with 429 + Retry-After. Before 2026-07 the per-card
# network path here had NO backoff, so a long `--all --write` run died
# mid-pass on the first 429 (urllib.error.HTTPError killed the loop).
# Mirrors archidekt_client._get_json_with_retry / edhrec_client's
# _http_get_text_with_retry: Retry-After honored and clamped, else
# exponential backoff, one stderr line per retry, bounded budget.

#: Retry budget for rate-limited / transient-5xx per-card requests.
MAX_RETRIES = 3
RETRY_BASE_DELAY_SEC = 1.0

# Same transient set as edhrec/archidekt: 429 is rate-limiting (honor
# Retry-After), 5xx is server-side weather. Other 4xx (404, 400, 403)
# are deterministic — retrying can't help, raise immediately.
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def _call_with_retry(
    fn: Callable[[], dict],
    label: str,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Call ``fn()`` with backoff on 429/5xx ``HTTPError``.

    ``sleep`` is an injection seam so tests assert the backoff schedule
    without real waiting. Non-retryable HTTPErrors (404 etc.) and
    network-level failures (URLError/OSError) propagate immediately —
    stacking sleeps onto a dead network or a deterministic 4xx only
    multiplies failure latency. Raises the last HTTPError once the
    budget is exhausted; the caller owns degrade-don't-die.
    """
    from .edhrec_client import MAX_RETRY_AFTER_SEC, _parse_retry_after

    last_exc: Optional[urllib.error.HTTPError] = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise
            last_exc = exc
        if attempt >= max_retries:
            break
        # Prefer the server's own backoff hint over our exp curve.
        hdrs = getattr(last_exc, "headers", None)
        hint = _parse_retry_after(
            hdrs.get("Retry-After") if hdrs is not None else None)
        delay = (min(hint, MAX_RETRY_AFTER_SEC) if hint is not None
                 else base_delay * (2 ** attempt))
        print(
            f"[oracle] retry {attempt + 1}/{max_retries} for {label!r} "
            f"after HTTP {last_exc.code} — sleeping {delay:.1f}s",
            file=sys.stderr, flush=True,
        )
        sleep(delay)
    assert last_exc is not None  # the loop only exits via return or here
    raise last_exc


def bulk_refresh(
    names: Optional[list[str]] = None,
    *,
    write: bool = False,
    stale_days: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Check (and optionally rewrite) oracle snapshots for ``names``.

    ``names=None`` walks the entire snapshot store. ``stale_days`` skips
    snapshots younger than that age (cheap incremental refresh).
    ``write=True`` rewrites the snapshot for any card whose oracle text
    drifted; the default is a read-only report.

    Per-card network failures get the house 429/5xx backoff
    (``_call_with_retry``); a card that stays rate-limited past the
    retry budget degrades loudly (stderr line + ``status="http_error"``
    in its result) and the run CONTINUES with the next card — a long
    ``--all --write`` pass must never die mid-run to one 429.

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

        try:
            res = _call_with_retry(
                lambda: check_errata(name), name, sleep=sleep)
        except urllib.error.HTTPError as exc:
            # Retry budget exhausted (or non-retryable code other than
            # the 404 check_errata already maps to upstream_404). Degrade
            # loudly and move on — do NOT kill the run.
            print(
                f"[oracle] giving up on {name!r}: HTTP {exc.code} "
                f"persisted past {MAX_RETRIES} retries — continuing "
                f"with the next card",
                file=sys.stderr, flush=True,
            )
            res = {"name": name, "status": "http_error", "changed": False,
                   "error": f"HTTP {exc.code}"}
        if res["status"] in ("not_cached", "corrupt", "upstream_404",
                             "http_error"):
            errors += 1
        if res.get("changed"):
            changed += 1
            if write:
                try:
                    _call_with_retry(
                        lambda: scryfall_client.refresh_card(name) or {},
                        name, sleep=sleep)
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


# --- Scryfall bulk-data snapshot path --------------------------------------
#
# One ~150MB GET replaces tens of thousands of per-card requests when the
# snapshot store is cold (the 2026-07 corpus-mining run found 11,721 cards
# with no snapshot; refreshing them one request at a time died on a 429).
# Per Scryfall's docs the bulk-data download_uri is served from CDN and is
# EXPLICITLY exempt from the per-request rate limits, so this path never
# needs the politeness sleep or the 429 backoff.

BULK_INDEX_URL = "https://api.scryfall.com/bulk-data"

#: A local bulk file younger than this is reused instead of re-downloaded
#: (Scryfall regenerates bulk exports daily; oracle text churns far more
#: slowly than that). ``--force-bulk`` overrides.
BULK_FRESH_DAYS = 7.0

_BULK_FILE_GLOB = "oracle-cards-*.json"


def bulk_data_dir() -> Path:
    """Directory the bulk oracle-cards file downloads into.

    Derived from ``scryfall_client.CACHE_DIR`` AT CALL TIME (tests
    monkeypatch that per test): a sibling ``bulk/`` next to the snapshot
    dir — ``mtg_cards/bulk/`` on the canonical layout, ``.cache/bulk/``
    on the fallback layout.
    """
    return scryfall_client.CACHE_DIR.parent / "bulk"


def find_fresh_bulk_file(max_age_days: float = BULK_FRESH_DAYS) -> Optional[Path]:
    """Newest local ``oracle-cards-*.json`` younger than ``max_age_days``,
    or ``None`` when there isn't one (missing dir, no files, all stale)."""
    d = bulk_data_dir()
    if not d.is_dir():
        return None
    best: Optional[Path] = None
    best_mtime = 0.0
    for p in d.glob(_BULK_FILE_GLOB):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = p, mtime
    if best is None:
        return None
    age_days = (time.time() - best_mtime) / 86400.0
    return best if age_days < max_age_days else None


def _http_open_stream(url: str):
    """Open ``url`` for streaming reads (the ~150MB bulk GET). Injection
    seam — tests hand ``download_bulk_oracle`` a fake opener instead."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": scryfall_client.USER_AGENT,
                 "Accept": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=300)


def download_bulk_oracle(
    *,
    force: bool = False,
    fetch_json: Optional[Callable[[str], dict]] = None,
    open_stream: Optional[Callable[[str], object]] = None,
) -> Path:
    """Ensure a local copy of Scryfall's ``oracle_cards`` bulk file and
    return its path.

    Reuses a fresh (< ``BULK_FRESH_DAYS`` days) local copy unless
    ``force``. Otherwise fetches the bulk-data index, picks the
    ``oracle_cards`` entry, and streams its ``download_uri`` to a dated
    filename (``oracle-cards-YYYYMMDD.json``) under ``bulk_data_dir()``,
    via a ``.part`` temp file so a killed download never masquerades as
    a complete one. Raises ``RuntimeError`` when the index carries no
    usable ``oracle_cards`` entry; network errors propagate.
    """
    if not force:
        fresh = find_fresh_bulk_file()
        if fresh is not None:
            print(
                f"[oracle-bulk] reusing {fresh} "
                f"(<{BULK_FRESH_DAYS:g} days old; --force-bulk to re-download)",
                file=sys.stderr, flush=True,
            )
            return fresh

    get = fetch_json or scryfall_client._http_get_json
    time.sleep(scryfall_client.REQUEST_SLEEP_SEC)  # index GET is rate-limited
    index = get(BULK_INDEX_URL)
    entry = next(
        (e for e in (index.get("data") or [])
         if isinstance(e, dict) and e.get("type") == "oracle_cards"),
        None,
    )
    if entry is None or not entry.get("download_uri"):
        raise RuntimeError(
            "Scryfall bulk-data index has no usable oracle_cards entry")

    dest_dir = bulk_data_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"oracle-cards-{time.strftime('%Y%m%d')}.json"
    tmp = dest.with_name(dest.name + ".part")
    opener = open_stream or _http_open_stream
    print(
        f"[oracle-bulk] downloading {entry['download_uri']} -> {dest}",
        file=sys.stderr, flush=True,
    )
    with opener(entry["download_uri"]) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def _front_face_name(card: dict) -> Optional[str]:
    """Front-face name of a multi-face card, or ``None`` for single-face.

    Mirrors ``lookup_card``'s convention: Scryfall's exact-named endpoint
    resolves a face name to the FULL card object, and the snapshot lands
    under the slug of the name the caller asked for. Deck files name DFC /
    split / adventure cards by their front face, so bulk-written snapshots
    must be reachable under that slug too or front-face lookups miss.
    """
    faces = card.get("card_faces")
    if not isinstance(faces, list) or not faces:
        return None
    face = faces[0]
    fname = face.get("name") if isinstance(face, dict) else None
    return fname if isinstance(fname, str) and fname.strip() else None


def _bulk_name_index(cards: list) -> dict[str, dict]:
    """Index bulk card objects by casefolded name — full names AND
    individual face names, full names winning collisions (two passes) so
    a face name can never shadow a real card's canonical name."""
    index: dict[str, dict] = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        name = card.get("name")
        if isinstance(name, str) and name.strip():
            index.setdefault(name.strip().lower(), card)
    for card in cards:
        if not isinstance(card, dict):
            continue
        for face in (card.get("card_faces") or []):
            fname = face.get("name") if isinstance(face, dict) else None
            if isinstance(fname, str) and fname.strip():
                index.setdefault(fname.strip().lower(), card)
    return index


def write_snapshots_from_bulk(
    names: Optional[list[str]],
    *,
    bulk_path: Path,
    everything: bool = False,
) -> dict:
    """Write per-card oracle snapshots from a local bulk file.

    Each snapshot is the FULL Scryfall card object, byte-format-identical
    to what ``lookup_card`` writes from ``/cards/named`` — so every
    existing reader (``lookup_card``, ``card_reference``, forge_py's
    shared-dir contract) hits without change.

    - ``names``: target set; only these get written (case-insensitive,
      face names resolve to their full card). Missing names are reported,
      not fatal.
    - ``everything=True``: ignore ``names`` and write ALL cards (~35k
      files) — each under its full-name slug, multi-face cards also under
      their front-face slug so deck-file names hit.

    Returns ``{"written": int, "missing": [str, ...], "targets": int}``.
    """
    with open(bulk_path, encoding="utf-8") as fh:
        cards = json.load(fh)
    if not isinstance(cards, list):
        raise RuntimeError(f"bulk file is not a JSON array: {bulk_path}")

    snap_dir = scryfall_client.CACHE_DIR
    snap_dir.mkdir(parents=True, exist_ok=True)

    def _write(lookup_name: str, card: dict) -> None:
        scryfall_client._cache_path(lookup_name).write_text(
            json.dumps(card), encoding="utf-8")

    written = 0
    missing: list[str] = []
    if everything:
        targets = 0
        for card in cards:
            if not isinstance(card, dict):
                continue
            name = card.get("name")
            if not (isinstance(name, str) and name.strip()):
                continue
            targets += 1
            _write(name, card)
            written += 1
            front = _front_face_name(card)
            if front and (scryfall_client._cache_path(front)
                          != scryfall_client._cache_path(name)):
                _write(front, card)
                written += 1
    else:
        index = _bulk_name_index(cards)
        targets = len(names or [])
        for name in (names or []):
            card = index.get(name.strip().lower())
            if card is None:
                missing.append(name)
                continue
            _write(name, card)
            written += 1

    # Keep the process memo coherent: a name negative-memoized as a miss
    # earlier in this process now HAS a snapshot on disk.
    scryfall_client.clear_lookup_memo()
    return {"written": written, "missing": missing, "targets": targets}


def names_from_deck_dir(deck_dir: Path) -> list[str]:
    """Distinct card names across every ``.dck`` under ``deck_dir``, in
    first-seen order (case-insensitive dedupe, same as
    ``names_from_deck``)."""
    from .deck_library_analyzer import iter_deck_files

    seen: set[str] = set()
    out: list[str] = []
    for deck_file in iter_deck_files(Path(deck_dir)):
        for name in names_from_deck(deck_file):
            key = name.lower()
            if key not in seen:
                seen.add(key)
                out.append(name)
    return out


def _main_from_bulk(args, names: Optional[list[str]]) -> int:
    """CLI backend for ``--from-bulk``: ensure a local bulk file, resolve
    the target name set, write snapshots. ``names=None`` means --all
    (every card named in the deck dir) or --everything."""
    if names is None and not args.everything:
        # --all under --from-bulk targets the DECK DIR, not the snapshot
        # store: the whole point is populating snapshots for cards that
        # don't have one yet, which a store walk can never reach.
        deck_dir = args.deck_dir
        if deck_dir is None:
            from .config_store import get_deck_dir
            deck_dir = get_deck_dir()
        if not Path(deck_dir).is_dir():
            print(f"ERROR: deck dir not found: {deck_dir}", flush=True)
            return 2
        names = names_from_deck_dir(Path(deck_dir))

    try:
        bulk_path = download_bulk_oracle(force=args.force_bulk)
    except Exception as exc:  # noqa: BLE001 — operator-facing CLI: one
        # clear line beats a traceback for "Scryfall is down" class errors.
        print(f"ERROR: bulk download failed: {type(exc).__name__}: {exc}",
              flush=True)
        return 2

    summary = write_snapshots_from_bulk(
        names, bulk_path=bulk_path,
        everything=bool(args.everything),
    )
    summary["bulk_file"] = str(bulk_path)

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Wrote {summary['written']} snapshot(s) from {bulk_path} "
          f"({summary['targets']} target(s), "
          f"{len(summary['missing'])} missing).")
    for name in summary["missing"]:
        print(f"  ? not in bulk data: {name}")
    return 0


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
                     help="Default mode: walk the entire snapshot store "
                          "(slow; one Scryfall request per cached card). "
                          "With --from-bulk: every card named in the deck "
                          "dir (see --deck-dir).")
    src.add_argument("--everything", action="store_true",
                     help="(--from-bulk only) Write a snapshot for EVERY "
                          "card in the bulk file (~35k files).")
    p.add_argument("--write", action="store_true",
                   help="Rewrite the snapshot for any drifted card "
                        "(default: report only). Implied by --from-bulk.")
    p.add_argument("--stale-days", type=float, default=None, metavar="N",
                   help="Skip snapshots younger than N days.")
    p.add_argument("--from-bulk", action="store_true",
                   help="Populate snapshots from Scryfall's oracle_cards "
                        "bulk export (ONE ~150MB rate-limit-exempt GET) "
                        "instead of one request per card. Always writes.")
    p.add_argument("--force-bulk", action="store_true",
                   help="Re-download the bulk file even if a fresh "
                        f"(<{BULK_FRESH_DAYS:g} days) local copy exists.")
    p.add_argument("--deck-dir", type=Path, default=None, metavar="DIR",
                   help="Deck directory for --from-bulk --all (default: "
                        "the configured deck dir).")
    p.add_argument("--json", action="store_true",
                   help="Emit the summary as JSON.")
    args = p.parse_args(argv)

    if args.everything and not args.from_bulk:
        print("ERROR: --everything requires --from-bulk", flush=True)
        return 2

    if args.deck is not None:
        if not args.deck.exists():
            print(f"ERROR: deck not found: {args.deck}", flush=True)
            return 2
        names: Optional[list[str]] = names_from_deck(args.deck)
    elif args.name:
        names = args.name
    else:  # --all / --everything
        names = None

    if args.from_bulk:
        return _main_from_bulk(args, names)

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
