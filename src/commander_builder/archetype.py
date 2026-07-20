"""Archetype classifier — replaces the `_stub_classifier` in pool_curator.

The stub returned `"midrange"` for every deck, which made the diversity rule
in `_split_into_slices` always fail (every pair was same-archetype) so the
one-shot swap fired every time. This module is a heuristic-first replacement
that actually distinguishes the five canonical archetypes.

Classification ladder (cheapest first; first hit wins):

  1. Deck-name token check: filenames often advertise the strategy
     ("Tribal", "Combo", "Voltron", etc.).
  2. Card-NAME scan: count cards in the .dck file whose NAMES match
     archetype-specific keyword sets. Whichever archetype has the highest
     score and clears a minimum threshold wins.
  3. Fall-through default: `"midrange"`.

SCOPE / HONESTY NOTE (2026-07 rebalance): classification is NAME-BASED and
COARSE. The scan sees only card names — never oracle text — so the keyword
sets are restricted to things names can actually carry: specific staple /
commander card names ("Winter Orb", "Thassa's Oracle", "Cyclonic Rift") and
tribal nouns ("Goblin", "Vampire"). Earlier versions also listed oracle-text
phrases ("counter target", "draw a card", "+1/+1 counter") that can never
appear in a name, which starved control/combo/midrange scores while the
ubiquitous tribal nouns inflated aggro — most decks skewed "aggro". A deck
that doesn't loudly advertise its strategy in card names lands on the
"midrange" default; that is expected and more honest than a false label.
Real strategy detection needs an oracle-text pipeline (the Phase-2 LLM
classifiers below).

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

Archetype = Literal["aggro", "midrange", "control", "combo", "stax"]


# Per-archetype keyword fingerprints, matched against CARD NAMES ONLY (see
# the module-docstring scope note). Every token here must be something a
# card name can actually contain: a specific card/commander name fragment or
# a tribal noun. Oracle-text phrases ("counter target", "draw a card",
# "+1/+1 counter", "haste", "win the game") were removed in the 2026-07
# rebalance — they can never match a name, so they only created the illusion
# of coverage while aggro's ubiquitous tribal nouns won by default.
# Compiled once at import.
#
# Aggro is split in two: named commanders/staples score like every other
# set, but the tribal NOUNS get their own pattern with a much higher
# threshold (``MIN_TRIBAL_MATCHES``) because words like "dragon" / "angel" /
# "elf" appear in a handful of names in almost ANY deck — only a genuinely
# tribal deck (a dozen-plus same-noun names) should claim aggro on nouns.
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
    # Tribal types — expanded per GAP-025 since Hakbal / Edgar / etc. were
    # missed by the prior keyword set. Singular AND plural where common.
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
    # Control-defining commanders + named pillow-fort / sweep staples.
    r"yuriko|teferi|narset|talrand|baral|"
    r"propaganda|ghostly prison|cyclonic rift|farewell"
    r")\b",
    re.IGNORECASE,
)
_COMBO_KEYWORDS = re.compile(
    r"\b("
    # Named combo pieces. "infinite" stays: real names carry it
    # (Infinite Reflection, Infinite Obliteration, ...).
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
    # "Tribal" IS a real name token (Tribal Forcemage, Tribal Unity). This
    # set is deliberately near-empty after the rebalance: midrange is the
    # ladder's fall-through default, so it doesn't need to win the scan.
    r"tribal"
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

# Separate, much higher bar for aggro's tribal-noun matches: nouns like
# "dragon" / "cat" / "spirit" show up in a few card names of nearly every
# deck, so at threshold 3 they made "aggro" the de-facto default. A real
# tribal deck packs well over a dozen same-tribe names; below this bar the
# noun matches contribute NOTHING to the aggro score (they aren't merely
# down-weighted — a weak tribal smell is no signal at all).
MIN_TRIBAL_MATCHES = 10


def _read_main_card_names(deck_path: Path) -> list[str]:
    """Pull just the card-name portion of every line under [Main]. Strip the
    leading qty and the trailing |SET|CN suffix."""
    if not deck_path.exists():
        return []
    out: list[str] = []
    in_main = False
    for raw in deck_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() == "[main]":
            in_main = True
            continue
        if line.startswith("[") and line.endswith("]"):
            in_main = False
            continue
        if in_main:
            m = re.match(r"^\d+\s+(.+?)(?:\|.*)?$", line)
            if m:
                out.append(m.group(1).strip())
    return out


def _content_scan(card_names: list[str]) -> tuple[Optional[Archetype], int]:
    """Run each archetype's keyword regex against the joined card-NAME corpus.
    Returns the winning archetype + its match count, or (None, 0) if no
    archetype clears `MIN_CONTENT_MATCHES`."""
    if not card_names:
        return None, 0
    corpus = "\n".join(card_names)
    # Tribal nouns are gated separately: below MIN_TRIBAL_MATCHES they add
    # zero (a goodstuff deck with a Shivan Dragon and two Angels is not an
    # aggro deck); at or above it the full count folds into aggro's score
    # so a true tribal deck outscores everything else decisively.
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
