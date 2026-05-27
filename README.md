# Commander Builder

Closed-loop MTG Commander deck improvement. Forge headless simulation
empirically validates whether LLM-proposed swaps actually improve win rate;
a SQLite knowledge log accumulates iterations so future runs learn from
the past.

The primary use case: *"I have a Commander deck. Make it better, prove
it's better, and learn what kinds of changes actually move the needle so
future audits get smarter."*

It is **not** a deck builder from scratch, not a Moxfield clone, not a
real-time game client. It's an iteration engine where Forge provides
ground-truth simulation and an LLM (Claude or local Ollama) acts as the
analyst that reads sim deltas and decides what to try next.

**Source-of-truth docs:**
- [STATUS.md](docs/STATUS.md) — current state, open backlog, parked plans
- [CHANGELOG.md](docs/CHANGELOG.md) — what landed, in reverse chronological order
- [docs/architecture.md](docs/architecture.md) — module map, data flow,
  conventions, working principles

## Setup

```bash
git clone <this repo>
cd commander_builder
python -m pip install -e ".[dev]"
```

After this, every CLI entry point works without `PYTHONPATH=src`.

For live Forge sims, drop a portable Forge release + JRE into
`vendor/forge/` and `vendor/jre/` (see `setup/forge/README.md`). The
system runs without Forge — only modules that hit the JVM
(`forge_runner`, `pool_curator`, `run_match`, `compare_versions`,
`iteration_loop`) need it.

For live LLM analyst, configure `ANTHROPIC_API_KEY` via one of:

- `commander-config init` → edit `~/.commander-builder/credentials`
  (the credentials file lives **outside the repo** so it can never be
  committed by accident). See [docs/SECRETS.md](docs/SECRETS.md).
- Or set the env var directly in your shell (overrides the file).
- Or provide a key through the web UI's BYO-key flow (per-request,
  never persisted server-side).

## Run the web app

```bash
python -m commander_builder.web
# → http://127.0.0.1:5000
```

Sidebar deck list + dashboard with hero / stat tiles / mana curve /
categories / suggested adds. Propose-swap drives A/B sims through the
parallel-pod harness; "Save iteration" persists to
`knowledge_log.sqlite`. The Claude analyst is opt-in per request via the
LLM toggle row.

## CLI commands

```bash
# Import a Moxfield deck as your baseline
commander-import --user https://moxfield.com/decks/<id>

# Snapshot a version (frozen baseline)
commander-snapshot "[USER] My Deck [B3].dck" --version v1

# Heuristic/Claude swap recommendations (no browser session needed)
commander-advise --user "[USER] My Deck v1 [B3].dck" --bracket 3

# End-to-end auto-curate: advisor -> Claude curator -> apply -> optional
# A/B sim with empirical kept/reverted/neutral verdict written back to
# the knowledge_log. ~$0.20-$0.50 in Anthropic + ~5-15 min Forge per run.
commander-auto-curate "[USER] My Deck [B3].dck" --bracket 3 --run-sim
# Polish (default, 5+5 swaps), overhaul (15+15), or free (unbounded).
commander-auto-curate "[USER] My Deck [B3].dck" --bracket 3 --mode overhaul

# Old-vs-new head-to-head A/B sim
commander-compare \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --games 10 --filler-pairs 2

# Wrap as one iteration with verdict + persistence
commander-iterate \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --manifest audit_manifest.json

# Bulk-harvest decks at a bracket for the curator's candidate pool
commander-import --harvest 3      # ~60 B3 decks via the multi-axis recipe

# Curate the canonical opponent pool from candidates on disk
commander-curate --bracket 3 --max-candidates 12 --seed 0

# Run a user deck against the curated pool
commander-match --user "[USER] My Deck [B3].dck" --bracket 3 --games 5 --pods 3

# Push a local .dck back to Moxfield via clipboard
commander-push "[USER] My Deck v2 [B3].dck"

# Compare your deck to consensus meta-references at a bracket
commander-meta-test "[USER] My Deck [B3].dck" --bracket 3

# Inspect or revert any historical iteration
commander-history --deck-id <publicId>
commander-revert --to-deck <publicId> --version 3

# Health-check Forge install + caches
commander-doctor

# Status snapshot for cold pickup
commander-status
```

## The audit cycle (manual workflow)

The full closed-loop iteration cycle when you want maximum control. See
[docs/architecture.md](docs/architecture.md) for the data-flow diagram.

```bash
# 1. Import a Moxfield deck as your "version 1" baseline
commander-import --user https://moxfield.com/decks/<id>

# 2. Snapshot v1 (frozen baseline)
commander-snapshot "[USER] My Deck [B3].dck" --version v1

# 3. Either:
#    (a) Run the web app's audit flow, OR
#    (b) Open a Claude session, paste prompts/moxfield_audit_v3.md.
#        The audit modifies your Moxfield deck and emits audit_manifest.json.

# 4. Re-pull the post-audit deck (overwrites the local file)
commander-import --user https://moxfield.com/decks/<id>

# 5. Snapshot v2 and run head-to-head A/B (see commands above)
```

The web app collapses steps 3–5 into a single propose-swap flow.

## Project layout

```
src/commander_builder/   ~30 production modules; key subsystems split:
  improvement_advisor.py  orchestrator (advise + _advise_steps generator)
  _advisor_*.py          7 sub-modules: models, heuristic, bracket_peers,
                         claude, manabase, filters, role_helpers
  web/
    app.py               Flask orchestrator (registers 5 blueprints)
    _helpers.py          pure functions (deck format, evidence scoring)
    routes_audit.py      /api/audit + /api/audit/stream (SSE) + /api/advise
    routes_sim.py        /api/propose_swap + iteration CRUD
    routes_decks.py      deck text/source/import + game_changers + deck_audit
    routes_dashboard.py  /api/dashboard + pricing + verdict breakdown
    routes_meta.py       root + health + forge_version + log_error
tests/                   820 unit tests, all offline (~37s)
scripts/                 integration tests + batch runners (hit Forge)
prompts/                 versioned LLM workflow prompts
docs/                    architecture, current handoff, sprint specs
vendor/                  Forge install + JRE (gitignored)
```

Companion repo at `C:\dev\forge_py\` — Python-native simulator that
emits Forge-compatible stdout. Used as a fast pre-filter for ranking
decks. Optional correlation harness in `forge_py_correlation.py` runs
both engines side-by-side; opt in via
`COMMANDER_BUILDER_CORRELATE_FORGE_PY=1`.

Shared card data at `C:\dev\mtg_cards\` (out-of-repo, ~180MB Scryfall
bulk + per-card snapshots + Magic Comp Rules). Both projects read via
`MTG_CARDS_DIR` env var with a sensible default.

## Where to start when picking this up cold

1. `docs/STATUS.md` — current state, open backlog, parked plans
2. `docs/architecture.md` — how the pieces fit
3. `python -m pytest tests/` — confirm the suite is green
4. `git log --oneline -10` — what landed most recently

Then either pick an item from STATUS's open backlog or jump into the
web app and run a propose-swap end-to-end on one of your decks.

## Working principles

These are how the project expects sessions to operate. They live in full
in [docs/architecture.md](docs/architecture.md#working-principles); the
short version:

1. **Verify before assuming.** Wrong assumptions wrapped in try/except
   rot quietly.
2. **Honest pushback over compliant building.** Say so if the spec
   doesn't make sense.
3. **Small, validated steps.** Each component built, tested, integrated
   before the next.
4. **Modularity over cleverness.** Clean interfaces > clever
   inheritance; backends swap at known seams.
5. **No silent failures.** Forge can fail many ways; surface errors
   loudly with actionable messages.
6. **Log everything that could become training data.** Phase 3 ML wants
   structured logs from day one.

## License

`pyproject.toml` reads `license = "TBD"`. Personal-use repo today;
adopt MIT (or similar) if the project ever goes public.
