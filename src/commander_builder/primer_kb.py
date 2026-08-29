"""FP-019.1 — the bundled primer knowledge base.

WHAT THIS IS
============
A read-only loader over ``data/primer_kb.json`` — 40 community primers
(21 Moxfield top-liked + 19 Archidekt, harvested 2026-08-29) distilled
into per-deck structured records: gameplan, construction rules, mulligan
criteria, sequencing notes, win lines *verified against each deck's
exact mainboard*, budget swaps, and generalizable heuristics. The
cross-primer synthesis these records support lives at
``primer_harvest/deckbuilding_heuristics.md`` (§-references throughout
FP-019 cite it).

PROFILES, NOT ONE TRUTH (§13)
=============================
Where the harvest holds two primers for one commander (Gitrog $50 vs
cEDH, Winota budget-aggro vs cEDH-stax) they *disagree by context* and
both are right. So the unit here is the :class:`PrimerProfile` — one
author's build — and :func:`profiles_for_commander` returns ALL of them.
Consumers must never collapse profiles into a single consensus record;
surface the spread instead.

TRUST BOUNDARY
==============
Card names in ``win_lines[].cards`` / ``key_cards`` / budget swaps were
extracted from primer prose by an LLM pass and then VERIFIED against the
deck's exact mainboard where possible — the per-card ``verified`` flags
record the outcome. A ``verified=False`` name is an author's prose
mention that did not resolve against the list: treat it as a hint, never
as a card fact (same doctrine as ``primer.py``'s card-link rule).

DESIGN CONTRACT (mirrors combos/game_changers loaders)
======================================================
* **Offline, stdlib only.** One bundled JSON, no network, no refresh
  path yet (§16 names the future harvest: changelogs and
  notable-exclusion sections).
* **Fail-quiet, never fabricate.** Missing file, corrupt JSON, or a
  malformed record yields an empty/partial result — a broken KB must
  degrade the advisor's context, not crash an audit.
* **Immutable.** Frozen dataclasses and tuples; the module-level cache
  hands every caller the same objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PRIMER_KB_PATH = Path(__file__).parent / "data" / "primer_kb.json"

#: Slack allowed past a prompt-block cap for the truncation marker line
#: (kept as a module constant so tests can assert the clip contract
#: without re-deriving ``primer.clip_for_prompt``'s marker length).
_CLIP_MARKER_ALLOWANCE = 80


@dataclass(frozen=True)
class WinLine:
    """One explicit win line: the cards, what they need, verification."""

    cards: tuple[str, ...]
    needs: str = ""
    note: str = ""
    verified: tuple[bool, ...] = ()

    @property
    def all_verified(self) -> bool:
        """True when every named card resolved against the mainboard."""
        return bool(self.cards) and len(self.verified) == len(self.cards) \
            and all(self.verified)


@dataclass(frozen=True)
class BudgetSwap:
    """A function-preserving budget swap with the author's reason (§10)."""

    out_card: str
    in_card: str
    reason: str = ""
    commander: str = ""
    url: str = ""


@dataclass(frozen=True)
class PrimerProfile:
    """One author's build of one commander — the KB's unit of truth."""

    id: str
    name: str
    url: str
    commanders: tuple[str, ...]
    source: str = ""
    color_identity: str = ""
    archetype: str = ""
    bracket: str = ""
    gameplan: str = ""
    construction_rules: tuple[tuple[str, str], ...] = ()
    mulligan_keep: tuple[str, ...] = ()
    mulligan_mull: tuple[str, ...] = ()
    sequencing: tuple[str, ...] = ()
    win_lines: tuple[WinLine, ...] = ()
    weaknesses: tuple[str, ...] = ()
    key_cards: tuple[str, ...] = ()
    budget_swaps: tuple[BudgetSwap, ...] = ()
    heuristics: tuple[str, ...] = ()


def _str_tuple(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v)


def _parse_win_line(raw) -> Optional[WinLine]:
    if not isinstance(raw, dict):
        return None
    cards = _str_tuple(raw.get("cards"))
    if not cards:
        return None
    verified_raw = raw.get("verified")
    verified = tuple(bool(v) for v in verified_raw) \
        if isinstance(verified_raw, list) else ()
    return WinLine(
        cards=cards,
        needs=str(raw.get("needs") or ""),
        note=str(raw.get("note") or ""),
        verified=verified,
    )


def _parse_profile(raw) -> Optional[PrimerProfile]:
    """One KB record → PrimerProfile, or None when structurally unusable.

    'Unusable' means no id/name/commanders — everything else degrades to
    empty fields, because a partial profile still carries signal."""
    if not isinstance(raw, dict):
        return None
    commanders = _str_tuple(raw.get("commanders"))
    if not (raw.get("id") and raw.get("name") and commanders):
        return None

    construction = raw.get("construction")
    construction_rules: tuple[tuple[str, str], ...] = ()
    if isinstance(construction, dict):
        construction_rules = tuple(
            (str(k), str(v)) for k, v in construction.items()
            if isinstance(v, str) and v
        )

    mull = raw.get("mulligan") if isinstance(raw.get("mulligan"), dict) else {}

    swaps = []
    for s in raw.get("budget_swaps") or []:
        if isinstance(s, dict) and s.get("out") and s.get("in"):
            swaps.append(BudgetSwap(
                out_card=str(s["out"]), in_card=str(s["in"]),
                reason=str(s.get("reason") or ""),
                commander=commanders[0], url=str(raw.get("url") or ""),
            ))

    win_lines = tuple(
        w for w in (_parse_win_line(x) for x in raw.get("win_lines") or [])
        if w is not None
    )

    return PrimerProfile(
        id=str(raw["id"]),
        name=str(raw["name"]),
        url=str(raw.get("url") or ""),
        commanders=commanders,
        source=str(raw.get("source") or ""),
        color_identity=str(raw.get("color_identity") or ""),
        archetype=str(raw.get("archetype") or ""),
        bracket=str(raw.get("bracket") or ""),
        gameplan=str(raw.get("gameplan") or ""),
        construction_rules=construction_rules,
        mulligan_keep=_str_tuple(mull.get("keep")),
        mulligan_mull=_str_tuple(mull.get("mull")),
        sequencing=_str_tuple(raw.get("sequencing")),
        win_lines=win_lines,
        weaknesses=_str_tuple(raw.get("weaknesses")),
        key_cards=_str_tuple(raw.get("key_cards")),
        budget_swaps=tuple(swaps),
        heuristics=_str_tuple(raw.get("heuristics")),
    )


# One parsed tuple per path — the KB is static data, so a byte re-read
# per audit would be pure waste. Tests that write a new file use a new
# tmp path; same-path mutation deliberately does NOT invalidate.
_CACHE: dict[Path, tuple[PrimerProfile, ...]] = {}


def load_profiles(path: Optional[Path] = None) -> tuple[PrimerProfile, ...]:
    """All KB profiles, parsed and cached. Empty tuple on any failure."""
    kb_path = Path(path) if path is not None else PRIMER_KB_PATH
    cached = _CACHE.get(kb_path)
    if cached is not None:
        return cached
    try:
        raw = json.loads(kb_path.read_text(encoding="utf-8"))
        decks = raw.get("decks") if isinstance(raw, dict) else None
        profiles = tuple(
            p for p in (_parse_profile(d) for d in decks or [])
            if p is not None
        )
    except (OSError, ValueError):
        profiles = ()
    _CACHE[kb_path] = profiles
    return profiles


def _commander_matches(query: str, commander: str) -> bool:
    q = query.strip().casefold()
    c = commander.strip().casefold()
    # A DFC commander may be recorded by its front face ("Kefka, Court
    # Mage // Kefka, Ruler of Ruin") — match either face too.
    return q == c or q in [f.strip() for f in c.split("//")]


def profiles_for_commander(
    name: str, profiles: Optional[tuple[PrimerProfile, ...]] = None,
) -> tuple[PrimerProfile, ...]:
    """Every profile whose commander (or either partner / DFC face)
    matches ``name``, case-insensitively. Multiple results are the
    normal case for popular commanders — see PROFILES, NOT ONE TRUTH."""
    pool = load_profiles() if profiles is None else profiles
    return tuple(
        p for p in pool
        if any(_commander_matches(name, c) for c in p.commanders)
    )


def budget_swap_table(
    profiles: Optional[tuple[PrimerProfile, ...]] = None,
) -> tuple[BudgetSwap, ...]:
    """All budget swaps across the KB, each tagged with its commander
    and source URL — the §10 function-preserving swap corpus for the
    advisor's budget mode."""
    pool = load_profiles() if profiles is None else profiles
    return tuple(s for p in pool for s in p.budget_swaps)


def _render_profile(p: PrimerProfile) -> str:
    lines = [f"## {p.name} ({' / '.join(p.commanders)})"
             + (f" — bracket {p.bracket}" if p.bracket else "")]
    if p.archetype:
        lines.append(f"Archetype: {p.archetype}")
    if p.gameplan:
        lines.append(f"Gameplan: {p.gameplan}")
    for key, rule in p.construction_rules:
        lines.append(f"Build rule ({key}): {rule}")
    if p.mulligan_keep:
        lines.append("Keep: " + "; ".join(p.mulligan_keep))
    if p.mulligan_mull:
        lines.append("Mull: " + "; ".join(p.mulligan_mull))
    for w in p.win_lines:
        flag = "" if w.all_verified else " [unverified names]"
        lines.append(f"Win line: {' + '.join(w.cards)} — {w.needs}{flag}")
    if p.weaknesses:
        lines.append("Weak to: " + "; ".join(p.weaknesses))
    return "\n".join(lines)


def prompt_block_for_commander(name: str, cap: Optional[int] = None) -> str:
    """A compact text block of every KB profile for ``name``, for LLM
    prompt context (FP-019.6). Clipped through ``primer.clip_for_prompt``
    so no prompt builder invents its own silent truncation. Empty string
    when the KB has nothing — callers emit no block at all."""
    matches = profiles_for_commander(name)
    if not matches:
        return ""
    text = "\n\n".join(_render_profile(p) for p in matches)
    # Local import: primer.py imports nothing from here, but keep the
    # edge one-directional and lazy so a KB consumer that never renders
    # prompts doesn't pull the primer module in.
    from .primer import clip_for_prompt

    if cap is None:
        return clip_for_prompt(text)
    return clip_for_prompt(text, cap=cap)
