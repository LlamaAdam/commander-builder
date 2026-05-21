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

import re
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


def parse_self_mill_from_response(payload: dict) -> set[str]:
    """Project one Scryfall ``/cards/search`` response into the set of
    lowercase card names that qualify as self-mill enablers.

    Qualification (must all hold):
      - oracle_text mentions both ``your library`` AND ``your graveyard``
        (the milling motion). Catches "reveal cards from the top of
        your library ... put the rest into your graveyard" patterns
        (Hermit Druid, Satyr Wayfinder) plus direct "into your
        graveyard" milling.
      - OR the text uses the literal word ``mill`` paired with
        ``you`` or ``your`` (avoids matching "target opponent
        mills"). Catches Stitcher's Supplier "mill three cards"
        and similar concise forms.
      - NOT a pure opponent-mill card: oracle must not contain
        ``target opponent`` or ``target player`` or
        ``each opponent`` as the milling target. Mesmeric Orb's
        "permanent's controller mills" survives because the
        targeting isn't a player.
      - NOT a card that exiles instead of mills (e.g. Bojuka Bog
        — "exile all cards in target player's graveyard" — wrong
        zone).

    Card-name normalization mirrors ``parse_mdfc_lands_from_response``:
    DFC names collapse to the front face's name; lowercase result.
    """
    out: set[str] = set()
    for card in (payload or {}).get("data") or []:
        oracle = (card.get("oracle_text") or "").lower()
        if not oracle:
            # DFC: walk per-face oracle text too.
            faces = card.get("card_faces") or []
            oracle = " ".join(
                ((f or {}).get("oracle_text") or "").lower() for f in faces
            )
        if not oracle:
            continue

        # Negative filters first — short-circuit obvious opponent-mill.
        if "target opponent" in oracle or "target player" in oracle:
            continue
        if "each opponent" in oracle and "mill" in oracle:
            # Cards like Mind Funeral / Maddening Cacophony.
            continue
        if "each player" in oracle and "mill" in oracle:
            # Symmetrical mill (everyone mills). Not a self-mill
            # enabler — players USE it sideways but it's an attack
            # card by intent.
            continue

        # Positive: any "mill" keyword surviving the negatives, OR
        # the explicit self-motion pattern (reveal-from-library +
        # put-into-your-graveyard). Magic's default when "mill N"
        # has no target is "you mill" — so any unfiltered ``mill``
        # mention is self-mill by elimination.
        has_mill = re.search(r"\bmill\b", oracle) is not None
        motion = "your library" in oracle and "your graveyard" in oracle
        if not (has_mill or motion):
            continue

        name = card.get("name") or ""
        if "//" in name:
            name = name.split("//", 1)[0].strip()
        if name:
            out.add(name.lower())
    return out


def fetch_self_mill_candidates(
    http_get: Optional[Callable[[str], dict]] = None,
    initial_url: str = (
        "https://api.scryfall.com/cards/search?"
        "q=oracle%3A%22into+your+graveyard%22+oracle%3A%22your+library%22"
    ),
) -> set[str]:
    """Walk Scryfall's paginated search response for self-mill
    candidates and project via ``parse_self_mill_from_response``.

    Query: ``oracle:"into your graveyard" oracle:"your library"``
    — broad enough to catch the Hermit-Druid / Satyr-Wayfinder /
    Buried-Alive shape; per-card post-filter trims the obvious
    opponent-mill false positives.

    Same pagination + safety-cap pattern as ``fetch_mdfc_lands``.
    """
    if http_get is None:
        from .scryfall_client import _http_get_json
        http_get = _http_get_json

    seen: set[str] = set()
    url: Optional[str] = initial_url
    pages = 0
    while url and pages < 50:
        payload = http_get(url)
        seen |= parse_self_mill_from_response(payload)
        if not payload or not payload.get("has_more"):
            break
        url = payload.get("next_page")
        pages += 1
    return seen


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
