"""Archetype classifier — replaces the `_stub_classifier` in pool_curator.

The stub returned `"midrange"` for every deck, which made the diversity rule
in `_split_into_slices` always fail (every pair was same-archetype) so the
one-shot swap fired every time. This module is a heuristic-first replacement
that actually distinguishes the five canonical archetypes.

Classification ladder (cheapest first; first hit wins):

  1. Card-content scan: count cards in the .dck file matching archetype-
     specific keyword sets. Whichever archetype has the highest score and
     clears a minimum threshold wins.
  2. Commander-name heuristic: famous archetype-defining commanders fall
     through to defaults (Edgar Markov → aggro, Yuriko → control, Krenko →
     aggro, etc.).
  3. Deck-name token check: filenames often advertise the strategy
     ("Tribal", "Combo", "Voltron", etc.).
  4. Fall-through default: `"midrange"` (only the *real* midrange decks land
     here, not all of them like the old stub).

Output is one of: `"aggro" | "midrange" | "control" | "combo" | "stax"`.

Phase 2 will add an LLM-based classifier (`claude_archetype()` /
`ollama_archetype()`) that reads the full decklist and reasons about
strategy — much more accurate but token-cost-sensitive. The router lives in
`classify()` so callers don't change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional

from . import dck_utils

Archetype = Literal["aggro", "midrange", "control", "combo", "stax"]


# Per-archetype keyword fingerprints. Tuned against the 6 B3 user decks +
# common reference decks; expect to tighten as we accumulate misclassification
# data in the knowledge log.
#
# Match against either the card name (left of the `|`) or generic strategy
# tokens. Compiled once at import.
_AGGRO_KEYWORDS = re.compile(
    r"\b("
    # Famous aggro / tribal commanders
    r"krenko|edgar markov|isshin|alesha|akiri|adriana|"
    r"hakbal|kumena|king narfi|brion|"
    # Tribal types — expanded per GAP-025 since Hakbal / Edgar / etc. were
    # missed by the prior keyword set. Singular AND plural where common.
    r"goblin|goblins|warrior|warriors|berserker|berserkers|"
    r"samurai|knight|knights|vampire|vampires|"
    r"merfolk|elf|elves|spirit|spirits|"
    r"dragon|dragons|angel|angels|wizard|wizards|"
    r"zombie|zombies|cat|cats|dinosaur|dinosaurs|"
    r"human|humans|elemental|elementals|"
    # Aggressive keywords / ability words on creatures
    r"battle cry|haste|double strike|menace|trample|prowess|"
    r"first strike|attacks each (combat|turn)|whenever .* attacks"
    r")\b",
    re.IGNORECASE,
)
_CONTROL_KEYWORDS = re.compile(
    r"\b("
    r"counter target|return target.*to.*hand|return.*to.*owner.*hand|"
    r"yuriko|teferi|narset|talrand|baral|"
    r"draw a card|scry|surveil|each opponent loses|"
    r"propaganda|ghostly prison|cyclonic rift|farewell"
    r")\b",
    re.IGNORECASE,
)
_COMBO_KEYWORDS = re.compile(
    r"\b("
    r"thassa's oracle|laboratory maniac|jace, wielder|demonic consultation|"
    r"tainted pact|ad nauseam|underworld breach|food chain|"
    r"protean hulk|hermit druid|thoracle|"
    r"infinite|win the game"
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
    r"creatures? matter|tokens?\b|\+1/\+1 counter|landfall|"
    r"tribal|hatebear"
    r")\b",
    re.IGNORECASE,
)

# Filename token patterns (the deck filename often telegraphs the strategy).
_FILENAME_HINTS: list[tuple[re.Pattern[str], Archetype]] = [
    (re.compile(r"\b(combo|storm|consult|breach|hulk)\b", re.IGNORECASE), "combo"),
    (re.compile(r"\b(stax|prison|hatebear|lockdown)\b", re.IGNORECASE), "stax"),
    (re.compile(r"\b(control|counterspell|prison)\b", re.IGNORECASE), "control"),
    (re.compile(r"\b(aggro|tribal|voltron|samurai|warrior)\b", re.IGNORECASE), "aggro"),
]

# Threshold for the keyword-content scan: a deck needs at least this many
# matches in the winning archetype to count. Below this we fall through to
# the next ladder rung.
MIN_CONTENT_MATCHES = 3


def _read_main_card_names(deck_path: Path) -> list[str]:
    """Pull just the card-name portion of every line under [Main]. Strip the
    leading qty and the trailing |SET|CN suffix.

    Thin wrapper over ``dck_utils.main_card_names``."""
    if not deck_path.exists():
        return []
    return dck_utils.main_card_names(deck_path.read_text(encoding="utf-8"))


def _content_scan(card_names: list[str]) -> tuple[Optional[Archetype], int]:
    """Run each archetype's keyword regex against the joined card-name corpus.
    Returns the winning archetype + its match count, or (None, 0) if no
    archetype clears `MIN_CONTENT_MATCHES`."""
    if not card_names:
        return None, 0
    corpus = "\n".join(card_names)
    scores: dict[Archetype, int] = {
        "aggro": len(_AGGRO_KEYWORDS.findall(corpus)),
        "control": len(_CONTROL_KEYWORDS.findall(corpus)),
        "combo": len(_COMBO_KEYWORDS.findall(corpus)),
        "stax": len(_STAX_KEYWORDS.findall(corpus)),
        "midrange": len(_MIDRANGE_KEYWORDS.findall(corpus)),
    }
    winner = max(scores, key=lambda k: scores[k])
    if scores[winner] < MIN_CONTENT_MATCHES:
        return None, scores[winner]
    return winner, scores[winner]


def _filename_hint(deck_filename: str) -> Optional[Archetype]:
    for pattern, archetype in _FILENAME_HINTS:
        if pattern.search(deck_filename):
            return archetype
    return None


def classify(deck_path: Path) -> Archetype:
    """Heuristic archetype classification for one deck.

    Order: filename hint → content scan → midrange fallback. The filename hint
    runs first because user-named decks ("Storm Combo", "Stax Lockdown") are
    high-signal — when present, the user is telling us the strategy directly.
    Content scan is the substantive fallback. Default `"midrange"` only fires
    when neither finds a strong signal."""
    # Filename hint first — high-confidence signal when present.
    hint = _filename_hint(deck_path.name)
    if hint:
        return hint

    # Content scan.
    cards = _read_main_card_names(deck_path)
    winner, _score = _content_scan(cards)
    if winner:
        return winner

    return "midrange"


# --- Future LLM-backed classifiers (stubs, mirroring analyst.py shape) -----

def claude_archetype(deck_path: Path) -> Archetype:
    """LLM-backed archetype call via Claude. NOT IMPLEMENTED.

    To flesh out: build a prompt that includes the commander + first ~30
    cards, ask for one of the 5 archetype labels in JSON, parse the response.
    Cache by deck content hash so re-classification doesn't burn tokens."""
    raise NotImplementedError(
        "Claude archetype classifier not wired yet. Wire when ANTHROPIC_API_KEY "
        "is available; pattern is the same as analyst.claude_verdict."
    )


def ollama_archetype(deck_path: Path) -> Archetype:
    """LLM-backed archetype call via local Ollama. NOT IMPLEMENTED.

    Same shape as `claude_archetype`. Local model trades accuracy for token
    cost — likely fine for archetype classification given the limited label
    space (5 categories)."""
    raise NotImplementedError(
        "Ollama archetype classifier not wired yet."
    )
