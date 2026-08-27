# Architecture, conventions, and working principles

> Single technical reference for the project: module map, data flow,
> persistence, coding conventions, and the decisions that shaped them.
> [STATUS.md](STATUS.md) tracks operational state; [CHANGELOG.md](CHANGELOG.md)
> records what landed.

---

## Layered view

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 4 — Phase 3 learned predictor: NOT BUILT                 │
│    (FP-002 closed-refuted 2026-07-30; the 25-feature schema's   │
│     executable record is scripts/experiments/ml_dataset.py)     │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3 — Phase 2: closed-loop iteration                       │
│    iteration_loop.py        (orchestrator)                      │
│    analyst.py               (verdict router)                    │
│    improvement_advisor.py   (orchestrator: 3 backends + filters) │
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
│      ├─ _helpers.py         (Flask-independent helpers)        │
│      ├─ routes_audit.py     (audit + advise endpoints)         │
│      ├─ routes_sim.py       (propose-swap + iteration CRUD)    │
│      ├─ routes_decks.py     (deck CRUD + import + GC)          │
│      ├─ routes_dashboard.py (dashboard aggregation)            │
│      └─ routes_meta.py      (health, version, error sink)      │
│    web/static/app.js        (UI + error collector)              │
│    web/static/app.css       (theme)                             │
└─────────────────────────────────────────────────────────────────┘
```

The arrows read top-to-bottom as "composes"; dependency actually flows
the other way for the last hop — **Layer 0 (web) sits on top and
imports layers 1-3**, not the reverse. Known, deliberate exceptions to
"lower never imports higher": `status.py` and `doctor.py` (Layer 1
boxes) read `knowledge_log` (Layer 3), and `deck_dashboard.py` reads
`archetype`/`staples` (Layer 2) — they are reporting surfaces that may
read any layer. Everything else honors the invariant.

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
| `staples` | `UNIVERSAL_STAPLES_LC`, `BASIC_LANDS_LC`, `classify_role_extended` (canonical), frequency labels, confidence tiers, role saturation thresholds, manabase essentials, tribal essentials, `card_theme_slugs` (per-card theme membership; `detect_themes` sums it, so deck- and card-level answers cannot drift) | Recommendation logic. Advisors use these. |
| `archetype` | Deck-classifier v2: oracle-derived signals (combos+tutors, interaction, stax patterns, tribal, curve) with a filename hint and a midrange default | Proposing changes. The old Claude/Ollama stubs are GONE (pinned by `test_llm_stubs_are_gone`); local tagging now lives in `local_model`. |
| `deck_judge` / `_deck_judge_prompt` | FP-016 Phase 1: blinded 6-judgment panel (3 per order, deck-keyed agreement, 5-of-panel supermajority), diff-focused intent-anchored prompts, schema-v4 `judge_verdict`/`judge_report` beside the sim verdict | Advancing decks or gating anything — observe-only by contract. Game outcomes: Forge decides which deck is BETTER, only Forge. |
| `cli` | The `commander` umbrella: 29-subcommand registry (1:1 aliases of every console script), grouped help from each module's own docstring, verbatim argv/exit-code passthrough | Any behavior. It is an alias layer — the hyphenated scripts stay canonical. |
| `init_cli` | Guided first-run sequencing (deps → oracle prime → decks → pool) with measured cost warnings, `--yes`/`--dry-run`, resumable by probing real artifacts rather than a state file | The steps themselves — it composes `bootstrap`, `oracle_store`, `moxfield_import`, `pool_curator`. |
| `local_model` | Local-model tier (A4): preflight, schema-first tasks with the evidence supplied, closed-taxonomy validation, degrade-to-deterministic routers, agreement harness | Proposals and verdicts — those stay on Claude. It also owns no taxonomy of its own; it imports `staples`' and `archetype`'s. |
| `game_changers` | WotC Game Changers list (HTML scrape, 7-day cache, bundled fallback); `offline_game_changers()` for cache-only callers (trusted disk cache → bundled fallback, never network — the deck judge's swap labeling uses it) | Bracket-fitting. The advisor + dashboard consume. |
| `pool_curator` | Round-robin tournament, candidate ranking, top-6 split with archetype/color diversity, persisted pool JSON | Picking candidates. That's the user / `moxfield_import`. |
| `run_match` | User deck vs curated pool (or fallback opponents), `MatchupReport` | Improvement decisions. That's the analyst loop. |
| `compare_versions` | Old-vs-new head-to-head A/B sim; parallel pod dispatch; adaptive early-stop; intra-pod abort; card-level diff | Whether the new version is "better". That's `analyst`. |
| `snapshot_deck` | File-copy `.dck` to versioned filename; refuse-clobber semantics | What to do with the snapshot. Workflow / iteration_loop owns. |
| `dck_meta` | The filename↔`Name=` win-attribution invariant: `rewrite_name_to_stem` rewrites `[metadata] Name=` to the filename stem (original kept as `DisplayName=`) in every deck writer that copies/splices an existing `.dck`. Also the filename↔`[B<n>]` half: `read/set/clear_bracket_unverified` maintain the `BracketUnverified=<n>` marker (2026-08-20) that keeps "this declared bracket has no measurement behind it" alive across saves | Deciding filenames. Callers (snapshot / proposer / meta-test / import) pick the name. Deciding when a bracket counts as re-verified — that is `web/routes_dashboard`. |
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
| `_llm_json` | Shared robust JSON extraction for LLM responses: `try_extract_json_object` (fence strip / brace-scan recovery) + `extract_json_object` raising a loud `LLMJsonError` with context + response snippets | Prompting or calling the LLM. Analyst / proposer / curator / advisor call it on the raw reply. |
| `proposer` (orchestrator) | Router for manual / Claude proposers; the `Proposal` dataclass; `auto_propose()` curator pipeline; `apply_proposal_to_deck`; `_extract_curator_json` | Validating proposals (`compare_versions` + `analyst` do). Local-model proposing — retired 2026-08-17, see `local_model`. |
| `_proposer_filters` | Post-response curator filters: `enforce_bracket_caps` (game-changers stripped at B1/B2), `enforce_color_identity` (off-color adds rejected via Scryfall CI), `_load_game_changers` | Recommendation logic. The advisor / curator generate; filters reject. |
| `_proposer_sim` | Forge A/B sim glue: `_verdict_from_ab` (binomial-significance verdict → kept/reverted/neutral), `_ab_to_iteration_fields`, bracket-aware `_pick_filler_decks`, `_run_sim_and_record`, `_log_auto_curate_iteration` | Running the sim itself. `forge_runner` + `compare_versions` do. |
| `_proposer_cli` | `auto_curate_main` (the `commander-auto-curate` console_script) — argparse + end-to-end orchestration of advisor → curator → apply → sim | Pipeline stages themselves; lives here only as a thin wrapper. |
| `_card_list_refresh` | Hardcoded-list staleness diff helpers (`diff_card_lists`, `parse_mdfc_lands_from_response`, `fetch_mdfc_lands`); used by `scripts/refresh_card_lists.py` | Mutating `deck_health`'s lists. Manual review only. |
| `consistency` | Opening-hand / mulligan / commander-on-curve math: exact hypergeometric + seeded Monte Carlo (`opening_hand_stats`). Wired 2026-08 into `deck_health`'s additive `consistency` signal → `/api/audit` payload → audit-panel tile | The letter grade. Deliberately display-only — folding it into `compute_health_grade` would silently re-grade every deck. |
| `iteration_loop` | Wiring compare → analyst → knowledge_log; `propose_then_iterate()` | Multi-iteration loop (FP-012 territory). |
| `knowledge_log` | SQLite-backed iteration history; lineage chains via `parent_id`; legacy deck_id migration | Reporting. `report.py` does. |
| `report` | Markdown rendering of one deck's iteration lineage; cross-deck recent-iterations summary | Mutating the log. Read-only. |
| `revert_to` | Restore deck to a previous iteration's snapshot blob; emits Moxfield push blob | Push step. User pastes. |
| `export` | JSON dump/restore of knowledge_log (full / per-deck / recent-N filter); skip-existing semantics | Schema validation. Trusts the dump. |
| `doctor` | 10 environment checks; GREEN/YELLOW/RED status; `--json` output. The local-model check delegates to `local_model.LocalModelClient.preflight` (flag off → GREEN with no socket; failures forward the preflight's own remedy text verbatim) | Fixing problems. Reports only. |
| `status` | Decks-per-bracket, curated pools, recent reports, knowledge_log stats | The work itself. Pure observation. |
| `deck_dashboard` | Stat tiles, mana curve, categories, theme tags (incl. tribal type), suggested adds, est. price, inferred bracket | Mutation. The web app's audit endpoint does. |
| `deck_builder` (FP-014 orchestrator) | Build-from-scratch: commander + bracket → legal exactly-99. Seeds from `edhrec_client.fetch_average_deck` (or a role-target shell), enforces commander/singleton/99/color-identity, owns the land budget, renders the `.dck`; `commander-build` CLI + `--improve` hand-off | Manabase fill + personalization (its two sub-modules); running the sim (`commander-improve`). |
| `deck_builder_manabase` (FP-014.2) | Color-source manabase: land count from the curve, per-color source targets (full Karsten per-CMC table, most-demanding-card rule; two-anchor fallback for unresolvable costs), fill order (keep seed lands → top-up fixing from advisor land tiers → basics). Degrades to basics-only when card data can't resolve | The 99-card budget + nonland trim. `deck_builder` owns those. |
| `deck_builder_personalize` (FP-014.3) | Three net-zero like-for-like nonland-spell passes — lift co-occurrence picks (skips without a ≥10-deck corpus), bracket-steer, owned-collection bias — each preserving exactly-99 / singleton / color-identity | Sourcing, rendering, re-validation. `deck_builder` owns those. |
| `forge_py_correlation` | Paired-verdict logging (Forge vs forge_py); CSV append; agreement-rate summary | Driving forge_py. Imported lazily; opt-in via env var. |
| `web/app.py` (orchestrator) | Flask app creation; blueprint registration; `create_app()` entry point; stale file cleanup; deck listing; path resolution | Business logic. Blueprints call into the layers above. |
| `web/_helpers.py` | Flask-independent helpers (`_apply_swaps_to_dck`, `_normalize_pasted_deck`, `_format_added_line`, etc.); `_BASIC_LANDS` constant; `atomic_write_text`, the crash-safe `.dck` overwrite shared by the two blueprints that write decks | Route-specific logic. Each blueprint uses as needed. |
| `web/routes_audit.py` | Audit + streaming (`GET /api/audit`, `GET /api/audit/stream`, `GET /api/advise`); wires `improvement_advisor` | Other route groups. Each lives in its own blueprint. |
| `web/routes_sim.py` | Propose-swap + iteration CRUD (`POST /api/propose_swap`, `POST /api/save_iteration`, `GET /api/iteration/<id>`, comparisons, snapshots) | Other endpoints. Organized by business domain. |
| `web/routes_decks.py` | Deck CRUD + import/GC (`GET/PUT/DELETE /api/deck_text`, `POST /api/import_deck`, `GET/PUT /api/deck_source`, manabase verification, audit) | Other routes. Grouped by deck lifecycle. |
| `web/routes_dashboard.py` | Dashboard data (`GET /api/decks`, `/api/dashboard`, `/api/iterations`, `/api/pricing_series`, `/api/verdict_breakdown`); the one write it makes: retiring a deck's `BracketUnverified=` marker when its own bracket estimate agrees with the filename tag | Audit/sim routes. Dashboard-specific aggregation. Setting the marker — that is the `deck_text` PUT. |
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
   │    heuristic_verdict default; claude_verdict with API key.   │
   │    ollama_verdict retired 2026-08-27 — see local_model.      │
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

web/_helpers.py (Flask-independent)
├── _apply_swaps_to_dck()               # Apply swap manifest to deck text
├── _normalize_pasted_deck()            # Canonicalize deck format
├── _format_added_line()                # Render added card for output
├── _iteration_to_dict()                # Serialize iteration row to JSON
├── _match_pct_from_evidence()          # Win/draw rate from records
├── _pad_main_to_target()               # Pad main to 100 - commander count (99 single, 98 partners)
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
| `~/.commander-builder/_js_errors.log` | `web/routes_meta.py` | Browser-side error reports via `/api/log_error` (moved out of `vendor/` 2026-08-16 — telemetry doesn't belong in a vendored tree, and the old path was derived positionally from the deck dir) |
| `vendor/forge/build.txt` | (bundled) | Forge build timestamp; consumed by `detect_forge_version` |
| `knowledge_log.sqlite` (repo root, or `COMMANDER_BUILDER_KNOWLEDGE_DB` override) | `knowledge_log` | Iteration history |
| `.cache/scryfall/*.json` and `C:\dev\mtg_cards\oracle_snapshots\*.json` | `scryfall_client` | Card metadata cache (shared with `forge_py`) |
| `.cache/edhrec/*.json` | `edhrec_client` | EDHREC page cache (24 h TTL) |
| `_forge_py_correlation.csv` (repo root) | `forge_py_correlation` | Paired-verdict log (opt-in) |

---

## Environment variables

Every `COMMANDER_BUILDER_*` flag in the codebase, in one place (added
2026-08-16 — before this, several were documented only in CHANGELOG
archaeology). Paths win over defaults; feature flags are opt-in unless
noted.

| Variable | Owner | Effect |
|----------|-------|--------|
| `COMMANDER_BUILDER_DECK_DIR` | `dck_utils` / web | Override the Forge deck directory (default `vendor/forge/userdata/decks/commander`) |
| `COMMANDER_BUILDER_KNOWLEDGE_DB` | `knowledge_log` | Path to the SQLite iteration log (default repo-root `knowledge_log.sqlite`) |
| `COMMANDER_BUILDER_CONFIG` | `config_store` | Path to `config.json` (default `~/.commander-builder/config.json`) |
| `COMMANDER_BUILDER_CREDENTIALS` | `_secrets` | Path to the credentials file holding `ANTHROPIC_API_KEY` (default `~/.commander-builder/credentials`) |
| `COMMANDER_BUILDER_SECRET_KEY` | `web/app.py` | Flask session secret; generated per-run when unset |
| `COMMANDER_BUILDER_COLLECTION` | `collection` | Path to the owned-cards collection export |
| `COMMANDER_BUILDER_REPLAY_DIR` | `replay_store` | Where replay-lite game records are written |
| `COMMANDER_BUILDER_REPLAY_CAP_MB` | `replay_store` | Byte cap for the replay store before eviction |
| `COMMANDER_BUILDER_KEEP_GAME_LOGS` | `forge_runner` | Keep raw Forge stdout per game (soak debugging; large) |
| `COMMANDER_BUILDER_LOCK_DIR` | `forge_batch` | Override where per-profile `.commander-builder.lock` files live |
| `COMMANDER_BUILDER_CARD_SCORE` | `card_score` | Enable the FP-015 CardScore path (default OFF — failed three pre-registered gates) |
| `COMMANDER_BUILDER_REBUILD_TIER` | `change_budget` | Allow auto-mode to select the 30+30 rebuild tier (default OFF — that 6× cost multiplier is gated on an unvalidated health score; `--mode rebuild` is unaffected) |
| `COMMANDER_BUILDER_DECK_JUDGE` | `deck_judge` | Enable the observe-only LLM judge panel beside sim verdicts (default OFF — Phase 2's agreement analysis and the pre-registered kill criteria decide whether it ever becomes more) |
| `COMMANDER_BUILDER_LOCAL_MODEL` | `local_model` | Enable the local-model tier for narrow tagging (default OFF — unmeasured; run the agreement harness first) |
| `COMMANDER_BUILDER_LOCAL_MODEL_NAME` | `local_model` | Ollama model tag for that tier (default `llama3.2:3b`) |
| `COMMANDER_BUILDER_LOCAL_MODEL_URL` | `local_model` | Base URL of the Ollama-compatible daemon (default `http://localhost:11434`) |
| `COMMANDER_BUILDER_CORPUS_NORMS` | `corpus_themes` | Blend mined per-cluster role norms into targets (default OFF — pending A/B) |
| `COMMANDER_BUILDER_CORRELATE_FORGE_PY` | `forge_py_correlation` | Log paired forge_py↔Forge verdicts to `_forge_py_correlation.csv` |
| `COMMANDER_BUILDER_FORGEPY_SCREEN` | `forge_py_screen` | Pre-screen candidates with forge_py before spending JVM time |
| `COMMANDER_BUILDER_LOG_DECISIONS` | `_advisor_logging` | Write advisor decision traces for debugging |
| `MTG_CARDS_DIR` | `scryfall_client` | Oracle-snapshot directory shared with `forge_py` (defaults to an OS-appropriate path; historically a Windows dev path) |

---

## Backend-swap seams

Where the architecture allows swapping a backend without touching
callers. Adding a new backend at one of these seams should never
require changing module boundaries.

| Seam | Default | Alternatives |
|------|---------|--------------|
| `improvement_advisor.advise(source=...)` | `"heuristic"` (EDHREC inclusion%/synergy) | `"bracket_peers"` (Moxfield peer rankings), `"claude"` (LLM-synthesized via `_advisor_claude`); each mapped to a different module |
| `analyst.analyze()` router | `heuristic_verdict` | `claude_verdict` (anthropic SDK; `ANTHROPIC_API_KEY` or BYO-key header) (`ollama_verdict` retired 2026-08-27 — nothing in the repo could ever set its flag; verdicts stay on Claude by policy, local models on `local_model`'s narrow tasks) |
| `proposer.propose()` router | `manual_propose` (read `audit_manifest.json`) | `claude_propose` (`ollama_propose` retired 2026-08-17 — a tool-less 3B model could not execute the 706-line browser audit prompt; local models moved to `local_model`'s narrow tasks) |
| `forge_runner` AI | Forge built-in heuristic AI | Phase 4 (out of scope today): Claude-as-pilot via decision-point hooks |
| `moxfield_push._api_push` | `NotImplementedError` (WON'T-DO for personal-use scope) | — |
| `forge_py_correlation` execution | OFF | `COMMANDER_BUILDER_CORRELATE_FORGE_PY=1` opts in to paired-verdict logging |

---

## Data sources — risk tiers (2026-08-20, decision C3)

Every external source this program depends on, ranked by how likely it
is to break underneath us and what breaks with it. "Blast radius" is
what stops working the day the source changes; "fallback" is what the
code does about it TODAY (not aspirationally).

| Source | Interface | Risk | Blast radius | Fallback today |
|--------|-----------|------|--------------|----------------|
| Moxfield | **Undocumented private API** (`api2.moxfield.com`) | **High** — no contract, ToS-gray, CDN/bot-shield changes have broken it before | Single-deck import, bulk bracket harvest, top-likes search, bracket peers, meta-test references — most acquisition at once | Archidekt lane for single-deck import (`import_deck(source=)`); harvest/top-likes have NO fallback and now say so when they fail |
| EDHREC | JSON twin first (`json.edhrec.com`), HTML `__NEXT_DATA__` scrape second | **Medium** — JSON endpoint is undocumented but stable; the scrape is schema-tolerant and has survived redesigns | Heuristic advisor candidates, average-deck comparisons, theme pages | Two lanes internally (JSON → scrape); 24 h cache absorbs outages; advisor degrades to bracket-peers/manual sources |
| Scryfall | **Documented public API** + bulk oracle snapshots | **Low** — versioned, documented, explicitly third-party-friendly | Card metadata, color identity, oracle text for every classifier | Disk cache + bulk snapshots mean a total outage only blocks NEW cards; everything cached keeps working offline |
| Archidekt | **Documented public API** | **Low-Medium** — documented but less battle-tested here; no like-count, bracket usually null | The fallback lane itself; commander-keyed reference decks (partial) | It IS the fallback; if both it and Moxfield are down, single-deck import is paste-from-clipboard (`import_formats`) |
| WotC Game Changers page | HTML scrape | **Medium** — marketing pages get redesigned without notice | Bracket legality's game-changer list | 7-day cache + bundled snapshot fallback ships in the repo |
| Forge | Vendored JAR, local | **None** (pinned) — but upgrades change the card corpus | The sim itself; unsupported-card preflight | Version-detected (`detect_forge_version`); corpus mtime keys the sim-coverage cache so an upgrade invalidates it |

Rules of thumb this table encodes:

- Anything that exists ONLY via Moxfield's private API (bulk harvest,
  top-likes) is accepted as best-effort: failures must name what broke
  and what still works, never masquerade as "no decks exist."
- A documented API beats a scrape, and a scrape with a bundled/cached
  fallback beats a bare scrape. New acquisition features should enter
  at the lowest-risk tier that can serve them.
- Caches are the real resilience layer: every source above is cached on
  disk, so the failure mode is "stale," not "dark" — and staleness is
  surfaced (legality TTL warnings, `price_data_age_days`, oracle-age
  warnings in `commander-doctor`).

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

## Where local models plug in — BUILT (`local_model.py`, 2026-08-17)

Owner decision A4. This section used to describe a deferred sketch
called `llm_router.py`; the tier is now built, and the routing question
it deferred has been answered with a policy rather than a threshold.

**The policy: local models get tasks where the evidence is SUPPLIED and
the answer comes from a closed list.** Not "low complexity" — that was
the wrong axis. What predicts whether a small model succeeds is whether
it must *recall* Magic (unreliable below frontier scale, and the source
of invented card names) or merely *read* text it was handed and pick a
label. Proposal and verdict work stays on Claude — not because it is
complex, but because it needs judgment over knowledge the model has to
bring itself.

| Task | Status |
|------|--------|
| Card role tagging (oracle text supplied → one of `staples`' roles) | ✅ Built — `local_model` task `role_tag` |
| Archetype classification (deck signals supplied → one `Archetype`) | ✅ Built — `local_model` task `archetype_tag` |
| Color identity from commander name | ➖ Not worth a model — already deterministic in `scryfall_client` |
| Card-pair synergy hint | ⚠️ Open — quality-sensitive, and no deterministic fallback to degrade to |
| Audit's blind ideal build / swap rationale | ❌ Stays on Claude (policy, not deferral) |
| Phase 2 analyst verdict / proposer | ❌ Stays on Claude (policy, not deferral) |

Shape of the built module:

- **Preflight** checks the daemon *and* that the model is pulled, naming
  the exact `ollama pull <model>` command. Silent, confusing failure was
  the original complaint against this path.
- **Schema-first**: each task owns a short purpose-written prompt, a
  JSON schema, and its own validation. An answer outside the taxonomy is
  a malformed response, not data.
- **Degrade, never fabricate**: every failure returns `None` and the
  caller falls back to the deterministic classifier. A local answer is
  never a silent default.
- **Taxonomies are imported** from `staples` / `archetype`, never copied,
  so they cannot drift from the classifiers they back up.
- **An agreement harness** measures the tier against the deterministic
  classifier. It reports agreement, explicitly not accuracy — whether
  this tier earns production use is a question for data.

No production call site is wired to it yet, and the flag is off by
default. That is deliberate: wiring an unmeasured classifier into the
dashboard would be the same unvalidated-default move this decision was
reacting against.

Retired at the same time: `proposer.ollama_propose`, which fed all 706
lines of the browser audit prompt to `llama3.2:3b` and waited 600
seconds for a full swap manifest. `analyst.ollama_verdict` followed on
2026-08-27 — the same dead-code shape (no caller in the repo could ever
set `AnalystConfig.use_ollama`), retired the same way: the router rung
makes no call and prints the retirement note, because raising from
inside `analyze()`'s quiet `except NotImplementedError` fall-through
would have been swallowed silently.
