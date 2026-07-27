"""Archidekt deck-search client — the reference corpus' third source.

FP-015 bubble slice follow-up (operator direction 2026-07-25: ground
"what good decks look like" in Moxfield + EDHREC "and some other
site"). Archidekt is that site: its public JSON API needs no auth and
carries a per-deck ``edhBracket`` field the other sources lack.

API shapes (probed live 2026-07-25 with the project User-Agent):

- **Search**: ``GET archidekt.com/api/decks/v3/?commanderName=<name>
  &orderBy=-viewCount&pageSize=N`` →
  ``{count, results: [{id, name, size, edhBracket, deckFormat, ...}]}``.
  ``edhBracket`` is 1-5 or null (most decks are null — treat null as
  "unknown", never as a mismatch).
- **Detail**: ``GET archidekt.com/api/decks/<id>/`` →
  ``{cards: [{quantity, categories: [<name>, ...],
  card: {oracleCard: {name}}}], categories: [{name, includedInDeck,
  ...}]}``. Category membership is the deck's own board semantics:
  Maybeboard/Sideboard categories carry ``includedInDeck: false``.

Same degrade-don't-die contract as the other clients: every public
function returns an empty result on network/shape failure rather than
raising, and ``fetch_json`` is injectable so tests stay offline.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

BASE = "https://archidekt.com/api"
USER_AGENT = (
    "commander-builder/0.1 (+https://github.com/LlamaAdam/commander-builder)"
)

#: Default number of reference decks to pull. Each deck is one detail
#: GET, so this is also the request budget for a cold corpus build —
#: deliberately smaller than the Moxfield default (50).
DEFAULT_N = 25

#: Skip obviously-partial decks (a "Commander deck" with 30 cards is a
#: draft in progress, not a reference build).
MIN_DECK_SIZE = 60

#: Retry budget for rate-limited / transient-5xx requests. A couple of
#: retries, smaller than EDHREC's (3): a cold corpus build issues up to
#: ~26 sequential GETs, so a hard-down Archidekt must not multiply into
#: minutes of backoff.
MAX_RETRIES = 2
RETRY_BASE_DELAY_SEC = 1.0

# Same transient set as edhrec_client: 429 is rate-limiting (honor
# Retry-After), 5xx is server-side weather. Other 4xx (404, 400, 403)
# are deterministic — retrying can't help, raise immediately.
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _get_json_with_retry(
    get: Callable[[str], dict],
    url: str,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_SEC,
) -> dict:
    """Call ``get(url)`` with backoff on 429/5xx HTTPError.

    Mirrors edhrec_client's ``_http_get_text_with_retry`` house pattern
    (Retry-After honored and clamped, else exponential backoff, one
    stderr line per retry) but wraps the injectable ``get`` seam so
    tests exercise it with canned responses, and retries ONLY
    HTTPError: network-level failures (URLError/OSError) propagate
    immediately — the caller's degrade-don't-die handling owns those,
    and stacking sleeps onto a dead network would triple the corpus
    build's failure latency.
    """
    from .edhrec_client import MAX_RETRY_AFTER_SEC, _parse_retry_after

    last_exc: Optional[urllib.error.HTTPError] = None
    for attempt in range(max_retries + 1):
        try:
            return get(url)
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
            f"[archidekt] retry {attempt + 1}/{max_retries} after "
            f"HTTP {last_exc.code} — sleeping {delay:.1f}s",
            file=sys.stderr, flush=True,
        )
        time.sleep(delay)
    assert last_exc is not None  # the loop only exits via return or here
    raise last_exc


def extract_mainboard(deck_json: dict) -> list[str]:
    """Pull the playable-deck card names out of an Archidekt detail JSON.

    Honors the deck's own category flags: a card counts iff its
    ``quantity`` > 0, none of its categories is marked
    ``includedInDeck: false`` at the deck level, and it is not in the
    ``Commander`` category (the corpus wants the 99 — the commander is
    the query key, not a data point). A card with NO categories counts
    (Archidekt leaves categories empty for plain mainboard entries).
    """
    excluded = {"commander"}
    for cat in deck_json.get("categories") or []:
        if isinstance(cat, dict) and cat.get("includedInDeck") is False:
            name = (cat.get("name") or "").strip().lower()
            if name:
                excluded.add(name)
    out: list[str] = []
    for entry in deck_json.get("cards") or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("quantity"):
            continue
        cats = [str(c).strip().lower()
                for c in (entry.get("categories") or [])]
        if any(c in excluded for c in cats):
            continue
        name = (((entry.get("card") or {}).get("oracleCard") or {})
                .get("name") or "").strip()
        if name:
            out.append(name)
    return out


def fetch_top_decks(
    commander: str,
    bracket: Optional[int] = None,
    n: int = DEFAULT_N,
    *,
    fetch_json: Optional[Callable[[str], dict]] = None,
) -> list[list[str]]:
    """Return up to ``n`` mainboard card-name lists for ``commander``,
    most-viewed first.

    ``bracket`` soft-filters: a search hit whose ``edhBracket`` is SET
    and differs is skipped; null (the common case) always passes —
    strict filtering would starve the corpus. A non-numeric
    ``edhBracket`` (API drift) drops that hit only — never the whole
    source. Individual detail-fetch failures skip that deck; a search
    failure returns ``[]``. 429/5xx responses get a couple of retries
    (Retry-After honored) before either verdict.
    """
    if n <= 0:
        return []
    get = fetch_json or _http_get_json
    params = {
        "commanderName": commander,
        "orderBy": "-viewCount",
        # Overshoot the goal so bracket mismatches, partial decks, and
        # fetch failures don't shrink the result below n.
        "pageSize": str(min(max(n * 2, 50), 100)),
    }
    url = f"{BASE}/decks/v3/?{urllib.parse.urlencode(params)}"
    try:
        payload = _get_json_with_retry(get, url)
    except Exception:  # noqa: BLE001 — search outage = empty corpus source
        return []
    results = payload.get("results") or []

    out: list[list[str]] = []
    for hit in results:
        if len(out) >= n:
            break
        if not isinstance(hit, dict) or not hit.get("id"):
            continue
        size = hit.get("size")
        if isinstance(size, int) and size < MIN_DECK_SIZE:
            continue
        if bracket is not None:
            hit_bracket = hit.get("edhBracket")
            if hit_bracket is not None:
                try:
                    if int(hit_bracket) != int(bracket):
                        continue
                except (TypeError, ValueError):
                    # API drift: an unparseable bracket drops THIS hit,
                    # not the whole source.
                    continue
        try:
            detail = _get_json_with_retry(get, f"{BASE}/decks/{hit['id']}/")
        except Exception:  # noqa: BLE001 — one dead deck, not the run
            continue
        names = extract_mainboard(detail)
        if names:
            out.append(names)
    return out
