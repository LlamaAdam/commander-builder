"""Commander Brackets "Game Changers" list — dynamic fetch with cache.

WotC updates the Game Changers list periodically (cards too strong for the
sub-cEDH brackets). The audit prompt's hardcoded fallback can drift; this
module fetches the canonical list and caches it locally.

Authoritative source: WotC's Commander Brackets page. Format isn't a JSON
API — it's an HTML list — so we parse the page and extract card names.

Cache: 7-day TTL since WotC updates are infrequent. On fetch failure, return
the bundled fallback so audits keep running.

Public API:

    from commander_builder.game_changers import load_game_changers

    cards = load_game_changers()  # set of card names
    "Smothering Tithe" in cards   # → True

The fallback list mirrors `prompts/moxfield_audit_v3.md` reference data so
the two stay in sync if WotC changes either side.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
# Cache filename is versioned so we don't read files written by the prior
# (over-permissive) parser. Bumping the suffix is the simplest "invalidate
# polluted caches everywhere" mechanism -- old game_changers.json files are
# just orphaned. Bump again whenever the schema or parser changes shape.
CACHE_PATH = REPO_ROOT / ".cache" / "game_changers.v2.json"
USER_AGENT = "commander-builder/0.2"
# WotC's Commander Brackets official page. May 404 / redirect over time;
# the fetch path is wrapped in a broad try/except so failures fall back to
# the hardcoded list rather than crashing the audit prompt.
WOTC_URL = "https://magic.wizards.com/en/news/announcements/introducing-commander-brackets-beta"
CACHE_TTL_DAYS = 7

# Fallback list — keep in sync with prompts/moxfield_audit_v3.md "Hardcoded
# fallback" section. Update when the prompt updates (or when this module's
# dynamic fetch surfaces additions).
_FALLBACK = frozenset({
    # White
    "Drannith Magistrate", "Enlightened Tutor", "Farewell", "Humility",
    "Serra's Sanctum", "Smothering Tithe", "Teferi's Protection",
    # Blue
    "Consecrated Sphinx", "Cyclonic Rift", "Force of Will",
    "Fierce Guardianship", "Gifts Ungiven", "Intuition", "Mystical Tutor",
    "Narset, Parter of Veils", "Rhystic Study", "Thassa's Oracle",
    # Black
    "Ad Nauseam", "Bolas's Citadel", "Braids, Cabal Minion",
    "Demonic Tutor", "Imperial Seal", "Necropotence", "Opposition Agent",
    "Orcish Bowmasters", "Tergrid, God of Fright", "Vampiric Tutor",
    # Red
    "Gamble", "Jeska's Will", "Underworld Breach",
    # Green
    "Biorhythm", "Crop Rotation", "Gaea's Cradle", "Natural Order",
    "Seedborn Muse", "Survival of the Fittest", "Worldly Tutor",
    # Multicolor
    "Aura Shards", "Coalition Victory", "Grand Arbiter Augustin IV",
    "Notion Thief",
    # Colorless
    "Ancient Tomb", "Chrome Mox", "Field of the Dead", "Glacial Chasm",
    "Grim Monolith", "Lion's Eye Diamond", "Mana Vault", "Mishra's Workshop",
    "Mox Diamond", "Panoptic Mirror", "The One Ring",
    "The Tabernacle at Pendrell Vale",
})


def _cache_is_fresh(path: Path, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc,
    )
    return age < timedelta(days=ttl_days)


def _http_get_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


_CHROME_BLOCK_RE = re.compile(
    r"<(nav|header|footer|aside)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _looks_like_card_name(text: str) -> bool:
    """Heuristic guard against site-chrome / sentence fragments.

    Real Magic card names: 1-7 words, Title Case, may include
    ``,`` ``'`` ``-`` ``/``. Reject sentence-punctuation chars and ``&``
    (no real Magic card has ``&``; this also catches "Banned & Restricted
    List" once entities are decoded — previously slipped through as
    ``Banned &amp; Restricted List``).
    """
    if not 2 <= len(text) <= 50:
        return False
    if not text[0].isupper():
        return False
    if any(c in text for c in (":", "|", "(", "—", "•", "&", ";", "?", "!")):
        return False
    if len(text.split()) > 7:
        return False
    return True


def _parse_card_names_from_html(html: str) -> set[str]:
    """Best-effort extraction of card names from the WotC announcement page.

    Two defenses against polluting the result with site-chrome links (the
    prior scraper let "About", "Privacy Policy", "Wizards Play Network",
    "Banned &amp; Restricted List", etc. through):

    1. Strip ``<nav>`` / ``<header>`` / ``<footer>`` / ``<aside>`` blocks
       before scanning ``<li>`` items — that is where the WotC page packs
       its site-wide nav, and every observed chrome ``<li>`` lived in one
       of them.
    2. Decode HTML entities first (``&amp;`` -> ``&``) so the ``&``
       reject-char in :func:`_looks_like_card_name` actually fires.

    Inevitably still noisy; the caller should union with the bundled
    ``_FALLBACK`` rather than treat this as authoritative.
    """
    import html as _html_mod
    decoded = _html_mod.unescape(html)
    body = _CHROME_BLOCK_RE.sub("", decoded)
    li_re = re.compile(r"<li[^>]*>(.+?)</li>", re.DOTALL | re.IGNORECASE)
    tag_re = re.compile(r"<[^>]+>")
    candidates: set[str] = set()
    for m in li_re.finditer(body):
        text = tag_re.sub("", m.group(1)).strip()
        if _looks_like_card_name(text):
            candidates.add(text)
    return candidates


def fetch_game_changers(use_cache: bool = True) -> set[str]:
    """Fetch the Game Changers list from WotC. Returns the parsed names
    union'd with the bundled fallback (so a parser regression can't shrink
    the list). Caches to `.cache/game_changers.json`.

    The cache is persisted ONLY when the scrape actually produced names. A
    failed/empty scrape degrades to the fallback WITHOUT writing the cache,
    so the fallback-only result doesn't masquerade as "fresh" for the whole
    TTL and block a retry on the next call."""
    if use_cache and _cache_is_fresh(CACHE_PATH):
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            # Re-apply the card-name filter to cached entries so caches written
            # before the stricter parser self-heal on next read (the prior
            # parser persisted site-chrome strings like "Privacy Policy").
            cached = {c for c in data.get("cards", []) if _looks_like_card_name(c)}
            return cached | set(_FALLBACK)
        except (OSError, ValueError):
            pass  # Re-fetch on cache corruption.

    try:
        html = _http_get_text(WOTC_URL)
        scraped = _parse_card_names_from_html(html)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        scraped = set()

    merged = set(scraped) | set(_FALLBACK)

    # Only persist when the scrape produced names. On a failed/empty scrape
    # we return the fallback but do NOT write the cache — otherwise the
    # fallback-only list would be cached "fresh" for the full TTL and never
    # retried.
    if use_cache and scraped:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source_url": WOTC_URL,
                "cards": sorted(merged),
                "scraped_count": len(scraped),
                "fallback_count": len(_FALLBACK),
            }, indent=2),
            encoding="utf-8",
        )
    return merged


def load_game_changers(force_refresh: bool = False) -> set[str]:
    """Load the cached Game Changers list. Triggers a fetch if cache is stale
    or missing. Returns the fallback set on any error so audits don't break."""
    try:
        return fetch_game_changers(use_cache=not force_refresh)
    except Exception:  # noqa: BLE001
        return set(_FALLBACK)


def is_game_changer(card_name: str) -> bool:
    """Convenience wrapper — `True` if `card_name` is on the GC list."""
    return card_name in load_game_changers()


if __name__ == "__main__":
    import sys
    cards = load_game_changers(force_refresh="--refresh" in sys.argv)
    print(json.dumps({
        "total": len(cards),
        "fallback_count": len(_FALLBACK),
        "first_10": sorted(cards)[:10],
    }, indent=2))
