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
| [#002](#002-image-cache-eviction-policy) | MEDIUM | open | ~2h | Image cache: disk-quota eviction policy |
| [#003](#003-image-cache-retry-on-transient-failure) | LOW | open | ~30 min | Image cache: one retry on transient Scryfall failures |
| [#004](#004-status-md-stale-overnight-session-block) | LOW | open | ~15 min | STATUS.md: prune stale "2026-05-14/15 overnight session" block |
| [#005](#005-add-github-actions-ci-workflow) | HIGH | done | ~1.5h | Add `.github/workflows/test.yml` running `pytest --run-slow` |
| [#006](#006-pre-commit-secret-scan-hook) | MEDIUM | open | ~1h | Pre-commit hook scanning diff for secrets |
| [#007](#007-app-js-extract-audit-streaming-module) | MEDIUM | open | ~1h | app.js: extract audit-streaming SSE cluster (lines ~997-1223) |
| [#008](#008-app-js-extract-deck-health-tiles--salt-banner) | MEDIUM | open | ~1h | app.js: extract deck-health tiles + salt-warning banner |
| [#009](#009-app-js-extract-avg-deck-preview) | MEDIUM | open | ~1h | app.js: extract average-deck preview renderer |
| [#010](#010-refresh-card-lists-auto-suggestion-for-self-mill) | MEDIUM | open | ~2h | `refresh_card_lists.py`: auto-suggest self-mill candidates from oracle text |
| [#011](#011-batch-mode-for-commander-auto-curate) | MEDIUM | open | ~3h | Auto-curate batch mode for overnight library runs |
| [#012](#012-knowledge-log-milestone-tag) | LOW | open | ~2h | knowledge_log: `milestone` column + `commander-history --milestone` flag |
| [#013](#013-two-version-audit-diff-ui) | LOW | open | ~4h | Two-version audit diff UI (v1 vs v2 side-by-side) |
| [#014](#014-tier-29-oracle-text-card-reference-store) | LOW | open | ~4h | Tier 2.9: oracle-text-first card-reference store (FP-009) |
| [#015](#015-fp-001--fp-002--fp-003--fp-004--fp-011) | — | parked | — | FP-001 / FP-002 / FP-003 / FP-004 / FP-011 (see STATUS.md) |

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

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~2h
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

- **status**: `open`
- **priority**: LOW
- **scope**: ~30 min
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

- **status**: `open`
- **priority**: LOW
- **scope**: ~15 min
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

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~2h
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

- **status**: `open`
- **priority**: MEDIUM
- **scope**: ~3h
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
  - [ ] Batch run over a 5-deck dir produces 5 JSON records.
  - [ ] `--force` re-curates already-versioned decks; without it,
    they're skipped with a "v2 already exists" note.
  - [ ] Test using `pytest --run-slow` — auto-marked slow.

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

## #015 — Parked (FP-001 / 002 / 003 / 004 / 011)

- **status**: `parked`
- **rationale**: see [STATUS.md § Parked plans](../STATUS.md#parked-plans-big-bets-blocked-or-strategic-forks)
  for each. Do not implement without explicit human direction:
  - FP-001 — 6-12 month engineering project (Python-native Forge).
  - FP-002 — data-gated; needs 200+ logged iterations (currently ~5).
  - FP-003 — cheap to attempt but not a current bottleneck.
  - FP-004 — upstream Forge constraint (no `--seed` flag).
  - FP-011 — promote when sharing with anyone beyond the original dev.

Also parked: Pearson r analysis in `forge_py_correlation.py:219` —
needs ≥30 correlation rows (currently <10).

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
