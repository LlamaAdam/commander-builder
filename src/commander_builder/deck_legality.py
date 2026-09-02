"""Construction-legality validation for Commander decks.

Until now nothing in this project could answer the question a deck
builder actually asks first: *is this deck legal?* Legality was only
ever enforced incidentally, as a side effect of other features —

  - ``web/deck_text_ops._apply_swaps_to_dck`` keeps a proposal from
    writing a duplicate non-basic or changing the mainboard size,
  - ``_proposer_filters.enforce_color_identity`` strips off-color
    curator adds,
  - ``dck_utils.main_target`` knows 100 - commanders,

which means a deck the user hand-edited, imported from Moxfield, or
pasted into the web UI was never checked at all. Forge's own loader
rejects such decks with an opaque error hours later, at simulation
time. This module is the single place that answers the question up
front, for a whole ``.dck`` blob, with a machine-readable reason.

Reuse, not reimplementation
---------------------------
Every primitive here already existed and is imported rather than
copied: ``dck_utils`` for ``.dck`` parsing (both section iteration and
the canonical ``<qty> <Name>|SET|CN`` regex),
``deck_library_analyzer.iter_deck_cards`` for the combined
[Commander] + [Main] walk (``deck_health`` reads only [Main]; the
commander is load-bearing here), and ``deck_text_ops``'s
``_is_basic_land_name`` / ``_dck_name_key`` for the basic-land
multiples exemption and the DFC ``A // B`` vs ``A`` name fold. The
import direction (core importing from ``web/``) is deliberate: those
two helpers are pure text predicates with no Flask dependency, and
duplicating them is exactly how the two slightly-divergent .dck line
regexes documented in ``dck_utils`` came to exist in the first place.

Scryfall is the source of truth
-------------------------------
Four of the seven checks need card data, and for all four Scryfall's
own fields are authoritative — we never re-derive what Scryfall
already computed:

  - ``legalities.commander``  → banned / not-in-format. Replaces the
    hand-typed ban list that used to live in ``web/routes_decks``,
    which was wrong in BOTH directions after the 2026-02-09 B&R
    update (it listed Coalition Victory and Panoptic Mirror — both
    are *Game Changers*, i.e. legal — while missing Fastbond,
    Griselbrand, Paradox Engine and seven others).
  - ``color_identity``        → color-identity subset. CRITICAL:
    color identity includes mana symbols in RULES TEXT, not just the
    mana cost (Kenrith's abilities make him WUBRG on a colorless-cost
    body). Scryfall's field already accounts for that, so any
    re-derivation from ``mana_cost`` would be silently wrong.
  - ``type_line``             → commander eligibility, Background /
    Time Lord Doctor pairing.
  - ``oracle_text``           → "can be your commander", the partner
    keywords, and the "A deck can have any number of cards named"
    singleton exemption.

Outage contract
---------------
Same contract as ``deck_health``: unavailable returns unknown, never
a fabricated verdict. A Scryfall outage must never manufacture a
"this card is banned" or "this card is off-color" claim, because the
whole point of the report is that a user acts on it. Concretely:

  - a per-card lookup that fails (network, 404, corrupt cache, or a
    cached snapshot that predates the field we need) makes that
    card's check UNVERIFIED, not violated;
  - unverified findings land in ``LegalityReport.unverified``, a
    list kept strictly separate from ``violations`` — the two are
    never conflated;
  - ``scan_banned`` returns ``None`` outright when MORE than half the
    names couldn't be resolved, mirroring deck_health's
    ``if lines and failed_lines * 2 > lines: return None``
    majority-failure guard;
  - ``LegalityReport.legal`` is therefore "no CONFIRMED violation",
    and callers that need the three-state answer read ``status``
    (``"legal"`` / ``"illegal"`` / ``"unverified"``).

Data freshness
--------------
``scryfall_client.lookup_card`` serves disk snapshots FOREVER — there
is no TTL, so a card banned after its snapshot was written keeps
reading "legal" until someone refreshes the store, and WotC now runs
seven B&R windows a year. Rather than adding refetch storms over the
~32k-snapshot store, every ``validate_deck`` report carries the age of
the OLDEST snapshot backing its verdict (file mtime, the same
snapshot-age convention as ``oracle_store.snapshot_age_days`` /
``BULK_FRESH_DAYS``) and a ``data_warning`` string once that age
crosses ``STALE_SNAPSHOT_DAYS`` — "legality data as of <date>, N days
old; run ``commander-oracle-refresh --from-bulk``". The warning is
informational only: it never flips ``legal``/``status``, because stale
data is a reason to refresh, not evidence of a violation.

Documented limitations
----------------------
  - **Companions.** The ``.dck`` format has no companion slot, so a
    companion is indistinguishable from the 99. Lutri, the
    Spellchaser was unbanned as a deck card on 2026-02-09 but remains
    banned AS A COMPANION — the one carve-out Scryfall's
    ``legalities.commander`` cannot express (it reads "legal", which
    is correct for the maindeck). Lutri in a decklist is therefore
    reported as unverified, never as a violation.
  - **Partner with <name>.** Verified when both commanders resolve;
    an unresolvable commander makes the pairing unverified.
  - **Attractions / stickers / other sideboard-ish zones** are not
    modelled; only [Commander] and [Main] are read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Callable, Iterable, Optional

from . import dck_utils
from .deck_library_analyzer import iter_deck_cards
from .web.deck_text_ops import _dck_name_key, _is_basic_land_name


# A legal Commander deck is exactly 100 cards: command zone + library.
# The split between the two is NOT fixed (99+1 single, 98+2 partners),
# which is why we check the TOTAL and let the commander-count check
# police the split. See dck_utils.COMMANDER_DECK_SIZE.
DECK_SIZE = dck_utils.COMMANDER_DECK_SIZE

#: Oldest-backing-snapshot age (days) beyond which a legality report
#: carries a staleness warning. ~45 days spans roughly one B&R window
#: at WotC's current seven-windows-a-year cadence, so a store older
#: than this has plausibly missed an update. A CONSTANT, not a config
#: knob: the number is a documented judgment call, and every consumer
#: (validate_deck, doctor) should agree on it.
STALE_SNAPSHOT_DAYS = 45.0


# ---------------------------------------------------------------------------
# Violation codes
# ---------------------------------------------------------------------------
#
# Machine-readable and stable: the web layer keys off these, so treat
# them as API. Human-facing wording lives in ``Violation.message``.

CODE_DECK_SIZE = "DECK_SIZE"
CODE_MALFORMED_CARD_LINE = "MALFORMED_CARD_LINE"
CODE_COMMANDER_MISSING = "COMMANDER_MISSING"
CODE_COMMANDER_COUNT = "COMMANDER_COUNT"
CODE_COMMANDER_INELIGIBLE = "COMMANDER_INELIGIBLE"
CODE_COMMANDER_PAIR = "COMMANDER_PAIR"
CODE_DUPLICATE_CARD = "DUPLICATE_CARD"
CODE_COPY_LIMIT = "COPY_LIMIT"
CODE_COLOR_IDENTITY = "COLOR_IDENTITY"
CODE_BANNED_CARD = "BANNED_CARD"
CODE_NOT_IN_FORMAT = "NOT_IN_FORMAT"

# Unverified codes — these never make a deck illegal. They say "this
# check could not run", which is a different statement from "this
# check failed".
CODE_UNVERIFIED_COMMANDER = "UNVERIFIED_COMMANDER"
CODE_UNVERIFIED_PAIR = "UNVERIFIED_PAIR"
CODE_UNVERIFIED_SINGLETON = "UNVERIFIED_SINGLETON"
CODE_UNVERIFIED_COLOR_IDENTITY = "UNVERIFIED_COLOR_IDENTITY"
CODE_UNVERIFIED_BANNED = "UNVERIFIED_BANNED"
CODE_LUTRI_COMPANION = "LUTRI_COMPANION"


# ---------------------------------------------------------------------------
# The Lutri overlay — the ONLY hand-maintained legality data in this module
# ---------------------------------------------------------------------------
#
# The 2026-02-09 Banned & Restricted update unbanned Lutri, the
# Spellchaser as a DECK CARD while leaving it banned as a COMPANION.
# Scryfall models one legality per card per format, and for Lutri that
# value is "legal" — accurate for the maindeck, silent about the
# companion zone. Since a .dck file has no companion slot we cannot
# tell which zone the user intends, so we surface the distinction
# instead of guessing. Keep this dict to carve-outs Scryfall
# *structurally* cannot express; anything Scryfall can answer belongs
# in scan_banned, not here.
_COMPANION_BANNED = {
    "lutri, the spellchaser": (
        "Lutri, the Spellchaser is legal in the 99 (unbanned 2026-02-09) "
        "but is BANNED as a companion. A .dck file has no companion slot, "
        "so this cannot be verified from the deck text."
    ),
}


# ---------------------------------------------------------------------------
# Report types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    """One legality finding.

    ``code`` is one of the ``CODE_*`` constants above (stable, keyed
    on by the UI). ``message`` is the human sentence. ``cards`` names
    the offending cards in deck casing — possibly empty for deck-wide
    findings like ``DECK_SIZE``.
    """
    code: str
    message: str
    cards: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "cards": list(self.cards),
        }


@dataclass(frozen=True)
class LegalityReport:
    """The result of ``validate_deck``.

    ``legal`` is True when NO confirmed violation was found. It is
    deliberately not a three-state value — callers that only want a
    gate can read it directly, and callers that must distinguish
    "we checked and it's fine" from "we couldn't check" read
    ``status`` / ``verified``. An outage yields ``legal=True`` with a
    populated ``unverified`` list, never ``legal=False``: we do not
    fail a deck on data we don't have.

    ``data_age_days`` is the age of the OLDEST disk snapshot backing
    the verdict (None when nothing on disk backs it — injected-lookup
    test runs, empty store), and ``data_warning`` is the human
    staleness sentence once that age crosses ``STALE_SNAPSHOT_DAYS``.
    Both are informational: they never affect ``legal``/``status``.
    """
    legal: bool
    violations: tuple[Violation, ...] = ()
    unverified: tuple[Violation, ...] = ()
    card_count: int = 0
    commander_count: int = 0
    lookup_failures: int = 0
    data_age_days: Optional[float] = None
    data_warning: Optional[str] = None

    @property
    def verified(self) -> bool:
        """True when every check actually ran (nothing unverified)."""
        return not self.unverified

    @property
    def status(self) -> str:
        """Three-state answer: ``illegal`` > ``unverified`` > ``legal``.

        A confirmed violation wins over an unverified check: the deck
        is illegal regardless of what the un-run checks would have
        said.
        """
        if not self.legal:
            return "illegal"
        if self.unverified:
            return "unverified"
        return "legal"

    def codes(self) -> list[str]:
        """Violation codes in report order — assertion ergonomics."""
        return [v.code for v in self.violations]

    def to_dict(self) -> dict:
        """JSON-friendly projection for the web layer."""
        return {
            "legal": self.legal,
            "status": self.status,
            "verified": self.verified,
            "card_count": self.card_count,
            "commander_count": self.commander_count,
            "lookup_failures": self.lookup_failures,
            "violations": [v.to_dict() for v in self.violations],
            "unverified": [v.to_dict() for v in self.unverified],
            "data_age_days": self.data_age_days,
            "data_warning": self.data_warning,
        }


@dataclass(frozen=True)
class BanScan:
    """Outcome of a Scryfall-backed ban sweep over a name list.

    Three buckets, never merged: ``banned`` (``legalities.commander ==
    "banned"``), ``not_in_format`` (``"not_legal"`` — Un-set cards,
    Conspiracies, oversized promos; illegal but for a different reason
    a user fixes differently), and ``unverified`` (no legality string
    available for that name). ``checked`` is the number of distinct
    names attempted.
    """
    banned: tuple[str, ...] = ()
    not_in_format: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    checked: int = 0

    @property
    def outage(self) -> bool:
        """True when MORE than half the names couldn't be resolved.

        Same threshold as deck_health's ``failed_lines * 2 > lines``:
        a single typo or custom card can't trip it, a dead Scryfall
        always does.
        """
        return bool(self.checked) and len(self.unverified) * 2 > self.checked

    def to_dict(self) -> dict:
        return {
            "banned": list(self.banned),
            "not_in_format": list(self.not_in_format),
            "unverified": list(self.unverified),
            "checked": self.checked,
        }


# ---------------------------------------------------------------------------
# Snapshot freshness
# ---------------------------------------------------------------------------

def snapshot_staleness(
    names: Iterable[str],
    *,
    threshold_days: float = STALE_SNAPSHOT_DAYS,
) -> tuple[Optional[float], Optional[str]]:
    """``(oldest_age_days, warning)`` for the disk snapshots behind
    ``names``.

    Reads the shared oracle-snapshot store via
    ``oracle_store.snapshot_age_days`` (file mtime — the repo's
    existing snapshot-age convention). Names with no snapshot are
    simply not counted: "never cached" is a resolution problem the
    unverified buckets already report, not a freshness problem. The
    OLDEST age is the honest one to surface — a single ancient
    snapshot is exactly where a post-snapshot banning hides.

    Returns ``(None, None)`` when nothing on disk backs the names (or
    the store can't be read at all). ``warning`` is None below
    ``threshold_days`` and the "legality data as of <date>, N days
    old; run refresh" sentence at/above it. Never raises.
    """
    try:
        from .oracle_store import snapshot_age_days
    except Exception:  # noqa: BLE001 — freshness is a bonus, never a blocker
        return None, None
    ages: list[float] = []
    seen: set[str] = set()
    for name in names:
        key = _dck_name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            age = snapshot_age_days(name)
        except Exception:  # noqa: BLE001 — one bad stat must not sink the scan
            age = None
        if age is not None:
            ages.append(age)
    if not ages:
        return None, None
    oldest = max(ages)
    if oldest < threshold_days:
        return oldest, None
    as_of = datetime.now(timezone.utc) - timedelta(days=oldest)
    return oldest, (
        f"Legality data as of {as_of:%Y-%m-%d} — the oldest card "
        f"snapshot backing this verdict is {oldest:.0f} days old "
        f"(warning threshold {threshold_days:g}). Bans from newer B&R "
        f"windows may be invisible; run "
        f"`commander-oracle-refresh --from-bulk` to refresh."
    )


# ---------------------------------------------------------------------------
# Card lookup plumbing
# ---------------------------------------------------------------------------

LookupFn = Callable[[str], Optional[dict]]


class _Cards:
    """Memoizing, never-raising wrapper around a card lookup function.

    Mirrors ``deck_health._lookup_card_safe`` (a network blip on one
    card must not poison the whole computation) plus per-name memoing,
    because a deck asks about the same name from four different checks
    and ``scryfall_client.lookup_card``'s disk cache is still a file
    read each time.

    ``fn`` defaults to ``scryfall_client.lookup_card``, imported
    lazily: that module writes to a disk cache on import-time-
    resolvable paths, and tests inject a stub instead of monkeypatching
    a module global.

    Network circuit breaker: the first HARD network failure (timeout /
    connection refused / 5xx — anything ``OSError``-shaped, which
    covers ``urllib.error.URLError``) trips the scan to cache-only for
    its REMAINDER: the default lookup drains the rest of the deck from
    disk snapshots via ``lookup_card(cache_only=True)``, and an
    injected lookup simply stops being called. Without this, a
    Scryfall outage costs one ~20s connect-timeout per distinct name —
    a 100-card deck scanned inside a synchronous web request stalls
    for over half an hour. Same shape as ``deck_pricing``'s printings
    breaker, including the retry-THIS-card-cache-only step so a
    mid-scan outage doesn't skip a card whose snapshot already exists.
    The breaker is per-``_Cards`` (i.e. per scan): one dead-network
    scan never poisons the next one.
    """

    def __init__(self, fn: Optional[LookupFn] = None) -> None:
        self._fn = fn
        self._cache: dict[str, Optional[dict]] = {}
        self.attempts = 0
        self.failures = 0
        self._offline = False  # Tripped by the first hard network failure.

    def get(self, name: str) -> Optional[dict]:
        """Card dict for ``name``, or None if it can't be resolved.

        Failures are counted once per DISTINCT name (a 27-Forest deck
        line is one lookup, not 27), which is what the majority-
        failure guards downstream want to reason about.
        """
        key = _dck_name_key(name)
        if key in self._cache:
            return self._cache[key]
        self.attempts += 1
        card = self._lookup(name)
        if not isinstance(card, dict):
            card = None
        if card is None:
            self.failures += 1
        self._cache[key] = card
        return card

    def _lookup(self, name: str) -> Optional[dict]:
        """One breaker-aware lookup attempt. Never raises."""
        fn = self._fn
        snapshot: Optional[LookupFn] = None
        if fn is None:
            from .scryfall_client import lookup_card
            fn = lookup_card
            snapshot = partial(lookup_card, cache_only=True)
        if not self._offline:
            try:
                return fn(name)
            except OSError:
                # Hard network failure: trip cache-only for the rest
                # of this scan, then fall through to retry THIS card
                # from the snapshot (mirrors deck_pricing).
                self._offline = True
            except Exception:  # noqa: BLE001 -- outage contract: unknown, not illegal
                return None
        if snapshot is None:
            # Injected lookups have no disk snapshot behind them; a
            # tripped breaker just stops calling them.
            return None
        try:
            return snapshot(name)
        except Exception:  # noqa: BLE001 -- outage contract: unknown, not illegal
            return None


def _faces(card: dict) -> list[dict]:
    """The card plus each of its faces, for field lookups.

    Split / MDFC / transform cards carry ``oracle_text`` and
    ``type_line`` per face and sometimes omit them on the parent, so
    every text predicate here scans parent + faces rather than
    assuming the flat single-face shape.
    """
    out = [card]
    faces = card.get("card_faces")
    if isinstance(faces, list):
        out.extend(f for f in faces if isinstance(f, dict))
    return out


def _oracle_text(card: Optional[dict]) -> str:
    """All oracle text on the card, faces included, lowercased.

    Apostrophes are normalized to ASCII because Scryfall ships curly
    quotes ("Doctor's companion") while hand-typed predicates use
    straight ones — the same normalization
    ``forge_cards_loader._APOSTROPHE_RE`` applies to slugs.
    """
    if not card:
        return ""
    parts = [
        f.get("oracle_text") or "" for f in _faces(card)
    ]
    text = "\n".join(p for p in parts if p).lower()
    return text.replace("’", "'").replace("‘", "'")


def _type_line(card: Optional[dict]) -> str:
    """All type lines on the card, faces included, lowercased."""
    if not card:
        return ""
    parts = [f.get("type_line") or "" for f in _faces(card)]
    return " // ".join(p for p in parts if p).lower()


def _color_identity(card: Optional[dict]) -> Optional[set[str]]:
    """Scryfall's ``color_identity`` as an upper-case letter set.

    Returns None when the field is absent (unknown — an old projected
    cache snapshot), which is NOT the same as the empty set (a
    genuinely colorless card like Kozilek). Conflating the two is how
    a color check turns an outage into "everything is off-color".
    """
    if not card:
        return None
    ci = card.get("color_identity")
    if not isinstance(ci, list):
        return None
    return {c.upper() for c in ci if isinstance(c, str)}


def commander_legality(card: Optional[dict]) -> Optional[str]:
    """``legalities.commander`` for a card dict, lowercased.

    Returns None when the card is missing OR the projection lacks the
    field. ``scryfall_client`` caches full Scryfall payloads (and
    explicitly keeps ``{"legalities": {"commander": ...}}`` in its
    trimmed printing projection), but shared ``oracle_snapshots``
    written by sibling projects can be slimmer — an absent field is
    "unknown", never "legal".
    """
    if not card:
        return None
    legalities = card.get("legalities")
    if not isinstance(legalities, dict):
        return None
    value = legalities.get("commander")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


# ---------------------------------------------------------------------------
# Banned-card sweep (shared by validate_deck and web/routes_decks)
# ---------------------------------------------------------------------------

def _scan_banned(names: Iterable[str], cards: _Cards) -> BanScan:
    """Bucket ``names`` by ``legalities.commander``. Never returns None.

    Internal so ``validate_deck`` can see the unverified names even
    during an outage; the public ``scan_banned`` applies the
    majority-failure guard on top.
    """
    banned: list[str] = []
    not_in_format: list[str] = []
    unverified: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = _dck_name_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        legality = commander_legality(cards.get(name))
        if legality is None:
            unverified.append(name)
        elif legality == "banned":
            banned.append(name)
        elif legality == "not_legal":
            not_in_format.append(name)
    return BanScan(
        banned=tuple(sorted(banned)),
        not_in_format=tuple(sorted(not_in_format)),
        unverified=tuple(sorted(unverified)),
        checked=len(seen),
    )


def scan_banned(
    names: Iterable[str], *, lookup: Optional[LookupFn] = None,
) -> Optional[BanScan]:
    """Ban sweep over ``names``, or None when Scryfall is unusable.

    The authoritative check for "is this card banned in Commander?".
    It reads ``legalities.commander`` from the disk-cached Scryfall
    payload — no hand-maintained list is involved, which is the whole
    point: the hardcoded set this replaced had drifted six cards in
    one direction and ten in the other within a single B&R cycle.

    Returns None when MORE than half the distinct names couldn't be
    resolved, matching deck_health's outage contract — a caller must
    not render "0 banned cards" (or, worse, a partial list) off a dead
    Scryfall. Below that threshold the scan is returned with the
    unresolvable names quarantined in ``BanScan.unverified`` so the
    caller can say how much of the deck it actually vouched for.

    Note this reports the MAINDECK legality only; see
    ``_COMPANION_BANNED`` for the one carve-out Scryfall can't model.
    """
    scan = _scan_banned(names, _Cards(lookup))
    if scan.outage:
        return None
    return scan


# ---------------------------------------------------------------------------
# Singleton / copy limits
# ---------------------------------------------------------------------------

# "A deck can have any number of cards named Relentless Rats."
_ANY_NUMBER_RE = re.compile(
    r"a deck can have any number of cards named", re.IGNORECASE,
)
# "A deck can have up to nine cards named Nazgûl." — the Lord of the
# Rings printing is the only card in Magic with a FINITE cap above 1,
# but the wording is templated, so parse the number rather than
# special-casing the name (a future printing gets this for free; a
# name check would silently allow 20 Nazgûl).
_UP_TO_RE = re.compile(
    r"a deck can have up to (\w+) cards named", re.IGNORECASE,
)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}


def copy_limit(card: Optional[dict]) -> Optional[int]:
    """Legal number of copies of ``card`` in one deck.

    ``1`` is the singleton default, ``None`` means unlimited
    (Relentless Rats / Persistent Petitioners / Dragon's Approach /
    Shadowborn Apostle and friends), and an integer above 1 is a
    finite cap (Nazgûl: 9).

    Basic lands are handled by the CALLER via
    ``deck_text_ops._is_basic_land_name`` — their exemption comes from
    the supertype, not from oracle text, and checking the name means
    the common ``35 Forest`` case needs no lookup and survives an
    outage.
    """
    text = _oracle_text(card)
    if not text:
        return 1
    if _ANY_NUMBER_RE.search(text):
        return None
    m = _UP_TO_RE.search(text)
    if m:
        token = m.group(1).lower()
        if token.isdigit():
            return int(token)
        if token in _NUMBER_WORDS:
            return _NUMBER_WORDS[token]
        # Templated wording we can't parse: treat as unlimited rather
        # than as singleton. The card explicitly grants multiples, so
        # claiming a duplicate violation would be the fabricated
        # verdict the outage contract forbids.
        return None
    return 1


# ---------------------------------------------------------------------------
# Commander eligibility + partner pairings
# ---------------------------------------------------------------------------

_PARTNER_WITH_RE = re.compile(r"partner with ([^(\n]+)", re.IGNORECASE)
# Bare "Partner" is its own keyword LINE ("Partner (You can have two
# commanders...)"), so anchor to line start — otherwise "Partner with
# Thrasios" and "Choose a Background" prose would both match.
_BARE_PARTNER_RE = re.compile(r"^partner(?:\s*\(|\s*$)", re.IGNORECASE | re.MULTILINE)


def is_eligible_commander(card: Optional[dict]) -> Optional[bool]:
    """Can ``card`` be a commander? None when it can't be determined.

    Rule 903.3: a legendary creature, or any card whose text says it
    "can be your commander" (Planeswalker commanders like Rowan/Will,
    and the Backgrounds/Doctor's-companion-adjacent designs). Both
    signals are read across all faces so a transforming legend
    (Delina) or an MDFC legend qualifies on its front face.
    """
    if card is None:
        return None
    type_line = _type_line(card)
    oracle = _oracle_text(card)
    if not type_line and not oracle:
        return None
    if "legendary" in type_line and "creature" in type_line:
        return True
    if "can be your commander" in oracle:
        return True
    return False


def _partner_traits(card: Optional[dict]) -> dict:
    """Pairing-relevant keywords/types for one candidate commander."""
    oracle = _oracle_text(card)
    type_line = _type_line(card)
    partner_with = _PARTNER_WITH_RE.search(oracle)
    return {
        "partner": bool(_BARE_PARTNER_RE.search(oracle)),
        "partner_with": (
            partner_with.group(1).strip().rstrip(".") if partner_with else None
        ),
        "friends_forever": "friends forever" in oracle,
        "doctors_companion": "doctor's companion" in oracle,
        "choose_background": "choose a background" in oracle,
        "background": "background" in type_line,
        "doctor": "time lord" in type_line and "doctor" in type_line,
    }


def _pair_is_legal(name_a: str, card_a: dict, name_b: str, card_b: dict) -> bool:
    """True when two cards may legally share the command zone.

    The five sanctioned pairings, all symmetric:
      - both have bare ``Partner``;
      - ``Partner with <name>`` naming the other card;
      - both have ``Friends forever``;
      - ``Choose a Background`` + a Background enchantment;
      - ``Doctor's companion`` + a Time Lord Doctor.
    """
    a = _partner_traits(card_a)
    b = _partner_traits(card_b)
    if a["partner"] and b["partner"]:
        return True
    key_a, key_b = _dck_name_key(name_a), _dck_name_key(name_b)
    if a["partner_with"] and _dck_name_key(a["partner_with"]) == key_b:
        return True
    if b["partner_with"] and _dck_name_key(b["partner_with"]) == key_a:
        return True
    if a["friends_forever"] and b["friends_forever"]:
        return True
    if (a["choose_background"] and b["background"]) or (
        b["choose_background"] and a["background"]
    ):
        return True
    if (a["doctors_companion"] and b["doctor"]) or (
        b["doctors_companion"] and a["doctor"]
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Deck walking
# ---------------------------------------------------------------------------

def _iter_commander_cards(deck_text: str) -> list[tuple[int, str]]:
    """``(qty, name)`` for each [Commander] line.

    ``dck_utils`` owns the section iteration and the line regex;
    ``count_commander_cards`` sums the same lines, so this is the
    missing per-card view of a walk that already exists rather than a
    second parser.
    """
    out: list[tuple[int, str]] = []
    for line in dck_utils.iter_section_lines(deck_text, "Commander"):
        parsed = dck_utils.parse_card_line(line)
        if parsed is None:
            continue
        qty, name = parsed
        if name:
            out.append((qty, name))
    return out


def _deck_quantities(deck_text: str) -> tuple[dict[str, int], dict[str, str]]:
    """Fold [Commander] + [Main] into ``{name_key: qty}`` + display names.

    Uses ``deck_library_analyzer.iter_deck_cards`` (the only existing
    walker covering BOTH sections — ``deck_health`` reads [Main] only,
    and the commander must be counted for both the 100-card total and
    the singleton rule). Names are folded via ``_dck_name_key`` so a
    deck listing ``Malakir Rebirth`` in [Main] and ``Malakir Rebirth //
    Malakir Mire`` in a sideboard-style paste can't dodge the check.
    """
    quantities: dict[str, int] = {}
    display: dict[str, str] = {}
    for qty, name in iter_deck_cards(deck_text):
        key = _dck_name_key(name)
        if not key:
            continue
        quantities[key] = quantities.get(key, 0) + qty
        display.setdefault(key, name)
    return quantities, display


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def _check_card_line_syntax(deck_text: str, violations: list[Violation]) -> None:
    """Reject active-zone entries the permissive card iterators skip.

    Use the canonical Forge parser so printing suffixes and foil markers
    remain supported. Blanks, comments, metadata, and other sections are
    not card entries; an unparseable active-zone line is not evidence that
    the remaining, successfully parsed cards form the entire deck.
    """
    for section in ("Commander", "Main"):
        for line in dck_utils.iter_section_lines(deck_text, section):
            if line.startswith(("#", "//", ";")):
                continue
            parsed = dck_utils.parse_card_line(line)
            if parsed is None or parsed[0] <= 0 or not parsed[1]:
                violations.append(Violation(
                    code=CODE_MALFORMED_CARD_LINE,
                    message=(
                        f"Malformed card line in [{section}]: {line!r}. "
                        "Expected a positive quantity and a card name."
                    ),
                ))


def _check_deck_size(
    deck_text: str, violations: list[Violation],
) -> tuple[int, int]:
    """Exactly 100 cards across [Commander] + [Main]. Returns the counts.

    Checks the TOTAL rather than "99 in [Main]": a partner pair is a
    legal 98 + 2, and hardcoding 99 is the exact bug
    ``dck_utils.main_target`` was introduced to fix.
    """
    commanders = dck_utils.count_commander_cards(deck_text)
    main = dck_utils.count_main_cards(deck_text)
    total = commanders + main
    if total != DECK_SIZE:
        violations.append(Violation(
            code=CODE_DECK_SIZE,
            message=(
                f"A Commander deck must be exactly {DECK_SIZE} cards "
                f"(command zone + library); this deck has {total} "
                f"({commanders} commander(s) + {main} mainboard)."
            ),
        ))
    return commanders, main


def _check_commanders(
    commander_lines: list[tuple[int, str]],
    cards: _Cards,
    violations: list[Violation],
    unverified: list[Violation],
) -> None:
    """Commander count, eligibility, and partner-pair legality."""
    if not commander_lines:
        violations.append(Violation(
            code=CODE_COMMANDER_MISSING,
            message="The deck has no [Commander] section entries.",
        ))
        return

    # A qty>1 commander line ("2 Krenko, Mob Boss") is two commanders
    # as far as the command zone is concerned, so expand before
    # counting — otherwise a duplicate commander reads as legal.
    names: list[str] = []
    for qty, name in commander_lines:
        names.extend([name] * max(1, qty))

    if len(names) > 2:
        violations.append(Violation(
            code=CODE_COMMANDER_COUNT,
            message=(
                f"A deck may have at most two commanders (and only via "
                f"Partner / Friends forever / Background / Doctor's "
                f"companion); this deck lists {len(names)}."
            ),
            cards=tuple(names),
        ))
        return

    resolved: list[tuple[str, Optional[dict]]] = [
        (name, cards.get(name)) for name in names
    ]

    # Resolve the pairing FIRST, because for one shape it is what
    # grants eligibility: a Background is a Legendary Enchantment, not
    # a legendary creature, and its own text never says "can be your
    # commander" — the partner's "Choose a Background" does. Same
    # structure keeps Doctor's companion pairs honest. ``None`` means
    # "couldn't tell" and suppresses the pair violation entirely.
    pair_legal: Optional[bool] = None
    if len(names) == 2:
        (name_a, card_a), (name_b, card_b) = resolved
        if card_a is not None and card_b is not None:
            pair_legal = _pair_is_legal(name_a, card_a, name_b, card_b)

    ineligible: list[str] = []
    unknown: list[str] = []
    for name, card in resolved:
        eligible = is_eligible_commander(card)
        if eligible is None:
            unknown.append(name)
        elif not eligible:
            # A legal pairing legitimizes a legendary non-creature
            # second commander (Backgrounds); nothing else.
            if pair_legal and "legendary" in _type_line(card):
                continue
            ineligible.append(name)
    if ineligible:
        violations.append(Violation(
            code=CODE_COMMANDER_INELIGIBLE,
            message=(
                "A commander must be a legendary creature or a card that "
                "says it can be your commander."
            ),
            cards=tuple(ineligible),
        ))
    if unknown:
        unverified.append(Violation(
            code=CODE_UNVERIFIED_COMMANDER,
            message=(
                "Commander eligibility could not be verified — no card "
                "data available."
            ),
            cards=tuple(unknown),
        ))

    if len(names) != 2:
        return
    if pair_legal is None:
        # Outage contract: a pairing we can't read is unverified, not
        # illegal. Claiming "these two can't be partners" off missing
        # oracle text would condemn every legal partner deck during an
        # outage.
        unverified.append(Violation(
            code=CODE_UNVERIFIED_PAIR,
            message=(
                "Two commanders are listed but the partner pairing could "
                "not be verified — no card data available."
            ),
            cards=tuple(names),
        ))
        return
    if not pair_legal:
        violations.append(Violation(
            code=CODE_COMMANDER_PAIR,
            message=(
                "Two commanders are only legal with Partner, Partner with, "
                "Friends forever, Choose a Background, or Doctor's "
                "companion; this pair has none of those."
            ),
            cards=tuple(names),
        ))


def _check_singleton(
    quantities: dict[str, int],
    display: dict[str, str],
    cards: _Cards,
    violations: list[Violation],
    unverified: list[Violation],
) -> None:
    """Singleton rule plus its two exemptions.

    Basic lands pass on the NAME (no lookup, so ``35 Forest`` is fine
    offline). Everything else needs oracle text to know whether the
    card grants multiples — and when that text is unavailable the
    duplicate is reported as unverified rather than as a violation,
    because "2 Sol Ring" and "9 Nazgûl" are indistinguishable without
    it and only one of them is illegal.
    """
    duplicates: list[str] = []
    over_cap: list[str] = []
    unknown: list[str] = []
    for key, qty in sorted(quantities.items()):
        if qty <= 1:
            continue
        name = display.get(key, key)
        if _is_basic_land_name(name):
            continue
        card = cards.get(name)
        if card is None:
            unknown.append(name)
            continue
        limit = copy_limit(card)
        if limit is None:
            continue
        if qty > limit > 1:
            over_cap.append(f"{name} ({qty} copies, limit {limit})")
        elif qty > limit:
            duplicates.append(f"{name} (x{qty})")
    if duplicates:
        violations.append(Violation(
            code=CODE_DUPLICATE_CARD,
            message=(
                "Commander is a singleton format: only basic lands and "
                "cards that say a deck can have any number of them may "
                "appear more than once."
            ),
            cards=tuple(duplicates),
        ))
    if over_cap:
        violations.append(Violation(
            code=CODE_COPY_LIMIT,
            message="More copies than the card's own limit allows.",
            cards=tuple(over_cap),
        ))
    if unknown:
        unverified.append(Violation(
            code=CODE_UNVERIFIED_SINGLETON,
            message=(
                "Cards appear more than once but the singleton exemption "
                "could not be verified — no card data available."
            ),
            cards=tuple(unknown),
        ))


def _check_color_identity(
    commander_names: list[str],
    quantities: dict[str, int],
    display: dict[str, str],
    cards: _Cards,
    violations: list[Violation],
    unverified: list[Violation],
) -> None:
    """Every card's color identity must be a subset of the commander's.

    The commander's identity is the UNION across a partner pair. We
    read Scryfall's ``color_identity`` field verbatim — it already
    folds in mana symbols that appear only in RULES TEXT (activated
    ability costs, hybrid symbols in reminderless text), which a
    mana-cost-derived identity would miss. Kenrith, the Returned King
    costs ``{4}{W}`` on the face of it but is WUBRG; deriving from the
    cost would wave three colors of off-color cards straight through.

    An unresolvable COMMANDER skips the whole check (mirrors
    ``_proposer_filters.enforce_color_identity``'s ``None`` contract:
    better noisy than wrong), and an unresolvable deck card is
    unverified individually.
    """
    if not commander_names:
        return
    identity: set[str] = set()
    for name in commander_names:
        ci = _color_identity(cards.get(name))
        if ci is None:
            unverified.append(Violation(
                code=CODE_UNVERIFIED_COLOR_IDENTITY,
                message=(
                    "Color identity could not be checked — the commander's "
                    "identity is unknown."
                ),
                cards=(name,),
            ))
            return
        identity |= ci

    commander_keys = {_dck_name_key(n) for n in commander_names}
    offenders: list[str] = []
    unknown: list[str] = []
    for key in sorted(quantities):
        if key in commander_keys:
            continue
        name = display.get(key, key)
        ci = _color_identity(cards.get(name))
        if ci is None:
            unknown.append(name)
            continue
        if not ci.issubset(identity):
            extra = "".join(sorted(ci - identity))
            offenders.append(f"{name} ({extra})")
    if offenders:
        violations.append(Violation(
            code=CODE_COLOR_IDENTITY,
            message=(
                "Every card's color identity must be a subset of the "
                "commander's ({}).".format("".join(sorted(identity)) or "colorless")
            ),
            cards=tuple(offenders),
        ))
    if unknown:
        unverified.append(Violation(
            code=CODE_UNVERIFIED_COLOR_IDENTITY,
            message=(
                "Color identity could not be verified for these cards — "
                "no card data available."
            ),
            cards=tuple(unknown),
        ))


def _check_banned(
    display: dict[str, str],
    cards: _Cards,
    violations: list[Violation],
    unverified: list[Violation],
) -> None:
    """Scryfall-backed ban sweep + the Lutri companion carve-out."""
    scan = _scan_banned(display.values(), cards)
    if scan.banned:
        violations.append(Violation(
            code=CODE_BANNED_CARD,
            message="Banned in Commander.",
            cards=scan.banned,
        ))
    if scan.not_in_format:
        violations.append(Violation(
            code=CODE_NOT_IN_FORMAT,
            message="Not legal in Commander (not a format-legal card).",
            cards=scan.not_in_format,
        ))
    if scan.unverified:
        unverified.append(Violation(
            code=CODE_UNVERIFIED_BANNED,
            message=(
                "Ban status could not be verified — Scryfall commander "
                "legality is unavailable for these cards."
            ),
            cards=scan.unverified,
        ))
    for key, note in _COMPANION_BANNED.items():
        if key in display:
            unverified.append(Violation(
                code=CODE_LUTRI_COMPANION,
                message=note,
                cards=(display[key],),
            ))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_deck(
    deck_text: str, *, lookup: Optional[LookupFn] = None,
) -> LegalityReport:
    """Validate ``deck_text`` (a Forge ``.dck`` blob) for Commander.

    Validates card-line syntax, then runs seven checks — deck size,
    singleton (with the "any number of cards named" and Nazgûl-style
    capped exemptions), color identity,
    commander eligibility, partner/Background/Doctor's-companion
    pairing, bans, and the Lutri companion note — and returns a
    ``LegalityReport``.

    ``lookup`` is the injection seam: any ``name -> dict | None``
    callable. It defaults to ``scryfall_client.lookup_card`` (disk-
    cached), and tests pass a dict-backed stub so the suite stays
    hermetic. Exceptions from ``lookup`` are swallowed per the outage
    contract — an unreachable Scryfall degrades checks to
    ``unverified``, it never manufactures a violation.

    The report's ``legal`` flag means "no CONFIRMED violation". Read
    ``status`` for the three-state ``legal`` / ``illegal`` /
    ``unverified`` answer before telling a user their deck is fine.
    """
    cards = _Cards(lookup)
    violations: list[Violation] = []
    unverified: list[Violation] = []

    _check_card_line_syntax(deck_text, violations)
    commanders, _main = _check_deck_size(deck_text, violations)
    commander_lines = _iter_commander_cards(deck_text)
    commander_names = [
        name for qty, name in commander_lines for _ in range(max(1, qty))
    ]
    quantities, display = _deck_quantities(deck_text)

    _check_commanders(commander_lines, cards, violations, unverified)
    _check_singleton(quantities, display, cards, violations, unverified)
    # Only run the color check against a legal command zone: with
    # three "commanders" the union identity is meaningless and would
    # bury the real problem under a wall of false off-color hits.
    if 1 <= len(commander_names) <= 2:
        _check_color_identity(
            commander_names, quantities, display, cards, violations, unverified,
        )
    _check_banned(display, cards, violations, unverified)

    # Freshness is read from the shared disk-snapshot store regardless
    # of the injected ``lookup``: every production lookup (the default,
    # routes_decks' cache-only wrapper, card_score's memoizer) is a
    # view over that same store, and hermetic test stubs simply find no
    # snapshots and get (None, None).
    data_age_days, data_warning = snapshot_staleness(display.values())

    return LegalityReport(
        legal=not violations,
        violations=tuple(violations),
        unverified=tuple(unverified),
        card_count=sum(quantities.values()),
        commander_count=commanders,
        lookup_failures=cards.failures,
        data_age_days=data_age_days,
        data_warning=data_warning,
    )
