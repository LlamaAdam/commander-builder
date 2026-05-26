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
  installed on the soak boxes). Aggregates `*throughput*.jsonl` rows per deck
  pair, computes win-rate margin = `(wins_b - wins_a) / decisive`, joins each
  pair to its original `.dck` to extract `deck_health` features, and reports
  per-feature Pearson `r` + a two-sided t-stat (df = n−2).
  - `python scripts/margin_analysis.py --min-games 40` (text) or `--json`.
  - `--decks DIR` (repeatable) overrides the deck search path.
- `tests/test_margin_analysis.py` — 13 pure-logic tests (aggregation, margin
  banding, Pearson edge cases, the deck-file join, end-to-end `analyze`).

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

## Verdict & next step

The reframing **works as an analysis** and the negative-class obstacle is
resolved, but **n=29 decks is too thin to ship a predictor**, and only one
feature clears significance. This is exploratory evidence, not a model.

To graduate from "analysis" to "predictor":

- **More unique decks**, not more games per deck — the unit of analysis is the
  deck. ~30 → ~80+ decks would let a single-feature OLS (wincon_protection →
  margin) be validated out-of-sample without sklearn.
- Then optionally a tiny pure-stdlib multiple regression / logistic fit on the
  2–3 features that survive. sklearn is still unnecessary at this scale.

The actionable takeaway *today* (no model needed): **the curator's expected
improvement is ~0; it pays off most on decks that already protect their
wincon and is near-useless on decks with big role deficits.** That argues for
pointing curation at already-coherent decks and using the deck-health
`under_built` signal (F2) to *fix structure first*, then curate.
