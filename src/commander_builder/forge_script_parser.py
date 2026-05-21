"""Parse Forge's card-script DSL into a structured AST.

First slice of FP-001 (Python-native MTG engine, see
docs/AGENT_BACKLOG.md item #015 + STATUS.md parked plans). This
module is **read-only** and does NOT interpret abilities — it just
turns Forge's ``.txt`` card-script files into Python dataclasses
that downstream code can analyze. Even if we never write a full
engine, this enables:

  - Static analysis (e.g. "how many cards in our 7,244-card library
    use the ``AB$ Token`` effect?")
  - Better auditing tools (compare oracle text vs DSL semantics
    to catch errata drift)
  - The foundation for any future Python-native engine

Scope intentionally bounded:

  - Parses ONE card-script file at a time.
  - Recognizes Name / ManaCost / Types / PT / Loyalty / K: / A: /
    T: / R: / S: / SVar: / DeckHints / DeckHas / AlternateMode /
    Oracle lines.
  - Splits A:/T:/R:/S: values on ``|`` and parses ``Key$ Value``
    pairs into a dict.
  - Recognizes DFC (``AlternateMode:DoubleFaced``) and parses both
    faces into ``CardScript.faces``.
  - Malformed lines emit a warning via the optional ``warn``
    callback (default: silently include the raw line in
    ``raw_unparsed_lines``) — we don't crash on tomorrow's
    DSL extension.

NOT in scope:

  - SVar expansion (``Count$Valid Goblin.YouCtrl`` stays a string;
    interpreting it requires a game state).
  - Ability execution (no rules engine here).
  - Cost-string parsing (``1 G G`` stays a string).
  - Cross-face references (handled at parser level: faces are
    parsed into separate CardScript instances).

DSL reference (extracted from Forge's ``forge-gui/res/cardsfolder``
on 2026-05-19 — 32,626 cards, 129 distinct AB$ effect kinds):

  Name:Krenko, Mob Boss
  ManaCost:2 R R
  Types:Legendary Creature Goblin Warrior
  PT:3/3
  A:AB$ Token | Cost$ T | TokenAmount$ X | TokenScript$ r_1_1_goblin
  SVar:X:Count$Valid Goblin.YouCtrl
  Oracle:{T}: Create X 1/1 red Goblin creature tokens...

Key$ Value separator is exactly ``$`` (dollar sign). Pipe ``|`` is
the argument separator within one ability line. The first pair on
every A/T/R/S line is the effect (e.g. ``AB$ Token``); its key is
the category (AB / SP / DB) and its value is the effect kind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# Pattern used to split `Key$ Value` pairs inside one ability line.
# Whitespace around `$` is permitted (Forge scripts vary). Capture
# group 1 = key (single token), group 2 = value (whatever's after
# the dollar sign up to the next | separator, stripped of leading
# whitespace).
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*\$\s*(.*?)\s*$")


@dataclass
class Ability:
    """One ability-bearing line (``A:`` / ``T:`` / ``R:`` / ``S:``).

    ``kind`` is the line prefix (``A``, ``T``, ``R``, ``S``).
    ``category`` is the first Key$ pair's key — ``AB`` (activated),
    ``SP`` (spell), ``DB`` (sub-ability, only appears inside SVar
    expansions but normalized here for symmetry), or empty for
    non-activated lines whose first pair uses a different shape
    (e.g. ``T:Mode$ ChangesZone | ...`` has category ``Mode`` —
    we store it as-is so callers can dispatch).
    ``effect`` is the first Key$ pair's value (``Mana``, ``Token``,
    ``Pump``, ``ChangesZone``, etc.).
    ``params`` is every Key$ Value pair on the line, including
    the first one. Values are stored as the raw string Forge
    emitted; SVar references stay symbolic.
    ``raw`` is the original line for diagnostics.
    """
    kind: str
    category: str
    effect: str
    params: dict[str, str] = field(default_factory=dict)
    raw: str = ""


@dataclass
class CardScript:
    """Parsed card-script AST.

    Faces are parsed recursively: a DFC like Bala Ged Recovery has
    the spell-face on the parent CardScript and the land-face on
    ``faces[0]``. Single-face cards have an empty ``faces`` list.
    """
    name: str = ""
    mana_cost: Optional[str] = None
    types: list[str] = field(default_factory=list)
    pt: Optional[tuple[str, str]] = None
    loyalty: Optional[int] = None
    defense: Optional[int] = None  # Battle cards
    keywords: list[str] = field(default_factory=list)
    abilities: list[Ability] = field(default_factory=list)
    svars: dict[str, str] = field(default_factory=dict)
    oracle: str = ""
    deck_hints: list[str] = field(default_factory=list)
    deck_has: list[str] = field(default_factory=list)
    alternate_mode: Optional[str] = None
    faces: list["CardScript"] = field(default_factory=list)
    # Lines the parser couldn't fit into the structured fields above.
    # Inspect this in tests to spot DSL features we haven't modeled
    # yet — empty means "fully understood".
    raw_unparsed_lines: list[str] = field(default_factory=list)

    @property
    def is_creature(self) -> bool:
        return "Creature" in self.types

    @property
    def is_land(self) -> bool:
        return "Land" in self.types

    @property
    def is_dfc(self) -> bool:
        return bool(self.faces) or (self.alternate_mode or "").lower() == "doublefaced"


def _parse_pt(value: str) -> Optional[tuple[str, str]]:
    """``PT:2/2`` → (``2``, ``2``). Returns None on parse failure.

    Values stay as strings because Forge allows ``*`` and ``*+1``
    for variable P/T (e.g. Tarmogoyf, Death's Shadow). Callers that
    need numeric coercion can handle the symbolic case themselves.
    """
    raw = value.strip()
    if "/" not in raw:
        return None
    p, t = raw.split("/", 1)
    return p.strip(), t.strip()


def _parse_ability_line(prefix: str, value: str) -> Ability:
    """Split one ability line into a structured ``Ability``.

    ``value`` is the part AFTER ``A:`` / ``T:`` / etc. — the leading
    prefix has already been stripped. The first Key$ pair's key
    becomes the category, its value becomes the effect.
    """
    segments = [s for s in (s.strip() for s in value.split("|")) if s]
    params: dict[str, str] = {}
    category = ""
    effect = ""
    for i, segment in enumerate(segments):
        m = _KEY_VALUE_RE.match(segment)
        if not m:
            # Segment doesn't fit Key$ Value — preserve it under a
            # synthetic key so it's recoverable, but don't crash.
            params[f"_unparsed_{i}"] = segment
            continue
        key, val = m.group(1), m.group(2)
        params[key] = val
        if i == 0:
            category = key
            effect = val
    return Ability(
        kind=prefix,
        category=category,
        effect=effect,
        params=params,
        raw=value,
    )


def _parse_face_block(
    lines: list[str], warn: Optional[Callable[[str], None]] = None,
) -> CardScript:
    """Parse a single face's block of lines into a CardScript.

    ``lines`` is a list of stripped, non-empty lines that belong
    to ONE face (the caller splits multi-face scripts at the
    AlternateMode marker before invoking this).
    """
    face = CardScript()
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line:
            continue
        if line.startswith("#"):
            # Forge scripts occasionally contain ``#`` comments —
            # not part of the DSL but tolerated.
            continue

        # Top-level Key:Value parse; rest of the line is the value.
        if ":" not in line:
            face.raw_unparsed_lines.append(line)
            if warn:
                warn(f"line without ':' separator: {line!r}")
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        # Don't strip the value's leading whitespace for Oracle —
        # Oracle text occasionally starts with a space-separated
        # cost token and we want to preserve it byte-for-byte.
        value = value if key == "Oracle" else value.strip()

        if key == "Name":
            face.name = value
        elif key == "ManaCost":
            # Forge uses literal "no cost" for lands / costless cards.
            face.mana_cost = value if value and value.lower() != "no cost" else None
        elif key == "Types":
            face.types = value.split()
        elif key == "PT":
            face.pt = _parse_pt(value)
        elif key == "Loyalty":
            try:
                face.loyalty = int(value)
            except ValueError:
                face.raw_unparsed_lines.append(line)
        elif key == "Defense":
            try:
                face.defense = int(value)
            except ValueError:
                face.raw_unparsed_lines.append(line)
        elif key == "K":
            face.keywords.append(value)
        elif key in ("A", "T", "R", "S"):
            face.abilities.append(_parse_ability_line(key, value))
        elif key == "SVar":
            # ``SVar:Name:Body`` — split the rest on the SECOND colon.
            if ":" in value:
                svar_name, _, svar_body = value.partition(":")
                face.svars[svar_name.strip()] = svar_body.strip()
            else:
                face.raw_unparsed_lines.append(line)
                if warn:
                    warn(f"SVar line missing body: {line!r}")
        elif key == "DeckHints":
            face.deck_hints.append(value)
        elif key == "DeckHas":
            face.deck_has.append(value)
        elif key == "AlternateMode":
            face.alternate_mode = value
        elif key == "Oracle":
            face.oracle = value
        else:
            # Recognized line shape (key:value) but unknown key.
            # Capture so the caller can audit what we're missing —
            # the test fixture's "no unparsed lines" assertion is
            # the early-warning system for DSL drift.
            face.raw_unparsed_lines.append(line)
            if warn:
                warn(f"unknown DSL key {key!r} on line: {line!r}")

    return face


def parse_card_script(
    text: str, warn: Optional[Callable[[str], None]] = None,
) -> CardScript:
    """Parse a card-script blob (the full file contents as a string).

    Splits at ``AlternateMode:`` markers so DFCs get their faces
    parsed into separate CardScript instances under
    ``CardScript.faces``. Single-face cards are returned with
    ``faces == []``.

    ``warn`` is invoked once per unparseable line if provided —
    useful for catching DSL drift in tests via a list-collecting
    callback. The parser never raises on malformed input; bad lines
    land in ``raw_unparsed_lines``.
    """
    # Split into per-face blocks. ``AlternateMode:`` is the delimiter
    # Forge uses for DFCs; each face starts with its own ``Name:``.
    blocks: list[list[str]] = [[]]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("AlternateMode:"):
            # AlternateMode itself belongs to the CURRENT face (so the
            # parent face records the mode); the NEXT face begins on
            # the following line.
            blocks[-1].append(line)
            blocks.append([])
            continue
        blocks[-1].append(line)

    if not blocks or not blocks[0]:
        return CardScript()

    parent = _parse_face_block(blocks[0], warn=warn)
    for face_lines in blocks[1:]:
        if not face_lines:
            continue
        parent.faces.append(_parse_face_block(face_lines, warn=warn))
    return parent


def parse_card_script_file(
    path: Path, warn: Optional[Callable[[str], None]] = None,
) -> CardScript:
    """Read ``path`` and parse it. Defers to ``parse_card_script``.

    UTF-8 with replacement so a stray encoding glitch (Forge ships
    cards from sets with non-ASCII names like Kaladesh's ``Vraska``
    variants, occasionally em-dashes in Oracle text) doesn't crash
    the parser. Encoding errors land in the result text but parse
    correctly because the DSL itself is ASCII-only.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_card_script(text, warn=warn)
