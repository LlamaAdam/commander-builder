# FP-002 (reframed) — curator margin regression

**Status: REOPENED → first result in (2026-05-26).** The original FP-002 was a
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
- `tests/test_margin_analysis.py` — 18 pure-logic tests (A/B + gauntlet
  aggregation, margin banding, Pearson edge cases, the deck-file join,
  end-to-end `analyze`).

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
