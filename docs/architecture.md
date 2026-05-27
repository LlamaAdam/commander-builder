# Architecture, conventions, and working principles

> Single technical reference for the project: module map, data flow,
> persistence, coding conventions, and the decisions that shaped them.
> [STATUS.md](STATUS.md) tracks operational state; [CHANGELOG.md](CHANGELOG.md)
> records what landed.

---

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
│    improvement_advisor.py   (orchestrator: routes to 7 sources) │
│      ├─ _advisor_models.py  (shared dataclasses)                │
│      ├─ _advisor_heuristic.py    (EDHREC-based)               │
│      ├─ _advisor_bracket_peers.py (Moxfield peer refs)        │
│      ├─ _advisor_claude.py       (LLM backend)                │
│      ├─ _advisor_filters.py      (validation + saturation)    │
│      ├─ _advisor_manabase.py     (curated essentials)         │
│      └─ _advisor_role_helpers.py (role classifier wrapper)    │
│    proposer.py              (programmatic LLM proposer)         │
│    knowledge_log.py         (SQLite history)                    │
│    report.py                (markdown reports of iteration chains)│
│    revert_to.py             (rollback automation)               │
│    export.py                (knowledge log JSON dump/restore)   │
│    prompts/moxfield_audit_v3.md  (manual proposer prompt)       │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 2 — Phase 1B: the testing harness                        │
│    compare_versions.py      (head-to-head A/B sim;              │
│                              parallel pods + early-stop +       │
│                              intra-pod abort)                   │
│    run_match.py             (user-deck vs pool)                 │
│    pool_curator.py          (opponent meta selection)           │
│    snapshot_deck.py         (deck versioning)                   │
│    meta_test.py             (consensus reference benchmark)     │
│    game_changers.py         (WotC Game Changers fetch + cache)  │
│    archetype.py             (deck classifier)                   │
│    staples.py               (universal staples + role classifier)│
│    forge_py_correlation.py  (optional forge_py↔Forge harness)   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1 — primitives                                           │
│    forge_runner.py          (Forge headless wrapper + version)  │
│    log_parser.py            (sim stdout → match-level data)     │
│    game_analyzer.py         (sim stdout → per-game telemetry)   │
│    moxfield_import.py       (Moxfield API → .dck)               │
│    moxfield_push.py         (.dck → Moxfield textarea)          │
│    scryfall_client.py       (card metadata + color identity)    │
│    edhrec_client.py         (EDHREC pages + retry-with-backoff) │
│    deck_dashboard.py        (stat tiles, mana curve, categories)│
│    doctor.py                (environment health checks)         │
│    status.py                (deck-set/pool/log snapshot)        │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 0 — web surface                                          │
│    web/app.py               (Flask app orchestrator)            │
│      ├─ _helpers.py         (pure Flask-independent helpers)   │
│      ├─ routes_audit.py     (audit + advise endpoints)         │
│      ├─ routes_sim.py       (propose-swap + iteration CRUD)    │
│      ├─ routes_decks.py     (deck CRUD + import + GC)          │
│      ├─ routes_dashboard.py (dashboard aggregation)            │
│      └─ routes_meta.py      (health, version, error sink)      │
│    web/static/app.js        (UI + error collector)              │
│    web/static/app.css       (theme)                             │
└─────────────────────────────────────────────────────────────────┘
```

The arrow is "depends on". Higher layers compose lower ones. Lower
layers never import higher.

## Module responsibility table

| Module | Owns | Doesn't own |
|--------|------|-------------|
| `forge_runner` | Spawn Forge JVM, capture stdout/stderr/returncode, timeout enforcement, streaming + per-line abort_check, jar-version detection | Parsing the output. That's `log_parser`. |
| `log_parser` | `Match Result`, `Game Result`, unsupported-card flags, active-player attribution | Per-game life curves. That's `game_analyzer`. |
| `game_analyzer` | Per-game telemetry: end_turn, winner, life curves, eliminations, draws | Match-level totals. That's `log_parser`. |
| `moxfield_import` | Pull Moxfield deck JSON, convert to Forge `.dck`, bulk harvest by bracket | Knowing what to pull. The user/curator picks. |
| `moxfield_push` | Render `.dck` as Moxfield textarea format (pipe→parens), clipboard copy | Authentication. `_api_push` is a typed stub (won't-do). |
| `scryfall_client` | Card lookups, disk cache, color identity, forced refresh | Anything beyond card metadata (archetype is its own thing). |
| `edhrec_client` | EDHREC commander page + average-deck fetch, schema-tolerant `__NEXT_DATA__` walk, retry-with-backoff (5xx/429/URLError, `Retry-After` honored, capped at 30 s) | What to do with the data. Heuristic advisor + meta-test consume. |
| `staples` | `UNIVERSAL_STAPLES_LC`, `BASIC_LANDS_LC`, `classify_role_extended` (canonical), frequency labels, confidence tiers, role saturation thresholds, manabase essentials, tribal essentials | Recommendation logic. Advisors use these. |
| `archetype` | Heuristic deck-classifier (filename hint → keyword scan → midrange fallback) | LLM escalation. Stubs exist for Claude/Ollama. |
| `game_changers` | WotC Game Changers list (HTML scrape, 7-day cache, bundled fallback) | Bracket-fitting. The advisor + dashboard consume. |
| `pool_curator` | Round-robin tournament, candidate ranking, top-6 split with archetype/color diversity, persisted pool JSON | Picking candidates. That's the user / `moxfield_import`. |
| `run_match` | User deck vs curated pool (or fallback opponents), `MatchupReport` | Improvement decisions. That's the analyst loop. |
| `compare_versions` | Old-vs-new head-to-head A/B sim; parallel pod dispatch; adaptive early-stop; intra-pod abort; card-level diff | Whether the new version is "better". That's `analyst`. |
| `snapshot_deck` | File-copy `.dck` to versioned filename; refuse-clobber semantics | What to do with the snapshot. Workflow / iteration_loop owns. |
| `meta_test` | Pull top-likes Moxfield + EDHREC Average Deck for a commander; compare-versus-references; must-add / consider / off-meta | Acting on the recommendations. The user does. |
| `improvement_advisor` (orchestrator) | Dispatch to multi-source recommenders; `advise()` entry point; `_advise_steps()` streaming generator; name validation + pricing snapshot | Running the sim. That's `compare_versions`. |
| `_advisor_models` | `DeckDiagnosis`, `SwapRecommendation`, `AdviceReport`, `AdvicePhase` dataclasses | Serialization schema. JSON mapping is implicit. |
| `_advisor_heuristic` | EDHREC inclusion%/synergy recommender (`_heuristic_swap_recommendations`) | Other sources. Multi-source dispatch is `improvement_advisor`. |
| `_advisor_bracket_peers` | Top-N Moxfield peer recommender (`_bracket_peers_recommendations`) | Heuristic fallback. EDHREC handles that. |
| `_advisor_claude` | LLM advisor (`_claude_swap_recommendations`) via anthropic SDK | Other backends. Router is `improvement_advisor`. |
| `_advisor_filters` | Card-name validator + saturation guard (`_filter_for_saturation`, `_validate_card_names`) | Recommendation logic. Called post-advice. |
| `_advisor_manabase` | Curated manabase essentials (`_missing_manabase_recommendations`) | Role-based adds. That's other advisor paths. |
| `_advisor_role_helpers` | Thin role-classifier wrapper for advisor use | Core classification. That's `staples.classify_role_extended`. |
| `analyst` | Verdict (`kept` / `reverted` / `neutral`) with confidence + reasoning + lessons | Running the comparison itself. |
| `proposer` (orchestrator) | Router for manual / Claude / Ollama proposers; the `Proposal` dataclass; `auto_propose()` curator pipeline; `apply_proposal_to_deck`; `_extract_curator_json` | Validating proposals. `compare_versions` + `analyst` do. |
| `_proposer_filters` | Post-response curator filters: `enforce_bracket_caps` (game-changers stripped at B1/B2), `enforce_color_identity` (off-color adds rejected via Scryfall CI), `_load_game_changers` | Recommendation logic. The advisor / curator generate; filters reject. |
| `_proposer_sim` | Forge A/B sim glue: `_verdict_from_ab` (margin → kept/reverted/neutral), `_ab_to_iteration_fields`, bracket-aware `_pick_filler_decks`, `_run_sim_and_record`, `_log_auto_curate_iteration` | Running the sim itself. `forge_runner` + `compare_versions` do. |
| `_proposer_cli` | `auto_curate_main` (the `commander-auto-curate` console_script) — argparse + end-to-end orchestration of advisor → curator → apply → sim | Pipeline stages themselves; lives here only as a thin wrapper. |
| `_card_list_refresh` | Hardcoded-list staleness diff helpers (`diff_card_lists`, `parse_mdfc_lands_from_response`, `fetch_mdfc_lands`); used by `scripts/refresh_card_lists.py` | Mutating `deck_health`'s lists. Manual review only. |
| `iteration_loop` | Wiring compare → analyst → knowledge_log; `propose_then_iterate()` | Multi-iteration loop (FP-012 territory). |
| `knowledge_log` | SQLite-backed iteration history; lineage chains via `parent_id`; legacy deck_id migration | Reporting. `report.py` does. |
| `report` | Markdown rendering of one deck's iteration lineage; cross-deck recent-iterations summary | Mutating the log. Read-only. |
| `revert_to` | Restore deck to a previous iteration's snapshot blob; emits Moxfield push blob | Push step. User pastes. |
| `export` | JSON dump/restore of knowledge_log (full / per-deck / recent-N filter); skip-existing semantics | Schema validation. Trusts the dump. |
| `ml_dataset` | Phase 3 feature schema (25 cols) + extraction + deck-level train/eval split | Training. No trainer until 200+ iterations. |
| `doctor` | 10 environment checks; GREEN/YELLOW/RED status; `--json` output | Fixing problems. Reports only. |
| `status` | Decks-per-bracket, curated pools, recent reports, knowledge_log stats | The work itself. Pure observation. |
| `deck_dashboard` | Stat tiles, mana curve, categories, theme tags (incl. tribal type), suggested adds, est. price, inferred bracket | Mutation. The web app's audit endpoint does. |
| `forge_py_correlation` | Paired-verdict logging (Forge vs forge_py); CSV append; agreement-rate summary | Driving forge_py. Imported lazily; opt-in via env var. |
| `web/app.py` (orchestrator) | Flask app creation; blueprint registration; `create_app()` entry point; stale file cleanup; deck listing; path resolution | Business logic. Blueprints call into the layers above. |
| `web/_helpers.py` | Pure Flask-independent helpers (`_apply_swaps_to_dck`, `_normalize_pasted_deck`, `_format_added_line`, etc.); `_BASIC_LANDS` constant | Route-specific logic. Each blueprint uses as needed. |
| `web/routes_audit.py` | Audit + streaming (`GET /api/audit`, `GET /api/audit/stream`, `GET /api/advise`); wires `improvement_advisor` | Other route groups. Each lives in its own blueprint. |
| `web/routes_sim.py` | Propose-swap + iteration CRUD (`POST /api/propose_swap`, `POST /api/save_iteration`, `GET /api/iteration/<id>`, comparisons, snapshots) | Other endpoints. Organized by business domain. |
| `web/routes_decks.py` | Deck CRUD + import/GC (`GET/PUT/DELETE /api/deck_text`, `POST /api/import_deck`, `GET/PUT /api/deck_source`, manabase verification, audit) | Other routes. Grouped by deck lifecycle. |
| `web/routes_dashboard.py` | Dashboard data (`GET /api/decks`, `/api/dashboard`, `/api/iterations`, `/api/pricing_series`, `/api/verdict_breakdown`) | Audit/sim routes. Dashboard-specific aggregation. |
| `web/routes_meta.py` | Meta/utility routes (`GET /`, `/api/health`, `/api/forge_version`, `/api/correlation_summary`, `POST /api/log_error`) | Business routes. Ops + topbar concerns. |
| `prompts/moxfield_audit_v3.md` | Current LLM proposer (manual paste workflow) + audit_manifest.json writeback JS | Validation. `compare_versions` + `analyst` do. |

---

## Data flow — the audit cycle

The full closed-loop cycle the user drives to iterate one deck. Both
the CLI workflow and the web app collapse to this shape.

```
   ┌──────────────────────────────────────────────────────────────┐
   │  Moxfield deck (live, online)                                │
   │  ↓ moxfield_import                                           │
   │  [USER] My Deck [B3].dck                                     │
   │  ↓ snapshot_deck v1                                          │
   │  [USER] My Deck v1 [B3].dck   (frozen baseline)              │
   └──────────────────────────────────────────────────────────────┘
                                │
                                │  Path A (manual): paste
                                │     prompts/moxfield_audit_v3.md
                                │     into a fresh Claude session
                                │  Path B (web app): "Run audit"
                                │     button calls /api/audit, which
                                │     dispatches to improvement_advisor
                                │     (heuristic default; ?llm=claude
                                │     opts in to BYO key)
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Audit (Path A — manual Claude session):                     │
   │   - blind-builds an ideal from EDHREC + Moxfield refs        │
   │   - diffs against current → swap manifest                    │
   │   - executes via JS in Moxfield bulk-edit textarea           │
   │   - emits audit_manifest.json                                │
   │                                                              │
   │  Audit (Path B — improvement_advisor):                       │
   │   - pulls EDHREC inclusion%/synergy via edhrec_client        │
   │   - reads prior match history from _matches/                 │
   │   - heuristic or Claude analyst synthesizes swap proposal    │
   │   - validates each card name against Scryfall (hallucination │
   │     defense; flags name_known=False)                         │
   │   - returns proposed_text (full .dck) + diff payload         │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Modified deck (Path A: re-pulled from Moxfield;             │
   │                 Path B: staged via /api/propose_swap)        │
   │  ↓ snapshot_deck v2                                          │
   │  [USER] My Deck v2 [B3].dck                                  │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  compare_versions.compare(v1, v2, bracket=3, games=5):       │
   │    pod 1: [v1, v2, filler_a, filler_b]                       │
   │    pod 2: [v1, v2, filler_c, filler_d]                       │
   │    ...                                                       │
   │    Pods dispatched in parallel (ThreadPoolExecutor).         │
   │    Adaptive early-stop: cancels queued pods when verdict     │
   │      is decisive (|margin| > games_remaining).               │
   │    Intra-pod abort: per-line callback kills the JVM as soon  │
   │      as the in-pod margin exceeds games-left.                │
   │  → ComparisonReport JSON in _compare/                        │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  analyst.analyze(audit_manifest, sim_report):                │
   │    → Verdict { label, confidence, reasoning, lessons }       │
   │    heuristic_verdict default; claude_verdict / ollama_verdict│
   │    available with API key / running daemon.                  │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  knowledge_log.record_iteration(...)                         │
   │    → row in iterations table                                 │
   │    → parent_id chains the lineage                            │
   │    → pricing snapshot in audit_manifest.pricing              │
   └──────────────────────────────────────────────────────────────┘
                                │
                                ▼
                  next iteration if verdict == "kept",
                  rollback via revert_to if "reverted",
                  user decides if "neutral".
```

### Audit manifest contract

`prompts/moxfield_audit_v3.md` (Step 6 / Closing Summary) writes
`audit_manifest.json` to the audit session's working directory.
`improvement_advisor.to_manifest()` emits the same shape. Schema:

```json
{
  "deck_id": "abc123XYZ",
  "deck_name": "My Deck",
  "bracket": 3,
  "audit_version": "v3",
  "audit_timestamp": "2026-04-26T15:30:00Z",
  "added": ["Card A", "Card B"],
  "removed": ["Card X", "Card Y"],
  "rationale": "One-paragraph summary of strategic intent.",
  "pricing": {
    "total_price_usd": 142.37,
    "captured_at": "2026-05-13T20:04:00+00:00"
  },
  "step_4_5_sweep_catches": ["Card Z"],
  "auto_bracket_after": 3,
  "user_bracket": 3
}
```

`compare_versions` computes its own card diff from the .dck files; the
manifest is **provenance** (which audit produced this swap?) + feeds
the Phase 2 knowledge log + the future Phase 3 ML feature set.

---

## Data flow — pool curation (Layer 2 standalone)

Independent pipeline; produces the canonical opponent pool used by
`run_match` and `compare_versions` filler decks.

```
   moxfield_import.harvest_bracket(B=3, count=60)
       ↓
   ~60 .dck files at vendor/forge/userdata/decks/commander/*[B3].dck
       ↓
   pool_curator.curate_bracket(...):
     - preflight 1 game per candidate (reject crashes /
       unsupported-card hits)
     - schedule_pods: round-robin, ~3 pods per deck
     - run pods, aggregate wins
     - top 6 by win rate
     - split into Pool A (ranks 1/3/5) + Pool B (ranks 2/4/6)
       with archetype/color diversity check + bounded swap search
       ↓
   _pools/B3.json         (canonical Pool A + Pool B)
   _pools/B3_analysis.json (per-pod MatchAnalysis)
```

Refresh trigger: user runs `commander-curate --recurate` or the cached
pool is older than 30 days. Curation wall-time: ~35 min for B3, ~55
min for B5 (cEDH games are slower). One-time cost per bracket per
refresh.

---

## 2026-05-13 refactors: modular advisor + web blueprints

### Advisor module split (1,267-line orchestrator → 7 focused modules)

The `improvement_advisor.py` orchestrator now routes swap-advice requests
to per-source recommenders. This keeps each strategy (EDHREC heuristic,
Moxfield peer ranking, Claude LLM) independent and testable.

**Advisor module structure** (all import-safe; no circular deps):

```
improvement_advisor.py (orchestrator)
├── advise()                           # Public entry point
├── _advise_steps()                    # Streaming generator for SSE
├── _aggregate_match_history()         # Read prior performance data
├── main() + CLI                       # Entry point: commander-advise

_advisor_models.py (dataclasses)
├── DeckDiagnosis                      # Aggregated match history + signals
├── SwapRecommendation                 # One source's proposal (adds/removes)
├── AdviceReport                       # Final merged recommendation
└── AdvicePhase                        # Streaming event (for SSE)

_advisor_heuristic.py
└── _heuristic_swap_recommendations()  # EDHREC inclusion%/synergy

_advisor_bracket_peers.py
└── _bracket_peers_recommendations()   # Top-N Moxfield peer refs

_advisor_claude.py
└── _claude_swap_recommendations()     # LLM-backed advisory

_advisor_filters.py
├── _validate_card_names()             # Hallucination defense
└── _filter_for_saturation()           # Role-count guards

_advisor_manabase.py
└── _missing_manabase_recommendations() # Curated essentials

_advisor_role_helpers.py
└── _role_for_card()                   # Thin wrapper over staples
```

**Data flow**: `advise()` calls `_aggregate_match_history()` to read the
deck's past performance, then dispatches to one of three sources
(heuristic/bracket_peers/claude) based on the `source=` parameter.
Each returns a `SwapRecommendation`. Filters run post-advice, then the
final `AdviceReport` is assembled. The `_advise_steps()` generator yields
`AdvicePhase` events for streaming endpoints (`GET /api/audit/stream`).

### Web module split (2,368-line routes file → 5 blueprints + helpers)

The Flask route handlers now live in per-group blueprints, each built via
a `make_<group>_blueprint(...)` factory function that closes over the
necessary state (deck_dir, knowledge_db, helper callbacks).

**Web module structure**:

```
web/app.py (orchestrator)
├── create_app(deck_dir, knowledge_db)  # Builds Flask app + registers 5 blueprints
├── _cleanup_stale_staged_files()       # Sweep transient *.dck files
├── _list_decks()                       # Enumerate [USER] decks
└── _resolve_deck_path()                # Validate path against deck_dir

web/_helpers.py (pure, Flask-independent)
├── _apply_swaps_to_dck()               # Apply swap manifest to deck text
├── _normalize_pasted_deck()            # Canonicalize deck format
├── _format_added_line()                # Render added card for output
├── _iteration_to_dict()                # Serialize iteration row to JSON
├── _match_pct_from_evidence()          # Win/draw rate from records
├── _pad_main_to_99()                   # Normalize deck to 99 cards
├── _to_constructed_format()            # 1v1 / Constructed format
└── _BASIC_LANDS constant               # Lands list

web/routes_audit.py (blueprint: audit + advise)
├── make_audit_blueprint(deck_dir, resolve_deck_path)
├── GET  /api/audit                     # Heuristic/peer/Claude advisor
├── GET  /api/audit/stream              # SSE: streaming AdvicePhase events
└── GET  /api/advise                    # Alias for /api/audit (deprecated)

web/routes_sim.py (blueprint: propose-swap + iteration CRUD)
├── make_sim_blueprint(deck_dir, knowledge_db, resolve_deck_path)
├── POST /api/propose_swap              # Stage A/B sim, return diffs
├── POST /api/save_iteration            # Persist row to knowledge_log
├── GET  /api/iteration/<id>            # Fetch one iteration
├── GET  /api/compare/<old_id>/<new_id> # Comparison details
└── GET  /api/iteration/<id>/snapshot   # Deck text at that iteration

web/routes_decks.py (blueprint: deck CRUD + import + GC)
├── make_decks_blueprint(deck_dir, resolve_deck_path)
├── GET    /api/deck_text               # Read .dck file
├── PUT    /api/deck_text               # Write .dck file
├── DELETE /api/deck_text               # Remove .dck file
├── POST   /api/import_deck             # Moxfield URL → .dck
├── GET    /api/deck_source             # Moxfield publicId from .dck
├── PUT    /api/deck_source             # Update Moxfield publicId metadata
├── GET    /api/verify_against_source   # Check Moxfield sync
├── GET    /api/moxfield_format         # Proposed-deck as Moxfield paste
├── GET    /api/game_changers           # WotC latest banned/restricted
└── GET    /api/deck_audit              # Full deck analysis

web/routes_dashboard.py (blueprint: dashboard aggregation)
├── make_dashboard_blueprint(deck_dir, knowledge_db, list_decks, resolve_deck_path)
├── GET /api/decks                      # All decks: { id, name, path }[]
├── GET /api/dashboard                  # DashboardData for one deck
├── GET /api/iterations                 # Recent iterations (all or filtered)
├── GET /api/pricing_series             # Sparkline data
└── GET /api/verdict_breakdown          # Audit-version win/loss stats

web/routes_meta.py (blueprint: meta + ops routes)
├── make_meta_blueprint(deck_dir, list_decks, asset_version)
├── GET  /                              # Root HTML
├── GET  /api/health                    # { status, deck_dir, deck_count }
├── GET  /api/forge_version             # Jar version + age check
├── GET  /api/correlation_summary       # forge_py↔Forge agreement log
└── POST /api/log_error                 # Browser error sink
```

**Blueprint factory pattern**: Each `make_<group>_blueprint(...)` returns
a Flask Blueprint closing over the necessary state. This enables:

- Clean separation of route groups by business domain (audit, sim, decks,
  etc.)
- Stateless blueprints; all deps passed in explicitly.
- Easy testing via mocked dependencies.
- No global Flask app import in route modules (keeps them pure).

**Lazy-import pattern for test monkeypatches**: The web layer imports
`detect_forge_version`, `build_dashboard`, and other Layer 1–2 functions
lazily inside route handlers or via module re-import (e.g.,
`from . import app as _app_mod; info = _app_mod.detect_forge_version()`).
This ensures test patches applied to `commander_builder.web.app.*` remain
in scope across the module boundary after the split.

---

## Persistence locations

| Path | Owner | What |
|------|-------|------|
| `vendor/forge/userdata/decks/commander/*.dck` | `moxfield_import` | Imported decks (`[USER]`-prefixed for own; `[REF]` for meta-test references) |
| `vendor/forge/userdata/decks/commander/_pools/B<n>.json` | `pool_curator` | Curated pool snapshots |
| `vendor/forge/userdata/decks/commander/_pools/B<n>_analysis.json` | `pool_curator` | Per-pod `MatchAnalysis` |
| `vendor/forge/userdata/decks/commander/_matches/*.json` | `run_match` | User-vs-pool `MatchupReports` |
| `vendor/forge/userdata/decks/commander/_compare/*.json` | `compare_versions` | A/B `ComparisonReports` |
| `vendor/_js_errors.log` | `web/app.py` | Browser-side error reports via `/api/log_error` |
| `vendor/forge/build.txt` | (bundled) | Forge build timestamp; consumed by `detect_forge_version` |
| `knowledge_log.sqlite` (repo root, or `COMMANDER_BUILDER_KNOWLEDGE_DB` override) | `knowledge_log` | Iteration history |
| `.cache/scryfall/*.json` and `C:\dev\mtg_cards\oracle_snapshots\*.json` | `scryfall_client` | Card metadata cache (shared with `forge_py`) |
| `.cache/edhrec/*.json` | `edhrec_client` | EDHREC page cache (24 h TTL) |
| `_forge_py_correlation.csv` (repo root) | `forge_py_correlation` | Paired-verdict log (opt-in) |

---

## Backend-swap seams

Where the architecture allows swapping a backend without touching
callers. Adding a new backend at one of these seams should never
require changing module boundaries.

| Seam | Default | Alternatives |
|------|---------|--------------|
| `improvement_advisor.advise(source=...)` | `"heuristic"` (EDHREC inclusion%/synergy) | `"bracket_peers"` (Moxfield peer rankings), `"claude"` (LLM-synthesized via `_advisor_claude`); each mapped to a different module |
| `analyst.analyze()` router | `heuristic_verdict` | `claude_verdict` (anthropic SDK; `ANTHROPIC_API_KEY` or BYO-key header), `ollama_verdict` (HTTP POST to `localhost:11434/api/generate`) |
| `proposer.propose()` router | `manual_propose` (read `audit_manifest.json`) | `claude_propose`, `ollama_propose` |
| `forge_runner` AI | Forge built-in heuristic AI | Phase 4 (out of scope today): Claude-as-pilot via decision-point hooks |
| `moxfield_push._api_push` | `NotImplementedError` (WON'T-DO for personal-use scope) | — |
| `forge_py_correlation` execution | OFF | `COMMANDER_BUILDER_CORRELATE_FORGE_PY=1` opts in to paired-verdict logging |

---

## Working principles

These are how sessions should operate on this project. Follow them.

1. **Verify before assuming.** If you're not sure how Forge does X,
   write a small test or read the source rather than guessing. Wrong
   assumptions wrapped in try/except blocks rot quietly.

2. **Honest pushback over compliant building.** If something in the
   spec doesn't make sense, say so. The user explicitly wants this
   kind of feedback.

3. **Small, validated steps.** Don't write 500 lines as the first
   deliverable. Each phase / component is built and validated before
   integration.

4. **Modularity over cleverness.** Phase 3 will swap part of Phase 2
   for a learned model. Phase 4 may swap Forge's AI for Claude.
   Clean interfaces > clever inheritance.

5. **Document drift.** When something in the docs becomes wrong,
   update the doc in the same commit. Don't let drift accumulate.

6. **No silent failures.** Forge can fail in many ways (missing
   cards, AI hangs, JavaFX issues). Surface failures loudly with
   actionable error messages, not generic exceptions.

7. **Minimum viable first.** Better a slow, ugly pipeline that runs
   end-to-end than a beautiful component that hasn't been integrated.

8. **Log everything that could become training data.** Phase 3 wants
   structured, complete logs from day one. Don't lose data we'd want
   later.

---

## Coding conventions

- **Many small files > few large ones.** Target 200–400 lines per
  module; hard ceiling 800. Extract utilities from large modules.

- **Immutable patterns where possible.** Prefer returning new objects
  to mutating in place. Scoped local mutations are fine; never leak.

- **Errors handled explicitly.** No silent `except: pass`. If an
  error means "skip this candidate", log and continue. If it means
  "abort the run", raise.

- **Network calls go through a cache.** See `scryfall_client` for
  the pattern — disk cache, slugified filenames, polite sleep between
  requests, retry-with-backoff for transient failures.

- **Forge subprocess paths are not unit-tested.** Mock at the
  boundary (e.g. monkeypatch `ForgeRunner.run` to return a canned
  `SimResult`) or exercise via `scripts/`.

- **CLIs use argparse.** Every module that's an entry point exposes
  `def main(argv: Optional[list[str]] = None) -> int:`.

- **Type hints required on public APIs.** `Optional[X]` over
  `X | None` for now (project still supports 3.10).

- **Naming.** `camelCase` for module-level helpers; `PascalCase` for
  dataclasses; `UPPER_SNAKE_CASE` for constants; `_underscore_prefix`
  for module-private helpers.

- **Modular refactoring pattern.** When splitting a large module into
  per-function or per-source modules, follow the 2026-05-13 improvement_advisor
  and web blueprints patterns:
  - Extract shared dataclasses into `_<module>_models.py` first (no circular deps).
  - Extract per-source / per-group functions into `_<module>_<source>.py` modules.
  - Keep the orchestrator module (`improvement_advisor.py`, `web/app.py`) light:
    route to sub-modules, aggregate results, handle CLI/entry-point logic.
  - Re-export public API from orchestrator so external imports stay stable.
  - For Flask blueprints: use factory functions (`make_<group>_blueprint(...)`)
    that close over dependencies; avoid global state.
  - For lazy imports (e.g., test monkeypatches): import parent module by name
    inside the function (`from . import app as _app_mod; _app_mod.func()`),
    not at module top-level. This preserves patches across the split.

### When you add a module

1. Create `src/commander_builder/<name>.py`. One file, public API at
   the top in a docstring.
2. Create `tests/test_<name>.py` with at least one test per public
   function.
3. Update this doc — responsibility table + the layered diagram.
4. Update `STATUS.md` if the new module changes the open-backlog
   landscape.
5. Update `CHANGELOG.md` under `[Unreleased] → ### Added`.
6. If it's a CLI entry point, add it to `pyproject.toml`'s
   `[project.scripts]`.

### When you fix a bug

1. Write the failing test FIRST. Confirm it fails on current main.
2. Fix the bug. Test should pass.
3. Update `CHANGELOG.md` under `[Unreleased] → ### Fixed` with a
   one-line description.
4. If the fix changed a public contract, update this doc.

### When you commit

The user has a global git config that disables Co-Authored-By
attribution. Don't add it back. Conventional-commits format:

```
feat(scope): add archetype classifier (heuristic)
fix(scope): log_parser regex order — was leaving [B<n>] suffix
refactor(scope): extract pool_curator main() for entry-point script
docs: update STATUS to reflect Phase 2 completion
test: add integration test for iteration_loop
```

Don't commit unless the user explicitly asks. Keep changes coherent —
prefer one feature per commit; never mix bug fixes with refactors.

### Public-repo safety

All MTG-stack repos are public on GitHub. Before every commit:

- Scan staged diffs for `sk-ant-`, `sk-`, `Bearer `, JWT prefixes,
  `.env` contents, personal emails.
- Test fixtures use placeholder keys like `"sk-test-byo-12345"` —
  never real ones.
- The web app's `GET /api/settings` (FP-011, not yet built) **must**
  redact key values before responding. Never log request bodies that
  may contain keys.

---

## Key decisions (rationale captured at the time)

For *recent* decisions (last few days) see
[STATUS.md](STATUS.md#decisions-recently-made-recent-context). Older
load-bearing decisions:

- **Python over Node.js.** Better stdlib subprocess management for
  invoking a Java CLI on Windows; the existing Moxfield→Forge converter
  is also Python.
- **Forge over XMage.** Forge has a documented and known-working
  headless `sim` mode. XMage's headless capabilities are less
  documented.
- **LLM-as-analyst before ML.** Generates training data while
  delivering value; small datasets favor reasoning over learning.
- **SQLite for the knowledge log.** Single-file, no server, easy to
  inspect, easy to dump as CSV when training the Phase 3 model.
- **Same-pool comparison preferred over same-RNG-seed.** Forge 2.0.12
  has no `--seed` flag. The next-best variance control is fixing the
  opponents and running enough games that hand-of-cards variance
  averages out.
- **Tournament-curated opponent pools, not hand-picked.** Hand-picking
  imports the user's biases, misses meta shifts, and doesn't scale
  across brackets. Tournament selection is reproducible and
  self-updating.
- **`[USER]` filename prefix for the deck under test.** Same flat
  directory as opponents (Forge sim doesn't recurse subfolders). The
  prefix + `[B<n>]` suffix lets the orchestrator distinguish the
  candidate from the pool by filename alone — no separate manifest.
- **Drop bundled precons from the opponent pool.** Forge's 167
  bundled commander precons are essentially all bracket-2. Useful as
  smoke tests, useless as opponents for B3+ user decks. Retired to
  `_retired_precons/`.
- **Bracket-locked sims, no cross-bracket.** B3 vs B5 is noise, not
  signal. `--bracket` is mandatory; the curated pool only contains
  decks whose Moxfield-confirmed bracket matches.
- **`publicId` as `deck_id` for lineage durability.** Moxfield deck
  renames break filename-keyed lineage. The `Moxfield=<publicId>`
  metadata line in `.dck` files survives renames; iteration_loop
  reads it preferentially.
- **Personal-project scope cuts.** Moxfield API push (FP-005) closed
  as WON'T-DO — clipboard textarea is the final design. LICENSE
  deferred to "TBD" — adopt when going public.
- **`forge_py` is NOT a hard dependency.** Imported lazily inside
  `forge_py_correlation` so a missing install never breaks
  commander_builder.

---

## Audit-prompt provenance

`prompts/moxfield_audit_v3.md` is versioned in-repo so prompt drift is
tracked. Step 8 self-improvements land as `_v4.md`, `_v5.md`, etc. —
never overwrite a prior version.

Step 5.6 (optional 100-game JS goldfish sim) is **superseded by
`compare_versions`** for in-pipeline runs. Use Step 5.6 only when Forge
isn't available (remote audit session) or for very large swaps where
the pre-execute consistency check (mulligan rate, commander-turn) has
independent value before committing to a full sim.

---

## Where Ollama (or another local LLM) could plug in

The audit prompt itself currently runs on Claude — it's a complex
multi-step workflow with web fetches and structured JSON manipulation.
Several **simpler tasks** in the broader pipeline are good candidates
for routing to a local Ollama model to save Claude tokens:

| Task | Complexity | Frequency | Good fit for local? |
|------|-----------|-----------|---------------------|
| Archetype classification (one-shot: aggro/midrange/control/combo/stax) | Low | Per-deck, occasional | ✅ Strong fit |
| Color identity from commander name | Low | Per-deck, occasional | ✅ Strong fit |
| Card role tagging for sim (regex first, LLM only on ambiguous) | Low | Per-card, batched | ✅ Strong fit |
| Card-pair synergy hint ("does X synergize with Y") | Medium | Per-swap | ⚠️ Maybe — quality-sensitive |
| Audit's blind ideal build | High | Per-deck audit | ❌ Stay on Claude |
| Audit's swap rationale generation | Medium-High | Per-deck audit | ❌ Stay on Claude |
| Phase 2 analyst verdict | High | Per-iteration | ❌ Stay on Claude |
| Phase 2 proposer | High | Per-iteration | ❌ Stay on Claude |

When we're ready, the natural shape is a thin `llm_router.py` module:

```python
def classify(prompt: str, *, complexity: str = "auto") -> str:
    # complexity: "low" → Ollama, "high" → Claude API, "auto" → router decides
    ...
```

Decisions about routing thresholds, prompt format, and quality
fallbacks are deferred until there's concrete cost pressure.
