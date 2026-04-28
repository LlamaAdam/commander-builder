# Architecture — module map and data flow

> The system is 14 Python modules + 1 prompt artifact + 1 SQLite store.
> This doc shows how they fit together. Read alongside `PROJECT.md`
> (the spec/roadmap) and `STATUS.md` (current state).

## Layered view

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 4 — Phase 3 (future): learned predictor                  │
│    ml_dataset.py    (feature schema + train/eval split)         │
│    [trainer.py]     (NOT BUILT — needs 200+ iterations first)   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3 — Phase 2: closed-loop iteration                       │
│    iteration_loop.py        (orchestrator)                      │
│    analyst.py               (verdict router)                    │
│    knowledge_log.py         (SQLite history)                    │
│    [proposer.py]            (NOT BUILT — GAP-005)               │
│    prompts/moxfield_audit_v3.md  (manual proposer for now)      │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2 — Phase 1B: the testing harness                        │
│    compare_versions.py      (head-to-head A/B sim)              │
│    run_match.py             (user-deck vs pool)                 │
│    pool_curator.py          (opponent meta selection)           │
│    snapshot_deck.py         (deck versioning)                   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1 — primitives                                           │
│    forge_runner.py          (Forge headless wrapper)            │
│    log_parser.py            (sim stdout → match-level data)     │
│    game_analyzer.py         (sim stdout → per-game telemetry)   │
│    moxfield_import.py       (Moxfield API → .dck)               │
│    moxfield_push.py         (.dck → Moxfield textarea)          │
│    scryfall_client.py       (card metadata + color identity)    │
└─────────────────────────────────────────────────────────────────┘
```

The arrow is "depends on". Higher layers compose lower ones. Lower layers
never import higher.

## Module responsibility table

| Module | Owns | Doesn't own |
|--------|------|-------------|
| `forge_runner` | Spawning Forge JVM, capturing stdout/stderr, timeout enforcement, returncode | Parsing the output. That's `log_parser`. |
| `log_parser` | Match Result, Game Result, unsupported, confirmAction, active-player attribution | Per-game life curves. That's `game_analyzer`. |
| `game_analyzer` | Per-game telemetry: end_turn, winner, life curves, eliminations, draws | Match-level totals. That's `log_parser`. |
| `moxfield_import` | Pull Moxfield deck JSON, convert to Forge `.dck`, bulk harvest by bracket | Knowing what to pull. That's the user / curator. |
| `moxfield_push` | Render `.dck` as Moxfield textarea format, clipboard copy | Authentication. `_api_push` is a typed stub. |
| `scryfall_client` | Card lookups, disk cache, color identity from `.dck` commander section | Anything beyond color identity (archetype classifier is its own thing). |
| `pool_curator` | Round-robin tournament, candidate ranking, top-6 split into Pool A / B with diversity rules | Picking the candidates. That's the user / `moxfield_import`. |
| `run_match` | User deck vs curated pool (or fallback opponents), `MatchupReport` | Deck improvement decisions. That's the analyst loop. |
| `compare_versions` | Old-vs-new head-to-head A/B sim with filler-pair rotation, card-level diff | Whether the new version is "better". That's `analyst`. |
| `snapshot_deck` | File-copy `.dck` to a versioned filename, refuse-clobber semantics | What to do with the snapshot. The workflow / iteration_loop owns that. |
| `analyst` | Verdict (`kept` / `reverted` / `neutral`) on a comparison, with reasoning + lessons | Running the comparison itself. That's `compare_versions`. |
| `iteration_loop` | Wiring compare → analyst → knowledge_log into one cycle | Multi-iteration loop with automated proposer (needs `proposer.py`, GAP-005). |
| `knowledge_log` | SQLite-backed iteration history, lineage chains via `parent_id` | Querying / reporting. That's `report.py` (GAP-010). |
| `ml_dataset` | Phase 3 feature schema + extraction from `knowledge_log` rows + deck-level train/eval split | Training. No trainer until iteration count clears the documented minimum. |
| `prompts/moxfield_audit_v3.md` | The current LLM proposer (manual paste workflow) | Validation. That's `compare_versions` + `analyst`. |

## Data flow — the audit cycle

The full closed-loop cycle a user goes through to iterate one deck:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Moxfield deck (live, online)                                │
   │  ↓ moxfield_import                                           │
   │  [USER] My Deck [B3].dck                                     │
   │  ↓ snapshot_deck v1                                          │
   │  [USER] My Deck v1 [B3].dck   (frozen baseline)              │
   └──────────────────────────────────────────────────────────────┘
                                │
                                │  (paste prompts/moxfield_audit_v3.md
                                │   into a fresh Claude session)
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Audit prompt:                                               │
   │   - blind-builds an ideal from EDHREC + Moxfield refs        │
   │   - diffs against current → swap manifest                    │
   │   - executes via JS in Moxfield bulk-edit textarea           │
   │   - emits audit_manifest.json                                │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Modified Moxfield deck (live, online)                       │
   │  ↓ moxfield_import (re-pull, overwrites local file)          │
   │  ↓ snapshot_deck v2                                          │
   │  [USER] My Deck v2 [B3].dck   (post-audit)                   │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  compare_versions.compare(v1, v2, bracket=3, games=10):      │
   │    pod 1: [v1, v2, filler_a, filler_b]                       │
   │    pod 2: [v1, v2, filler_c, filler_d]                       │
   │    20 games of head-to-head signal                           │
   │  → ComparisonReport JSON in _compare/                        │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  analyst.analyze(audit_manifest, sim_report):                │
   │    → Verdict { label, confidence, reasoning, lessons }        │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  knowledge_log.record_iteration(...)                         │
   │    → row in iterations table; parent_id chains the lineage   │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
                  next iteration if verdict == "kept",
                  rollback if "reverted",
                  user decides if "neutral"
```

## Data flow — pool curation (Layer 2 standalone)

Independent pipeline; produces the canonical opponent pool used by `run_match`:

```
   moxfield_import.harvest_bracket(B=3, count=60)
       ↓
   ~60 .dck files at vendor/forge/userdata/decks/commander/*[B3].dck
       ↓
   pool_curator.curate_bracket(...):
     - preflight 1 game per candidate (reject crashes)
     - schedule_pods: round-robin, 3 pods per deck
     - run pods, aggregate wins
     - top 6 by win rate
     - split into Pool A (ranks 1/3/5) + Pool B (ranks 2/4/6)
       with archetype/color diversity check + one-shot swap
       ↓
   _pools/B3.json         (canonical Pool A + Pool B)
   _pools/B3_analysis.json (per-pod MatchAnalysis)
```

## Persistence locations

| Path | Owner | What |
|------|-------|------|
| `vendor/forge/userdata/decks/commander/*.dck` | `moxfield_import` | Imported decks (`[USER]`-prefixed for own decks) |
| `vendor/forge/userdata/decks/commander/_pools/B<n>.json` | `pool_curator` | Curated pool snapshots |
| `vendor/forge/userdata/decks/commander/_pools/B<n>_analysis.json` | `pool_curator` | Per-pod MatchAnalysis |
| `vendor/forge/userdata/decks/commander/_matches/*.json` | `run_match` | User-vs-pool MatchupReports |
| `vendor/forge/userdata/decks/commander/_compare/*.json` | `compare_versions` | A/B ComparisonReports |
| `knowledge_log.sqlite` (repo root) | `knowledge_log` | Iteration history |
| `.cache/scryfall/*.json` | `scryfall_client` | Card metadata cache |

## Backend-swap seams

Where the architecture is set up to allow swapping a backend without touching
callers:

| Seam | Default | Alternatives |
|------|---------|--------------|
| `analyst.analyze()` router | `heuristic_verdict` | `claude_verdict` (stub), `ollama_verdict` (stub) — toggled via `AnalystConfig` |
| `pool_curator.ArchetypeClassifier` | `_stub_classifier` (broken — GAP-001) | Heuristic / Claude / Ollama once GAP-001 lands |
| `forge_runner` AI | Forge built-in AI | Phase 4 (out of scope): Claude-as-pilot via decision-point hooks |
| `moxfield_push._api_push` | NotImplementedError | Authenticated API call once Moxfield exposes one |

These seams are the project's flex points. Adding a new backend should never
require changing module boundaries.

## What's NOT in this diagram (yet)

- `proposer.py` — programmatic LLM proposer (GAP-005)
- `report.py` — Markdown/HTML rendering of iteration chains (GAP-010)
- `edhrec_client.py` — automated meta-opponent discovery (GAP-009)
- `revert_to.py` — rollback automation (GAP-017)
- `commander_builder.status` — top-level health command (GAP-014)

These all land on Layer 3 or as siblings of existing modules. None require
architectural changes — just module-level additions.
