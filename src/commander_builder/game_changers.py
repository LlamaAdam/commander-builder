"""Commander Brackets "Game Changers" list — dynamic fetch with cache.

WotC updates the Game Changers list periodically (cards too strong for the
sub-cEDH brackets). The audit prompt's hardcoded fallback can drift; this
module fetches the canonical list and caches it locally.

Authoritative source: WotC's Commander Brackets page. Format isn't a JSON
API — it's an HTML list — so we parse the page and extract card names.

Cache: 7-day TTL since WotC updates are infrequent. On fetch failure, return
the bundled fallback so audits keep running.

MERGE POLICY: a scrape that passes the sanity check REPLACES the bundled
fallback rather than being union'd with it — WotC removes cards from the
list, and a union can only ever grow. See :func:`fetch_game_changers`.

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
import sys
import time
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
# WotC's MAINTAINED Commander format page — the living Game Changers list.
#
# WHY NOT THE ANNOUNCEMENT PAGE: this used to point at
# ``/en/news/announcements/introducing-commander-brackets-beta``, the
# original beta announcement. That is a frozen news post: it still carries
# the launch-era ~40-card list and will never be edited again, so every
# subsequent WotC revision (additions AND removals) was invisible to us.
# The format page is the one WotC actually updates. May 404 / redirect over
# time; the fetch path is wrapped in a broad try/except so failures fall
# back to the hardcoded list rather than crashing the audit prompt.
WOTC_URL = "https://magic.wizards.com/en/formats/commander"
CACHE_TTL_DAYS = 7

# --- Scrape trust thresholds ------------------------------------------------
# A scrape REPLACES the bundled fallback (see fetch_game_changers), which is
# the only way a card WotC removed can ever leave our list. Replacement is
# also the only way a parser regression can silently gut the list, so the
# scrape has to earn it by looking like the real thing:
#
#   * ``_MIN_SCRAPED_NAMES`` — the official list has been in the 40-55 card
#     range since launch (the bundled fallback is 53). A parse that yields
#     fewer than 40 plausible names found a redesigned page, a cookie wall,
#     or an error page — not the list.
#   * ``_MIN_FALLBACK_OVERLAP`` — the list changes by a handful of cards per
#     revision, never wholesale. A trustworthy scrape must still contain at
#     least 80% of the bundled fallback; below that we are looking at a
#     different page, not a WotC update.
#
# Both are deliberately loose enough to let a real revision through (WotC
# could drop ~10 of 53 cards and still clear 80%) and tight enough that
# garbage never replaces a known-good list. When the checks fail we use the
# fallback WHOLESALE and do not cache — conservative on missing data.
_MIN_SCRAPED_NAMES = 40
_MIN_FALLBACK_OVERLAP = 0.80

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

    Inevitably still noisy. The caller does NOT union the result with the
    bundled ``_FALLBACK`` (that made removals impossible); instead it
    sanity-checks the parse via :func:`_scrape_is_trustworthy` and either
    takes it wholesale or discards it wholesale.
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


def _scrape_is_trustworthy(names: set[str]) -> tuple[bool, float]:
    """``(trusted, fallback_overlap)`` for a candidate Game Changers list.

    Gatekeeper for the replace-not-union merge. Returns the overlap ratio
    alongside the verdict so callers can log HOW far off a rejected parse
    was — "0 of 53 bundled names present" and "44 of 53" are very different
    failures (dead page vs. a real WotC revision we should hand-review).

    Overlap is measured as *the share of the bundled fallback the candidate
    still contains*, not Jaccard: additions are expected and must not count
    against a scrape, only unexplained disappearances should. Case-folded
    for the same reason every other name comparison in this repo is.
    """
    fallback_folded = {c.casefold() for c in _FALLBACK}
    if not fallback_folded:  # defensive; _FALLBACK is never empty
        return len(names) >= _MIN_SCRAPED_NAMES, 1.0
    folded = {c.casefold() for c in names}
    # Computed even when the count bar already failed, so the rejection log
    # can tell "dead page, nothing recognizable" apart from "the real list,
    # truncated" — different bugs, different fixes.
    overlap = len(fallback_folded & folded) / len(fallback_folded)
    trusted = (
        len(names) >= _MIN_SCRAPED_NAMES
        and overlap >= _MIN_FALLBACK_OVERLAP
    )
    return trusted, overlap


def _log_divergence(names: set[str]) -> None:
    """Print what a trusted scrape changed relative to the bundled list.

    THE STALENESS ALARM. ``_FALLBACK`` is hand-synced with
    ``prompts/moxfield_audit_v3.md``; once a scrape is trusted enough to
    replace it, any disagreement means the bundled list (and the prompt) are
    out of date. Loud on purpose — this is the only signal a maintainer gets
    that WotC moved. Print-only: divergence is news, never an error.
    """
    folded_fallback = {c.casefold(): c for c in _FALLBACK}
    folded_scraped = {c.casefold(): c for c in names}
    added = sorted(folded_scraped[k] for k in folded_scraped.keys() - folded_fallback.keys())
    removed = sorted(folded_fallback[k] for k in folded_fallback.keys() - folded_scraped.keys())
    if not added and not removed:
        return
    print(
        f"[game_changers] scraped list diverges from bundled _FALLBACK "
        f"({len(names)} scraped vs {len(_FALLBACK)} bundled) — "
        f"update _FALLBACK + prompts/moxfield_audit_v3.md. "
        f"ADDED: {', '.join(added) or 'none'}. "
        f"REMOVED: {', '.join(removed) or 'none'}.",
        file=sys.stderr, flush=True,
    )


def fetch_game_changers(use_cache: bool = True) -> set[str]:
    """Fetch the Game Changers list from WotC. Caches to
    ``.cache/game_changers.v2.json``.

    MERGE POLICY — a trusted scrape REPLACES the bundled fallback; it is not
    union'd with it. The union was a one-way ratchet: WotC has removed cards
    from the Game Changers list before, and a union can only ever grow, so a
    removed card stayed on our list forever and kept flooring innocent decks
    to B3. Replacement is the only merge that can shrink.

    Replacement is gated on :func:`_scrape_is_trustworthy` precisely because
    it is destructive — a parser regression that returns junk must not be
    able to gut the list. A rejected scrape degrades to the bundled fallback
    WHOLESALE (conservative on missing data), and divergence between a
    trusted scrape and the bundled list is logged loudly as the staleness
    alarm.

    The cache is persisted ONLY when the scrape was trusted. A failed /
    empty / rejected scrape degrades to the fallback WITHOUT writing the
    cache, so a fallback-only result never masquerades as "fresh" for the
    whole TTL and never blocks a retry on the next call — and a bad parse
    never poisons the cache for a week.
    """
    if use_cache and _cache_is_fresh(CACHE_PATH):
        try:
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            # Re-apply the card-name filter to cached entries so caches written
            # before the stricter parser self-heal on next read (the prior
            # parser persisted site-chrome strings like "Privacy Policy").
            cached = {c for c in data.get("cards", []) if _looks_like_card_name(c)}
            # A cache entry is just a persisted scrape, so it faces the same
            # trust bar — and for the same reason. We return it INSTEAD of the
            # fallback (that's what makes removals stick across process
            # restarts), so a cache that was hand-edited, truncated, or
            # written by an older/looser parser must not be honored. An
            # untrusted cache falls through to a live re-fetch rather than
            # being served for the rest of its TTL.
            trusted, _overlap = _scrape_is_trustworthy(cached)
            if trusted:
                return cached
        except (OSError, ValueError):
            pass  # Re-fetch on cache corruption.

    try:
        html = _http_get_text(WOTC_URL)
        scraped = _parse_card_names_from_html(html)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        scraped = set()

    trusted, overlap = _scrape_is_trustworthy(scraped)
    if not trusted:
        if scraped:
            # Distinguish "we got nothing" (network down — silent, normal)
            # from "we got something that doesn't look like the list", which
            # means the page moved or the parser broke and needs a human.
            print(
                f"[game_changers] rejecting scrape of {WOTC_URL}: "
                f"{len(scraped)} plausible name(s), "
                f"{overlap:.0%} of the bundled list present "
                f"(need >= {_MIN_SCRAPED_NAMES} names and "
                f">= {_MIN_FALLBACK_OVERLAP:.0%} overlap) — "
                f"using bundled fallback, not caching.",
                file=sys.stderr, flush=True,
            )
        return set(_FALLBACK)

    _log_divergence(scraped)
    merged = set(scraped)

    if use_cache:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source_url": WOTC_URL,
                "cards": sorted(merged),
                "scraped_count": len(scraped),
                "fallback_count": len(_FALLBACK),
                "fallback_overlap": round(overlap, 4),
            }, indent=2),
            encoding="utf-8",
        )
    return merged


# Process-level memo for load_game_changers. The list is effectively a
# process constant, yet before this memo existed a single deck build paid
# ~6 calls, and on the (currently broken) WotC-scrape path EVERY call was a
# live HTTPS round-trip: the rejected-scrape branch deliberately never
# writes the disk cache, so nothing short-circuited the retry. The TTL
# keeps the documented "a failed scrape never blocks a retry" property at
# process scope — a long-lived web app re-attempts every 15 minutes
# instead of on every request, and short-lived CLIs fetch exactly once.
_MEMO: Optional[tuple[float, frozenset[str]]] = None
_MEMO_TTL_SEC = 900.0

# Folded-set cache for is_game_changer, keyed on the exact set the last
# call to load_game_changers() returned so tests that monkeypatch the
# loader still see their patched list take effect immediately.
_FOLDED: Optional[tuple[frozenset[str], frozenset[str]]] = None


def clear_memo() -> None:
    """Reset the in-process memos (test isolation seam)."""
    global _MEMO, _FOLDED
    _MEMO = None
    _FOLDED = None


def load_game_changers(force_refresh: bool = False) -> set[str]:
    """Load the cached Game Changers list. Triggers a fetch if cache is stale
    or missing. Returns the fallback set on any error so audits don't break.

    Memoized per process (15 min TTL); ``force_refresh=True`` bypasses and
    repopulates the memo."""
    global _MEMO
    if not force_refresh and _MEMO is not None:
        stamp, cards = _MEMO
        if time.monotonic() - stamp < _MEMO_TTL_SEC:
            return set(cards)
    try:
        result = fetch_game_changers(use_cache=not force_refresh)
    except Exception:  # noqa: BLE001
        result = set(_FALLBACK)
    _MEMO = (time.monotonic(), frozenset(result))
    return set(result)


def is_game_changer(card_name: str) -> bool:
    """Convenience wrapper — `True` if `card_name` is on the GC list.

    Case-insensitive: every other consumer of card-name sets in this
    codebase folds case before membership tests (deck files, EDHREC
    slugs, and user input all disagree on capitalization), and an
    exact-case check here silently missed e.g. "smothering tithe".
    casefold (not lower) for parity with how Python recommends caseless
    matching. The folded set is cached against the loader's exact return
    value so per-card loops don't rebuild it, while a monkeypatched or
    refreshed loader still takes effect immediately."""
    global _FOLDED
    cards = frozenset(load_game_changers())
    if _FOLDED is None or _FOLDED[0] != cards:
        _FOLDED = (cards, frozenset(c.casefold() for c in cards))
    return card_name.casefold() in _FOLDED[1]


if __name__ == "__main__":
    import sys
    cards = load_game_changers(force_refresh="--refresh" in sys.argv)
    print(json.dumps({
        "total": len(cards),
        "fallback_count": len(_FALLBACK),
        "first_10": sorted(cards)[:10],
    }, indent=2))
