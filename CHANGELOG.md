# Changelog

All notable changes to this project will be documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; semver
applies once we tag a 1.0.

## [Unreleased]

### Project management
- New `BACKLOG.md` with 25 numbered gaps across 4 tiers (3 closed this session)
- New `STATUS.md` for current operational state
- New `CHANGELOG.md` (this file)
- New `docs/architecture.md` module map with layered diagram + responsibility table
- New `pyproject.toml` — `pip install -e .` works, `PYTHONPATH=src` no longer
  required. CLI entry points: `commander-import`, `commander-snapshot`,
  `commander-curate`, `commander-match`, `commander-compare`, `commander-iterate`,
  `commander-push`
- New `CONTRIBUTING.md` — dev setup walkthrough, conventions, ADR template
- README.md rewritten to reflect Phase 2 workflow (closes GAP-016)
- `.gitignore` extended to cover `*.log`, `.cache/`, `*.sqlite` artifacts

### Added
- New `archetype.py` module with heuristic classifier (filename hint →
  keyword content scan → midrange fallback). Replaces `_stub_classifier` in
  `pool_curator`. `claude_archetype` and `ollama_archetype` stubs in place
  for future LLM escalation. Closes GAP-001.
- `pool_curator` CLI `--max-candidates` (default 12) with seed-stable
  sampling — pool curation can now actually run end-to-end without 4+ hour
  wall times. Closes GAP-002.
- New `iteration_loop.resolve_deck_id()` — reads `Moxfield=<publicId>` from
  .dck metadata so iteration lineage survives Moxfield deck renames. Falls
  back to filename for legacy decks. Closes GAP-003.

### Fixed
- `archetype` regex bug: `+1/+1 counter` keyword had unescaped `+` causing
  `re.PatternError`. Caught immediately by the test suite — 173/173 passing
  after the fix.
- `pool_curator._filename_for_match` collision-suffix gap (GAP-004): now
  strips both `[USER]` prefix and `_uniquify` `(N)` suffix before matching;
  prefers exact stem over de-uniquified to disambiguate when both forms
  exist on disk. Was silently dropping wins for any deck whose filename had
  been uniquified.
- `pool_curator._split_into_slices` persistent-violation case (GAP-006):
  bounded swap search (5 swaps + default) replaces the prior one-shot 3↔4
  swap. If all candidates violate, ships default with a `WARN`. Also no
  longer mutates the caller's `top6` list.

### Tests (Tier 1 hardening pass)
- 173 → 183. Added `test_iteration_loop` (5 new tests for `run_one_iteration`
  with mocked `compare`), 3 new `_filename_for_match` tests (collision
  suffix, [USER] prefix, exact-over-deuniquified ordering), 3 new
  `_split_into_slices` tests (no-mutation, search-finds-non-violating,
  WARN-when-no-arrangement).

### Added (Tier 2 round)
- New `proposer.py` module + `commander-iterate --auto-propose` flag.
  Three-backend router (manual / Claude / Ollama) with graceful fallback to
  manual when LLM backends aren't available. Closes GAP-005.
- New `status.py` module + `commander-status` entry point. Reports decks
  per bracket, curated pools, recent matches/compares, knowledge_log stats.
  `--json` flag for scripting. Closes GAP-014.
- `ForgeRunner.run` accepts `stream=True` and `on_line=callback` for
  long-running sims. Default behavior unchanged (battle-tested blocking
  path). Closes GAP-008.
- New `propose_then_iterate()` in `iteration_loop` ties proposer +
  run_one_iteration into one call. The seam where the manual paste loop
  closes once `claude_propose` body is filled in.

### Tests (Tier 2 round)
- 183 → 218. New: `test_proposer` (15), `test_status` (13),
  `test_forge_runner` (7). All offline; LLM backends mocked or stubbed.

### Added (Tier 3 round — closing the iteration cycle)
- New `revert_to.py` module + `commander-revert` CLI. Restores a deck to a
  previous iteration's `deck_snapshot` blob and generates a Moxfield push
  blob ready for paste. Records the revert as its own iteration row by
  default. Closes GAP-017.
- New `edhrec_client.py` module. Fetches `edhrec.com/commanders/<slug>`
  pages and parses the embedded `__NEXT_DATA__` blob for top cards / high
  synergy / new cards / related commanders. 24-hour disk cache. Tolerant
  of EDHREC schema shifts. Closes GAP-009.
- New `game_changers.py` module. Fetches WotC's Game Changers list with
  HTML-list parsing + heuristic filtering, 7-day cache, fallback to bundled
  list on parse/network failure. Closes GAP-018.
- New `migrate_legacy_deck_ids()` in `knowledge_log`. Walks rows whose
  `deck_id` looks like a filename, looks up the publicId from the row's
  `deck_snapshot` `Moxfield=` line, updates. Dry-run mode supported.
  Closes GAP-024.
- `prompts/moxfield_audit_v3.md` Closing Summary now embeds a JS snippet
  using `URL.createObjectURL` to one-click download the
  `audit_manifest.json` with the right filename. Closes GAP-011.

### Tests (Tier 3 round)
- 218 → 260. New: `test_revert_to` (8), `test_edhrec_client` (13),
  `test_game_changers` (8), `test_compare_versions` integration tests (4),
  4 new `test_knowledge_log` tests for `migrate_legacy_deck_ids`. All
  offline; HTTP mocked, no external dependencies.

### Added (Tier 4 round — LLM bodies + ops)
- `analyst.claude_verdict` body wired with full anthropic SDK. Builds a
  system prompt describing the verdict taxonomy + JSON schema, calls
  `messages.create`, normalizes the response. Falls back to
  `NotImplementedError` cleanly without API key / SDK. Closes the
  remaining half of GAP-007.
- `analyst.ollama_verdict` body wired via `urllib` POST to
  `localhost:11434/api/generate` with `format: "json"`. Same fallback
  semantics on unreachable daemon.
- `proposer.claude_propose` body wired the same way, using
  `prompts/moxfield_audit_v3.md` as the system prompt. Strips markdown
  code fences from responses. Finishes GAP-005.
- `proposer.ollama_propose` body wired identically.
- New `doctor.py` module + `commander-doctor` CLI. 10 environment
  checks (Python, package, Forge, Java, decks dir, knowledge_log, two
  cache dirs, Anthropic key, Anthropic SDK, optional Ollama).
  GREEN/YELLOW/RED status with mapped exit codes. `--json` output.
  Verified GREEN on real env.
- `archetype._AGGRO_KEYWORDS` expanded with 14 tribal types + more
  aggressive keywords. Closes GAP-025.
- New `.github/workflows/test.yml` — matrix runs on Ubuntu+Windows,
  Python 3.10/3.11/3.12. Closes GAP-015.
- New `FUTURE_PLANS.md` — 5 parked architectural questions including the
  Forge replacement discussion (FP-001).

### Tests (Tier 4 round)
- 260 → 288. New `test_doctor` (13), 11 new tests across `test_analyst`
  and `test_proposer` for the LLM body success paths (mocked anthropic SDK
  via `types.ModuleType` injection, mocked `urlopen` for Ollama).
- Bug surfaced and fixed: existing `claude_verdict_is_unimplemented` test
  was leaking the dev-environment's stale `ANTHROPIC_API_KEY` and
  installed `anthropic` package, causing real API calls to leak through.
  Replaced with explicit `monkeypatch.delenv` and clearer module-injection
  pattern.

### Added (Tier 5 round — reporting + export + scope cuts)
- New `report.py` + `commander-history` CLI. Markdown rendering of a deck's
  full iteration lineage with per-iteration card-diff tables, win-rate
  trajectory line, verdict badges, and rationale + analyst notes.
  `--recent` mode gives a cross-deck summary table. Closes long-standing
  GAP-010 from PROJECT.md's Phase 1B component list.
- New `export.py` + `commander-export` CLI. JSON export/import of the
  knowledge log with full / per-deck / recent-N filtering. Skip-existing
  semantics on import so re-runs don't duplicate. Closes GAP-026.
- Personal-project scope decisions:
  - **GAP-022 / FP-005** (Moxfield API push) closed as WON'T-DO. The
    clipboard textarea workflow is the final design.
  - **GAP-023** (LICENSE) marked LOW-PRIORITY. `pyproject.toml` keeps
    `license = "TBD"` for personal use.
  - **CI simplified** to Windows + Python 3.12 only (drop multi-OS /
    multi-Python matrix that didn't match the actual dev environment).
  - **`CONTRIBUTING.md`** reframed as "session notes" rather than
    open-source contribution guide.

### Tests (Tier 5 round)
- 288 → 319. New: `test_report` (20), `test_export` (11). All offline.

### Added (Tier 6 round — improvement advisor + meta-reference benchmark)
- New `improvement_advisor.py` module + `commander-advise` CLI. Generates
  swap recommendations without needing a browser-Claude session. Pulls
  EDHREC inclusion-% / synergy data via `edhrec_client`, prior match
  history from `_matches/`, and synthesizes either a heuristic proposal
  (default) or a Claude-LLM-aided one (`--use-claude`). Output mirrors
  `audit_manifest.json` so it feeds `commander-iterate`. Closes GAP-027.
- New `meta_test.py` module + `commander-meta-test` CLI. Auto-fetches
  Moxfield top-likes deck + EDHREC "Average Deck" for a commander, imports
  both with `[REF]` prefix, runs `compare_versions` against each. Set-arith
  card diff identifies "must-add" (in all references, not user),
  "consider" (in some), "off-meta" (only in user). Closes GAP-028.
- New `moxfield_import.find_top_liked_deck_for_commander()` helper that
  uses the public read-API search endpoint (the same one `search_decks`
  already uses) with exact-name filtering.

### Tests (Tier 6 round)
- 319 → 355. New: `test_improvement_advisor` (18), `test_meta_test` (13),
  3 new `test_moxfield_import` cases for `find_top_liked_deck_for_commander`,
  2 new `test_compare_versions` integration tests for the runner-injection
  path. All offline; HTTP mocked.

### Fixed (Tier 7 round — meta-test bugs surfaced by live Hakbal run)
- `edhrec_client.fetch_average_deck()` — new function. EDHREC's "Average
  Deck" lives at `/average-decks/<slug>/<bracket>/<budget>`, not as a
  Moxfield link inside the commander page. Old logic was looking for
  `moxfield.com/decks/...` strings in EDHREC's `__NEXT_DATA__` blob; that
  data isn't there. New function constructs the canonical URL from
  bracket+budget, falls back to less-specific URLs if the most-specific
  404s. Closes GAP-029.
- `meta_test._fetch_edhrec_average_deck` rewired to use the new fetcher.
- `--reference-url` now smart-routes: EDHREC URLs go through
  `fetch_average_deck`, Moxfield URLs go through `fetch_deck` (existing).
- `find_top_liked_deck_for_commander` now uses two-step lookup
  (card-search → ID → deck-search by `commanderCardId`) instead of the
  unsupported `commanderName` query param. Old approach silently returned
  empty results.

### Added (Tier 7 round — suggestion quality)
- New `UNIVERSAL_STAPLES` frozenset in meta_test. Sol Ring, Arcane Signet,
  basic lands, Command Tower etc. are filtered from both must-add and
  off-meta because they're noise in either direction. The user's first
  meta-test run had Arcane Signet in off-meta (false signal); this fixes
  that class of bug.
- New `CardSuggestion` dataclass replacing flat `list[str]`. Each entry
  carries `in_n_references` / `total_references` / `role` so callers
  can rank by confidence and group by role.
- New `_classify_card_role()` heuristic: tags adds as
  finisher / lord / tutor / wipe / removal / counter / draw / ramp / other.
- `CardDiffReport.must_add_by_role()` groups suggestions in priority order
  (finisher first, since "deck can't close" is the common diagnosis).
- "All draws" framing: 0-0-N output now says "NEITHER deck could close —
  add a finisher", not "roughly even".

### Tests (Tier 7 round)
- 355 → 370. +5 new tests for universal-staples filter, frequency labels,
  role grouping. +5 new tests in `test_edhrec_client` for
  `fetch_average_deck`. +3 in `test_moxfield_import` for the two-step
  card-id lookup. +2 in `test_meta_test` for smart URL routing.

## [0.2.0] — 2026-04-26 (Phase 2 scaffolding)

### Added
- `prompts/moxfield_audit_v3.md` — Moxfield deck-audit prompt as the LLM
  proposer step, versioned in-repo
- `snapshot_deck.py` — pre/post-audit `.dck` versioning
- `compare_versions.py` — head-to-head A/B Forge sim with two modes (4-player
  same-pod default; 1v1 constructed)
- `scryfall_client.py` — disk-cached commander color identity lookups
- `knowledge_log.py` — SQLite iteration history (audit_manifest + sim_report
  + verdict + lineage chain via parent_id)
- `analyst.py` — verdict router with heuristic / Claude / Ollama backends
  (LLM backends stubbed pending API access)
- `iteration_loop.py` — orchestrator wiring compare → analyst → knowledge_log
- `moxfield_push.py` — clipboard-based "push to Moxfield" helper; `_api_push`
  stub for future authenticated API access
- `ml_dataset.py` — Phase 3 scaffolding: 25-feature schema, deck-level
  train/eval split, `dataset_summary()`
- `docs/audit_workflow.md` — end-to-end pipeline doc with Ollama design space
- `scripts/integration_test_b3.py` — full Phase 2 smoke against the 6 B3
  user decks

### Changed
- `log_parser.py`: added `Phase: Ai(N)-...` line tracking → real per-deck
  `confirm_action_by_deck` attribution; replaces the `/pod_size` even-split
  stopgap in `pool_curator`
- `pool_curator._read_color_identity` now calls `scryfall_client` instead of
  returning the `""` stub
- `pool_curator.curate_bracket` writes a second JSON
  (`_pools/B<n>_analysis.json`) with per-pod `MatchAnalysis`
- `pool_curator.preflight_candidate` rejects on timeout / non-zero exit /
  no-games-completed (was passing crashed sims through)
- `pool_curator.CuratedPool.to_dict` now persists computed properties
  (`win_rate`, `confirm_action_per_game`, `suspected_inflated`) that
  `asdict()` was silently dropping
- `moxfield_import._uniquify` raises after 99 collisions instead of silently
  overwriting
- `prompts/moxfield_audit_v3.md` Step 5.6 reframed as superseded-by-Forge for
  in-pipeline runs

### Fixed
- `log_parser._normalize` regex order (was `[B<n>]$` before `.dck$`, so the
  `$` anchor on the bracket regex never matched). Decorated names like
  `[USER] Foo [B3].dck` were leaving `[B3]` in the output, silently breaking
  match attribution everywhere downstream.

### Live runs
- B3 batch preflight: 6/6 pass
- B4 batch preflight: 6/6 pass (3 of 6 hit slow-match cutoff — useful real
  signal)
- Hakbal vs Hash 20-game smoke: passed end-to-end (18 of 20 games drew —
  exposed `analyst`'s "decks_drew_too_often" lesson)
- Integration test on the 6 B3 decks: full Phase 2 stack validated against
  real data

### Tests
- 81 → 144 (added `test_scryfall_client`, `test_knowledge_log`,
  `test_analyst`, `test_moxfield_push`, `test_ml_dataset`, plus active-player
  attribution cases in `test_log_parser`)

## [0.1.0] — 2026-04-26 (Phase 1B foundation)

Documented in `HANDOFF_2026-04-26.md`. Highlights:

- `forge_runner` (Forge headless harness)
- `log_parser` (sim stdout extraction)
- `game_analyzer` (per-game telemetry: turns, life curves, eliminations)
- `moxfield_import` (Moxfield → Forge `.dck` conversion + bulk-by-bracket
  harvest)
- `pool_curator` (tournament-style opponent meta selection)
- `run_match` (user deck vs pool with `MatchupReport`)
- 41 → 81 tests; suite under 1s

## [0.0.1] — 2026-04-26 (Phase 1A verifier)

Initial Forge verifier — surfaced the actual `sim` log format on Windows so
Phase 1B parser had a real schema to target. Documented authoritative parse
points (Match Result, Game Result) and the 4-player Game Outcome bug.
