# Backlog — known gaps, bugs, and deferred work

> Living list, written 2026-04-26 after the Phase 2 scaffolding session. Each
> item has a stable ID (`GAP-NNN`) so cross-references in commits, comments,
> and test names stay valid as the list reorders. Effort estimates are
> rough — assume 1.5–2× when actually doing the work.
>
> Tier 1 = real failure modes that look fine until you hit them.
> Tier 2 = pinned bugs and feature gaps that PROJECT.md explicitly promises.
> Tier 3 = ops, UX, test coverage.
> Tier 4 = deferred until prerequisite data / API access exists.

---

## Tier 1 — Silent failures that bite when you actually use the system

### GAP-001 — Archetype classifier is a stub ✅ DONE 2026-04-26
- **Resolution**: New `archetype.py` module with heuristic classifier
  (filename hint → keyword content scan → midrange fallback). Replaces the
  `_stub_classifier`. Verified on synthetic stax/combo piles. The 6 B3 user
  decks all land on `midrange` — plausible for casual B3 builds.
  `claude_archetype` and `ollama_archetype` stubs landed for future
  escalation. 18 tests added.

### GAP-002 — Pool curation can't realistically run end-to-end ✅ DONE 2026-04-26
- **Resolution**: `pool_curator.py` now has `--max-candidates` (default 12)
  and seed-stable sampling via `_sample_candidates`. Inline CLI block
  refactored into `main()` so `commander-curate` entry point in
  `pyproject.toml` works. 4 tests added covering sampling determinism.

### GAP-003 — Lineage breaks if a Moxfield deck is renamed ✅ DONE 2026-04-26
- **Resolution**: New `resolve_deck_id(deck_path, fallback)` in
  `iteration_loop` reads `Moxfield=<publicId>` from the .dck metadata block.
  Falls back to filename only for legacy decks pre-dating the metadata patch.
  `run_one_iteration` now uses publicId as `deck_id` so lineage survives
  Moxfield deck renames. 7 tests added. Migration helper for backfilling
  legacy rows is **not** built — flagged as future work in GAP-024.

### GAP-004 — `_filename_for_match` collision-suffix gap ✅ DONE 2026-04-26
- **Resolution**: New `_candidate_match_keys()` helper strips both `[USER]`
  prefix and `_uniquify` suffix `(N)`. Two-pass match: exact stem first
  (so a non-uniquified deck wins over a `(N)`-suffixed one when both exist),
  then de-uniquified fallback. Old "documents the gap" test re-pinned as
  PASSING; added `test_filename_for_match_strips_user_prefix` and
  `test_filename_for_match_prefers_exact_over_deuniquified`.
- **Note**: The Unicode-name case (filename strips emoji, internal Name=
  keeps it) is still unhandled — Scryfall doesn't normalize emoji either.
  Tracked as future hardening; rare enough in practice to defer.

---

## Tier 2 — Pinned bugs and PROJECT.md promises

### GAP-005 — `proposer.py` doesn't exist ✅ DONE 2026-04-26 (skeleton)
- **Resolution**: Module landed with three backends sharing one interface:
  `manual_propose` (works today — reads `audit_manifest.json` from disk),
  `claude_propose` (stub raising NotImplementedError without API key OR
  without `anthropic` SDK; full wiring sketch in docstring), `ollama_propose`
  (stub). Router `propose()` falls back gracefully when LLM backends aren't
  available, so calling code never crashes from missing API access. New
  `propose_then_iterate()` in `iteration_loop` ties the two together;
  `commander-iterate --auto-propose` flag exposed at the CLI. 15 tests added.
- **Remaining work**: Replace the `claude_propose` stub body with the
  documented sketch when ANTHROPIC_API_KEY is wired. ~30 lines + a few
  integration tests with mocked `anthropic.Anthropic`.

### GAP-006 — `_split_into_slices` one-shot swap can leave persistent violations ✅ DONE 2026-04-26
- **Resolution**: Bounded swap search (5 candidate swaps + the no-op default).
  First non-violating arrangement wins; if all 6 violate, ship the default
  and log a `WARN`. Also: builds local copies of `top6` so the caller's list
  is never mutated. Added `test_split_into_slices_does_not_mutate_caller_list`,
  `test_split_into_slices_finds_non_violating_via_later_swap`, and
  `test_split_into_slices_warns_when_no_arrangement_works`.

### GAP-007 — `claude_verdict` and `ollama_verdict` are stubs ✅ DONE 2026-04-26
- **Resolution**: Both bodies wired with full anthropic SDK and Ollama HTTP
  integration. `claude_verdict` builds a system prompt describing the
  verdict taxonomy + JSON schema, calls `messages.create`, parses the
  structured response. `ollama_verdict` POSTs to `localhost:11434/api/generate`
  with `format: "json"` for structured output. Both fall back to
  `NotImplementedError` cleanly when key/SDK/daemon is missing — router
  catches and degrades to heuristic. 7 new tests with mocked Anthropic SDK
  (via `types.ModuleType` injection) and mocked `urlopen`. Same pattern
  applied to `claude_propose` and `ollama_propose` which were also stubs
  in `proposer.py` — both now have working bodies + 4 new tests each.

### GAP-008 — `forge_runner` blocks; no streaming output ✅ DONE 2026-04-26
- **Resolution**: New `_run_streaming()` helper using `subprocess.Popen` +
  consumer threads for stdout/stderr. `ForgeRunner.run` accepts `stream=True`
  (echoes each line as it arrives) and/or `on_line=callback` (per-line
  hook for progress bars / log files). Default behavior unchanged — when
  neither flag is set, the battle-tested blocking path runs. Stderr is
  consumed in parallel to avoid pipe-deadlock. 7 new tests in
  `test_forge_runner.py` mock `subprocess.Popen`.

### GAP-009 — `edhrec_client.py` doesn't exist ✅ DONE 2026-04-26
- **Resolution**: New module fetches `edhrec.com/commanders/<slug>` and
  extracts the embedded `__NEXT_DATA__` JSON blob. Walks the blob recursively
  for cardlists (top cards / high synergy / new cards) since EDHREC's schema
  shifts across page versions. 24-hour disk cache. Tolerant of missing
  fields — schema changes degrade to empty results rather than crashing the
  caller. 13 tests with mocked HTTP.

### GAP-010 — `report.py` doesn't exist ✅ DONE 2026-04-26
- **Resolution**: New `report.py` module + `commander-history` CLI.
  `render_deck_history(deck_id)` reads the iteration chain and renders a
  Markdown doc with: deck header, verdict tally, win-rate trajectory line
  ("40% → 70% (+30% over 5 iterations)"), per-iteration sections with
  card-diff tables, sim summary line (auto-detects ComparisonReport vs
  MatchupReport shape), verdict badge, and rationale + analyst notes.
  `render_recent_iterations_summary()` for cross-deck table view.
  `--output <path>` flag for writing to disk. 20 tests.

### GAP-011 — Audit prompt's `audit_manifest.json` writeback isn't actually wired ✅ DONE 2026-04-26
- **Resolution**: New JS snippet in the Closing Summary uses
  `URL.createObjectURL` + `<a>.click()` to trigger a one-click manifest
  download. Filename matches the convention `proposer.manual_propose`
  expects (`[USER] <safe_name> [B<n>].dck.audit_manifest.json`). Sanitization
  matches `moxfield_import.safe_filename` so the download lands next to the
  re-imported deck.

---

## Tier 3 — Ops, UX, test coverage

### GAP-012 — No integration test for `iteration_loop.run_one_iteration` ✅ DONE 2026-04-26
- **Resolution**: 5 new tests in `test_iteration_loop.py` mock at the
  `compare` boundary (cleaner than mocking `ForgeRunner.run` since
  `compare_versions.compare` already builds the ComparisonReport from raw
  data). Coverage: kept verdict, reverted verdict, inconclusive draw-heavy
  case, parent_id chaining, deck_snapshot blob persistence. The
  draw-heavy test specifically replicates the real Hakbal-vs-Hash signal
  from the smoke test (1W/1W/18D over 20 games) so we have a regression
  guard against the analyst's `decks_drew_too_often` lesson disappearing.

### GAP-013 — No unit tests for `forge_runner.ForgeRunner.run` or `compare_versions.compare` ✅ DONE 2026-04-26
- **Resolution**: `forge_runner` was covered earlier by GAP-008's 7 tests.
  This round added 4 new tests for `compare_versions.compare` mocking at the
  runner boundary: full integration with hand-crafted Forge stdout (verifies
  log_parser → game_analyzer → aggregation → JSON write), plus rejection
  paths for same-old-and-new, missing source deck, and invalid mode. CI is
  now Forge-independent.

### GAP-014 — No top-level status command ✅ DONE 2026-04-26
- **Resolution**: New `status.py` module + `commander-status` entry point.
  Reports decks-per-bracket (with [USER] vs filler split), curated pools
  on disk, recent run_match + compare reports, and knowledge_log stats +
  latest 5 iterations. `--json` flag for machine-readable output. 13 tests.
  Verified against real state: 337 decks across 4 brackets, 1 prior
  matchup, 1 prior comparison.

### GAP-015 — No CI / GitHub Actions ✅ DONE 2026-04-26
- **Resolution**: `.github/workflows/test.yml` runs the suite on push +
  PR + manual dispatch. Matrix: `[ubuntu-latest, windows-latest]` ×
  `[3.10, 3.11, 3.12]`. Steps: install editable + dev extras, run pytest,
  verify all 20 modules import. No secrets needed since the suite is
  offline-only.

### GAP-016 — README doesn't reflect Phase 2 workflow
- **File**: `README.md`
- **What**: Points at Phase 1A docs. With Phase 2 mostly done, the
  "Getting started" walkthrough should reflect actual usage:
  import → snapshot → audit → import → snapshot → compare → record → analyze.
- **Effort**: ~30 lines of Markdown.

### GAP-017 — Rollback automation ✅ DONE 2026-04-26
- **Resolution**: New `revert_to.py` module + `commander-revert` CLI.
  `revert_to_iteration(id)` writes the snapshot blob to disk and generates
  a Moxfield textarea blob ready for paste. By default records the revert
  as its own iteration row so the audit chain stays intact. `--to-deck`
  + `--version` for human-friendly invocation by deck publicId. 8 tests.
  Fully automated except for the final paste-into-Moxfield step (gated on
  `_api_push` availability — GAP-022).

### GAP-018 — Game Changers list is hardcoded; "fetch dynamically" is TODO ✅ DONE 2026-04-26
- **Resolution**: New `game_changers.py` module fetches from WotC's
  Commander Brackets announcement page, parses card names from `<li>` items
  with shape heuristics (filters sentences, lowercase-start, overlong text).
  7-day disk cache, falls back to bundled list on network/parse failure.
  `load_game_changers()` is the public entry; `is_game_changer(name)` is the
  one-shot lookup. 8 tests. List currently `union`'d with bundled fallback
  so a parser regression can't shrink it; bundled list mirrors the audit
  prompt for sync.

---

## Tier 4 — Deferred until prerequisites exist

### GAP-019 — ML training (Phase 3)
- **Status**: Premature. `knowledge_log` has 1 row today. Need 200+ across
  5+ decks before training is meaningful.
- **What unblocks it**: GAP-005 + several months of running iterations.

### GAP-020 — Forge sim seed for reproducibility
- **What**: Forge 2.0.12 has no `--seed` flag, so we can't reproduce a
  specific game's outcome. Mitigation today: average over enough games that
  variance smooths.
- **What unblocks it**: Newer Forge release that exposes a seed, OR a JVM
  agent that intercepts `Random` seeding.
- **Effort when ready**: Small (a flag in `forge_runner`) once Forge
  supports it.

### GAP-021 — Concurrent sims (parallel JVMs)
- **What**: Two Forge JVMs in parallel could halve curation wall time.
- **Why it's not done**: Forge writes `forge.log` shared across runs;
  concurrent writes might race. Needs feasibility check on per-process
  `cwd` + isolated profile dirs.
- **What unblocks it**: A 30-min spike to test whether two `cwd`-isolated
  Forge processes interfere with each other.

### GAP-022 — Moxfield API push (`_api_push`) ❌ WON'T-DO 2026-04-26
- **Resolution**: Decision made — this is a personal project. The clipboard
  textarea workflow (via `moxfield_push.prepare_push`) IS the final design.
  `_api_push` stays as a permanent `NotImplementedError` since there's no
  public Moxfield write API and capturing auth tokens isn't worth the
  fragility for personal-use scope. Removed from FUTURE_PLANS.md.

---

### GAP-029 — EDHREC average-deck fetch + suggestion quality pass ✅ DONE 2026-04-26
- **What**: Three real bugs surfaced from running the meta-test on Hakbal:
  EDHREC's "Average Deck" never fetched (looked for Moxfield URLs in
  EDHREC's blob, but those decks live ON edhrec.com); n=1 reference produced
  a noisy 64-card must-add list including Arcane Signet (universal staple);
  flat list with no ranking made it hard to act on.
- **Resolution**:
  - New `edhrec_client.fetch_average_deck()` hits
    `/average-decks/<slug>/<bracket>/<budget>` directly. Auto-discovery
    tries 3 specificity levels with fallback. Cached on disk.
  - New `_classify_card_role()` heuristic + `UNIVERSAL_STAPLES` frozenset.
    Sol Ring / Arcane Signet / basics / Command Tower etc. are now
    excluded from must-add AND off-meta because they're noise.
  - `CardSuggestion` dataclass replaces flat strings. Each suggestion
    carries `in_n_references` / `total_references` / `role`. Output
    sorted by frequency desc.
  - `must_add_by_role()` groups suggestions by finisher / tutor / wipe /
    removal / counter / draw / ramp / lord / other.
  - "All draws" framing fixed: 0-0-N now says "neither could close",
    not "roughly even".
  - `--reference-url` smart-routes EDHREC URLs through the new fetcher.
  - 370 tests (was 365); +5 new for staples filter, frequency labels,
    role grouping.

### GAP-027 — Local improvement advisor (no browser needed) ✅ DONE 2026-04-26
- **What**: A way to figure out swap recommendations without needing a
  browser-Claude session running the audit prompt.
- **Resolution**: New `improvement_advisor.py` module + `commander-advise`
  CLI. Pulls EDHREC stats via `edhrec_client` (HTML scrape, no Moxfield
  API), Scryfall metadata, and prior match history from `_matches/` +
  `knowledge_log`. Heuristic backend recommends adds/cuts based on
  inclusion-% deltas; LLM backend (Claude) synthesizes contextual
  recommendations using the diagnosis + EDHREC data. Output flows directly
  into `iteration_loop` via the `audit_manifest` schema. 18 tests.

### GAP-028 — Meta-reference benchmarking ✅ DONE 2026-04-26
- **What**: User wanted to pull the most-liked Moxfield deck and EDHREC's
  canonical reference for their commander, run those head-to-head against
  their own deck, and identify "must-add" cards (in winning references,
  missing from user's deck).
- **Resolution**: New `meta_test.py` module + `commander-meta-test` CLI.
  Auto-fetches Moxfield top-likes (via the existing public read API's
  `commanderName` search) + EDHREC's "Average Deck" URL (parsed from the
  commander page's `__NEXT_DATA__` blob). Imports references with `[REF]`
  prefix so they don't pollute the `[USER]`/filler namespaces. Runs
  `compare_versions` against each. Set arithmetic produces `must_add` (in
  ALL refs, not user), `consider` (in some), `off_meta` (in user, in no
  refs). 16 tests; helper `find_top_liked_deck_for_commander` added to
  `moxfield_import` with 3 tests. Honest framing in the module docstring
  on "saltiest deck from EDHREC" — that view doesn't exist; we use
  EDHREC's "Average Deck" sample as the canonical reference instead.

### GAP-026 — Knowledge log export/import for backup ✅ DONE 2026-04-26
- **Resolution**: New `export.py` module + `commander-export` CLI.
  `export_knowledge_log(path, deck_id=None, recent=None)` writes a portable
  JSON dump (full / per-deck / recent-N filtering). `import_knowledge_log(path)`
  re-ingests an exported file with skip-existing-by-default semantics so
  re-running an import doesn't duplicate. Useful for backups, machine
  migration, or sharing a deck's iteration chain. 11 tests.

## Discovered during PM session

### GAP-023 — LICENSE not chosen ⏸ LOW-PRIORITY 2026-04-26
- **Resolution scope**: Personal project, not slated for public release.
  License decision is low-priority — `pyproject.toml`'s `license = "TBD"`
  is acceptable for personal use. If/when the project ever goes public,
  add a `LICENSE` file (MIT is the safe default) and update pyproject.

### GAP-024 — Legacy `deck_id` migration not built ✅ DONE 2026-04-26
- **Resolution**: New `migrate_legacy_deck_ids(db_path, dry_run=False)` in
  `knowledge_log`. Walks the iterations table, finds rows whose `deck_id`
  matches `[B<n>].dck`, extracts the publicId from the row's
  `deck_snapshot` `Moxfield=` line, and updates. Returns a structured
  report (scanned / updated / would_update / skipped / details). Dry-run
  mode for safe inspection. Rows without `Moxfield=` metadata are skipped
  with a recorded reason (legitimately legacy — pre-publicId imports).
  4 tests.

### GAP-025 — Heuristic archetype classifier may misclassify common B3 builds ✅ DONE 2026-04-26
- **Resolution**: `_AGGRO_KEYWORDS` expanded with 14 more tribal types
  (merfolk, elf/elves, spirit/spirits, dragon/dragons, angel/angels,
  wizard/wizards, zombie/zombies, cat/cats, dinosaur/dinosaurs, human/humans,
  elemental/elementals) and additional commander names (hakbal, kumena,
  king narfi, brion). Also added more aggressive keywords (prowess, first
  strike, attack triggers). The classifier now catches the previously-missed
  tribal aggro shapes. LLM escalation (`claude_archetype` body fill)
  remains future work — adequate signal from the heuristic for now.

## Recommended next 3 (rolling — updated as items land)

**Closed this session (cumulative, 19 items): GAPs 001, 002, 003, 004, 005,
006, 007, 008, 009, 011, 012, 013, 014, 015, 016, 017, 018, 024, 025.**

Plus new this session: `commander-doctor` (GAP-014 was status; doctor is
the verify-environment companion, not on backlog originally). And the
Forge replacement question moved to FUTURE_PLANS.md as FP-001.

Open items remaining (all blocked on external dependencies / decisions):

1. **GAP-022** Moxfield API push (`_api_push`) — needs auth-token capture
   workflow. PARKED in FUTURE_PLANS.md as FP-005.
2. **GAP-023** LICENSE choice — needs user input.

Tier 4 (deferred by design, see FUTURE_PLANS.md):

- **FP-002 / GAP-019** Phase 3 ML training — premature, 1 row today
- **FP-003 / GAP-021** Concurrent sims feasibility — needs Forge spike
- **FP-004 / GAP-020** Forge sim seed — Forge limitation
- **FP-005 / GAP-022** Moxfield API push — needs auth solution

The active backlog has effectively reached steady-state. New work will
either be net-new feature ideas or items moving out of FUTURE_PLANS.md
when their unblock conditions fire.
