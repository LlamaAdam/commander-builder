"""Compare Forge's ``Oracle:`` text against Scryfall's
``oracle_text`` to surface errata drift.

Pattern bucketing is data-driven (#020): the default rule set
lives at ``src/commander_builder/data/oracle_diff_buckets.json``
and the CLI accepts ``--bucket-rules <path>`` to override. Each
rule is a dict with ``label`` plus any combination of:
  - ``scryfall_contains``     (substring required in Scryfall side)
  - ``scryfall_not_contains`` (substring forbidden in Scryfall side)
  - ``forge_contains``        (required in Forge side)
  - ``forge_not_contains``    (forbidden in Forge side)
All keys are AND'd; the bucket walker assigns the first matching
label and otherwise returns ``other``. Match is case-insensitive
substring; regex would be a follow-up if simple substring proves
insufficient.

The problem this catches: WotC ships errata updates roughly
quarterly (oracle-text refinements that change how a card resolves
under tournament rules). Scryfall updates its API within days.
Forge updates whenever the bundled card-script corpus refreshes,
which can lag by a release cycle or two. Sims that run against
stale Forge scripts produce wrong verdicts long before anyone
notices.

Examples surfaced on a 2026-05-19 spot-check:

  Underground River (errata: ``Underground River`` → ``This land``)
    Forge:    "...Add {U} or {B}. Underground River deals 1 damage to you."
    Scryfall: "...Add {U} or {B}. This land deals 1 damage to you."

This module produces a structured diff per card. No interpretation,
no auto-correction — the report is for human review (the maintainer
decides whether to refresh the Forge corpus, accept a known-stale
text, or whitelist a card whose Forge variant is deliberate).

Used by ``scripts/oracle_diff_report.py``; pure-helper friendly so
downstream tooling (or a future audit dashboard) can compose it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path
from typing import Optional

from .forge_script_parser import CardScript


DEFAULT_BUCKET_RULES_PATH = (
    Path(__file__).parent / "data" / "oracle_diff_buckets.json"
)


# Forge's Oracle text uses LITERAL ``\n`` (two characters,
# backslash + n) as the paragraph separator inside the .txt file.
# Scryfall returns actual newline characters. Normalize to the
# Scryfall form before comparing.
_FORGE_NL_RE = re.compile(r"\\n")

# Forge oracle text uses CARDNAME and NICKNAME as placeholders that
# get substituted at render time. Scryfall always has the actual
# card name spelled out. Replace before comparing.
_PLACEHOLDER_TOKENS = ("CARDNAME", "NICKNAME")

# Collapse multiple whitespace runs to a single space. Both Forge
# and Scryfall are inconsistent about trailing whitespace and
# multi-space gaps between sentences.
_WHITESPACE_RE = re.compile(r"[ \t]+")

# Scryfall uses the actual Unicode minus sign (U+2212) in
# planeswalker loyalty costs (e.g. ``−2: ...``). Forge uses ASCII
# hyphen-minus (``-2: ...``) in its card scripts. Both render the
# same to a player; the difference is cosmetic and drowns out real
# errata-drift signal otherwise. Normalize to ASCII hyphen.
_UNICODE_MINUS = "−"

# Forge wraps planeswalker loyalty costs in square brackets
# (``[-2]: ...``, ``[+1]: ...``, ``[0]: ...``) while Scryfall emits
# bare (``-2: ...``, ``+1: ...``, ``0: ...``). Pure rendering
# convention; strip the brackets so the comparison focuses on
# substantive text.
_PW_LOYALTY_RE = re.compile(r"\[([-+]?\d+)\]")


@dataclass
class OracleDiffResult:
    """One card's oracle-text comparison.

    ``match`` is the high-signal boolean. ``normalized_forge`` and
    ``normalized_scryfall`` carry the two strings AFTER normalization
    so the caller can render a side-by-side or unified diff for
    human review.
    """
    card_name: str
    match: bool
    # Status is one of:
    #   "match"        — normalized texts are identical
    #   "differ"       — both sides have text but normalized values diverge
    #   "missing_forge" — Forge has no Oracle field for this card
    #   "missing_scryfall" — Scryfall didn't return oracle_text for this card
    #   "missing_both" — neither source provided oracle text
    status: str = "match"
    normalized_forge: str = ""
    normalized_scryfall: str = ""
    raw_forge: str = ""
    raw_scryfall: str = ""
    diff_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "card_name": self.card_name,
            "match": self.match,
            "status": self.status,
            "normalized_forge": self.normalized_forge,
            "normalized_scryfall": self.normalized_scryfall,
            "raw_forge": self.raw_forge,
            "raw_scryfall": self.raw_scryfall,
            "diff_lines": list(self.diff_lines),
        }


def normalize_oracle(text: str, card_name: Optional[str] = None) -> str:
    """Bring Forge / Scryfall oracle text into a common canonical form
    suitable for equality comparison.

    Operations:
      1. Replace literal ``\\n`` (Forge's source-file convention) with
         actual ``\n``.
      2. Substitute ``CARDNAME`` / ``NICKNAME`` placeholders with the
         supplied ``card_name`` (Forge uses both interchangeably).
      3. Strip trailing whitespace on each line.
      4. Collapse runs of spaces/tabs to a single space.
      5. Strip leading + trailing whitespace from the whole blob.

    NOT done here:
      - Punctuation normalization (em-dash vs hyphen, curly vs
        straight quotes). Could be added if a noisy diff demands it,
        but for now the raw Unicode rides.
      - Reminder-text stripping. Scryfall doesn't include reminder
        text in oracle_text by default; Forge sometimes does. When
        this becomes a common false-positive we can add a
        ``strip_reminder`` flag.
    """
    if not text:
        return ""
    # 1. Unescape Forge's literal \n.
    out = _FORGE_NL_RE.sub("\n", text)
    # 2. Placeholder substitution.
    if card_name:
        for token in _PLACEHOLDER_TOKENS:
            out = out.replace(token, card_name)
    # 3. Strip trailing whitespace per line.
    out = "\n".join(line.rstrip() for line in out.split("\n"))
    # 4. Collapse intra-line whitespace runs.
    out = _WHITESPACE_RE.sub(" ", out)
    # 5. Normalize Unicode minus → ASCII hyphen (planeswalker
    #    loyalty costs only differ by this; not a real errata signal).
    out = out.replace(_UNICODE_MINUS, "-")
    # 6. Strip Forge's bracket convention on planeswalker loyalty
    #    costs (``[-2]`` → ``-2``).
    out = _PW_LOYALTY_RE.sub(r"\1", out)
    # 7. Strip blob edges.
    return out.strip()


def _extract_scryfall_oracle(scryfall_data: dict) -> str:
    """Pull the oracle text out of a Scryfall card payload.

    For DFCs Scryfall puts the per-face text in ``card_faces[].oracle_text``
    with the top-level ``oracle_text`` empty. We concatenate face
    oracles with a sentinel separator (``//``) so the structure
    matches what Forge stores as one blob (two ``Name:`` blocks
    with two ``Oracle:`` lines).

    Falls back to the top-level ``oracle_text`` for single-face cards.
    """
    top = (scryfall_data or {}).get("oracle_text") or ""
    if top:
        return top
    faces = (scryfall_data or {}).get("card_faces") or []
    face_oracles = [
        (f or {}).get("oracle_text") or "" for f in faces
    ]
    face_oracles = [o for o in face_oracles if o]
    if not face_oracles:
        return ""
    return "\n//\n".join(face_oracles)


def _extract_forge_oracle(forge_card: CardScript) -> str:
    """Pull oracle text out of a parsed Forge ``CardScript``.

    DFCs have their second face's Oracle on ``faces[0].oracle``;
    concatenate with the same ``//`` sentinel
    ``_extract_scryfall_oracle`` uses so the two normalized blobs
    line up.
    """
    parent = forge_card.oracle or ""
    if not forge_card.faces:
        return parent
    face_oracles = [parent] + [f.oracle for f in forge_card.faces if f.oracle]
    face_oracles = [o for o in face_oracles if o]
    return "\n//\n".join(face_oracles)


def compare_card_oracle(
    card_name: str,
    forge_card: Optional[CardScript],
    scryfall_data: Optional[dict],
) -> OracleDiffResult:
    """Build a structured diff between Forge + Scryfall oracle text
    for one card.

    Either input may be None (Forge doesn't ship a script for the
    card, or Scryfall couldn't be reached). Such cases get a
    ``missing_*`` status; the caller decides whether to surface or
    suppress them in the report.
    """
    raw_forge = _extract_forge_oracle(forge_card) if forge_card else ""
    raw_scryfall = _extract_scryfall_oracle(scryfall_data) if scryfall_data else ""

    if not raw_forge and not raw_scryfall:
        return OracleDiffResult(
            card_name=card_name, match=False, status="missing_both",
        )
    if not raw_forge:
        return OracleDiffResult(
            card_name=card_name, match=False, status="missing_forge",
            raw_scryfall=raw_scryfall,
            normalized_scryfall=normalize_oracle(raw_scryfall, card_name),
        )
    if not raw_scryfall:
        return OracleDiffResult(
            card_name=card_name, match=False, status="missing_scryfall",
            raw_forge=raw_forge,
            normalized_forge=normalize_oracle(raw_forge, card_name),
        )

    nforge = normalize_oracle(raw_forge, card_name)
    nscryfall = normalize_oracle(raw_scryfall, card_name)
    if nforge == nscryfall:
        return OracleDiffResult(
            card_name=card_name, match=True, status="match",
            raw_forge=raw_forge, raw_scryfall=raw_scryfall,
            normalized_forge=nforge, normalized_scryfall=nscryfall,
        )

    # Mismatch: generate a unified diff so a human reviewer can see
    # what changed at a glance.
    diff_lines = list(unified_diff(
        nscryfall.splitlines(),
        nforge.splitlines(),
        fromfile=f"scryfall:{card_name}",
        tofile=f"forge:{card_name}",
        lineterm="",
        n=2,
    ))
    return OracleDiffResult(
        card_name=card_name, match=False, status="differ",
        raw_forge=raw_forge, raw_scryfall=raw_scryfall,
        normalized_forge=nforge, normalized_scryfall=nscryfall,
        diff_lines=diff_lines,
    )


# ---------------------------------------------------------------------------
# Pattern bucketing (AGENT_BACKLOG #020)
# ---------------------------------------------------------------------------

@dataclass
class DiffBucket:
    """One entry from the bucket-rules JSON. ``matches(result)``
    returns True iff every configured ``*_contains`` substring is
    present and every ``*_not_contains`` substring is absent."""
    label: str
    scryfall_contains: Optional[str] = None
    scryfall_not_contains: Optional[str] = None
    forge_contains: Optional[str] = None
    forge_not_contains: Optional[str] = None

    def matches(self, result: "OracleDiffResult") -> bool:
        scryfall = result.normalized_scryfall.lower()
        forge = result.normalized_forge.lower()
        if self.scryfall_contains and self.scryfall_contains.lower() not in scryfall:
            return False
        if self.scryfall_not_contains and self.scryfall_not_contains.lower() in scryfall:
            return False
        if self.forge_contains and self.forge_contains.lower() not in forge:
            return False
        if self.forge_not_contains and self.forge_not_contains.lower() in forge:
            return False
        # At least one constraint must be set, else every diff matches
        # and bucketing collapses. Guard so a typo'd rule (all None)
        # doesn't silently swallow everything.
        if not any((
            self.scryfall_contains, self.scryfall_not_contains,
            self.forge_contains, self.forge_not_contains,
        )):
            return False
        return True


def load_diff_buckets(path: Optional[Path] = None) -> list[DiffBucket]:
    """Read the bucket-rules JSON file (default: shipped data file).

    Returns the ordered list of ``DiffBucket`` rules. Schema is:

      {
        "$schema_version": 1,
        "buckets": [
          {"label": "...", "scryfall_contains": "...", ...},
          ...
        ]
      }

    Unknown fields per bucket are ignored so a future schema
    extension doesn't break older code. A missing ``label`` is a
    fatal config error (no sensible default).
    """
    if path is None:
        path = DEFAULT_BUCKET_RULES_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_buckets = payload.get("buckets") or []
    out: list[DiffBucket] = []
    for entry in raw_buckets:
        label = entry.get("label")
        if not label:
            raise ValueError(
                f"bucket rule missing 'label' in {path}: {entry!r}"
            )
        out.append(DiffBucket(
            label=label,
            scryfall_contains=entry.get("scryfall_contains"),
            scryfall_not_contains=entry.get("scryfall_not_contains"),
            forge_contains=entry.get("forge_contains"),
            forge_not_contains=entry.get("forge_not_contains"),
        ))
    return out


def categorize_diff(
    result: OracleDiffResult, buckets: list[DiffBucket],
) -> str:
    """Walk ``buckets`` in order, return the first matching bucket's
    label. Returns ``other`` when nothing matches — the genuinely
    interesting "real edge case" diffs that aren't one of the known
    sweep patterns.
    """
    for bucket in buckets:
        if bucket.matches(result):
            return bucket.label
    return "other"
