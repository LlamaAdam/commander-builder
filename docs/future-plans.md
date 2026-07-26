# Future plans (consolidated)

> Consolidated 2026-05-26 from the per-FP plan docs; **reordered
> 2026-07-25** — active / work-needed items at the top, shipped or
> fully-parked reference material at the bottom. **STATUS.md -> Parked
> plans is the authoritative status**; this file collects the detailed
> findings/plans in one place.

---

# ── ACTIVE / WORK NEEDED ──────────────────────────────────────────────

# FP-015 — Unified per-card scoring formula (`CardScore`)

**Status: IN PROGRESS — implemented on `feature/eval-fixes`,
committed 2026-07-25 as `25c1a54`.** The spec below (2026-07-24) was
implemented alongside the REVIEW.md build-order items: `card_score.py`
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
(default None/empty — the default path is untouched);
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

**Open (next slices):** RUN the bubble arm
(`--arms bucket,bubble`) once the CardScore tier-3 pilot finishes —
the two runs must not compete for Forge. Same honesty contract as
CardScore: heuristic prior, Forge stays the arbiter.

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

**Status: REOPENED → first result in (2026-05-26) → n=45 gauntlet re-run
(2026-07-23, the ≥+10-deck unblock fired; see "Result 2026-07-23" below).**
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

**Status: SHIPPED 2026-07-24 (code + tests); empirical validation
PENDING post-soak.** The full-slice search deliberately shipped without
a live Forge shakedown — the gauntlet soak owns the CPU — so the design
below is unit-verified against injected sims only. Post-soak, run a
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

## Open questions for the post-soak shakedown

1. Does per-swap probing beat spending the same total games on one
   bigger verdict sim of the curator's proposal? (FP-002 says curation
   is ~neutral on average — the bandit's bet is that per-swap
   attribution finds the wins the averaged proposal buries.)
2. Is 45 games/pull the right probe size, or do cheaper noisier pulls
   (more of them) win under UCB1? Reward variance vs pull count is
   exactly the bandit's trade to tune.

---

# FP-014 — Build-from-scratch deck assembly

**Status: SHIPPED — first cut (2026-07-21, `feature/fp014-build-from-scratch`,
unmerged pending PR).** Four commits (`76f1ca7` core assembler + the
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
  it is **not** solved — it is deferred to the improve loop.
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

# ── SHIPPED / REFERENCE — no open work beyond what's noted ────────────

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
remains, parked on `forge_py` game-state (with FP-001).

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
