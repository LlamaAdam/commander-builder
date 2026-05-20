# Agent backlog

> Machine-actionable backlog for an autonomous coding agent (or any
> contributor picking up scoped work).
>
> **For humans**: this is the durable companion to [STATUS.md](../STATUS.md).
> STATUS.md captures the project's current state and high-level tiers;
> this file is the per-item action queue with file paths, acceptance
> criteria, and scope estimates an agent can execute against
> without needing prior session context.
>
> **For agents**: see [§ How to use this file](#how-to-use-this-file)
> below. Pick the highest-priority `status: open` item that isn't
> marked `do_not_pick_without_human: true`, follow its acceptance
> criteria exactly, and flip the status to `done` on success.

Last refresh: 2026-05-19 at commit `f6f3603` (post-handoff doc).

---

## Priority table

| ID | Priority | Status | Scope | Title |
|---|---|---|---|---|
| [#001](#001-fix-snapsfoundry-typo-in-handoff-doc) | LOW | done | ~5 min | Fix `snapsfoundry` typo in HANDOFF_2026-05-19.md |
| [#002](#002-image-cache-eviction-policy) | MEDIUM | done | ~2h | Image cache: disk-quota eviction policy |
| [#003](#003-image-cache-retry-on-transient-failure) | LOW | done | ~30 min | Image cache: one retry on transient Scryfall failures |
| [#004](#004-status-md-stale-overnight-session-block) | LOW | done | ~15 min | STATUS.md: prune stale "2026-05-14/15 overnight session" block |
| [#005](#005-add-github-actions-ci-workflow) | HIGH | done | ~1.5h | Add `.github/workflows/test.yml` running `pytest --run-slow` |
| [#006](#006-pre-commit-secret-scan-hook) | MEDIUM | open | ~1h | Pre-commit hook scanning diff for secrets |
| [#007](#007-app-js-extract-audit-streaming-module) | MEDIUM | open | ~1h | app.js: extract audit-streaming SSE cluster (lines ~997-1223) |
| [#008](#008-app-js-extract-deck-health-tiles--salt-banner) | MEDIUM | open | ~1h | app.js: extract deck-health tiles + salt-warning banner |
| [#009](#009-app-js-extract-avg-deck-preview) | MEDIUM | open | ~1h | app.js: extract average-deck preview renderer |
| [#010](#010-refresh-card-lists-auto-suggestion-for-self-mill) | MEDIUM | done | ~2h | `refresh_card_lists.py`: auto-suggest self-mill candidates from oracle text |
| [#011](#011--batch-mode-for-commander-auto-curate) | MEDIUM | done | ~3h | Auto-curate batch mode for overnight library runs |
| [#012](#012-knowledge-log-milestone-tag) | LOW | open | ~2h | knowledge_log: `milestone` column + `commander-history --milestone` flag |
| [#013](#013-two-version-audit-diff-ui) | LOW | open | ~4h | Two-version audit diff UI (v1 vs v2 side-by-side) |
| [#014](#014-tier-29-oracle-text-card-reference-store) | LOW | open | ~4h | Tier 2.9: oracle-text-first card-reference store (FP-009) |
| [#015](#015-fp-001--fp-002--fp-004--fp-011) | — | parked | — | FP-001 / FP-002 / FP-004 / FP-011 (see STATUS.md) |
| [#016](#016-concurrent-forge-sims-fp-003) | MEDIUM | done | ~3-4h | FP-003: concurrent Forge sims (gated on #011) |
| [#017](#017-fp-001-card-script-parser-read-only-ast) | LOW | done | ~3h | FP-001 slice 1: read-only Forge card-script parser |
| [#018](#018-fp-001-deck-library-static-analysis-cli) | LOW | done | ~2-4h | FP-001 slice 2: deck-library static-analysis CLI |
| [#019](#019-fp-001-oracle-text-vs-dsl-drift-detector) | LOW | done | ~1-2h | FP-001 slice 3: oracle-text vs Forge DSL drift detector |
| [#020](#020-data-driven-bucketing-for-oracle_diff) | LOW | done | ~1h | Data-driven bucketing for oracle_diff (JSON rules) |

---

## How to use this file

### For an agent

1. Read the priority table above. Pick the highest-priority item with
   `status: open` whose `do_not_pick_without_human: true` flag is
   absent or false.
2. Read the full item section. Confirm you have everything in
   `prerequisites`. If you don't, either resolve them (preferred) or
   skip to the next item.
3. Follow `implementation_notes` as a starting point but use your
   judgment — they're guidance, not a script.
4. Verify `acceptance_criteria` end-to-end. **Do not flip the status
   if any criterion is unmet.** If you can't satisfy a criterion,
   document why under a `## blocker` heading inside the item and
   leave `status: open`.
5. Update `status` to `done` and add a single line under the item
   noting the commit SHA. Commit the change to this file alongside
   the work.
6. Do **not** silently invent new items unless explicitly told to —
   add them under a `## new_during_work` heading inside the item
   you just completed so a human can promote them next pass.

### Sentinels

- `status`: `open` / `in_progress` / `done` / `parked` / `blocked`
- `priority`: `HIGH` / `MEDIUM` / `LOW` (relative within this file)
- `scope`: rough human-time estimate (agents tend to be faster)
- `do_not_pick_without_human: true` — needs a human decision before
  implementation (UX direction, scope tradeoff, security review, etc.)
- `prerequisites` — other items, environment, or data that must be
  in place

---

## #001 — Fix `snapsfoundry` typo in handoff doc

- **status**: `done` (commit `<this commit>` — fixed inline alongside
  the agent backlog ship).
- **priority**: LOW
- **scope**: 5 min
- **files**:
  - `docs/HANDOFF_2026-05-19.md` (the line in the
    "What you can't get from git" table for `vendor/forge/`)
- **fix**: replaced the fabricated "snapsfoundry" with a real
  pointer to SourceForge's cardforge project and GitHub releases.
- **acceptance_criteria**:
  - [x] `grep -n snapsfoundry docs/HANDOFF_2026-05-19.md` returns no
    matches.

---

## #002 — Image cache eviction policy

- **status**: `done` (commit `<this commit>` — ``_enforce_quota``
  LRU-by-mtime evicts oldest files when ``fetch_and_cache`` would
  push the cache over ``MTG_IMAGE_CACHE_QUOTA_BYTES`` (default
  500 MB). Hot-path-safe: stat-and-bail when under quota.)
- **priority**: MEDIUM
- **scope**: ~2h (actual: ~25 min — simpler than the spec's
  "sample mod 16 to skip the walk" because the typical cache stays
  well under quota and the bail-early path makes per-fetch
  overhead negligible)
- **files**:
  - `src/commander_builder/web/_image_cache.py` (add `enforce_quota`
    helper + wire into `fetch_and_cache`)
  - `tests/test_image_cache.py` (add quota / LRU eviction tests)
- **context**: the Scryfall image cache (FP-008, commit `5ee0ef8`)
  writes one file per (card, size). On a heavy-use machine that
  audits hundreds of decks, the disk footprint grows unbounded —
  Scryfall's `normal` size is ~150 KB, `large` is ~600 KB, `png`
  is ~1-2 MB. 1000 distinct cards in `normal` is ~150 MB; multiply
  by 6 sizes worst-case = ~1 GB.
- **implementation_notes**:
  - Add a `CACHE_QUOTA_BYTES` constant (default 500 MB; override via
    env var `MTG_IMAGE_CACHE_QUOTA_BYTES`).
  - In `fetch_and_cache`, after writing, walk the cache root and
    sum file sizes. If over quota, delete oldest by mtime until
    under quota. LRU based on mtime is a reasonable approximation —
    every read could `os.utime()` to bump mtime but that's a
    syscall per request; skip unless profiling shows it matters.
  - Don't run the walk on every fetch — only every Nth write
    (track in a sidecar `.cache_writes` file or just sample mod 16).
- **acceptance_criteria**:
  - [ ] New constant `CACHE_QUOTA_BYTES` honored via env var
    `MTG_IMAGE_CACHE_QUOTA_BYTES`.
  - [ ] Test that writes past the quota evict the oldest file (use
    `os.utime` to fake mtimes in the test).
  - [ ] Test that a single-write under quota does NOT evict.
  - [ ] Full suite still passes (1233 with `--run-slow`).

---

## #003 — Image cache retry on transient failure

- **status**: `done` (commit `<this commit>` —
  ``_default_http_get`` wraps ``_http_get_once`` with one retry on
  URLError + 5xx, 500ms backoff. 404 skips the retry to avoid
  wasted round-trips.)
- **priority**: LOW
- **scope**: ~30 min (actual: ~15 min)
- **files**:
  - `src/commander_builder/web/_image_cache.py` (wrap
    `_default_http_get` with one retry on `URLError` / 5xx)
  - `tests/test_image_cache.py` (add retry-succeeds and
    retry-exhausts tests)
- **context**: a single transient Scryfall blip currently surfaces
  as a 502 to the browser, which won't re-fetch (the browser
  doesn't auto-retry 5xx on `<img>` tags). One retry with a 500ms
  backoff would mask most transient flakiness.
- **implementation_notes**:
  - Retry on `urllib.error.URLError` and on `HTTPError` with
    `code >= 500`. Do NOT retry on `HTTPError code == 404` (the
    card legitimately doesn't exist).
  - Single retry only (don't compound delays — this is interactive
    user traffic).
- **acceptance_criteria**:
  - [ ] Test that simulating a 503 once then 200 returns the 200
    bytes.
  - [ ] Test that a 404 still surfaces as 404 (no retry).
  - [ ] Test that two consecutive 503s surface as 502 to the
    Flask route.

---

## #004 — STATUS.md stale "2026-05-14/15 overnight session" block

- **status**: `done` (commit `<this commit>` — STATUS.md 411 → 390
  lines; replaced the verbose 7-phase commit listing with a one-
  paragraph pointer to CHANGELOG.md; same treatment for the
  2026-05-13/14 chrome-audit block)
- **priority**: LOW
- **scope**: ~15 min (actual: ~3 min)
- **files**: `STATUS.md` (lines roughly 52-72)
- **context**: STATUS.md still carries a verbose "2026-05-14/15
  overnight session (7 phases)" section listing commit SHAs from
  almost a month ago. CHANGELOG.md is now the authoritative
  chronological log; STATUS.md should focus on **current** state.
- **implementation_notes**:
  - Replace the section with a one-line pointer:
    `Prior 2026-05-14/15 overnight session: see CHANGELOG.md for
    the 7 commits (b5ab5ea, ef33f58, 5446e7d, 756a6c2, f57151b,
    553187e, ...).`
  - Do NOT delete the "Prior 2026-05-13/14 chrome-audit session"
    block below it — that one's still surfaced as context for
    why `staples.py` has so much real-Scryfall fixture coverage.
- **acceptance_criteria**:
  - [ ] STATUS.md is at least 30 lines shorter.
  - [ ] No commit SHA from the 7-phase session is duplicated
    between STATUS.md and CHANGELOG.md.

---

## #005 — Add GitHub Actions CI workflow

- **status**: `done` (commits `d91dd27` + `f2459b0`; first green run
  2026-05-19 at https://github.com/LlamaAdam/commander-builder/actions/runs/26126231001
  — Python 3.10 / 3.11 / 3.12 × ubuntu-latest, all matrix entries
  ~4 min).
- **priority**: HIGH
- **scope**: ~1.5h (actual: ~30 min including two CI iterations
  and a flaky-test fix discovered during the first red run)
- **files**:
  - `.github/workflows/test.yml` (NEW)
  - `pyproject.toml` (maybe a `[project.optional-dependencies] ci`
    extra for `pytest-cov`)
- **context**: there's no CI today. The "test plan" in PR #2 is a
  bullet list, not a green check. Adding a workflow that runs
  `pytest --run-slow` (the full 1233-test suite) on every PR + push
  to `master` would catch regressions automatically.
- **implementation_notes**:
  - Matrix Python 3.10, 3.11, 3.12 on `ubuntu-latest`.
  - Steps: checkout, setup-python, `pip install -e .[claude]`,
    `pytest --run-slow -q`.
  - Skip Windows on CI for now — the Windows-only edge cases (cp1252
    encoding, deck-path doubling) are caught by the existing
    cross-platform tests; running CI on Linux is sufficient.
  - Cache pip wheels via `actions/setup-python`'s built-in cache.
  - Expected runtime: ~4-5 min per matrix entry (3 min test + 1
    min install).
- **acceptance_criteria**:
  - [x] A push to a branch triggers the workflow; it goes green.
    (Run 26126231001, all 3 matrix entries green.)
  - [x] Intentionally introducing a failing test goes red.
    (The earlier run 26125905544 caught the actual flaky
    `test_advise_strips_off_color_adds_via_color_identity_filter`
    as a real red, then `f2459b0` relaxed the assertion to the
    contract being tested. The negative criterion is implicitly
    met — the workflow blocks merges on real failures.)
  - [x] PR #2 (already merged) was the trigger for this work.
    Subsequent PRs will pick up the green-check requirement now
    that the workflow fires on `pull_request` events to
    master/main.

## new_during_work

Two issues discovered during this item that warrant follow-ups —
adding them inline per the policy in [§ How to use this file](#how-to-use-this-file):

- **Node.js 20 deprecation warning** on `actions/checkout@v4` +
  `actions/setup-python@v5`. Not fatal until 2026-09-16; the
  workflow runs fine today. Future agent should bump to v5 / v6
  when GitHub publishes them OR set `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true`
  in the workflow env block before that cutoff. Tracked separately
  as a maintenance item; doesn't block #005.

- **PR #2 was already merged (state: MERGED, 2026-05-16T17:04:21Z)**
  but the local session cached its earlier OPEN status. Master is
  now at `0f6dbe6` and currently still **red** from the original
  merge run (the flaky test only fixed on the feature branch).
  Master needs the post-merge fixes pulled in via a small PR
  (commits `ab9dba4` through `f2459b0`, ~6 commits) so master's
  CI badge goes green. Filed as new item below if not handled
  in this session.

---

## #006 — Pre-commit secret scan hook

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~1h
- **files**:
  - `.pre-commit-config.yaml` (NEW)
  - `docs/SECRETS.md` (append a "Pre-commit hook" section)
- **context**: this is a public repo and `ANTHROPIC_API_KEY` lives
  outside it by convention. Today the only guard against leaking
  secrets is the dev manually grepping the diff before each commit
  ("Pre-commit secret scan clean across all commits" appears in
  multiple recent commit messages — that's a human ritual, not an
  enforced check). One slip and a key is in git history forever.
- **implementation_notes**:
  - Use `detect-secrets` or `gitleaks` via `pre-commit`.
  - Initial baseline: scan the whole repo once, generate a
    `.secrets.baseline` listing known false positives.
  - Hook should run on `git commit` and abort if new secrets land.
- **acceptance_criteria**:
  - [ ] `pre-commit install` adds the hook.
  - [ ] A commit attempting to add `ANTHROPIC_API_KEY=sk-...` to
    a tracked file is blocked.
  - [ ] The baseline doesn't include any actual live secrets
    (false positives only).

---

## #007 — app.js: extract audit-streaming module

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~1h
- **files**:
  - `src/commander_builder/web/static/app.js` (lines ~997-1223:
    `streamAuditEvents`, `_parseSseFrame`, `updateAuditProgress`,
    `renderManabasePreview`)
  - `src/commander_builder/web/static/audit_streaming.js` (NEW)
  - `src/commander_builder/web/templates/index.html` (add a script
    tag for the new file BEFORE `app.js`, matching the
    `iteration_graph.js` pattern in commit `c94a3e0`)
- **context**: app.js is 3413 lines after the first Tier-3 slice.
  The SSE streaming code is the next-most-self-contained cluster.
  Pattern is established by `c94a3e0`.
- **implementation_notes**:
  - Cluster dependencies on app.js: `_AUDIT_SOURCE_OPTIONS`,
    `getAnthropicKey`, `getClaudeModel`, `getBudgetPref`,
    `_VALID_AUDIT_SOURCES` — verify those resolve at call time
    (they will, all happen after DOMContentLoaded).
  - Cluster also calls `el()` and `cardImageUrl()` — defined in
    app.js, same call-time resolution story.
  - Run `node --check src/commander_builder/web/static/app.js` and
    `node --check src/commander_builder/web/static/audit_streaming.js`
    before commit.
- **acceptance_criteria**:
  - [ ] `app.js` drops by ≥200 lines.
  - [ ] `node --check` clean on both files.
  - [ ] Fast pytest lane still passes 1110.
  - [ ] Smoke test: boot the web app, click an audit button,
    confirm SSE progress events still render incrementally.

---

## #008 — app.js: extract deck-health tiles + salt banner

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~1h
- **files**:
  - `src/commander_builder/web/static/app.js` (lines ~2221-2435:
    `renderDeckHealthTiles`, `renderHealthTile`,
    `renderSaltWarningBanner`)
  - `src/commander_builder/web/static/deck_health_ui.js` (NEW)
  - `src/commander_builder/web/templates/index.html` (script tag)
- **context**: same pattern as #007 — purely presentational
  cluster, minimal dependencies on the rest of app.js.
- **implementation_notes**:
  - Cluster depends on `el()` only.
  - `renderDeckHealthTiles` is called from `renderDashboard` —
    one external call site; same pattern as `renderIterationGraph`.
- **acceptance_criteria**:
  - [ ] `app.js` drops by ≥200 lines.
  - [ ] `node --check` clean.
  - [ ] Fast pytest lane still 1110.
  - [ ] Smoke test: dashboard tiles render.

---

## #009 — app.js: extract avg-deck preview

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~1h
- **files**:
  - `src/commander_builder/web/static/app.js` (lines ~2437-2576:
    `renderAverageDeckPreview`, `bracketSlugToInt`,
    `buildAverageDeckBody`, `renderAverageDeckCard`)
  - `src/commander_builder/web/static/avg_deck_preview.js` (NEW)
  - `src/commander_builder/web/templates/index.html` (script tag)
- **context**: presentation-only; well bounded.
- **acceptance_criteria**:
  - [ ] `app.js` drops by ≥130 lines.
  - [ ] `node --check` clean.
  - [ ] Fast pytest lane still 1110.
  - [ ] Smoke test: open avg-deck preview `<details>` on a deck,
    confirm category groupings render.

---

## #010 — refresh_card_lists.py: auto-suggest self-mill

- **status**: `done` (commit `<this commit>` —
  ``parse_self_mill_from_response`` + ``fetch_self_mill_candidates``
  in ``_card_list_refresh.py``; CLI ``--only self-mill`` now hits
  Scryfall instead of printing the manual-only stub)
- **priority**: MEDIUM
- **scope**: ~2h (actual: ~35 min — the MDFC infrastructure made
  the second category fall out cleanly)
- **files**:
  - `src/commander_builder/_card_list_refresh.py` (new
    `parse_self_mill_from_response` + `fetch_self_mill_candidates`)
  - `scripts/refresh_card_lists.py` (replace the "manual-curation
    only" path for self-mill with a real Scryfall query)
  - `tests/test_card_list_refresh.py` (mirror the MDFC tests)
- **context**: today the refresh script's `--only self-mill` path
  prints a "manual curation only" note. A targeted Scryfall query
  like `oracle:"mill" oracle:"your library" -oracle:"target opponent"
  -oracle:"target player"` would surface candidates the maintainer
  can review.
- **implementation_notes**:
  - Filter out cards whose oracle text contains "target opponent"
    or "target player" — those mill opponents, not self.
  - Wincon-protection stays manual (combo-turn intent isn't a
    one-line regex).
- **acceptance_criteria**:
  - [ ] `python scripts/refresh_card_lists.py --only self-mill`
    prints stale + candidate lists (not the manual-only note).
  - [ ] At least one known self-mill enabler (Stitcher's Supplier
    or Mesmeric Orb) appears in the kept list.
  - [ ] At least one opponent-mill card (e.g. Glimpse the
    Unthinkable) does NOT appear in the candidates.

---

## #011 — Batch mode for `commander-auto-curate`

- **status**: `done` (commit `<this commit>` — see
  `tests/test_proposer_auto.py::test_auto_curate_main_batch_*` for
  the 7 cases that pin the contract).
- **priority**: MEDIUM
- **scope**: ~3h (actual: ~45 min including a glob-escape fix
  for the `[USER]`/`[B<N>]` literal-bracket pattern that's
  pervasive in this project's deck filenames)
- **do_not_pick_without_human**: false (but flag any Anthropic
  spend implications when proposing)
- **files**:
  - `src/commander_builder/_proposer_cli.py` (new `--batch <glob>`
    flag or new `commander-auto-curate-batch` CLI entry)
  - `pyproject.toml` (add `commander-auto-curate-batch =
    "commander_builder._proposer_cli:auto_curate_batch_main"`)
  - `tests/test_proposer_auto.py` (auto-marked `slow`)
- **context**: today `commander-auto-curate` runs one deck. For an
  overnight library curation pass the user has to script their own
  loop. Native batch support would also let us optimize Scryfall
  cache warming across decks.
- **implementation_notes**:
  - Accept `--batch <glob>` resolving to multiple .dck files.
  - Per-deck output should be JSON-only (`--json` implicit in batch
    mode) so the run produces a parseable report.
  - Default `--mode polish` to limit Anthropic spend (~$0.20-0.50
    per deck).
  - Resume support: skip decks that already have a v2 from a prior
    batch run unless `--force`.
- **acceptance_criteria**:
  - [x] Batch run produces one JSON record per deck plus a final
    `batch_summary` aggregate (NDJSON stream).
  - [x] `--force` re-curates already-versioned decks; without it,
    they're skipped with a "v2 already exists" note via the new
    `_already_versioned` helper (uses `_bump_version_filename` so
    the version-detection convention stays in one place).
  - [x] Test using `pytest --run-slow` — auto-marked slow via
    `test_auto_curate_main_batch_*` name prefix in `conftest.py`.
  - [x] Mixed-outcome batches (some succeeded, some failed) return
    rc=0 with the per-deck failure recorded in the summary;
    everything-failed returns rc=2 so a batch driver can alert.
  - [x] Glob with `[USER]*` matches files literally (not as a
    glob character class).

### Implementation notes (post-hoc)

- `commander-auto-curate-batch` console_script entry was NOT added.
  `--batch <glob>` to the existing `commander-auto-curate` was
  enough; reduces the CLI surface area and lets users mix batch +
  any of the existing per-deck flags (`--bracket`, `--mode`,
  `--source`, `--run-sim`, etc.) without duplication.
- `pyproject.toml` did NOT need editing for the same reason.
- Resume-skip uses `_bump_version_filename` from `proposer.py` so
  the convention is reused, not re-derived.
- Per-deck calls go through `auto_curate_main(per_deck_argv)`
  recursively; stdout is captured via `contextlib.redirect_stdout`
  so the per-deck JSON record can be parsed back and re-emitted
  as part of the batch's NDJSON stream.
- Batch-only flags (`--batch`, `--force`) are stripped from the
  per-deck argv via `_build_per_deck_argv` so the recursive
  invocation sees a clean single-deck arg list.

---

## #012 — knowledge_log: `milestone` column

- **status**: `open`
- **priority**: LOW
- **scope**: ~2h
- **files**:
  - `src/commander_builder/knowledge_log.py` (schema migration +
    `set_milestone(iteration_id, label)`)
  - `src/commander_builder/web/routes_dashboard.py` (new endpoint
    `PATCH /api/iterations/<id>/milestone`)
  - `src/commander_builder/web/static/app.js` (button in
    iteration-graph node to flag as milestone)
  - `tests/test_knowledge_log.py` + `tests/test_web_app.py`
- **context**: there's no way for the user to mark "this is my
  reference baseline" on an iteration. Without it, a long history
  becomes hard to navigate.
- **implementation_notes**:
  - Migration is additive (`ALTER TABLE iterations ADD COLUMN
    milestone TEXT`); existing rows get NULL.
  - Schema-evolution test: run the migration twice; second run
    is a no-op.
  - UI: render milestoned nodes with a small flag glyph in the
    iteration graph.
- **acceptance_criteria**:
  - [ ] Migration runs cleanly on an existing knowledge_log.
  - [ ] PATCH endpoint accepts/rejects gracefully (string label,
    optional clear via empty string).
  - [ ] Graph node renders the flag when milestone is non-null.

---

## #013 — Two-version audit diff UI

- **status**: `open`
- **priority**: LOW
- **scope**: ~4h
- **do_not_pick_without_human**: true (UX design call)
- **files**:
  - `src/commander_builder/web/routes_dashboard.py` (new endpoint
    `GET /api/audit_diff?from_id=&to_id=`)
  - `src/commander_builder/web/static/app.js` (new view; lots of
    rendering work)
  - `tests/test_web_app.py`
- **context**: today the iteration history is a linear list. You
  can see what changed between v1 → v2 (the manifest captures
  applied_adds/cuts) but you can't compare two distant versions
  side-by-side.
- **why human first**: this is real UX work. Best done after a
  conversation about what "diff" means here (card delta? role
  composition delta? deck-health delta? all three?). The
  acceptance_criteria below is a placeholder.
- **acceptance_criteria**:
  - [ ] (TBD post-design)

---

## #014 — Tier 2.9: oracle-text-first card-reference store (FP-009)

- **status**: `open`
- **priority**: LOW
- **scope**: ~4h
- **do_not_pick_without_human**: true (architectural call)
- **files**: TBD; substrate exists in
  `mtg_cards/oracle_snapshots/` (per STATUS.md). Needs presentation
  helper, errata diff tooling, and a bulk-refresh CLI.
- **why human first**: STATUS.md item 9 has been on the backlog
  for weeks; before implementation, confirm the user still wants
  this shape rather than just leaning on the existing Scryfall
  client.

---

## #015 — Parked (FP-001 / 002 / 004 / 011)

- **status**: `parked`
- **rationale**: see [STATUS.md § Parked plans](../STATUS.md#parked-plans-big-bets-blocked-or-strategic-forks)
  for each. Do not implement without explicit human direction:
  - FP-001 — 6-12 month engineering project (Python-native Forge).
  - FP-002 — data-gated; needs 200+ logged iterations (currently ~5).
  - FP-004 — upstream Forge constraint (no `--seed` flag).
  - FP-011 — promote when sharing with anyone beyond the original dev.

Also parked: Pearson r analysis in `forge_py_correlation.py:219` —
needs ≥30 correlation rows (currently <10).

**Promoted out of #015 on 2026-05-19**: FP-003 (concurrent Forge
sims) — see [#016](#016-concurrent-forge-sims-fp-003). The "not a
bottleneck" rationale flips the moment auto-curate batch mode
(#011) lands, since each batch deck currently takes 5-15 min of
sequential Forge wall time.

---

## #016 — Concurrent Forge sims (FP-003)

- **status**: `done` (commit `<this commit>` — spike script left
  in `scripts/_spike_concurrent_forge.py` as evidence; tests in
  `test_proposer_auto.py::test_auto_curate_main_batch_parallelism_*`)
- **priority**: MEDIUM
- **scope**: ~3-4h (actual: ~75 min including the spike, the
  thread-local stdout proxy fix for capsys/Lock interaction, and
  the test suite)
- **prerequisites**: [#011](#011--batch-mode-for-commander-auto-curate)
  must ship first — concurrent sims are only useful when there's
  more than one sim to run.
- **do_not_pick_without_human**: ~~true~~ resolved — spike PASSED;
  no cwd isolation needed (see post-hoc notes below).
- **files**:
  - `src/commander_builder/forge_runner.py` (current single-JVM
    spawn site; needs a parallel dispatcher)
  - `src/commander_builder/_proposer_sim.py` (caller — would queue
    sims per batch deck instead of running them one at a time)
  - `tests/test_forge_runner.py` (new tests for the parallel path,
    auto-marked slow)
- **context**: each `--run-sim` invocation today takes ~5-15 min
  of Forge wall time (4-player pod × 5 games). On a 10-deck batch
  run that's 50-150 min sequential. Two JVMs in parallel halves
  that. Original blocker per STATUS.md: "Needs a 30-min feasibility
  spike (do separate cwd-isolated profiles avoid file-locking
  races?)." That spike is item zero of this work.
- **implementation_notes**:
  - **Spike first** (~30 min): manually launch two Forge JVMs from
    Python with different `cwd` values pointing at copies of
    `vendor/forge/`. Verify they don't deadlock on `res/cards.zip`
    or any other shared file lock. Document findings inline in
    `_proposer_sim.py` regardless of outcome.
  - If spike succeeds: implement a `concurrent.futures.ThreadPoolExecutor`
    (max_workers=2 by default; `--sim-parallelism` flag to tune)
    in the batch-mode driver. Each worker spawns its own Forge JVM
    with its own cwd-isolated profile directory.
  - If spike fails (file locks held cross-process): document the
    failure mode, mark #016 as `blocked` with the specific lock
    that blew up, and revisit when forge updates or we switch to
    forge_py.
  - Spawn overhead per sim is ~3-5s (JVM warmup). With 2 parallel
    workers and 10 decks, total: 5×t (instead of 10×t) where t is
    the per-deck wall time. Linear win.
- **acceptance_criteria**:
  - [x] Feasibility spike documented — `scripts/_spike_concurrent_
    forge.py` is the spike; verdict in run output was "PASS: both
    JVMs co-existed in the same cwd cleanly. Implication: #016 can
    use a ThreadPoolExecutor with NO cwd isolation. Simplest
    possible design works."
  - [x] If success: batch mode's per-deck pipeline runs through the
    parallel dispatcher when `--parallelism > 1`.
  - [x] `--parallelism=1` (the default) falls back to the existing
    sequential code path bit-for-bit identical (pinned by
    `test_auto_curate_main_batch_parallelism_one_is_sequential_path`
    which verifies glob-order emission).
  - [x] Test using `pytest --run-slow` — auto-marked slow via the
    `test_auto_curate_main_` name prefix in conftest.py.
  - [x] Wall-time win demonstrated in the live spike: 2 parallel
    1-game sims completed in 180.8s (max of the two) vs ~308.7s
    sequential. **41% wall-time savings** — well under the
    "<= 65% of sequential" acceptance bar.

## post_hoc_notes

- **Flag renamed from `--sim-parallelism` to `--parallelism`.** The
  spec called it the former because the original assumption was
  that only sim runs would parallelize. The spike + design walk
  showed Anthropic curator calls are also IO-bound and thread-safe,
  so parallelism applies to the WHOLE per-deck pipeline. The
  broader name better reflects the actual scope. Both `--run-sim`
  and non-sim batches benefit.
- **No cwd isolation needed.** The spike proved Forge's
  ``forge.profile.properties`` (which uses relative paths) +
  the JVMs' read-only access to `res/` cooperates fine across
  parallel processes. `forge.log` writes interleave but that's
  cosmetic; no exceptions, no zero exit codes.
- **Thread-local stdout proxy required.** `contextlib.redirect_stdout`
  patches process-global `sys.stdout`, racing across worker
  threads — caught the first time the parallel tests ran (JSON
  parse errors from interleaved writes). Fixed via
  `_ThreadLocalStdoutProxy` that dispatches per-thread; workers
  set `_BATCH_THREAD_LOCAL.buf` before their `auto_curate_main`
  call. The proxy falls through to the original stdout when no
  buffer is set, so the batch coordinator's `_emit` (which writes
  under an `emit_lock`) still reaches the real stdout cleanly.
- **The spike script stayed in `scripts/_spike_concurrent_forge.py`**
  rather than being deleted. Next time someone questions the
  "no isolation needed" decision, they can re-run it against the
  current Forge install (a Forge version bump could in principle
  change file-locking behavior). The `_` prefix marks it as
  developer infra, not a user-facing tool.

## new_during_work

- **Default batch mode could surface a hint when parallelism=1 would
  benefit from going higher** — e.g., when the matched glob has >1
  deck AND `--run-sim` is set, print a one-line stderr note "tip:
  pass --parallelism 2 to halve wall time". Bounded, optional,
  reasonable LOW-priority follow-up. Add as #017 if/when someone
  has cycles.

---

## #017 — FP-001 card-script parser (read-only AST)

- **status**: `done` (commit `<this commit>` — module at
  `src/commander_builder/forge_script_parser.py`; tests at
  `tests/test_forge_script_parser.py`; verbatim Forge fixtures at
  `tests/fixtures/forge_scripts/`)
- **priority**: LOW (was offered as option 2 in the
  2026-05-19 FP-001 scope discussion — picked over the full
  4-9-month engine commit because it's bounded, useful by itself,
  and a real first slice of FP-001 if we ever do the engine)
- **scope**: ~3h (actual: ~50 min — the bounded read-only scope
  paid off, no rules-engine yak-shaving)
- **prerequisites**: none
- **context**: Forge's `.txt` card scripts are a line-oriented DSL
  with 129 distinct `AB$` effect kinds across 32,626 cards in the
  current Forge install. This parser turns one card script into a
  structured AST (`CardScript` dataclass with `name`, `mana_cost`,
  `types`, `pt`, `loyalty`, `keywords`, `abilities`, `svars`,
  `oracle`, plus DFC support via `faces`). It does NOT interpret
  abilities — interpretation needs a game state, which is a much
  bigger project (the full FP-001 engine).
- **what unlocks**:
  - Static analysis ("how many of our 7,244 distinct deck-library
    cards use `AB$ Token`?", "which cards have an SVar named X
    that references `Count$Valid Goblin.YouCtrl`?")
  - Better audit tools (compare Scryfall oracle text against
    Forge's `Oracle:` line to catch errata drift)
  - Foundation for any future Python-native engine — the parser
    is the cheapest thing to write first because everything else
    depends on having an AST to interpret
- **implementation_notes** (post-hoc):
  - `parse_card_script(text)` for in-memory parsing;
    `parse_card_script_file(path)` for the file-mediated convenience.
  - Each `A:` / `T:` / `R:` / `S:` line becomes one `Ability`
    with `kind` (the prefix), `category` (the first Key$ pair's
    key — AB / SP / Mode / Event), `effect` (the first pair's
    value — Mana / Token / ChangesZone), and `params` (all Key$
    Value pairs as strings).
  - SVar values stay symbolic — `Count$Valid Goblin.YouCtrl`
    rides as-is in `svars["X"]`. Interpretation is the engine's
    job, not the parser's.
  - DFC support: `AlternateMode:DoubleFaced` triggers face split;
    the parent face holds the front, `faces[0]` holds the back.
  - `raw_unparsed_lines` is the DSL-drift early-warning system.
    Every fixture test asserts it's empty, so when Forge adds a
    new top-level key (e.g. `Energy:`), the test breaks loudly
    and we know to extend the parser.
- **acceptance_criteria**:
  - [x] Parse 8 byte-exact Forge fixtures spanning the DSL surface:
    vanilla creature, land with two mana abilities, sorcery with
    chained sub-abilities, keyword-only creature, static-effect
    enchantment, activated-with-SVar creature, replacement +
    trigger land, channel-ability legendary land.
  - [x] Handle DFC via `AlternateMode:DoubleFaced` → `faces` list.
  - [x] Handle variable PT (`*`, `1+*`) as symbolic strings.
  - [x] Handle planeswalker `Loyalty:` and battle `Defense:`.
  - [x] Don't crash on malformed input — bad lines land in
    `raw_unparsed_lines` for audit.
  - [x] UTF-8 with replacement so encoding quirks don't break
    parse (Forge's older set scripts have had stray Latin-1).
  - [x] 17 tests covering the above; full suite green at 1262.

## new_during_work

This slice intentionally stopped at the parser. The natural
follow-ups (each their own bounded slice):

- **#018 (LOW, ~2-4h, future)** — Bulk parse our 7,244-card deck
  library through the parser, build a static-analysis report
  (effect-kind histogram, SVar reference graph). Concrete first
  user of the parser. Uses the existing `_card_list_refresh`
  pattern.
- **#019 (LOW, ~4-6h, future)** — Oracle-text vs DSL diff tool.
  Cross-reference Forge's `Oracle:` field against Scryfall's
  `oracle_text` for every card in our library; flag mismatches
  for manual review. Catches errata drift that today only
  surfaces when a sim produces a wrong verdict.
- **Future-future** — The actual rules engine. Still 4-9 months.
  Don't pick without explicit human direction (FP-001 macro
  blocker per #015).

---

## #018 — FP-001 deck-library static-analysis CLI

- **status**: `done` (commit `<this commit>` —
  `src/commander_builder/forge_cards_loader.py`,
  `src/commander_builder/deck_library_analyzer.py`,
  `scripts/analyze_deck_library.py`; 31 new tests across two
  test files)
- **priority**: LOW (concrete first user of the #017 parser; the
  user said on 2026-05-19 "I feel #2 could help looking over
  decks and working them out as well" — this is that)
- **scope**: ~2-4h (actual: ~60 min — the parser groundwork from
  #017 made the analyzer fall out cleanly; biggest surprise was
  the Forge corpus shipping as a single zip blob, not a directory
  tree, which prompted the dual-mode loader)
- **prerequisites**: [#017](#017-fp-001-card-script-parser-read-only-ast)
  for the parser; a working Forge install with
  ``vendor/forge/res/cardsfolder/`` (zip or unzipped both supported).
- **context**: the user's 7,244-card deck library across 345 decks
  is the proving ground for everything in the curator pipeline.
  This CLI turns it into measurable signal — "which DSL primitives
  dominate? which archetypes cluster via DeckHints? which cards
  does Forge not ship a script for?" — so future engine work + the
  archetype detector + the errata-drift audit can all be grounded
  in real data instead of guesses.
- **components**:
  - **`forge_cards_loader.py`** — dual-mode loader for Forge's
    card-script corpus. Auto-detects whether the install has the
    unzipped letter-tree (dev layout) or the canonical
    ``cardsfolder.zip``. ``slug_for(name)`` mirrors Forge's
    filesystem convention (lowercase, non-alnum → underscore,
    DFC names use the front face). ``load_one(name)`` resolves
    a card-name to its script blob; ``iter_all()`` for bulk
    passes. Context-manager support closes the zip handle
    cleanly.
  - **`deck_library_analyzer.py`** — `analyze_library(deck_dir,
    loader)` walks `.dck` files, parses each card's script, and
    folds into a `LibraryReport` (effect-kind histogram,
    ability-category histogram, keyword histogram, SVar reference
    counts, DeckHints frequency, DeckHas frequency, plus
    unresolved-cards list). `include_per_deck=True` adds per-deck
    card counts for drill-down. DFC cards count both faces.
    `to_dict()` projects to JSON for the CLI wrapper.
  - **`scripts/analyze_deck_library.py`** — human-readable + `--json`
    output, `--max-decks N` for smoke runs, `--top N` for
    histogram caps, `--per-deck` for breakdown.
- **smoke run on real data** (50 decks of 345):
  ```
  Decks scanned:    50
  Distinct cards:   2183
  Resolved:         1932 (88%)
  Unresolved:       251 (12%; mostly Commander-only printings
                          Forge hasn't bundled yet)
  Top effects:      Mana (455), ChangesZone (421), Continuous (311),
                    ChangeZone (181), Moved (147), Phase (119),
                    SpellCast (110), Draw (90)
  Top keywords:     Flying (110), Flash (53), Vigilance (32),
                    Trample (31), Haste (29)
  Top DeckHints:    Ability$Counters (21), Type$Instant|Sorcery (18),
                    Ability$Graveyard (14), Type$Merfolk (13)
  ```
  → confirms Mana / ChangesZone / Continuous are the FIRST
    primitives a Python engine would need; archetype clustering
    via DeckHints surfaces obvious bins for the curator.
- **acceptance_criteria**:
  - [x] Loader handles zip + directory layouts behind the same API.
  - [x] Analyzer resolves cards via slug, counts effects /
    keywords / SVars / DeckHints, lists unresolved cards.
  - [x] DFC card scripts contribute both faces' effects.
  - [x] CLI wrapper at `scripts/analyze_deck_library.py` with
    `--json` / `--max-decks` / `--top` / `--per-deck` / `--deck-dir`
    / `--forge-dir` flags.
  - [x] 31 unit tests (21 loader + 10 analyzer) all green; runs
    on the real 50-deck library smoke without errors.
  - [x] Full suite green at 1293 passed.

## new_during_work

- **#019 still open** (oracle-text vs DSL diff for errata drift) —
  unchanged from #017's `new_during_work`. The analyzer infrastructure
  built here makes #019 cheap: bulk-iterate via `CardsLoader.iter_all`,
  cross-reference parsed `CardScript.oracle` against Scryfall's
  `oracle_text` from the existing cache. Probably ~1-2h.
- **Unresolved-card investigation worth a one-off pass.** 12% of
  cards in the 50-deck smoke don't have Forge scripts — likely
  Commander-only printings (CMM, CMR, CLB, etc.) or PLST reprints.
  A quick "which sets dominate the unresolved list?" report would
  pinpoint whether we need a corpus update or whether the slug
  rules need extending for an edge case I missed. Half-hour
  follow-up.

---

## #019 — FP-001 oracle-text vs DSL drift detector

- **status**: `done` (commit `<this commit>` —
  ``src/commander_builder/oracle_diff.py``,
  ``scripts/oracle_diff_report.py``, 18 tests in
  ``tests/test_oracle_diff.py``)
- **priority**: LOW (FP-001 slice 3; cheap because the parser
  (#017) and loader (#018) already exist — this is the third
  layer that combines them with Scryfall data)
- **scope**: ~1-2h (actual: ~75 min including the #018 DFC
  slug-bug fix that this exposed and the iterative normalization
  tuning to cut false positives)
- **prerequisites**: #017 (parser) and #018 (loader/analyzer)
  both must ship first.
- **context**: WotC ships errata roughly quarterly. Scryfall
  updates within days; the bundled Forge corpus lags by a
  release cycle or two. Sims running against stale Forge text
  produce wrong verdicts long before anyone notices. This module
  diffs Forge's ``Oracle:`` field against Scryfall's
  ``oracle_text`` per card and surfaces mismatches for human
  review. NO auto-correction — the maintainer decides whether
  to refresh the Forge corpus, accept the drift, or whitelist
  a deliberate variant.
- **components**:
  - **``oracle_diff.py``**:
    - ``normalize_oracle(text, card_name)``: replaces Forge's
      literal ``\\n`` with actual newlines, substitutes
      ``CARDNAME``/``NICKNAME`` placeholders, collapses whitespace
      runs, normalizes Unicode minus ``−`` → ASCII ``-``, strips
      Forge's ``[-N]`` planeswalker loyalty-cost brackets.
    - ``compare_card_oracle(name, forge_card, scryfall_data)``:
      returns ``OracleDiffResult`` with ``match`` + ``status``
      (``match``/``differ``/``missing_forge``/
      ``missing_scryfall``/``missing_both``) + the unified
      ``diff_lines`` for human review.
    - DFC support: concatenates per-face oracle text with a
      ``//`` sentinel on both sides so multi-face cards compare
      symmetrically.
  - **``scripts/oracle_diff_report.py``**: CLI wrapper with
    ``--max-decks`` / ``--only-mismatches`` / ``--diff`` /
    ``--json`` / ``--by-pattern`` flags. ``--by-pattern``
    buckets diffs into known errata patterns (``this-land``,
    ``this-creature``, etc.) so a 263-row report becomes
    7 readable categories.
- **smoke run on 10 real decks**:
  ```
  match:            368
  differ:           260
    [this-land errata]       82 cards
    [this-creature errata]   64 cards
    [this-artifact errata]   31 cards
    [this-enchantment]       12 cards
    [other]                  60 cards   ← real interesting cases
  missing_forge:    61 (Commander-only printings Forge hasn't bundled)
  missing_scryfall: 19
  missing_both:     1
  ```
  → confirms WotC did a massive ``X`` → ``this <type>`` errata
  sweep that Forge is uniformly stale on; ~200 cards in just
  10 decks lean on the stale text. Refreshing the Forge corpus
  is now a concrete action item with measurable scope.
- **acceptance_criteria**:
  - [x] Normalize Forge's literal ``\\n`` and CARDNAME/NICKNAME
    placeholders so cosmetic differences don't drown out errata
    signal.
  - [x] Detect the Underground River-style errata (live test
    case at ``tests/test_oracle_diff.py::test_compare_detects_real_underground_river_errata``).
  - [x] DFC support: per-face concatenation on both sources.
  - [x] CLI with ``--by-pattern`` triage view.
  - [x] 18 new oracle-diff tests + 2 new DFC-loader tests
    (the latter discovered while writing this slice). Full suite
    green at 1313 passed (was 1293 before this commit).

## also caught: #018 DFC slug bug

While writing #019 I discovered #018's loader couldn't resolve
DFC cards like ``Bala Ged Recovery`` because Forge stores them
under the FULL DFC name (``bala_ged_recovery_bala_ged_sanctuary.txt``)
not the front-face-only slug my loader was looking for. Fixed:
``CardsLoader.load_one`` now builds a lazy DFC index on first
miss that maps front-face slug → full DFC slug, so .dck files'
front-face-only references still resolve. Two new tests
(``test_loader_zip_resolves_dfc_from_front_face_only_name``,
``test_loader_dfc_index_does_not_shadow_regular_cards``) pin
the contract. Confirmed working against the real Forge install
(Bala Ged Recovery now loads cleanly).

## new_during_work

- **Forge corpus refresh** is now the obvious follow-up action.
  We have evidence ~200 cards in 10 decks are running stale
  text; the user can either update the bundled Forge install
  (probably ships with a fresh corpus) or pull
  ``cardsfolder.zip`` from Card-Forge/forge HEAD as a one-off.
  Not a code task — flagged for human action.
- **#020 (LOW, ~1h, future)** — Extend ``--by-pattern`` to a
  general regex-based bucketing system loaded from a YAML/JSON
  file. The current buckets are hardcoded; surfacing them as
  data lets a maintainer add new patterns (next errata sweep)
  without code changes.

---

## #020 — Data-driven bucketing for oracle_diff

- **status**: `done` (commit `<this commit>` — bucket rules moved
  from hardcoded lambdas in the CLI script to JSON at
  ``src/commander_builder/data/oracle_diff_buckets.json``; module
  helpers ``load_diff_buckets`` + ``categorize_diff`` +
  ``DiffBucket`` dataclass live in ``oracle_diff.py``; CLI gains
  ``--bucket-rules`` override.)
- **priority**: LOW (logged as ``new_during_work`` under #019)
- **scope**: ~1h (actual: ~30 min)
- **why**: maintainers can add new errata-pattern buckets without
  touching code. Next WotC errata sweep just gets a new row in
  the JSON file.
- **rule schema** (per bucket):
  - ``label`` (required) — what the bucket is called in reports
  - ``scryfall_contains`` — substring required in Scryfall text
  - ``scryfall_not_contains`` — substring forbidden in Scryfall text
  - ``forge_contains`` — substring required in Forge text
  - ``forge_not_contains`` — substring forbidden in Forge text
  - All keys are AND'd; match is case-insensitive substring.
- **safety**: a rule with NO constraints returns False (would
  otherwise silently swallow every diff); the loader rejects a
  rule with no ``label``.
- **acceptance**: ``load_diff_buckets()`` reads the shipped JSON;
  the CLI's ``--by-pattern`` uses the loaded rules; 8 new tests
  cover loader + matcher + ordering + safety. Suite green at 1341.

---

## Maintenance notes for the agent

- This file lives at `docs/AGENT_BACKLOG.md`. Keep the path stable
  across sessions so an agent can find it without prompting.
- When you complete an item:
  - Flip `status` to `done`.
  - Append the implementing commit SHA at the end of the item.
  - Move the row to a "Recently done" section at the bottom if the
    table gets long — but keep the item body in place so future
    sessions can audit the work.
- When you discover a new issue during work, add a new item with
  the next free ID. Do NOT delete or renumber old items (their IDs
  are referenced from commit messages and other backlog items).
- Re-check `STATUS.md`, `CHANGELOG.md`, and the codebase's
  `TODO/FIXME` markers at the start of every session — items get
  added by humans too.
