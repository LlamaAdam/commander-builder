"""Helpers for refreshing the hardcoded card lists in
``deck_health.py`` and ``bracket_estimator.py`` against current
Scryfall data.

The lists (``deck_health._MDFC_LANDS`` / ``_WINCON_PROTECTION`` /
``_SELF_MILL_ENABLERS``, ``bracket_estimator._EXTRA_TURN_CARDS`` /
``_MLD_CARDS``) are short, stable, and curated by hand — but they
slowly go stale as new sets ship. This module backs the
``scripts/refresh_card_lists.py`` CLI that surfaces:

- Cards in our list but not matched by fresh data under the relevant
  filter (typos, renames, mis-curation).
- Fresh-data cards that match the filter but aren't in our list
  (candidates a maintainer should review before adding).

The maintainer reads the report and updates the hardcoded lists.
This is intentionally NOT a code generator — each list has curation
nuance (e.g. ``_SELF_MILL_ENABLERS`` excludes opponent-mill cards even
though their oracle text matches simple patterns) that's easier to
express via human review than via more regex.

Two data sources, chosen per list:

- **Scryfall search API** (``fetch_mdfc_lands`` and friends) for
  filters Scryfall can express server-side. Wrappers take an
  ``http_get`` callable so the network hop can be injected.
- **The local oracle-snapshot store** (``iter_snapshot_cards`` +
  ``extra_turn_names_from_snapshots`` / ``mld_names_from_snapshots``)
  for oracle-text scans over the whole card pool: the ~32k snapshots
  ``commander-oracle-refresh --from-bulk`` maintains already sit on
  disk, so a full-pool text scan costs ZERO network requests. The
  freshness of these lists is therefore governed by the snapshot
  store's own refresh cadence.

Pure helpers (``diff_card_lists``, ``parse_*_from_response``, the two
``card_*`` predicates) have no IO and are unit-tested.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional


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


# ---------------------------------------------------------------------------
# Local-snapshot scans — bracket_estimator's extra-turn + MLD lists
# ---------------------------------------------------------------------------
#
# Unlike the MDFC/self-mill categories above, these two scans read the
# LOCAL oracle-snapshot store (``scryfall_client.CACHE_DIR``, the same
# per-card files ``commander-oracle-refresh --from-bulk`` maintains)
# instead of the Scryfall search API: the filter is a plain oracle-text
# scan over the whole card pool, the pool already sits on disk, and a
# zero-network refresh keeps this script cheap to run alongside every
# snapshot refresh. Predicates are pure (card dict -> bool) and the
# store walk is a separate, injectable step, mirroring the
# parse-vs-fetch split above.


def _all_oracle_text(card: dict) -> str:
    """Lowercased oracle text across the card and its faces.

    DFC/split/adventure cards carry per-face ``oracle_text`` and often
    omit it at the top level; scanning both mirrors
    ``parse_self_mill_from_response``'s face walk.
    """
    parts = [(card or {}).get("oracle_text") or ""]
    for face in (card or {}).get("card_faces") or []:
        parts.append((face or {}).get("oracle_text") or "")
    return "\n".join(p for p in parts if p).lower()


def _front_face_name_lc(card: dict) -> Optional[str]:
    """Lowercase front-face name, matching the ``//``-splitting
    convention of the parsers above (deck files reference DFCs by the
    front face). None when the card has no usable name."""
    name = (card or {}).get("name") or ""
    if "//" in name:
        name = name.split("//", 1)[0].strip()
    return name.lower() or None


# "Take an extra turn after this one." / "Target player takes an extra
# turn..." / "takes two extra turns" (Time Stretch) / "take X extra
# turns". Anchored on take/takes so prevention text ("If a player
# would BEGIN an extra turn, that player skips that turn instead" —
# Ugin's Nexus, Stranglehold) never matches.
_EXTRA_TURN_RE = re.compile(
    r"\btakes?\s+(?:an|two|three|x|\d+)\s+extra\s+turns?\b",
)


def card_grants_extra_turn(card: dict) -> bool:
    """True when the card's oracle text grants an extra turn.

    Known review-noise: cards that hand an OPPONENT an extra turn as a
    cost (Emrakul, the Promised End) also match — that's what the
    maintainer-review step is for, and over-reporting candidates beats
    silently missing new time-magic printings.
    """
    return _EXTRA_TURN_RE.search(_all_oracle_text(card)) is not None


# Mass-land-denial oracle shapes, each anchored inside one sentence
# ([^.\n]* never crosses a period). ``\blands?\b`` word-bounds so
# "Islands"/"Wastelands" never false-positive. The "each player"
# patterns require all/a-number so one-land symmetric sacrifice
# (Smallpox) doesn't read as MASS denial.
_MLD_NUMBER = r"(?:all|two|three|four|five|six|seven|eight|nine|ten|x|\d+)"
_MLD_PATTERNS = tuple(re.compile(p) for p in (
    # Armageddon / Obliterate / Jokulhaups ("destroy all ... lands")
    r"destroys? all [^.\n]*?\blands\b",
    # Decree of Annihilation
    r"exiles? all [^.\n]*?\blands\b",
    # Sunder
    r"returns? all [^.\n]*?\blands\b",
    # Wildfire / Impending Disaster / Keldon Firebombers / Epicenter /
    # Death Cloud ("each player ... sacrifices four/all/X ... lands")
    r"each player [^.\n]*?sacrifices " + _MLD_NUMBER + r" [^.\n]*?\blands?\b",
    # Cataclysm / Global Ruin ("each player chooses ... a land ...,
    # then sacrifices the rest")
    r"each player chooses [^.\n]*?\bland\b[^.\n]*?sacrifices the rest",
    # Burning of Xinye ("you destroy four lands ... opponent destroys
    # four lands")
    r"destroys? " + _MLD_NUMBER + r" lands\b",
))


def card_matches_mass_land_denial(card: dict) -> bool:
    """True when the oracle text reads as MASS land denial.

    Mass = hits every player's lands wholesale (destroy/exile/return
    ALL lands, symmetric multi-land sacrifice, choose-N-sacrifice-the-
    rest). Single-target land destruction (Strip Mine) and one-land
    symmetric edicts (Smallpox) deliberately do NOT match — WotC's
    bracket guidance gates the board-wiping kind.
    """
    text = _all_oracle_text(card)
    if not text:
        return False
    return any(p.search(text) for p in _MLD_PATTERNS)


def iter_snapshot_cards(cache_dir: Optional[Path] = None) -> Iterator[dict]:
    """Yield every parseable card dict in the oracle-snapshot store.

    ``cache_dir`` defaults to ``scryfall_client.CACHE_DIR`` (resolved at
    call time so test monkeypatches apply). Corrupt / non-dict files are
    skipped silently — one bad snapshot must not abort a full-store scan
    (same contract as ``oracle_store.iter_cached_names``). NOTE the
    store writes alias files (front-face / diacritic-folded slugs), so
    the same card may be yielded more than once; the set-building
    consumers below dedupe by name.
    """
    if cache_dir is None:
        from .scryfall_client import CACHE_DIR as _dir
        cache_dir = _dir
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return
    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            yield data


def extra_turn_names_from_snapshots(
    cards: Optional[Iterable[dict]] = None,
) -> set[str]:
    """Lowercase front-face names of every snapshot card granting an
    extra turn. ``cards`` is injectable for tests; defaults to a full
    walk of the local snapshot store."""
    if cards is None:
        cards = iter_snapshot_cards()
    out: set[str] = set()
    for card in cards:
        if card_grants_extra_turn(card):
            name = _front_face_name_lc(card)
            if name:
                out.add(name)
    return out


def mld_names_from_snapshots(
    cards: Optional[Iterable[dict]] = None,
) -> set[str]:
    """Lowercase front-face names of every snapshot card matching the
    mass-land-denial oracle shapes. ``cards`` is injectable for tests;
    defaults to a full walk of the local snapshot store."""
    if cards is None:
        cards = iter_snapshot_cards()
    out: set[str] = set()
    for card in cards:
        if card_matches_mass_land_denial(card):
            name = _front_face_name_lc(card)
            if name:
                out.add(name)
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
