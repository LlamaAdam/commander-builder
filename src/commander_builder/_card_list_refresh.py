"""Helpers for refreshing the hardcoded card lists in
``deck_health.py`` against current Scryfall data.

The lists (``_MDFC_LANDS``, ``_WINCON_PROTECTION``, ``_SELF_MILL_ENABLERS``)
are short, stable, and curated by hand — but they slowly go stale as new
sets ship. This module backs the ``scripts/refresh_card_lists.py`` CLI
that surfaces:

- Cards in our list but no longer (or never) on Scryfall under the
  relevant filter (typos, renames, mis-curation).
- Cards on Scryfall that match the filter but aren't in our list
  (candidates a maintainer should review before adding).

The maintainer reads the report and updates the hardcoded lists in
``deck_health.py``. This is intentionally NOT a code generator —
each list has curation nuance (e.g. ``_SELF_MILL_ENABLERS`` excludes
opponent-mill cards even though their oracle text matches simple
patterns) that's easier to express via human review than via more
regex.

Pure helpers (``diff_card_lists``, ``parse_mdfc_lands_from_response``)
have no IO and are unit-tested. The fetch wrappers
(``fetch_mdfc_lands``) take a ``http_get`` callable so the network
hop can be injected.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional


CardSet = frozenset[str]


def diff_card_lists(current: Iterable[str], fresh: Iterable[str]) -> dict:
    """Compare ``current`` (our hardcoded list) against ``fresh`` (the
    set Scryfall returned today).

    Both inputs are case-folded internally so casing differences don't
    create false noise. Returns::

        {
          "stale": [str, ...],     # in current but NOT in fresh
          "candidates": [str, ...],# in fresh but NOT in current
          "kept": [str, ...],      # in both (sanity / progress signal)
        }

    Lists are sorted alphabetically for stable diff output.
    """
    cur = frozenset(c.lower() for c in current if c)
    new = frozenset(c.lower() for c in fresh if c)
    return {
        "stale": sorted(cur - new),
        "candidates": sorted(new - cur),
        "kept": sorted(cur & new),
    }


def parse_mdfc_lands_from_response(payload: dict) -> set[str]:
    """Project one Scryfall ``/cards/search`` response into the set of
    lowercase card names that qualify as MDFC lands.

    Qualification: ``layout == 'modal_dfc'`` AND at least one of the
    card's faces has ``Land`` in its ``type_line``. That matches the
    curation rule in ``deck_health._MDFC_LANDS`` — Pathways (both
    faces land) qualify, spell+spell modal cards (like Sea Gate
    Stormcaller) don't.

    Card names with ``//`` separators (the Scryfall convention for
    double-faced cards) are reduced to the front-face name so they
    line up with how .dck files reference them.
    """
    out: set[str] = set()
    for card in (payload or {}).get("data") or []:
        if (card.get("layout") or "").lower() != "modal_dfc":
            continue
        faces = card.get("card_faces") or []
        if not any(
            "land" in ((f or {}).get("type_line") or "").lower()
            for f in faces
        ):
            continue
        name = card.get("name") or ""
        if "//" in name:
            name = name.split("//", 1)[0].strip()
        if name:
            out.add(name.lower())
    return out


def fetch_mdfc_lands(
    http_get: Optional[Callable[[str], dict]] = None,
    initial_url: str = (
        "https://api.scryfall.com/cards/search?q=layout:modal_dfc"
    ),
) -> set[str]:
    """Walk Scryfall's paginated search response for MDFC layout cards,
    accumulating the set of lowercase names that qualify as MDFC lands.

    ``http_get`` is the JSON-fetching callable; defaults to
    ``scryfall_client._http_get_json`` (which handles rate-limit
    backoff and User-Agent). Injected for testability — tests pass a
    fake that yields canned responses without touching the network.

    Pagination follows the standard ``has_more`` / ``next_page``
    fields. The loop stops when ``has_more`` is false or
    ``next_page`` is missing, so a malformed response doesn't spin.
    """
    if http_get is None:
        from .scryfall_client import _http_get_json
        http_get = _http_get_json

    seen: set[str] = set()
    url: Optional[str] = initial_url
    pages = 0
    while url and pages < 50:  # safety cap; real result is well under
        payload = http_get(url)
        seen |= parse_mdfc_lands_from_response(payload)
        if not payload or not payload.get("has_more"):
            break
        url = payload.get("next_page")
        pages += 1
    return seen
