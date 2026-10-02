"""Deck pricing helpers for the web layer.

Holds the Scryfall-USD pricing functions that operate over ``.dck``
blobs. Extracted verbatim from ``web/_helpers.py`` (2026-06-12 split);
``_helpers`` re-exports every name here for backward compatibility.
"""

from __future__ import annotations

from typing import Optional

from ..dck_utils import iter_section_lines, parse_card_line


def price_deck_text(text: str) -> dict:
    """Sum Scryfall USD prices across all cards (commander + main) in a
    ``.dck`` blob, COUNTING every card that could not be priced.

    Returns::

        {"total_usd": float | None,   # None when zero cards priced
         "n_priced": int,             # card copies that contributed
         "n_unpriced": int,           # card copies that did not
         "unpriced": [{"name", "qty", "reason"}, ...],
         "partial": bool}             # n_unpriced > 0

    ``total_usd`` is None when zero cards in the deck have a Scryfall
    price (e.g. all-digital-only deck, Scryfall down). The UI
    distinguishes between "$0.00 priced" and "unpriced" via this None
    signal so a budget-mode user doesn't get confused by a zero total
    that's actually "no data."

    WHY the unpriced list exists (2026-09-09, audit open bug 2): the
    oracle snapshot dir is shared with a trimmed writer whose snapshots
    have no ``prices`` block, and until this change such cards were
    silently skipped — a "$180" deck could really be a $180-plus-
    whatever-was-trimmed deck with nothing on screen saying so. Every
    card that contributes nothing is now named with a reason (see
    ``price_status.REASON_*``), the total is flagged ``partial``, and
    the trimmed-schema case additionally prints one stderr line so the
    operator learns the dir needs a refresh.

    Quantities count: ``29 Mountain`` contributes 29× the Mountain
    price (which is ~$0.00 anyway, but consistent with the
    dashboard's tile math) — and 29 to ``n_unpriced`` if unpriced.
    """
    from ..price_status import price_status, report_unpriced
    from ..scryfall_client import lookup_card
    total = 0.0
    n_priced = 0
    n_unpriced = 0
    unpriced: list[dict] = []
    for name, qty in _deck_card_quantities(text):
        try:
            card = lookup_card(name)
        except Exception:
            card = None
        price, reason = price_status(card)
        if price is None:
            n_unpriced += qty
            unpriced.append({"name": name, "qty": qty, "reason": reason})
            continue
        total += price * qty
        n_priced += qty
    report_unpriced(unpriced, where="deck total")
    return {
        "total_usd": round(total, 2) if n_priced else None,
        "n_priced": n_priced,
        "n_unpriced": n_unpriced,
        "unpriced": unpriced,
        "partial": n_unpriced > 0,
    }


def _total_price_for_deck_text(text: str) -> tuple[Optional[float], int]:
    """``(total_or_none, n_priced_cards)`` — the historical two-tuple
    over :func:`price_deck_text`, kept for the ``web/_helpers`` shim
    and callers that only need the number. New code should read the
    full result so it can surface the unpriced cards."""
    result = price_deck_text(text)
    return result["total_usd"], result["n_priced"]


def audit_pricing_fields(original_text: str, proposed_text: str) -> dict:
    """The audit payload's pricing block: original/proposed totals, the
    delta, the priced counts, and — since 2026-09-09 — the unpriced
    cards on each side plus a ``price_partial`` flag so the client can
    label a short total instead of presenting it as authoritative.

    Lives here rather than in ``routes_audit`` (which sits at the
    800-line module ceiling) so the route composes it with ``**``.
    """
    original = price_deck_text(original_text)
    proposed = price_deck_text(proposed_text)
    if original["total_usd"] is not None and proposed["total_usd"] is not None:
        price_delta = round(proposed["total_usd"] - original["total_usd"], 2)
    else:
        price_delta = None
    return {
        "original_price_usd": original["total_usd"],
        "proposed_price_usd": proposed["total_usd"],
        "price_delta_usd": price_delta,
        "n_priced_cards_original": original["n_priced"],
        "n_priced_cards_proposed": proposed["n_priced"],
        "unpriced_cards_original": original["unpriced"],
        "unpriced_cards_proposed": proposed["unpriced"],
        "price_partial": original["partial"] or proposed["partial"],
    }


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


# --- Snapshot freshness (P19) -----------------------------------------

def price_snapshot_staleness(names) -> tuple[Optional[float], bool]:
    """``(oldest_snapshot_age_days, is_stale)`` for the price data
    behind ``names``.

    Every price in this module comes from ``lookup_card``, which serves
    disk snapshots with NO TTL — the same "snapshot of any age, served
    forever" property that made legality verdicts silently stale.
    Legality solved it by reporting the age of the oldest snapshot
    backing the verdict; prices had no such signal at all, so a
    six-month-old ``$3.20`` renders identically to one fetched a minute
    ago. Card prices move faster than ban lists, so if anything the
    signal matters more here.

    Deliberately REUSES ``deck_legality.snapshot_staleness`` (which in
    turn reads ``oracle_store.snapshot_age_days`` — file mtime) and its
    ``STALE_SNAPSHOT_DAYS`` threshold instead of duplicating either:
    both readings describe the same disk store, so a user must never
    see "legality data 60 days old" next to fresh-looking prices. Only
    the age is taken from that helper; its warning STRING is legality-
    worded, so the boolean here is derived from the same threshold and
    the caller words the price-side message.

    Never raises: freshness is a bonus signal on a payload that must
    render offline, so any import/stat failure degrades to
    ``(None, False)`` — "unknown age", not "stale".
    """
    try:
        from ..deck_legality import STALE_SNAPSHOT_DAYS, snapshot_staleness
    except Exception:  # noqa: BLE001 — a bonus signal, never a blocker
        return None, False
    try:
        age, _legality_warning = snapshot_staleness(
            names, threshold_days=STALE_SNAPSHOT_DAYS,
        )
    except Exception:  # noqa: BLE001
        return None, False
    if age is None:
        return None, False
    return age, age >= STALE_SNAPSHOT_DAYS


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

    Freshness (P19): the payload also carries ``price_data_age_days``
    — the age of the OLDEST disk snapshot behind the quoted cards, the
    same reading legality reports as ``data_age_days`` — and
    ``price_data_stale``, true once that age crosses
    ``deck_legality.STALE_SNAPSHOT_DAYS`` (45 days, imported, never
    re-declared here). Both keys are ADDITIVE: the three historical
    keys keep their exact meaning, so a client that ignores freshness
    reads the payload it always did. Age ``None`` means "no snapshot on
    disk to date" — an empty suggestion list, a cold store, a hermetic
    test — and is reported as NOT stale, because unknown age is not
    evidence of staleness.

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
    # Age the prices being rendered, using the cards they were read
    # from — the OLDEST of those snapshots is the honest number,
    # exactly as validate_deck reports the oldest snapshot behind a
    # legality verdict. An empty suggestion list quotes no prices and
    # consults no snapshots, so it reports an unknown (None) age rather
    # than a fabricated fresh one.
    age, stale = price_snapshot_staleness([s["card"] for s in suggestions])
    return {
        "total": total,
        "count": len(suggestions),
        "suggestions": suggestions,
        "price_data_age_days": age,
        "price_data_stale": stale,
    }
