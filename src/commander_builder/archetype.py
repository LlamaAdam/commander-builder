"""Archetype classifier — v2, oracle-signal backed.

Turns one deck into one of five labels:
``"aggro" | "midrange" | "control" | "combo" | "stax"``.

WHY V2 EXISTS (the v1 failure this replaces)
============================================
v1 matched five small regex sets against the joined card NAMES of
``[Main]``. Its own docstring conceded the problem: a name carries a card
title ("Winter Orb") or a tribal noun ("Goblin") and nothing else, so a
deck that doesn't ADVERTISE its strategy in its card names could not be
classified and fell to ``"midrange"``. Most real decks did, and two
consumers quietly broke:

* ``pool_curator._slice_violates`` treats same-archetype slice-mates as a
  diversity violation. With near-uniform ``"midrange"`` labels EVERY
  arrangement violated, the bounded swap search always exhausted, and the
  WARN-and-ship-default path fired on every curation — archetype
  diversity in opponent pools was a de facto no-op.
* ``bracket_estimator``'s ``archetype_combo`` (+1.0) and
  ``archetype_stax`` (+0.5) weights almost never fired, so dedicated
  combo decks under-estimated by a full bracket point.

The fix is to stop guessing from names and read the signals the codebase
ALREADY derives from oracle text. Nothing here re-implements a classifier
that exists elsewhere; this module composes them.

THE LADDER (cheapest first; first hit wins)
===========================================
1. **Filename hint** — unchanged fast path, one regex. A deck the user
   named "Storm Combo" is telling us the strategy outright.
2. **Oracle-signal scan** (NEW — the substance of v2), in this order:

   a. ``combo``   — ``combo_detection.detect_combos_in_deck`` finds a
      GAME-ENDING combo. Present-and-lethal is the strongest single piece
      of evidence a deck can offer.
   b. ``stax``    — ``>= MIN_STAX_CARDS`` cards match the resource-denial
      oracle table below.
   c. ``combo``   — tutor density (``>= MIN_TUTORS_FOR_COMBO`` cards in
      ``bracket_estimator._TUTOR_CARDS``). Deliberately BELOW stax: a
      cEDH prison list runs tutors too, and its lock pieces are the more
      specific signal.
   d. ``control`` — ``interaction.interaction_report``'s stack row plus
      either the board-wipe row or a majority-instant answer suite.
   e. ``aggro``   — a tribal identity (``staples.detect_tribal_type``)
      with a real creature share, or a low curve with a high one.
3. **Card-NAME content scan** — v1's ``_content_scan``, kept verbatim as
   the degradation path for a cold snapshot cache. It is still right for
   the loud cases it was written for, and
   ``bracket_estimator._derive_archetype`` imports it directly for the
   no-file-on-disk case.
4. **``"midrange"``** — the honest default. A deck that trips none of the
   above reads as a goodstuff pile, and a wrong label is worse than an
   unopinionated one.

OFFLINE, ALWAYS. Every lookup goes through the Scryfall DISK CACHE only
(``_cached_scryfall``, mirroring ``combo_detection``'s reader of the same
name). ``pool_curator`` classifies ~60 candidates of ~100 cards per
curation; a network fetch per card would turn a curation into an
afternoon and make the result depend on Scryfall's uptime. A cold cache
is not an error — the oracle rungs abstain and the ladder falls through
to the name scan, exactly as v1 behaved. The name-based rungs (combo
detection, tutor density) need no oracle text and still fire; only
stax/control/aggro are gated on ``MIN_ORACLE_COVERAGE``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Literal, Optional

from . import dck_utils

Archetype = Literal["aggro", "midrange", "control", "combo", "stax"]

#: Resolves a card name to its Scryfall dict (or None) — the injectable
#: seam every oracle-backed signal reads through. Same shape as
#: ``combo_detection.CardLookup``.
CardLookup = Callable[[str], Optional[dict]]


# ---------------------------------------------------------------------------
# Rung 1 — filename hints (unchanged from v1)
# ---------------------------------------------------------------------------

# Filename token patterns (the deck filename often telegraphs the strategy).
_FILENAME_HINTS: list[tuple[re.Pattern[str], Archetype]] = [
    (re.compile(r"\b(combo|storm|consult|breach|hulk)\b", re.IGNORECASE), "combo"),
    (re.compile(r"\b(stax|prison|hatebear|lockdown)\b", re.IGNORECASE), "stax"),
    (re.compile(r"\b(control|counterspell|prison)\b", re.IGNORECASE), "control"),
    (re.compile(r"\b(aggro|tribal|voltron|samurai|warrior)\b", re.IGNORECASE), "aggro"),
]


def _filename_hint(deck_filename: str) -> Optional[Archetype]:
    for pattern, archetype in _FILENAME_HINTS:
        if pattern.search(deck_filename):
            return archetype
    return None


# ---------------------------------------------------------------------------
# Rung 2 thresholds — ONE documented block (the tuning surface)
# ---------------------------------------------------------------------------

#: Fraction of a deck's DISTINCT card names that must resolve against the
#: local snapshot cache before the oracle rungs (stax / control / aggro)
#: get an opinion. Below it we are reading a minority of the deck, and a
#: signal derived from a third of the list is not evidence. Half is the
#: same "most of the deck must be readable" bar ``interaction_report``
#: uses for its own outage contract.
MIN_ORACLE_COVERAGE = 0.5

#: Cards from ``bracket_estimator._TUTOR_CARDS`` that read as a deliberate
#: combo deck. 4 mirrors the estimator's own ``tutors_4_plus`` step: four
#: searches in a 99 means the deck is assembling something specific, not
#: smoothing its draws. Kept in sync BY IMPORTING that list, never copying
#: it — a name added there is counted here for free.
MIN_TUTORS_FOR_COMBO = 4

#: Cards matching the resource-denial table below that make a deck stax. A
#: committed list runs 8-12 lock pieces and the patterns are narrow (only
#: templated denial wording fires) so they UNDER-count; 5 is where "this
#: deck locks the table" stops being an accident. Below it one Ghostly
#: Prison — a legitimate single hit in decks that are not stax — cannot
#: claim the label.
MIN_STAX_CARDS = 5

#: Counterspells (``interaction``'s ``stack`` row) required for control.
#: ``BRACKET_INTERACTION_MINIMUMS`` asks a *well-rounded* B4 deck for 2 and
#: a cEDH deck for 6; 5 is the band where a deck is actually playing
#: draw-go rather than splashing permission.
MIN_CONTROL_STACK = 5

#: Board wipes required alongside them. ``ROLE_TARGETS`` and the bracket
#: minimums both put a rounded deck at 2; 3 is mass removal as a plan
#: rather than as insurance.
MIN_CONTROL_WIPES = 3

#: The alternative to the wipe count: share of the deck's interaction
#: castable on an opponent's turn. 0.5 is the B4 minimum in
#: ``BRACKET_INTERACTION_MINIMUMS`` — a majority-instant answer suite is
#: the draw-go posture that defines control even without sweepers.
MIN_CONTROL_INSTANT_SHARE = 0.5

#: Creature share (creatures / nonlands) for the non-tribal aggro path. A
#: goodstuff pile sits near 0.30; a deck whose plan is the board runs
#: 25-35 creatures in ~63 nonlands (0.40-0.55).
MIN_AGGRO_CREATURE_SHARE = 0.40

#: Creature share for the TRIBAL aggro path. Lower because the tribal
#: identity is itself evidence of the plan; this only rules out a
#: "tribal-flavored" list that is really a value engine.
MIN_AGGRO_TRIBAL_CREATURE_SHARE = 0.30

#: Average nonland mana value at or below which the curve reads "deploy
#: and attack". Tighter than ``bracket_estimator``'s loose ``curve_tight``
#: band (2.6): 2.8 admits the two- and three-drop tribal curves that are
#: the archetype's bread and butter, not a 3.5-CMC battlecruiser deck.
MAX_AGGRO_AVG_CMC = 2.8

#: Same-creature-subtype cards that make a deck tribal when the COMMANDER
#: doesn't name a tribe. 12 matches ``corpus_themes.TRIBAL_MIN`` ("a
#: build-around, not incidental"); "Human" gets that module's higher bar
#: because it rides along on a third of all creatures ever printed.
MIN_TRIBAL_SUBTYPE_CARDS = 12
MIN_TRIBAL_SUBTYPE_CARDS_HUMAN = 20


# ---------------------------------------------------------------------------
# Rung 2b — the stax oracle table (NEW, and deliberately local)
# ---------------------------------------------------------------------------
#
# WHY HERE AND NOT IN ``staples``: that module owns roles a card plays
# inside a deck's own gameplan (ramp / draw / removal / wipe). "This card
# denies the TABLE a resource" is not a role in that taxonomy — it is an
# archetype fingerprint, read only by this module.
#
# Written against REAL Scryfall wording (fixtures in
# tests/test_archetype.py), one pattern per templated denial shape:
#
#   players_cant   "Players can't untap more than one land..."  Winter Orb
#   cost_tax       "Noncreature spells cost {1} more to cast."  Thorn of Amethyst
#   doesnt_untap   "...doesn't untap during..."                 Kismet class
#   cant_untap     "...can't untap..."                          Static Orb
#   skip_step      "Players skip their untap steps."            Stasis
#   pay_or_cant    "Creatures can't attack you unless their
#                   controller pays {2}..."                     Ghostly Prison
#   recurring_sac  "At the beginning of each player's upkeep,
#                   that player sacrifices..."                  Smokestack
#   draw_limit     "...can't draw more than one card each turn" Spirit of the Labyrinth
#   cant_be_cast   "...can't be cast..."
#   cant_cast      "Your opponents can't cast spells from..."   Drannith Magistrate
#
# THREE FALSE-POSITIVE GUARDS, all load-bearing:
#
# 1. YOUR-OWN-COST REDUCERS. ``cost_tax`` matches only "more to cast", and
#    a reducer says "less" — but that alone would still let a card taxing
#    YOUR OWN spells (a drawback, not a lock) read as stax, so a hit is
#    DISCARDED when its clause is self-scoped ("spells you cast").
# 2. ``pay_or_cant`` REQUIRES A "CAN'T". The bare "unless that player pays"
#    is the Rhystic Study / Mystic Remora template — two of the most-played
#    blue VALUE cards in the format. Requiring a prohibition in the same
#    clause keeps the pillow-fort taxes and drops the cantrip engines.
# 3. ``recurring_sac`` is scoped to the upkeep-trigger template, not a bare
#    "each player sacrifices": the bare phrase is a one-shot edict
#    (Fleshbag Marauder), which is removal, not a lock.

_STAX_PATTERN_SOURCES: tuple[tuple[str, str], ...] = (
    ("players_cant",
     r"\b(?:each |all |your )?(?:players?|opponents?) can'?t\b"),
    ("cost_tax",
     r"\bcosts? \{\d+\}(?: or more)? more to cast\b"),
    ("doesnt_untap",
     r"\bdoesn'?t untap during\b"),
    ("cant_untap",
     r"\bcan'?t untap\b"),
    ("skip_step",
     r"\bskips? (?:your|their|his or her|that player'?s|its controller'?s)"
     r"[^.\n]{0,24}\b(?:untap|upkeep|draw|combat|step|phase)"),
    ("pay_or_cant",
     r"\bcan'?t\b[^.\n]{0,80}\bunless (?:that player|their controller|"
     r"its controller|they|the player)[^.\n]{0,24}\bpays?\b"),
    ("recurring_sac",
     r"beginning of each player'?s[^.\n]{0,40}(?:upkeep|end step|draw step)"
     r"[^.\n]{0,80}\bsacrifices?\b"),
    ("draw_limit",
     r"\bcan'?t draw more than\b"),
    ("cant_be_cast",
     r"\bcan'?t be cast\b"),
    ("cant_cast",
     r"\b(?:players?|opponents?)\b[^.\n]{0,30}\bcan'?t cast\b"),
)

_STAX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(source, re.IGNORECASE))
    for label, source in _STAX_PATTERN_SOURCES
)

#: A ``cost_tax`` clause carrying any of these is taxing the CONTROLLER's
#: own spells (a drawback), not the table's. See guard 1 above.
_SELF_SCOPED_COST = re.compile(
    r"\b(?:you cast|spells you|creatures you control|permanents you control)\b",
    re.IGNORECASE,
)


def stax_categories(oracle_text: str) -> set[str]:
    """Resource-denial categories one card's oracle text proves.

    Empty set means "no denial wording found" — which is the common case
    and carries no penalty; the caller only ever counts non-empty results.
    Clause-scoped (``.``/newline delimited) so a guard can inspect the
    same sentence the pattern matched rather than the whole card.
    """
    text = (oracle_text or "").strip()
    if not text:
        return set()
    clauses = [c for c in re.split(r"[.\n]", text) if c.strip()]
    found: set[str] = set()
    for label, pattern in _STAX_PATTERNS:
        for clause in clauses:
            if not pattern.search(clause):
                continue
            if label == "cost_tax" and _SELF_SCOPED_COST.search(clause):
                continue  # guard 1: self-scoped tax is a drawback, not a lock
            found.add(label)
            break
    return found


# ---------------------------------------------------------------------------
# Rung 3 — the v1 card-NAME content scan, kept as the degradation path
# ---------------------------------------------------------------------------
#
# Every token here must be something a card NAME can actually contain: a
# specific card/commander name fragment or a tribal noun. Oracle-text
# phrases were removed in the 2026-07 rebalance because they can never
# match a name. v2 does not extend this table — new signal goes into the
# oracle rungs above, where it can be stated precisely.
#
# ``bracket_estimator._derive_archetype`` imports ``_content_scan``
# directly for the case where it is scoring rendered deck text with no
# file on disk, so this function's ``(winner, score)`` contract is public
# in practice. Do not change its shape without checking that caller.

_AGGRO_COMMANDERS = re.compile(
    r"\b("
    # Famous aggro / tribal commanders
    r"krenko|edgar markov|isshin|alesha|akiri|adriana|"
    r"hakbal|kumena|king narfi|brion"
    r")\b",
    re.IGNORECASE,
)
_AGGRO_TRIBAL_NOUNS = re.compile(
    r"\b("
    r"goblin|goblins|warrior|warriors|berserker|berserkers|"
    r"samurai|knight|knights|vampire|vampires|"
    r"merfolk|elf|elves|spirit|spirits|"
    r"dragon|dragons|angel|angels|wizard|wizards|"
    r"zombie|zombies|cat|cats|dinosaur|dinosaurs|"
    r"human|humans|elemental|elementals"
    r")\b",
    re.IGNORECASE,
)
_CONTROL_KEYWORDS = re.compile(
    r"\b("
    r"yuriko|teferi|narset|talrand|baral|"
    r"propaganda|ghostly prison|cyclonic rift|farewell"
    r")\b",
    re.IGNORECASE,
)
_COMBO_KEYWORDS = re.compile(
    r"\b("
    r"thassa's oracle|laboratory maniac|jace, wielder|demonic consultation|"
    r"tainted pact|ad nauseam|underworld breach|food chain|"
    r"protean hulk|hermit druid|"
    r"infinite"
    r")\b",
    re.IGNORECASE,
)
_STAX_KEYWORDS = re.compile(
    r"\b("
    r"winter orb|static orb|stasis|smokestack|tangle wire|sphere of resistance|"
    r"thalia, guardian|drannith magistrate|grand arbiter|kataki|"
    r"trinisphere|thorn of amethyst|null rod|stony silence|collector ouphe|"
    r"glowrider|vryn wingmare|opposition agent"
    r")\b",
    re.IGNORECASE,
)
_MIDRANGE_KEYWORDS = re.compile(
    r"\b("
    r"tribal"
    r")\b",
    re.IGNORECASE,
)

#: Minimum matches for the NAME scan's winner. Unchanged from v1.
MIN_CONTENT_MATCHES = 3

#: Separate, much higher bar for aggro's tribal-NOUN matches: nouns like
#: "dragon" / "cat" / "spirit" show up in a few card names of nearly every
#: deck, so at threshold 3 they made "aggro" the de-facto default. Below
#: this bar the noun matches contribute NOTHING (not merely less).
MIN_TRIBAL_MATCHES = 10


def _content_scan(card_names: list[str]) -> tuple[Optional[Archetype], int]:
    """Run each archetype's keyword regex against the joined card-NAME
    corpus. Returns the winning archetype + its match count, or
    ``(None, 0)`` if no archetype clears ``MIN_CONTENT_MATCHES``."""
    if not card_names:
        return None, 0
    corpus = "\n".join(card_names)
    tribal_hits = len(_AGGRO_TRIBAL_NOUNS.findall(corpus))
    aggro_score = len(_AGGRO_COMMANDERS.findall(corpus))
    if tribal_hits >= MIN_TRIBAL_MATCHES:
        aggro_score += tribal_hits
    scores: dict[Archetype, int] = {
        "aggro": aggro_score,
        "control": len(_CONTROL_KEYWORDS.findall(corpus)),
        "combo": len(_COMBO_KEYWORDS.findall(corpus)),
        "stax": len(_STAX_KEYWORDS.findall(corpus)),
        "midrange": len(_MIDRANGE_KEYWORDS.findall(corpus)),
    }
    winner = max(scores, key=lambda k: scores[k])
    if scores[winner] < MIN_CONTENT_MATCHES:
        return None, scores[winner]
    return winner, scores[winner]


def _read_main_card_names(deck_path: Path) -> list[str]:
    """Pull just the card-name portion of every line under [Main]. Strip the
    leading qty and the trailing |SET|CN suffix.

    Thin wrapper over ``dck_utils.main_card_names``."""
    if not deck_path.exists():
        return []
    return dck_utils.main_card_names(deck_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Oracle plumbing — disk cache only
# ---------------------------------------------------------------------------

def _cached_scryfall(card_name: str) -> Optional[dict]:
    """On-disk Scryfall snapshot for ``card_name``, or None. NEVER fetches.

    Mirrors ``combo_detection._cached_scryfall`` and
    ``_advisor_heuristic._cached_scryfall``. Cache-only is a hard
    requirement, not an optimization — see the module docstring's
    "OFFLINE, ALWAYS" note. Never raises: a classifier that can throw
    would abort a 40-minute curation over one corrupt snapshot.
    """
    try:
        from .scryfall_client import lookup_card
        return lookup_card(card_name, cache_only=True)
    except Exception:  # noqa: BLE001 — classification must not raise
        return None


def _oracle_of(data: dict) -> str:
    """Full oracle text of a snapshot, joining ``card_faces`` when the top
    level has none (split / adventure / MDFC layouts keep text on faces)."""
    text = data.get("oracle_text")
    if text:
        return str(text)
    faces = data.get("card_faces") or []
    return "\n".join(
        str(f.get("oracle_text") or "") for f in faces if isinstance(f, dict)
    )


def _type_line_of(data: dict) -> str:
    """Type line of a snapshot, falling back to the joined faces."""
    line = data.get("type_line")
    if line:
        return str(line)
    faces = data.get("card_faces") or []
    return " // ".join(
        str(f.get("type_line") or "") for f in faces if isinstance(f, dict)
    )


def _cmc_of(data: dict) -> Optional[float]:
    cmc = data.get("cmc")
    if cmc is None:
        return None
    try:
        return float(cmc)
    except (TypeError, ValueError):
        return None


def _load_snapshots(
    deck_text: str, lookup: CardLookup,
) -> tuple[list[tuple[int, str]], dict[str, dict]]:
    """``([(qty, name), ...], {lowercased name: snapshot})`` for the deck.

    ONE pass over the list, one lookup per DISTINCT name — the resulting
    dict is then reused by every signal (and handed to
    ``interaction_report`` as its injected lookup) so a 99-card deck costs
    at most 99 cache reads no matter how many rungs run.
    """
    from .deck_library_analyzer import iter_deck_cards
    entries = [(qty, name) for qty, name in iter_deck_cards(deck_text or "") if name]
    snapshots: dict[str, dict] = {}
    for _qty, name in entries:
        key = name.lower()
        if key in snapshots:
            continue
        try:
            data = lookup(name)
        except Exception:  # noqa: BLE001 — an injected lookup may raise
            data = None
        if isinstance(data, dict) and data:
            snapshots[key] = data
    return entries, snapshots


def _oracle_coverage(
    entries: list[tuple[int, str]], snapshots: dict[str, dict],
) -> float:
    """Share of DISTINCT card names that resolved. 0.0 for an empty deck."""
    distinct = {name.lower() for _qty, name in entries}
    if not distinct:
        return 0.0
    return len(snapshots) / len(distinct)


# ---------------------------------------------------------------------------
# The individual signals
# ---------------------------------------------------------------------------

def _game_ending_combos(deck_text: str) -> int:
    """Count of detected combos whose ``produces`` describes a win or an
    infinite loop. Pure name matching against the combo DB — needs no
    oracle text, so it fires even on a cold snapshot cache."""
    try:
        from .combo_detection import detect_combos_in_deck, is_game_ending
        return sum(
            1 for c in detect_combos_in_deck(deck_text or "") if is_game_ending(c)
        )
    except Exception:  # noqa: BLE001
        return 0


def _tutor_count(entries: list[tuple[int, str]]) -> int:
    """Distinct cards from ``bracket_estimator._TUTOR_CARDS`` in the deck.

    IMPORTS the estimator's curated list rather than copying it: the two
    modules must agree on what a tutor is, and a second copy would drift.
    Distinct names (not quantities) because Commander is singleton and a
    duplicated line is a file artifact, not a second tutor.
    """
    try:
        from .bracket_estimator import _TUTOR_CARDS
    except Exception:  # noqa: BLE001
        return 0
    names = {name.lower() for _qty, name in entries}
    return len(names & _TUTOR_CARDS)


def _stax_count(
    entries: list[tuple[int, str]], snapshots: dict[str, dict],
) -> int:
    """Distinct cards whose oracle text matches at least one denial shape."""
    hits = 0
    for key in {name.lower() for _qty, name in entries}:
        data = snapshots.get(key)
        if not data:
            continue
        if stax_categories(_oracle_of(data)):
            hits += 1
    return hits


def _interaction_signals(
    deck_text: str, snapshots: dict[str, dict],
) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """``(stack_count, wipe_count, instant_share)`` from ``interaction``.

    Delegates to ``interaction.interaction_report`` — the module that owns
    "what can this deck answer" — with the snapshots we already loaded as
    its injected lookup, so nothing is fetched twice.

    ``use_forge=False`` on purpose: the Forge-script classifier is more
    precise per card but loads and parses a script file per card, and this
    runs over ~60 decks in a curation. The oracle-regex path is the
    documented fallback and is what an archetype label needs.

    All three are ``None`` when the report abstains (its own >half-
    unreadable outage contract), which the caller treats as "no opinion".
    """
    try:
        from .interaction import interaction_report
        report = interaction_report(
            deck_text or "",
            lookup=lambda n: snapshots.get(n.lower()),
            use_forge=False,
        )
    except Exception:  # noqa: BLE001
        return None, None, None
    if not report:
        return None, None, None
    categories = report.get("categories") or {}
    stack = (categories.get("stack") or {}).get("count")
    wipes = (categories.get("board_wipe") or {}).get("count")
    share = (report.get("instant_speed") or {}).get("share")
    return stack, wipes, share


def _tribal_type(
    deck_text: str,
    entries: list[tuple[int, str]],
    snapshots: dict[str, dict],
) -> Optional[str]:
    """The deck's tribe, or None.

    Commander first (``staples.detect_tribal_type`` over its oracle text —
    the use the function was written for: Edgar Markov, Lathliss and The
    Ur-Dragon all name their tribe outright), then a deck-wide creature
    SUBTYPE concentration for generic commanders running a tribal package.
    """
    try:
        from .staples import detect_tribal_type
    except Exception:  # noqa: BLE001
        return None

    for name in dck_utils.section_card_names(deck_text or "", "Commander"):
        data = snapshots.get(name.lower())
        if not data:
            continue
        tribe = detect_tribal_type(_oracle_of(data), _type_line_of(data))
        if tribe:
            return tribe

    subtype_counts: dict[str, int] = {}
    for qty, name in entries:
        data = snapshots.get(name.lower())
        if not data:
            continue
        type_line = _type_line_of(data)
        low = type_line.lower()
        if "creature" not in low or "—" not in type_line:
            continue
        for sub in type_line.split("—", 1)[1].split():
            if sub == "//":
                continue
            subtype_counts[sub] = subtype_counts.get(sub, 0) + max(1, qty)
    for sub, count in sorted(subtype_counts.items(), key=lambda kv: -kv[1]):
        floor = (
            MIN_TRIBAL_SUBTYPE_CARDS_HUMAN if sub.lower() == "human"
            else MIN_TRIBAL_SUBTYPE_CARDS
        )
        if count >= floor:
            return sub
    return None


def _curve_signals(
    entries: list[tuple[int, str]], snapshots: dict[str, dict],
) -> tuple[Optional[float], Optional[float]]:
    """``(creature_share, avg_cmc)`` over the deck's NONLAND cards.

    Quantity-weighted and lands-excluded, matching ``deck_dashboard``'s
    stat tiles and ``bracket_estimator._derive_avg_cmc`` so the same deck
    yields the same number everywhere. ``(None, None)`` when no nonland
    card resolved — never a fabricated 0.0, which would read as the
    tightest possible curve.
    """
    nonland = 0
    creatures = 0
    cmcs: list[float] = []
    for qty, name in entries:
        data = snapshots.get(name.lower())
        if not data:
            continue
        type_line = _type_line_of(data).lower()
        if "land" in type_line:
            continue
        n = max(1, qty)
        nonland += n
        if "creature" in type_line:
            creatures += n
        cmc = _cmc_of(data)
        if cmc is not None:
            cmcs.extend([cmc] * n)
    if not nonland:
        return None, None
    share = creatures / nonland
    avg = round(sum(cmcs) / len(cmcs), 2) if cmcs else None
    return share, avg


# ---------------------------------------------------------------------------
# Rung 2 — the oracle-signal scan
# ---------------------------------------------------------------------------

def derive_archetype_signals(
    deck_text: str, *, lookup: Optional[CardLookup] = None,
) -> dict:
    """Every signal the oracle rung reads, plus the label it produces.

    Exposed as a public function (not folded into ``classify``) because
    "why did this deck get this label?" is the first question anyone asks
    of a classifier, and because it is the seam tests inject a lookup
    through — ``classify``'s own signature is fixed by its callers.

    Returns a plain dict::

        {"oracle_coverage": 0.98,   # share of distinct names resolved
         "oracle_available": True,  # coverage >= MIN_ORACLE_COVERAGE
         "game_ending_combos": 1, "tutors": 2, "stax_cards": 0,
         "stack_count": 6, "wipe_count": 3, "instant_share": 0.61,
         "creature_share": 0.19, "avg_cmc": 2.44, "tribal_type": None,
         "label": "control"}       # None when nothing fired

    Every count is present even when the label is ``None``. Never raises;
    a signal that cannot be derived is ``None``.
    """
    resolve = lookup or _cached_scryfall
    entries, snapshots = _load_snapshots(deck_text, resolve)
    coverage = _oracle_coverage(entries, snapshots)
    oracle_ok = coverage >= MIN_ORACLE_COVERAGE

    combos = _game_ending_combos(deck_text)
    tutors = _tutor_count(entries)

    # None (not 0) when the cache is cold: "we couldn't read the deck" and
    # "the deck has no lock pieces" are opposite conclusions and must never
    # serialize the same — the same contract avg_cmc follows in
    # bracket_estimator._derive_avg_cmc.
    stax_cards = _stax_count(entries, snapshots) if oracle_ok else None
    if oracle_ok:
        stack, wipes, instant_share = _interaction_signals(deck_text, snapshots)
        creature_share, avg_cmc = _curve_signals(entries, snapshots)
        tribe = _tribal_type(deck_text, entries, snapshots)
    else:
        stack = wipes = None
        instant_share = creature_share = avg_cmc = None
        tribe = None

    signals = {
        "oracle_coverage": round(coverage, 3),
        "oracle_available": oracle_ok,
        "game_ending_combos": combos,
        "tutors": tutors,
        "stax_cards": stax_cards,
        "stack_count": stack,
        "wipe_count": wipes,
        "instant_share": instant_share,
        "creature_share": creature_share,
        "avg_cmc": avg_cmc,
        "tribal_type": tribe,
    }
    signals["label"] = _label_from_signals(signals)
    return signals


def _label_from_signals(s: dict) -> Optional[Archetype]:
    """Apply the rung-2 ladder to a derived signal set. ``None`` = abstain.

    Order is documented in the module docstring; the two non-obvious
    choices, restated where they are implemented:

    * game-ending combo OUTRANKS everything — the deck can win from an
      arbitrary board, which is the single most consequential fact about
      how it plays (and the fact ``bracket_estimator`` pays +1.0 for).
    * stax OUTRANKS the tutor-density combo signal — a cEDH stax list
      also runs four tutors, and its lock pieces are the more specific
      evidence. Reversing these would relabel every tutor-heavy prison
      deck as combo.
    """
    if (s.get("game_ending_combos") or 0) >= 1:
        return "combo"
    if (s.get("stax_cards") or 0) >= MIN_STAX_CARDS:
        return "stax"
    if (s.get("tutors") or 0) >= MIN_TUTORS_FOR_COMBO:
        return "combo"

    stack = s.get("stack_count")
    wipes = s.get("wipe_count") or 0
    share = s.get("instant_share")
    if stack is not None and stack >= MIN_CONTROL_STACK:
        if wipes >= MIN_CONTROL_WIPES or (
            share is not None and share >= MIN_CONTROL_INSTANT_SHARE
        ):
            return "control"

    creature_share = s.get("creature_share")
    avg_cmc = s.get("avg_cmc")
    if creature_share is not None:
        if s.get("tribal_type") and creature_share >= MIN_AGGRO_TRIBAL_CREATURE_SHARE:
            return "aggro"
        if (
            creature_share >= MIN_AGGRO_CREATURE_SHARE
            and avg_cmc is not None
            and avg_cmc <= MAX_AGGRO_AVG_CMC
        ):
            return "aggro"
    return None


def _oracle_scan(
    deck_text: str, lookup: Optional[CardLookup] = None,
) -> Optional[Archetype]:
    """The rung-2 label for a deck's text, or None to fall through."""
    try:
        return derive_archetype_signals(deck_text, lookup=lookup)["label"]
    except Exception:  # noqa: BLE001 — classification must not raise
        return None


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def classify(deck_path: Path) -> Archetype:
    """Archetype classification for one deck on disk.

    Ladder: filename hint -> oracle-signal scan -> card-NAME content scan
    -> ``"midrange"``. See the module docstring for what each rung reads
    and why they are in this order.

    Signature and return type are load-bearing: ``pool_curator``
    (``ArchetypeClassifier``), ``bracket_estimator._derive_archetype``,
    ``deck_dashboard``, ``intent.learn_intent`` and ``card_score``'s
    ``ARCHETYPE_CURVE_TILT`` all key off exactly ``(Path) -> Archetype``.
    Never raises and never blocks on the network — a missing file, a cold
    snapshot cache and a corrupt deck all degrade to ``"midrange"``.
    """
    # Rung 1 — filename hint. High-confidence when present, one regex.
    hint = _filename_hint(deck_path.name)
    if hint:
        return hint

    try:
        deck_text = deck_path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return "midrange"

    # Rung 2 — oracle-backed signals.
    label = _oracle_scan(deck_text)
    if label:
        return label

    # Rung 3 — the v1 card-NAME scan (degradation path).
    winner, _score = _content_scan(dck_utils.main_card_names(deck_text))
    if winner:
        return winner

    # Rung 4 — the honest default.
    return "midrange"
