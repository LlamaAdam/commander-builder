"""Thin Scryfall API client for card metadata lookups.

Single responsibility: look up cards by name and return their relevant
Commander-format metadata — primarily color identity, primary type, and CMC.
Cached on disk so repeat lookups don't re-hit Scryfall.

Public API:

    from commander_builder.scryfall_client import lookup_card, color_identity_for_commander

    card = lookup_card("Atraxa, Praetors' Voice")
    # → {"name": "...", "color_identity": "BGUW", "type_line": "...", ...}

    ci = color_identity_for_commander("[USER] Atraxa Stuff [B4].dck")
    # → "BGUW"  (sorted, WUBRG-ordered)

Scryfall asks for ≥75ms between requests and a User-Agent. We sleep 100ms.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from . import dck_utils

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_cards_dir() -> Path:
    """Resolve the shared mtg_cards directory.

    Order of precedence:
    1. ``MTG_CARDS_DIR`` environment variable (operator override / CI).
    2. ``C:\\dev\\mtg_cards`` if it exists (the canonical local path).
    3. Project-local ``.cache/`` fallback (legacy; keeps tests / fresh
       checkouts working on machines without the shared folder).
    """
    env = os.environ.get("MTG_CARDS_DIR")
    if env:
        return Path(env)
    canonical = Path("C:/dev/mtg_cards")
    if canonical.exists():
        return canonical
    return REPO_ROOT / ".cache"


_CARDS_DIR = _resolve_cards_dir()
CACHE_DIR = (
    _CARDS_DIR / "oracle_snapshots"
    if _CARDS_DIR.name == "mtg_cards"
    else _CARDS_DIR / "scryfall"
)
# All-printings snapshots live in a SIBLING dir, not inside CACHE_DIR:
# the oracle snapshots are one-file-per-name with a stable "this is THE
# card" contract that forge_py also reads — mixing multi-printing lists
# into the same dir would break that shared contract. Naming mirrors
# the CACHE_DIR convention above (mtg_cards gets the descriptive name,
# the legacy .cache/ fallback gets a scryfall_-prefixed one).
PRINTS_CACHE_DIR = (
    _CARDS_DIR / "prints_snapshots"
    if _CARDS_DIR.name == "mtg_cards"
    else _CARDS_DIR / "scryfall_prints"
)
SCRYFALL_BASE = "https://api.scryfall.com"
USER_AGENT = "commander-builder/0.1 (+https://github.com/LlamaAdam/commander-builder)"
REQUEST_SLEEP_SEC = 0.1   # Scryfall's published rate-limit floor is 50-100ms.

# WUBRG canonical ordering — used to render color identity strings consistently.
_WUBRG = "WUBRG"


def _slug(name: str) -> str:
    """Slugify a card name to a cache filename stem. Uses lowercase + safe
    chars only so cross-platform (Windows is the realistic target) doesn't
    break."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{_slug(name)}.json"


def _prints_cache_path(name: str) -> Path:
    return PRINTS_CACHE_DIR / f"{_slug(name)}.json"


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def refresh_card(name: str) -> Optional[dict]:
    """Force-fetch ``name`` from Scryfall, bypassing the local cache.

    Use when you need guaranteed-current oracle text — for example, before
    classifying a card whose effect depends on errata-prone wording.
    Mirrors ``forge_py.cards.refresh``; the two projects independently
    write to the same shared snapshot dir."""
    if not name:
        return None
    url = f"{SCRYFALL_BASE}/cards/named?{urllib.parse.urlencode({'exact': name})}"
    try:
        time.sleep(REQUEST_SLEEP_SEC)
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    cache_path = _cache_path(name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data), encoding="utf-8")
    return data


def format_card_for_display(name: str) -> str:
    """Render a card from the cache as a plain-text reference block.

    Same shape as ``forge_py.cards.format_card_for_display`` so the
    two projects emit interchangeable card-reference output. Returns
    the empty string if the card can't be found locally.

    The format is intentionally plain-text — readable in CLI output,
    web panels, and LLM prompts identically. See
    [FUTURE_PLANS.md FP-009] for context: oracle text is
    authoritative, images are decorative.
    """
    card = lookup_card(name) if name else None
    if not card:
        return ""

    head_left = card.get("name") or name
    cost = card.get("mana_cost") or ""
    head = f"{head_left}   {cost}" if cost else head_left

    type_line = card.get("type_line") or ""
    pt = ""
    power = card.get("power")
    toughness = card.get("toughness")
    if power is not None and toughness is not None:
        pt = f"   {power}/{toughness}"

    oracle = (card.get("oracle_text") or "").rstrip()
    color_id = "".join(card.get("color_identity") or []) or "—"
    cmc = card.get("cmc")
    cmc_s = f"{cmc:g}" if isinstance(cmc, (int, float)) else "?"

    parts = [head, f"{type_line}{pt}", "----"]
    if oracle:
        parts.append(oracle)
    parts.append("")
    parts.append(f"Color identity: {color_id}   CMC: {cmc_s}")
    return "\n".join(parts)


def diff_oracle_text(name: str, candidate: str) -> Optional[dict]:
    """Compare cached oracle text for ``name`` against ``candidate``.

    Returns ``None`` if the card can't be found in the cache.
    Otherwise ``{"changed": bool, "before": str, "after": str}``.
    Use the freshly-fetched text as ``candidate`` to detect errata.
    """
    cached = lookup_card(name)
    if cached is None:
        return None
    before = (cached.get("oracle_text") or "").strip()
    after = (candidate or "").strip()
    return {"changed": before != after, "before": before, "after": after}


def lookup_card(name: str, cache: bool = True) -> Optional[dict]:
    """Look up `name` via Scryfall's exact-named endpoint. Caches successful
    responses to ``oracle_snapshots/<slug>.json``. Returns None on 404."""
    if not name:
        return None
    cache_path = _cache_path(name)
    if cache and cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # Re-fetch on corruption.
    url = f"{SCRYFALL_BASE}/cards/named?{urllib.parse.urlencode({'exact': name})}"
    try:
        time.sleep(REQUEST_SLEEP_SEC)
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
    return data


# --- All-printings lookup (cheaper-printing savings feature) -----------

# Page cap for the paginated /cards/search?unique=prints walk. Scryfall
# pages hold 175 cards; even Counterspell/Lightning Bolt-class reprint
# champions fit in 2-3 pages, so 4 pages (~700 printings) is a safety
# valve against pathological pagination loops, not a realistic limit.
_PRINTS_PAGE_CAP = 4


def _trim_printing(card: dict) -> dict:
    """Project one Scryfall card object down to the fields the
    cheaper-printing feature needs.

    Full card objects are ~4-8KB each and a heavily reprinted card has
    100+ printings — caching them verbatim would balloon the snapshot
    dir by orders of magnitude for fields (oracle text, image URIs,
    rulings links) that are identical across printings or unused here.
    The trimmed shape keeps the cache small AND acts as documentation
    of what downstream pricing code may rely on."""
    prices = card.get("prices") or {}
    legalities = card.get("legalities") or {}
    return {
        "set": card.get("set"),
        "set_name": card.get("set_name"),
        # set_type distinguishes "memorabilia" (gold-border World
        # Championship decks, oversized promos) which are not legal
        # game pieces — the pricing layer excludes them.
        "set_type": card.get("set_type"),
        "collector_number": card.get("collector_number"),
        "border_color": card.get("border_color"),
        "oversized": bool(card.get("oversized")),
        # digital=True printings (MTGO/Arena) can't be sleeved in a
        # paper Commander deck; kept so cached data filters correctly
        # even though the fetch query already excludes most of them.
        "digital": bool(card.get("digital")),
        "prices": {
            k: prices.get(k) for k in ("usd", "usd_foil", "usd_etched")
        },
        # Only the commander legality matters to this project; storing
        # the full 20+-format map would be dead weight in every file.
        "legalities": {"commander": legalities.get("commander")},
    }


def lookup_card_prints(
    name: str, cache: bool = True, cache_only: bool = False,
) -> Optional[list[dict]]:
    """Fetch ALL printings of ``name`` (trimmed to pricing-relevant
    fields — see ``_trim_printing``), cached one file per card under
    ``PRINTS_CACHE_DIR``.

    The oracle snapshots (``lookup_card``) carry exactly ONE printing
    per name (Scryfall's /cards/named default), so printing-price
    comparisons need this separate lazily-populated cache.

    - ``cache_only=True`` never touches the network — returns the
      cached list or None. Callers use this as an offline circuit
      breaker: after one network failure, drain the remaining cards
      from cache instead of eating a connect-timeout per card.
    - Returns None on 404 (unknown card) or a cache-only miss.
    - Network errors (URLError, timeouts, 5xx) PROPAGATE — unlike the
      404 case they mean "unknown right now", not "doesn't exist", and
      the caller needs to tell the two apart to degrade gracefully.
    """
    if not name:
        return None
    cache_path = _prints_cache_path(name)
    if cache and cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            printings = data.get("printings")
            if isinstance(printings, list):
                return printings
        except (OSError, ValueError):
            pass  # Re-fetch on corruption (mirrors lookup_card).
    if cache_only:
        return None
    # Exact-name search across all printings. ``game:paper`` drops
    # digital-only printings server-side (they can't be bought for a
    # paper deck and their usd prices are null anyway), shrinking the
    # page walk for heavily reprinted cards.
    query = urllib.parse.urlencode({
        "q": f'!"{name}" game:paper',
        "unique": "prints",
        "order": "released",
    })
    url: Optional[str] = f"{SCRYFALL_BASE}/cards/search?{query}"
    printings: list[dict] = []
    pages = 0
    try:
        while url and pages < _PRINTS_PAGE_CAP:
            time.sleep(REQUEST_SLEEP_SEC)  # Scryfall politeness floor.
            data = _http_get_json(url)
            printings.extend(
                _trim_printing(c)
                for c in data.get("data", [])
                if isinstance(c, dict)
            )
            url = data.get("next_page") if data.get("has_more") else None
            pages += 1
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # Card name doesn't exist — a real answer.
        raise  # 5xx/429 → caller's offline/backoff handling.
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Store under a "printings" key (not a bare list) so the file
        # format can grow metadata (fetched_at, schema version) without
        # breaking old readers.
        cache_path.write_text(
            json.dumps({"name": name, "printings": printings}),
            encoding="utf-8",
        )
    return printings


def normalize_color_identity(colors: list[str]) -> str:
    """Render Scryfall's color identity list as a WUBRG-ordered string."""
    if not colors:
        return ""  # Colorless commanders are valid and produce ''.
    seen = set(c.upper() for c in colors if isinstance(c, str))
    return "".join(c for c in _WUBRG if c in seen)


def _parse_commander_names_from_dck(dck_path: Path) -> list[str]:
    """Extract commander names from a Forge .dck file's [Commander] section.

    Forge supports partner / Background / Signature Spell, so multiple names
    are valid. Strips set/CN suffixes (`Atraxa|CMM|1` → `Atraxa`).

    Thin wrapper over ``dck_utils.section_card_names``."""
    if not dck_path.exists():
        return []
    text = dck_path.read_text(encoding="utf-8")
    return dck_utils.section_card_names(text, "Commander")


def color_identity_for_commander(dck_path: Path) -> str:
    """Resolve the union color identity of all commanders in a .dck file.

    For partner pairs, both color identities merge (WUBRG-ordered). Returns
    `""` for colorless commanders or when no commanders are listed."""
    names = _parse_commander_names_from_dck(dck_path)
    if not names:
        return ""
    union: set[str] = set()
    for name in names:
        card = lookup_card(name)
        if card is None:
            continue
        ci = card.get("color_identity") or []
        union.update(c.upper() for c in ci if isinstance(c, str))
    return "".join(c for c in _WUBRG if c in union)


if __name__ == "__main__":
    # Smoke entry point: `python -m commander_builder.scryfall_client <card>`
    import sys
    if len(sys.argv) < 2:
        print("Usage: scryfall_client.py <card-name>")
        sys.exit(2)
    card = lookup_card(" ".join(sys.argv[1:]))
    if card is None:
        print("Not found.")
        sys.exit(1)
    print(json.dumps({
        "name": card.get("name"),
        "color_identity": normalize_color_identity(card.get("color_identity") or []),
        "type_line": card.get("type_line"),
        "cmc": card.get("cmc"),
    }, indent=2))
