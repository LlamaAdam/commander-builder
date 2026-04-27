# Commander Builder

Closed-loop MTG Commander deck improvement. Forge headless simulation
empirically validates whether LLM-proposed swaps actually improve win rate;
a SQLite knowledge log accumulates iterations so future runs learn from the
past.

**Source-of-truth docs**: [PROJECT.md](PROJECT.md) (spec & roadmap),
[STATUS.md](STATUS.md) (current state), [BACKLOG.md](BACKLOG.md) (work queue),
[docs/architecture.md](docs/architecture.md) (module map),
[docs/audit_workflow.md](docs/audit_workflow.md) (the user-facing pipeline),
[CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md).

## Status (2026-04-26)

- **Phase 1A — Forge verifier**: ✅ complete
- **Phase 1B — Forge orchestrator pipeline**: ✅ complete
- **Phase 2 — LLM analyst + iteration loop**: scaffolding complete; programmatic
  proposer (`proposer.py`) and Claude/Ollama verdict backends pending —
  see `BACKLOG.md` GAP-005 / GAP-007.
- **Phase 3 — Learned predictor**: deferred until 200+ iterations are logged.
  Feature schema and dataset extraction in `ml_dataset.py` ready to feed it.

14 modules, 144 tests passing in ~0.6s. Live smoke and integration tests on
real B3 decks all passing.

## Setup

```bash
git clone <this repo>
cd commander_builder
python -m pip install -e ".[dev]"
```

After this, every CLI works without `PYTHONPATH=src`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the full dev setup.

For live Forge sims, drop a portable Forge release + JRE into `vendor/` (see
[vendor/README.md](vendor/README.md)). The system runs without it — only
modules that hit Forge subprocess (`forge_runner`, `pool_curator`,
`run_match`, `compare_versions`, `iteration_loop`) need it.

## The user-facing pipeline (audit one deck)

The full closed-loop iteration cycle. See
[docs/audit_workflow.md](docs/audit_workflow.md) for the diagram.

```bash
# 1. Import a Moxfield deck as your "version 1" baseline
commander-import --user https://moxfield.com/decks/<id>

# 2. Snapshot v1 (frozen baseline)
commander-snapshot "[USER] My Deck [B3].dck" --version v1

# 3. Open a Claude session, paste prompts/moxfield_audit_v3.md.
#    The audit will modify your Moxfield deck and emit audit_manifest.json.

# 4. Re-pull the post-audit deck (overwrites the local file)
commander-import --user https://moxfield.com/decks/<id>

# 5. Snapshot v2
commander-snapshot "[USER] My Deck [B3].dck" --version v2

# 6. Run head-to-head A/B
commander-compare \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --games 10 --filler-pairs 2

# 7. Wrap as one iteration with verdict + persistence
commander-iterate \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 \
    --manifest audit_manifest.json
```

The `commander-iterate` step writes to `knowledge_log.sqlite` so future
iterations chain via `parent_id`.

## Other useful commands

```bash
# Bulk-harvest decks at a bracket for the curator's candidate pool
commander-import --harvest 3      # ~60 B3 decks via the multi-axis recipe

# Curate the canonical opponent pool from candidates on disk
commander-curate --bracket 3 --max-candidates 12 --seed 0

# Run a user deck against the curated pool
commander-match --user "[USER] My Deck [B3].dck" --bracket 3 --games 5 --pods 3

# Push a local .dck back to Moxfield via clipboard
commander-push "[USER] My Deck v2 [B3].dck"
```

## Phase 1A: run the verifier (one-time, machine-specific)

```bash
python -m commander_builder.verify_forge
```

Locates Java + Forge, runs 2-player constructed + 4-player commander sims,
writes `verify_output/findings.json`. Useful for one-time machine
verification or when troubleshooting a Forge install.

## Project layout

```
src/commander_builder/   14 production modules
tests/                   144 unit tests, all offline (~0.6s)
scripts/                 integration tests + batch runners (hit Forge)
prompts/                 versioned LLM workflow prompts
docs/                    architecture, audit workflow, decision logs
vendor/                  Forge install (gitignored)
```

See [docs/architecture.md](docs/architecture.md) for the module map and
data-flow diagrams.

## Where to start when picking this up cold

1. `PROJECT.md` — the spec
2. `STATUS.md` — what we're working on right now
3. `BACKLOG.md` — what's queued
4. `docs/architecture.md` — how the pieces fit
5. Run `python -m pytest tests/` to confirm the suite is green

Then either pick a `GAP-NNN` from `BACKLOG.md` Tier 1 or read the most recent
`HANDOFF_*.md` file for narrative context.
