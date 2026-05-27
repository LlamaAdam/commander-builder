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

This repo already has living docs — **read these first**; this handoff
orients you + covers fresh-machine setup:

| Doc | What it is |
|-----|-----------|
| **[docs/STATUS.md](STATUS.md)** | **Source of truth.** Current state, ranked open backlog, and *Parked plans* (the FP-### catalog). Start here. |
| [docs/CHANGELOG.md](CHANGELOG.md) | Per-commit history of what landed. |
| `docs/future-plans.md` | Consolidated detailed FP plans/findings (FP-002 margin analysis + deck-gen, FP-007, FP-010). |
| `docs/PROJECT-MANAGER.md` | **PM agent prompt + routing playbook** — triage work to the orchestrator, an isolated local worker, or escalate. Use to drive the project at scale. |
| `docs/orch-worklist.md` | The live orchestrator red-test queue + workflow. |
| `docs/architecture.md` | Architecture + key decisions. |

- **Branch:** `feature/2026-04-28-session` (the active line; on `origin`).
- **Run it:** `python -m commander_builder.web` (web app) · `commander-auto-curate …`
  (curator+sim pipeline) · `python -m pytest -q` (suite: ~1472 fast, +slow with `--run-slow`).
- **Key modules:** `forge_runner.py` (Forge sim wrapper + A/B harness),
  `proposer.py` / `_proposer_sim.py` / `_proposer_cli.py` (curator + auto-curate),
  `ml_dataset.py` (FP-002 features), `knowledge_log.py` (iterations DB at repo-root
  `knowledge_log.sqlite`), `web/` (Flask app, blueprint-split), `vendor/forge` (Forge 2.0.12 + JRE).

---

## Future plans

The FP-### catalog + current status lives in **[STATUS.md](STATUS.md) -> Parked
plans**; the detailed per-FP plans/findings are in
**[future-plans.md](future-plans.md)**. To check for changes, skim those two
plus recent [CHANGELOG.md](CHANGELOG.md).

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
