# Commander Builder — evaluation review, rules gaps, and a card-scoring formula

Reviewed against `LlamaAdam/commander-builder` @ HEAD (July 24 2026) and the
official Commander rules as of the **February 9, 2026** Banned & Restricted
and Brackets Beta updates.

Three sections:

1. **Verified defects** — things that are wrong today, with file/line refs
2. **Rules-coverage gaps** — where the evaluator can't tell a good deck from a bad one
3. **The card-scoring formula** — the thing you asked for, spec'd to drop in

---

## 1. Verified defects (fix these before adding features)

### 1.1 The banned list is wrong in both directions

`web/routes_decks.py:885` `_CORE_BANS` is a hardcoded set inside a route
function. Against the official list as of Feb 9 2026:

**Flagged as banned but currently legal (6 false positives):**

| Card | Reality |
|---|---|
| Coalition Victory | Legal — and is **on the Game Changers list** |
| Panoptic Mirror | Legal — and is **on the Game Changers list** |
| Painter's Servant | Unbanned Sept 2024 |
| Worldfire | Unbanned |
| Sway of the Stars | Unbanned |
| Tempest Efreet | Unbanned |

So `/api/deck_audit` will currently tell a user that two cards WotC
explicitly blesses at Bracket 3+ are illegal. It also emits a
"cards are banned in Commander" warning for them.

**Banned but missing (10 false negatives):**
Balance, Fastbond, Flash, Golos Tireless Pilgrim, Griselbrand, Karakas,
Leovold Emissary of Trest, Paradox Engine, Rofellos Llanowar Emissary,
Tolarian Academy.

**Missing entirely:** the *Lutri, the Spellchaser* case — unbanned Feb 9 2026
but **banned as a companion**, a designation the format has never had before.
Also missing: ante cards, Conspiracy-type cards, stickers/Attractions, and
the racially-offensive-card ban.

`Time Vault` is listed twice (harmless, but it's a tell that this set was
hand-typed and never audited).

**Fix:** delete `_CORE_BANS` entirely. `legalities.commander` is already in
every Scryfall snapshot on disk (`scryfall_client.py:246` even projects it)
and auto-updates with the format. Read it. Keep a tiny hardcoded overlay
*only* for the two things Scryfall can't express cleanly — the Lutri
companion carve-out, and any card where you want a louder warning.

### 1.2 The bracket estimator silently loses ~1.5 points of signal in two of its three callers

`bracket_estimator.estimate_bracket(deck_text, declared, *, avg_cmc=None, archetype=None, ...)`
never computes `avg_cmc` or `archetype` — they must be passed in.

Only **one** of three callers passes them:

| Caller | Passes `avg_cmc` / `archetype`? |
|---|---|
| `deck_dashboard.py:565` | ✅ both |
| `improvement_advisor.py:1283` (the `commander-advise` CLI) | ❌ neither |
| `deck_builder.py:994` (bracket steering during `commander-build`) | ❌ neither |

Which means during **build-from-scratch bracket steering** and every CLI
advise run, these `DEFAULT_WEIGHTS` entries can never fire:

```
curve_tight     +0.5
curve_high      -0.5
archetype_combo +1.0
archetype_stax  +0.5
```

That's a 1.5-point swing on a 1–5 scale, missing from the exact code path
that's supposed to steer a deck to a target bracket. A `commander-build
--bracket 2` run that produces a 2.2-avg-MV combo pile will not notice.

`avg_cmc` is one line — `deck_dashboard.py:384` already does
`round(sum(cmcs)/len(cmcs), 2)`. `archetype.classify(deck_path)` is already
importable. Hoist both into a helper and pass them everywhere.

### 1.3 The Game Changers scrape points at a stale page and can only ever grow

`game_changers.py:44`:

```python
WOTC_URL = "https://magic.wizards.com/en/news/announcements/introducing-commander-brackets-beta"
```

That's the **original beta announcement**. It still carries the launch list
(~40 cards). Your bundled `_FALLBACK` is 53 names and — I checked all seven
color groups — **exactly matches the current Feb 9 2026 list**, including
Farewell and Biorhythm. Good hand-curation.

But the mechanism is broken in a way that will bite later:

- `fetch_game_changers()` returns `scraped | _FALLBACK`. The union is
  deliberate (a parser regression can't shrink the list) — but it also means
  **a card can never be removed from Game Changers**. WotC has already
  removed cards from this list once. When it happens again, your fallback
  pins it forever and you'll be flooring decks to B3 for no reason.
- The scrape target is a frozen announcement, so the "live" path adds
  nothing and can only re-add cards you'd want dropped.

**Fix:** point at `https://magic.wizards.com/en/formats/commander` (the
maintained list), and change the merge to *replace* on a successful,
sanity-checked parse (e.g. accept the scrape only if it yields ≥ 40 names
and ≥ 80% overlap with the fallback), falling back wholesale otherwise.
Log when scraped and bundled disagree — that's your staleness alarm.

### 1.4 The two-card-combo bracket floor is stricter than the actual rule

`combo_detection.combo_bracket_floor()` — any game-ending combo with ≤ 2
cards returns floor **4**.

The official Bracket 3 text is *"no intentional **early-game** two-card
infinite combos"*, with late-game two-card combos explicitly permitted.
Bracket 3 is where most upgraded decks live, so this misfires constantly:
Heliod + Ballista in a 4-mana-average lifegain deck gets slammed to B4.

**Fix:** gate on combo *speed*, not card count. You have the mana costs —
`combined_mv = Σ cmc(piece)`. Something like:

```python
def combo_bracket_floor(combo, lookup=None) -> int:
    if not is_game_ending(combo):
        return 1
    n = len(combo.get("cards") or [])
    mv = _combined_mv(combo, lookup)      # None if lookup unavailable
    if n <= 2:
        if mv is not None and mv >= EARLY_GAME_MV_CEILING:   # 7-8ish
            return 3          # late-game 2-card combo: B3 legal
        return 4              # early or unknown: keep the strict floor
    return 3
```

Keep the strict behavior when the lookup fails — a conservative floor on
missing data is right.

### 1.5 The health grade cannot see the commander

`deck_health.py` reads **only** the `[Main]` section. Every signal, every
grade component, every reason string. The commander is invisible.

In Commander, the commander is the single most load-bearing card in the deck
— it's in your opening hand every game. Consequences today:

- A 5-mana commander in a deck with 8 ramp sources grades identically to a
  2-mana commander with 8 ramp sources.
- Color-identity coherence is never checked against the actual commander.
- `role_targets` doesn't credit a commander that *is* the card draw engine
  (Edric, Kydele, Tatyova) — the deck gets dinged for a `draw` deficit it
  doesn't have.
- Voltron decks, where the commander is the win condition, have no
  `finisher`/`win_condition` credit at all.

This is the single highest-leverage correctness fix in the health module.
Thread the commander through `compute_deck_health(deck_text)` and give it
its own weighted component.

---

## 2. Rules-coverage gaps — what the evaluator can't currently see

Current deck evaluation surface, honestly summarized:

- `compute_health_grade` = 0.40 × role deficits + 0.25 × land-count band +
  0.35 × (mana sinks, wincon protection)
- `estimate_bracket` = 2.0 + weighted name-set membership signals
- `lift_analysis` = co-occurrence lift over your harvested corpus

Everything else is boolean set membership against hand-curated frozensets.
Here's what a Commander player evaluates that none of that touches.

### 2.1 No opening-hand / consistency math — the biggest gap

Zero code in `src/` touches mulligans, opening hands, or hypergeometric
probability. Grepped it; it's genuinely absent, and it has no FP-xxx entry.

This matters more than anything else on this list because **Commander is a
singleton format** — consistency, not raw power, is what separates a good
deck from a bad one at every bracket. The questions a builder actually asks:

- P(keepable 7) — 2–5 lands, castable early play
- P(≥ 3 lands by turn 3), P(≥ 5 lands by turn 5)
- P(commander castable on curve) given its MV and pip requirements
- P(color-screwed) — you have lands but not the right colors
- P(≥ 1 ramp piece in opener)

All of this is closed-form hypergeometric or a 10k-iteration Monte Carlo over
the decklist. Milliseconds, fully offline, no Forge, no LLM. And it gives you
a *continuous* consistency score, which is exactly the kind of feature FP-002
is starving for — your current 31 features are almost all post-hoc sim
outputs plus coarse deck-health counts.

`architecture.md:653` already admits the value: the superseded goldfish step's
*"pre-execute consistency check (mulligan rate, commander-turn) has
independent value before committing to a full sim."* It never got built.

**Ship this as a module.** `consistency.py`:

```python
def opening_hand_stats(deck: Deck, *, trials: int = 10_000, seed: int = 0) -> dict:
    """
    → {
        "p_keepable_7": 0.83,
        "p_3_lands_by_t3": 0.71,
        "p_commander_on_curve": 0.44,
        "p_color_screw": 0.09,
        "avg_lands_in_7": 2.9,
        "mulligan_rate": 0.17,
      }
    """
```

Rules that make this correct for Commander specifically: London mulligan
(draw 7, bottom N), no free mulligan in most groups, and the commander is
*always* available from the command zone — so "commander castability" is a
turn-count question, not a draw question.

### 2.2 Land count is checked; mana *quality* isn't

`_score_mana_health` scores `33 ≤ effective_lands ≤ 38` as 100, and penalizes
12 points per land outside the band. A Command Tower and a Wastes are worth
exactly 1.0 each.

Not checked: untapped vs. ETB-tapped ratio, fixing quality, utility-land
count, or — the big one — **whether the color sources actually meet Karsten
targets**.

The irony: you already have a correct Karsten implementation.
`deck_builder_manabase.KARSTEN_99_SOURCES` transcribes the 99-card column of
the 2022 table, and `color_source_targets()` implements the
most-demanding-card rule properly. It runs **only at build time** and is
never used to *evaluate* an existing deck.

**Fix (cheap, high value):** expose `manabase_report(deck) -> dict` that runs
the existing `pip_stats` → `color_source_targets` → `land_color_sources`
pipeline over an existing deck and reports per-color `sources vs. target`.
Then add it as a health-grade component. That converts your best piece of
real math from write-only into the deck's mana grade.

Also: `produced_mana` is on every Scryfall snapshot and read by **zero**
code. You're regex-inferring mana production from oracle text
(`deck_health._MANA_SINK_ACTIVATION_RE`) when the structured field is sitting
right there.

### 2.3 Interaction is counted, never characterized

`ROLE_TARGETS` says `removal: 8, wipe: 3`. A deck with 8 creature-removal
spells and zero answers to artifacts, enchantments, graveyards, or the stack
scores a perfect 100 on role deficits.

In multiplayer Commander that deck loses to the first Smothering Tithe.
What's needed is a **coverage matrix**, not a count:

| Answer type | Count | Min for B3 |
|---|---|---|
| Creature (spot) | 6 | 4 |
| Artifact / Enchantment | 0 | 2 |
| Graveyard hate | 0 | 1 |
| Stack interaction | 2 | 0 |
| Board wipe | 3 | 2 |
| Instant-speed share | 45% | 40% |

That last row matters too: 8 sorcery-speed removal spells in a deck with no
instant-speed interaction plays completely differently, and nothing in the
codebase distinguishes them. You already parse `type_line`.

Forge card scripts (`forge_script_parser.CardScript.abilities[].effect`) give
you the actual Forge effect primitive — `Destroy`, `Exile`, `Counter`, `Pump`
— which is a far more reliable classifier than the `_ROLE_PATTERNS` regex
table, and it's fully offline. That corpus is currently only used by
`deck_library_analyzer` for aggregate counters.

### 2.4 Curve is a single scalar band, not a shape

The only curve signal anywhere is `avg_cmc ≤ 2.6` (+0.5) / `≥ 3.8` (−0.5) in
the bracket estimator — and per §1.2 it doesn't even fire in two of three
callers.

Never computed: the MV histogram, the count of 1–2 drops, top-heaviness, or
curve-vs-ramp alignment. Two decks both averaging 3.2 — one a smooth
5/12/14/10/6/3 curve, the other 20 two-drops and 15 six-drops — are
indistinguishable to every part of this program.

### 2.5 No win condition check

`ROLE_TARGETS` has entries for ramp, draw, removal, wipe, and protection.
There is **no `finisher` or `win_condition` target**. `count_wincon_protection`
counts cards that *protect* a win attempt — but nothing verifies the deck can
actually win. A deck of 99 ramp and card draw grades an A.

`staples.classify_role_extended` already emits `win_condition` and `finisher`
labels. They're just never counted against a target.

### 2.6 Redundancy and "one piece away" are invisible

`detect_combos_in_deck` is an exact set-superset test over ~20 offline / ~1500
refreshed combos. It answers "do I have this combo," never "am I one card
away from it" — which is the single most actionable suggestion a deck builder
can give, and directly feeds the scoring formula below.

Same for effect redundancy: singleton format means a deck needs 3–4
functionally-similar effects to reliably draw one. Nothing counts functional
duplicates.

### 2.7 No construction-legality validator

Construction rules are enforced only incidentally, inside
`web/deck_text_ops._apply_swaps_to_dck` (singleton, exactly-99) and
`_proposer_filters.enforce_color_identity`. There's no module that answers
"is this pile a legal Commander deck?"

Missing checks: exactly 100 cards, singleton (excepting basics and
`A deck can have any number of cards named ___`), color identity ⊆ commander's
(including mana symbols in *rules text*, which is the part people get wrong),
commander is a legendary creature or says "can be your commander", Partner /
Friends Forever / Background / Doctor's companion pairings, and the new
companion rule for Lutri.

That's a self-contained ~200-line module and it's the thing users will hit
first when they import a deck.

---

## 3. The card-scoring formula

### 3.1 What exists, and why a formula is the right addition

There is **no per-card score anywhere in the codebase**. What exists instead:

- `_advisor_heuristic._heuristic_swap_recommendations` orders adds by
  **bucket insertion order**, then re-sorts by `(role_rank, trending_rank)`.
  `inclusion_pct` and `synergy_pct` are used *only* as boolean gates
  (`≥ 30.0`, `≥ 25.0`) and to write rationale strings — they never enter a
  sort key.
- Cuts are ordered by `sorted(deck_cards)` — **alphabetically**, with a
  comment at `_advisor_heuristic.py:490` conceding "no per-card score exists
  at this point."
- `lift_analysis.lift_candidates` is the only real per-card number:
  `mean(top-5 lifts)`, co-occurrence only — no mana value, no role, no color,
  no price.

Two different "match %" scales already exist and quietly disagree
(`deck_dashboard.match_score` includes a rank bonus; `_helpers._match_pct_from_evidence`
deliberately omits it).

So: a unified per-card score is a real gap. But one honest caveat first,
because your own docs raise it.

**FP-014 states the project's position:** *"assembled decks get
Forge-VALIDATED, not just heuristically scored… every other from-scratch
builder stops at a static power heuristic."* And FP-002 currently reports
that at n=45, **no pre-sim deck feature predicts curation margin at |t| ≥ 2**.

Both are reasons to scope the formula correctly. Don't position it as a truth
claim about card quality — position it as a **ranking prior that shrinks the
search space Forge has to validate**. Its success metric is not R² against
margin. It's:

> Does A/B-simming the top-k scored swaps beat A/B-simming k
> threshold-passing swaps chosen by today's bucket order?

That's a directly measurable question with the harness you already have, and
it survives FP-002's null result — a scorer can be a useful ordering even
when no single feature regresses on margin. It also pairs naturally with
FP-012's UCB1: the score becomes the bandit's **prior**, so the arm search
starts warm instead of uniform.

### 3.2 Design

Multiplicative gates × weighted additive base × bounded modifiers. Same shape
as `bracket_estimator.DEFAULT_WEIGHTS` — one documented dict as the tuning
surface, every term explainable in a UI tooltip.

```
CardScore(card | deck, commander, bracket, context)

  = Gate(card)  ×  [ 100 · Σ wₖ · fₖ(card | deck)  +  Σ mⱼ(card | deck) ]

  clamped to [0, 100]
```

**Gates** — multiplicative, return 0 or 1. A gate failure means the card
cannot be considered, and the reason is reported.

| Gate | Condition for 0 |
|---|---|
| `legal` | `legalities.commander != "legal"` (Scryfall, not a hardcoded set) |
| `color_identity` | `card.color_identity ⊄ commander.color_identity` |
| `singleton` | already in deck and not a basic |
| `bracket_cap` | adding it would exceed the bracket's Game Changer cap (0 at B1/B2, 3 at B3) |

### 3.3 Base components

All `fₖ ∈ [0, 1]`. Weights sum to 1.0.

```python
CARD_SCORE_WEIGHTS = {
    "consensus":   0.18,   # does the format agree this belongs here
    "synergy":     0.24,   # does it fit THIS deck
    "role_fit":    0.28,   # does the deck need this job done
    "curve_fit":   0.16,   # does it fit the mana curve and land count
    "mana_fit":    0.14,   # does it help the manabase meet Karsten targets
}
```

**`consensus`** — EDHREC inclusion. Available on `CardEntry.inclusion_pct`.

```python
f_consensus = clamp(inclusion_pct / 60.0, 0.0, 1.0)
```

60% inclusion saturates — above that you're measuring "is a staple," which
`role_fit` and `synergy` already handle better. Fall back to Scryfall's
`edhrec_rank` (present in every snapshot, currently **read by zero code**)
when EDHREC is unreachable: `f = clamp(1 - log10(rank)/4.5, 0, 1)`.

**`synergy`** — blend the two independent synergy signals you have.

```python
f_synergy = 0.55 * clamp(synergy_pct / 40.0, 0, 1) \
          + 0.45 * clamp((lift_score - 1.0) / 2.0, 0, 1)
```

`lift_score` from `lift_analysis.lift_candidates`. Renormalize to the EDHREC
term alone when the corpus is under `MIN_CORPUS_DECKS` (10), rather than
feeding a zero — same "unavailable ≠ bad" contract `deck_health` already
uses.

**`role_fit`** — the deficit-driven term. This is what makes the score
*deck-relative* instead of a global power ranking.

```python
role   = staples.classify_role_extended(oracle_text, type_line)
report = staples.role_target_report(deck_card_names)
count, target = report["roles"][role]["count"], ROLE_TARGETS[role]

if count < target:                       # under-built → reward
    f_role = 0.5 + 0.5 * (target - count) / target
elif count < ROLE_SATURATION_THRESHOLDS[role]:
    f_role = 0.5 * (1 - (count - target) / (sat - target))
else:                                    # saturated
    f_role = 0.0
```

Two changes this needs upstream:

1. Add `finisher` / `win_condition` to `ROLE_TARGETS` (see §2.5). Suggested:
   `finisher: 3`. Without it the formula will happily build 99 ramp.
2. Have `role_target_report` count **the commander too**, and let a commander
   that fills a role reduce that role's target (Edric shouldn't demand 10
   draw spells).

**`curve_fit`** — needs the MV histogram from §2.4.

```python
target_curve = archetype_curve(archetype, bracket)   # {1:6, 2:12, 3:14, ...} normalized
mv           = int(min(card.cmc, 7))
deficit      = max(0.0, target_curve[mv] - actual_curve[mv])
f_curve      = clamp(deficit / max(1.0, target_curve[mv]), 0.0, 1.0)
```

Aggro/combo shift the target curve down, control up. This is where
`archetype.classify` finally earns its keep — right now it feeds nothing but
pod diversity and a bracket nudge.

**`mana_fit`** — the highest-value component and the one nobody else has,
because you already built the math.

```python
targets = color_source_targets(identity, pip_stats(deck_names, lookup))
sources = current_source_counts(deck_names, lookup)   # land_color_sources()
produced = card.get("produced_mana") or []            # ← currently unread

f_mana = mean(
    clamp((targets[c] - sources[c]) / max(1, targets[c]), 0, 1)
    for c in produced if c in identity
) if produced else 0.0
```

A land that fixes your *most under-served* color scores near 1.0; a fifth
Island when you're already at target scores ~0. This is Frank Karsten's table
doing real evaluative work instead of only firing once at build time.

### 3.4 Modifiers

Additive on the 0–100 scale, each bounded, each producing an explanation
string. These encode the bracket rules and the things that aren't smooth
functions.

| Modifier | Range | Trigger |
|---|---|---|
| `combo_completion` | **+15** | card completes a known combo where every other piece is already in the deck |
| `combo_partial` | **+6** | 3-card combo, 2 pieces present |
| `redundancy_relief` | **+5** | deck has < 3 instances of an effect this card duplicates |
| `owned` | **+6** | `collection.owns()` and collection bias is active |
| `price_penalty` | **−0 … −12** | `−12 · clamp((usd − soft_cap) / soft_cap, 0, 1)`, `soft_cap` from budget setting |
| `salt_penalty` | **−0 … −10** | at bracket ≤ 3 only: `−10 · clamp((salt − 1.5) / 2.5, 0, 1)` — matches `_SALT_WARN_THRESHOLD` |
| `bracket_pressure` | **−0 … −20** | tutor count already ≥ target for bracket ("tutors should be sparse" at B1/B2); fast mana over budget; extra-turn card when one is already present (B1/B2 forbid, B3 forbids chaining); any MLD card at B ≤ 3 |
| `mdfc_bonus` | **+3** | modal land — `_MDFC_LANDS`, already tracked, counts 0.5 land in the health grade |

`combo_completion` is the one that will visibly change recommendations
tomorrow. Today a candidate that completes Heliod + Ballista when Heliod is
already in the deck gets **exactly zero** boost — combo membership is never
consulted during ranking.

### 3.5 Cut scoring

Cuts are currently alphabetical. Reuse the same function:

```python
cut_score(card) = 100 - CardScore(card | deck_without_card, ...)
```

Recompute against the deck *minus that card* so a saturated role correctly
makes its weakest member the cut candidate. Guard rails, all of which already
exist in the codebase:

- never cut a `Protect=` line (`protected_cards`)
- never cut below `ROLE_TARGETS` on any role
- never cut a land if that drops effective lands below the 33 floor
- never cut a piece of a detected in-deck combo
- respect the like-for-like role constraint already enforced in
  `deck_builder_personalize.lift_swaps` (lines 217–224)

### 3.6 Where it plugs in

Highest leverage first. All signatures current.

1. **`_advisor_heuristic.py:403`** — replace
   `_rank(r) -> tuple[int, int]` with `score_card(...) -> float`.
   `inclusion_pct`, `synergy_pct`, `bucket`, `role`, and `trending` are all
   already in scope at that point and thrown away. The values already flow
   into `SwapRecommendation.evidence` (lines 383–389), so the UI can show the
   breakdown with **no schema change**.
2. **`_advisor_heuristic.py:490`** — the alphabetical cut loop.
3. **`deck_builder_personalize.synergy_scorer` (line 91)** — it's already
   consumed as `quality_of: Callable[[str], float]`. A composite scorer drops
   into that slot with zero signature churn, upgrading personalization stages
   1 and 3 at once.
4. **`deck_builder._fallback_candidates` (line 304)** — currently raw bucket
   concatenation, truncated by `nonlands[:nonland_target]`. Ordering here
   literally decides which cards make the 99 on the no-average-deck path.
   Biggest single quality win for `commander-build`.
5. **`lift_analysis.lift_candidates` (line 458)** — its docstring calls it
   "the one true definition — every surface routes through here." Widening
   its row with `components: dict` propagates the breakdown to dashboard,
   advisor, CLI report, and builder for free.

**Don't** put it in `staples.classify_role` — that function's internal
`score` field is a classifier argmax with entirely different semantics, and
overloading it would silently change role labels project-wide.

### 3.7 Calibration

Weights start hand-set (above). Then:

1. **Ordinal sanity suite** — assert known orderings on fixed decks.
   Sol Ring > Worn Powerstone in every deck. Rhystic Study > Divination at
   B4, and the gap narrows at B2. Cheap, offline, catches sign errors.
2. **Rank-correlation against your own history** — for each `knowledge_log`
   iteration with `verdict='kept'`, the added cards should score above the
   cut cards. Spearman ρ over the manifest is the metric. You have the rows
   (mind the pre-2026-07-19 win-rate denominator change and the
   `id < 314` measurement artifacts flagged in STATUS.md).
3. **The real test** — top-k-by-score vs. k-by-current-bucket-order, both
   A/B simmed through `compare_versions`. Same harness, same game counts.
   That's a direct answer to "does the formula help," and unlike FP-002's
   regression it doesn't need a per-feature t-stat to be conclusive.
4. **Feed FP-002** — `f_consistency` from §2.1 and the per-color source
   deficits from §2.2 are exactly the kind of *pre-sim, continuous*
   features the 31-feature set is missing. Its current features are mostly
   post-hoc sim outputs, which is part of why nothing regresses.

---

## 4. Suggested build order

| # | Item | Effort | Why now |
|---|---|---|---|
| 1 | Replace `_CORE_BANS` with Scryfall `legalities.commander` + Lutri overlay | S | Actively wrong output today (§1.1) |
| 2 | Pass `avg_cmc` + `archetype` in all three `estimate_bracket` callers | S | 1.5 points of dead signal in the steering loop (§1.2) |
| 3 | `consistency.py` — opening hand / mulligan / commander-on-curve | M | Biggest evaluation gap; offline; feeds both the formula and FP-002 (§2.1) |
| 4 | `manabase_report()` — run the existing Karsten math on existing decks | S | Best math in the repo is write-only (§2.2) |
| 5 | `CardScore` v1 behind a flag, wired into `_advisor_heuristic._rank` | M | The formula (§3) |
| 6 | Commander-aware `deck_health` | M | The most important card is invisible to the grade (§1.5) |
| 7 | `deck_legality.py` — 100 cards, singleton, CI, commander eligibility, partners, companion | M | First thing a user hits on import (§2.7) |
| 8 | Interaction coverage matrix + instant-speed share | M | Turns a count into a diagnosis (§2.3) |
| 9 | Combo `one_piece_away()` + `combo_completion` modifier | S | Most actionable single suggestion type (§2.6) |
| 10 | Speed-gated `combo_bracket_floor` | S | Currently stricter than WotC's own rule (§1.4) |
| 11 | Game Changers scrape: new URL + replace-with-sanity-check | S | Cannot currently shrink (§1.3) |
| 12 | MV histogram + `curve_fit`; `finisher` in `ROLE_TARGETS` | S | Prereqs for §3.3 (§2.4, §2.5) |

Items 1, 2, 4, 10, 11 are all small and independently shippable — a good
afternoon.

---

## Sources

- [Introducing Commander Brackets (beta) — Wizards of the Coast](https://magic.wizards.com/en/news/announcements/introducing-commander-brackets-beta)
- [Commander Brackets Beta Update — February 9, 2026](https://magic.wizards.com/en/news/announcements/commander-brackets-beta-update-february-9-2026)
- [Commander Banned and Restricted Announcement — February 9, 2026](https://magic.wizards.com/en/news/announcements/commander-banned-and-restricted-february-9-2026)
- [Magic Banned & Restricted List](https://magic.wizards.com/en/banned-restricted-list)
- [Commander format page](https://magic.wizards.com/en/formats/commander)
- [MTG Commander Game Changers List 2026 — all 53 cards](https://scrollvault.net/guides/game-changers.html)
