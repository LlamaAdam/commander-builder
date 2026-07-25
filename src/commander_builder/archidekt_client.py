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


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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
    strict filtering would starve the corpus. Individual detail-fetch
    failures skip that deck; a search failure returns ``[]``.
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
        payload = get(url)
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
        hit_bracket = hit.get("edhBracket")
        if (bracket is not None and hit_bracket is not None
                and int(hit_bracket) != int(bracket)):
            continue
        try:
            detail = get(f"{BASE}/decks/{hit['id']}/")
        except Exception:  # noqa: BLE001 — one dead deck, not the run
            continue
        names = extract_mainboard(detail)
        if names:
            out.append(names)
    return out
