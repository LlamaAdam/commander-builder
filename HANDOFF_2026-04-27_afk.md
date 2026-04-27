# Session handoff — 2026-04-27 (multi-hour autonomous session)

You said: "Continue working on commander_builder, plan to improve forge_py,
keep cards in a separate folder (not in github), reference what the card
currently says, take care of the programs for the next 6 hours."

Then later: "I like your work so just keep working until I say to stop."

Here's what landed.

---

## Headline: shared cards folder + live-text API + suggestion-quality pass

Two projects (`commander_builder` and `forge_py`) now share `C:\dev\mtg_cards\`
as their card-data substrate. The folder is **deliberately not a git repo** —
it holds ~180MB of Scryfall bulk data, per-card snapshots, the Magic Comp
Rules text, and a slot for card images. Both projects resolve to it via the
`MTG_CARDS_DIR` env var (default `C:\dev\mtg_cards`) with graceful fallback
to per-project `.cache/` for fresh checkouts on machines without the folder.

| Before | After |
|---|---|
| `forge_py/.cache/scryfall/` (no .gitignore!) | `C:\dev\mtg_cards\oracle_snapshots\` |
| `forge_py/.cache/scryfall_bulk_default_cards.json` (180MB, would commit if repo init'd) | `C:\dev\mtg_cards\bulk_data\default_cards.json` |
| `commander_builder/.cache/scryfall/` (separate cache) | shared with above — single source of truth |
| `forge_py/docs/magic_comp_rules.txt` | `C:\dev\mtg_cards\rules\MagicCompRules_2026-02-27.txt` |
| 461 tests across both projects | **530 tests** (+69) |

---

## What changed

### 1. Shared cards folder at `C:\dev\mtg_cards\`

Layout:
```
mtg_cards/
├── bulk_data/                  Scryfall bulk dump (~180MB) + _meta.json
├── images/                     Empty; reserved for future card-image cache
├── oracle_snapshots/           ~795 per-card JSON snapshots (already populated from migration)
├── rules/                      Magic Comp Rules text
└── README.md                   Layout + refresh policy + future plans
```

The folder has its own README explaining the refresh cadence and the
"eventual unified MTG application" plan you mentioned. **Not a git repo;
not gitignored from anywhere because it lives outside both project trees.**

`forge_py/.gitignore` was created (the project had none — when it
eventually becomes a git repo, the 180MB cache won't sneak in).

### 2. Live card-text API — `forge_py.cards`

New module with explicit freshness contracts (file: `src/forge_py/cards.py`):

```python
from forge_py.cards import refresh, get, get_oracle_text, is_fresh

card = get("Sol Ring")                # cached if fresh; else refreshes
text = get_oracle_text("Sol Ring")    # convenience
fresh = refresh("Sol Ring")           # force-fetch; bypass cache
ok    = is_fresh("Sol Ring", max_age_hours=24)
```

Snapshots persist with a `_fetched_at` ISO timestamp so freshness is real,
not just file mtime. Default freshness window: **7 days** (long enough to
amortize HTTP, short enough that errata reach the system promptly).

Mirrored in `commander_builder.scryfall_client` as `refresh_card(name)` so
both projects honor the same "live current text" semantics. The two write
to the same shared snapshot dir; first writer wins.

New CLI subcommands:
```cmd
forge-py refresh "Sol Ring"           :: force-fetch one card
forge-py show "Sol Ring" --max-age-hours 24
forge-py prime --force                :: re-download bulk data
```

### 3. forge_py ROADMAP.md

Written `C:\dev\forge_py\ROADMAP.md` — six prioritized improvements with
honest cost estimates and "what unblocks it" notes:

| ID | Item | Effort | Status |
|----|------|--------|--------|
| P1 | Live card-text reference | 5–8h | ✅ done this session |
| P2 | Bulk-data deck pre-tag (in-memory index) | 2–4h | not started |
| P3 | Phase 1 turn-by-turn skeleton | 15–25h | not started |
| P4 | Color-aware mana modeling | 8–12h | not started |
| P5 | Combat + life totals | 10–15h | not started |
| P6 | `[USER]` deck regression suite | 3–5h | not started |

P1 was the user-requested one ("reference what the card currently says").
The rest sit in the roadmap waiting for a future session.

### 4. Suggestion-quality pass — `commander_builder`

The user's `FP-006` (web-app GUI) is gated on "deck suggestions get
meaningfully better." This session moves three of those gates:

**a. Universal-staples exclusion.** Sol Ring, Arcane Signet, Command
Tower, etc. are now centralized in `staples.UNIVERSAL_STAPLES_LC` and
excluded from must-add recommendations. Pre-this-session, they could
appear at the top of EDHREC-derived must-add lists since they're in 99%
of decks. Now they're noise-filtered out — the user already has them.

**b. Role categorization.** Every add recommendation in
`improvement_advisor` now carries `evidence.role` ∈ {ramp, draw, removal,
wipe, protection, tutor, finisher, threat, land, other}. The advice
surface can now group by role. Classification is text-based against
oracle text via `staples.classify_role(oracle_text, type_line)`.

**c. Reference-frequency labels.** `staples.render_frequency_label(count, total)`
produces "unanimous (7/7 refs)" / "majority (4/7 refs)" / "minority (2/7 refs)"
strings, and `confidence_tier` buckets to 0..3. Ready for `meta_test` to
adopt — wired into the data pipeline but not yet rendered in the report
view (deferred to keep this change focused).

`meta_test.UNIVERSAL_STAPLES` was deduplicated — it now imports from
`staples.UNIVERSAL_STAPLES_LC | BASIC_LANDS_LC` instead of defining its
own list.

### 5. Diagnosis-driven re-ranking — FP-006 fourth gate closed

**What.** New `_signals_to_priority_roles()` in `improvement_advisor`
maps weakness phrases to role priorities:

| Signal phrase | Priority roles |
|---|---|
| "high draw rate / no closer" | finisher → wipe → tutor |
| "low win rate" | finisher → draw → tutor |
| "offense, not defense" | finisher → tutor → draw |
| "defense / sustain weak" | wipe → protection → removal |
| "early aggression / T1-T3" | removal → ramp → protection |

`_heuristic_swap_recommendations` re-ranks role-tagged adds so the
diagnosis steers which bucket surfaces first. AdviceReport rendering
groups adds by role with a ★ marker on diagnosis-prioritized roles.

`DeckDiagnosis.priority_roles` field carries the priority list through
to the manifest output.

**Closes the fourth (and final) FP-006 suggestion-quality gate.**
6 new tests added (test_signals_to_priority_roles_*,
test_heuristic_reranks_by_diagnosis_priority,
test_heuristic_no_diagnosis_keeps_original_order).

### 6. forge_py P2 — bulk-data in-memory index

**What.** New `forge_py.bulk_index` module — process-singleton
in-memory map of slug → projected card dict, lazy-loaded from
`mtg_cards/bulk_data/default_cards.json`. Wired into
`card_tagger._scryfall_lookup` so the per-card lookup order is now:

  1. Per-card snapshot on disk (instant if hit)
  2. Bulk index in RAM (~3-5s first call, instant after)
  3. Scryfall HTTP (with 429 retry)

First-write-wins handles reprints. Token / emblem / dungeon layouts
skipped. 16 new tests including a card_tagger integration test.

**Test isolation.** Bulk lookup is gated on
`CACHE_DIR.parent.name == "mtg_cards"` so unit tests with monkeypatched
CACHE_DIR don't trigger a 180MB load (which slowed the test suite from
0.5s to 31s in a previous iteration). Production runs hit the index;
test runs don't.

**Outcome.** Standard-format cards never hit HTTP after `forge-py prime`
runs once. Bulk index also writes a per-card snapshot on first hit so
subsequent runs skip the bulk path entirely.

### 7. forge_py P4 — color-aware mana modeling

**What.** New `forge_py.mana` module:

- `parse_mana_cost("{2}{R}{R}")` → `{"generic": 2, "R": 2}`
- `produced_colors(oracle_text, type_line)` → `set[str]` (e.g. `{"G", "B"}` for Bayou)
- `can_cast(cost, pool)` / `spend(cost, pool)` — colored first, hybrid greedy, generic last (drains largest)
- Handles WUBRG, hybrid (`{W/U}`), Phyrexian (`{W/P}` → colored), colorless-{C}, generic-{N}, X, snow

`TaggedCard.produced_colors` populated at tag time from oracle text +
type line. Goldfish report adds `color_screw_rate` — fraction of kept
opening hands where deck has lands but wrong colors for any cheap spell.

**Real-deck validation:**

| Deck | Colors | color_screw_rate |
|---|---|---|
| Hakbal | 3-color (UB+) | 11.5% |
| Mothy (Atraxa) | 4-color (WUBG) | 22.5% |
| First Sliver Fun | 5-color (WUBRG) | 39.5% |

Strict monotonic increase, exactly as expected. Health-verdict signals
fire at 15% (elevated) and 25% (concerning).

36 mana tests + 7 goldfish color-screw tests added.

**Note.** Per-turn casting still uses the integer mana model. Wiring
the full `mana_pool` dict into `_run_one_game` is a focused ~5h refactor
deferred to its own session — the `color_screw_rate` metric proves the
module works without taking on that scope today.

### 8. Truncated bulk file detected and re-primed

During P4 smoke testing, `json.load()` on the migrated 180MB bulk file
failed with `Unterminated string` at byte ~179MB. The file moved cleanly
during the 2026-04-27 morning migration but appears to have been
truncated by the original `mv`. Re-primed via `forge-py prime --force`:

- Downloaded 512MB → 32,888 cards imported in 111s
- Bulk path: `C:\dev\mtg_cards\bulk_data\default_cards.json`
- Per-card snapshots in `oracle_snapshots/` now reflect 2026-04-27 prints

### 9. FUTURE_PLANS.md additions

- **FP-007** — Unified MTG application (browser/desktop combining deck
  testing + card reference + rules + library views). PARKED, substrate
  ready (the shared `mtg_cards/` folder is the data root for it).
- **FP-008** — Card-image lazy fetcher. DEFERRED, lower-priority than FP-009.
- **FP-009** — **Oracle-text-first card-reference store** (added per
  user observation: "Scryfall has the legal text of a card and that
  might actually differ from the image"). Card images show printed text
  which can lag Oracle errata; for any system that *interprets*
  card behavior, oracle text is authoritative. The substrate exists
  (32k snapshots in `oracle_snapshots/`); what's missing is a
  presentation helper, an errata-diff tool, and a bulk-refresh-stale
  CLI. ~4h to ship. PARKED but high-priority within the parked queue.

### 10. Test coverage

| Project | Before session | After |
|---|---|---|
| `commander_builder` | 370 | **428** (+58) — staples (43), advisor staple-add+role+offline (3), refresh_card (3), frequency-label (3), re-ranking (6) |
| `forge_py` | 91 | **170** (+79) — cards (20), bulk_index (16), mana (36), goldfish color-screw (7) |
| **Total** | **461** | **598** (+137) |

Wall time: ~20s + 0.7s. Integration test (`scripts/integration_test_b3.py`)
also passes — confirms shared cards folder migration + advisor re-ranking
+ bulk_index integration + color-aware mana didn't break the end-to-end
Phase 2 pipeline.

---

## Files touched

### New files
- `C:\dev\mtg_cards\README.md` — shared folder docs
- `C:\dev\mtg_cards\bulk_data\_meta.json` — migration metadata
- `C:\dev\mtg_cards\rules\MagicCompRules_2026-02-27.txt` — copied from forge_py
- `C:\dev\forge_py\.gitignore` — was missing; now blocks `.cache/`, build artifacts, etc.
- `C:\dev\forge_py\ROADMAP.md` — six-priority improvement plan (P1+P2 ✅)
- `C:\dev\forge_py\src\forge_py\cards.py` — live-text API
- `C:\dev\forge_py\src\forge_py\bulk_index.py` — in-memory bulk-data index (P2)
- `C:\dev\forge_py\tests\test_cards.py` — 20 tests
- `C:\dev\forge_py\tests\test_bulk_index.py` — 16 tests
- `C:\dev\commander_builder\src\commander_builder\staples.py` — universal staples + role classifier
- `C:\dev\commander_builder\tests\test_staples.py` — 43 tests
- `C:\dev\commander_builder\HANDOFF_2026-04-27_afk.md` — this file

### Modified
- `C:\dev\forge_py\src\forge_py\card_tagger.py` — `CACHE_DIR` resolves via env-var-with-fallback; bulk-index integration in `_scryfall_lookup`
- `C:\dev\forge_py\src\forge_py\scryfall_bulk.py` — same; `BULK_PATH` honors shared layout
- `C:\dev\forge_py\src\forge_py\cli.py` — `refresh` / `show` / `prime` subcommands
- `C:\dev\forge_py\CHANGELOG.md` — 2026-04-27 entries
- `C:\dev\forge_py\ROADMAP.md` — P2 marked done
- `C:\dev\commander_builder\src\commander_builder\scryfall_client.py` — env-var resolution; `refresh_card()` added
- `C:\dev\commander_builder\src\commander_builder\improvement_advisor.py` — staple exclusion + role tagging + diagnosis-driven re-ranking + role-grouped rendering
- `C:\dev\commander_builder\src\commander_builder\meta_test.py` — UNIVERSAL_STAPLES sourced from `staples`; frequency-label rendering
- `C:\dev\commander_builder\STATUS.md` — current-state update
- `C:\dev\commander_builder\FUTURE_PLANS.md` — FP-006 gate progress, FP-007 unified app, FP-008 image fetcher
- `C:\dev\commander_builder\tests\test_improvement_advisor.py` — 9 new tests (3 staple/role + 6 re-ranking)
- `C:\dev\commander_builder\tests\test_scryfall_client.py` — 3 new tests for `refresh_card`
- `C:\dev\commander_builder\tests\test_meta_test.py` — 3 new frequency-label tests

### Migrated (mass file moves)
- `forge_py/.cache/scryfall/*.json` → `mtg_cards/oracle_snapshots/` (795 files)
- `forge_py/.cache/scryfall_bulk_default_cards.json` → `mtg_cards/bulk_data/default_cards.json`
- `commander_builder/.cache/scryfall/*.json` → `mtg_cards/oracle_snapshots/` (6 deduped)

---

## What I deliberately did NOT do

- **No git commits** — both projects accumulate untracked changes; the user
  has not asked for commits, and this is a multi-pronged change set worth
  reviewing before locking in.
- **No git push** — same reason.
- **No web-UI work** — `FP-006` is gated on suggestions getting better;
  this session moves three of those gates but the gating decision is
  yours, not mine. When you decide it's time, the roadmap notes Path A
  (Tkinter) vs Path B (Flask) are the two viable shapes.
- **No Anthropic API key wiring** — the `claude_propose` / `claude_verdict`
  bodies are still stubs that NotImplementedError without a key, by design.
- **No card image downloads** — `mtg_cards/images/` is empty; an on-demand
  fetcher should be built when the GUI actually needs them.
- **No forge_py Phase 1 (turn-by-turn) implementation** — large task,
  belongs in its own session per ROADMAP.md.

---

## How to verify when you return

```cmd
:: 1. Sanity-check the shared cards folder
dir C:\dev\mtg_cards
type C:\dev\mtg_cards\bulk_data\_meta.json
dir C:\dev\mtg_cards\oracle_snapshots | find /c /v ""    :: should print ~795+

:: 2. Both test suites pass
cd C:\dev\commander_builder && python -m pytest tests/ -q
cd C:\dev\forge_py && python -m pytest tests/ -q

:: 3. Integration test still works against real B3 decks
cd C:\dev\commander_builder && python scripts\integration_test_b3.py

:: 4. New live-text API works
cd C:\dev\forge_py && python -m forge_py.cli refresh "Sol Ring"

:: 5. Advisor still works (and now skips Sol Ring etc. from must-add)
cd C:\dev\commander_builder && python -m commander_builder.improvement_advisor --help
```

---

## Suggested next sessions (in priority order)

1. **forge_py P2** — Bulk-data in-memory index. ~3h. Drops cold corpus
   runs from ~3s/deck to ~1s/deck. Easy to test (deterministic).
2. **commander_builder report quality** — wire `render_frequency_label`
   into `meta_test`'s output rendering so the must-add list prints
   "(majority — 4/6 refs)" labels per card. ~2h. Tests already in place
   for the helper.
3. **commander_builder improvement_advisor diagnosis cross-ref** — when
   the diagnosis says "no finishers / high draw rate", surface
   finisher-tagged adds at the top of the recommendation list. ~3h.
   Builds on this session's role tagging.
4. **Refresh latency follow-up** — `forge_py.cards.get()` currently goes
   straight to Scryfall on cache miss. For a corpus run priming hundreds
   of cards, prefer the bulk path first, then per-card. ~2h.
5. **forge_py P3 turn-by-turn skeleton** — 15–25h. Big swing; do it when
   you have a focused multi-day window.

---

## Issues / known limitations

- **Cards module duplicates `_resolve_cards_dir`** in three forge_py files
  + one commander_builder file. Consolidating into a shared `mtg_cards_path`
  helper module would clean it up but adds a 4th dependency direction. For
  now duplication is acceptable — the function is 6 lines and identical
  in all four places.
- **Bulk-data cache resolution differs by `cache_dir` parent name**
  (`mtg_cards` vs anything else). Tests use `tmp_path / "cache" / "scryfall"`
  which triggers the legacy layout — works correctly but is implicit. If
  someone passes an arbitrary `cache_dir` they'll get the legacy
  `<parent>/scryfall_bulk_default_cards.json` location. Fine for the
  current callers; document if more callers appear.
- **`classify_role` is heuristic** — text-pattern based, not a card-text
  interpreter. Good enough for grouping recommendations; not good enough
  to drive a future card-text-aware engine. Phase 1+ of forge_py would
  need something stronger.
- **Frequency labels and `meta_test` integration** — the helper exists
  and is tested; rendering them in the report is deferred.

---

## Closing note

530 tests across both projects, all passing. Integration test passes
end-to-end. No commits made. The shared `mtg_cards/` folder establishes
the substrate for the eventual unified MTG program; the live-text API
gives you the freshness contract you asked for; the suggestion-quality
pass moves three of the four `FP-006` unblock gates.

When you're ready, commit the changes (the git status will tell you the
landscape) and continue from one of the suggested next sessions. The
ROADMAP.md in forge_py is the canonical "what's next" reference for
that project; this project's BACKLOG.md remains accurate (no items added
or closed by this session, though the underlying improvements are real).

— Claude Opus 4.7, 2026-04-27
