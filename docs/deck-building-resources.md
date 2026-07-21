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

## Implemented (2026-05-24) — all three shipped
1. ✅ **EDHREC `/top` time-windowed candidates** — `edhrec_client.fetch_top_cards(slug)`
   (year/month/week or card type) + `commander-top` CLI. Recency-aware
   staples. (9da091e)
2. ✅ **Deck-health target ratios** — `staples.ROLE_TARGETS` +
   `role_target_report()`, wired into `compute_deck_health` as a
   `role_targets` signal (flags roles BELOW the template minimums —
   complements the saturation guard's EXCESS check). (5ad1db3)
3. ✅ **Combo detection** — `combo_detection.py`: `detect_combos_in_deck()`
   + `commander-combos` CLI (`--deck` / `--refresh`). Hand-curated offline
   fallback + a top-1500 `data/combos.json` built from Commander
   Spellbook's API (the full 500MB export is too big to bundle). (95c05a2)

### Follow-ups
- ✅ **Wire `fetch_top_cards` into the advisor (2026-05-26).** The heuristic
  recommender now takes a `trending` set sourced from
  `edhrec_client.fetch_top_cards("month")` (lazy + best-effort in
  `improvement_advisor._fetch_trending_lazy`). It *re-ranks* existing
  commander-relevant candidates — a card spiking this month floats above a
  stale all-time staple and its rationale says "trending now on EDHREC
  /top" (`evidence.trending=True`). Re-rank only, so a failed fetch can
  never introduce off-archetype/off-color noise.
- ✅ **Surface `role_targets` deficits in the web audit UI (2026-05-26).**
  A 6th deck-health tile ("Role targets") shows the count of under-built
  roles with a tooltip itemizing count/target/deficit per role
  (`deck_health_ui.js`). Complements the saturation guard's excess check.
  `_EMPTY_DECK_HEALTH` gained the `role_targets` key so the empty/failed
  path keeps the same shape.
- ✅ **Feed `detect_combos_in_deck` into bracket enforcement (2026-05-26).**
  `combo_detection.assess_deck_brackets(deck_text, bracket)` maps each
  detected combo to its WotC bracket floor (two-card infinite/win combo →
  B4; 3+-card → B3; value loop → B1), flags combos that exceed the
  declared bracket, and recommends the bracket the deck actually plays at.
  Surfaced in `/api/audit` (+ stream) as `combo_assessment`, rendered as a
  red bracket-pressure banner / collapsed info panel
  (`deck_health_ui.js:renderComboAssessment`).
