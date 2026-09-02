"""FP-019.1 — the bundled primer knowledge base.

WHAT THIS IS
============
A read-only loader over ``data/primer_kb.json`` — 40 community primers
(21 Moxfield top-liked + 19 Archidekt, harvested 2026-08-29) distilled
into per-deck structured records: gameplan, construction rules, mulligan
criteria, sequencing notes, author-described lines whose card names were
checked against each deck's harvested mainboard, budget swaps, and generalizable
heuristics. Rules text and combo claims are a separate provenance axis. The
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
extracted from primer prose by an LLM pass and then checked against the
deck's exact mainboard where possible. The legacy JSON ``verified`` array
records only that harvested-mainboard presence, exposed as ``cards_present``.
It does NOT verify card rules or prove that a described interaction wins.
``rules_status`` carries that separate provenance. A false presence flag
is an author's prose mention that did not resolve against the list: treat
it as a hint, never as a card fact (same doctrine as ``primer.py``'s
card-link rule).

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

_RULES_STATUSES = frozenset({
    "author_claimed",
    "conditional",
    "engine",
    "rules_verified",
})


@dataclass(frozen=True)
class WinLine:
    """One author-described line with separate name/rules provenance."""

    cards: tuple[str, ...]
    needs: str = ""
    note: str = ""
    verified: tuple[bool, ...] = ()
    rules_status: str = "author_claimed"

    @property
    def cards_present(self) -> tuple[bool, ...]:
        """Clear name for legacy ``verified`` harvested-mainboard flags."""
        return self.verified

    @property
    def all_cards_present(self) -> bool:
        """True when every named card resolved against the harvested mainboard."""
        return bool(self.cards) \
            and len(self.verified) == len(self.cards) \
            and all(self.verified)

    @property
    def all_verified(self) -> bool:
        """Legacy alias; this never represented rules verification."""
        return self.all_cards_present


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
    presence_raw = raw.get("cards_present", raw.get("verified"))
    cards_present = tuple(bool(v) for v in presence_raw) \
        if isinstance(presence_raw, list) else ()
    rules_status = str(raw.get("rules_status") or "author_claimed")
    if rules_status not in _RULES_STATUSES:
        rules_status = "author_claimed"
    return WinLine(
        cards=cards,
        needs=str(raw.get("needs") or ""),
        note=str(raw.get("note") or ""),
        verified=cards_present,
        rules_status=rules_status,
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


def budget_swaps_for_deck(
    commander_names, deck_card_names,
    profiles: Optional[tuple[PrimerProfile, ...]] = None,
) -> tuple[BudgetSwap, ...]:
    """KB swaps applicable to THIS deck (FP-019.6 advisor budget mode).

    Only swaps documented for one of the deck's own commanders, whose
    ``out_card`` the deck actually runs and whose ``in_card`` it does
    not already run. Swap direction is the AUTHOR'S (usually cheap-in,
    sometimes an upgrade path) — the reason string carries which, so
    consumers must surface it rather than assume."""
    matched: list[PrimerProfile] = []
    for cmdr in commander_names or ():
        matched.extend(profiles_for_commander(cmdr, profiles))
    deck_keys = {str(n).casefold() for n in deck_card_names or ()}
    out: list[BudgetSwap] = []
    seen = set()
    for p in {id(m): m for m in matched}.values():
        for s in p.budget_swaps:
            key = (s.out_card.casefold(), s.in_card.casefold())
            if key in seen:
                continue
            if s.out_card.casefold() in deck_keys \
                    and s.in_card.casefold() not in deck_keys:
                seen.add(key)
                out.append(s)
    return tuple(out)


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
        all_present = bool(w.cards) and all(
            (index < len(w.cards_present) and w.cards_present[index])
            or any(_commander_matches(name, commander) for commander in p.commanders)
            for index, name in enumerate(w.cards)
        )
        presence = (
            "all named cards confirmed in harvested deck"
            if all_present
            else "one or more names not confirmed in harvested mainboard or command zone"
        )
        labels = {
            "author_claimed": "Author-claimed win line",
            "conditional": "Conditional line",
            "engine": "Engine/value line",
            "rules_verified": "Rules-verified win line",
        }
        rules_note = (
            "rules independently verified"
            if w.rules_status == "rules_verified"
            else "rules not independently verified"
        )
        needs = f" — {w.needs}" if w.needs else ""
        lines.append(
            f"{labels[w.rules_status]}: {' + '.join(w.cards)}{needs} "
            f"[{presence}; {rules_note}]"
        )
        if w.note:
            lines.append(f"Line note: {w.note}")
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
