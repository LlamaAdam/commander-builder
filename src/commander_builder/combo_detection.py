"""Infinite-combo detection (FP / new capability).

Flags two/three-card infinite combos present in a deck. Useful for
bracket awareness (combos push a deck up brackets) and for surfacing
unintended combos in a build.

Data: the full Commander Spellbook export is ~500 MB / 89k variants —
far too big to bundle. So we keep a small **hand-curated fallback** of
well-known combos (works offline, zero deps), and an API-backed
``refresh_combos`` that pulls the top-N most *popular* combos from
Commander Spellbook's backend (paginated, ordering=-popularity) into a
compact ``data/combos.json``. Mirrors the game_changers.py pattern
(fallback set + cached/refreshable list).

A "combo present" = the deck's cards (Commander + Main) is a superset of
every card a combo uses (case-insensitive). ``one_piece_away`` answers the
complementary and more actionable question: which combos is this deck
exactly ONE card short of?
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Resolves a card name to its Scryfall dict (or None when unknown) — the
# injectable seam the mana-value/speed rule reads through.
CardLookup = Callable[[str], Optional[dict]]

REPO_ROOT = Path(__file__).resolve().parents[2]
COMBO_DATA_PATH = REPO_ROOT / "data" / "combos.json"
SPELLBOOK_API = "https://backend.commanderspellbook.com/variants/"
USER_AGENT = "commander-builder/0.2 (+https://github.com/LlamaAdam/commander-builder)"

# Hand-curated well-known infinite combos — the offline floor so detection
# works with no network / no refresh. Each: cards (all must be present) +
# what it produces. Kept short + iconic; refresh_combos expands this.
_FALLBACK: list[dict] = [
    {"cards": ["Thassa's Oracle", "Demonic Consultation"], "produces": "Win the game"},
    {"cards": ["Thassa's Oracle", "Tainted Pact"], "produces": "Win the game"},
    {"cards": ["Laboratory Maniac", "Demonic Consultation"], "produces": "Win the game"},
    {"cards": ["Isochron Scepter", "Dramatic Reversal"], "produces": "Infinite mana (with nonland mana rocks)"},
    {"cards": ["Mikaeus, the Unhallowed", "Triskelion"], "produces": "Infinite damage"},
    {"cards": ["Kiki-Jiki, Mirror Breaker", "Restoration Angel"], "produces": "Infinite creatures/attackers"},
    {"cards": ["Kiki-Jiki, Mirror Breaker", "Zealous Conscripts"], "produces": "Infinite creatures"},
    {"cards": ["Splinter Twin", "Deceiver Exarch"], "produces": "Infinite creatures"},
    {"cards": ["Walking Ballista", "Heliod, Sun-Crowned"], "produces": "Infinite damage"},
    {"cards": ["Devoted Druid", "Vizier of Remedies"], "produces": "Infinite green mana"},
    {"cards": ["Sanguine Bond", "Exquisite Blood"], "produces": "Infinite life drain"},
    {"cards": ["Basalt Monolith", "Rings of Brighthearth"], "produces": "Infinite colorless mana"},
    {"cards": ["Grand Architect", "Pili-Pala"], "produces": "Infinite mana"},
    {"cards": ["Midnight Guard", "Presence of Gond"], "produces": "Infinite tokens"},
    {"cards": ["Worldgorger Dragon", "Animate Dead"], "produces": "Infinite mana/loops"},
    {"cards": ["Aetherflux Reservoir", "Bolas's Citadel"], "produces": "Win the game"},
    {"cards": ["Food Chain", "Eternal Scourge"], "produces": "Infinite creature mana"},
    {"cards": ["Niv-Mizzet, the Firemind", "Curiosity"], "produces": "Win the game"},
    {"cards": ["Underworld Breach", "Lion's Eye Diamond", "Brain Freeze"], "produces": "Win the game"},
    {"cards": ["Dualcaster Mage", "Twinflame"], "produces": "Infinite creatures"},
]


# Cache of the parsed combos file, keyed on (path, mtime_ns, size) so a
# --refresh (or a test pointing COMBO_DATA_PATH elsewhere) is picked up
# without a process restart. estimate_bracket() loads the pool on every
# call — 4x per bracket-steer loop — and a refreshed combos.json runs to
# ~1,500 entries, so the re-parse is worth skipping.
_COMBOS_CACHE: tuple[tuple[str, int, int], list[dict]] | None = None


def load_combos(force_fallback: bool = False) -> list[dict]:
    """Load the combo list: the refreshed ``data/combos.json`` if present,
    else the hand-curated fallback. Each combo is ``{cards, produces,
    [popularity], [identity]}``.

    COPY CONTRACT: every return hands back a fresh LIST (append/sort/del
    on it is safe), but the combo dicts INSIDE are the cached objects
    themselves, shared by every caller in the process — deep-copying a
    ~1,500-entry refreshed DB on each call would claw back most of what
    the mtime-keyed cache saves, and no caller mutates them today. DO
    NOT MUTATE a returned combo dict; copy it first. Pinned by
    ``test_load_combos_inner_dicts_are_shared_do_not_mutate``."""
    global _COMBOS_CACHE
    if not force_fallback and COMBO_DATA_PATH.exists():
        try:
            stat = COMBO_DATA_PATH.stat()
            key = (str(COMBO_DATA_PATH), stat.st_mtime_ns, stat.st_size)
            if _COMBOS_CACHE is not None and _COMBOS_CACHE[0] == key:
                return list(_COMBOS_CACHE[1])
            data = json.loads(COMBO_DATA_PATH.read_text(encoding="utf-8"))
            combos = data.get("combos") if isinstance(data, dict) else data
            if combos:
                _COMBOS_CACHE = (key, combos)
                return list(combos)
        except (OSError, ValueError):
            pass
    # Same zero-copy contract as above: fresh list, shared inner dicts
    # (here the module-level _FALLBACK entries).
    return list(_FALLBACK)


def detect_combos_in_deck(deck_text: str, combos: list[dict] | None = None) -> list[dict]:
    """Return combos whose EVERY card is present in the deck (Commander +
    Main), sorted by popularity desc. Each result carries its ``cards`` +
    ``produces``."""
    from .deck_library_analyzer import iter_deck_cards
    have = {name.lower() for _qty, name in iter_deck_cards(deck_text)}
    pool = combos if combos is not None else load_combos()
    found: list[dict] = []
    for combo in pool:
        cards = combo.get("cards") or []
        if len(cards) >= 2 and all(c.lower() in have for c in cards):
            found.append(combo)
    found.sort(key=lambda c: c.get("popularity", 0) or 0, reverse=True)
    return found


# --------------------------------------------------------------------------- #
# Bracket awareness — combos push a deck up the WotC bracket ladder.
# --------------------------------------------------------------------------- #
# WotC's Commander bracket guidelines single out **early-game two-card
# infinite combos** as the gating category: Brackets 1–2 (Exhibition / Core)
# and 3 (Upgraded) all say no *early-game* two-card infinite combos;
# Bracket 4 (Optimized) and 5 (cEDH) are unrestricted.
#
# READ THE RULE EXACTLY: "early-game" is part of it, not decoration. A B3
# "Upgraded" deck is explicitly ALLOWED to run a late-game two-card infinite
# — the restriction is on combos that end the game before the table has had
# a game. Flooring every two-card combo at B4 (what this module used to do)
# is not "conservative", it is a different, stricter rule: it slammed
# ordinary upgraded decks — an Exquisite Blood + Sanguine Bond deck is the
# canonical B3 list — all the way to Optimized.
#
# So we gate on combo SPEED, proxied by the summed mana value of its pieces.
# MV is the only speed measure available from a static list, and it is a
# decent one: a combo you must pay 12 total mana for cannot be assembled in
# the early game, while a 4-mana pair can. See _LATE_GAME_COMBO_MV.
#
# The mapping from a detected combo to the lowest bracket that permits it:
#   * game-ending, <=2 cards, combined MV >= _LATE_GAME_COMBO_MV -> floor 3
#     (late-game two-card combo: B3-legal by rule)
#   * game-ending, <=2 cards, combined MV below that              -> floor 4
#     (the WotC-restricted early-game case)
#   * game-ending, <=2 cards, MV UNKNOWN                          -> floor 4
#     (conservative on missing data — see combo_bracket_floor)
#   * game-ending, 3+ cards        -> floor 3  (heuristic: more setup = later;
#                                               still a deliberate combo finish)
#   * not game-ending (value loop) -> floor 1  (no bracket pressure)
_GAME_ENDING_TOKENS = ("win the game", "win ", "infinite", "mill out", "lose the game")
_COMBO_BRACKET_CEILING = 5  # B5/cEDH: unrestricted

# Combined mana value at or above which a two-card combo counts as
# "late-game" and therefore B3-legal.
#
# WHY 7: seven total mana is two full turns of Commander ramp past the
# curve — you are not assembling it on turn 3, and in practice you cast the
# halves across two turns, giving the table a window to answer the first
# piece. It also lands cleanly between the archetypes: the combos WotC's
# rule is aimed at (Thassa's Oracle + Demonic Consultation = 3, Devoted
# Druid + Vizier of Remedies = 4, Kiki-Jiki + Restoration Angel = 9 —
# arguably the edge case) sit below it, while the durdly upgraded-deck pairs
# (Sanguine Bond + Exquisite Blood = 11, Mikaeus + Triskelion = 12) sit
# above. Tunable: raising it makes the estimator stricter, never unsafe.
_LATE_GAME_COMBO_MV = 7


def is_game_ending(combo: dict) -> bool:
    """True if the combo's ``produces`` text describes a win or an infinite
    loop (the bracket-gating category). Case-insensitive substring match."""
    produces = str(combo.get("produces", "")).lower()
    return any(tok in produces for tok in _GAME_ENDING_TOKENS)


def _cached_scryfall(card_name: str) -> Optional[dict]:
    """On-disk Scryfall snapshot for ``card_name``, or None. NEVER fetches.

    Mirrors ``_advisor_heuristic._cached_scryfall``. Cache-only is a hard
    requirement here, not an optimization: ``combo_bracket_floor`` runs once
    per detected combo inside ``bracket_estimator`` — which itself runs per
    deck inside pool_curator/meta_test loops and per iteration inside
    deck_builder's steering loop — and the estimator's contract is that it
    is OFFLINE-SAFE and never blocks on the network. A cold cache therefore
    reads as "speed unknown", which floors conservatively at 4; the cache is
    populated as a side effect of the ordinary ``lookup_card`` traffic the
    dashboard and advisor already generate.
    """
    try:
        from .scryfall_client import _cache_path
        p = _cache_path(card_name)
    except Exception:  # noqa: BLE001 — bracket floors must not raise
        return None
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _combined_mana_value(combo: dict, lookup: CardLookup) -> Optional[float]:
    """Summed mana value of every piece of ``combo``, or None if ANY piece
    can't be resolved.

    All-or-nothing on purpose: a partial sum understates the combo's cost,
    which would push it below the late-game threshold and produce the
    *stricter* floor for the wrong reason. "I don't know" must be a distinct
    answer from "it's cheap" so the caller can apply its own conservative
    default deliberately.
    """
    cards = combo.get("cards") or []
    if not cards:
        return None
    total = 0.0
    for name in cards:
        try:
            data = lookup(name)
        except Exception:  # noqa: BLE001 — an injected lookup may raise
            return None
        if not data:
            return None
        cmc = data.get("cmc")
        if cmc is None:
            return None
        try:
            total += float(cmc)
        except (TypeError, ValueError):
            return None
    return total


def combo_bracket_floor(
    combo: dict, lookup: Optional[CardLookup] = None,
) -> int:
    """Lowest WotC bracket that permits a deck containing this combo.

    See the module comment for the mapping rationale. Non-game-ending combos
    return 1 (no pressure); a game-ending two-card combo returns 3 when its
    pieces are collectively expensive enough to be a late-game line and 4
    otherwise.

    ``lookup`` resolves a card name to its Scryfall dict (needs only
    ``cmc``); it defaults to the cache-only reader :func:`_cached_scryfall`
    and is injectable for tests. WHEN THE LOOKUP CAN'T ANSWER — cold cache,
    unknown card, injected stub returning None — we return the STRICT floor
    of 4. Over-flagging a late-game combo as B4 costs a deck one bracket of
    headroom; under-flagging an early-game combo silently passes a deck that
    breaks the B3 rule, which is the failure this whole module exists to
    prevent. Conservative on missing data.

    Backward-compatible: ``combo_bracket_floor(combo)`` still works.
    """
    if not is_game_ending(combo):
        return 1
    n_cards = len(combo.get("cards") or [])
    if n_cards > 2:
        return 3
    total_mv = _combined_mana_value(combo, lookup or _cached_scryfall)
    if total_mv is None:
        return 4
    return 3 if total_mv >= _LATE_GAME_COMBO_MV else 4


def one_piece_away(deck_text: str, combos: list[dict] | None = None,
                   *, lookup: Optional[CardLookup] = None) -> list[dict]:
    """Combos the deck is EXACTLY one card short of, most popular first.

    WHY THIS EXISTS: ``detect_combos_in_deck`` answers "do I have this
    combo" — a fact about a deck that is already built. The actionable
    question for an advisor is "am I ONE CARD away", because that names a
    specific card to add (or, for a low-bracket deck, a specific card to
    keep OUT). Exactly-one-missing is the only useful cut: zero missing is
    already reported by ``detect_combos_in_deck``, and two-plus missing is a
    suggestion to rebuild, not a card recommendation.

    Each row is machine-readable and self-contained so a card-scoring
    formula can consume it without re-deriving anything::

        {
          "missing": "Demonic Consultation",   # the single card to add
          "have": ["Thassa's Oracle"],         # pieces already in the deck
          "cards": [...],                      # the full combo, as listed
          "produces": "Win the game",
          "popularity": 314670,                # 0 when the DB has none
          "bracket_floor": 4,                  # floor IF completed
        }

    ``bracket_floor`` is the floor the deck WOULD take on by adding
    ``missing`` — carried so a B2 deck gets warned ("this would floor you at
    B4") off the same row a B4 deck gets tempted by. It is computed with the
    same ``lookup``-backed speed rule as :func:`combo_bracket_floor`,
    including its conservative missing-data default.

    Sorted by popularity descending (matching ``detect_combos_in_deck``) so
    the most-played, most-likely-intended lines surface first. Pure-offline;
    never raises on a weird decklist beyond what the deck parser does.
    """
    from .deck_library_analyzer import iter_deck_cards
    have = {name.lower() for _qty, name in iter_deck_cards(deck_text)}
    pool = combos if combos is not None else load_combos()
    resolver = lookup or _cached_scryfall
    out: list[dict] = []
    for combo in pool:
        cards = combo.get("cards") or []
        # Same >= 2 guard detect_combos_in_deck applies: a 1-card "combo" is
        # a data artifact, and "one piece away" from it is just "you don't
        # own a card", which is not a combo suggestion.
        if len(cards) < 2:
            continue
        missing = [c for c in cards if c.lower() not in have]
        if len(missing) != 1:
            continue
        out.append({
            "missing": missing[0],
            "have": [c for c in cards if c.lower() in have],
            "cards": list(cards),
            "produces": combo.get("produces", "combo"),
            "popularity": combo.get("popularity", 0) or 0,
            "bracket_floor": combo_bracket_floor(combo, lookup=resolver),
        })
    out.sort(key=lambda r: r["popularity"], reverse=True)
    return out


def assess_deck_brackets(deck_text: str, bracket: int,
                         combos: list[dict] | None = None) -> dict:
    """Assess a deck's detected combos against a target ``bracket``.

    Returns:
      ``combos``               — every detected combo, each annotated with
                                 ``bracket_floor`` + ``game_ending``
      ``recommended_bracket``  — max floor across detected combos (1 if none);
                                 the lowest bracket the deck's combos justify
      ``violations``           — combos whose floor > target bracket (i.e. the
                                 combos that push the deck above its declared
                                 bracket); empty when the deck is within bracket
      ``within_bracket``       — True when there are no violations

    Pure-offline (uses the cached/fallback combo DB). The target bracket is
    typically the ``[B<n>]`` suffix the rest of the pipeline already parses.
    """
    found = detect_combos_in_deck(deck_text, combos=combos)
    annotated: list[dict] = []
    recommended = 1
    violations: list[dict] = []
    for c in found:
        floor = combo_bracket_floor(c)
        item = {**c, "bracket_floor": floor, "game_ending": is_game_ending(c)}
        annotated.append(item)
        recommended = max(recommended, floor)
        if floor > bracket:
            violations.append(item)
    return {
        "combos": annotated,
        "recommended_bracket": recommended,
        "violations": violations,
        "within_bracket": not violations,
    }


def refresh_combos(top_n: int = 1500, page_size: int = 500,
                   out_path: Path | None = None,
                   _opener=None) -> int:
    """Fetch the top-N most popular combos from Commander Spellbook's
    backend (paginated, ordering=-popularity) and write a compact
    ``data/combos.json``. Returns the count written. ``_opener`` is
    injectable for tests (defaults to urllib)."""
    def _default_opener(url: str) -> bytes:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT,
                          "Accept": "application/json"})
        # `with` so the response socket is closed promptly (the lambda
        # form leaked it until GC — every other urlopen here uses `with`).
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    opener = _opener or _default_opener
    url = f"{SPELLBOOK_API}?ordering=-popularity&limit={page_size}"
    collected: list[dict] = []
    while url and len(collected) < top_n:
        payload = json.loads(opener(url))
        for r in payload.get("results", []):
            cards = [u["card"]["name"] for u in (r.get("uses") or [])
                     if u.get("card", {}).get("name")]
            if len(cards) < 2:
                continue
            produces = [p["feature"]["name"] for p in (r.get("produces") or [])
                        if p.get("feature", {}).get("name")]
            collected.append({
                "cards": cards,
                "produces": "; ".join(produces) or "combo",
                "popularity": r.get("popularity") or 0,
                "identity": r.get("identity"),
            })
            if len(collected) >= top_n:
                break
        url = payload.get("next")
        time.sleep(0.3)

    out = out_path or COMBO_DATA_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "commanderspellbook.com",
        "count": len(collected),
        "combos": collected,
    }), encoding="utf-8")
    return len(collected)


def main(argv=None) -> int:
    """``commander-combos`` — detect combos in a deck, or refresh the DB."""
    import argparse
    p = argparse.ArgumentParser(
        prog="commander-combos",
        description="Detect infinite combos in a .dck, or refresh the combo "
                    "DB from Commander Spellbook.")
    p.add_argument("--deck", type=Path, help="Detect combos in this .dck.")
    p.add_argument("--refresh", action="store_true",
                   help="Refresh data/combos.json from Commander Spellbook.")
    p.add_argument("--top-n", type=int, default=1500)
    args = p.parse_args(argv)

    if args.refresh:
        n = refresh_combos(top_n=args.top_n)
        print(f"refreshed combo DB: {n} combos -> {COMBO_DATA_PATH}")
        return 0
    if args.deck:
        if not args.deck.exists():
            print(f"ERROR: deck not found: {args.deck}")
            return 2
        found = detect_combos_in_deck(args.deck.read_text(encoding="utf-8"))
        src = "data/combos.json" if COMBO_DATA_PATH.exists() else "fallback list"
        print(f"combos found in {args.deck.name} (DB: {src}): {len(found)}")
        for c in found:
            print(f"  • {' + '.join(c['cards'])}  =>  {c.get('produces','combo')}")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
