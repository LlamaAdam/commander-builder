"""EDHREC scraper for commander metadata + meta-opponent discovery.

EDHREC publishes per-commander pages with aggregated stats: card inclusion
percentages, synergy scores, popular variants, and "Average Deck" links to
Moxfield. The audit prompt scrapes this manually via a logged-in browser
session; this module is the programmatic equivalent for automated curation.

Strategy: EDHREC's commander page (`/commanders/<slug>`) renders a
hydrating React app, but the static HTML is enough for our purposes — the
key data ships in a `<script id="__NEXT_DATA__">` JSON blob that next.js
bakes into every page. We grab the page HTML, extract that blob, and parse.

The bracket-deck-list pages (`/decks/<slug>/<bracket>`) sometimes block
non-browser fetches with a "Cookie/query string data" guard. The commander
page is reliably fetchable.

Public API:

    fetch_commander_page("Atraxa, Praetors' Voice") → CommanderPage
    fetch_commander_page("atraxa-praetors-voice")    → CommanderPage  (slug also OK)

    page.top_cards           # most-included cards (>50% inclusion)
    page.high_synergy_cards  # cards uniquely correlated with this commander
    page.average_deck_url    # Moxfield URL for the "Average Deck" sample
    page.related_commanders  # similar-archetype commanders for opponent discovery

Disk cache mirrors `scryfall_client` — 24-hour TTL since EDHREC stats refresh
weekly.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / ".cache" / "edhrec"
EDHREC_BASE = "https://edhrec.com"
# Direct JSON API (no HTML scrape). Used by fetch_top_cards for the
# time-windowed / by-type "top cards" pages.
EDHREC_JSON_BASE = "https://json.edhrec.com/pages"
USER_AGENT = "commander-builder/0.2 (+https://github.com/LlamaAdam/commander-builder)"
REQUEST_SLEEP_SEC = 0.5  # EDHREC isn't rate-limited like Scryfall but be polite.
CACHE_TTL_HOURS = 24

# next.js embeds page data here. Match across newlines because the JSON can
# span thousands of lines.
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"\s+type="application/json"[^>]*>(.+?)</script>',
    re.DOTALL,
)


@dataclass
class CardEntry:
    """Single row from a card-list section of an EDHREC page."""
    name: str
    inclusion_pct: float = 0.0
    synergy_pct: float = 0.0
    num_decks: int = 0


@dataclass
class CommanderPage:
    """Parsed view of an EDHREC commander page. Lists are best-effort —
    EDHREC's HTML schema shifts, so missing fields default to empty rather
    than raising."""
    commander_name: str
    slug: str
    fetched_at: str
    top_cards: list[CardEntry] = field(default_factory=list)
    high_synergy_cards: list[CardEntry] = field(default_factory=list)
    new_cards: list[CardEntry] = field(default_factory=list)
    # Per-category card lists keyed by EDHREC's section header
    # (``"Creatures"``, ``"Instants"``, ``"Sorceries"``, ``"Lands"``,
    # ``"Mana Artifacts"``, ``"Game Changers"``, etc.). The
    # 2026-05-14 live-audit investigation revealed EDHREC ships
    # 200+ cards per commander split across ~14 sections, but the
    # original parser only kept the 25 cards in top_cards +
    # high_synergy + new_cards. Capturing all sections gives the
    # heuristic 5-10× more signal for both adds and cuts.
    category_lists: dict[str, list[CardEntry]] = field(default_factory=dict)
    related_commanders: list[str] = field(default_factory=list)
    average_deck_url: Optional[str] = None
    deck_count: Optional[int] = None
    raw_size_bytes: int = 0

    def all_known_cards(self) -> set[str]:
        """Return the lowercase union of every card across every section.

        Used by the heuristic cut path to answer "is this deck card
        in EDHREC's data for this commander?" — before the
        2026-05-14 parser expansion the answer relied only on
        top_cards + high_synergy (25 cards), so ~80% of any 99-card
        deck looked off-archetype. With all 14 sections captured
        the typical set is 200+ cards, comparable to the real
        EDHREC page.
        """
        out: set[str] = set()
        for c in self.top_cards:
            out.add(c.name.lower())
        for c in self.high_synergy_cards:
            out.add(c.name.lower())
        for c in self.new_cards:
            out.add(c.name.lower())
        for cards in self.category_lists.values():
            for c in cards:
                out.add(c.name.lower())
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def commander_slug(commander_name: str) -> str:
    """EDHREC's URL slugs are lowercase + hyphenated, with apostrophes / commas
    stripped. `Atraxa, Praetors' Voice` → `atraxa-praetors-voice`.

    Double-faced commanders (DFCs) like
    ``Sephiroth, Fabled SOLDIER // Sephiroth, One-Winged Angel`` use
    only the front face on EDHREC. Split on ``//`` and slugify the
    front half — this matches EDHREC's URL convention exactly.
    """
    name = commander_name.split("//")[0].strip()
    s = name.lower()
    s = re.sub(r"[',.]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unknown"


def _cache_path(slug: str) -> Path:
    return CACHE_DIR / f"{slug}.json"


def _is_cache_fresh(path: Path, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    if not path.exists():
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    )
    return age < timedelta(hours=ttl_hours)


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# HTTP status codes that indicate a transient server-side problem and are
# worth retrying. 429 is rate-limiting (back off harder). 4xx other than
# 404 / 429 means the request itself is wrong — don't retry.
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

# Upper bound on Retry-After honor. EDHREC sits behind a CDN that
# occasionally sends Retry-After: 300+ during incidents — long enough
# that the user would rather see a degraded result than block. Cap so
# a misbehaving server can't pin the audit forever.
MAX_RETRY_AFTER_SEC = 30.0


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header per RFC 7231 §7.1.3.

    Returns the indicated delay in seconds, or None when the header is
    missing or malformed. Supports both forms the spec allows:
    ``delta-seconds`` (e.g., ``"60"``) and ``HTTP-date`` (e.g.,
    ``"Wed, 21 Oct 2026 07:28:00 GMT"``). Negative deltas clamp to 0.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    # delta-seconds form first — most common from CDNs.
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime
        target = parsedate_to_datetime(s)
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(target.tzinfo)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _http_get_text_with_retry(
    url: str,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> str:
    """GET ``url`` with exponential backoff on transient failures.

    Retries on 5xx HTTPError, 429 (rate-limited), and URLError (network /
    DNS / timeout). 404 is deterministic — caller decides whether the
    miss is fatal — and propagates without retrying. Other 4xx (400,
    401, 403) are caller-bug class errors and also don't retry.

    Backoff: when the server sends a ``Retry-After`` header (RFC 7231),
    honor it (clamped to ``MAX_RETRY_AFTER_SEC`` so a 300+s instruction
    can't pin the audit). Otherwise fall back to ``base_delay * 2 **
    attempt``, so ``max_retries=3`` with ``base_delay=1.0`` yields
    sleeps of 1s, 2s, 4s between 4 total attempts.

    Each retry emits a single line to stdout so the operator sees
    "EDHREC was 503, retried" instead of silent slowdowns. The happy
    path stays quiet. Raises the final exception when retries are
    exhausted; callers downstream of ``fetch_*`` functions translate
    that into a graceful ``None`` return.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return _http_get_text(url)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc
        except TimeoutError as exc:
            last_exc = exc
        if attempt >= max_retries:
            break
        # Prefer the server's own backoff hint over our exp curve.
        delay: Optional[float] = None
        if isinstance(last_exc, urllib.error.HTTPError):
            hdrs = getattr(last_exc, "headers", None)
            raw = hdrs.get("Retry-After") if hdrs is not None else None
            hint = _parse_retry_after(raw)
            if hint is not None:
                delay = min(hint, MAX_RETRY_AFTER_SEC)
        if delay is None:
            delay = base_delay * (2 ** attempt)
        # Single-line log so flaky EDHREC traffic is diagnosable from
        # the server log. Match the rest of the codebase's print style.
        reason = (
            f"HTTP {last_exc.code}"
            if isinstance(last_exc, urllib.error.HTTPError)
            else type(last_exc).__name__
        )
        print(
            f"[edhrec] retry {attempt + 1}/{max_retries} "
            f"after {reason} — sleeping {delay:.1f}s",
            flush=True,
        )
        time.sleep(delay)
    assert last_exc is not None  # the loop only exits via return or here
    raise last_exc


def _extract_next_data(html: str) -> dict:
    """Pull the `__NEXT_DATA__` JSON out of an EDHREC HTML page. Raises
    ValueError if the blob isn't present (page changed shape, or we hit a
    redirect / 404 page)."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("__NEXT_DATA__ blob not found in HTML")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"__NEXT_DATA__ blob isn't valid JSON: {exc}") from exc


def _walk_for_cardlists(node, out: dict[str, list[CardEntry]]) -> None:
    """Recursively walk the next-data blob looking for objects that look like
    EDHREC cardlist entries (`{name, inclusion, synergy, num_decks, ...}`).
    EDHREC's schema isn't strictly typed across page versions, so we hunt by
    shape rather than by path.

    Sections we recognize explicitly (kept under stable keys):
      - "high_synergy"  ← "High Synergy Cards"
      - "new_cards"     ← "New Cards"
      - "top_cards"     ← "Top Cards" / unheadered top section

    Every OTHER section ("Creatures", "Instants", "Sorceries",
    "Lands", "Mana Artifacts", "Game Changers", etc.) is bucketed
    under ``category:<original-header>`` so callers can reach all
    14ish sections EDHREC ships (~200+ cards total per page) via
    ``CommanderPage.category_lists``. Live audit 2026-05-14
    revealed the original parser was discarding 90% of EDHREC's
    data by ignoring every header that wasn't one of the three
    above — Muxus, Path to Exile, and most other staples lived in
    the broader category sections.
    """
    if isinstance(node, dict):
        # An EDHREC cardlist section typically has `header` and `cardviews`.
        if "header" in node and isinstance(node.get("cardviews"), list):
            raw_header = str(node.get("header", ""))
            header = raw_header.lower()
            bucket: list[CardEntry] = []
            for cv in node["cardviews"]:
                if not isinstance(cv, dict):
                    continue
                bucket.append(CardEntry(
                    name=str(cv.get("name", cv.get("sanitized", ""))),
                    inclusion_pct=float(cv.get("inclusion", 0) or 0),
                    synergy_pct=float(cv.get("synergy", 0) or 0) * 100,
                    num_decks=int(cv.get("num_decks", 0) or 0),
                ))
            # Bucket the section under a normalized key so the parser
            # tolerates EDHREC's variations ("Top Cards", "topcards", etc.).
            if "high synergy" in header or "high-synergy" in header:
                out.setdefault("high_synergy", []).extend(bucket)
            elif "new card" in header:
                out.setdefault("new_cards", []).extend(bucket)
            elif "top card" in header or header.strip() == "":
                out.setdefault("top_cards", []).extend(bucket)
            else:
                # Per-category sections: Creatures, Instants, Sorceries,
                # Lands, Mana Artifacts, Game Changers, Utility Lands,
                # Utility Artifacts, Enchantments, Planeswalkers,
                # Battles, etc. Keyed by the original header so the
                # CommanderPage.category_lists dict mirrors EDHREC's
                # own organization.
                key = f"category:{raw_header}"
                out.setdefault(key, []).extend(bucket)
        for v in node.values():
            _walk_for_cardlists(v, out)
    elif isinstance(node, list):
        for item in node:
            _walk_for_cardlists(item, out)


def _walk_for_moxfield_url(node) -> Optional[str]:
    """Recursively scan the next-data blob for any string that points to a
    Moxfield deck. EDHREC's "Average Deck" link goes through
    `moxfield.com/decks/<id>` but the path within the blob varies across
    page versions, so we hunt by content rather than by key."""
    if isinstance(node, str):
        if "moxfield.com/decks/" in node:
            return node
        return None
    if isinstance(node, dict):
        for v in node.values():
            hit = _walk_for_moxfield_url(v)
            if hit:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _walk_for_moxfield_url(item)
            if hit:
                return hit
    return None


def _walk_for_int(node, key: str) -> Optional[int]:
    """Find the first int-typed value at any depth keyed by `key`."""
    if isinstance(node, dict):
        if key in node:
            v = node[key]
            if isinstance(v, int):
                return v
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
        for v in node.values():
            hit = _walk_for_int(v, key)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _walk_for_int(item, key)
            if hit is not None:
                return hit
    return None


def _parse_commander_page(commander_name: str, slug: str, html: str) -> CommanderPage:
    """Build a CommanderPage from raw HTML. Tolerant of schema shifts —
    missing fields surface as empty lists, not exceptions."""
    page = CommanderPage(
        commander_name=commander_name,
        slug=slug,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        raw_size_bytes=len(html),
    )
    try:
        next_data = _extract_next_data(html)
    except ValueError:
        return page  # Empty page; caller can detect via empty top_cards.

    buckets: dict[str, list[CardEntry]] = {}
    _walk_for_cardlists(next_data, buckets)
    page.top_cards = buckets.get("top_cards", [])
    page.high_synergy_cards = buckets.get("high_synergy", [])
    page.new_cards = buckets.get("new_cards", [])
    # Per-category sections (Creatures, Instants, Sorceries, Lands,
    # Mana Artifacts, Game Changers, etc.) — strip the
    # ``"category:"`` prefix and keep the original EDHREC header
    # as the dict key.
    page.category_lists = {
        k[len("category:"):]: v
        for k, v in buckets.items()
        if k.startswith("category:")
    }

    # Recursively hunt for the "Average Deck" Moxfield URL — schema varies.
    page.average_deck_url = _walk_for_moxfield_url(next_data)

    # Deck count is also schema-fluid; search for it by key name at any depth.
    deck_count = _walk_for_int(next_data, "num_decks") or _walk_for_int(next_data, "deck_count")
    page.deck_count = deck_count

    # Related commanders — best-effort, optional.
    props = next_data.get("props", {}).get("pageProps", {})
    container = props.get("data") or props
    if isinstance(container, dict):
        related = container.get("related_commanders") or []
        if isinstance(related, list):
            page.related_commanders = [
                str(r.get("name", r.get("sanitized", "")))
                for r in related if isinstance(r, dict)
            ]
    return page


# Recognized "top" page slugs: time windows + card types. EDHREC serves
# each at json.edhrec.com/pages/top/<slug>.json.
TOP_WINDOWS = ("year", "month", "week")  # year == "Past 2 Years"
TOP_TYPES = ("creatures", "instants", "sorceries", "artifacts",
             "enchantments", "planeswalkers", "lands", "battles")


def fetch_top_cards(
    slug: str = "year",
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> list[CardEntry]:
    """Fetch EDHREC's "top cards" page for a time window or card type.

    ``slug`` is a window (``year`` = past 2 years, ``month``, ``week``) or
    a card type (``creatures``, ``instants``, ``lands``, …). Returns a
    ``CardEntry`` list ranked by popularity (``num_decks`` desc). Recency-
    aware: ``month``/``week`` surface cards trending NOW vs all-time
    staples — a stronger signal for "what to add" than a stale staple.

    Returns ``[]`` on any failure (network/404/parse) so callers degrade
    gracefully. Cached to ``.cache/edhrec/top-<slug>.json``.
    """
    cache_path = _cache_path(f"top-{slug}")
    if cache and _is_cache_fresh(cache_path, ttl_hours):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return [CardEntry(**e) for e in data.get("cards", [])]
        except (OSError, ValueError, TypeError):
            pass

    url = f"{EDHREC_JSON_BASE}/top/{urllib.parse.quote(slug)}.json"
    time.sleep(REQUEST_SLEEP_SEC)
    try:
        raw = _http_get_text_with_retry(url)
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 — degrade to empty on any failure
        return []

    buckets: dict[str, list[CardEntry]] = {}
    _walk_for_cardlists(payload, buckets)
    seen: set[str] = set()
    cards: list[CardEntry] = []
    for lst in buckets.values():
        for c in lst:
            key = c.name.lower()
            if key in seen:
                continue
            seen.add(key)
            cards.append(c)
    # /top "inclusion" is a raw deck count, not a %, so rank by num_decks.
    cards.sort(key=lambda c: c.num_decks, reverse=True)

    if cache and cards:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"slug": slug,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "cards": [asdict(c) for c in cards]}),
            encoding="utf-8")
    return cards


def top_main(argv=None) -> int:
    """``commander-top`` — list EDHREC's most-played cards for a window/type."""
    import argparse
    p = argparse.ArgumentParser(
        prog="commander-top",
        description="EDHREC top cards by time window (year/month/week) or "
                    "card type (creatures/instants/lands/…).")
    p.add_argument("slug", nargs="?", default="year",
                   help="year (past 2yr) | month | week | a card type. Default year.")
    p.add_argument("--limit", type=int, default=25)
    args = p.parse_args(argv)
    cards = fetch_top_cards(args.slug)
    if not cards:
        print(f"(no top cards for {args.slug!r} — bad slug or network)")
        return 1
    print(f"EDHREC top cards [{args.slug}] (by deck count):")
    for i, c in enumerate(cards[:args.limit], 1):
        print(f"  {i:>3}. {c.name}  ({c.num_decks:,} decks)")
    return 0


def fetch_commander_page(
    commander_or_slug: str,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> Optional[CommanderPage]:
    """Fetch + parse one commander's EDHREC page. Caches the parsed
    CommanderPage to disk; subsequent calls within `ttl_hours` skip the
    network. Returns ``None`` when EDHREC has no page (404 — unknown/mis-slugged
    commander), the retries are exhausted, or parsing fails; callers must
    handle None (the heuristic recommender already does)."""
    slug = commander_or_slug if "-" in commander_or_slug and commander_or_slug.islower() \
        else commander_slug(commander_or_slug)
    cache_path = _cache_path(slug)
    if cache and _is_cache_fresh(cache_path, ttl_hours):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return _page_from_dict(data)
        except (OSError, ValueError):
            pass  # Fall through to fresh fetch on cache corruption.

    url = f"{EDHREC_BASE}/commanders/{urllib.parse.quote(slug)}"
    time.sleep(REQUEST_SLEEP_SEC)
    try:
        html = _http_get_text_with_retry(url)
    except urllib.error.HTTPError as exc:
        # 404 happens when the slug doesn't match EDHREC's
        # canonical name (newly released commanders, edge-case
        # spellings). Return None instead of crashing the audit;
        # the caller falls back to no-EDHREC heuristics. Exhausted
        # retries on 5xx land here too — same graceful fallback.
        if exc.code == 404:
            return None
        return None
    except Exception:
        return None
    page = _parse_commander_page(commander_or_slug, slug, html)
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(page.to_json(), encoding="utf-8")
    return page


# Common tribal-tag slug mappings. EDHREC's tag URLs use lowercase
# plural forms with hyphens. Most tribes pluralize with `-s`
# ("dragon" → "dragons"), but a handful are irregular (elf →
# elves, merfolk stays "merfolk"). Curated here so callers don't
# need to special-case.
_TRIBE_TO_TAG_SLUG: dict[str, str] = {
    "Dragon": "dragons",
    "Goblin": "goblins",
    "Sliver": "slivers",
    "Elf": "elves",
    "Vampire": "vampires",
    "Zombie": "zombies",
    "Spirit": "spirits",
    "Angel": "angels",
    "Demon": "demons",
    "Beast": "beasts",
    "Wizard": "wizards",
    "Knight": "knights",
    "Merfolk": "merfolk",
    "Ninja": "ninjas",
    "Pirate": "pirates",
    "Dinosaur": "dinosaurs",
    "Faerie": "faeries",
    "Eldrazi": "eldrazi",
    "Werewolf": "werewolves",
    "Cat": "cats",
    "Bird": "birds",
    "Hydra": "hydras",
    "Treefolk": "treefolk",
    "Giant": "giants",
    "Minotaur": "minotaurs",
    "Druid": "druids",
    "Warrior": "warriors",
    "Soldier": "soldiers",
    "Human": "humans",
}


def tribe_tag_slug(tribe_name: str) -> Optional[str]:
    """Map a tribe display name (``"Dragon"``) to the EDHREC tag
    URL slug (``"dragons"``). Returns None when the tribe isn't in
    the curated map — caller skips the tag-page fetch.

    Matches ``detect_tribal_type``'s canonical tribal-type list.
    """
    return _TRIBE_TO_TAG_SLUG.get(tribe_name)


def fetch_salt_list(
    cache: bool = True,
    ttl_hours: int = 168,  # 7 days — salt scores change slowly
) -> dict[str, float]:
    """Fetch EDHREC's ``/top/salt`` page and return a mapping of
    ``{card_name_lowercase: salt_score}``.

    Salt scores are EDHREC's 0-5 measure of how unpopular a card
    is with opponents (Smothering Tithe, Rhystic Study, Cyclonic
    Rift, Stasis, etc.). Higher = more "salt" = more likely to
    cause table-talk problems. Used by the audit to flag
    bracket-mismatched picks (a B1/B2 Exhibition/Core deck
    shouldn't include the top-10 saltiest cards).

    Returns an empty dict on fetch failure. Cached for 168h
    (a week) since salt scores update slowly and the URL doesn't
    take any parameters.
    """
    cache_path = CACHE_DIR.parent / "edhrec_salt" / "top-salt.json"
    if cache and _is_cache_fresh(cache_path, ttl_hours):
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # Cache corruption → fresh fetch.

    url = f"{EDHREC_BASE}/top/salt"
    time.sleep(REQUEST_SLEEP_SEC)
    try:
        html = _http_get_text_with_retry(url)
    except Exception:  # noqa: BLE001
        return {}

    try:
        next_data = _extract_next_data(html)
    except ValueError:
        return {}

    # Walk the blob looking for a section with cards carrying a
    # ``label: "Salt Score: X.XX"`` annotation. There's exactly
    # one such section on the /top/salt page; the parser tolerates
    # any header text.
    salt_map: dict[str, float] = {}
    def _walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("cardviews"), list):
                for cv in node["cardviews"]:
                    if not isinstance(cv, dict):
                        continue
                    name = cv.get("name") or cv.get("sanitized")
                    label = cv.get("label", "")
                    if not name or "Salt Score" not in label:
                        continue
                    # "Salt Score: 3.06" → 3.06
                    import re as _re
                    m = _re.search(r"([\d.]+)", label)
                    if m:
                        try:
                            salt_map[name.lower()] = float(m.group(1))
                        except ValueError:
                            pass
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
    _walk(next_data)

    if cache and salt_map:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(salt_map), encoding="utf-8",
            )
        except OSError:
            pass
    return salt_map


def fetch_tag_page(
    tag_slug: str,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> Optional[CommanderPage]:
    """Fetch + parse an EDHREC ``/tags/<slug>`` page.

    Tag pages have IDENTICAL structure to commander pages: same
    14ish sections (Top Cards / High Synergy / New Cards / Game
    Changers / Creatures / Instants / Sorceries / Lands / Mana
    Artifacts / ...) and the same ``__NEXT_DATA__`` shape. So we
    reuse ``_parse_commander_page`` verbatim — the returned
    ``CommanderPage`` is structurally identical, just sourced from
    a tag instead of a commander.

    Returns None on 404 (unknown tag slug) or any other fetch
    failure. Cached separately from commander pages under
    ``.cache/edhrec_tag/<slug>.json``.

    Use case: tribal/themed decks (Dragon, Goblin, Sliver, Tokens,
    Spellslinger, …). The commander-specific page covers what THIS
    commander runs; the tag page covers the broader archetype
    pool. Folding both into the heuristic's known-card set gives
    fuller coverage for cut decisions, and the tag page's high-
    synergy/top sections surface archetype staples the commander
    page might miss.
    """
    if not tag_slug:
        return None
    safe_slug = tag_slug.strip().lower()
    if not safe_slug:
        return None
    cache_path = CACHE_DIR.parent / "edhrec_tag" / f"{safe_slug}.json"
    if cache and _is_cache_fresh(cache_path, ttl_hours):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return _page_from_dict(data)
        except (OSError, ValueError):
            pass  # Cache corruption → fresh fetch.

    url = f"{EDHREC_BASE}/tags/{urllib.parse.quote(safe_slug)}"
    time.sleep(REQUEST_SLEEP_SEC)
    try:
        html = _http_get_text_with_retry(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return None
    except Exception:
        return None
    # Reuse the commander-page parser — tag pages have the same
    # __NEXT_DATA__ shape. The ``commander_name`` field on the
    # returned CommanderPage carries the tag slug (no real
    # commander name for tag pages); downstream code that cares
    # about display can grep for ``"slug": tag``.
    page = _parse_commander_page(
        commander_name=f"tag:{safe_slug}",
        slug=safe_slug,
        html=html,
    )
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(page.to_json(), encoding="utf-8")
    return page


def _page_from_dict(d: dict) -> CommanderPage:
    """Rehydrate a CommanderPage from a cached JSON dict."""
    def card_list(rows):
        return [CardEntry(**r) for r in rows or []]
    return CommanderPage(
        commander_name=d.get("commander_name", ""),
        slug=d.get("slug", ""),
        fetched_at=d.get("fetched_at", ""),
        top_cards=card_list(d.get("top_cards", [])),
        high_synergy_cards=card_list(d.get("high_synergy_cards", [])),
        new_cards=card_list(d.get("new_cards", [])),
        category_lists={
            k: card_list(v)
            for k, v in (d.get("category_lists") or {}).items()
        },
        related_commanders=list(d.get("related_commanders", [])),
        average_deck_url=d.get("average_deck_url"),
        deck_count=d.get("deck_count"),
        raw_size_bytes=int(d.get("raw_size_bytes", 0)),
    )


@dataclass
class AverageDeck:
    """EDHREC's auto-generated 'average deck' for a commander+bracket+budget.
    Lives at `/average-decks/<slug>/<bracket>/<budget>`. Returned as a
    Moxfield-shape dict so it can flow through `to_dck` and `_import_reference`
    unchanged."""
    commander_name: str
    slug: str
    url: str
    bracket_slug: Optional[str]      # "upgraded" / "optimized" / etc.
    budget_slug: Optional[str]       # "expensive" / "budget" / None
    cards: list[CardEntry]           # mainboard + commander, mixed

    def to_moxfield_shape(self, bracket_int: Optional[int] = None) -> dict:
        """Build a Moxfield-shape deck JSON so existing import code can
        consume this without a separate path. Commander cards are routed to
        the [Commander] section by name match (the commander itself in the
        cards list); everything else lands in [Main]."""
        cmdr_name_lc = self.commander_name.lower()
        commanders: dict[str, dict] = {}
        mainboard: dict[str, dict] = {}
        for i, card in enumerate(self.cards):
            entry = {
                "quantity": int(card.num_decks) if card.num_decks else 1,
                "card": {
                    "name": card.name,
                    "set": "",
                    "cn": "",
                },
            }
            if card.name.lower() == cmdr_name_lc:
                # Commander goes to its own bucket; quantity is always 1.
                entry["quantity"] = 1
                commanders[f"cmdr-{i}"] = entry
            else:
                # `num_decks` from EDHREC is "appears in N decks", not "use N
                # copies". For mainboard, use 1 unless name suggests basic
                # land where multiples are typical.
                qty = 1
                # Basics get a generous default; EDHREC average decks
                # represent basic counts in the deck-count number.
                lc = card.name.lower()
                if lc in {"forest", "island", "plains", "mountain", "swamp", "wastes"}:
                    qty = max(1, min(40, int(card.num_decks) or 1))
                entry["quantity"] = qty
                mainboard[f"main-{i}"] = entry
        return {
            "name": f"EDHREC Average — {self.commander_name}"
                    + (f" ({self.bracket_slug})" if self.bracket_slug else "")
                    + (f"/{self.budget_slug}" if self.budget_slug else ""),
            "publicId": None,  # Not a Moxfield deck.
            "format": "commander",
            "bracket": bracket_int,
            "boards": {
                "commanders": {"cards": commanders},
                "mainboard": {"cards": mainboard},
            },
        }


# Map our integer bracket → EDHREC's URL slug.
BRACKET_SLUG: dict[int, str] = {
    1: "exhibition",
    2: "core",
    3: "upgraded",
    4: "optimized",
    5: "cedh",
}


def _walk_for_average_deck_cards(node) -> list[CardEntry]:
    """Find the cardlist that represents the average deck itself. EDHREC's
    average-deck pages typically have ONE big cardlist (the deck contents)
    where every entry has a num_decks count near 100 (since it's "this many
    of the average deck's slots are this card"). Distinct from the commander
    page's many small cardlists per category."""
    out: list[CardEntry] = []
    seen: set[str] = set()

    def _walk(n):
        if isinstance(n, dict):
            # An average-deck card list looks like a `cardviews` array under
            # any object that also has cards in it. The cardviews inside
            # average-deck pages have `name` and either `num_decks` or
            # similar count.
            views = n.get("cardviews")
            if isinstance(views, list):
                for cv in views:
                    if not isinstance(cv, dict):
                        continue
                    name = str(cv.get("name", cv.get("sanitized", ""))).strip()
                    if not name or name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    out.append(CardEntry(
                        name=name,
                        inclusion_pct=float(cv.get("inclusion", 0) or 0),
                        synergy_pct=float(cv.get("synergy", 0) or 0) * 100,
                        num_decks=int(cv.get("num_decks", 0) or 0),
                    ))
            for v in n.values():
                _walk(v)
        elif isinstance(n, list):
            for item in n:
                _walk(item)

    _walk(node)
    return out


def fetch_average_deck(
    commander_or_slug: str,
    bracket: Optional[int] = None,
    budget: Optional[str] = None,
    direct_url: Optional[str] = None,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> Optional[AverageDeck]:
    """Fetch EDHREC's average deck.

    Three call shapes:
      1. `direct_url=...` (e.g. user passed an EDHREC URL on the CLI) —
         fetch that exact URL.
      2. `bracket=N, budget=X` — build URL `/average-decks/<slug>/<bracket-slug>/<budget>`.
      3. `bracket=N` — build URL `/average-decks/<slug>/<bracket-slug>` and
         try without the budget tier.
      4. Bare commander — fall back to `/average-decks/<slug>`.

    Returns None if no average deck is published for the request OR the page
    has no parseable cardlist."""
    if direct_url:
        url = direct_url
        slug_from_url = urllib.parse.urlparse(direct_url).path
    else:
        slug = commander_slug(commander_or_slug)
        bracket_slug = BRACKET_SLUG.get(bracket) if bracket else None
        path = f"/average-decks/{slug}"
        if bracket_slug:
            path += f"/{bracket_slug}"
            if budget:
                path += f"/{budget}"
        url = f"{EDHREC_BASE}{path}"
        slug_from_url = path

    cache_key = re.sub(r"[^a-z0-9]+", "_", slug_from_url.lower()).strip("_")[:120] or "avg"
    cache_path = CACHE_DIR.parent / "edhrec_avg" / f"{cache_key}.json"
    if cache and _is_cache_fresh(cache_path, ttl_hours):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return AverageDeck(
                commander_name=data["commander_name"],
                slug=data["slug"],
                url=data["url"],
                bracket_slug=data.get("bracket_slug"),
                budget_slug=data.get("budget_slug"),
                cards=[CardEntry(**c) for c in data["cards"]],
            )
        except (OSError, ValueError, KeyError):
            pass  # Fall through to fresh fetch.

    try:
        time.sleep(REQUEST_SLEEP_SEC)
        html = _http_get_text_with_retry(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        return None
    except Exception:
        return None

    try:
        next_data = _extract_next_data(html)
    except ValueError:
        return None

    cards = _walk_for_average_deck_cards(next_data)
    if not cards:
        return None

    deck = AverageDeck(
        commander_name=commander_or_slug if direct_url is None else "Unknown",
        slug=commander_slug(commander_or_slug) if direct_url is None else "",
        url=url,
        bracket_slug=BRACKET_SLUG.get(bracket) if bracket else None,
        budget_slug=budget,
        cards=cards,
    )

    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "commander_name": deck.commander_name,
            "slug": deck.slug,
            "url": deck.url,
            "bracket_slug": deck.bracket_slug,
            "budget_slug": deck.budget_slug,
            "cards": [asdict(c) for c in deck.cards],
        }, indent=2), encoding="utf-8")

    return deck


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: edhrec_client.py <commander-name-or-slug>")
        sys.exit(2)
    page = fetch_commander_page(" ".join(sys.argv[1:]))
    if page is None:
        print(json.dumps({"error": "no EDHREC page found (unknown commander, "
                          "bad slug, or fetch failed)"}))
        sys.exit(1)
    print(json.dumps({
        "commander": page.commander_name,
        "slug": page.slug,
        "deck_count": page.deck_count,
        "top_card_count": len(page.top_cards),
        "high_synergy_count": len(page.high_synergy_cards),
        "first_5_top_cards": [c.name for c in page.top_cards[:5]],
        "average_deck_url": page.average_deck_url,
    }, indent=2))
