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
every card a combo uses (case-insensitive).
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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


def load_combos(force_fallback: bool = False) -> list[dict]:
    """Load the combo list: the refreshed ``data/combos.json`` if present,
    else the hand-curated fallback. Each combo is ``{cards, produces,
    [popularity], [identity]}``."""
    if not force_fallback and COMBO_DATA_PATH.exists():
        try:
            data = json.loads(COMBO_DATA_PATH.read_text(encoding="utf-8"))
            combos = data.get("combos") if isinstance(data, dict) else data
            if combos:
                return combos
        except (OSError, ValueError):
            pass
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


def refresh_combos(top_n: int = 1500, page_size: int = 500,
                   out_path: Path | None = None,
                   _opener=None) -> int:
    """Fetch the top-N most popular combos from Commander Spellbook's
    backend (paginated, ordering=-popularity) and write a compact
    ``data/combos.json``. Returns the count written. ``_opener`` is
    injectable for tests (defaults to urllib)."""
    opener = _opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                             "Accept": "application/json"}),
        timeout=60).read())
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
