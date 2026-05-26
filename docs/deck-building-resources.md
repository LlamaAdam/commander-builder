# Deck-building reference resources

Operator-supplied references (2026-05-24) for improving the advisor /
curator's deck-building knowledge. Each entry notes how it maps to what
commander-builder already does and where it could drive an improvement.

> Note: these are external sites. The repo's clients already reach EDHREC
> + Moxfield with browser-like headers (bare requests 403). Treat the
> rest as human-reference / design input, not scrape targets.

## EDHREC top cards (highest-value, already integrated)
- **https://edhrec.com/top** — most-played cards; filterable by **time
  window (past 2 years / month / week)** and by **card type**.
- Maps to: `edhrec_client.py` + the advisor's heuristic source already
  pull EDHREC. The **time-windowed `/top`** view is the new hook —
  recency-aware "trending staples" by type could feed a `--source
  edhrec_top` or enrich candidate ranking (a card spiking in the last
  month is a stronger add than a stale all-time staple).

## Deck-template ratios (refine saturation thresholds)
- **https://archidekt.com/decks/1048638** — EDH deck template (role
  ratios: ramp / draw / removal / wipes / etc., read the description).
- Maps to: `staples.ROLE_SATURATION_THRESHOLDS` (ramp=12, draw=12,
  removal=10, wipe=6, protection=7, tutor=8, finisher=14) + the
  deck-health tiles. Use this template to **sanity-check / tune those
  target counts** and the audit's deck-health targets.

## Lands / manabase guide
- **https://archidekt.com/decks/58548** — EDH lands list (read the
  description) — land counts + utility-land selection.
- Maps to: the advisor's **manabase recommendations** + budget-mode
  land filtering. Source for land-count targets per bracket and which
  utility lands to prioritize.

## Infinite-combo finder (capability GAP → future feature)
- **https://combo-finder.com/** — finds infinite combos among a card set.
- commander-builder has **no combo detection** today. Potential feature:
  flag combos present in a deck (and bracket-legality — combos push a
  deck up brackets), or suggest combo pieces. Bracket enforcement
  (`game_changers` / `enforce_bracket_caps`) is the nearest existing hook.

## General build guides (human reference / curator-prompt material)
- **https://www.threeforonetrading.com/en/commander-deck-build-guide** —
  overall build process.
- **https://www.mtgsalvation.com/articles/49793** — first-Commander-deck
  walkthrough + "interesting cards".
- **https://tappedout.net/mtg-forum/commander/commander-resource-kaldheim-updated/**
  — broad Commander resource kit.
- Maps to: design input for the **curator system prompt** (`_advisor_claude.py`
  / proposer) — the principles in these guides (curve, interaction count,
  wincon density) can sharpen what the curator optimizes for.

## Most actionable next steps (not yet started — operator's call)
1. **EDHREC `/top` time-windowed candidates** — recency-aware staple
   suggestions; smallest lift, builds on existing edhrec_client.
2. **Tune `ROLE_SATURATION_THRESHOLDS` + deck-health targets** against the
   archidekt template ratios.
3. **Combo detection** (new capability) — bigger lift; ties into bracket
   enforcement.
