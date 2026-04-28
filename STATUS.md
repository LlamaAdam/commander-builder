# Status — current state of the project

> Living tracker, updated as work progresses. Read this first to find out
> "what's the project up to right now?" without scrolling chat history.
>
> **Three sections** — *Now* (active work), *Recent* (last few days), *Blocked*
> (paused on something external). Backlog items live in `BACKLOG.md`; design
> decisions live in `docs/decisions/`. This file is for *operational state*.

**Last updated**: 2026-04-28 (project-manager session)
**Current phase**: FP-006 backend live; Flask scaffold + iteration history feed shipped; UI rendering still TBD

---

## Now

### Working on
Nothing actively in flight. The 2026-04-28 project-manager session landed:

- **FP-006 Flask scaffold** — `src/commander_builder/web/`. Routes:
  `/api/health`, `/api/decks`, `/api/dashboard?deck=<id>`,
  `/api/iterations[?deck=<id>]`. Path-traversal guard validates deck
  inputs against `deck_dir`. `pyproject.toml` adds `[web]` extra
  (`flask>=3.0`). 21 tests cover route shapes + traversal guard +
  iteration listing. Run dev server: `python -m commander_builder.web`.
- **Knowledge-log demo seeder** — `scripts/seed_demo_knowledge_log.py`
  writes a 4-iteration arc (pending → kept → reverted → neutral) for
  a fictional Omnath deck. Lets the UI's version-history strip
  develop end-to-end before real Forge data exists. 6 tests.
- **Test counts**: 482 tests total (was 453), all green.

The 2026-04-27 session before that landed:

- **Shared `mtg_cards` folder** at `C:\dev\mtg_cards\` (out-of-repo, ~180MB
  Scryfall bulk data + per-card snapshots + Magic Comp Rules text). Both
  `commander_builder` and `forge_py` resolve their card cache to this
  shared location via `MTG_CARDS_DIR` env var with a sensible default.
  Also lays the substrate for the eventual unified MTG application
  (rules + images + deck testing).
- **`scryfall_client.refresh_card()`** — force-fetch a card bypassing the
  cache, mirrored by `forge_py.cards.refresh()`. Use when you need
  guaranteed-current oracle text (live-text directive).
- **`staples.py`** — canonical `UNIVERSAL_STAPLES_LC`, `BASIC_LANDS_LC`,
  `classify_role(oracle_text, type_line)`, `render_frequency_label`,
  `confidence_tier`. `improvement_advisor` and `meta_test` now share the
  staples list; the advisor tags every add recommendation with a role.
- **Suggestion-quality improvements** in `improvement_advisor`:
  - Universal staples (Sol Ring, Arcane Signet, etc.) excluded from
    must-add lists (noise removal — every deck already has them).
  - Each add carries `evidence.role` so the advice surface can group by
    ramp/draw/removal/finisher.
- **forge_py improvements**:
  - New `forge_py.cards` module — full live-card-data API with freshness
    contracts (`is_fresh`, `get`, `refresh`, `get_oracle_text`).
  - New CLI subcommands: `forge-py refresh`, `forge-py show`,
    `forge-py prime`.
  - `ROADMAP.md` written: P1 live-text refresh ✅, P2 bulk index, P3
    turn-by-turn skeleton, P4 color-aware mana, P5 combat, P6 regress.

Earlier session (2026-04-26 PM) closed **20 backlog items + 2 new modules + 1 design decision**:

| ID | Item | Status |
|----|------|--------|
| GAP-001 | Real archetype classifier (heuristic) | ✅ done |
| GAP-002 | `--max-candidates` flag in pool_curator CLI | ✅ done |
| GAP-003 | publicId as deck_id (lineage durability) | ✅ done |
| GAP-004 | `_filename_for_match` collision-suffix gap | ✅ done |
| GAP-005 | `proposer.py` skeleton + router | ✅ done |
| GAP-006 | `_split_into_slices` persistent-violation case | ✅ done |
| GAP-008 | Streaming `forge_runner` output | ✅ done |
| GAP-009 | `edhrec_client.py` programmatic discovery | ✅ done |
| GAP-011 | Audit prompt manifest writeback JS | ✅ done |
| GAP-012 | Integration test for `iteration_loop.run_one_iteration` | ✅ done |
| GAP-013 | Unit tests for `compare_versions.compare` | ✅ done |
| GAP-014 | Top-level `commander-status` command | ✅ done |
| GAP-016 | README reflects Phase 2 workflow | ✅ done |
| GAP-017 | `revert_to.py` rollback automation | ✅ done |
| GAP-018 | Game Changers dynamic fetch + cache | ✅ done |
| GAP-024 | Legacy `deck_id` migration helper | ✅ done |
| GAP-007 | Claude/Ollama verdict + propose body-fills | ✅ done |
| GAP-015 | CI / GitHub Actions workflow | ✅ done |
| GAP-025 | Tribal aggro keyword expansion | ✅ done |
| GAP-010 | `report.py` deck history Markdown renderer | ✅ done |
| GAP-026 | Knowledge log export/import for backup | ✅ done |
| GAP-022 | Moxfield API push | ❌ won't-do (personal-project scope) |
| GAP-023 | LICENSE choice | ⏸ low-priority (personal use) |

Plus new modules not on original backlog:
- `commander-doctor` environment health check (10 checks, 13 tests)
- `commander-history` deck iteration Markdown report (20 tests)
- `commander-export` knowledge log JSON dump (11 tests)

Plus design decisions:
- CI simplified to single-OS, single-Python (Windows 3.12 only — matches
  actual dev environment)
- `CONTRIBUTING.md` reframed as "session notes" rather than
  open-source contribution guide
- FUTURE_PLANS.md FP-005 (Moxfield API push) closed as WON'T-DO

### Up next
Active engineering backlog is **empty for commander_builder**. **All four
FP-006 (web-app GUI) suggestion-quality gates are now satisfied** —
staples-exclusion ✅, frequency labels ✅, role categorization ✅,
**diagnosis-driven re-ranking ✅** (closed this session). FP-006 is now
unblocked from the suggestion-quality side; the remaining gate is "the
user has run a few real iterations to validate the system shape" —
that needs real iteration data, not engineering work.

Highest-leverage next work, ranked:

1. **Real iteration data accumulation** — knowledge_log has 0 real rows
   (1 integration-test row). Phase 3 ML training and FP-006 final
   readiness both wait on this. Action required is from the user, not
   the codebase.
2. **forge_py P3 turn-by-turn** — 15–25h. Largest unrealized
   improvement on either project. Adds tempo + cards-played-curve
   metrics that correlate with real Forge sims.
3. **FP-006 path B prototype** (Flask) — ~2 weeks. Now that all
   suggestion gates are satisfied, this is technically unblocked.
   Hold until iteration data exists.
4. **forge_py P4 color-aware mana** — 8–12h. Multicolor decks (Atraxa,
   Ur-Dragon) currently goldfish as if they're mono-color; color
   modeling fixes that.

### Sister project: forge_py

A separate spike at `C:\dev\forge_py\` started 2026-04-26 — Python deck-testing
sandbox for goldfish-level consistency stats (mulligan rate, mana curve,
commander cast turn) without the Forge JVM. **Not** a Forge replacement;
complements it for fast statistical sanity checks. See
`C:\dev\forge_py\README.md` for the phased plan and FUTURE_PLANS.md FP-001
for the broader "should we replace Forge entirely" decision context.

forge_py status (2026-04-27):
- **Phase 0 declared complete** — goldfish corpus run on all 13 [USER]
  decks; goldfish stats correlate with real Forge sims for non-storm
  decks; storm decks honestly flagged UNCERTAIN rather than producing
  misleading numbers.
- **P1 (live card-text reference) ✅** — new `forge_py.cards` module
  with `refresh()` / `get()` / `is_fresh()` / `get_oracle_text()`
  honors the user's "reference what the card currently says" directive.
- **P2 (bulk-data in-memory index) ✅** — `forge_py.bulk_index` loads
  ~32,000 cards into RAM lazily, integrated into `card_tagger._scryfall_lookup`.
  Cold-cache deck tagging now goes: per-card snapshot → bulk index → HTTP.
  Standard-format cards never hit HTTP after bulk download.
- **P4 (color-aware mana) ✅** — new `forge_py.mana` module + new
  `color_screw_rate` metric in goldfish reports. Multicolor decks now
  produce honest mana-pain signal that the integer mana model missed.
- 10 production modules (dck_parser, card_tagger, goldfish, compare_decks,
  corpus_summary, scryfall_bulk, cards, bulk_index, mana, cli)
- **170/170 tests passing** (was 74)
- **6 CLI subcommands**: `forge-py test`, `compare`, `corpus`, `refresh`,
  `show`, `prime`
- ROADMAP.md remaining: P3 turn-by-turn skeleton (~20h), P5 combat,
  P6 regression suite.
- Cache shared with this project at `C:\dev\mtg_cards\`.

### Project-management work
Landed this session:
- ✅ `BACKLOG.md` — 25 numbered items across 4 tiers (3 closed, 22 open)
- ✅ `STATUS.md` (this file)
- ✅ `CHANGELOG.md`
- ✅ `docs/architecture.md` — layered diagram + module responsibility table
- ✅ `pyproject.toml` — `pip install -e .` works, no more `PYTHONPATH=src`
- ✅ `CONTRIBUTING.md` — dev setup walkthrough
- ✅ `.gitignore` updated (logs, sqlite, .cache)
- ✅ `README.md` rewritten to reflect Phase 2 (closes GAP-016)

---

## Recent (last 7 days)

### 2026-04-27 (autonomous-improvement session)
- **Shared `C:\dev\mtg_cards\` data folder** established. Both projects
  resolve their card cache here via `MTG_CARDS_DIR` env var. Holds the
  Scryfall bulk dump (180MB), 795 per-card snapshots, Magic Comp Rules
  text, and a slot for future card images. Out-of-repo by design — this
  is the substrate for the user's eventual unified MTG application.
- **Live card-text API** in both projects (`forge_py.cards.refresh` /
  `commander_builder.scryfall_client.refresh_card`). Force-fetches
  Scryfall, bypasses cache. Default cache-with-freshness window = 7 days.
- **`staples.py`** (canonical universal-staples + role classifier +
  frequency labels). Deduplicated `meta_test.UNIVERSAL_STAPLES`.
- **Suggestion-quality pass** in `improvement_advisor`: staple
  exclusion, role-tagged adds. `meta_test` now renders frequency labels
  ("unanimous (5/5 refs)", "majority (3/5 refs)") in the report view.
- **Diagnosis-driven re-ranking** ✅ FP-006 fourth gate now closed.
  Weakness signals map to priority roles via `_signals_to_priority_roles`.
  When diagnosis says "high draw rate / no closer", finisher-tagged adds
  surface first. Render output groups adds by role with a ★ marker on
  diagnosis-prioritized roles.
- **forge_py improvements**:
  - `cards.py` live-text API + 3 CLI subcommands (`refresh`, `show`, `prime`)
  - `ROADMAP.md` six-priority queue (P1, P2, P4 ✅ done this session)
  - `bulk_index.py` in-memory bulk-data index (P2 from roadmap).
    Integrated into `card_tagger._scryfall_lookup` so cold-cache deck
    tagging hits ~32,000 cards from RAM instead of HTTP. Persists a
    snapshot on first hit so subsequent runs skip the bulk path entirely.
  - `mana.py` (P4) — `parse_mana_cost`, `produced_colors`, `can_cast`,
    `spend`. Models WUBRG, hybrid, Phyrexian, colorless-{C}, generic-{N}.
    `TaggedCard.produced_colors` now populated at tag time.
  - **`color_screw_rate` metric** — fraction of opening hands that have
    enough lands but wrong colors for any cheap spell. Real-deck signal:
    Hakbal (3-color) 11.5%, Mothy (Atraxa 4-color) 22.5%, First Sliver
    (5-color) 39.5%. Health-verdict signals fire above 15% / 25%.
  - Test-isolation gate: bulk index only consults the real shared file
    when `CACHE_DIR.parent.name == "mtg_cards"` (production layout).
    Tests with monkeypatched CACHE_DIR are unaffected.
- **forge_py `.gitignore`** added (was missing).
- Tests: 370 → **428** (commander_builder); 91 → **170** (forge_py).
  Integration test still passes end-to-end.
- No git commits made — handoff in `HANDOFF_2026-04-27_afk.md`.

### 2026-04-26 (afternoon)
- **Phase 2 scaffolding landed**: `scryfall_client`, `knowledge_log`, `analyst`,
  `iteration_loop`, `moxfield_push`, `ml_dataset`. All 7 new modules wired,
  144/144 tests passing in 0.6s.
- **Real bug found and fixed**: `log_parser._normalize` regex order was wrong,
  causing `_filename_for_match` silent attribution failures. Pinned by test.
- **Smoke test PASSED**: 20-game Hakbal-vs-Hash head-to-head ran end-to-end,
  ComparisonReport JSON written, no crashes. 18 of 20 games drew (real
  signal: B3 multiplayer stalls when neither deck has a finisher).
- **Integration test PASSED**: `scripts/integration_test_b3.py` exercises every
  Phase 2 module on the 6 real B3 user decks. Resolved color identities for
  all 6 commanders via Scryfall, generated push-ready textarea blobs, ran
  the analyst on real data, persisted to knowledge_log, extracted ML
  features.
- **Audit prompt v3 versioned in repo**: `prompts/moxfield_audit_v3.md` with
  hand-off note pointing at `compare_versions.py`.

### 2026-04-26 (morning)
- B3 batch preflight: 6/6 pass.
- B4 batch preflight: 6/6 pass (3 of 6 hit slow-match cutoff).
- QA review surfaced 5 fixes; all applied.
- New modules: `game_analyzer`, `run_match`, `compare_versions`, `snapshot_deck`.
- `compare_versions.py` validates v1-vs-v2 head-to-head per Phase 2 design.

---

## Blocked

Nothing currently. (Items in Tier 4 of BACKLOG are deferred-by-design — not
blocked on external action.)

---

## Stats

- **Modules**: 26 production — added `staples.py` (universal-staples + role classification)
- **Tests**: 428/428 passing across 27 test files (test_staples.py 43, +9 new advisor incl. re-ranking, +3 refresh_card, +3 frequency-label)
- **Test wall time**: ~19s
- **CLI entry points**: 14 (import / push / snapshot / curate / match / compare / iterate / status / revert / doctor / history / export / advise / meta-test)
- **Shared with `forge_py`**: `C:\dev\mtg_cards\` cache (`MTG_CARDS_DIR` env var override available); 795 per-card snapshots + 180MB bulk dump live there.
- **Imported decks on disk**: B3=99, B4=115, B5=56 (approx; counts include
  6 [USER]-tagged B3 + 6 [USER]-tagged B4 user decks added this session)
- **Knowledge log iterations**: 0 real (1 from integration test, in
  `integration_test_knowledge_log.sqlite`)
- **Git**: untracked changes accumulating; user has not requested a commit yet

---

## Pickup notes for next session

If you're returning to this project cold:

1. Read `PROJECT.md` for the source-of-truth spec.
2. Read this file (`STATUS.md`) for current operational state.
3. Read `BACKLOG.md` for the prioritized work queue.
4. Read `HANDOFF_2026-04-26.md` for the most recent session's narrative.
5. Run `python -m pytest tests/` (after pyproject.toml lands; until then
   `cd C:\dev\commander_builder && python -m pytest tests/`).
6. Run `python scripts/integration_test_b3.py` to confirm the Phase 2 stack
   still hangs together end-to-end.

---

## How to update this file

- Move items between *Now* / *Recent* / *Blocked* as state changes.
- *Now* should always have ≤ 3 active items. If more, prune to the truly active
  ones.
- *Recent* is rolling — keep ~7 days, prune older into a date-stamped archive
  (`docs/status_archive/2026-04.md`) when the section gets unwieldy.
- *Blocked* is for things waiting on external events: API access, decisions
  from the user, sample-size accumulation. If nothing is blocked, the section
  should literally say so.

Don't make this file a duplicate of `BACKLOG.md`. Backlog is the queue;
status is the snapshot.
