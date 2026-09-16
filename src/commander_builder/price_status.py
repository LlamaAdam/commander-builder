"""Per-card price status shared by every deck-total renderer.

Two readers sum ``prices.usd`` off oracle snapshots — the web pricing
helpers (``web/deck_pricing``: audit cost delta, cheaper printings) and
the dashboard's Est. price tile (``deck_dashboard``). Until 2026-09-09
each had its own ``prices.get("usd")`` extractor and both SILENTLY
dropped a card that had no price, so a total could be quietly short and
nothing on screen said so.

WHY a card can have no price (2026-09-09, audit open bug 2): the oracle
snapshot dir is SHARED with ``forge_py``, whose ``cards.refresh`` writes
snapshots trimmed to the engine's needs — no ``prices`` block at all —
under the same ``<slug>.json`` names. Every writer in THIS repo
(``scryfall_client.lookup_card`` / ``refresh_card``,
``oracle_store.write_snapshots_from_bulk``) persists the full Scryfall
object, so a price-less snapshot is the other writer's, and the reader
has to tolerate the mixed schema for as long as the dir is shared. The
honest behaviour is the loud-failure idiom: COUNT the card, NAME it,
and label the total partial — never drop it.

Public API:

    price, reason = price_status(card)     # (float, None) | (None, why)
    report_unpriced(unpriced, where="dashboard")   # one stderr line
"""

from __future__ import annotations

import sys
from typing import Optional

#: No snapshot at all — lookup returned None or raised.
REASON_NO_SNAPSHOT = "no snapshot"
#: Snapshot exists but carries no ``prices`` block: the trimmed schema.
REASON_NO_PRICES = "snapshot has no prices block"
#: ``prices`` is present but ``usd`` is null/unparsable (digital-only
#: printings, cards too new to be priced).
REASON_NO_USD = "no usd price"

# Cap on names in the stderr line — a whole-deck trim would otherwise
# print 99 names; the payload still carries every one.
_REPORT_NAME_CAP = 8


def price_status(card: Optional[dict]) -> tuple[Optional[float], Optional[str]]:
    """``(usd_price, None)`` when ``card`` carries a usable ``prices.usd``;
    otherwise ``(None, reason)`` with one of the ``REASON_*`` strings.

    Never raises: this sits inside per-card loops that must render a
    payload for a deck whose data is partly missing.
    """
    if not isinstance(card, dict) or not card:
        return None, REASON_NO_SNAPSHOT
    prices = card.get("prices")
    if not isinstance(prices, dict):
        return None, REASON_NO_PRICES
    raw = prices.get("usd")
    if not raw:
        return None, REASON_NO_USD
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, REASON_NO_USD


def report_unpriced(unpriced: list[dict], *, where: str) -> None:
    """One loud stderr line when any snapshot lacked a ``prices`` block.

    Only the trimmed-schema reason is reported here: a missing snapshot
    or a null ``usd`` is ordinary Scryfall reality and already visible
    in the payload's unpriced list, whereas a price-less snapshot means
    the shared dir's schema drifted under us and a refresh fixes it.
    ``unpriced`` entries are ``{"name", "qty", "reason"}``.
    """
    trimmed = [u for u in unpriced if u.get("reason") == REASON_NO_PRICES]
    if not trimmed:
        return
    names = ", ".join(str(u.get("name")) for u in trimmed[:_REPORT_NAME_CAP])
    if len(trimmed) > _REPORT_NAME_CAP:
        names += f", … (+{len(trimmed) - _REPORT_NAME_CAP} more)"
    print(
        f"[pricing] {where}: {len(trimmed)} card(s) have an oracle snapshot "
        f"WITHOUT a prices block ({names}) — the total is PARTIAL. The "
        f"shared snapshot dir carries a mixed schema (a trimmed writer "
        f"shares it); `commander-oracle-refresh` rewrites those cards "
        f"with full snapshots.",
        file=sys.stderr, flush=True,
    )
