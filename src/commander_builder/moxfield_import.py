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

from .dck_meta import rewrite_name_to_stem, stamp_name_preserving_display

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

# Filename prefix marking a deck as the USER's own test deck. This is a ROLE
# boundary, not just cosmetics: web/app.py's sidebar lists only `[USER]`-
# prefixed files, and pool_curator treats every non-`[USER]` file as an
# opponent-pool candidate. The same Moxfield deck may therefore legitimately
# exist TWICE on disk — once as the user's test copy, once as a harvested
# opponent — and same-id matching must never cross that boundary. (Match on
# the bare prefix, no trailing space, mirroring the `startswith("[USER]")`
# checks in status/app/pool_curator so all role filters agree.)
_USER_PREFIX = "[USER]"


def _is_user_deck_file(path: Path) -> bool:
    """True if the file lives on the user side of the [USER]/pool boundary."""
    return path.name.startswith(_USER_PREFIX)


# Trailing ` v<N>` version token on a stem CORE (i.e. AFTER the ` [B<n>]`
# bracket tag has been stripped). This is the machine convention shared by
# BOTH version writers:
#   - snapshot_deck.versioned_path:  `[USER] Foo [B3].dck` + "v1"
#                                      -> `[USER] Foo v1 [B3].dck`
#   - proposer._bump_version_filename: `[USER] Foo [B3].dck`
#                                      -> `[USER] Foo v2 [B3].dck`
# Both insert ` v<digits>` immediately before the bracket tag. A `(2)`
# uniquify counter is NOT a version: _uniquify's counter marks a DIFFERENT
# deck whose name sanitized onto the same filename, so `Foo (2)` must never
# be folded into `Foo`'s lineage (they carry different Moxfield ids anyway,
# but the distinction matters for how we CLASSIFY a same-id pair below).
_VERSION_TOKEN = re.compile(r"^(?P<root>.+) v(?P<ver>\d+)$")


def _lineage_root(stem: str) -> tuple[str, Optional[int]]:
    """Split a .dck filename STEM into (lineage root, version-or-None).

    `[USER] Foo v2 [B3]` -> ("[USER] Foo", 2)
    `[USER] Foo [B3]`    -> ("[USER] Foo", None)   # unversioned = the BASE
    `[USER] Foo (2) [B3]`-> ("[USER] Foo (2)", None)  # counter != version

    WHY the bracket tag is stripped from the root too: bracket drift
    (_rename_for_bracket_drift) renames only the LIVE base file's ` [B<n>]`
    tag; frozen version snapshots keep their old stems. After a drift
    rename, `[USER] Foo [B4]` (base) and `[USER] Foo v2 [B3]` (stale-tagged
    snapshot) are still ONE deck's lineage — comparing tag-stripped roots is
    what keeps them grouped. That is the whole drift-rename edge decision:
    lineage identity = stem minus bracket tag minus version token, nothing
    fancier (no bracket matching heuristics — the shared Moxfield id is
    already the identity; the root check only guards against UNRELATED
    files that happen to share an id)."""
    # _BRACKET_TAG_STEM is defined further down (module-level regex shared
    # with _uniquify) — resolved at call time, so the ordering is fine.
    m = _BRACKET_TAG_STEM.match(stem)
    core = m.group("base") if m else stem
    vm = _VERSION_TOKEN.match(core)
    if vm:
        return vm.group("root"), int(vm.group("ver"))
    return core, None


def _lineage_representative(members: list[Path]) -> tuple[Path, bool]:
    """Pick the file that represents ONE lineage (same root, same id).

    Rule (the frozen-snapshot rule): the BASE — the member WITHOUT a
    version token — is the live, mutable file; ` v<N>` members are frozen
    audit snapshots (snapshot_deck) or proposal outputs (proposer) that a
    re-pull/dedupe/revert must never treat as "the" deck. So:

      1. an unversioned member always wins;
      2. with no unversioned member on disk (base hand-deleted), the LOWEST
         version stands in — it is the oldest surviving member and the
         closest thing to the live file; minting nothing and pointing
         nowhere would break every same-id consumer.

    Returns (winner, ambiguous). `ambiguous` is True when TWO members tie
    at the winning rank — e.g. `Foo [B3]` and `Foo [B4]` both unversioned
    with the same id (a hand copy across brackets, NOT drift: drift RENAMES
    the base, it never duplicates it). That is a genuine ambiguity the
    caller should still warn about; the tie-break is sorted-first for
    determinism."""
    def rank(p: Path) -> tuple[int, int]:
        ver = _lineage_root(p.stem)[1]
        # Base (no version) outranks every version; versions sort ascending.
        return (0, 0) if ver is None else (1, ver)

    ordered = sorted(members, key=lambda p: (rank(p), p.name))
    ambiguous = len(ordered) > 1 and rank(ordered[0]) == rank(ordered[1])
    return ordered[0], ambiguous


def _resolve_same_id_group(pid: str, paths: list[Path]) -> Path:
    """Resolve several same-role files recording ONE Moxfield id to the
    single path the id map should hand out.

    LINEAGE vs COLLISION — the load-bearing distinction:

    - LINEAGE (expected, silent): the versioning writers (snapshot_deck,
      apply_proposal_to_deck) copy a deck's [metadata] verbatim, so a base
      file and its ` v<N>` copies ALL record the same Moxfield= id — by
      design, that id IS the deck's identity across versions. The deck
      sweep found 41 base/v2 [USER] pairs in this exact shape; warning on
      them (the old behavior) was 41 spurious WARNs per id-map build, and
      "sorted-first" was only accidentally the base. Same tag-stripped
      root ⇒ one deck ⇒ resolve to the base (see
      _lineage_representative), NO warning.

    - TRUE COLLISION (unexpected, loud): files whose stems are NOT one
      lineage (unrelated names, or a `(2)` uniquify sibling — a different
      deck by definition) claiming one id means someone hand-copied a .dck.
      "The" same-id destination is genuinely ambiguous: keep the loud WARN
      and the deterministic sorted-first winner, exactly the pre-lineage
      behavior. Never crash — a stray manual copy must not break imports.

    Mixed groups (a lineage PLUS an unrelated claimant) collapse each root
    to its lineage representative first — the intra-lineage part stays
    silent — then warn about the cross-root ambiguity that remains."""
    by_root: dict[str, list[Path]] = {}
    for p in paths:
        by_root.setdefault(_lineage_root(p.stem)[0], []).append(p)

    reps: list[Path] = []
    for root in sorted(by_root):
        rep, ambiguous = _lineage_representative(by_root[root])
        if ambiguous:
            # Two same-rank members of one root (hand copy) — the intra-
            # lineage pick is itself a guess; say so.
            others = ", ".join(
                p.name for p in sorted(by_root[root]) if p != rep
            )
            print(
                f"  WARN: {others} and {rep.name} both record "
                f"Moxfield={pid} at the same version; treating {rep.name} "
                f"(first sorted) as the canonical copy. Delete or re-id "
                f"the stray duplicate.",
            )
        reps.append(rep)

    if len(reps) == 1:
        return reps[0]
    winner = min(reps, key=lambda p: p.name)
    for loser in sorted(reps, key=lambda p: p.name):
        if loser != winner:
            print(
                f"  WARN: {loser.name} and {winner.name} both record "
                f"Moxfield={pid}; treating {winner.name} (first sorted) as "
                f"the canonical copy. Delete or re-id the stray duplicate.",
            )
    return winner


def _existing_moxfield_ids(
    out_dir: Path,
    bracket: Optional[int] = None,
    is_user: Optional[bool] = None,
) -> dict[str, Path]:
    """Scan on-disk decks and map each stored Moxfield publicId → its path.

    ONE directory scan builds the whole map — every writer's same-id lookup
    goes through this so a bulk run never rescans the deck dir per candidate.
    Returning the PATH (not just the id) is what lets the same-id overwrite
    semantics reach decks living under a UNIQUIFIED name: `Foo (2) [B3].dck`
    still records its own `Moxfield=` id, and the map hands the caller that
    exact file to overwrite/skip. Checking only the base destination path —
    the pre-fix behavior — saw the OTHER colliding deck there and minted a
    fresh `(3)` duplicate on every re-pull.

    ``is_user`` scopes the map to one side of the [USER]/pool role boundary
    (see ``_USER_PREFIX``): True → only `[USER]`-prefixed files, False →
    only non-`[USER]` files, None → the whole dir (role-agnostic tooling
    only). Every WRITER must pass its own role: an unscoped map made a
    user import "find" the opponent-pool copy of the same Moxfield id and
    either skip the import (bulk paths — the user could never obtain a
    `[USER]` copy) or overwrite the pool file in place (import_deck — the
    user's deck stayed invisible to the `[USER]`-only sidebar AND remained
    an opponent candidate). User copy and pool copy are different ROLES of
    the same deck; both may exist, so same-id lookups stay within a role.

    `bracket=None` scans the whole dir (the writers need same-id-anywhere
    WITHIN their role); a bracket restricts to that suffix (harvest's
    per-bracket `seen` seed). Decks imported before the publicId-in-metadata
    patch are invisible to this scan — callers still get best-effort dedupe
    via the "unknown" verdict path on the base destination.

    Several files claiming the SAME id are resolved by
    _resolve_same_id_group, which tells apart the two very different ways
    that happens:

    - a VERSION LINEAGE — base + ` v<N>` snapshots that legitimately share
      the id because the version writers preserve metadata by design —
      resolves silently to the BASE (the live file; frozen snapshots must
      never be "the" same-id target), or to the lowest version when no
      base survives;
    - a TRUE COLLISION — unrelated stems (hand-copied .dck) — keeps the
      loud WARN and the deterministic sorted-first winner.

    (A user copy + a pool copy of one id is NEITHER situation — a
    role-scoped scan sees only one of them, so the legitimate cross-role
    pair never reaches the resolver.)

    Note: globbing `*[B<n>].dck` doesn't work — pathlib treats the brackets as
    a character class. Glob `*.dck` and filter by suffix instead."""
    suffix = f" [B{bracket}].dck" if bracket is not None else None
    groups: dict[str, list[Path]] = {}
    for path in sorted(out_dir.glob("*.dck")):
        if suffix is not None and not path.name.endswith(suffix):
            continue
        if is_user is not None and _is_user_deck_file(path) != is_user:
            # Wrong side of the role boundary — invisible to this caller.
            continue
        pid = _read_moxfield_id(path)
        if pid is None:
            continue
        groups.setdefault(pid, []).append(path)
    return {
        pid: (paths[0] if len(paths) == 1
              else _resolve_same_id_group(pid, paths))
        for pid, paths in groups.items()
    }


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



# Trailing ` [B<n>]` / ` [B?]` bracket tag on a deck filename STEM. _uniquify
# must insert its counter BEFORE this tag: every bracket-aware consumer
# (status._count_decks, _existing_moxfield_ids, the orchestrator's deck
# filters) matches on the ` [B<n>].dck` filename SUFFIX, so a name like
# `Foo [B3] (2).dck` would be invisible to all of them.
_BRACKET_TAG_STEM = re.compile(r"^(?P<base>.+)(?P<tag> \[B[1-5?]\])$")


def _uniquify(path: Path) -> Path:
    """Return `path` if free, else insert ` (2)`, ` (3)`, ... into the name.

    Sanitization (NON_ASCII strip, INVALID_FN substitution) can collapse two
    distinct deck names onto the same filename — e.g. "Blue Farm 🐮" and
    "Blue Farm" both flatten to `Blue Farm [B5].dck`. Without this, the second
    write silently overwrites the first.

    The counter goes BEFORE any trailing ` [B<n>]` bracket tag
    (`Foo (2) [B3].dck`, never `Foo [B3] (2).dck`) so uniquified decks keep
    the `[B<n>].dck` suffix shape that every bracket filter keys on."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    m = _BRACKET_TAG_STEM.match(stem)
    base, tag = (m.group("base"), m.group("tag")) if m else (stem, "")
    parent = path.parent
    for n in range(2, 100):
        candidate = parent / f"{base} ({n}){tag}{suffix}"
        if not candidate.exists():
            return candidate
    # Pathological: 99 collisions on the same sanitized name. Refuse rather
    # than silently overwrite — this is exactly the bug `_uniquify` exists to
    # prevent.
    raise RuntimeError(f"_uniquify exhausted suffixes for {path}")


def _rename_for_bracket_drift(
    path: Path,
    bracket: int,
    id_map: Optional[dict[str, Path]] = None,
) -> Path:
    """Rename a same-id matched file whose ` [B<n>]` tag no longer matches
    the incoming deck's bracket. Returns the (possibly new) path.

    WHY — the same-id match keys on the recorded `Moxfield=` id, so a
    re-pull of a deck whose Moxfield bracket CHANGED lands on the old
    `[Bn]`-named file. Keeping that name forever means every filename-keyed
    bracket consumer (`_bracket_from_filename`, status._count_decks, the
    orchestrator's pool filters) serves the stale bracket for the deck's
    whole lifetime. Renaming at match time keeps the filename — the single
    source of bracket truth — honest.

    Mechanics:
    - Only the trailing bracket tag changes. The role prefix (`[USER] `),
      the base name, and any uniquify counter stay put — the counter sits
      BEFORE the tag (6ccf3f0 invariant), so swapping the tag preserves
      `Foo (2) [B3]` → `Foo (2) [B4]` with the counter still in front.
    - `bracket` outside 1-5 (Moxfield stopped reporting one) is NOT drift
      we can assert — keep the old tag rather than downgrade to `[B?]`.
    - The new name may already be owned by a DIFFERENT deck at the target
      bracket: _uniquify rather than clobber.
    - Restamp `Name=` from the renamed stem immediately (rewrite_name_to_stem
      — no DisplayName synthesis: the old bracketed stem is not a "pretty
      name" worth preserving) so the dck_meta invariant
      `_normalize(stem) == _normalize(Name=)` never dangles, even on skip
      paths that won't rewrite the file's content afterwards.
    - ``id_map`` entries pointing at the old path are repointed so bulk
      loops sharing the map keep resolving this id to a real file.
    """
    if not 1 <= bracket <= 5:
        return path
    m = _BRACKET_TAG_STEM.match(path.stem)
    if m is None:
        # No recognizable bracket tag (hand-renamed file) — nothing to fix.
        return path
    new_tag = f" [B{bracket}]"
    if m.group("tag") == new_tag:
        return path
    target = _uniquify(path.with_name(f"{m.group('base')}{new_tag}{path.suffix}"))
    path.rename(target)
    rewrite_name_to_stem(target)
    print(f"  bracket changed: renamed {path.name} -> {target.name}")
    if id_map is not None:
        for pid, p in id_map.items():
            if p == path:
                id_map[pid] = target
    return target


def _moxfield_id_from_text(text: str) -> Optional[str]:
    """Parse the `Moxfield=` publicId out of raw .dck TEXT.

    Split out from `_read_moxfield_id` so a caller holding .dck content that
    never touched disk shares the exact same parse as the on-disk reader —
    one regex, one notion of "the id". `revert_to` needs this: it resolves a
    restore target from a knowledge_log `deck_snapshot` blob (in-memory
    string), not from the file about to be overwritten.

    None when the text carries no `Moxfield=` line (bare paste, or a snapshot
    recorded before the publicId-in-metadata patch) — callers must treat that
    as "identity unknown", not "different"."""
    m = _MOXFIELD_META.search(text)
    return m.group(1).strip() if m else None


def _read_moxfield_id(path: Path) -> Optional[str]:
    """Best-effort read of the `Moxfield=` publicId recorded in a .dck.

    None when the file is unreadable or predates the publicId-in-metadata
    patch — callers must treat that as "identity unknown", not "different"."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _moxfield_id_from_text(text)


def _classify_destination(dest: Path, public_id: str) -> str:
    """Classify an import destination against the deck about to be written.

    Every import path used to decide on `dest.exists()` alone, which
    conflated two opposite situations: a re-pull of the SAME deck (should
    overwrite or dedupe-skip) and a DIFFERENT deck whose name sanitizes to
    the same filename (should get a uniquified name, never be dropped).
    The recorded `Moxfield=` publicId disambiguates. Returns:

      "free"      — nothing on disk; write normally.
      "same"      — dest records the SAME publicId: it IS this deck.
      "collision" — dest records a DIFFERENT publicId: two distinct decks
                    collapsed onto one sanitized filename.
      "unknown"   — can't tell: dest predates Moxfield= metadata, or the
                    incoming deck has no publicId. Callers keep their old
                    conservative exists() behavior for this case.

    Scope: this inspects ONE candidate path. It cannot see a same-id deck
    living under a DIFFERENT filename (a uniquified `Foo (2) [B3].dck`
    sibling), so every writer first resolves same-id-anywhere — within its
    own [USER]/pool role — via `_existing_moxfield_ids(out_dir,
    is_user=<role>)` and only consults this verdict when no file on that
    side of the role boundary records the incoming id, leaving "same" as a
    defensive dead branch in the callers. (Role-safety of THIS check is
    structural: `dest` comes from deck_destination with the caller's own
    is_user, so the path it inspects always carries the caller's role
    wrapper — a cross-role file can't occupy it.)
    """
    if not dest.exists():
        return "free"
    if not public_id:
        return "unknown"
    existing = _read_moxfield_id(dest)
    if existing is None:
        return "unknown"
    return "same" if existing == public_id else "collision"


# User-authored metadata lines carried across same-id re-imports.
# `Protect=`: pet-card locks for the proposer (see
# web/_helpers.read_protected_cards) — written locally, never present in the
# Moxfield payload, so a fresh to_dck render drops it.
_PROTECT_META = re.compile(r"^Protect=.*$", re.MULTILINE)
# `DisplayName=`: the pretty deck name stamp_name_preserving_display writes
# when safe_filename mangled the Moxfield name — and which the user may have
# hand-edited since. `.+` (not `.*`) on purpose: an EMPTIED local
# `DisplayName=` line reads as "user cleared it", so we don't carry it and
# the stamp below falls back to the fresh render's pretty name. (Mirrors
# dck_meta._DISPLAY_NAME_LINE, which also requires a value.)
_DISPLAY_META = re.compile(r"^DisplayName=.+$", re.MULTILINE)


def _merge_local_metadata(old_text: str, fresh_dck: str) -> str:
    """Carry user-authored `[metadata]` lines from the on-disk deck into a
    freshly rendered import.

    `to_dck` only regenerates `Name=`/`Moxfield=`; a plain same-id overwrite
    would silently wipe local-only metadata. Two keys are carried:

    - `Protect=` pet-card locks (all lines), and
    - `DisplayName=` (first line) — dck_meta documents that user edits to
      the display name survive re-imports, and this carry is what makes
      that true: import_deck runs this merge BEFORE
      stamp_name_preserving_display, whose "existing DisplayName wins" rule
      then sees the carried line and never synthesizes a competing one, so
      exactly one DisplayName= comes out and the LOCAL edit is it.

    Re-insert them right after the metadata block (before the first card
    section header)."""
    carried = _PROTECT_META.findall(old_text)
    local_display = _DISPLAY_META.search(old_text)
    if local_display:
        # Local DisplayName wins over the fresh render's. to_dck never emits
        # DisplayName= today, but strip any that shows up anyway — otherwise
        # the merge would produce two lines and which one a display surface
        # honors would be ordering luck.
        fresh_dck = re.sub(r"^DisplayName=.*\n?", "", fresh_dck,
                           flags=re.MULTILINE)
        carried.append(local_display.group(0))
    if not carried:
        return fresh_dck
    lines = fresh_dck.splitlines()
    # First section header AFTER [metadata] (i.e. [Commander] or [Main]) —
    # carried lines must stay inside the metadata block to be parseable.
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        if i > 0 and ln.startswith("["):
            insert_at = i
            break
    lines[insert_at:insert_at] = carried
    return "\n".join(lines) + "\n"


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
    public_id = deck_json.get("publicId", "")
    out_path = deck_destination(deck_json.get("name", deck_id), bracket, out_dir, is_user)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Same-id lookup over the whole deck dir WITHIN THIS IMPORT'S ROLE, not
    # just the base destination. Two halves to that:
    # - whole-dir: a deck that lost an earlier name collision lives under a
    #   uniquified name (`Foo (2) [B3].dck`); classifying only the base path
    #   saw the OTHER deck there, called it "collision", and minted a fresh
    #   `(3)` duplicate on every re-pull — the documented same-id overwrite
    #   (and the Protect=/DisplayName= merge below) never applied.
    # - role-scoped (is_user=is_user): the [USER]/pool boundary is the role
    #   contract every consumer keys on (sidebar lists only [USER] files;
    #   pool_curator treats the rest as opponents). An UNSCOPED match here
    #   made `commander-import --user` of a previously HARVESTED deck
    #   overwrite the pool file in place under its pool name — the user's
    #   "import" landed invisible to the [USER]-only surfaces and stayed an
    #   opponent candidate. A same-id file in the OTHER role is not "this
    #   deck already imported"; it's a different role's copy, and the two
    #   legitimately coexist. One directory scan per import call.
    same_path = (
        _existing_moxfield_ids(out_dir, is_user=is_user).get(public_id)
        if public_id else None
    )
    if same_path is not None:
        # Documented re-pull semantics (README audit-cycle step 4,
        # snapshot_deck docstring): re-importing the SAME Moxfield deck
        # overwrites the local file in place. Uniquifying here — the old
        # behavior — broke the audit A/B twice over: the v2 snapshot copied
        # the untouched v1 file (deck compared against itself), and the
        # "(2)" name fell outside the `[B<n>].dck` suffix the bracket
        # filters key on. The file KEEPS its existing base name and any
        # uniquify counter — the recorded id, not the filename, is the
        # deck's identity, and renaming the base would orphan every
        # name-keyed history row. Frozen-snapshot rule, pinned: `same_path`
        # comes from the lineage-aware id map, so when this deck also has
        # ` v<N>` snapshot/proposal copies on disk (which share the id by
        # design) the map hands back the BASE — a re-pull overwrites the
        # live file and NEVER a frozen v2 snapshot (that would silently
        # rewrite the audit A/B's "before" side). ONE exception: a stale
        # ` [B<n>]` tag.
        # The bracket lives in the filename for every bracket consumer, so
        # when Moxfield's bracket drifted since the last pull the tag must
        # follow (rename BEFORE reading, so the merge sees the final file).
        out_path = _rename_for_bracket_drift(same_path, bracket)
        dck = _merge_local_metadata(out_path.read_text(encoding="utf-8"), dck)
    else:
        # No file IN THIS ROLE records this id (a cross-role copy may well
        # exist — that's fine, this import proceeds as new). "same" can't
        # come back here — a base-path file recording this id carries this
        # role's wrapper and would have been in the map — so the only
        # occupied-destination verdicts left are:
        verdict = _classify_destination(out_path, public_id)
        if verdict in ("collision", "unknown"):
            # A DIFFERENT deck (or one we can't identify) owns this
            # filename — never clobber it; write under a uniquified name.
            out_path = _uniquify(out_path)
    # Stamp Name= from the FINAL filename stem — after any _uniquify, and
    # for a same-id overwrite the kept file's OWN stem (counter included:
    # a re-pull landing in `Foo (2) [B3].dck` stamps `Name=Foo (2) [B3]`,
    # never the base stem) — so every name-keyed consumer (Forge's picker,
    # compare_versions, pool_curator) agrees with the file on disk even
    # when safe_filename mangled the pretty Moxfield name. The pretty name
    # survives as DisplayName= for the status CLI. Must run AFTER
    # _merge_local_metadata so a re-import stamps the merged text, not a
    # soon-discarded render.
    dck = stamp_name_preserving_display(dck, out_path.stem)
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
    # Whole-dir Moxfield-id → path map, built ONCE per call — scoped to the
    # POOL role (is_user=False): this loop only writes opponent-pool files,
    # and a [USER] copy of a candidate's id must not read as "already
    # harvested" (the pool would silently stay one deck short forever).
    # _write_deck dedupes each candidate against the map (same id anywhere
    # within the role — uniquified siblings included) and refreshes it
    # after each write/rename, so the bulk loop never rescans the deck dir
    # per candidate.
    id_map = _existing_moxfield_ids(out_dir, is_user=False)
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
            except Exception as exc:
                print(f"  ERROR fetching {pid}: {type(exc).__name__}: {exc}")
                continue
            finally:
                # Politeness delay on BOTH paths. It used to sit after
                # fetch_deck inside the try, so a fetch exception moved on
                # to the next entry with zero delay — hammering Moxfield
                # exactly when it was erroring/rate-limiting. `finally`
                # runs even through the `continue` above.
                time.sleep(FETCH_SLEEP_SEC)
            if cutoff and not _within_window(deck_json, cutoff):
                continue
            actual = resolve_bracket(deck_json)
            if actual != bracket:
                # Skip near-bracket result; don't pollute the requested bucket.
                continue
            try:
                path = _write_deck(deck_json, actual, out_dir, id_map=id_map)
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
    # Keys only — harvest's `seen` is a plain id set (it also accumulates
    # ids seen in search results, which have no path yet). Per-bracket on
    # purpose: this seed only pre-skips FETCHES for decks already harvested
    # at THIS bracket; cross-bracket same-id dedupe still happens inside
    # _write_deck via its whole-dir id map. Pool role only (is_user=False):
    # a [USER] deck at this bracket is the user's copy, not a harvested
    # opponent — pre-skipping its fetch would block the pool from ever
    # getting its own copy of that deck.
    seen: set[str] = set(_existing_moxfield_ids(out_dir, bracket, is_user=False))
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


def _write_deck(
    deck_json: dict,
    bracket: int,
    out_dir: Path,
    id_map: Optional[dict[str, Path]] = None,
) -> Optional[Path]:
    """Write a fetched deck JSON to disk and report counts.

    Returns None when the deck is already on disk (same recorded Moxfield
    publicId — under ANY filename, uniquified siblings included — or an
    unidentifiable pre-metadata file under the same name; correct harvest
    dedupe either way). Returns the new path otherwise. `_uniquify` only
    fires on genuine name collisions: a DIFFERENT publicId under the same
    sanitized filename. The old exists()-before-_uniquify check silently
    dropped those distinct decks as "already on disk" — and the follow-up
    base-path-only same-id check still re-imported a deck whose earlier copy
    had lost a name collision and lived under `Foo (2) [B3].dck`.

    ``id_map`` is the POOL-role Moxfield-id → path map from
    `_existing_moxfield_ids(out_dir, is_user=False)`. Bulk loops
    (import_by_bracket) build it ONCE and pass it in — this function
    refreshes it in place after each write so later candidates dedupe
    without a per-candidate directory rescan. Standalone callers may omit
    it; we scan ourselves. Role scoping matters here too: this writer only
    ever produces opponent-pool files, so a `[USER]` copy of the same
    Moxfield id must NOT count as "already harvested" — skipping on it
    left the pool permanently missing that deck (and the user's test deck
    doing double duty as its own opponent's stand-in)."""
    fmt = (deck_json.get("format") or "").lower()
    if fmt and fmt != "commander":
        print(f"  WARN: deck format is '{fmt}', not 'commander'. Importing anyway.")
    dck = to_dck(deck_json)
    public_id = deck_json.get("publicId", "")
    out_path = deck_destination(deck_json.get("name", "deck"), bracket, out_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if id_map is None:
        # Pool role only — never match the user's [USER] copy of this id.
        id_map = _existing_moxfield_ids(out_dir, is_user=False)
    same_path = id_map.get(public_id) if public_id else None
    if same_path is not None:
        # Same Moxfield deck already harvested — wherever it lives in the
        # pool, base name or uniquified sibling. The lineage-aware map also
        # means: if only versioned ` v<N>` copies of this id survive on
        # disk, same_path is the lowest of them and this still SKIPS — the
        # deck IS on disk, and re-writing a fresh base next to frozen
        # snapshots is not this bulk path's call. Skip is the correct dedupe
        # for the bulk pool (unlike import_deck's user re-pull, nothing here
        # implies "give me the fresh version"). But the FILENAME must not go
        # stale: we just fetched the deck and know its current bracket, so
        # if the on-disk ` [B<n>]` tag drifted, rename now (and repoint the
        # shared id_map) — otherwise _bracket_from_filename serves the old
        # bracket to the curator forever, since every future harvest would
        # take this same skip path.
        same_path = _rename_for_bracket_drift(same_path, bracket, id_map=id_map)
        print(f"  SKIP {same_path.name} (same Moxfield deck already on disk)")
        return None
    verdict = _classify_destination(out_path, public_id)
    if verdict == "same":
        # Unreachable when id_map covers the dir (a base-path file recording
        # this id is in the map) — kept as a cheap guard against a stale
        # caller-supplied map, where skipping is the only safe answer.
        print(f"  SKIP {out_path.name} (same Moxfield deck already on disk)")
        return None
    if verdict == "unknown":
        # Existing deck without Moxfield= metadata. Skip rather than
        # re-write, since we can't tell if it's actually the same deck.
        # The skip is safe because the user already has a deck under that name
        # at that bracket — close enough for the curator's purposes.
        print(f"  SKIP {out_path.name} (already on disk)")
        return None
    if verdict == "collision":
        out_path = _uniquify(out_path)
    # Same Name=-from-final-stem stamping as import_deck — pool decks are
    # exactly the ones pool_curator matches by name, so the invariant
    # matters most here. (Stamp after _uniquify: the counter is part of
    # the stem Forge reports.)
    dck = stamp_name_preserving_display(dck, out_path.stem)
    out_path.write_text(dck, encoding="utf-8")
    if public_id:
        # Refresh the shared map in place so later candidates in the same
        # bulk run see this write — without it, a deck appearing twice in
        # one harvest (paging glitch) would import twice.
        id_map[public_id] = out_path
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
    # Whole-dir Moxfield-id → path map, built ONCE per bulk run and
    # refreshed after each write — scoped to THIS batch's role
    # (is_user). The same-id duplicate check consults this instead of
    # classifying only the base destination path, so a deck whose earlier
    # import lost a name collision (lives as `Foo (2) [B3].dck`) still
    # dedupes — the old base-path check saw the OTHER deck there and wrote
    # a fresh numbered copy on every re-paste. The role scope is the other
    # half of the same fix: with an UNSCOPED map, a user (is_user=True)
    # batch containing a deck that was ever HARVESTED into the opponent
    # pool matched the pool's `Foo [B3].dck`, reported "duplicate", and
    # skipped — so the user could never obtain a [USER] copy of that deck
    # (the sidebar lists only [USER] files; the pool file kept serving as
    # an opponent). Same-id matching stays within the role; the user copy
    # and the pool copy legitimately coexist.
    id_map = _existing_moxfield_ids(out_dir, is_user=is_user)

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
            public_id = deck_json.get("publicId", "")
            dest = deck_destination(
                deck_json.get("name", deck_id),
                bracket, out_dir, is_user=is_user,
            )
            # Same-id-anywhere-within-the-role first: this exact Moxfield
            # deck is already on disk in THIS role (base name OR uniquified
            # sibling) — re-pasting a URL is a no-op, per the module
            # contract. `existing_path` points at the ACTUAL file, not the
            # base-path guess. Even on this skip path the filename's
            # bracket tag must track the deck's CURRENT bracket (we just
            # fetched it) — otherwise repeated re-pastes pin the stale tag
            # forever; the rename also repoints id_map for later URLs in
            # this batch.
            same_path = id_map.get(public_id) if public_id else None
            if same_path is not None:
                same_path = _rename_for_bracket_drift(
                    same_path, bracket, id_map=id_map,
                )
                result.duplicates.append({
                    "url": url,
                    "deck_id": deck_id,
                    "existing_path": str(same_path),
                    "reason": "same Moxfield deck already on disk",
                })
                seen_in_batch.add(deck_id)
                continue
            verdict = _classify_destination(dest, public_id)
            if verdict in ("same", "unknown"):
                # "unknown": a pre-Moxfield=-metadata file owns the name;
                # can't verify identity, so keep the old conservative skip.
                # ("same" is unreachable now — a base-path file recording
                # this id sits in id_map — but skipping stays the only safe
                # answer if the map were ever stale.)
                result.duplicates.append({
                    "url": url,
                    "deck_id": deck_id,
                    "existing_path": str(dest),
                    "reason": (
                        "same Moxfield deck already on disk"
                        if verdict == "same" else "file already on disk"
                    ),
                })
                seen_in_batch.add(deck_id)
                continue
            if verdict == "collision":
                # A DIFFERENT deck's name sanitized to the same filename —
                # the old bare exists() check misreported these as
                # duplicates and silently skipped the import.
                dest = _uniquify(dest)

            # Stamp Name= from the FINAL destination stem (post-_uniquify)
            # — see import_deck. Without it, a bulk-imported deck with a
            # non-ASCII/':' name is invisible to every name-keyed pipeline.
            dck = stamp_name_preserving_display(to_dck(deck_json), dest.stem)
            dest.write_text(dck, encoding="utf-8")
            seen_in_batch.add(deck_id)
            if public_id:
                # Refresh so a later URL resolving to the same publicId
                # (different URL spelling of one deck) dedupes against
                # this write without a rescan.
                id_map[public_id] = dest
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
