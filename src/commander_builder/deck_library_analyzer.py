"""Bulk static-analysis of a deck library against Forge card scripts.

Combines:
  - the deck-file iterator (`_iter_main_cards` lives in deck_health
    but the equivalent walker is duplicated here so this module
    stays standalone)
  - the `CardsLoader` that resolves card names to Forge scripts
  - the `forge_script_parser` that turns scripts into ASTs

Output: a `LibraryReport` with:
  - decks scanned (count, names)
  - distinct cards (count, set of names)
  - cards resolved by the Forge corpus vs unresolved (typos /
    custom cards / new Forge sets not yet shipped)
  - per-card-script effect-kind histogram (which `AB$ <Effect>`
    primitives dominate the library — useful for "what would a
    Python engine need to implement first?")
  - keyword histogram (Reach, Trample, Deathtouch, Hexproof, ...)
  - SVar reference counts (which Forge SVar expressions are
    invoked across the library; surfaces analytical patterns)
  - DeckHints frequency (archetype signals at the card level)

Use case: helps "look over decks and work them out" per the
2026-05-19 user request. An audit can spot "every B4 deck in the
library leans on AB$ Token + AB$ Pump" or "Krenko derivatives all
DeckHints:Type$Goblin" and tune the curator's archetype detection
accordingly.

Pure with respect to inputs: same decks + same Forge corpus →
same report. Safe to run repeatedly, cheap to extend with new
aggregations.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .dck_utils import CARD_LINE_RE
from .forge_cards_loader import CardsLoader
from .forge_script_parser import CardScript, parse_card_script


# Canonical ``<qty> <name>[|<set>|<cn>]`` line regex lives in dck_utils
# (a pure leaf that imports nothing project-local, so this module still
# pulls in no audit-side code). Aliased here for the [Commander]/[Main]
# scan below.
_DCK_LINE_RE = CARD_LINE_RE


@dataclass
class DeckCardCounts:
    """Per-deck card counts. Sum across decks for the library total."""
    deck_path: Path
    cards: Counter = field(default_factory=Counter)


@dataclass
class LibraryReport:
    """The aggregate report produced by ``analyze_library``."""
    decks_scanned: int = 0
    distinct_cards: int = 0
    resolved_cards: int = 0
    unresolved_cards: list[str] = field(default_factory=list)
    # Histograms — all Counters keyed by raw Forge token.
    effect_kinds: Counter = field(default_factory=Counter)
    ability_categories: Counter = field(default_factory=Counter)
    keywords: Counter = field(default_factory=Counter)
    svar_names: Counter = field(default_factory=Counter)
    deck_hints: Counter = field(default_factory=Counter)
    deck_has: Counter = field(default_factory=Counter)
    # Per-deck breakdown (handy for "what's in deck X specifically").
    per_deck: list[DeckCardCounts] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-friendly projection. Counters → ordered dicts; Paths → strs.

        Used by the CLI (scripts/analyze_deck_library.py) to emit a
        machine-readable report; also useful in tests for assertion
        ergonomics.
        """
        def _counter_to_dict(c: Counter) -> dict:
            return dict(c.most_common())

        return {
            "decks_scanned": self.decks_scanned,
            "distinct_cards": self.distinct_cards,
            "resolved_cards": self.resolved_cards,
            "unresolved_cards": sorted(self.unresolved_cards),
            "effect_kinds": _counter_to_dict(self.effect_kinds),
            "ability_categories": _counter_to_dict(self.ability_categories),
            "keywords": _counter_to_dict(self.keywords),
            "svar_names": _counter_to_dict(self.svar_names),
            "deck_hints": _counter_to_dict(self.deck_hints),
            "deck_has": _counter_to_dict(self.deck_has),
            "per_deck": [
                {
                    "deck": str(d.deck_path),
                    "cards": dict(d.cards),
                }
                for d in self.per_deck
            ],
        }


def iter_deck_cards(deck_text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(qty, name)`` for every card line in the [Commander]
    and [Main] sections. Metadata and other sections (sideboard,
    considering) are skipped.
    """
    in_relevant_section = False
    for raw in deck_text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            label = s.lower()
            in_relevant_section = label in ("[commander]", "[main]")
            continue
        if not in_relevant_section:
            continue
        m = _DCK_LINE_RE.match(s)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        name = m.group(2).strip()
        if name:
            yield qty, name


def iter_deck_files(deck_dir: Path) -> Iterable[Path]:
    """Sorted iterator over .dck files under ``deck_dir``."""
    yield from sorted(deck_dir.glob("*.dck"))


def _aggregate_card_into_report(
    card_name: str, script: CardScript, report: LibraryReport,
) -> None:
    """Fold one card's parsed script into the running report.

    Counts both the parent face and every alternate face (DFC) so
    a `Boseiju, Who Endures` style card contributes its mana ability
    AND its channel ability to the histograms.
    """
    for face in (script, *script.faces):
        for ability in face.abilities:
            if ability.effect:
                report.effect_kinds[ability.effect] += 1
            if ability.category:
                report.ability_categories[ability.category] += 1
        for kw in face.keywords:
            # Strip parens / parameters — `Channel — {1}{G}` becomes
            # `Channel`, `Cycling {2}` becomes `Cycling`. Keeps the
            # histogram about the *keyword*, not the activation cost.
            kw_token = kw.split(" ", 1)[0].split("(", 1)[0].strip()
            if kw_token:
                report.keywords[kw_token] += 1
        for svar_name in face.svars.keys():
            report.svar_names[svar_name] += 1
        for hint in face.deck_hints:
            report.deck_hints[hint] += 1
        for has in face.deck_has:
            report.deck_has[has] += 1


def analyze_library(
    deck_dir: Path,
    loader: CardsLoader,
    max_decks: Optional[int] = None,
    include_per_deck: bool = False,
) -> LibraryReport:
    """Build a ``LibraryReport`` across every .dck in ``deck_dir``.

    ``loader`` provides Forge card scripts; this function never
    touches the filesystem for cards directly. Pass a
    ``CardsLoader.locate(vendor_forge_path)`` in production or a
    test-fixture loader in tests.

    ``max_decks`` caps the scan for quick smoke runs; ``None`` means
    scan everything. ``include_per_deck`` populates
    ``LibraryReport.per_deck`` — costs O(decks × cards) memory so
    it's off by default; turn on for the "what's in deck X?"
    drill-down.

    Each distinct card is parsed at most once even if it appears
    in many decks. Unresolved cards (Forge doesn't ship a script)
    are recorded in ``unresolved_cards`` for downstream auditing
    (typos vs new-set additions vs custom cards).
    """
    report = LibraryReport()
    distinct: set[str] = set()
    # Cache parsed scripts so a card appearing in 50 decks parses
    # once. Key = card name as it appears in .dck (case-preserved).
    parsed_cache: dict[str, Optional[CardScript]] = {}

    deck_files = list(iter_deck_files(deck_dir))
    if max_decks is not None:
        deck_files = deck_files[:max_decks]

    for deck_path in deck_files:
        try:
            text = deck_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable deck: count as scanned but contribute nothing.
            # Better than aborting the whole analysis on one bad file.
            report.decks_scanned += 1
            continue
        per_deck = DeckCardCounts(deck_path=deck_path) if include_per_deck else None
        for qty, name in iter_deck_cards(text):
            distinct.add(name)
            if per_deck is not None:
                per_deck.cards[name] += qty
        report.decks_scanned += 1
        if per_deck is not None:
            report.per_deck.append(per_deck)

    report.distinct_cards = len(distinct)

    for name in sorted(distinct):
        if name not in parsed_cache:
            raw = loader.load_one(name)
            if raw is None:
                parsed_cache[name] = None
            else:
                parsed_cache[name] = parse_card_script(raw)
        script = parsed_cache[name]
        if script is None:
            report.unresolved_cards.append(name)
        else:
            report.resolved_cards += 1
            _aggregate_card_into_report(name, script, report)

    return report
