"""FP-006 backend prep — the data feed the eventual UI dashboard
consumes.

The user's UI mockup decomposes a deck-detail page into seven
panels (commander hero, stat tiles, mana curve, categories,
suggested adds, theme tags, deck progress). This module builds the
single dict the front-end will request — `build_dashboard(deck_path)`
returns everything panel-ready in one shot.

Public API:

    from commander_builder.deck_dashboard import build_dashboard

    data = build_dashboard(deck_path, bracket=3)
    # → {
    #     "commander": {"name": ..., "type_line": ..., "color_identity": [...]},
    #     "deck_progress": {"current": 99, "target": 100},
    #     "stat_tiles": {"avg_cmc": 2.84, "lands": 37,
    #                    "power_level": 7, "est_price_usd": 284.0},
    #     "mana_curve": [(0, 4), (1, 11), (2, 17), ...],
    #     "categories": {"ramp": 12, "draw": 10, "removal": 8, ...},
    #     "theme_tags": ["Landfall", "Counters"],
    #     "suggested_adds": [
    #         {"card": "Lotus Cobra", "match_pct": 98,
    #          "rationale": "Mana on landfall — accelerates Omnath...",
    #          "price_usd": 8.0},
    #         ...
    #     ],
    #   }

Backend prerequisites this module addresses (per FP-006 in
FUTURE_PLANS.md):

- Price field — projects ``prices.usd`` from Scryfall responses.
  Aggregated to ``est_price_usd`` for the deck-total stat tile.
- Expanded role taxonomy — adds ``land_payoff`` and ``win_condition``
  detection to the existing ``staples.classify_role`` set.
- Power-level heuristic — derived from average CMC + game-changer
  count + archetype + bracket fit.
- Match% on suggestions — combines synergy% + inclusion% from
  ``improvement_advisor`` into a single 0..100 score.

Honest scope: this is the *data shape*. The Flask routes that serve
it and the HTML/CSS that renders it are still future work
(remaining ~14h of FP-006 per the canonical spec).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .archetype import classify as _classify_archetype_path
from .scryfall_client import lookup_card, _parse_commander_names_from_dck
from .staples import (
    BASIC_LANDS_LC,
    UNIVERSAL_STAPLES_LC,
    classify_role as _classify_role_text,
)


# Categories we surface in the UI's "Categories" panel, in display order.
DISPLAY_CATEGORIES = (
    "ramp", "draw", "removal", "wipe", "land_payoff", "win_condition",
)


# --- Expanded role taxonomy (additions to staples.classify_role) -------

_LAND_PAYOFF_PATTERNS = [
    re.compile(r"whenever a land enters", re.IGNORECASE),
    re.compile(r"whenever .*land .* enters", re.IGNORECASE),
    re.compile(r"landfall", re.IGNORECASE),
    re.compile(r"for each land you control", re.IGNORECASE),
    re.compile(r"whenever you play a land", re.IGNORECASE),
]

_WIN_CONDITION_PATTERNS = [
    re.compile(r"target opponent loses the game", re.IGNORECASE),
    re.compile(r"each opponent loses \d+ life", re.IGNORECASE),
    re.compile(r"deals damage equal to .* to each opponent", re.IGNORECASE),
    re.compile(r"each opponent's life total becomes", re.IGNORECASE),
    re.compile(r"infect", re.IGNORECASE),
    re.compile(r"poison counter", re.IGNORECASE),
    # Big-trample finishers
    re.compile(r"creatures you control get \+\d+/\+\d+ and gain trample",
               re.IGNORECASE),
    re.compile(r"craterhoof", re.IGNORECASE),
]


def classify_role_extended(oracle_text: str, type_line: str = "") -> str:
    """Expanded role taxonomy. Tries the new (UI-relevant) categories
    first; falls back to the base ``staples.classify_role`` taxonomy.

    Returns one of: ``land_payoff``, ``win_condition``, or whatever
    ``staples.classify_role`` returns (ramp / draw / removal / wipe /
    protection / tutor / finisher / threat / land / other).
    """
    text = (oracle_text or "").lower()
    if any(p.search(text) for p in _LAND_PAYOFF_PATTERNS):
        return "land_payoff"
    if any(p.search(text) for p in _WIN_CONDITION_PATTERNS):
        return "win_condition"
    return _classify_role_text(oracle_text, type_line)


# --- Price extraction --------------------------------------------------

def _extract_price_usd(card_data: dict | None) -> Optional[float]:
    """Pull ``prices.usd`` from a Scryfall card dict. Returns None when
    Scryfall didn't return a price (digital-only cards, just-released
    sets) — caller treats absent prices as 0 for aggregation."""
    if not card_data:
        return None
    prices = card_data.get("prices")
    if not isinstance(prices, dict):
        return None
    raw = prices.get("usd")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# --- Power-level heuristic --------------------------------------------

# Game Changers from commander_builder.game_changers (rough proxy for
# strong cards). When present in the deck, push the power level up.
def _count_game_changers(card_names: list[str]) -> int:
    """Count how many of ``card_names`` appear on Wizards' Game
    Changers list.

    Earlier versions of this function tried to import a
    ``GAME_CHANGERS`` constant that doesn't exist on the module —
    the public API is ``load_game_changers()``, which returns a
    set. The old import-error path silently returned 0 for every
    deck, breaking the bracket heuristic + the dashboard tile.
    """
    try:
        from .game_changers import load_game_changers
        gc_set = load_game_changers()
    except Exception:
        return 0
    lower = {n.lower() for n in card_names}
    return sum(1 for gc in gc_set if gc.lower() in lower)


# Wizards' official Commander Bracket system (replaced the old 1-10
# power level scale). Reference:
# https://magic.wizards.com/en/news/announcements/introducing-commander-brackets-beta
BRACKET_NAMES = {
    1: "Exhibition",
    2: "Core",
    3: "Upgraded",
    4: "Optimized",
    5: "cEDH",
}


def _power_bracket(
    avg_cmc: float,
    n_game_changers: int,
    bracket: Optional[int],
    archetype: Optional[str] = None,
) -> int:
    """Heuristic Commander Bracket (1..5).

    Inputs:
    - avg_cmc — lower curves push toward higher brackets.
    - n_game_changers — count of cards on the official Game Changers
      list. Brackets 1-3 expect 0; bracket 4 allows 3+; bracket 5 is
      uncapped.
    - bracket — when explicitly set by the user, anchors the result.
    - archetype — combo / stax tendencies nudge the score.

    Returns an int in [1, 5]. Heuristic-only — Wizards' system has
    a hard "Game Changers" check the user is responsible for; we
    surface a best-guess so the dashboard tile is informative.
    """
    # Game-changer count is the dominant signal under the Wizards rules.
    # 0 GCs and reasonable curve → bracket 2-3.
    # 1-2 GCs → bracket 3 (Upgraded).
    # 3+ GCs → bracket 4 (Optimized).
    # cEDH-class fast-mana suite → bracket 5 (manual override only).
    if n_game_changers >= 3:
        guess = 4
    elif n_game_changers >= 1:
        guess = 3
    elif avg_cmc <= 2.6:
        guess = 3  # tight curve, no GCs → upper Core / lower Upgraded
    elif avg_cmc <= 3.4:
        guess = 2  # standard Core deck
    else:
        guess = 1  # high-curve casual / Exhibition

    # Archetype nudges — only push UP, never down (combo decks are
    # almost always at least bracket 3 even without GCs in our list).
    if archetype:
        if "combo" in archetype.lower() and guess < 4:
            guess += 1
        elif "stax" in archetype.lower() and guess < 3:
            guess = 3

    # User-supplied bracket trumps the heuristic — this is the
    # bracket the deck declares it's playing at.
    if bracket and 1 <= bracket <= 5:
        return bracket

    return max(1, min(5, guess))


# Backwards-compatible alias kept so any external caller importing
# ``_power_level`` doesn't break. Returns a bracket integer (1..5),
# not the legacy 1..10 score.
_power_level = _power_bracket


# --- Match% scoring for suggestions ------------------------------------

def match_score(
    inclusion_pct: float, synergy_pct: float = 0.0,
    rank_in_list: int = 0,
) -> int:
    """Combine inclusion% + synergy% into a single 0..100 match score
    for the UI's "Suggested adds" panel.

    Heuristic:
    - inclusion% is the base (50% inclusion → 50 base points)
    - synergy% is a bonus capped at +20pp
    - top-of-list bonus: rank 0 gets +5, rank 1 gets +3, etc.
      (encourages the UI to show a clear "best match" gradient)

    Returns an integer 1..100 for the green/yellow/amber pill display.
    """
    base = inclusion_pct  # 0..100
    syn = min(synergy_pct, 20.0)
    rank_bonus = max(0, 5 - rank_in_list) if rank_in_list >= 0 else 0
    raw = base + syn + rank_bonus
    return max(1, min(100, round(raw)))


# --- Deck reading helpers ----------------------------------------------

def _read_main_with_quantities(deck_path: Path) -> list[tuple[str, int]]:
    """Parse the [Main] section, returning (name, qty) pairs."""
    out: list[tuple[str, int]] = []
    if not deck_path.exists():
        return out
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
        if not in_main:
            continue
        m = re.match(r"^(\d+)\s+(.+?)(?:\|.*)?$", line)
        if m:
            qty = int(m.group(1))
            name = m.group(2).strip()
            out.append((name, qty))
    return out


# --- Top-level dashboard builder --------------------------------------

@dataclass
class DashboardData:
    """The single dict the UI will consume per page load."""
    commander: dict = field(default_factory=dict)
    deck_progress: dict = field(default_factory=dict)
    stat_tiles: dict = field(default_factory=dict)
    mana_curve: list[tuple[int, int]] = field(default_factory=list)
    categories: dict[str, int] = field(default_factory=dict)
    theme_tags: list[str] = field(default_factory=list)
    suggested_adds: list[dict] = field(default_factory=list)
    # Per-deck legality + brackets-fit banner data. Lets the UI
    # surface "All cards legal in Commander" / "X illegal cards" /
    # "Y game changers (bracket 4+)" without an extra API call.
    legality: dict = field(default_factory=dict)
    # Moxfield URL parsed from the deck's `Moxfield=<publicId>`
    # metadata line. None when the deck wasn't imported from
    # Moxfield (manually pasted, etc).
    moxfield_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_dashboard(
    deck_path: Path,
    bracket: Optional[int] = None,
    suggested: Optional[list[dict]] = None,
) -> DashboardData:
    """Build the full UI-feed for one deck.

    ``suggested`` is the list of ``{card, inclusion_pct, synergy_pct,
    rationale, price_usd}`` dicts the advisor produced. When None,
    the suggested_adds panel comes back empty — the UI can then call
    ``improvement_advisor.advise()`` separately and re-merge.
    """
    main_with_qty = _read_main_with_quantities(deck_path)
    deck_card_names = [n for n, _ in main_with_qty]
    total_main = sum(q for _, q in main_with_qty)

    # Commander section.
    commander_names = _parse_commander_names_from_dck(deck_path)
    primary_commander = commander_names[0] if commander_names else ""
    commander_data = lookup_card(primary_commander) if primary_commander else None
    color_identity = []
    if commander_data:
        color_identity = list(commander_data.get("color_identity") or [])

    # Stat tiles: avg_cmc, lands, power_level, est_price_usd.
    cmcs: list[float] = []
    lands = 0
    total_price = 0.0
    cards_with_price = 0
    role_counts: dict[str, int] = {}
    curve_buckets: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
    for name, qty in main_with_qty:
        try:
            data = lookup_card(name)
        except Exception:
            data = None
        # Lands count
        type_line = (data or {}).get("type_line", "") if data else ""
        oracle_text = (data or {}).get("oracle_text", "") if data else ""
        is_land = "land" in (type_line or "").lower()
        if is_land:
            lands += qty
            continue
        # CMC bucketing for non-lands.
        cmc = (data or {}).get("cmc") if data else None
        if cmc is not None:
            try:
                cmc_val = float(cmc)
            except (TypeError, ValueError):
                cmc_val = 0.0
            cmcs.extend([cmc_val] * qty)
            bucket = int(cmc_val) if cmc_val < 6 else 6
            curve_buckets[bucket] = curve_buckets.get(bucket, 0) + qty
        # Price.
        price = _extract_price_usd(data)
        if price is not None:
            total_price += price * qty
            cards_with_price += qty
        # Role classification.
        if not is_land:
            role = classify_role_extended(oracle_text, type_line)
            if role in DISPLAY_CATEGORIES:
                role_counts[role] = role_counts.get(role, 0) + qty

    avg_cmc = round(sum(cmcs) / len(cmcs), 2) if cmcs else 0.0
    n_game_changers = _count_game_changers(deck_card_names)
    try:
        archetype = _classify_archetype_path(deck_path)
    except Exception:
        archetype = "unknown"

    # Theme tags — current archetype + any "*-tribal" subtypes if dominant.
    theme_tags: list[str] = []
    if archetype and archetype != "unknown":
        theme_tags.append(archetype.title())

    # Categories panel — guarantee every UI slot is present (zeros OK).
    categories = {cat: role_counts.get(cat, 0) for cat in DISPLAY_CATEGORIES}

    # Suggested adds — match% + price for each.
    sug: list[dict] = []
    if suggested:
        for i, s in enumerate(suggested):
            card_name = s.get("card", "")
            inclusion = float(s.get("inclusion_pct") or 0)
            synergy = float(s.get("synergy_pct") or 0)
            price = s.get("price_usd")
            if price is None:
                try:
                    data = lookup_card(card_name)
                    price = _extract_price_usd(data)
                except Exception:
                    price = None
            sug.append({
                "card": card_name,
                "match_pct": match_score(inclusion, synergy, rank_in_list=i),
                "rationale": s.get("rationale") or s.get("reason") or "",
                "price_usd": round(price, 2) if price is not None else None,
            })

    mana_curve = sorted(curve_buckets.items())

    # --- Legality banner data + Moxfield URL ----------------------------
    # Pull the (Moxfield publicId) metadata line if present.
    moxfield_url: Optional[str] = None
    try:
        text = deck_path.read_text(encoding="utf-8")
        m = re.search(r"^Moxfield=(.+)$", text, re.MULTILINE)
        if m:
            mox_id = m.group(1).strip()
            moxfield_url = f"https://moxfield.com/decks/{mox_id}"
    except OSError:
        pass

    # Cross-reference deck names against the Game Changers list AND
    # the doctor module's banned-in-Commander set if it exposes one.
    in_deck_gcs: list[str] = []
    illegal: list[str] = []
    try:
        from .game_changers import load_game_changers
        gc_set = load_game_changers()
        in_deck_gcs = sorted({n for n in deck_card_names if n in gc_set})
    except Exception:
        pass
    try:
        from . import doctor as _doctor
        banned = getattr(_doctor, "BANNED_IN_COMMANDER", None)
        if banned:
            illegal = sorted(set(deck_card_names) & set(banned))
    except Exception:
        pass
    legality = {
        "in_deck_game_changers": in_deck_gcs,
        "n_game_changers": len(in_deck_gcs),
        "illegal_cards": illegal,
        "n_illegal": len(illegal),
        "all_legal": len(illegal) == 0,
    }

    return DashboardData(
        commander={
            "name": primary_commander,
            "type_line": (commander_data or {}).get("type_line", ""),
            "color_identity": color_identity,
        },
        deck_progress={
            "current": total_main + len(commander_names),
            "target": 100,  # standard Commander 100-card constraint
        },
        stat_tiles={
            "avg_cmc": avg_cmc,
            "lands": lands,
            # Wizards' official Commander Bracket (1..5). Replaces the
            # old 1..10 power-level scale. Both keys are emitted for
            # backwards-compat with any client still reading
            # `power_level` — both contain the same bracket integer.
            "bracket": _power_bracket(
                avg_cmc, n_game_changers, bracket, archetype,
            ),
            "bracket_name": BRACKET_NAMES.get(
                _power_bracket(avg_cmc, n_game_changers, bracket, archetype),
                "Unknown",
            ),
            "power_level": _power_bracket(
                avg_cmc, n_game_changers, bracket, archetype,
            ),
            "n_game_changers": n_game_changers,
            "est_price_usd": round(total_price, 2),
            "n_priced_cards": cards_with_price,
        },
        mana_curve=mana_curve,
        categories=categories,
        theme_tags=theme_tags,
        suggested_adds=sug,
        legality=legality,
        moxfield_url=moxfield_url,
    )
