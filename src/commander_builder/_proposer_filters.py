"""Post-response curator filters used by ``proposer.auto_propose``.

The auto-curate pipeline chains several defensive filters over Claude's
candidate output so the resulting .dck file is legal in Commander format
regardless of what the model proposed. Each filter takes a list of card
names (typically curator adds) and returns ``(kept, dropped)``.

  ``enforce_bracket_caps``   — strip game-changers at B1/B2.
  ``enforce_color_identity`` — strip off-color adds.

The matching ``dropped_for_protection`` and ``dropped_for_balance``
filters live inside the auto_propose pipeline because they need the
broader proposal state (protected_cards list, balance arithmetic
between adds and cuts). They share the ``(kept, dropped)`` shape so
the orchestrator can chain identically.

Split out of ``proposer.py`` on 2026-05-16 (Tier-3 refactor) to bring
the orchestrator under the 800-line guideline ceiling. Public symbols
are re-exported from ``proposer`` for back-compat with existing
imports.
"""
from __future__ import annotations

from typing import Optional


# Threshold below which game-changers get filtered out. Comes from the WotC
# bracket guidelines: B1 (Exhibition), B2 (Core) -- no game-changers allowed.
# B3 (Upgraded) and B4 (Optimized) permit up to 3; B5 (cEDH) is unbounded.
# We currently only enforce the binary 'allowed at all' line -- the 3-card
# cap at B3/B4 is a follow-up.
_BRACKET_NO_GAME_CHANGERS_THRESHOLD = 3


def _load_game_changers() -> set[str]:
    """Return the WotC-designated game-changers set.

    Wrapping ``game_changers.load_game_changers()`` here gives tests a
    proposer-local symbol to monkeypatch without depending on the
    game_changers module's HTTP cache lifecycle. Production calls fall
    through to the real loader (disk-cached scrape of WotC's bracket
    guidelines page)."""
    from .game_changers import load_game_changers
    return load_game_changers()


def enforce_bracket_caps(
    adds: list[str], bracket: int,
) -> tuple[list[str], list[str]]:
    """Split ``adds`` into (kept, dropped) by the bracket cap rule.

    Below B3 (i.e. B1 + B2), game-changers are stripped from adds and
    returned separately so the caller can log them. At B3+ this is a
    no-op pass-through -- game-changers are allowed and the WotC 3-card
    cap is enforced elsewhere (deck-level audit, not the curator).

    Card-name comparison is case-insensitive: the game-changers set
    holds the canonical Scryfall casing, but EDHREC scrape / Moxfield
    export sometimes vary, so we fold both sides before comparing.
    """
    if bracket >= _BRACKET_NO_GAME_CHANGERS_THRESHOLD:
        return list(adds), []

    gc_set = _load_game_changers()
    gc_lower = {g.lower() for g in gc_set}

    kept: list[str] = []
    dropped: list[str] = []
    for card in adds:
        if card.lower() in gc_lower:
            dropped.append(card)
        else:
            kept.append(card)
    return kept, dropped


def _safe_lookup_card(lookup_fn, name: str):
    """Wrap a scryfall_client.lookup_card call so a network blip on
    one card doesn't cascade into the broader curator pipeline.
    Returns the card dict or None on any failure."""
    try:
        return lookup_fn(name)
    except Exception:  # noqa: BLE001
        return None


def enforce_color_identity(
    adds: list[str], deck_color_identity: Optional[str],
) -> tuple[list[str], list[str]]:
    """Split ``adds`` into (kept, dropped) by the deck's color identity.

    Commander format requires every mainboard card's color identity
    to be a subset of the deck's commander color identity. A green
    creature in a mono-red Goblin deck is illegal; Forge refuses to
    load such decks. The curator system prompt asks for this rule
    explicitly, but Claude occasionally proposes off-color picks --
    especially at high model temperature or when the deck's CI is
    unusual (colorless, partner pairs).

    ``deck_color_identity`` semantics:
      "WUBRG"  -- five-color deck, anything goes
      "R"      -- mono-red, only red + colorless legal
      ""       -- colorless commander (e.g. Karn), only colorless OK
      None     -- COULDN'T RESOLVE the commander's identity. Skip the
                  filter entirely -- pass through all adds. Better
                  noisy than empty when we can't verify; this avoids
                  rejecting every add against a phantom "colorless"
                  deck when the commander isn't in Scryfall (typo,
                  test fixture, etc.).

    Lookups go through ``scryfall_client.lookup_card`` which is disk-
    cached, so a 5-add filter typically costs ~0 wall time on a warm
    cache. Cards Scryfall doesn't return (typos, custom cards) are
    treated as IN-color so a Scryfall outage doesn't strip every add
    -- we don't reject what we can't verify.

    Returns (kept, dropped) preserving input order. Same shape as
    ``enforce_bracket_caps`` so the auto_propose pipeline can chain
    the two filters identically.
    """
    if not adds:
        return [], []
    # None deck CI = couldn't resolve, skip filter entirely so
    # unverifiable decks don't strip everything.
    if deck_color_identity is None:
        return list(adds), []
    # Empty deck CI = colorless commander (e.g. Karn). Empty target
    # only permits cards that are themselves colorless. Build the
    # allowed-letter set from the WUBRG-ordered string.
    deck_set = set(deck_color_identity.upper()) if deck_color_identity else set()

    # Lazy import: scryfall_client.lookup_card has side effects (disk
    # cache) that we don't want firing on module-load test collection.
    from .scryfall_client import lookup_card

    kept: list[str] = []
    dropped: list[str] = []
    for card_name in adds:
        try:
            card = lookup_card(card_name)
        except Exception:  # noqa: BLE001 -- a Scryfall failure shouldn't
            # take down the whole curator call. Treat unverifiable cards
            # as in-color (better than rejecting everything on outage).
            kept.append(card_name)
            continue
        if card is None:
            # Scryfall 404 -- typo, custom card, or a name Claude
            # invented. Treat as in-color rather than rejecting silently;
            # the existing hallucination flag (name_known) catches this
            # category elsewhere in the response.
            kept.append(card_name)
            continue
        card_ci = {
            c.upper() for c in (card.get("color_identity") or [])
            if isinstance(c, str)
        }
        if card_ci.issubset(deck_set):
            kept.append(card_name)
        else:
            dropped.append(card_name)
    return kept, dropped
