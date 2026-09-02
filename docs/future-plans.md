# Future plans (consolidated)

> Consolidated 2026-05-26 from the per-FP plan docs; **reordered
> 2026-07-25** — active / work-needed items at the top, shipped or
> fully-parked reference material at the bottom. **STATUS.md -> Parked
> plans is the authoritative status**; this file collects the detailed
> findings/plans in one place.

---

# ── ACTIVE / WORK NEEDED ──────────────────────────────────────────────

# FP-019 — Primer-derived heuristics: encode the 40-primer synthesis

**STATUS 2026-08-29: slices 019.1–019.6 SHIPPED** on
`feat/fp-019-primer-heuristics` (KB asset `data/primer_kb.json` +
`primer_kb.py`; `consistency_targets.py` + deck_health tile;
`staples.contextual_role_targets` / `infer_commander_role`; four
CardScore penalties; `nonbo_lint.py` + tile; judge/Claude-advisor
grounding + budget-mode KB swaps). Remaining follow-ups: wire an
archetype/plan classifier so the conditional consistency floors and
quota context activate automatically; §16 re-harvest path (changelogs,
notable exclusions); own-stax-vs-own-combo nonbo class (needs combo-line
awareness).

Source (2026-08-29): `primer_harvest/deckbuilding_heuristics.md` — a
cross-primer synthesis of 40 community primers (21 Moxfield top-liked +
19 Archidekt) — and `primer_harvest/primer_knowledge_base.json` (per-deck
structured records: gameplan, construction rules, mulligan trees,
sequencing, author-described lines with card-name presence checked
against each deck's exact mainboard (not rules-verified), budget swaps,
heuristics). Section numbers below (§) cite the heuristics doc.

## What the system already does (validate/tune, don't rebuild)

| Primer lesson | Existing seam | Gap |
|---|---|---|
| §1 per-color source ladder (18/23/25/28 by turn+pips) | `deck_builder_manabase.color_source_targets` — full Karsten table, build + report | None — numbers agree. Fetch rules below are new |
| §1 land-drop / commander-on-curve / mulligan probabilities | `consistency.py` (hypergeom + seeded Monte Carlo, wired into deck_health 2026-08) | Thresholds aren't named targets; primers give convergent floors (85% 3rd drop, ~90% CA by T5, 13-enabler rule) |
| §9 interaction floors rise with bracket; instant-speed share | `interaction.py` matrix + `BRACKET_INTERACTION_MINIMUMS` | Asymmetry/parity signal (wipes that spare your own board) not modeled |
| §11 tutor density as bracket dial (weighted, not rule) | `bracket_estimator` DEFAULT_WEIGHTS (post-repeal handling matches §11 exactly) | Tuning input only |
| §7 combo presence / one-away | `combo_detection.py` | No `min_requirements` audit, no assembly probability |
| §4 role quotas | `staples.ROLE_TARGETS` + commander credit | Quotas are FLAT; primers show they're a function of archetype/commander-role/avg-MV/bracket |

## Slices (cheap-first)

**019.1 — Package the KB as a data asset.** Copy
`primer_knowledge_base.json` → `data/primer_kb.json` with a loader
module mirroring the `combos.json` / `game_changers` pattern (offline
floor, refresh path later per §16 — changelogs and notable-exclusion
sections are the richest future harvest). Exposes: per-commander
consensus records (§13 — note the two-Ur-Dragon-profiles finding:
encode PROFILES per commander, not one truth), win-line
`min_requirements`, budget-swap table.

**019.2 — Named consistency targets.** A `CONSISTENCY_TARGETS` dict
(3rd-land-drop ≥ .85; on-curve commander color .85–.95; CA by T5 ≥ .90;
T1-enabler count ≥ 13 *when the deck declares a T1-enabler plan*;
tapped-fetchables ≤ 2 for proactive decks; §1 reveal-engine
hypergeometric floor with post-shuffle depletion) evaluated from
`consistency.py` + `deck_builder_manabase` outputs, surfaced as a
deck_health tile. ADDITIVE ONLY — same doctrine as the 2026-08
consistency wiring: reported, NOT folded into `compute_health_grade`
without its own reviewed re-pin.

**019.3 — Context-sensitive quotas.** `role_target_report` gains an
optional context (archetype, commander-role from a §3 taxonomy
classifier, avg MV, bracket); ROLE_TARGETS becomes the fallback when
context is absent, so every existing caller is unchanged. Seed the
adjustment rules from §4's generator sketch (+lands/−rocks as avg MV
drops; +ramp+protection at total commander-reliance; interaction floor
by bracket).

**019.4 — CardScore terms (flag stays OFF).** New gates/modifiers from
§2/§3/§5: dead-without-commander gate; value-delay/tempo-fail modifier
(aggro/snowball); capped-vs-uncapped engine; tutor card-delta by
strategy; variance-card audit (§2: success-definition → target count →
hit probability vs build-relative threshold). FP-015's validation gate
still governs enablement — these are ranking-prior refinements, not a
bypass.

**019.5 — Nonbo lint.** §14's table as data-driven pairwise checks
(new `nonbo_lint.py`, audit tile + advisor pre-filter). First
anti-synergy detector in the tree; also covers the §3
commander-dependence check ("does this card do anything with the
command zone occupied?").

**019.6 — Advisor/judge grounding.** Feed 019.1's per-commander
consensus + §5 WHY-rules into `_advisor_claude` / `_deck_judge_prompt`
context blocks (same clip-with-marker cap as primers, via
`primer.clip_for_prompt`), and the budget-swap table into
`improvement_advisor`'s budget mode (§10 spend order: lands → ramp/draw
→ threats; function-preserving swaps).

## Non-goals

§8 piloting/sequencing axioms (Forge's AI can't be steered by them —
they'd be dead config) beyond what `_deck_judge_prompt` already quotes
from primers; §12 archetype playbooks as hard rules (they're context
for 019.3/019.6, not validators); any auto-enable of CardScore.

# FP-018 — Adopt a deck: primer-guided understanding + gentle personalization

Owner, 2026-08-27: "the primer could let someone use a deck and
understand it and do small modifications on that deck list to what they
like doing rather than a crazy overhaul."

The improve loop optimizes; this flow ADOPTS. A player finds a deck
with a primer, and the app helps them (1) understand it and (2) make it
theirs — small, identity-preserving changes steered by what THEY like,
with the overhaul path structurally off the table.

## Why the pieces already exist

| Need | Existing seam |
|---|---|
| Primer arrives with the deck | `archidekt_client` captures `description` (Quill Delta JSON — parser needed, shape pinned by `tests/fixtures/hazel_primer.md`) |
| "Small, not crazy" | `change_budget` polish tier; the rebuild tier is ALREADY opt-in (decision C4) |
| "Don't touch the identity" | User-authored `Protect=` metadata; `intent.key_wincons` and politics guards in their existing flows |
| "What the pilot likes" | `intent` themes soft-bias the advisor's candidate pool; `deck_builder_personalize` (FP-014.3) already does like-for-like preference passes under the 99/CI/singleton invariants |
| "Is this change true to the deck" | `deck_judge` (observe-only) judges against intent — needs the free-text field its boundary tests were built to force a decision on |

## Slices

**018.1 — Primer ingestion.** A Quill-Delta→text parser (one op-walk;
the Hazel fixture is the test vector); imports store the rendered
primer beside the deck (sidecar `<deck>.primer.md`, not `[metadata]` —
primers are paragraphs, not directives); `commander import` reports
"primer captured (N words)".

**018.2 — Free-text intent.** `Intent` gains `stated` (the deck's
primer) and `pilot_preferences` (the adopter's own words). Both flow
into the judge's intent block — updating the two tests that pin the
Phase-1 boundary, which is the contract change they exist to force —
and into the advisor as a soft bias, exactly as themes do today.
Grounding rule unchanged: free text steers ATTENTION, never invents
card facts; every card named still resolves through the oracle cache.

**018.3 — `commander adopt <deck>`.** Two outputs, in order:
1. UNDERSTAND: a grounded explanation — the primer's plan cross-checked
   against the actual list (engine pieces, role distribution, what the
   primer says to keep/mulligan, where the wincons live), flagging
   where primer and list disagree.
2. PERSONALIZE: swap suggestions hard-capped at the polish tier,
   honoring only explicit `Protect=` locks while retaining primer links
   as exact-name evidence, candidates biased by pilot_preferences via
   the FP-014.3 passes generalized to imported decks. Every suggestion
   says which preference it serves and what it preserves. Existing
   Commander legality checks appear as warnings and do not block this
   read-only flow. The rebuild tier is not reachable from this flow at all.

**018.4 — Primer corpus (supporting study).** Batch-capture primer'd
decks via the CI capture lane; distill how real primers explain decks
(what a good explanation covers) and where real lists diverge from
ROLE_TARGETS — evidence-backed patches only, per the corpus-norms
discipline (C5 stays parked until its A/B).

## Non-goals

- No sim gating: adopt is a comprehension-and-taste flow; Forge
  verdicts remain available but are never required.
- No free-text card invention: the explainer and suggester cite only
  cards present in the list or resolved via the oracle cache.
- No overhaul: if a deck is genuinely misbuilt for its primer, adopt
  SAYS so and points at the improve loop; it does not become it.


# FP-017 — cEDH tournament results as a fourth corpus source (edhtop16)

**Status (2026-08-05): importer SHIPPED. Exploratory data source, NOT a
predictor. No gate has been run on it and none is claimed.**

## SCOPE AND HONESTY NOTE — read this before using any number from it

This is the part that matters more than the code:

1. **Bracket-5 humans only.** edhtop16 aggregates cEDH tournament
   results. Every statistic it yields describes what people registered
   and won with in competitive events. **No claim is made that any of
   it transfers to casual brackets.** A B2-B4 deck is not trying to do
   what a cEDH deck is doing, and "the winning lists all run Force of
   Will" is advice about a different game. The importer therefore gates
   itself to bracket 5 *in code, in two places* — the client
   (`fetch_top_decklists` refuses any bracket != 5 without issuing a
   request) and the corpus builder (`build_reference_corpus` only
   consults the source when `bracket == 5`) — with tests asserting a
   B3 corpus build contains zero tournament decks. `bracket=None` is
   refused too: an unknown bracket is not a bracket-5 bracket.
2. **Exploratory source, not a predictor.** Nothing here has passed a
   pre-registered gate. Presence rates are a *play rate among
   top-finishing entries*, sampled best-finish-first, so they are
   soaked in selection bias: "strong players brought it and did well"
   is not "this card would raise your win rate". Per-card win rates are
   confounded by the deck the card sits in. These are descriptive
   statistics about a population, full stop.
3. **Why we are this careful: three prior gate failures.** FP-015
   whole-ordering failed twice (2026-07-28 n=6, 2026-07-31 n=9), the
   FP-015 per-swap design failed (2026-08-05, pooled rho = −0.090,
   p = 0.7048 — *negative*, not underpowered), and FP-002 closed
   REFUTED at n=93 after an n=66 false positive that looked exactly
   like signal. The pattern in all three: a plausible heuristic, an
   honest gate, a fail. FP-017 gets the same treatment or it does not
   ship as a predictor.

## WHY it is worth importing anyway

The FP-002 closure named the only honest reopening path: a **new
feature substrate**, not more games on the same features. One diagnosis
of why every prior attempt failed is that all of our signals were
EDHREC-derived — they encode human *deckbuilding preference* (what
people put in decks) with no human *win* data to anchor them.
Preference and performance are different quantities, and we were
regressing performance on preference.

cEDH tournament aggregators are the only large-scale source of real
humans actually winning games. That makes edhtop16 a genuinely
different substrate rather than a fourth flavor of the same one. It may
still fail a gate. But it fails for a new reason, which is worth
something.

## WHAT the API actually offers (probed live 2026-08-05)

edhtop16.com serves a public, unauthenticated **GraphQL** API at
`/api/graphql`. No HTML scraping is performed or needed. Introspected
schema facts the client depends on:

- `commanders(first:, sortBy: CommandersSortBy!, timePeriod:
  TimePeriod!, minEntries:, minTournamentSize:, colorId:, after:)` →
  `CommanderConnection`. `CommandersSortBy` ∈ {CONVERSION, POPULARITY,
  TOP_CUTS, WINRATE}; `TimePeriod` ∈ {ALL_TIME, ONE_MONTH,
  THREE_MONTHS, SIX_MONTHS, ONE_YEAR, POST_BAN}.
- `Commander.stats(filters: CommanderStatsFilters!)` → `{count,
  conversionRate, winRate, topCuts, metaShare}`. The `filters` argument
  is **required** — a bare `stats` is a query error.
- `Commander.entries(first:, sortBy: EntrySortBy, filters:
  EntriesFilter!)` → `EntryConnection`. `EntriesFilter` **requires**
  both `timePeriod` and `minEventSize`.
- `Entry` → `{standing, wins, losses, draws, winRate, decklist,
  maindeck {name}, tournament {name TID size tournamentDate topCut}}`.
- Also present, unused so far: `Commander.staples`,
  `Commander.cardWinrateStats(cardName)` (with/without-card conversion
  split), `Card.playRateLastYear`, `tournaments`, `player`,
  `monthlySeatWinRates`, `leaderboard`.

Cost profile: **one POST** returns a commander's aggregate stats *and*
~20 full 98-card maindecks in ~6.5s — no N+1 detail fetch, unlike the
Archidekt source. That is why this source's request budget is 1/commander
while Archidekt's is ~26.

Failure shapes: the site is behind Cloudflare, and an unknown commander
comes back as **HTTP 200 + `{"errors": [...], "data": {"commander":
null}}`**. A 200 is therefore not proof of data, which is precisely the
case the house NO-CACHE-ON-EMPTY convention exists for.

## WHAT SHIPPED

- `src/commander_builder/edhtop16_client.py` — house client
  conventions: injectable `fetch_json(url, payload)` seam, disk cache
  under `.cache/edhtop16/` at a 24h TTL, **no-cache-on-empty** (an
  empty parse is warned loudly and never written), 429/5xx retry with
  `Retry-After` honored and clamped (the PR #40 pattern, reusing
  `edhrec_client._parse_retry_after` / `MAX_RETRY_AFTER_SEC`),
  deterministic 4xx never retried, loud degrade to empty on failure.
  Records: `CommanderStats`, `TournamentEntry`, `CardTournamentStats`.
  `card_presence()` is pure and refuses to report below
  `MIN_ENTRIES_FOR_PRESENCE` (8) — "we don't know" must never render
  as "nobody plays it".
- Corpus integration: a fourth source in `bubble_analysis`'
  `build_reference_corpus`, behind the bracket-5 gate, honoring the
  PR #40 partial-source rule (asked-and-got-nothing marks `edhtop16`
  partial and takes the 1h TTL; gated-off is **not** partial — nothing
  failed, we chose not to ask). `ReferenceCorpus.tournament_decks`
  records how many merged decks came from this source, so a clean B3
  corpus is assertable.
- `scripts/margin_analysis.py --features tournament` — an exploratory
  regressor lane following the PR #58 `card_score` pattern, with the
  same multiple-testing honesty output. **Cache-only** (no network in a
  regression loop, same rule as `card_score_features`' `corpus=None`)
  and bracket-5-gated, so a soak made entirely of B3 decks reports
  every feature unavailable *and says why*.
- CLI: `commander-tournament` (leaderboard mode / per-commander mode);
  every output path prints the scope note.

## HOW to run a real import

```bash
# top cEDH commanders by top-cut conversion, last 6 months, events 60+
commander-tournament --sort-by CONVERSION --time-period SIX_MONTHS -n 25

# one commander: aggregate stats + top-finishing decklists + card presence
commander-tournament "Kinnan, Bonder Prodigy" -n 20 --min-event-size 60
```

## NOT planned

Wiring tournament presence into the advisor's recommendations for any
bracket, or into `CardScore`, without its own pre-registered gate.
Given FP-015's and FP-002's records, the prior on "this new heuristic
signal predicts margin" should be low.

---

# FP-015 — Unified per-card scoring formula (`CardScore`)

**Status (2026-08-03): SHIPPED behind default-off flag; whole-ordering
validation CONCLUDED (gate FAIL 2026-07-28 and 2026-07-31 — see the
dated GATED RESULT sections); per-swap validation harness BUILT
(PR #63) with its gated run IN FLIGHT — the flag's fate rides on that
result. `COMMANDER_BUILDER_CARD_SCORE` remains default-off.**
Original implementation note (2026-07-25): The spec below (2026-07-24) was
implemented alongside the build-order items from
`docs/archive/REVIEW-2026-07-24.md`: `card_score.py`
(flag-gated, default off), `deck_legality.py`, `consistency.py`,
`interaction.py`, commander-aware `deck_health`, and the
bracket-caller plumbing, plus ~4,400 lines of new tests (work was
interrupted twice by usage-limit resets and resumed 2026-07-25).
Remaining: the `test_compare_versions` hermeticity fix (in flight
separately) → full green run → PR → the tier-3 ranking validation
below (top-k-by-score vs bucket-order, both A/B simmed) before the
flag defaults on.

## Addendum 2026-07-25 — deck-level verdict + bubble cards (first cut shipped)

**Operator direction:** stop forcing 1–10 swaps. First decide whether
the deck is *already good overall*; then surface only the cards that
are **on the bubble** — weak in this deck AND rarely played in
successful builds of the same commander AND cheaply replaceable. Ground
"what good decks look like" in the top ~50 liked Moxfield builds,
EDHREC (average deck + salt), and further sites as they're added.

**Shipped:** `bubble_analysis.py` + 33 tests.
- `build_reference_corpus(commander, bracket, n=50)` — top-liked
  Moxfield decks (`find_top_liked_decks_for_commander`) + EDHREC
  average deck + salt list, disk-cached 168h beside the EDHREC caches.
  Fetchers are injectable — the seam for a third reference site
  (Archidekt is the natural next one).
- `score_deck(...)` — 0–100 whole-deck score over reference_alignment
  (0.40) / role_fit (0.25) / mana_fit (0.25, Karsten) / salt_fit
  (0.10, B≤3 only), unavailable components renormalized away. Verdict
  bands → change budget: ≥75 "keep" (0–2 swaps), ≥55 "polish" (2–5),
  else "overhaul" (0 swaps — fix structure first, FP-002's finding
  operationalized).
- `find_bubble_cards(...)` — bubble = cut_score ≥ 55 AND reference
  support ≤ 0.25 (guard-railed/protected cards never qualify); each
  bubble paired with the best replacement from the corpus'
  high-consensus absentees (≥ +10 score margin, greedy so adds aren't
  double-claimed); ranked by ease = score gap × replacement consensus.
- CLI: `python -m commander_builder.bubble_analysis <deck.dck>
  [--bracket N] [--refs 50] [--no-network] [--json]`.

**Also shipped (2026-07-25, later):** advisor integration.
`AdviceReport` gained optional `deck_score` / `bubble_cards` fields
(default None/empty — flag-off behavior is untouched, but the flag-off
invariant is *behavioral, not byte-level* for serialized shapes:
`to_dict()` and every JSON surface built from it always carry both
keys as null/[] — schema additive by design, see
`AdviceReport.deck_score`);
`bubble_analysis.apply_verdict_to_report()` returns a NEW report with
recommendations trimmed to the change budget (advisor-ranked adds
capped; cuts reordered **bubble-first** then capped; `*_essentials`
structural recs always survive; `overhaul` trims nothing — the recs ARE
the structure fixes). `commander-advise` runs the verdict pass
automatically when `COMMANDER_BUILDER_CARD_SCORE` is on (fail-quiet,
corpus cache-first) and prints a "Deck verdict" section. 6 more tests.

**Also shipped (2026-07-25, still later):** web audit backend. The
verdict pass now lives at the TOP of `routes_audit._build_audit_payload`
(flag-gated, fail-quiet) so the sync `/api/audit` and SSE stream
payloads stay identical (the 2026-06 warning-asymmetry lesson), the
trimmed recommendations drive proposed-text/pricing consistently, and
the payload carries `deck_score` + `bubble_cards` keys (None/[] when
the flag is off). 2 web tests.

**Also shipped (2026-07-25, evening):** the audit UI verdict panel
(`renderDeckVerdict` — pill + budget line + collapsible "why" + top-5
bubbles; live-verified in Chrome via SSE, zero console errors) and
**Archidekt as the third corpus source** (`archidekt_client.py` —
public JSON API, no auth, per-deck `edhBracket` soft-filter,
category-flag-honoring mainboard extraction, own request budget of 25;
merged into `build_reference_corpus` via the `fetch_extra_lists` seam;
live-verified: 14-deck merged Krenko corpus). Also fixed: sim-job
sidecar now persists BEFORE the terminal status is observable
(restart-recoverability hole + the reattach-test flake).

**Also shipped (2026-07-25, night):** the auto-curate path.
`auto_curate_main` runs the same flag-gated verdict pass after
`advise()` — the curator prompt is built from the budget-trimmed
candidate set, the text mode prints a `deck verdict:` line, and the
`--json` payload carries `deck_score` (null when the flag is off).
With that, **every advise surface (CLI, web sync+SSE, auto-curate) is
budget-aware**. Also fixed en route: EDHREC/game_changers diagnostics
now write to stderr (an EDHREC bot-challenge page was corrupting every
--json mode via a stdout warning).

**Also shipped (2026-07-25, late night):** the tier-3 validation
harness (`scripts/validate_card_score.py` — both rankings via the real
production `advise()` paths, staged through the shared legality path,
each arm A/B simmed vs the original; a 3-deck × 40-game pilot is
running detached, see STATUS) and **FP-015 seam #4**: the FP-014
commander-page fallback pool is now corpus-augmented and
CardScore-ordered when the flag is on (`_score_ordered_fallback` —
flag off / any failure = byte-identical legacy pile).

**Also shipped (2026-07-25, post-merge):** the bubble arm of the tier-3
harness. `scripts/validate_card_score.py` now takes `--arms` (any two
or more of `bucket` / `score` / `bubble`); the `bubble` arm runs the
flag-on ranking through `apply_verdict_to_report`, so the deck's own
change budget decides how many swaps and cuts arrive bubble-first.
**When `bubble` is present its budget caps every other arm** — same
number of adds and cuts everywhere — because otherwise the comparison
would conflate "fewer swaps" with "better-chosen swaps" and only the
second is a claim about ranking. A 0-swap verdict skips the deck
rather than simming it against itself. 10 more tests (19 in
`tests/test_validate_card_score.py`), all offline via injected
`advise` / `verdict` / `corpus` / `compare` fns.

### Tier-3 pilot RESULT — 2026-07-26 (inconclusive; flag stays default-off)

The 3-deck pilot launched 2026-07-25 17:21 local finished 21:36 local
(`_tier3_pilot_result.json`). Config: `--bracket 3 --k 5 --games 40`,
arms `bucket` (current insertion order) vs `score` (CardScore ranking),
each arm A/B simmed against the unmodified deck.

| Deck | `bucket_margin` | `score_margin` | winner |
|---|---:|---:|---|
| BlackPanther [B3] | −0.347 (16-33, 66 g) | −0.148 (26-35, 73 g) | score |
| Hash [B3] | +0.130 (26-20, 71 g) | −0.217 (18-28, 70 g) | bucket |
| Mothy [B3] | +0.094 (29-24, 77 g) | +0.186 (35-24, 73 g) | score |

Tally: **score 2 / bucket 1 / ties 0 / skipped 0** over 3 decks.

**This does not support flipping the flag, and the headline tally is
the least informative number in the table.** Four findings, in
descending order of how much they should change your mind:

1. **The Hash row is an accidental null replicate, and it blew up.**
   Both Hash arms selected the *same five adds* and the *same three
   cuts* — only the cut ordering differed, so the two staged decklists
   are card-for-card identical (diffed: the sole differences are
   printing annotations on two lands and the `Name=` line). Two
   identical decks scored **+0.130 and −0.217** — a **0.348 margin
   swing from simulation noise alone**. That noise floor is *larger*
   than the bucket-vs-score separation on BlackPanther (0.199) and
   Mothy (0.092), i.e. larger than every real effect the pilot
   measured. At 40 games/pod this design cannot distinguish the arms;
   "score 2 / bucket 1" is consistent with three coin flips.
2. **Win-count and mean margin disagree.** Mean margin is **−0.041
   bucket vs −0.060 score** — by that statistic bucket is very slightly
   *ahead*, the opposite of the 2-1 tally. When two summaries of n=3
   point opposite ways, neither is a signal.
3. **Both rankings mostly made decks worse.** Four of six arms are
   negative; only Mothy improved under both. The open question this
   pilot actually raises is not "which ranking is better" but "why does
   k=5 swapping at B3 lose to the original deck at all" — the same
   direction the curator-vs-original A/B found in May.
4. **The arms barely differ where it counts.** All three decks drew
   *identical adds* in both arms (lands and fixing, every time);
   only the cuts differed, and for Hash not even those. Whatever
   CardScore changes about ranking, it is currently near-invisible on
   the add side, which is the side the harness was built to test.

Also: 5 of 12 pods hit intra-pod aborts, landing 29–37 of the requested
40 games, so effective n is below the nominal 40/pod.

**Verdict — per the FP-015 contract, `COMMANDER_BUILDER_CARD_SCORE`
stays default-off.** The contract says the flag flips when the score
arm *clearly wins across a larger run*; it did not clearly win, and
this was not a larger run. Nothing here argues CardScore is *worse* —
it argues the pilot has no discriminating power.

**Before re-running, fix the design, not just the sample size:**
(a) ~~**skip decks whose arms stage identical decklists**~~ — **DONE
2026-07-26** (`fix/tier3-null-replicate-guard`). `arms_identical` now
compares card *multisets* via `_swap_signature`, not ordered lists, so
an arm pair that picks the same cards in a different order is skipped
instead of simmed. Replayed against the pilot's own
`_tier3_pilot_result.json` the guard skips Hash and leaves BlackPanther
and Mothy simming. (Multisets, not sets: a repeated card is a real
staging difference.) +3 offline tests. *Superseded 2026-07-27:*
`_staged_signature` now compares the staged deck *texts* — requested
multisets were still order-sensitive through
`_apply_swaps_to_dck`'s pair-drop validation;
(b) **run explicit null replicates** (same deck vs itself) to publish a
measured noise floor rather than discovering one by accident;
(c) raise games/pod substantially and widen the deck set — at a 0.35
noise floor, 3 decks × 40 games was never going to resolve anything;
(d) consider gating on **mean margin with a CI**, not a 2-of-3 tally.

**Design fixes SHIPPED + the gated run LAUNCHED (2026-07-26):**
(a) staged-decklist guard (PR #32 shipped a requested-swap *multiset*
compare; hardened 2026-07-27 to compare the *staged deck texts*, since
`_apply_swaps_to_dck` drops invalid (cut, add) pairs order-dependently
— identical requested multisets can stage different decks and vice
versa); (b) explicit `--null-replicates N` sims an unmodified copy vs
itself to publish a **single-margin noise reference** — a heuristic
magnitude check, NOT the sampling noise of the gated statistic (each
replicate is one base-vs-self |margin|; the gated statistic is a mean
over >= 6 decks of a *paired difference* of two independently simmed
margins, so per-deck noise is ~sqrt(2)x a single margin and the mean
shrinks by sqrt(n_decks) — gating the mean advantage on the raw
reference is conservative, and it needs >= 2 replicates or the floor
criterion is reported as not evaluated); (d) the gate is now a
**paired 95% t-interval vs the baseline arm** — default-on requires
>= 6 paired decks, CI excluding zero, AND mean advantage above that
noise reference (winner tallies gate nothing). Policy set 2026-07-26;
machinery in `build_summary`/`run_null_replicate` (PR #35, 29 harness
tests).
(c) The properly-powered run is RUNNING detached since ~13:30 local:
6 B3 decks, arms bucket vs bubble (bubble budget caps both arms),
60 games/pod, 2 null replicates (~1,680 games). Verdict lands at
repo-root `_tier3_gated_result.json`; a scheduled report fires
2026-07-27 09:30. `master` was tagged `tier3-baseline-2026-07-26`
before the run as the stable "before" reference.

### Tier-3 GATED RESULT — 2026-07-28 (gate FAIL; flag stays default-off)

The 2026-07-26 run above was **discarded**: it consumed flag-on advisor
rankings poisoned by the quantity-collapse bug (PR #37 — `mana_fit` saw
"27 Mountain" as "1 Mountain", inflating every mana producer), so its
verdict measured the bug, not the ranking. The re-run launched
2026-07-27 on post-#37/#39 master with **7** B3 decks (one spare so an
identical-arms skip can't drop paired n below the >=6 gate minimum),
same config otherwise. The box needed `commander-import --harvest 3`
first — it had no B3 filler pool, and the hardened harness (PR #39)
failed that first attempt loudly per-deck instead of losing the run.

Result (`_tier3_gated_result.json`, ~24 h wall): 6 paired decks
(Hakbal skipped — arms staged identical decklists), bubble won 4,
bucket 2, mean bubble advantage **+0.075, 95% CI [−0.159, +0.310]** —
the CI includes zero, so **gate: fail**. Null-noise reference from 2
replicates: mean |margin| 0.378 (max 0.621) — single-deck margins at
60 games/pod are still large versus the effect being hunted. Honest
read: with unpoisoned rankings and a clean harness, bubble-first
ordering shows no statistically demonstrable win-rate advantage at this
power; the 4-2 winner tally is exactly the kind of signal the gate
policy was designed not to trust. `COMMANDER_BUILDER_CARD_SCORE`
stays default-off. Next escalation, if wanted: more decks (paired n
drives the CI width down as sqrt(n)) rather than more games per deck.

### FP-015 FINAL — 2026-08-05: per-swap pooled gate FAIL; three designs agree; closed

The per-swap reopening path ran as a two-box pre-registered study:
box1 (6 [USER] B3 decks, 36 measured single swaps) and box2b (6
disjoint [USER] B3 decks) with the pooled analysis code committed
BLIND to master before unblinding (`scripts/pool_perswap_results.py`,
PR #73) and both arms held unread until both completed.

Pooled verdict (seed 20260801, reproducible from the two result
files): **Spearman rho = −0.090, one-sided permutation p = 0.7048;
top-vs-bottom contrast +0.011 [−0.135, +0.156] → gate FAIL.** The
rho is *negative* — not a power shortfall. Caveat recorded honestly,
and CORRECTED later the same day (2026-08-05): box2b's arm
contributed 0 measured swaps (30 staged swaps skipped across all 6
decks). At unblinding time this was recorded as a possible paired-cut
staging defect on imported/POP lists; the file was subsequently
identified as `--dry-run` output from box2b's machine, written to the
shared `--out` path — NOT a completed arm at all. The registered
pooled run therefore mechanically evaluated box1's arm alone (n = 36
measured swaps); the gate verdict above (FAIL; negative rho) stands
as box1's registered result. box2b's arm, if recovered or re-run,
will be analyzed independently as a replication — not folded into the
already-unblinded registered decision. The verdict over box1's data
is unaffected: a negative rho cannot be rescued by added n under a
one-sided gate. Guards against a recurrence (dry-run labeling in the
harness `--out`; pooled-side refusal of dry-run and zero-measured
inputs) landed with this correction — see CHANGELOG 2026-08-05.

**FP-015 is CLOSED.** Whole-ordering (twice) and per-swap designs all
fail their pre-registered gates. `COMMANDER_BUILDER_CARD_SCORE`
remains default-off permanently; the CardScore machinery stays
available behind the flag for display/verdict UI only, with no
win-rate claim. No further validation designs are planned.

### Tier-3 GATED RESULT — 2026-07-31 (second gate FAIL at 9 paired decks; FP-015 validation concluded)

The escalation ran: 19 B3 decks (9 [USER] incl. both FP2 decks + 10
[PREMADE]), same config (bucket vs bubble, 60 games/pod, 2 null
replicates), ~2 days wall (restarted once after an app reset; the
PR #39 crash-tolerance and staged-deck cleanup both earned their keep).

Result: **9 paired decks** — bubble 6 / bucket 3, mean bubble advantage
**+0.088, 95% CI [−0.125, +0.302]** → **gate: fail** (CI spans zero).
Null-noise reference: mean |margin| 0.345 (max 0.559). **10 decks
skipped with identical staged arms — including all 8 EDHREC-average
premades**: an average deck already contains the advisor's consensus
recommendations, so both orderings stage the same swaps; only
organically-built (Moxfield-sourced / [USER]) decks can differentiate
the arms. That structurally caps paired n well below deck count.

Honest read after two consecutive clean-gate fails (n=6 then n=9,
mildly positive means both times, CIs spanning zero both times): if a
bubble-first ordering advantage exists it is small (< ~0.1 mean margin)
and would need ~25+ differentiable paired decks to resolve — poor
return on Forge-hours. **FP-015's empirical validation is concluded:
`COMMANDER_BUILDER_CARD_SCORE` stays default-off; the CardScore
machinery remains available behind the flag for UI/verdict display
where it is useful without a win-rate claim.** Reopen only with a
fundamentally different design (e.g. per-swap A/B at scale via the
FP-012 bandit + forge_py screen, which measures individual swaps
instead of whole-ordering bundles).

### Addendum 2026-08-01 — per-swap validation harness BUILT (run pending)

The reopening path named above is now built:
`scripts/validate_card_score_perswap.py` measures **individual swaps**
instead of whole-ordering bundles. Per deck it runs the advisor's
candidate-add generation once (flag untouched — candidates are scored
through the flag-independent internals, `card_score.deck_context` +
`score_card`, never by flipping `COMMANDER_BUILDER_CARD_SCORE`), scores
EVERY candidate, records the full ranked list, selects the top-K and
bottom-K by CardScore (default 3/3), stages each as a SINGLE-swap deck
(that add + the advisor's paired cut — held FIXED per deck at the
top-ranked matchable cut, so within-deck margin differences are
attributable to the add), and A/B sims each staged deck vs the
unmodified base. This design sidesteps the identical-arms skip that ate
10 of 19 decks (incl. all 8 EDHREC-average premades) in the 2026-07-31
run: every deck with candidates and one matchable cut contributes 2K
observations. Reuses the tier-3 machinery wholesale (compare seam,
in-deck-dir staging, `Name=` restamping, try/finally cleanup,
staged-text degeneracy skips, per-deck failure containment,
`--null-replicates` noise reference).

**GATE POLICY — pre-registered 2026-08-01, before any run:** CardScore
is predictive iff (1) pooled Spearman rho between CardScore and
measured per-swap margin is > 0 with one-sided permutation p < .05
(pure-stdlib, tied ranks mid-ranked, 10k seeded shuffles), AND (2) the
top-K group's mean margin exceeds the bottom-K group's (Welch t-based
95% interval printed as context; the criterion is only the direction).
Anything else: not predictive, FP-015 stays concluded. The noise
reference is published context, not a criterion; the gate is one
pre-registered conjunction and everything else in the summary is
labeled exploratory / not multiplicity-corrected. 31 offline tests
(`tests/test_validate_card_score_perswap.py`, injected
advise/score/compare fns). **Run pending** — no Forge games have been
played through this harness yet.

## The gap

There is **no per-card score anywhere in the codebase.** Card ordering is
done in four unrelated places, none of which produce a comparable scalar,
and two of which are not scores at all:

- `_advisor_heuristic._heuristic_swap_recommendations` orders adds by
  **bucket insertion order** (`high_synergy` → `top_cards` →
  `average_deck` → `tag_*` → `new_cards`), then re-sorts by
  `_rank(r) = (role_rank, trending_rank)`. `inclusion_pct` and
  `synergy_pct` are used **only as boolean gates** (`>= MIN_SYNERGY_PCT`
  25.0, `>= MIN_INCLUSION_PCT_FOR_ADD` 30.0) and to build rationale
  strings. Neither ever enters a sort key.
- Cuts are ordered by `sorted(deck_cards)` — **alphabetically**. The
  comment at `_advisor_heuristic.py:490` concedes it: *"no per-card score
  exists at this point."*
- `lift_analysis.lift_candidates` is the only genuine per-card number:
  `score = mean(top-5 lifts)` where `lift = (co * n) / (ca * cb)`.
  Co-occurrence only — no mana value, no role, no color, no price, no
  curve, no combo membership.
- `deck_builder_personalize.synergy_scorer` computes a *different* shape
  over the same matrix (`Σ (lift - 1)` over all deck cards — unbounded,
  rewards breadth) and is used side-by-side with `lift_candidates`'
  mean-of-top-5 in `lift_swaps`. **The two are not on the same scale.**

Two "match %" scales also already disagree: `deck_dashboard.match_score`
adds a `max(0, 5 - rank_in_list)` bonus that
`web/_helpers._match_pct_from_evidence` deliberately omits.

Meanwhile these signals exist, are computed, and are **thrown away at
ranking time**: `CardEntry.num_decks` (parsed, read nowhere), combo
membership (a candidate that completes a 2-card combo already half-present
in the deck gets **zero** boost), mana value, role deficit *magnitude*
(only the ordered role list survives), salt, Game-Changer status (boolean
gate only), ownership (tiebreak only), price, and Scryfall's `edhrec_rank`
and `produced_mana` (present in every cached snapshot, **read by zero
code**).

## Honest framing — read this before building

FP-014 states the project's position plainly: *"assembled decks get
Forge-VALIDATED, not just heuristically scored… Every other from-scratch
builder stops at a static power heuristic."* And FP-002's 2026-07-23
result is that at n=45, **no pre-sim deck feature predicts curation margin
at |t| >= 2**.

Both are reasons to scope this correctly rather than reasons to skip it.
`CardScore` is **not** a truth claim about card quality and must not be
sold as one. It is a **ranking prior that shrinks the search space Forge
has to validate.** The curator/sim stays the arbiter; the formula decides
what gets simmed first.

That framing survives FP-002's null result — a scorer can be a useful
*ordering* even when no single feature *regresses* on margin. And it
composes with FP-012: the score becomes the UCB1/Thompson **prior**, so
the bandit's arm search starts warm instead of uniform, which is where the
sim-cost savings actually come from.

## Shape

Multiplicative gates × weighted additive base × bounded modifiers. Same
structural pattern as `bracket_estimator.DEFAULT_WEIGHTS` — **one
documented dict as the tuning surface**, every term explainable in a UI
tooltip, no term that can't be written as one sentence to the user.

```
CardScore(card | deck, commander, bracket, context)
  = Gate(card) * [ 100 * Σ w_k * f_k(card | deck) + Σ m_j(card | deck) ]
  clamped to [0, 100]
```

### Gates (multiplicative, 0 or 1; a failure is reported with its reason)

| Gate | Zero when |
|---|---|
| `legal` | `legalities.commander != "legal"` — **from Scryfall, not a hardcoded set** (see the FP-015 prerequisite below) |
| `color_identity` | `card.color_identity` not a subset of the commander's |
| `singleton` | already in the deck and not a basic land |
| `bracket_cap` | adding it would exceed the bracket's Game Changer cap (0 at B1/B2, 3 at B3) |

### Base components (all `f_k` in `[0, 1]`; weights sum to 1.0)

```python
CARD_SCORE_WEIGHTS = {
    "consensus":   0.18,   # does the format agree this belongs here
    "synergy":     0.24,   # does it fit THIS deck
    "role_fit":    0.28,   # does the deck need this job done
    "curve_fit":   0.16,   # does it fit the curve and land count
    "mana_fit":    0.14,   # does it help meet Karsten color-source targets
}
```

**`consensus`** — `clamp(inclusion_pct / 60.0, 0, 1)`. 60% saturates;
above that we're measuring "is a staple," which `role_fit` handles better.
Offline fallback uses Scryfall's `edhrec_rank` (already cached, unread):
`clamp(1 - log10(rank) / 4.5, 0, 1)`.

**`synergy`** — blends the two independent synergy signals:
```
0.55 * clamp(synergy_pct / 40.0, 0, 1)
+ 0.45 * clamp((lift_score - 1.0) / 2.0, 0, 1)
```
When the corpus is under `MIN_CORPUS_DECKS` (10), **renormalize to the
EDHREC term alone** rather than feeding a zero — the same
"unavailable != bad" contract `deck_health` signals already honor by
returning `None`.

**`role_fit`** — the deficit-driven term, and the thing that makes this
deck-relative rather than a global power ranking:
```python
if count < target:
    f = 0.5 + 0.5 * (target - count) / target
elif count < ROLE_SATURATION_THRESHOLDS[role]:
    f = 0.5 * (1 - (count - target) / (sat - target))
else:
    f = 0.0
```
Two upstream changes this needs: add `finisher: 3` to `ROLE_TARGETS`
(today there is **no win-condition target at all** — a deck of 99 ramp and
draw satisfies every role), and make `role_target_report` count the
commander, letting a commander that fills a role reduce that role's target
(Edric should not demand 10 draw spells).

**`curve_fit`** — needs an MV histogram, which nothing currently computes:
```python
deficit = max(0.0, target_curve[mv] - actual_curve[mv])
f = clamp(deficit / max(1.0, target_curve[mv]), 0, 1)
```
`target_curve` shifts down for aggro/combo, up for control. **This is
where `archetype.classify` finally earns its keep** — today it feeds only
pod diversity and a bracket nudge, and touches card selection nowhere.

**`mana_fit`** — highest-value component, and the one no competitor has,
because the math is already written:
```python
targets  = color_source_targets(identity, pip_stats(deck_names, lookup))
sources  = current_source_counts(deck_names, lookup)   # land_color_sources()
produced = card.get("produced_mana") or []             # <- currently unread
f = mean(clamp((targets[c] - sources[c]) / max(1, targets[c]), 0, 1)
         for c in produced if c in identity) if produced else 0.0
```
`KARSTEN_99_SOURCES` + `color_source_targets` currently run **only at
build time** and are never used to evaluate an existing deck. This turns
the best math in the repo from write-only into an evaluative signal.

### Modifiers (additive on the 0–100 scale, each bounded, each with an explanation string)

| Modifier | Range | Trigger |
|---|---|---|
| `combo_completion` | **+15** | completes a known combo where every other piece is already in the deck |
| `combo_partial` | **+6** | 3-card combo with 2 pieces present |
| `redundancy_relief` | **+5** | deck has < 3 instances of an effect this card duplicates |
| `owned` | **+6** | `collection.owns()` and collection bias is active |
| `price_penalty` | −0 … −12 | `-12 * clamp((usd - soft_cap) / soft_cap, 0, 1)` |
| `salt_penalty` | −0 … −10 | bracket <= 3 only: `-10 * clamp((salt - 1.5) / 2.5, 0, 1)`, matching `_SALT_WARN_THRESHOLD` |
| `bracket_pressure` | −0 … −20 | tutors already at the bracket's budget ("tutors should be sparse" at B1/B2); fast mana over budget; an extra-turn card when one is already present (B1/B2 forbid outright, B3 forbids chaining); any MLD card at B <= 3 |
| `mdfc_bonus` | **+3** | modal land — already tracked in `_MDFC_LANDS`, already worth 0.5 land in the health grade |

`combo_completion` is the modifier that visibly changes recommendations on
day one, and it costs almost nothing: `combo_detection` already loads the
combo list, it just never gets consulted during ranking.

## Cut scoring

Cuts are alphabetical today. Reuse the same function against the deck
*minus that card*, so a saturated role correctly surfaces its weakest
member:

```python
cut_score(card) = 100 - CardScore(card | deck_without_card, ...)
```

Guard rails, **all of which already exist** and must be preserved: never
cut a `Protect=` line; never cut below `ROLE_TARGETS` on any role; never
cut a land if effective lands would drop below the 33 floor; never cut a
piece of a detected in-deck combo; respect the like-for-like role
constraint enforced at `deck_builder_personalize.lift_swaps:217-224`.

## Plug-in seams (highest leverage first, signatures current)

1. **`_advisor_heuristic.py:403`** — replace `_rank(r) -> tuple[int, int]`
   with `score_card(...) -> float`. `inclusion_pct`, `synergy_pct`,
   `candidate_bucket`, `role`, and `is_trending` are **all already in
   scope** at that point and discarded. The values already flow into
   `SwapRecommendation.evidence` (lines 383–389), so the UI can render the
   component breakdown with **no schema change**.
2. **`_advisor_heuristic.py:490`** — the alphabetical cut loop.
3. **`deck_builder_personalize.synergy_scorer` (line 91)** — already
   consumed as `quality_of: Callable[[str], float]` by
   `apply_collection_bias` and `lift_swaps`. A composite scorer drops into
   that slot with **zero signature churn**, upgrading personalization
   stages 1 and 3 at once.
4. **`deck_builder._fallback_candidates` (line 304)** — raw bucket
   concatenation, truncated by `nonlands[:nonland_target]`. Ordering here
   literally decides which cards make the 99 on the no-average-deck path,
   which is exactly FP-014's acknowledged weak path ("a *defensible pile*,
   not a coherent deck"). **Biggest single quality win for
   `commander-build`.**
5. **`lift_analysis.lift_candidates` (line 458)** — its docstring calls it
   *"the one true definition — every surface routes through here."*
   Widening its row with `components: dict` propagates the breakdown to
   dashboard, advisor, CLI report, and builder for free.

**Do NOT put this in `staples.classify_role`** — that function's internal
`score` field is a classifier argmax with entirely different semantics.
Overloading it would silently change role labels project-wide.

## Prerequisites (small, independently shippable, each valuable alone)

**ALL FIVE SHIPPED — verified 2026-07-25 against `master` @ `c53a31b`.**
Kept below for the rationale (each bullet explains *why* the thing was
needed); none of them is open work any more:

1. Scryfall-backed legality → `deck_legality.py` (the hand-typed
   `_CORE_BANS` set is gone from `web/routes_decks.py`; the module
   docstring records the same both-directions-wrong finding).
2. `finisher` in `ROLE_TARGETS` → `staples.py` (`"finisher": 3`, with
   the `win_condition` coherence note beside it); the MV curve lives on
   `DeckContext.curve` and feeds `_f_curve_fit`.
3. `manabase_report()` → `deck_builder_manabase.py:769`.
4. `combo_detection.one_piece_away()` → `combo_detection.py:241`,
   surfaced on `DeckContext.one_piece_away`.
5. `avg_cmc` + `archetype` at every `estimate_bracket` caller →
   `deck_dashboard.py`, `improvement_advisor.py` and `deck_builder.py`
   all pass both now (the latter two via `derive_signals`).

- **Scryfall-backed legality.** `web/routes_decks.py:885` `_CORE_BANS` is
  a hand-typed set inside a route function and is wrong in both
  directions as of the 2026-02-09 B&R: it flags **Coalition Victory** and
  **Panoptic Mirror** as banned (both are on the *Game Changers* list —
  we tell users a WotC-blessed B3 card is illegal), plus Painter's
  Servant, Worldfire, Sway of the Stars, and Tempest Efreet; and it
  **misses** Balance, Fastbond, Flash, Golos, Griselbrand, Karakas,
  Leovold, Paradox Engine, Rofellos, and Tolarian Academy. It also has no
  representation for Lutri, the Spellchaser's new **"banned as a
  companion"** designation, and lists `Time Vault` twice.
  `legalities.commander` is already in every snapshot
  (`scryfall_client.py:246` projects it) and auto-updates with the format.
  Delete the set; keep a tiny overlay only for the Lutri carve-out.
- **MV histogram + `finisher` in `ROLE_TARGETS`** — prerequisites for
  `curve_fit` and `role_fit` respectively.
- **`manabase_report()`** — run the existing
  `pip_stats` → `color_source_targets` → `land_color_sources` pipeline
  over an *existing* deck. Prerequisite for `mana_fit`, and a health-grade
  component in its own right.
- **`combo_detection.one_piece_away()`** — prerequisite for
  `combo_completion`, and the most actionable single suggestion type the
  advisor could emit.
- **Pass `avg_cmc` + `archetype` to every `estimate_bracket` caller.**
  Only `deck_dashboard.py:565` does today; `improvement_advisor.py:1283`
  and `deck_builder.py:994` (the **bracket-steering loop**) pass neither,
  so `curve_tight` / `curve_high` / `archetype_combo` / `archetype_stax` —
  1.5 points on a 1–5 scale — can never fire during `commander-build`
  steering or `commander-advise`. Not strictly a `CardScore` prerequisite,
  but it's the same missing plumbing (`avg_cmc` is one line;
  `deck_dashboard.py:384` already computes it).

## How this gets validated (the part that makes it worth merging)

**Not** R² against margin. FP-002 already establishes that no pre-sim
feature clears |t| >= 2 at n=45; a card scorer would fail that bar too and
that failure would be uninformative. Four tiers instead, cheapest first:

1. **Ordinal sanity suite** — assert known orderings on fixed decks.
   Sol Ring > Worn Powerstone in every deck; Rhystic Study > Divination at
   B4 with the gap narrowing at B2. Pure-stdlib, offline, milliseconds.
   Catches sign errors and weight typos, which is most of what goes wrong.
2. **Rank correlation against our own history** — for every
   `knowledge_log` iteration with `verdict='kept'`, the manifest's added
   cards should score above its cut cards. Spearman ρ over the manifest.
   **Mind the data caveats:** the win-rate denominator changed
   2026-07-19/20 (bucket by write date) and `id < 314` rows are A/B
   name-attribution artifacts.
3. **The real test — top-k-by-score vs. k-by-current-bucket-order**, both
   A/B simmed through `compare_versions` at equal game counts. This is a
   direct, conclusive answer to *"does the formula help?"* using the
   harness that already exists, and unlike a regression it does not need a
   per-feature t-stat to be readable.
4. **Feed FP-002.** The per-color source deficits from `manabase_report`
   (and consistency metrics, if the opening-hand work lands) are exactly
   the kind of **pre-sim, continuous** features the 31-feature set lacks —
   its current features are overwhelmingly post-hoc sim outputs plus
   coarse `deck_health` counts, which is part of why nothing regresses.

Ship behind a flag; default off until tier 3 reads positive.

## Substrate that already exists (why this is cheap to start)

`staples.classify_role_extended` / `ROLE_TARGETS` /
`ROLE_SATURATION_THRESHOLDS` / `role_target_report`, `lift_analysis`,
`edhrec_client.CardEntry`, `combo_detection`, `game_changers`,
`collection.owns`, `deck_pricing`, `bracket_estimator.DEFAULT_WEIGHTS`
(the pattern to copy), `deck_builder_manabase.KARSTEN_99_SOURCES` /
`color_source_targets` / `pip_stats` / `land_color_sources`, and the
Scryfall snapshot cache (including `edhrec_rank` and `produced_mana`,
both cached and both currently unread) are all shipped and tested.

## Honest limitations to write into the module docstring

- **It is a prior, not a verdict.** Every number it emits is a heuristic
  ordering. Forge remains the arbiter; nothing about this plan changes
  that, and the UI must not present a `CardScore` as a power rating.
- **Weights are hand-set until tier 3 reads positive.** They encode our
  priors about Commander, not measured effect sizes.
- **`role_fit` inherits `classify_role`'s regex accuracy.** Misclassified
  cards get mis-scored, and the failure is silent. The Forge card scripts
  (`forge_script_parser.CardScript.abilities[].effect` — the actual Forge
  effect primitive, fully offline) are the better long-term classifier and
  are currently used only for aggregate counters in
  `deck_library_analyzer`.
- **`synergy` degrades to EDHREC-only under 10 harvested decks**, so early
  users get a meaningfully different (and more generic) ranking than
  users with a corpus. Surface which mode is active rather than hiding it.
- **`curve_fit` assumes cast turn = MV**, the same simplification
  `deck_builder_manabase` already makes and documents.

---

# FP-002 (reframed) — curator margin regression

**Status: CLOSED — REFUTED (2026-07-30). Full chain: REOPENED →
first result (2026-05-26) → n=45 re-run (2026-07-23) → 80-pair gate
CLEARED (2026-07-29) → n=93 re-check refuted the sole surviving
feature (2026-07-30, "Result 2026-07-30" below) → substrate probe at
n=102 found no signal in theme clusters or CardScore components
either (PR #58). Reopening requires a genuinely new regressor family,
not more games.**

## Result 2026-07-29 — gate cleared; one stable feature survives

The 80-pair gate cleared via the [PREMADE] pair program (popularity pulls
from Moxfield/EDHREC + free heuristic v2 minting, PRs #46/#47/#48) with
both boxes soaking: 83 pairs at ≥40 decisive games per side, 39k gauntlet
games total; `margin_analysis --mode gauntlet --min-games 40` admitted
**n=66 decks / 28,960 games** (17 since-deleted deck files and 26
still-incomplete pairs excluded — n keeps growing as the soaks run).

- **Mean curator margin −0.005** (kept=4 / reverted=8 / neutral=54):
  heuristic curation is net-neutral against the fixed gauntlet. The
  honest headline is unchanged from every prior cut — curation rarely
  hurts, rarely demonstrably helps.
- **One feature survives |t|≥2: `wincon_protection` r=−0.27 (t=−2.23)**,
  stable from the n=58 preview to n=66. Decks that already protect their
  wincons gain less from curation. `mdfc` (t=−1.84 here, significant at
  n=58) did not hold; `bracket` sits at t=−1.98, borderline.
- Caveats stated plainly: one survivor of ten features tested at p<.05
  (expected false positives ≈ 0.5), r²≈7% of variance, and the n=58→66
  "replication" shares most of its data. Suggestive, not proven.
- **Actionable use**: as a soft advisor prior — spend curation/audit
  effort preferentially on decks with weak wincon protection — NOT as a
  shipped predictor. Re-check at n≥90 (both soaks are still filling
  pairs) before wiring anything in.

## Result 2026-07-30 — n≥90 re-check REFUTES the survivor; FP-002 closed

The re-check fired a day later after 17 missing base decks (POP decks
staged in the inbox's popular_decks/, plus 5 in box2_decks/) were
restored to the deck dir, unlocking their 129 excluded rows:
**n=93 decks / 37,120 games**. `wincon_protection` collapsed to
r=−0.055 (t=−0.53); NOTHING clears |t|≥2 — the n=66 hit was the false
positive the caveats predicted. Mean curator margin **−0.010**
(kept=4 / reverted=16 / neutral=73): heuristic curation is net-neutral
on average and, when it moves the needle at all, hurts (reverted) 4×
more often than it helps (kept) — the empirical verdict gate is doing
exactly its job. **Conclusion: no pre-sim deck-health feature predicts
curation margin at practical n. No advisor prior ships. FP-002 is
CLOSED (refuted, not parked)** — reopening requires a new feature
substrate (e.g. CardScore components or corpus-theme cluster labels as
regressors), not more games on the same features.
The original FP-002 was a
*kept-vs-reverted classifier*. It was concluded NOT VIABLE on 2026-05-22 for a
specific reason: after the A/B seat-attribution fix (`e8777b6`), the curator's
swaps almost never made a deck strictly *worse*, so there was **no negative
class** to learn. STATUS.md proposed the unblock itself: *"regress on improvement
margin, not more sim hours."*

The accumulated **40-game** A/B soak rows reopen it under exactly that framing.
Two things changed:

1. **The negative-class blocker is gone.** Across high-confidence (≥40-game)
   pairs we now see both winners and losers among the curated decks
   (**kept=6, reverted=4, neutral=19** of 29 decks). Curation *can* hurt — it
   just usually doesn't.
2. We can regress a **signed, continuous target** (win-rate margin) on
   **pre-sim features of the original deck** — the honest predictive substrate
   (no sim outcome leaks in; we ask *"from the deck alone, can we tell whether
   curation will help it?"*).

## Tooling

- `scripts/margin_analysis.py` — pure-stdlib (numpy/sklearn/scipy are **not**
  installed on the soak boxes). Two designs:
  - `--mode ab` (default): aggregates `*throughput*.jsonl`, margin =
    `(wins_b - wins_a) / decisive` (v1-vs-v2 *in the same pod*).
  - `--mode gauntlet`: aggregates `*gauntlet*.jsonl`, margin =
    `winrate(v2) - winrate(base)` where base and v2 *each* play the **same
    fixed 3-deck gauntlet** — no head-to-head pod confound (the cleaner test).
  - Both join each deck to its original `.dck` for `deck_health` features and
    report per-feature Pearson `r` + a two-sided t-stat (df = n−2).
  - `python scripts/margin_analysis.py [--mode gauntlet] --min-games 40`
    (text) or `--json`; `--decks DIR` (repeatable) overrides the search path.
- `tests/test_margin_analysis.py` — 22 pure-logic tests (A/B + gauntlet
  aggregation, margin banding, Pearson edge cases, the deck-file join,
  end-to-end `analyze`, loud missing-deck-file accounting).
- Rows whose original deck file has left the disk (renamed/pruned from the
  library) are **excluded loudly**: a per-file stderr warning with the dropped
  row count, plus `missing_deck_files` / `missing_deck_rows_dropped` totals in
  the report (2026-07-23; the exclusion itself is unchanged — we cannot
  feature a deck we cannot read).

## Result (min_games=40, n=29 decks, 11,960 games)

```
mean curator margin: +0.0009   (per-deck win-rate delta; >0 = curation helps)
per-deck verdicts:   kept=6  reverted=4  neutral=19

feature -> margin correlation (|r| desc):
  wincon_protection    r=+0.447  t= 2.60  *   <- only feature past |t|>=2 (~p<.05)
  mana_sinks           r=-0.328  t=-1.80
  deficit_total        r=-0.303  t=-1.65
  spell_density        r=+0.292  t= 1.59
  under_built_roles    r=-0.282  t=-1.53
  basic_lands          r=-0.259  t=-1.40
  bracket              r=+0.256  t= 1.37
  main_count           r=+0.093  t= 0.49
  self_mill            r=+0.066  t= 0.34
  mdfc                 r=-0.063  t=-0.33
```

## Reading

- **Curation is empirically ~neutral.** Mean margin is +0.0009 and 19 of 29
  decks land in the ±0.05 neutral band. On the population of decks we've curated,
  the v2 is a coin-flip against the original. (This corroborates the earlier
  ad-hoc finding: all-rows mean ≈ −0.009.) A blanket "always curate" policy is
  **not** supported by the sim data.
- **One robust signal:** decks that *already* carry more **wincon-protection**
  benefit more from curation (`r=+0.45`, the only feature past the significance
  flag). Intuition: when the deck can already protect its win, the curator's
  consistency/interaction tweaks convert into wins rather than being wasted on a
  deck that loses the wincon anyway.
- **Weaker, sub-threshold hints** (don't over-read at n=29): curation helps
  decks with *fewer* mana sinks and *smaller* role deficits (negative `r` on
  `mana_sinks`, `deficit_total`, `under_built_roles`) — i.e. it adds the most to
  decks that are already coherent, and adds little to decks with large structural
  holes. This is the opposite of the "curation rescues weak decks" hypothesis.

## Cross-validation: the gauntlet design (min_games=40, n=26 decks, 5,760 games)

The A/B design has a confound: base and v2 play *in the same pod*, so they take
wins directly off each other and share two filler opponents. The **gauntlet**
soak removes it — base and v2 *each* play the same fixed 3-deck gauntlet
independently, so their win-rates are measured against identical opposition.
Running the same regression there:

```
mean curator margin: -0.0108   (winrate(v2) - winrate(base) vs fixed gauntlet)
per-deck verdicts:   kept=5  reverted=4  neutral=17

feature -> margin correlation (|r| desc):
  deficit_total        r=-0.359  t=-1.88     <- closest to significance
  mana_sinks           r=+0.332  t=+1.72     (sign FLIPS vs A/B -> noise)
  self_mill            r=-0.280  t=-1.43
  under_built_roles    r=-0.261  t=-1.32
  ...
  wincon_protection    r=+0.223  t=+1.12     (A/B's "significant" feature: NOT replicated)
```

**What the cleaner design tells us:**

1. **"Curation is ~neutral" is robust.** Two independent experimental designs
   agree: mean margin +0.0009 (A/B) and −0.0108 (gauntlet), both ≈ 0, with the
   large majority of decks (19/29 and 17/26) in the neutral band. This is the
   finding to trust.
2. **The A/B `wincon_protection` result was a confound artifact.** It dropped
   from r=+0.45 (t=2.6, "significant") to r=+0.22 (t=1.1, not significant) once
   the head-to-head pod confound is removed. **Do not build on it.** This is
   exactly why the cleaner design was worth running.
3. **The one directionally-consistent signal** across both designs is
   `deficit_total` / `under_built_roles` — *negative* in A/B (−0.30 / −0.28)
   and in gauntlet (−0.36 / −0.26). Curation adds the **least** to decks with
   large structural role deficits. Neither crosses significance alone, but the
   agreement across designs makes it the most credible (weak) lever.

## Verdict & next step

The reframing **works as an analysis** and the negative-class obstacle is
resolved. But **no feature survives cross-validation at significance** — the
one A/B winner (`wincon_protection`) failed to replicate in the unconfounded
gauntlet design — and **n is too thin** (29 / 26 decks). This is exploratory
evidence, not a shippable model.

To graduate from "analysis" to "predictor":

- **More unique decks**, not more games per deck — the unit of analysis is the
  deck. ~30 → ~80+ decks would let the directionally-consistent `deficit_total`
  signal be validated out-of-sample without sklearn.
- Run *both* designs and only trust features that agree across them — the
  gauntlet/A/B disagreement on `wincon_protection` shows single-design "hits"
  are unreliable here.
- Then optionally a tiny pure-stdlib regression on the features that survive
  cross-validation. sklearn is still unnecessary at this scale.

The actionable takeaway *today* (no model needed): **the curator's expected
improvement is ~0** (confirmed by two independent designs). The only credible
(if weak, cross-validated) lever is **structural deficit**: curation adds the
least to decks with big role-target shortfalls. That argues for using the
deck-health `under_built` signal (F2) to **fix structure first, then curate** —
and for not assuming curation is a free win on an already-coherent deck.

## Result 2026-07-23 — gauntlet at n=45 (the ≥+10-deck unblock fired)

The documented unblock condition — *re-run once the deck-gen campaign adds
≥10 new decks* — fired: the gauntlet dataset grew **26 → 45 unique decks**
(**10,360 games** at min_games=40; AB mode is unchanged at n=29 / 11,960
games). A fresh gauntlet soak (`--games 40 --append`, started 2026-07-23) is
still appending toward the ~80-deck predictor gate.

```
mode=gauntlet, min_games=40, n=45 decks, 10,360 games

mean curator margin: -0.0133   (winrate(v2) - winrate(base) vs fixed gauntlet)
per-deck verdicts:   kept=7  reverted=12  neutral=26

feature -> margin correlation (|r| desc):
  bracket              r=-0.219  t=-1.47      <- new top feature; still sub-threshold
  main_count           r=+0.162  t=+1.08
  spell_density        r=+0.144  t=+0.95
  deficit_total        r=-0.109  t=-0.72      (was -0.359/-1.88 at n=26)
  mdfc                 r=+0.105  t=+0.69
  wincon_protection    r=+0.092  t=+0.60      (A/B's one-time star: still dead)
  self_mill            r=-0.083  t=-0.54
  basic_lands          r=-0.064  t=-0.42
  mana_sinks           r=-0.040  t=-0.26      (was +0.332/+1.72 at n=26 -- sign flip again)
  under_built_roles    r=-0.020  t=-0.13      (was -0.261/-1.32 at n=26)
```

### Honest reading at n=45

1. **"Curation is ~neutral" holds — and leans slightly negative.** Mean margin
   −0.0133 with 26/45 decks in the ±0.05 neutral band; among decided decks the
   split is now 7 kept vs 12 reverted. Still ≈0 in absolute terms, but there
   is *no* evidence curation helps on average in the unconfounded design.
2. **NO feature passes |t|≥2 at n=45 — and, worse, every n=26-era candidate
   collapsed as n grew** instead of gaining power: `deficit_total`
   −0.36 → −0.11, `under_built_roles` −0.26 → −0.02, `wincon_protection`
   +0.22 → +0.09, `mana_sinks` +0.33 → −0.04 (another sign flip). Real effects
   sharpen with more data; these melted. That is the signature of noise.
   **The 2026-05-26 "most credible weak lever" (structural deficit) is dead
   at n=45** — the fix-structure-first advice may still be good deck-building
   hygiene, but the margin data no longer supports it as a *predictor*.
3. **The A/B cross-check makes the same point louder.** Re-running AB mode
   (rows unchanged: n=29, mean +0.0009, kept=6/reverted=4/neutral=19) with
   today's feature extractor now flags **three** features at |t|≥2 —
   `wincon_protection` (+0.447/2.60), `spell_density` (+0.416/2.38),
   `deficit_total` (−0.387/−2.18). (The r's moved vs the 2026-05-26 table
   because `deck_health` evolved across the merged PRs; the soak rows did
   not.) **None of the three replicates in the gauntlet design at n=45.**
   Single-design AB "significance" keeps manufacturing hits that the clean
   design keeps killing — do not build on AB-only results.
4. **What the data supports at this n:** the curator's expected improvement is
   ≈0 (both designs, now at larger n), and *no pre-sim deck feature predicts
   the margin*. **What it does not support:** any feature-gated "when to
   curate" policy. The ~80-deck gate stays the next checkpoint, but the prior
   should now be that the predictor comes up empty — the value of finishing
   the campaign is closing the question cleanly, not rescuing it.

---

# FP-002 deck-generation plan — toward a real margin predictor

**Status: COMPLETE and superseded (2026-08-03 note). The pool grew to
79 premades / 50 minted pairs (PRs #46-#48) and the 80-pair gate
cleared — then FP-002 itself closed refuted (see above), so no further
pool growth is planned for this purpose. The premade pool remains in
service for gauntlet soaks and validation runs.**

**Goal (your call, 2026-05-26):** grow the soak deck set from ~13 unique
commanders to **~80+ unique decks**, so the margin regression in
`scripts/margin_analysis.py` has enough rows (the unit of analysis is the
*deck*, not the game) to attempt an out-of-sample predictor on the one
cross-validated signal (`deficit_total` / `under_built_roles`).

## Why this is a campaign, not a step

- 80 decks × **40-game** gauntlet (operator directive: never 5-game) × 2 roles
  (base + v2) ≈ **6,400 games**. At ~40s/game that's ~70h single-runner, or
  **~12–18h** on the autoscaling `soak_pool` (the box1 Ryzen 3900X did ~200
  games/hr in prior soaks). It is a soak you launch deliberately, like the
  prior gauntlet runs — not a single command that returns.
- The acquisition + curation phase (below) is network/Claude-CLI bound and
  unattended-able, but still ~1–2h for 30+ commanders.

## Pipeline

**Phase 1 — acquire base decks (network-bound, safe to run anytime)**
1. Pick ~30 more commanders spanning brackets B3/B4/B5 and color identities
   (diversity matters more than raw count — avoid 30 mono-red goblin decks).
   Source options, in preference order:
   - EDHREC average deck per commander (`edhrec_client.fetch_average_deck`) —
     coherent, no Moxfield dependency.
   - Top-liked Moxfield build (`moxfield_import.find_top_liked_deck_for_commander`)
     — what `scripts/pull_popular_decks.py` already does for existing decks.
2. Write each as a `[USER] <Name> [B<n>].dck` into the shared inbox so
   `soak_pool` discovers it. Keep names unique.

**Phase 2 — curate a v2 per deck (Claude-CLI bound, unattended)**
- `commander-auto-curate <base>.dck` (subscription CLI path; scrubs API keys)
  writes the curated `... v2 ...dck`. `soak_pool` pairs base + ` v2 ` by name.
- Resumable: skip any base that already has a v2.

**Phase 3 — soak (the long Forge run; launch deliberately)**
- Gauntlet mode, 40 games, the unconfounded design margin_analysis prefers:
  ```
  python scripts/soak_pool.py --mode gauntlet --games 40 --append \
      --label Llama --out C:/Users/pilot/soak_inbox/Llama_gauntlet.jsonl
  ```
- **Machine-identity invariant:** box1 is `Llama` — use `--label Llama` and the
  `Llama_*` output; never a box2b label on box1.
- Run in short blocks during dev; bump to 24h when stable (per memory).

**Phase 4 — analyze**
- `python scripts/margin_analysis.py --mode gauntlet --min-games 40`
  now reports over ~80 decks. With n≈80, validate the `deficit_total`
  single-feature OLS out-of-sample (pure stdlib; sklearn still unneeded).

## What to build (small, optional)

A driver `scripts/build_fp002_deckset.py` that automates Phase 1+2 from a
commander list: fetch base deck → write `.dck` → `commander-auto-curate` →
emit progress, resumable on the count of paired decks. ~1 file; the per-step
calls already exist (`fetch_average_deck`, `find_top_liked_deck_for_commander`,
`auto_curate_main`). Left unbuilt pending go-ahead because the commander LIST
is a curation choice (which 30 commanders) better made with you.

## Status / honesty

- Acquisition + curation: ready to run with existing tooling.
- The soak is a multi-hour Forge campaign — best launched when box1 is free
  (you stop soaks when actively editing the program), not unattended mid-dev.
- Caveat from the completed analysis: curation is ~neutral and only one feature
  cross-validated, so even at n=80 the predictor may stay weak. The value is a
  definitive answer, not a guaranteed model.

---

# FP-012 — budget-bounded UCB1 swap search in the improve loop (full slice)

**Status: SHIPPED 2026-07-24 (code + tests); forge_py screening gate
added (PR #50); empirical shakedown COMPLETE 2026-08-02/03 — the
screen engaged live (4 candidates → kept 2 / pruned 2, scores logged),
Forge judged the survivors, and the verdict machinery correctly
reverted a swap that lost 47%→40% (knowledge-log iteration #33).
End-to-end validated; screening halves Forge spend per search round.**
Original shipping note: the full-slice search deliberately shipped without
a live Forge shakedown — the gauntlet soak owned the CPU — so the design
below was unit-verified against injected sims only. Post-soak, run a
real `--search-budget` improve round and compare against a plain greedy
round before trusting it with overnight budgets.

## What shipped

`commander-improve --search-budget N` (plus `--search-min-pulls`,
default 2) puts a UCB1 bandit INSIDE each greedy improve round:
`improve_search.py` builds swap arms, searches under a sim budget, and
hands the winners to the unchanged keep-if-better round machinery.
`--search-budget 0` (default) is byte-identical greedy — pinned by a
test that spies the search module is never constructed.

- **Arm** = one concrete (cut X, add Y) swap from the advisor's
  already-filtered candidate pool (`improvement_advisor.advise`,
  offline sources only: `heuristic` = EDHREC inclusion/synergy,
  `bracket_peers` = local tuned builds; `--source claude` is coerced to
  `heuristic` with a stderr note — the curator stays OUT of the search
  inner loop, rewards come from sims, not model judgment). Adds pair
  with cuts i-mod-n, the slice-2 convention; protected cards
  (`--protect`, `--protect-from`, deck `Protect=`, intent wincons)
  never become cut arms. Pool capped at `budget // min_pulls` — a
  wider pool only dilutes UCB1's mandatory cold-start.
- **Pull** = apply that ONE swap to the round's base deck (through the
  shared `apply_proposal_to_deck` legality path) and run ONE A/B sim of
  `--sim-games`. **Reward** = the decisive margin mapped to UCB1's
  required [0,1]: `(m+1)/2` with `m = (wins_new − wins_old)/decisive`,
  which collapses to `wins_new/decisive`. Zero decisive games → no
  reward update, arm marked dead (no signal ≠ break-even).
- **Budget** = `--search-budget` total pulls per round; UCB1
  exploration constant reuses `--ucb-c` (default 1.4 ≈ √2). After
  exhaustion, arms with ≥ `--search-min-pulls` pulls AND mean strictly
  above break-even (0.5) win, best-mean first, capped at 3 applied
  swaps per round (interaction effects are unmeasured by independent
  arms — that's the still-parked GP slice B2).
- **Round contract unchanged:** winners form a normal `Proposal`
  (source `bandit-search`), applied via the shared legality guards,
  logged to knowledge_log, verdict-simmed once more as a combined deck,
  and `run_improve_loop` advances only on `kept`, exactly as before.
- **Honest cost:** a round costs ≈ `(N+1) × sim-games` Forge pod games
  (documented in `--help`); `--search-budget < --search-min-pulls` is
  refused up front because no arm could ever qualify.

Tests: `tests/test_improve_search.py` — 27 deterministic tests
(hand-computed UCB1 pull-sequence pin, reward-mapping algebra,
zero-decisive marking, budget/min-pulls interplay, winner cap, offline
candidate sourcing incl. the claude-coercion, full round integration
through `run_improve_loop` with injected arm builder + sim, legality
guard kill-path, CLI wiring + validation). No test touches Forge.

## forge_py screening gate (added 2026-07-29)

`--screen` (or `COMMANDER_BUILDER_FORGEPY_SCREEN=1`) puts a cheap
forge_py pre-filter in front of the `--search-budget` arm pool:
before the bandit spends ANY Forge games, every candidate swap is
staged through the shared `apply_proposal_to_deck` legality path and
goldfished against the base deck in forge_py (`forge_py_screen.py`,
invoking forge_py ONLY through the FP-001 correlation harness's
`run_forge_py_ab` seam — one sanctioned invocation path). The weakest
arms are pruned so the Forge budget concentrates on arms forge_py
already ranks as plausible.

**The contract — SCREEN, NOT JUDGE.** forge_py's measured agreement
with real Forge outcomes is r ≈ 0.898 rank correlation (FP-001
measurement; the paired corpus lives with `forge_py_correlation.py`).
That is plenty to rank a candidate pool and nowhere near enough to
render a verdict: nothing from the screen may ever feed a
keep/kept/advance decision. Forge remains the ONLY verdict engine —
the round's keep-if-better machinery is untouched, and every applied
swap still earns its place through real Forge sims.

Mechanics and guarantees (all pinned by tests in
`tests/test_forge_py_screen.py` + the screen section of
`tests/test_improve_search.py`, driven through injected runner/screen
seams — no test touches forge_py or Forge):

- **Default OFF = byte-identical.** With the flag and env var unset the
  screen seam is never consulted and the search round behaves exactly
  as before.
- **Prune rule:** keep the top `--screen-keep` fraction (default 0.5)
  of MEASURED arms, floor of 2 arms kept overall; pools of ≤ 2 arms
  skip screening entirely. `--screen-games` (default 20) forge_py
  games per arm — in-process Python sims, seconds not minutes.
- **A screen only condemns what it measured.** Unstageable arms,
  forge_py errors, and zero-decisive results are always KEPT; the
  bandit's own evaluate path handles them at pull time as today.
- **Loud degrade:** a missing/broken forge_py (or a screen crash)
  stands the screen down with a stderr note and the full unscreened
  pool proceeds — the screen can never block the improve loop.
- **No silent drops:** every pruned arm is logged to stderr with its
  screen score (house convention).

FP-014 hand-off note: the same hook was considered for
`commander-build`'s personalize stage and deliberately skipped — the
personalize pipeline (lift → bracket-steer → collection) transforms a
single deck through staged swaps and never materializes a candidate
SET to rank, and its `--improve` hand-off already delegates to
`commander-improve`, where this gate lives. No clean seam, nothing
duplicated.

## Open questions for the post-soak shakedown

1. Does per-swap probing beat spending the same total games on one
   bigger verdict sim of the curator's proposal? (FP-002 says curation
   is ~neutral on average — the bandit's bet is that per-swap
   attribution finds the wins the averaged proposal buries.)
2. Is 45 games/pull the right probe size, or do cheaper noisier pulls
   (more of them) win under UCB1? Reward variance vs pull count is
   exactly the bandit's trade to tune.
3. Screening thresholds (`--screen-keep 0.5`, 20 py-games/arm) are
   educated defaults, not measurements — once real screened rounds
   run, check the correlation log for pruned-arm regret (did the
   screen ever prune a swap Forge would have kept?).

---

# Adaptive change budget (`--mode auto`) — SHIPPED 2026-08-05

**The honest framing first:** per the FP-002 and FP-015 closures,
heuristic scores carry NO win-rate claim. The deck-health score
(`deck_health.compute_health_grade`, 0-100, descriptive — "this is a
bad combination of cards") is therefore allowed to decide exactly one
thing: **how much to change**. **What stays is still decided by the
empirical Forge A/B verdict**, unchanged. The score picks the budget;
the sims pick the keeps.

**Why:** curation intensity was manually selected (`--mode
polish|overhaul|free`), so a 30/100 deck got the same timid 5-card
polish as a 70/100 deck unless the operator remembered to escalate.

**What shipped** (`change_budget.py` + wiring):

- `resolve_tier(score)`: >=75 → `keep` (0-2 swaps, the existing
  bubble-analysis keep semantics); 55-74 → `polish` (5+5); 35-54 →
  `overhaul` (15+15); <35 → **new `rebuild` tier** (30+30). Score
  unavailable (outage / empty deck) → polish fallback with a printed
  note, never a crash, never an escalation on missing data.
- `--mode auto` on `commander-advise`, `commander-auto-curate`, and
  `commander-improve` resolves the tier at run time and prints
  `auto mode: overhaul (health 42/100)`. **Opt-in, NOT the default**:
  budget escalation multiplies curator + Forge A/B cost, and that
  spend is the operator's call. Explicit modes are unchanged; default
  runs are byte-identical.
- `rebuild` tier: 30+30 through the existing proposer/curator
  plumbing, plus an optional manabase-rebuild step (default on for
  rebuild only; `--no-manabase-rebuild` opts out) that recomputes the
  land mix via the FP-014 Karsten per-CMC model
  (`change_budget.plan_manabase_rebuild`, reusing
  `deck_builder_manabase` the way commander-build does, applied to the
  existing deck's colors/curve) and stages balanced land swaps through
  the same legality path (`apply_proposal_to_deck`) and the same A/B
  verdict as every other change. Land-count-neutral by construction.
- Web audit: `suggested_mode` payload field rendered next to the
  health-grade tile ("suggested mode: overhaul (health 42/100)") and
  an "Auto" option in the audit controls' Mode select (`?mode=`).

---

# FP-014 — Build-from-scratch deck assembly

**Status: SHIPPED and MERGED (first cut PR #14, 2026-07-21; second cut
2026-07-27 — partner-pair support + full Karsten per-CMC manabase;
corpus-norms steering added 2026-07-31 behind
`COMMANDER_BUILDER_CORPUS_NORMS`; A/B'd 2026-08-05 — see below).**

**Corpus-norms A/B result (2026-08-05, n=2 commanders):** built
Lathril and Talrand with the flag off and on, 40-game head-to-heads.
Talrand: norms-ON won 33–27 (+0.10). Lathril: norms-ON LOST 24–35
(−0.19) — though its entire card delta was one swap (a Forest for
Prowess of the Fair), so most of that margin is noise. Verdict at this
n: **inconclusive, leaning slightly negative; the flag stays
default-off.** Structural observation: norms steering barely perturbs
EDHREC-seeded builds (the seed already matches population norms); its
real leverage would be on the fallback role-target shells, which is
where any future test should focus. No further Forge spend planned. Original first-cut note: four commits (`76f1ca7` core assembler + the
`commander-build` CLI, `d02fc62` color-source manabase, `dd818b1`
lift/bracket/collection personalization, `545b2db` web `POST /api/build_deck`
+ `GET /api/build_job/<id>` + a "Build from scratch" tab + a
`commander-build --improve` hand-off). **Live-verified:** a real build of
*Krenko, Mob Boss* against live EDHREC produced a legal exactly-99 mono-red
deck, bracket-steered to B3, that loaded in the dashboard.

## What the first cut actually does

`commander-build --commander "<name>" --bracket <n>` runs the pipeline the
scope sketch below describes: commander + bracket → **seed a legal 99** from
EDHREC's average deck (the coherence source — see the honesty note) →
**color-source manabase** (`deck_builder_manabase.py`) → **personalization**
(`deck_builder_personalize.py` — lift / bracket-steer / owned-collection
stages) → optional **`--improve N`** hand-off into the existing
`commander-improve` empirical loop. The three modules are `deck_builder.py`
(orchestrator), `deck_builder_manabase.py`, and `deck_builder_personalize.py`;
the web surface is the "Build from scratch" tab wiring the async
`build_deck` / `build_job` endpoints.

## Honest limitations of the first cut (read before trusting output)

- **Coherence is borrowed, not synthesized.** The deck's spine is EDHREC's
  aggregate average deck taken largely verbatim — the community already made
  it coherent. When no average deck is published we fall back to a
  role-target-filled shell from the commander page: that path is a
  *defensible pile*, not a coherent deck. This is the "hard 20%" below, and
  it is **not** solved — it is deferred to the improve loop. With the
  corpus-norms extension (below) coherence is *additionally* borrowed from
  measured population norms mined off the local reference corpus — still
  borrowed, never synthesized.
- **Corpus-norms steering (2026-07, `corpus_themes.py`) is measured, blended,
  and flag-gated.** `commander-corpus-themes` scans the ~100-200 on-disk
  reference decks (pool harvests, `[PREMADE]` popularity pulls, precons,
  `[REF]` imports; `[USER]`/`[CONTROL]` excluded), derives per-deck
  structural profiles (role-bucket counts via `classify_role`, curve, lands,
  creature-type and oracle-motif theme signals — offline, snapshots only),
  clusters them with transparent threshold rules (tribal-X / spellslinger /
  aristocrats / lands-matter / stax-control / … / goodstuff-midrange
  fallback; no ML), and writes per-cluster norms + signature cards to
  `data/corpus_theme_norms.v1.json`. When `COMMANDER_BUILDER_CORPUS_NORMS=1`
  and the assembling shell matches a measured cluster (≥3 decks),
  `commander-build` steers toward the empirical norms **conservatively**: a
  50/50 blend of the hand-written `ROLE_TARGETS` / curve-model land count
  with the cluster medians, and at most 4 bounded like-for-like swaps that
  pull deficit roles toward the blend using the cluster's signature cards.
  The blend (not replacement) is deliberate: templates encode format wisdom,
  medians encode what this population actually builds; neither is allowed to
  overrule the other. Flag off / artifact absent / no cluster match = the
  build is byte-identical to before.
- **The manabase now uses the full Karsten per-CMC source table** (second
  cut): per-color targets are the MAX over each card's (cmc, pips) entry
  from the published 99-card Commander column (Karsten 2022 update), with
  the old two-anchor pip model preserved as the fallback for costs that
  can't be resolved offline. Cast turn is assumed = CMC (on-curve), the
  assembler's necessary simplification. Seed duals/fetches are still kept
  and topped up from the advisor's land tiers.
- **Lift personalization needs a harvested corpus (≥10 decks) or it skips.**
  With no corpus the lift stage is a no-op; bracket-steer and owned-bias
  still run.
- **First-cut decks are "legal + reasonable," not tuned.** The intended
  quality path is the `--improve` loop — Forge A/B sims turn a plausible
  pile into a measured one. Treat a raw `commander-build` output as a
  starting point, not a finished deck.

## Motivation

ManaFoundry.gg (and similar tools) assemble a **full deck from a chosen
commander** in one shot. commander-builder deliberately does the opposite
today: it is an *iteration engine* that improves an **existing** deck (the
README's own framing — "not a deck builder from scratch"). This plan
reverses that — take a commander (+ target bracket / archetype) and emit a
complete, legal 99 — with an angle the competitors structurally lack:
**assembled decks get Forge-VALIDATED, not just heuristically scored.**
Every other from-scratch builder stops at a static power heuristic; we can
hand the assembled list straight to the existing empirical
improve-loop and prove it out in simulation.

## Scope sketch (cite what already exists)

Seed and fill the shell from modules that are already built and tested:

- **Seed the skeleton** from the EDHREC average deck for the commander —
  `edhrec_client.fetch_average_deck` (coherent, no Moxfield dependency) —
  shaped by **archetype templates** (`archetype.py`) and **role targets**
  (`staples.ROLE_TARGETS`) so the ramp/draw/removal/wipe/protection counts
  land in-band from the start.
- **Synergy-driven picks** from the new **`lift_analysis.py`** — the
  co-occurrence matrix over the harvested corpus surfaces "pairs well with
  this commander/shell" candidates with empirical support, exactly the
  pick-selection signal a from-scratch builder needs.
- **Hit a target power level** with the new **`bracket_estimator.py`** —
  estimate the assembled list's bracket and steer picks (Game Changers /
  fast mana / combo density) up or down until the estimate matches the
  requested bracket.
- **Prefer owned cards** via **`collection.py`** — bias the fill toward
  what the user already owns (the same exclude/flag machinery the advisor
  now uses), so the first cut is buildable, not a wishlist.
- **Validate legality** with the guards the adversarial-review fix
  campaign hardened: the singleton / exactly-99-mainboard / drop-reporting
  checks in `web/deck_text_ops._apply_swaps_to_dck`, plus
  `_proposer_filters.enforce_color_identity` for color-identity legality.
- **Empirically tune** by handing the assembled `.dck` to the existing
  `commander-improve` loop — Forge A/B sims + knowledge_log verdicts turn
  a plausible pile into a measured one. **This is the validation moat.**

## The honest hard part

The assembler above is the **easy 80%**. Going from *"a pile of
role-appropriate, high-lift, in-color cards"* to *"a coherent 99 with a
real manabase"* — curve, color-source counts, the actual land base, and
the non-obvious glue that makes a deck *function* rather than merely
satisfy per-role quotas — is the **hard 20%** and the real research. Role
targets and lift scores get you a defensible shell; they do **not** get you
coherence. Expect the first cut to produce legal-but-mediocre decks that
the improve-loop then has to do heavy lifting on, and treat
"curated-coherence" as the open problem this plan actually has to solve,
not a detail.

## Substrate that already exists (why it's cheap to start)

`fetch_average_deck`, `archetype.py`, `staples.ROLE_TARGETS`,
`lift_analysis.py`, `bracket_estimator.py`, `collection.py`, the
legality/color-identity guards (`_apply_swaps_to_dck`,
`enforce_color_identity`), and the whole `commander-improve` empirical
loop are all shipped and tested. The first cut (above) composed them into
`deck_builder.py` and built the color-source manabase step. **What remains
open research is the coherence half of the hard 20%:** from-atoms synthesis
(a coherent 99 without leaning on EDHREC's aggregate seed) and the full
per-CMC Karsten source model. The first cut leans on the seed for coherence
and hands the rest to the improve loop; closing that gap — genuine
synthesis for commanders with no published average deck — is the remaining
FP-014 research.

---

# Performance backlog — carried from the 2026-07-25 optimization audit

Carried forward when the audit was archived to
`docs/archive/OPTIMIZATION_AUDIT_2026-07-25.md` (see it for full evidence,
line references, and measurements). Already landed and NOT repeated here:
the audit's low-risk "IMPLEMENTED" batch (`5dbb1f9`), P1+P3 Scryfall lookup
memo + offline-contract fix (#33), P4 collection-bias screening (#34), and
the follow-up memo-hygiene fixes (#43). Still unshipped:

**Tier 1**
- **P2. `card_score.DeckContext.without()` memo inheritance** — cut scoring
  re-derives every deck-level report per candidate (O(n²)); simulated fix
  measured 7.8× on a 99-card cut ordering.
- **P5. Forge `compare()` game-chunking** — second parallelism axis so a
  40-game comparison uses more than pod-count workers (~2.4× wall-clock);
  also switch worker sizing to physical cores.
- **P6. `meta_test` reference-loop parallelism** — currently single-core
  (~46 min for 5 refs × 5 games); expected 4-6×.

**Tier 2**
- **P7.** `run_ab_simulation` batches games into 2 JVM invocations instead
  of one JVM per game (measure startup cost from soak rows first).
- **P8.** `CardsLoader` DFC index from `zf.namelist()` (no full-zip
  decompression) + cached default loader.
- **P9.** Per-game stall watchdog in pods (kill on no new `Game Result:`
  line in 180 s) instead of whole-pod timeout.
- **P10.** `deck_health` shared per-audit lookup memo (mostly subsumed by
  P1; zero-risk standalone).
- **P11.** `knowledge_log` `init_db` gating + single connection + batched
  `collect_user_decks_summary`.
- **P12.** `run_matchup` parallel dispatch + `keep_partial_output=True`
  (the blocking path's timeout salvage is likely dead code — also Bug 3).
- **P13.** consistency Monte Carlo micro-opts (~40 % off
  `opening_hand_stats`) — deferred until a production consumer imports
  `consistency`.

**Tier 3 (cleanups / latent cliffs)** — lazy imports for CLI cold start;
one TTL-cached `salt_scores_cached()` replacing 3 duplicated salt-cache
paths; single shared `DeckContext` per advisor audit; `_mod_combo`
inverted index; `lift_swaps` synergy memo (+ first direct tests);
log-parser prefix dispatch/prefilters; gate `forge_log_tail` read on
failure; `iter_cached_names` single-field parse; collection file
read-once per build; `deck_pricing` single deck walk.

**Open bugs from the audit (not perf)**
1. WotC Game Changers scrape broken in production — every process serves
   the bundled `_FALLBACK` list; parser needs updating for the current
   page structure, and the divergence alarm only goes to stderr.
2. Mixed snapshot schema in the shared oracle cache dir — trimmed
   `forge_py` snapshots lack `prices`, silently dropping cards from
   `deck_pricing` totals.
3. `tests/conftest.py` has no network-blocking autouse fixture — unpatched
   lookup paths can still make live HTTP calls in tests.

---

# ── SHIPPED / REFERENCE — no open work beyond what's noted ────────────

# FP-016 — Replay-lite: turn-by-turn game replays from Forge's own logs

**Status: SHIPPED (merged 2026-08-01, PR #59) — slices 1–3 + docs.** Turn-by-turn game
review built on the sim stdout Forge ALREADY emits, not on `forge_py`
game state. This partially unparks FP-007 slice 5: it covers the
practical 80% (what happened each turn, who lost when and why, who won)
without waiting on engine work. **Explicitly:** log-replay is COARSER
than `forge_py` full-state replays — no board state, no hands, no stack;
you see what Forge chose to log. FP-007 slice 5 **stays parked** (with
FP-001) for true state-level replay.

## What shipped

1. **Persistence (default OFF)** — `replay_store.py`. Opt in with
   `COMMANDER_BUILDER_KEEP_GAME_LOGS=1` (or `--keep-logs` on
   `run_match` / `compare_versions`). The single seam is the tail of
   `ForgeRunner.run` — every harness (A/B, gauntlet, parallel chunks,
   compare pods, web sims) funnels through it — which splits each sim's
   stdout into per-game chunks and writes
   `~/.commander-builder/replays/<run-id>/game_<n>.log` + a per-run
   `index.json` (decks/seats, winner + eliminations from the EXISTING
   parser attribution, duration, truncated marker). One run dir per
   process; a lock serializes game-number allocation + atomic index
   writes under the threaded dispatcher. **Retention cap:** total
   replays dir bounded (default ~500MB, `COMMANDER_BUILDER_REPLAY_CAP_MB`)
   with oldest-run eviction at write time; the in-flight run stops
   recording (flagged `cap_reached`) rather than grow unbounded — the
   39GB log incident is the reason this is a hard requirement. Flag off
   ⇒ byte-identical sim behavior (pinned by test).
2. **Parser** — `replay_timeline.py`, pure `log → timeline`:
   `split_games` (per-game chunks at `Game Result:` boundaries, trailing
   partials kept) + `parse_timeline` (turns with active player, life
   events + per-turn life totals, eliminations with Forge's loss
   reasons including commander-damage-at-positive-life, best-effort
   cast/attack lines, game result). REUSES the log_parser /
   game_analyzer regex vocabulary (imported, not copied) so replays can
   never disagree with match scoring. Truncated/aborted logs
   (`loop_unattributed` rows) parse to a partial timeline with an
   honest `truncated: true` marker.
3. **Web viewer** — "Replays" in the left-rail nav. `GET /api/replays`
   (runs → game summaries from the index files), `GET
   /api/replay/<run>/<game>` (parsed timeline; clean JSON 404s; run ids
   allowlist-validated + resolved-path containment, log filename derived
   from the validated game number — no arbitrary path reads). UI:
   run list → game list → collapsible per-turn timeline (native
   `<details>`, keyboard accessible per the PR #36 patterns), life
   totals per turn, eliminations highlighted with a non-color marker,
   truncated banner on partials. Vanilla JS (`replays.js`) matching the
   existing `el()` no-innerHTML discipline.

## How to capture replays on the next soak / tier-3 run

```powershell
$env:COMMANDER_BUILDER_KEEP_GAME_LOGS = "1"   # or --keep-logs on the CLI
# ... run the soak / compare as usual ...
# then browse: python -m commander_builder.web → Replays rail section
```

---

# FP-007 — Unified MTG application (implementation plan)

**Decision (2026-05-26):** start FP-007. North star: one app consolidating
deck testing + card reference + rules lookup + a deck library + (later)
replays, instead of the current pile of CLIs + the audit web GUI.

**Reality:** ~6–10 weeks of work. This doc is the plan + the first slice;
it is NOT done. It stays on `feature` as the living spec; slices land
incrementally behind the existing web app so nothing regresses.

## What already exists (the substrate — don't rebuild)

- **Deck testing:** the Flask web app (`web/`) — audit, propose/sim, dashboard,
  combos, role-targets, image cache. This is the natural shell to grow into.
- **Card reference:** `oracle_store.py` + `scryfall_client` snapshot cache +
  `mtg_cards/` shared image/oracle data.
- **Combos / rules-ish:** `combo_detection.py`, `game_changers.py`, bracket
  enforcement, `staples.classify_role*`.
- **Library:** the `.dck` deck dir + `knowledge_log` iteration history +
  pricing series.
- **Engine:** Forge (via `forge_runner`) + the parked `forge_py` goldfish sim.

The unification is mostly **navigation + a shared card-reference surface**
over substrate that's 80% built — not a green-field rewrite.

## Gating (from STATUS.md)

"Ship FP-006 fully first." FP-006 (web GUI) is shipped and was just
exercised end-to-end in Chrome (every button, the full audit→propose flow).
The practical gate — "the web app works for a full iteration cycle on real
decks without touching a CLI" — is **met**. So FP-007 is unblocked to *start*,
incrementally.

## Slices (each independently shippable, behind the existing app)

1. **Card reference panel (first slice — scoped below).** A `/card/<name>`
   view + a search box in the topbar: oracle text, type line, mana cost,
   legality, price, printings — all from `oracle_store` / `scryfall_client`
   (no new datastore). This is the biggest missing leg and the cleanest to
   add to the existing Flask shell.
2. **Unified nav shell.** Left-rail sections: Decks (current) / Cards (slice 1)
   / Rules. Keep the deck dashboard as the Decks section.
3. **Rules / combo lookup.** Surface `combo_detection` + bracket rules as a
   browsable reference (what combos exist for a color identity, what pushes a
   bracket) rather than only inline in the audit.
4. **Library view.** Cross-deck search over the `.dck` set + knowledge_log
   history (which decks run a card, verdict history, price trend).
5. **Replays (last, gated on `forge_py`).** Turn-by-turn game review — only
   meaningful once `forge_py` produces inspectable game state; parked with FP-001.

## First slice — Card reference panel (concrete, ~1–2 sessions)

- **Backend:** `GET /api/card/<name>` in a new `web/routes_cards.py` blueprint:
  returns `{name, type_line, mana_cost, oracle_text, color_identity,
  legalities, prices, printings, image_url}` from `oracle_store.card_reference`
  + `scryfall_client.lookup_card` (cache-first; `cache=False` refetch on miss).
  Degrades to a clean 404 on unknown card.
- **Frontend:** a topbar "Cards" search input → `/card/<name>` overlay reusing
  the existing card-image overlay + a details pane. No framework change (same
  vanilla `el()` helpers).
- **Tests:** route returns shape on a stubbed lookup; 404 on miss; search
  input wired (verified in Chrome like the other buttons).

## Risks / notes

- Don't fork state: the unified app must keep using the same `deck_dir`,
  `knowledge_log`, and `mtg_cards/` cache — no parallel datastores.
- Keep each slice behind the working app so `feature` + CI stay green; this
  doc + slice-1 tests are the contract.
- FP-013 (project-tuned LLM) and replays remain parked; FP-007 does not
  depend on them.

## Status

**Slices 1–4 SHIPPED** (confirmed 2026-07-04; this entry was stale):
slice 1 card-reference panel (`30def0d` — `/api/card` + topbar Cards
search), nav shell + `/api/rules` + `/api/library` (merged via
`dac2ed6`), plus loading/empty/error-state polish and keyboard
accessibility (`ff8395a`, `e006f7c`, PR #5). Only slice 5 (replays)
remains, parked on `forge_py` game-state (with FP-001). **Update
2026-07-30:** FP-016 replay-lite ships log-based turn-by-turn replays
(the practical 80%); slice 5 stays parked for STATE-level replay only.

---

# FP-010 — Desktop EXE (status + how to build)

**Decision (2026-05-26):** package the web app as a double-click desktop EXE.
~16h total; this is the first pass — a working launcher + freeze pipeline +
tests. Gate ("web app proven via browser for a full cycle") is met (verified
in Chrome this session).

## What shipped

- **`commander_builder/desktop.py`** — runs `web.app.create_app` on a daemon
  thread and shows it in a native window via **pywebview** at
  `http://127.0.0.1:<free-port>/`. One process, no browser, no manual server.
  Injectable `webview` / `serve` hooks make the wiring unit-testable
  (`tests/test_desktop.py`, 6 tests). Entry point: `commander-builder-desktop`.
- **`packaging/commander-builder.spec`** + **`packaging/desktop_entry.py`** —
  PyInstaller one-folder freeze; bundles the Flask `templates/` + `static/`
  as data files (so `create_app()` finds them inside `_MEIPASS`).
- **`scripts/build_desktop.py`** — installs the `[desktop]` extra and runs the
  freeze. Output: `dist/CommanderBuilder/CommanderBuilder.exe`.
- **pyproject**: `[desktop]` extra (`pywebview`, `pyinstaller`, `flask`).

## Build it

```powershell
python scripts/build_desktop.py          # installs deps + freezes
# -> dist/CommanderBuilder/CommanderBuilder.exe
```
Run on Windows for a Windows EXE (PyInstaller doesn't cross-compile). First
build is slow (pywebview pulls a native EdgeChromium/pythonnet backend).

## Deliberately external (NOT bundled)

The EXE bundles only the Python app + Flask assets. These stay on disk and the
app locates them like the dev setup:

| Data | Size | Why external |
|------|------|--------------|
| Forge JAR | ~120 MB | huge; updated every set; user already has `vendor/forge/` |
| JRE | ~150 MB | huge; platform-specific |
| `mtg_cards/` (images + oracle) | ~180 MB | huge; grows over time |

When Forge/JRE are absent the app still runs — only the audit/sim calls that
shell out to Forge error per-request (same as a dev box without Forge). Card
images lazy-fetch from Scryfall through the existing cache.

## Remaining slices (the rest of the ~16h)

1. **First-run data bootstrap** — on first launch, detect missing
   `vendor/forge/` + `mtg_cards/` and offer a downloader (Forge release from
   GitHub, JRE, and prime the card cache) instead of silently degrading.
2. **Deck-dir picker** — a first-run prompt / setting for where `.dck` files
   live (today it defaults to the Forge userdata path; a packaged app may want
   `%USERPROFILE%\Documents\CommanderBuilder\decks`).
3. **Icon + window chrome** — app icon, single-instance guard, graceful
   shutdown of the Flask thread on window close.
4. **Installer** — wrap the one-folder dist in an installer (Inno Setup /
   NSIS) or ship a zip; optional code-signing.
5. **CI build job** — a Windows GitHub Actions runner that produces the EXE
   artifact on tag.

## Status

**All five slices SHIPPED** (confirmed 2026-07-04; this entry was
stale): downloader + deck-dir picker + window chrome + JRE extraction
(merged via `d13db07`), Windows CI build job (`bc4d101`), and the Inno
Setup installer + `build_installer.py` driver (`8146450`, PR #7).
Producing the `.exe`/installer remains a local
`python scripts/build_desktop.py` / `build_installer.py` run (deps are
heavy); CI builds the artifact on tag.

**EXE refreshed 2026-07-29** against everything that landed since the
first freeze (mobile-responsive layout, accessibility overhaul PR #36,
Build-from-scratch tab + async build/sim job endpoints, premade deck
type, FP-007 card-reference/rules/library nav). Spec changes:

- `datas` now bundles the whole `src/commander_builder/data/` package
  dir (picks up `oracle_diff_buckets.json`, FP-009; previously only the
  optional icon PNG shipped from there) — future package-data files are
  covered automatically.
- `hiddenimports` collects **all of `commander_builder`** instead of
  just `commander_builder.web`, so route handlers' lazy in-function
  imports no longer depend on bytecode analysis alone.
- Repo-root `data/` artifacts (`combos.json`,
  `corpus_theme_norms.v1.json`) stay unbundled on purpose: they are
  gitignored derived data and every reader has an explicit fallback /
  flag-gate (game-changers 53-card bundled list, corpus norms off by
  default) — same behavior as a fresh dev checkout.

Live-launch verified (pywebview window + HTTP probes against the
frozen server): `GET /` serves the shell with Build/Cards/Rules/
Library/Settings nav, `/api/health` ok, `/api/decks` lists decks with
`type` fields, `/api/dashboard?deck=…` returns the full payload,
`/api/library?card=Sol+Ring` cross-deck search works, and
`/api/rules/game_changers` serves the bundled fallback offline.

**EXE refreshed 2026-08-05** against `master` @ `d084ddf` — the
2026-07-29 freeze had frozen the app at roughly PR #53 and ~25 PRs had
landed since. Now included:

- **FP-016 replay-lite** — `web/routes_replays.py` blueprint,
  `static/replays.js`, and the Replays nav section. The old EXE 404'd
  `/api/replays`; this one serves it.
- **Consistency deck-health tile** (`static/deck_health_ui.js`).
- **Adaptive change budget** — `change_budget.py` plus the audit UI's
  Mode select (rendered from `static/app.js`).
- **Web UX batch** — `/api/dashboard/core` +
  `/api/dashboard/section/<name>` progressive load and the sidebar deck
  filter.
- **FP-017** — `edhtop16_client.py` and the `commander-tournament`
  entry point.
- **Dashboard outage guard** (PR #79).

**No spec changes were needed.** The 2026-07-29 collection strategy
absorbed all of the above by construction, and this rebuild is the
evidence that the strategy — not just its then-current output — is what
holds:

- `collect_submodules("commander_builder")` picked up every new
  first-party module with no edit. Verified against the frozen archive:
  **102 of 102** `src/commander_builder/**.py` modules are in the PYZ,
  including `change_budget`, `edhtop16_client`, `web.routes_replays`,
  `replay_store`, and `replay_timeline`.
- Bundling the whole `web/static/` + `web/templates/` dirs picked up
  `replays.js` with no edit. All 9 static assets and `index.html` ship;
  SHA-256 of the served `app.js`, `replays.js`, `nav.js`,
  `deck_health_ui.js` and `app.css` match the repo files (i.e. the EXE
  serves the CURRENT bundle, not a stale one).
- No new **package** data files landed — `src/commander_builder/data/`
  still holds only `oracle_diff_buckets.json` — so the whole-dir `datas`
  entry needed no change either.
- The repo-root `data/` exclusion still holds: nothing added since
  reads a repo-root artifact without a fallback. `replay_store` writes
  under `~/.commander-builder/replays/` (user home, not `_MEIPASS`), so
  replays resolve correctly from a frozen build.
- The only PyInstaller "missing module" warnings are the expected
  optional/delayed ones — `anthropic`, `psutil`, `fcntl` (POSIX),
  `forge_py.*` (external sibling repo, same deliberate exclusion as the
  Forge JAR) — all guarded at their import sites.

Live-launch verified again (`dist/CommanderBuilder/CommanderBuilder.exe`,
ephemeral port): `GET /` serves the shell containing the sidebar
deck-filter markup and the Replays section; `/api/health` ok with
`deck_dir` = `%USERPROFILE%\Documents\CommanderBuilder\decks`;
`/api/decks` lists 258 user+premade decks and `?all=1` lists all 492
`.dck` files in that library; **`/api/replays` → 200** with 7 recorded
runs; `/api/dashboard/core?deck=…` → 200 advertising
`deferred_sections: [lift_picks, pricing]`, and both
`/api/dashboard/section/<name>?deck=…` fetches → 200 `status: ok`.
