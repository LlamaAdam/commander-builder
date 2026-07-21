"""Deck pricing helpers for the web layer.

Holds the Scryfall-USD pricing functions that operate over ``.dck``
blobs. Extracted verbatim from ``web/_helpers.py`` (2026-06-12 split);
``_helpers`` re-exports every name here for backward compatibility.
"""

from __future__ import annotations

from typing import Optional

from ..dck_utils import iter_section_lines, parse_card_line


def _total_price_for_deck_text(text: str) -> tuple[Optional[float], int]:
    """Sum Scryfall USD prices across all cards (commander + main)
    in a ``.dck`` blob. Returns ``(total_or_none, n_priced_cards)``.

    ``total_or_none`` is None when zero cards in the deck have a
    Scryfall price (e.g. all-digital-only deck, Scryfall down). The
    UI distinguishes between "$0.00 priced" and "unpriced" via this
    None signal so a budget-mode user doesn't get confused by a
    zero total that's actually "no data."

    Quantities count: ``29 Mountain`` contributes 29× the Mountain
    price (which is ~$0.00 anyway, but consistent with the
    dashboard's tile math).

    Used by the audit endpoint to compute the post-swap deck price
    so the UI can show "$X → $Y (Δ +$12.30)" alongside the diff
    list. Tier-2 backlog item from STATUS.md.
    """
    from ..scryfall_client import lookup_card
    total = 0.0
    n_priced = 0
    in_card_section = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            # Count cards in [Main] and [Commander]; ignore
            # [Sideboard], [Considering], [metadata], etc.
            sl = s.lower()
            in_card_section = sl in ("[main]", "[commander]")
            continue
        if not in_card_section:
            continue
        parsed = parse_card_line(s)
        if parsed is None:
            continue
        qty, name = parsed
        try:
            card = lookup_card(name)
        except Exception:
            card = None
        if not card:
            continue
        prices = card.get("prices") if isinstance(card, dict) else None
        if not isinstance(prices, dict):
            continue
        raw_price = prices.get("usd")
        if not raw_price:
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        total += price * qty
        n_priced += qty
    if n_priced == 0:
        return (None, 0)
    return (round(total, 2), n_priced)


# --- Cheaper-printing savings (ManaFoundry parity) ---------------------

# Only bother suggesting swaps for cards the user is meaningfully paying
# for. Sub-$1 cards can't produce the $1 minimum saving below anyway,
# so this floor is mostly a fast-path that skips the (potentially
# network-backed) prints lookup for the bulk-commons half of a deck.
_SAVINGS_MIN_CARD_PRICE_USD = 1.00
# A suggestion must save at least max($1, 30% of the current price).
# The $1 floor keeps noise like "$1.40 → $0.90, save $0.50" out of the
# list; the 30% rule keeps "$40 → $37" out — technically $3, but nobody
# re-buys a card to shave 7.5%.
_SAVINGS_ABS_FLOOR_USD = 1.00
_SAVINGS_PCT_OF_CURRENT = 0.30
# Scryfall set_types that are never legal game pieces. "memorabilia"
# covers World Championship gold-border decks, oversized commanders,
# and similar collectibles that price low precisely BECAUSE they aren't
# playable — exactly the false positives this feature must not emit.
_EXCLUDED_SET_TYPES = {"memorabilia"}


def _printing_min_usd(printing: dict) -> Optional[float]:
    """Cheapest way to buy one physical copy of this printing.

    Takes the min over usd / usd_foil / usd_etched because some
    printings only exist in one finish (foil-only promo sets have
    ``usd: null``) and a foil copy is just as legal in a deck as a
    nonfoil one. Returns None when no finish has a price."""
    prices = printing.get("prices")
    if not isinstance(prices, dict):
        return None
    vals: list[float] = []
    for key in ("usd", "usd_foil", "usd_etched"):
        raw = prices.get(key)
        if not raw:
            continue
        try:
            vals.append(float(raw))
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else None


def _printing_is_commander_legal(printing: dict) -> bool:
    """Can this specific printing be sleeved in a paper Commander deck?

    Card-level legality (banned list) is carried on every printing's
    ``legalities.commander``; printing-level problems (gold border,
    oversized, memorabilia sets, digital-only) are what actually vary
    between printings and are the reason this filter exists — the
    cheapest listing for many staples is a not-legal WC-deck copy."""
    if (printing.get("set_type") or "").lower() in _EXCLUDED_SET_TYPES:
        return False
    # Gold border = pre-2000s World Championship reprints: distinct
    # backs, not tournament legal. Silver border (Un-sets) is already
    # not_legal in the legalities map, but check both borders anyway so
    # stale cached data can't slip one through.
    if (printing.get("border_color") or "").lower() in ("gold", "silver"):
        return False
    if printing.get("oversized"):
        return False
    if printing.get("digital"):
        return False
    legal = ((printing.get("legalities") or {}).get("commander") or "").lower()
    # Missing legality info (very old cached shapes) falls through to
    # "assume legal" — the border/set_type checks above already caught
    # the printing-specific problems, and a false "no suggestion" is
    # worse than suggesting a card the user already legally runs.
    return legal not in ("not_legal", "banned")


def _deck_card_quantities(text: str) -> list[tuple[str, int]]:
    """Fold [Main] + [Commander] into ordered ``(name, qty)`` pairs.

    Same section policy as ``_total_price_for_deck_text`` (sideboard /
    considering piles aren't part of the deck's price, so they don't
    get savings suggestions either). Duplicate lines merge so a deck
    listing ``1 Sol Ring`` twice yields one suggestion covering qty 2."""
    order: list[str] = []
    qty_by_name: dict[str, int] = {}
    for section in ("Main", "Commander"):
        for line in iter_section_lines(text, section):
            parsed = parse_card_line(line)
            if parsed is None:
                continue
            qty, name = parsed
            if not name:
                continue
            if name not in qty_by_name:
                order.append(name)
                qty_by_name[name] = 0
            qty_by_name[name] += qty
    return [(n, qty_by_name[n]) for n in order]


def printing_savings_for_deck_text(text: str) -> dict:
    """Find cards where a legal cheaper PRINTING would trim the deck's
    price (ManaFoundry parity feature).

    Returns ``{"total": float, "count": int, "suggestions": [...]}``
    where each suggestion is ``{card, qty, current_price, current_set,
    cheapest_price, cheapest_set, cheapest_collector, savings}``.
    ``savings`` is quantity-aware (per-copy saving × qty) and ``total``
    is the sum, so the UI's "save up to $X" headline is the real
    whole-deck number.

    Offline behavior: printings come from ``lookup_card_prints``, which
    is lazily network-backed. After the FIRST network failure we flip a
    circuit breaker and drain the remaining cards cache-only — without
    it, a fully-offline dashboard load would eat one connect-timeout
    per expensive card. Nothing cached + offline ⇒ empty suggestions,
    never an exception (the dashboard must render regardless).
    """
    from ..scryfall_client import lookup_card, lookup_card_prints

    suggestions: list[dict] = []
    offline = False
    for name, qty in _deck_card_quantities(text):
        try:
            card = lookup_card(name)
        except Exception:
            card = None  # Same degrade-to-unpriced policy as the tile.
        if not isinstance(card, dict):
            continue
        # Basic lands are excluded outright: every printing is
        # functionally identical and near-free, and "swap your Forest
        # printing" is exactly the noise ManaFoundry avoids too.
        if "basic" in (card.get("type_line") or "").lower():
            continue
        prices = card.get("prices")
        raw_current = prices.get("usd") if isinstance(prices, dict) else None
        try:
            current = float(raw_current) if raw_current else None
        except (TypeError, ValueError):
            current = None
        # Current price intentionally mirrors the est_price_usd tile
        # math (prices.usd of the default printing) so "current" here
        # is the same number the user already sees priced.
        if current is None or current <= _SAVINGS_MIN_CARD_PRICE_USD:
            continue
        try:
            printings = lookup_card_prints(name, cache_only=offline)
        except Exception:
            # First failure trips the breaker; also retry THIS card
            # cache-only so a mid-deck outage doesn't skip a card whose
            # printings were already snapshotted.
            offline = True
            try:
                printings = lookup_card_prints(name, cache_only=True)
            except Exception:
                printings = None
        if not printings:
            continue
        cheapest_price: Optional[float] = None
        cheapest: Optional[dict] = None
        for p in printings:
            if not isinstance(p, dict) or not _printing_is_commander_legal(p):
                continue
            usd = _printing_min_usd(p)
            if usd is None:
                continue
            if cheapest_price is None or usd < cheapest_price:
                cheapest_price = usd
                cheapest = p
        if cheapest is None or cheapest_price is None:
            continue
        per_copy_saving = current - cheapest_price
        threshold = max(
            _SAVINGS_ABS_FLOOR_USD, _SAVINGS_PCT_OF_CURRENT * current,
        )
        # Single-printing cards land here with per_copy_saving ≈ 0 and
        # drop out — no "swap it for itself" suggestions.
        if per_copy_saving < threshold:
            continue
        suggestions.append({
            "card": name,
            "qty": qty,
            "current_price": round(current, 2),
            "current_set": card.get("set"),
            "cheapest_price": round(cheapest_price, 2),
            "cheapest_set": cheapest.get("set"),
            "cheapest_collector": cheapest.get("collector_number"),
            "savings": round(per_copy_saving * qty, 2),
        })
    # Biggest wins first — the UI shows a collapsed list, so the top
    # rows are the only ones many users will ever read.
    suggestions.sort(key=lambda s: s["savings"], reverse=True)
    total = round(sum(s["savings"] for s in suggestions), 2)
    return {
        "total": total,
        "count": len(suggestions),
        "suggestions": suggestions,
    }
