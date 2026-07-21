# FP-012 next slices — scoping (intent-learning + Bayesian swap-opt)

**Date:** 2026-05-27 · **Status:** UNPARKED → scoping (design surfaced for
approval before any large build) · **Gates met:** knowledge_log >=150 rows,
programmatic proposer (`commander-auto-curate`), concurrent JVMs (`run_ab_batch`).

> FP-012 (autonomous deck-improvement agent) shipped slices 1–2: `commander-improve`
> fixed-N greedy loop + the bandit (`bandit.py`: ε-greedy / UCB1 swap arms). The
> "parked" remainder was *scope*, not a blocker. These are the next two slices.
> Both build on existing seams — neither is greenfield.

---

## Slice A — intent-learning (a deck's intent guides the improver)

**Problem today.** `run_improve_loop` (improve.py) optimizes via auto-curate
toward bracket-fit + win margin. It does NOT explicitly preserve the deck's
*intent* (archetype / theme / key wincons) — so a curated v2 can drift away
from what made the deck that deck.

**The seam already exists — this is wiring, not detection.**
- `archetype.classify(deck_path)` → Archetype (heuristic; `claude_archetype` /
  `ollama_archetype` variants exist).
- `staples.detect_themes(deck_oracles)` → list[str] of themes.
- `moxfield_import.find_top_liked_deck_for_commander` / fetch-by-URL → a deck.

**Design.**
1. `learn_intent(deck) -> Intent{archetype, themes, key_wincons, color_identity}`
   — compose the existing classifiers + a wincon scan (reuse the audit's
   win-condition detection). Accept a Moxfield URL or a local `.dck`.
2. Thread `Intent` into `run_improve_loop`:
   - **Soft bias** the advisor's candidate adds toward the intent's themes.
   - **Auto-protect** the intent's key wincons / signature synergy pieces
     (extend the existing protected-card list) so the curator can't cut the
     deck's identity.
3. New `commander-improve --learn-intent <url|dck>` flag; intent is advisory by
   default.

**Key decision to confirm:** intent as a **soft bias + protect-list** (recommended)
vs a **hard constraint** (reject swaps that change archetype). Soft keeps the
win-margin objective primary; hard risks stalling the loop.

**Effort:** ~4–6h (Channel B). Mostly an `Intent` dataclass + protect-list
integration + tests with injected classifiers (no Forge/Anthropic in tests).

---

## Slice B — Bayesian swap optimization (beyond the bandit)

**Problem today.** `bandit.py` treats each swap as an **independent** arm
(ε-greedy / UCB1). It doesn't model swap **interactions** (two adds that only
pay off together) or carry an uncertainty posterior.

**Options, with the honest trade-off.**
- **B1 — Thompson sampling policy (recommended first step).** A pure-stdlib
  Bayesian policy (Gaussian/Beta posterior per arm) dropped in alongside the
  existing ε-greedy/UCB1, selected via `--strategy thompson`. Captures
  uncertainty better than UCB1, mirrors the existing policy interface, **no new
  dependency.** ~3–4h.
- **B2 — full GP-based Bayesian optimization over swap *combinations*.** A
  surrogate model (GP) + acquisition (EI) over the combinatorial swap space —
  the only thing that models interactions. **But:** needs numpy/scipy/sklearn
  (deliberately NOT installed — see FP-002 notes), AND each evaluation is a
  ~40-game sim (~10 min), so a sample-hungry GP fights the real bottleneck (sim
  time). Likely poor ROI vs B1 until sims get much cheaper (cf. FP-001 fork +
  the FP-004 seed, which lowers sim variance).

**Key decisions to confirm:**
1. **B1 vs B2.** Recommend **B1 (Thompson, stdlib)** now; defer B2 until sim
   cost drops. B2 also forces relaxing the no-numpy constraint.
2. If B2: is modeling swap *interactions* worth a numpy dependency + the sim
   budget? (My read: not yet.)

**Effort:** B1 ~3–4h (Channel B, mirrors existing policies + tests). B2 is a
multi-day research build gated on the dependency + sim-cost decisions.

---

## Recommendation
Unpark FP-012 for **Slice A (intent-learning)** and **Slice B1 (Thompson
policy)** — both are tractable, stdlib-only, and reuse existing seams. Hold
**B2 (GP-BO)** parked behind the numpy + sim-cost decisions. Awaiting go-ahead
to build (per the "surface design before a big build" agreement).
