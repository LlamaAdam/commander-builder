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
  card: {collectorNumber, edition: {editioncode},
  oracleCard: {name, layout, faces}}}],
  categories: [{name, isPremier, includedInDeck, ...}]}``. Category
  membership is the deck's own board semantics — the ``includedInDeck``
  FLAG decides, never the category's name.

WHAT A REAL DETAIL RESPONSE ACTUALLY LOOKS LIKE (R2-P18, 2026-08-20).
Everything above was probed by hand; the adapter below was then tested
only against synthesized shapes, which is exactly the setup where a wrong
assumption survives a green suite. Deck 24864897 ("Hazel demands
Sacrifice", 98 entries) was captured live and trimmed into
``tests/fixtures/archidekt_deck_shape.json``. What it corrected:

- **``includedInDeck`` is per-deck user state, NOT a property of the
  category's name.** This deck's ``Sideboard`` category carries
  ``includedInDeck: true`` — the old claim here ("Maybeboard/Sideboard
  carry ``includedInDeck: false``") is simply false, and any future
  name-based shortcut would import that deck's sideboard as maindeck.
  ``Maybeboard`` was the only excluded category in the capture.
- **Commanders are marked by the card-level category string
  ``"Commander"``**, matching a deck-level category that is the only one
  with ``isPremier: true``. There is no top-level ``commander`` field.
- **Entry categories stack**: a maybeboarded card carries BOTH, e.g.
  ``["Maybeboard", "Tokens"]`` — so exclusion has to test EVERY category
  on the entry (it does), not just the first.
- **``quantity`` is a real multiplier** (11x Forest, 9x Swamp in the
  capture) and must survive into the ``.dck``.
- **``collectorNumber`` is a string and is not always numeric**: The List
  printings come back as ``"M20-193"`` / ``"HOU-77"`` with
  ``editioncode: "plst"``.
- **Non-``normal`` layouts need no special handling here**: the capture's
  one ``layout: "class"`` card (Ninja Teen) carries a single
  ``oracleCard.name`` and ``faces: []`` like every other entry.
  UNPINNED: no modal-DFC/transform card was in this deck, so whether an
  MDFC's ``name`` is ``"A // B"`` (with ``faces`` populated) is still
  unverified — capture a real MDFC deck rather than guessing.
- **``description`` is a Quill Delta JSON string**, not prose — see
  ``tests/fixtures/hazel_primer.md``. Nothing here reads it yet.
- **``intentionallySkippedCardData``** (false in the capture) is the API
  telling you the response omitted card data — see :func:`fetch_deck`.

Same degrade-don't-die contract as the other clients: every CORPUS
function returns an empty result on network/shape failure rather than
raising, and ``fetch_json`` is injectable so tests stay offline. The
single-deck import lane added 2026-08-17 (decision C3) is the documented
exception — see :func:`fetch_deck`.

WHAT THIS CLIENT COVERS, AND WHAT IT DOESN'T (decision C3, 2026-08-17).
Archidekt is the resilience lane for Moxfield, not a replacement:

- **Single-deck import** — full parity. ``parse_deck_id`` /
  ``fetch_deck`` / ``to_deck_json`` feed ``moxfield_import.import_deck``'s
  ``source="archidekt"`` path, which then runs the same render, dedupe,
  destination and metadata-merge code as a Moxfield import.
- **Commander-keyed reference decks** — partial. ``fetch_top_decks``
  ranks by ``viewCount`` (Archidekt exposes no like count) and can only
  SOFT-filter bracket, because ``edhBracket`` is null on most decks.
- **Bulk harvest by bracket** — NO equivalent. Archidekt's search has no
  reliable bracket axis to page through, so the opponent-pool harvest
  stays Moxfield-only.
- **Top-likes search** — NO equivalent (no like count in the API).
"""

from __future__ import annotations

import json
import re
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


def _excluded_categories(deck_json: dict) -> set[str]:
    """Lowercased category names whose cards are NOT in the playable deck.

    Archidekt's board semantics live in the deck's own category list, and
    the ``includedInDeck`` FLAG is the whole rule. Do not shortcut it by
    name: in the real capture (R2-P18, deck 24864897, 2026-08-20) the
    ``Sideboard`` category carries ``includedInDeck: true`` while
    ``Maybeboard`` carries false — the flag is per-deck user state, so a
    name-based rule would silently import that deck's sideboard.
    """
    excluded: set[str] = set()
    for cat in deck_json.get("categories") or []:
        if isinstance(cat, dict) and cat.get("includedInDeck") is False:
            name = (cat.get("name") or "").strip().lower()
            if name:
                excluded.add(name)
    return excluded


def _split_boards(deck_json: dict) -> tuple[list[dict], list[dict]]:
    """``(commander_entries, mainboard_entries)`` from a detail JSON.

    ONE walk, ONE notion of "counts as part of the deck", shared by the
    corpus reader (:func:`extract_mainboard`) and the single-deck importer
    (:func:`to_deck_json`) — the two used to be able to disagree about a
    Maybeboard card, which would have meant the imported ``.dck`` and the
    reference corpus read the same URL differently.

    An entry counts iff ``quantity`` > 0 and none of its categories is
    excluded at the deck level. An entry in the ``Commander`` category is
    a commander; everything else that counts is mainboard. A card with NO
    categories is mainboard (Archidekt leaves categories empty for plain
    entries).
    """
    excluded = _excluded_categories(deck_json)
    commanders: list[dict] = []
    mainboard: list[dict] = []
    for entry in deck_json.get("cards") or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("quantity"):
            continue
        cats = [str(c).strip().lower()
                for c in (entry.get("categories") or [])]
        if any(c in excluded for c in cats):
            continue
        if "commander" in cats:
            commanders.append(entry)
        else:
            mainboard.append(entry)
    return commanders, mainboard


def _entry_name(entry: dict) -> str:
    return (((entry.get("card") or {}).get("oracleCard") or {})
            .get("name") or "").strip()


def extract_mainboard(deck_json: dict) -> list[str]:
    """Pull the playable-deck card names out of an Archidekt detail JSON.

    Commanders are excluded — the corpus wants the 99; the commander is
    the query key, not a data point. See :func:`_split_boards` for the
    membership rules.

    ONE NAME PER ENTRY, not per copy: the real capture's 81 in-deck
    entries sum to 99 cards (11x Forest, 9x Swamp), and this returns 81
    names. Deliberate — ``bubble_analysis`` folds each list into a
    ``frozenset`` of card keys, so duplicates would be discarded anyway.
    A caller that needs deck SIZE must not count this list (R2-P18,
    2026-08-20).
    """
    _, mainboard = _split_boards(deck_json)
    return [n for n in (_entry_name(e) for e in mainboard) if n]


# ---------------------------------------------------------------------------
# Single-deck import lane (decision C3, 2026-08-17)
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS: essentially all deck acquisition rides
# ``api2.moxfield.com`` — an UNDOCUMENTED private API. One ToS change, one
# CDN rule or one schema rename strands single-deck import, bracket-peer
# references and the meta-test reference pull at the same moment, with no
# second lane. Archidekt's API is public and documented, so this module
# becomes the fallback lane for the one capability a user cannot work
# around by hand: getting THEIR deck into Forge.
#
# Scope is deliberately the CLIENT layer only. There is no way to map a
# Moxfield deck id onto an Archidekt one (different sites, different decks,
# no shared identifier), so this is not an automatic rescue of a Moxfield
# URL — it is a swappable source the user points at their Archidekt copy.

#: Deck ids in an Archidekt URL are numeric:
#: ``archidekt.com/decks/1234567/my-deck-slug``. The slug is decoration.
_DECK_URL_RE = re.compile(r"/decks/(\d+)")


def is_archidekt_url(url_or_id: str) -> bool:
    """True when the string is recognizably an Archidekt deck URL.

    Host match only — a BARE numeric id is deliberately not claimed here,
    because a bare id is exactly what a Moxfield-shaped call site passes.
    Source selection is the caller's decision; this only answers "is this
    unambiguously Archidekt".
    """
    return "archidekt.com" in (url_or_id or "").lower()


def parse_deck_id(url_or_id: str) -> str:
    """Accept a full Archidekt URL or a bare deck id; return the id.

    Mirrors ``moxfield_import.parse_deck_id`` so the two importer lanes
    take the same shape of user input.
    """
    m = _DECK_URL_RE.search(url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def fetch_deck(
    deck_id: str,
    *,
    fetch_json: Optional[Callable[[str], dict]] = None,
) -> dict:
    """Fetch one deck's detail JSON. RAISES on failure, unlike the corpus
    functions above.

    The degrade-don't-die contract belongs to bulk corpus reads, where one
    dead deck out of 25 is noise. A single-deck import has no such
    redundancy: swallowing the error here would write an empty ``.dck``
    and call it success, which is exactly the silent failure the project's
    working principles forbid. Retries still apply (429/5xx).

    Raises :class:`ValueError` when the payload sets
    ``intentionallySkippedCardData``: a 200 with the card data omitted.

    WHY (R2-P18, 2026-08-20): the real capture
    (``tests/fixtures/archidekt_deck_shape.json``) carries this key —
    false there, so the deck was complete — which is the API stating out
    loud that it CAN return a deck whose card data it deliberately left
    out. Down that path every entry loses its ``oracleCard.name``,
    :func:`to_deck_json` drops every nameless entry, and the importer
    writes a 0-card ``.dck`` and prints "Wrote ... (0 commander + 0
    main)" as if it had worked. The exact silent success this function's
    raise-don't-degrade contract exists to prevent, so it is checked here
    rather than left to the caller. The corpus lane is untouched: it
    calls ``_get_json_with_retry`` directly and a skipped-data deck just
    yields no names, which is a degrade, not a lie.
    """
    get = fetch_json or _http_get_json
    payload = _get_json_with_retry(
        get, f"{BASE}/decks/{parse_deck_id(deck_id)}/")
    if isinstance(payload, dict) and payload.get(
            "intentionallySkippedCardData"):
        raise ValueError(
            f"Archidekt returned deck {parse_deck_id(deck_id)} with "
            f"intentionallySkippedCardData=true — the response omits the "
            f"card data, so importing it would write an empty deck. "
            f"Retry, or export the deck from Archidekt by hand."
        )
    return payload


def deck_bracket(deck_json: dict) -> int:
    """Archidekt's ``edhBracket`` as a 1-5 int, or 0 for unknown.

    Most decks leave it null. 0 is the same "unknown" value
    ``moxfield_import.resolve_bracket`` returns, so the filename tag comes
    out as ``[B?]`` and every bracket-aware consumer treats the deck the
    same way it treats an unbracketed Moxfield import.
    """
    value = deck_json.get("edhBracket")
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= 5 else 0


def to_deck_json(deck_json: dict) -> dict:
    """Adapt an Archidekt detail JSON to the project's internal deck shape.

    That internal shape happens to be Moxfield's response shape — not
    because Moxfield is privileged, but because it is what
    ``moxfield_import.to_dck`` / ``card_line`` / ``deck_destination``
    already consume, and ``premade_import`` already treats it as the
    interchange format between sources ("Moxfield-shape deck JSON"). One
    adapter here is the whole cost of making the importer source-agnostic;
    the alternative — a second renderer — would fork the ``.dck`` writer,
    and with it the C0-control-character sanitizing and the printing
    (set/collector-number) handling.

    ``publicId`` is deliberately NOT set: an Archidekt id is not a
    Moxfield publicId and stamping one into the ``Moxfield=`` line would
    poison the re-import dedupe index. The importer records provenance as
    ``Archidekt=<id>`` / ``Source=archidekt`` instead.
    """
    commanders, mainboard = _split_boards(deck_json)

    def _mox_entry(entry: dict) -> dict:
        card = entry.get("card") or {}
        edition = card.get("edition") or {}
        return {
            "quantity": entry.get("quantity", 1),
            "card": {
                "name": _entry_name(entry),
                "set": (edition.get("editioncode") or "").strip(),
                "cn": str(card.get("collectorNumber") or "").strip(),
            },
        }

    def _board(entries: list[dict]) -> dict:
        return {"cards": {
            f"{i}": _mox_entry(e) for i, e in enumerate(entries)
            if _entry_name(e)
        }}

    # An entry with no ``oracleCard.name`` is unrenderable and gets
    # dropped above. Say so — WHY (R2-P18, 2026-08-20): the real capture
    # has a ``customCards`` list (empty for this deck) and an
    # ``intentionallySkippedCardData`` flag, i.e. two documented ways for
    # the API to hand back entries this adapter cannot name. Dropping
    # them quietly turns "we lost 12 cards" into a deck that merely looks
    # short, which no downstream consumer can tell from a 76-card
    # brew. One stderr line, never an exception: the import still
    # produces the cards it CAN name, and the user gets told what is
    # missing. ``fetch_deck`` owns the all-or-nothing case.
    counted = commanders + mainboard
    nameless = sum(1 for e in counted if not _entry_name(e))
    if nameless:
        print(
            f"[archidekt] WARN: {nameless} of {len(counted)} in-deck "
            f"entries have no oracleCard name and were dropped — the "
            f"imported deck is incomplete (custom cards, or a response "
            f"with card data omitted).",
            file=sys.stderr, flush=True,
        )

    return {
        "name": (deck_json.get("name") or "").strip() or "Untitled",
        "format": "commander",
        # ``resolve_bracket`` reads this key first; 0 stays unset so it
        # falls through to its own "unknown" default rather than being
        # handed an out-of-range int.
        **({"bracket": deck_bracket(deck_json)}
           if deck_bracket(deck_json) else {}),
        "boards": {
            "commanders": _board(commanders),
            "mainboard": _board(mainboard),
        },
    }


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
