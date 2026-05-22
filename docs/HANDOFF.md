# HANDOFF — commander-builder (the ACTUAL MTG PROGRAM)

> **You are in PROGRAM 2 of 2.** This is **commander-builder** — the
> Commander/EDH deck-building app: it audits decks, proposes improvements
> (curator), runs Forge A/B simulations, and records empirical verdicts in a
> knowledge_log. This is the *product*.
>
> **The OTHER program is the `commander-orchestrator`** — a separate
> dev-automation tool at `C:\dev\commander-orchestrator` (published:
> github.com/LlamaAdam/commander-orchestrator; see its `HANDOFF.md`) whose only
> job is to *run and auto-fix this repo's tests*. If you're thinking about
> tier-1/tier-2 fix loops, qwen routing, `orch fix`, or auto-fix branches →
> that's the orchestrator, not here.

---

## Orientation: where the truth lives

This repo already has living docs — **read these first**, this handoff just
orients + records the latest session deltas:

| Doc | What it is |
|-----|-----------|
| **`STATUS.md`** | **Source of truth.** Current state, ranked open backlog, and *Parked plans* (the FP-### catalog). Start here. |
| `CHANGELOG.md` | Per-commit history of what landed. |
| `docs/AGENT_BACKLOG.md` | Detailed task backlog (#001…). |
| `docs/future-plans-action.md` | Scoped action plans for the most actionable FPs. |
| `docs/architecture.md` | Architecture + key decisions. |

- **Branch:** `feature/2026-04-28-session` (the active line; on `origin`).
- **Run it:** `python -m commander_builder.web` (web app) · `commander-auto-curate …`
  (curator+sim pipeline) · `python -m pytest -q` (suite: ~1287 fast, +slow with `--run-slow`).
- **Key modules:** `forge_runner.py` (Forge sim wrapper + A/B harness),
  `proposer.py` / `_proposer_sim.py` / `_proposer_cli.py` (curator + auto-curate),
  `ml_dataset.py` (FP-002 features), `knowledge_log.py` (iterations DB at repo-root
  `knowledge_log.sqlite`), `web/` (Flask app, blueprint-split), `vendor/forge` (Forge 2.0.12 + JRE).

---

## ★ Future plans (FP) — snapshot for "what's the plan now?" checks

You said you'll occasionally want this checked for *changing* future plans.
The authoritative list is in **`STATUS.md` → Parked plans**; this is the
current status (✅ shipped · 🔭 parked · 🟡 active/substrate):

| FP | Title | Status |
|----|-------|--------|
| FP-001 | Python-native engine / **LLM-piloted Forge AI** | 🔭 Parked (LLM-AI variant is the high-leverage slice) |
| FP-002 | Phase-3 ML predictor (kept-vs-reverted) | 🔭 **Concluded NOT VIABLE this session** (see below) |
| FP-003 | Concurrent Forge sims | ✅ **SHIPPED this session** (`forge_runner.run_ab_batch` + `vendor/forge2`) |
| FP-004 | Deterministic Forge seed | 🔭 Parked (no `--seed` in Forge 2.0.12) |
| FP-006 | Web GUI | ✅ Shipped |
| FP-007 | Unified MTG application | 🔭 Parked (ship FP-006 fully first) |
| FP-008/009 | Card images + oracle-text store | 🟡 Substrate landed (image cache, `oracle_diff`); active backlog |
| FP-010 | Package as desktop EXE | 🔭 Parked (gate: ≥5 browser-only audits; deep-path fragility) |
| FP-011 | BYO LLM token | 🔭 Parked (secret-scan hook exists; web config GET/PUT still TODO) |
| FP-012 | Autonomous deck-improvement agent | 🔭 Parked (north star; gated on data + concurrent sims) |
| FP-013 | Project-tuned LLM (LoRA) | 🔭 Parked, do-not-promote (needs 2000+ rows) |

To check for *changes*: skim `STATUS.md` Parked-plans + recent `CHANGELOG.md`
entries, and diff this table against them.

---

## This session's key deltas (2026-05-21/22)

1. **FP-003 SHIPPED** — `run_ab_batch(jobs, runners)` runs A/B sims across
   cwd-isolated Forge profiles in parallel (≈2× throughput); `vendor/forge2`
   is recreatable via `scripts/setup_forge_profile.py`.

2. **A/B SIM WIN-ATTRIBUTION BUG — fixed (commit `e8777b6`).** `run_ab_simulation`
   credited wins by deck *name*, but deck A and deck B routinely share the same
   internal `Name=` (a curated deck keeps its parent's; a detuned deck keeps the
   original's) → Forge emitted identical seat tokens → wins funnelled to one side.
   **Fix: attribute by SEAT.** Consequence: the prior `knowledge_log` FP-002
   labels (78 kept / 153 reverted) are **measurement artifacts** — use
   `--min-id 314` to train only on post-fix rows.

3. **FP-002 concluded NOT VIABLE via this pipeline.** With correct attribution,
   the curator's swaps almost never make a deck *worse* than its input (verified
   across detune depths 0–10 → 11 kept / 3 neutral / **0 reverted**). So the
   kept-vs-reverted classifier has no negative class to learn. A future FP-002
   would need a different framing (e.g. regress on improvement margin), not more
   sim hours. (Pre-fix rows left in the DB as archive; never deleted.)

---

## Relationship to the orchestrator (keep them straight)

- The orchestrator **runs this repo's pytest and auto-fixes failures** on
  local-only `auto-fix/*` branches. It never pushes.
- If you see throwaway `dogfood/*` or `auto-fix/*` branches, or `flask`
  uninstalled, that's the orchestrator dogfooding — restore with
  `git checkout feature/2026-04-28-session` and `pip install flask`.
- FP-002 data-gen scripts (`scripts/detune_deck.py` here; the generators +
  `train_fp002.py` live in the orchestrator) are part of the (now-concluded)
  FP-002 effort.
