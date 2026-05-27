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
| `docs/future-plans.md` | Consolidated detailed FP plans/findings (FP-002 margin analysis + deck-gen, FP-007, FP-010). |
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

---

## Setup & verify (fresh machine)

Gets a clean clone to a working install. Requires **Python 3.10+**.

```bash
git clone https://github.com/LlamaAdam/commander-builder.git
cd commander-builder
git checkout feature/2026-04-28-session
pip install -e .[claude]          # commander-* CLIs on PATH; [claude] = anthropic SDK
# Optional extras: [web] (Flask GUI), [desktop] (pywebview + PyInstaller EXE)
```

**Credentials (one-time, outside the repo).** The curator's API-key path reads
`~/.commander-builder/credentials` (never committed). Skip if you only use the
heuristic advisor. `commander-config init`, paste the key, then `chmod 600` on
Unix. Details: [docs/SECRETS.md](SECRETS.md).

**Forge (optional, for A/B sims).** Install under `vendor/forge/` (the desktop
fat jar + `res/`); optional portable JRE under `vendor/jre/bin/`. Both are
`.gitignore`d. `commander-doctor` verifies them (falls back to system `java`).
Not needed for the web app, advisor, or test suite — only the Forge sim loop.
On a fresh box `commander-builder-bootstrap --download-forge` fetches the jar.

**Card cache (optional).** Scryfall JSON/images cache under `mtg_cards/`,
resolved by: `MTG_CARDS_DIR` env → `C:\dev\mtg_cards` → `<repo>/.cache/`. It
self-builds on first audit; set `MTG_CARDS_DIR` to share across clones.

**Verify:**
```bash
commander-doctor              # health check; non-zero on RED issues
python -m pytest -q           # fast lane
python -m pytest -q --run-slow  # full suite (incl. integration)
python -m commander_builder.web --port 5050   # then open http://127.0.0.1:5050
```

### Machine-specific data you can't get from git (`.gitignore`d)

| Path | How to recreate |
|------|-----------------|
| `~/.commander-builder/credentials` | `commander-config init` + paste key |
| `vendor/forge/` | `commander-builder-bootstrap --download-forge`, or grab a release from github.com/Card-Forge/forge/releases |
| `vendor/jre/` | optional; only if you don't want system Java 17+ on PATH |
| `mtg_cards/` (or `.cache/`) | auto-built on first audit; copy from another box to skip the cold rebuild |
| `vendor/forge/userdata/decks/commander/*.dck` | your deck library — copy from another box |
