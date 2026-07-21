"""LLM-aided variant — Claude advisor for the deck improvement loop.

Contains the system prompt + ``_claude_swap_recommendations``
function. The prompt teaches Claude to reason in two passes (adds
first, then cuts), prioritizing bracket-peer references when
available and EDHREC data as fallback. Hard fences in the prompt
prevent cutting any land or universal staple.

Extracted from ``improvement_advisor.py`` as part of the per-source
module split. External code keeps importing from
``commander_builder.improvement_advisor``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Optional

from ._advisor_models import DeckDiagnosis, SwapRecommendation
from ._llm_json import extract_json_object
from .edhrec_client import CommanderPage


_CLAUDE_ADVISOR_SYSTEM = """You are a deck-tuning advisor for Magic: the Gathering Commander. \
Given:
  - The deck's commander(s)
  - The current decklist
  - Performance signals from prior simulations (win rate, draw rate, fastest loss, etc.)
  - EDHREC inclusion% / synergy% data for this commander (aggregate across all brackets)
  - When available: `bracket_peer_references` — top-N highest-liked
    Moxfield decks for the same commander **at the same bracket**.
    These are tuned builds. Their card overlap is the strongest
    signal for what belongs in a deck at this power level.

Think in two passes:

1. **Adds — what's missing?** Scan `bracket_peer_references.cards_by_frequency`.
   Cards appearing in a majority of the references but NOT in the
   `current_decklist` are the strongest add candidates. EDHREC data
   is a fallback when peer references aren't available or the peer
   set is small. Don't recommend universal staples (Sol Ring, Arcane
   Signet, Command Tower, basic lands) — they're either already
   present or deliberately excluded.

2. **Cuts — what's not pulling weight?** Cards in `current_decklist`
   that don't appear in any peer reference AND aren't role-essential
   are candidates. **NEVER cut any land** (basic, dual, fetch,
   shock, MDFC, utility) — manabase decisions are deliberate.
   **NEVER cut universal staples**. Cut card-disadvantage pieces or
   off-archetype slots first.

Return JSON ONLY (no prose, no markdown):

{
  "rationale": "one paragraph diagnosing the weakness and explaining the swap strategy, citing references by name when relevant",
  "added": ["Card A", "Card B", ...],
  "removed": ["Card X", "Card Y", ...]
}

Constraints:
- Recommend 4-8 adds and 4-8 removes (the deck must stay at 99 cards in the [Main]).
- Each `added` card SHOULD appear in `bracket_peer_references` when present, OR in EDHREC's data; rare deviations need strong synergy reason in the rationale.
- Each `removed` card MUST be in the current decklist.
- **Don't recommend cutting ANY land** (basic, dual, fetch, shock, MDFC, utility).
- Don't recommend cutting Sol Ring, Arcane Signet, Command Tower, or other universal staples.
- Match adds and removes 1-for-1 by count.
- Focus the rationale on the weakness signals + reference-deck consensus, not generic deck-building advice.
"""


DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"


def _claude_swap_recommendations(
    deck_filename: str,
    bracket: int,
    deck_cards: set[str],
    diagnosis: DeckDiagnosis,
    edhrec_page: CommanderPage,
    model: str = DEFAULT_CLAUDE_MODEL,
    bracket_peers_summary: Optional[dict] = None,
    api_key: Optional[str] = None,
) -> tuple[list[SwapRecommendation], str]:
    """LLM-aided variant. Falls back via NotImplementedError when no
    API key or SDK — caller catches and degrades to heuristic.

    ``api_key`` (optional) is an EXPLICIT per-call Anthropic key,
    threaded down from the web layer's BYO-key header/config. None
    means "use the process credential" (``ANTHROPIC_API_KEY`` from
    the shell env or the credentials file) exactly as before. The
    explicit parameter exists so a threaded server can serve two
    concurrent requests with two different keys WITHOUT mutating the
    process-global ``os.environ`` — the old stage-key-in-env dance
    let concurrent requests read each other's keys (or wipe one
    mid-API-call on restore). Never write this value into os.environ.

    ``model`` lets callers pick a tier (haiku for cheap, sonnet for
    default quality, opus for deepest reasoning). The default tracks
    ``DEFAULT_CLAUDE_MODEL`` so existing callers don't change
    behavior; cost-sensitive runs can pass
    ``model='claude-haiku-4-5'`` for a ~3-5x cheaper request.

    ``bracket_peers_summary`` (optional) carries a compact
    frequency-keyed view of the top-N highest-liked Moxfield decks
    for this commander at this bracket. When present, Claude is
    instructed to prioritize adds from cards in the majority of peer
    references that are missing from the user's deck — strongest
    archetype-specific signal available. Add ~3-4K input tokens to
    the request (~$0.01 extra on Sonnet) but produces materially
    better recommendations than EDHREC-only context.
    """
    # Resolve the effective key: explicit per-call key wins; else fall
    # back to the process env (shell / credentials-file). Resolved ONCE
    # here and passed straight to the client constructor — never staged
    # through os.environ, so concurrent calls can't cross-bill.
    effective_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not effective_key:
        raise NotImplementedError("claude advisor requires ANTHROPIC_API_KEY")
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise NotImplementedError("claude advisor requires `pip install anthropic`") from exc

    # Compact the EDHREC payload — full pages are too large.
    edhrec_compact = {
        "commander": edhrec_page.commander_name,
        "deck_count": edhrec_page.deck_count,
        "top_cards": [
            {"name": c.name, "inclusion_pct": c.inclusion_pct, "synergy_pct": c.synergy_pct}
            for c in edhrec_page.top_cards[:50]
        ],
        "high_synergy_cards": [
            {"name": c.name, "inclusion_pct": c.inclusion_pct, "synergy_pct": c.synergy_pct}
            for c in edhrec_page.high_synergy_cards[:30]
        ],
    }
    user_payload = {
        "deck_filename": deck_filename,
        "bracket": bracket,
        "current_decklist": sorted(deck_cards),
        "performance": asdict(diagnosis),
        "edhrec_signals": edhrec_compact,
    }
    # Include bracket-peer references only when actually available.
    # Omitting the key entirely (rather than sending an empty/null
    # value) keeps the prompt smaller for obscure commanders where
    # no references could be fetched.
    if bracket_peers_summary:
        user_payload["bracket_peer_references"] = bracket_peers_summary
    user_message = json.dumps(user_payload, indent=2)

    client = Anthropic(api_key=effective_key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=_CLAUDE_ADVISOR_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text
    if not text.strip():
        raise RuntimeError("claude advisor: empty response")

    # Shared robust extractor (see _llm_json): handles fenced JSON even
    # after a prose preamble, prose trailers, and braces-in-strings — the
    # old startswith-``` strip here missed all of those and let a bare
    # JSONDecodeError escape. On unparseable/truncated output this raises
    # LLMJsonError with the response head/tail; the advise() dispatcher's
    # broad except catches it, prints a loud WARN naming the exception,
    # and degrades to the heuristic advisor (that degradation IS the
    # advise() contract — unlike propose(), there's a real heuristic
    # fallback here, not a misleading file-not-found).
    parsed = extract_json_object(
        text,
        context=f"claude advisor (model={model}, response {len(text)} chars)",
    )
    rationale = str(parsed.get("rationale", ""))
    recs: list[SwapRecommendation] = []
    for name in parsed.get("added", []) or []:
        recs.append(SwapRecommendation(
            card=str(name), action="add",
            reason="recommended by Claude advisor", evidence={"source": "claude"},
        ))
    for name in parsed.get("removed", []) or []:
        recs.append(SwapRecommendation(
            card=str(name), action="cut",
            reason="recommended by Claude advisor", evidence={"source": "claude"},
        ))
    return recs, rationale
