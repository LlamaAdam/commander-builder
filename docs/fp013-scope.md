# FP-013 — feasibility scope (project-tuned LLM via LoRA)

**Date:** 2026-05-28 · **Status:** scope memo (no build) · **Verdict:**
**stay parked** — but with a corrected data picture and an explicit
unblock path through FP-012 usage.

> Written after I quoted "only 29 rows" as the FP-013 data gate and was
> rightly challenged. The 29 was the *live* knowledge_log only — the real
> picture is much larger, and the gate is about *row type*, not raw count.

---

## The data picture (corrected)

| Source | Rows | What it is | Use for FP-013? |
|--------|----:|------------|-----------------|
| `knowledge_log.sqlite` (live) | **29** | recent curation iterations (audit → swaps → verdict) | ✅ rich manifest |
| `knowledge_log.ARCHIVED-lowgame-...sqlite` | **331** | iterations from pre-2026-05-24 (mostly 2–6 game) | ⚠ weak labels (low-game) |
| All `*gauntlet*.jsonl` (current) | ~208 | deck-level fixed-gauntlet win rates | label only (no manifest) |
| All `*throughput*.jsonl` (A/B soak) | ~2,200 | deck-level v2-vs-base win rates | label only (no manifest) |
| `combined_soak.jsonl` (canonical merged) | **~720** | de-duped high-confidence 40-game rows | label only |
| **Iteration manifests, high-confidence (40-game)** | **~29 + a slice of growing iterations** | the rows FP-013 actually needs | this IS the gate |

The "2000+" gate in STATUS isn't about raw rows — it's about
**(audit manifest, curator decisions, sim outcome)** triples. The soak
generates labels (the outcome); the curator generates manifests. We have
plenty of one and very few of the other.

## Why fine-tuning needs manifests, not just outcomes

A LoRA on Llama 3.1 8B / Qwen 2.5 7B that tries to *replicate the curator*
needs the question and the answer:
- **Question** = the audit manifest (deck state + role distribution +
  deficits + bracket).
- **Answer** = the curator's chosen swaps (with rationale).
- **Reward signal** = the seat-attributed sim verdict.

Soak rows alone give us answers without the question they answered. They
ground a *reward model* but not a *behavior model*.

## Realistic gate: ~1,000 high-confidence curator iterations

Reframing the gate from "2,000 rows" (ambiguous) to **~1,000
high-confidence curator iterations** — each = one audit → swaps → 40-game
verdict triple. Today we have ~29 live + a few in flight. Growth path:

1. **FP-012 (improve loop + bandit/Thompson) USAGE is the data pipeline.**
   Every improve run produces a chain of curator iterations with sim
   verdicts; the more we run `commander-improve` on real decks, the faster
   we approach 1,000.
2. The FP-002 deck-set campaign (now soaking) produces ~30 new decks, each
   of which can drive an improve run → tens of iterations per deck → low
   hundreds per soak campaign.
3. **At that rate**, ~1,000 high-confidence iterations is plausibly weeks
   of focused FP-012 usage, not 18-30 months. The original "moonshot"
   timeline assumed manual curation only.

## Other gates beyond data

- **Eval harness.** A fine-tuned model is worthless without paired-sim
  ground truth to score its proposals. Build alongside data growth:
  `scripts/eval_curator.py` that runs both the model and the current
  Claude-curator on a held-out manifest set and reports win-rate delta.
- **Beat-the-baseline.** Even with 1,000 manifests, fine-tuning only wins
  if it beats the current `commander-auto-curate` pipeline (free Claude
  via subscription, no infra). The bar is *not* "works at all" — it's
  "cheaper or better than the subscription curator we already have."
- **Cost.** STATUS pegs LoRA at $80–200 on A100; eval-sim cost may dwarf
  training cost (each eval = a 40-game sim ≈ ~10 min on box1; 100 eval
  decks ≈ ~17 box-hours of sim).

## Recommendation

**Keep FP-013 parked, with a precise unblock condition** (mirrors how
FP-001 was parked + redirected):

> Promote to active when (a) the live `knowledge_log` holds **≥1,000
> 40-game curator iterations**, AND (b) `scripts/eval_curator.py` (paired
> baseline vs candidate) exists and runs.

At that point the spike is bounded: one LoRA run ($80–200), one eval
batch (~17 box-hours), one verdict in this memo. Until then the highest
leverage is **using FP-012 a lot** — it both improves decks today and
grows the FP-013 training set as a side effect.

**Note on the soak that's currently running.** It produces *labels*
(deck-level win rates), not curator iterations. Useful for FP-002
margin regression; not directly useful for FP-013. Don't let the soak's
row count flatter FP-013 readiness — those are the wrong rows.

## What to do next (small, optional, cheap)

- Add a row-count health-check command (`commander-improve --health` or a
  small dashboard panel) that reports "high-confidence curator
  iterations: N / 1,000 toward FP-013," so we see the gate approaching.
  Nice-to-have, not blocking.
- Sketch `scripts/eval_curator.py` as a stub + interface (no model
  needed) so the gate's second condition has a place to land.
