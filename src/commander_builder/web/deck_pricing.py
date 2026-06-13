"""Deck pricing helpers for the web layer.

Holds the Scryfall-USD pricing functions that operate over ``.dck``
blobs. Extracted verbatim from ``web/_helpers.py`` (2026-06-12 split);
``_helpers`` re-exports every name here for backward compatibility.
"""

from __future__ import annotations

from typing import Optional


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
    import re as _re
    from ..scryfall_client import lookup_card
    line_re = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")
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
        m = line_re.match(s)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        name = m.group(2).strip()
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
