# Status — current state of the project

> Living tracker. Read this first to find out *"what's the project up to
> right now?"* without scrolling chat history.
>
> **Three sections** — *State of the tree* (right now), *Open backlog*
> (next work, ranked), *Parked plans* (deliberately deferred). History
> of what landed lives in [CHANGELOG.md](CHANGELOG.md); architecture +
> conventions live in [docs/architecture.md](docs/architecture.md).

**Last updated:** 2026-05-26 (FP-002 reopened — margin regression on
40-game soak rows; first result in)
**Phase status:** Phase 2 complete + FP-006 web GUI shipped +
`commander-auto-curate` end-to-end loop (advisor → Claude curator →
apply → Forge A/B sim → knowledge_log verdict) shipped. **FP-003
(concurrent Forge sims) shipped**; **FP-002 (Phase-3 ML predictor)
REOPENED** under the margin-regression framing now that 40-game soak
rows supply a negative class — curation is empirically ~neutral; one
significant predictor (`wincon_protection`). See Parked plans +
[docs/fp002-margin-analysis.md](docs/fp002-margin-analysis.md). 130+
commits on `feature/2026-04-28-session` ahead of `master`.

---

## State of the tree

- **Tests:** ~1287 passing fast lane (+slow with `--run-slow`), ~167s
  offline. Zero warnings under `python -W default`.
- **Branch:** `feature/2026-04-28-session` (130+ commits ahead of
  `master`, in sync with `origin`).

### 2026-05-21/22 session — FP-003 shipped, A/B attribution fix, FP-002 concluded

See [CHANGELOG.md](CHANGELOG.md) for the full breakdown. Highlights:

- **FP-003 SHIPPED** — `forge_runner.run_ab_batch(jobs, runners)` runs
  A/B sims across cwd-isolated Forge profiles in parallel (≈2×
  throughput); `vendor/forge2` is recreatable via
  `scripts/setup_forge_profile.py`. (`0f8f945`)
- **A/B win-attribution bug fixed** (`e8777b6`) — `run_ab_simulation`
  credited wins by deck *name*, but A and B routinely share the same
  internal `Name=` → wins funnelled to one side. Now attributed by
  **seat**. ⚠️ Prior FP-002 labels (78 kept / 153 reverted) are
  measurement artifacts — train only on post-fix rows (`--min-id 314`).
- **FP-002 concluded NOT VIABLE** via this pipeline — with correct
  attribution the curator's swaps almost never make a deck worse
  (detune depths 0–10 → 11 kept / 3 neutral / 0 reverted), so the
  kept-vs-reverted classifier has no negative class. Would need
  reframing (regress on improvement margin), not more sim hours.
- **Subscription-CLI curator routing** (`12d7f2c`) + **pre-commit
  secret scanner** (`803debe`, FP-011 piece).

### 2026-05-15/16 session — auto-curate loop + audit polish + Tier-3 refactor

29 commits landed; see [CHANGELOG.md](CHANGELOG.md) for the full
breakdown. Highlights:

- **`commander-auto-curate` end-to-end loop** — advisor → Claude
  curator → apply → optional Forge A/B sim → knowledge_log row with
  empirical verdict. `--mode polish/overhaul/free` presets, color-
  identity post-filter, bracket-aware filler picking, protected-card
  list, `--run-sim` closes the loop.
- **5 deck-health signals** in the audit panel (MDFC, spell density,
  mana sinks with activated-ability detection, wincon protection,
  self-mill).
- **EDHREC category fallback** via Scryfall type_line — was 21%
  uncategorized, now ~0%.
- **Manual verdict UI** — `PATCH /api/iterations/<id>/verdict` plus
  Kept/Reverted/Neutral buttons in the iteration-graph view.
- **`refresh_card_lists` script** — diff hardcoded `_MDFC_LANDS`
  against current Scryfall.
- **`proposer.py` split** (Tier-3 refactor): 1766 → 944 lines,
  three new private modules (`_proposer_filters`, `_proposer_sim`,
  `_proposer_cli`). Zero behavior change; all symbols re-exported.
- **External credentials file** at `~/.commander-builder/credentials`
  keeps `ANTHROPIC_API_KEY` outside the repo.

### Earlier session blocks (now in CHANGELOG)

Detailed per-commit history for prior sessions lives in
[CHANGELOG.md](CHANGELOG.md):

- **2026-05-14/15** — 7-phase overnight enrichment session: UI split
  for applied vs suggested adds (`b5ab5ea`), proposed-deck pricing
  (`ef33f58`), EDHREC tag-pages (`5446e7d`), theme detection (`756a6c2`),
  card thumbnails (`f57151b`), salt-list integration (`553187e`).
- **2026-05-13/14** — chrome-audit fixes for nine production-only
  bugs that synthetic-text tests hid (Cyclonic Rift / Crux of Fate /
  Coalition Victory / Three Visits / Sylvan Library / Toxic Deluge /
  Mystical Tutor / Craterhoof Behemoth / etc.) — pinned the rule that
  classifier tests source oracle text verbatim from Scryfall via
  `tests/fixtures/real_oracles.py`.

### Prior 2026-05-13/14 chrome-audit session (preserved for context)

Full per-commit detail now in [CHANGELOG.md](CHANGELOG.md). Headline:
chrome-audit closed the 2026-05-13 ranked bug list (1.1/1.2/1.3/1.4,
2.1, 3.1) and caught 9 production-only bugs that synthetic-text tests
were hiding — established the verbatim-Scryfall-text discipline
documented at `tests/fixtures/real_oracles.py`.

- **Modules:** ~30 production, plus the `web/` subpackage now split
  into 6 modules: `app.py` (302-line orchestrator), `_helpers.py`,
  and 5 blueprint factories (`routes_audit`, `routes_sim`,
  `routes_decks`, `routes_dashboard`, `routes_meta`). The advisor is
  similarly split: orchestrator + 7 `_advisor_*.py` sub-modules.
- **CLI entry points:** 14. `commander-advise` retains
  `--source`, `--claude-model`, `--budget`.
- **Knowledge log:** few rows (mix of integration tests + real saves).
  Phase 3 ML still gated on volume.

### How to resume from cold

1. `cd C:\dev\commander-builder && python -m pytest tests/ -q` — confirm
   green.
2. `git log --oneline master..HEAD` — see what's on the feature branch.
3. Skim *Open backlog* below, pick an item, or jump into the web app
   (`python -m commander_builder.web`) and run a propose-swap.

---

## Open backlog (ranked)

### Active — promoted from Parked plans (2026-05-22)

These three were unblocked this session (FP-003 concurrent sims shipped,
curator now programmatic) and promoted out of *Parked plans* so they can
be worked. Sized for a single session each.

- ~~**A1. Finish FP-011 — web config GET/PUT.**~~ ✅ **Built 2026-05-22.**
  New `config_store.py` (per-user `config.json` at
  `%LOCALAPPDATA%\commander-builder\` on Windows, `~/.commander-builder/`
  elsewhere; `COMMANDER_BUILDER_CONFIG` override) + `web/routes_config.py`
  blueprint: `GET /api/config` returns the config with the token
  **redacted** (`*_set` flag + last-4 `*_hint`, raw key never echoed);
  `PUT /api/config` validates a sparse update (token shape mirrors
  `scripts/scan_secrets.py`; unknown keys + bad values → 400 with nothing
  persisted), merges, and writes owner-only (0o600). Minimal Settings
  panel (native `<dialog>` + `settings.js`) wired into the topbar. 32
  tests (25 store + endpoints via Flask test client). Web config GET/PUT
  was the last open piece of FP-011 (secret-scan hook already shipped).
  **Unified 2026-05-22:** config.json is now the single key store — the
  audit endpoint resolves the BYO key `header → config.json → env`, and
  the audit panel's key button opens the Settings dialog (no more
  per-browser localStorage copy). Verified in Chrome.

- ~~**A2. FP-012 first slice — unattended single-deck improve loop.**~~
  ✅ **Built 2026-05-22.** `commander-improve --deck <id> --rounds N`
  (`commander_builder/improve.py`). Greedy keep-if-better loop: composes
  `commander-auto-curate --run-sim` per round, advances the base deck
  only on a `kept` seat-attributed verdict, stops early on a no-op
  (zero-change) round or an errored round. Fixed N, no bandit/Bayesian
  search (those stay parked under the full FP-012). Bracket inferred from
  the `[B<n>]` filename suffix. 15 tests (loop logic driven by an
  injected `round_fn`, so no Forge/Anthropic in the suite).

- ~~**A3. FP-001 bounded spike — LLM-piloted Forge AI (time-boxed).**~~
  ✅ **Memo delivered 2026-05-22 — verdict NO-GO (as scoped) / GO
  redirected + gated.** See [docs/fp001-llm-pilot-spike.md](docs/fp001-llm-pilot-spike.md).
  Finding: you **cannot** pilot Forge 2.0.12's AI with an LLM — it's a
  vendored compiled JAR run as a fire-and-forget subprocess with no
  decision-injection seam (only read-stdout / kill-process), and there's
  no Forge source to patch. The real LLM-pilot seam is **`forge_py`**'s
  Python decision points, but that engine is absent here and not yet
  mature (turn-by-turn/combat incomplete). The experiment (≥30 paired
  games, Pearson r ≥ 0.90) is fully designed and the scaffolding
  (`analyst.py` LLM client, `run_ab_simulation`, correlation log) is
  ready, but there's no pilotable player to run it against today. Net: a
  valuable negative result that prevents a 2–4 wk dead-end; FP-001 stays
  parked with a precise unblock condition (see Parked plans). Optional
  cheap follow-up: add a Pearson-r helper beside `correlation_summary`.
  ✅ **Done 2026-05-22** — `forge_py_correlation.pearson_r()` +
  `correlation_summary` now returns `pearson_r`/`pearson_n` against the
  r ≥ 0.90 gate.

### Tier 1 — Worth doing soon

0. ~~**Curated real-oracle test fixture.**~~ ✅ Shipped.
   `tests/fixtures/real_oracles.py` holds 10 verbatim-Scryfall card
   entries covering every classifier role (`win_condition`, `wipe`,
   `tutor`, `draw`, `ramp`). `tests/test_real_oracle_fixture.py`
   self-tests via `EXPECTED_ROLE` so any new fixture entry must
   declare its expected role and any regex regression breaks a
   named parametrized test. Remaining synthetic-text classifier
   tests are intentional degenerate cases (empty string,
   "nothing-matches" text).

0a. ~~**Move `_resolve_deck_path` into `web/_helpers.py`.**~~ ✅
    Shipped during the 2026-05-13 blueprint refactor. Lives at
    `web/_helpers.py:32`; the 5 blueprints + `web/app.py` import
    it directly.

0b. ~~**Structured per-recommendation debug logging.**~~ ✅ Shipped.
    `_advisor_logging.log_decisions()` writes one line per rec to
    `<deck_dir>.parent.parent/_audit_decisions.log`; gated by the
    `COMMANDER_BUILDER_LOG_DECISIONS` env var so prod runs stay
    quiet. Called from `improvement_advisor._advise_steps`.

1. ~~**Wire `/api/forge_version` into the topbar badge.**~~ ✅ Shipped
   in commit `1ac9d53`.

2. ~~**Fix multi-jar selection bug in `detect_forge_version`.**~~ ✅
   Shipped in commit `23fc108` (parsed-version sort).

3. ~~**Pricing chart / query endpoint.**~~ ✅ Shipped.
   `pricing_series_for_deck()` + `/api/pricing_series?deck=<id>` +
   inline SVG sparkline on the deck dashboard (activates at ≥2
   captured points; tooltip per dot; trend label with $ delta + %
   change). 7 new tests.

4. ~~**Reject negative `total_price_usd`.**~~ ✅ Shipped in commit
   `43205d4`.

5. ~~**Auto-refresh dashboard when knowledge_log gains rows.**~~ ✅
   Shipped. Both `save_iteration` paths (post-sim verdict save +
   audit-only "Save audit (no sim)") now trigger a soft-refresh of
   the active deck on success so the iteration history /
   verdict-breakdown / pricing-sparkline pick up the new row
   without a manual reload.

6. ~~**Per-archetype win-rate breakdown.**~~ ✅ Shipped.
   `verdict_breakdown_for_deck()` + `/api/verdict_breakdown?deck=<id>` +
   "Verdict by audit version" panel that activates at ≥5
   iterations. Groups by `audit_version` with zero-padded
   {kept, reverted, neutral, pending, total}. 8 new tests.

7. ~~**Advisor: archetype-aware redundancy guard.**~~ ✅ Shipped.
   New `_filter_for_saturation()` + `staples.ROLE_SATURATION_THRESHOLDS`
   (ramp=12, draw=12, removal=10, wipe=6, protection=7, tutor=8,
   finisher=14). Applies in `advise()` after both heuristic and
   bracket_peers paths. Dropped adds surface as
   `AdviceReport.skipped_for_saturation` → `/api/audit` payload →
   UI summary line grouped by role. 16 new tests.

8. ~~**Advisor: bracket-peers reference mode.**~~ ✅ Shipped in
   commit `34dcfdb`. New `advise(source="bracket_peers")` path +
   `/api/audit?source=bracket_peers` + UI 3-way selector. Pulls
   top-5 highest-liked Moxfield decks for the same commander at
   the same bracket and frequency-ranks the diff against the
   user's deck. Falls back to EDHREC heuristic with
   `fallback_reason` set when no references found. 20 new tests.

### Tier 2 — Bigger but tractable

7. ~~**Proposed-deck price in audit response.**~~ ✅ Shipped.
   `/api/audit` returns `original_price_usd`, `proposed_price_usd`,
   `n_priced_cards_proposed`. UI shows `$X → $Y (Δ)` headline above
   the audit. SSE streaming endpoint emits the same fields.

8. ~~**Card-image lazy fetcher (FP-008).**~~ ✅ **Already shipped**
   (confirmed 2026-05-22; entry was stale). The suggestions panel
   (`app.js` `renderAddRow`) renders lazy (`loading="lazy"`,
   `decoding="async"`) thumbnails via `cardImageUrl()` → the local
   `/api/card_image/<size>/<name>` route, with click-to-expand
   (`openCardImageOverlay`). The route disk-caches Scryfall bytes
   (`web/_image_cache.py`: quota eviction + transient-retry) and serves
   `Cache-Control: …immutable`. Covered by `test_image_cache.py`.

9. ~~**Oracle-text-first card-reference store (FP-009).**~~ ✅ **Built
   2026-05-22.** New `oracle_store.py` — thin layers over the existing
   `scryfall_client` snapshot cache (no new datastore): `card_reference()`
   presentation alias, `check_errata()` (cached snapshot vs fresh Scryfall
   oracle drift), and `bulk_refresh()` + `commander-oracle-refresh` CLI
   (`--deck` / `--name` / `--all`, `--stale-days`, `--write`, `--json`).
   Read-only by default; rewrites drifted snapshots only with `--write`.
   17 tests (network stubbed). (`format_card_for_display` + `oracle_diff`
   already covered the rest of the FP-009 surface.)

### Tier 3 — Deferred until prerequisites exist

10. ~~**`commander-iterate --auto-propose` programmatic.**~~ ✅
    Shipped as the `commander-auto-curate` CLI (commits `b859463`,
    `023134e`, plus 25+ refinement commits). Full advisor → Claude
    curator → apply → optional Forge A/B sim pipeline with `--mode`
    presets, color-identity filter, protected-card list, and
    knowledge_log row writer. See [CHANGELOG.md](CHANGELOG.md)
    2026-05-15/16 entry for the full breakdown.

11. **Phase 3 ML training (FP-002).** 🔬 **REOPENED (2026-05-26) under the
    margin-regression framing** the 2026-05-22 NOT-VIABLE note called for.
    40-game soak rows now give a negative class (29 decks → 6 kept / 4
    reverted / 19 neutral) and a signed margin target.
    `scripts/margin_analysis.py` (pure stdlib) regresses it on deck-health
    features: curation is empirically ~neutral (mean +0.0009), one
    significant predictor (`wincon_protection` r=+0.45). Not yet a shippable
    model — needs ~80+ unique decks. See Parked plans +
    [docs/fp002-margin-analysis.md](docs/fp002-margin-analysis.md).

12. ~~**Concurrent Forge sims (FP-003).**~~ ✅ **Shipped** (2026-05-22,
    `0f8f945`). `forge_runner.run_ab_batch(jobs, runners)` runs A/B sims
    across cwd-isolated Forge profiles in parallel (≈2× throughput);
    second profile at `vendor/forge2`, recreatable via
    `scripts/setup_forge_profile.py`. The feasibility spike confirmed
    separate `cwd`-isolated profiles avoid file-locking races.

13. **Forge sim seed (FP-004).** No `--seed` flag in Forge 2.0.12.
    Variance-via-game-count works fine today. Watch upstream releases.

14. ~~**Settings UI + BYO LLM token (FP-011).**~~ ⬆️ **Promoted
    2026-05-22** to *Active → A1* (web config GET/PUT). The secret-scan
    hook already shipped; only the per-user config surface remains.

---

## Parked plans (big bets, blocked, or strategic forks)

> Items here are **deliberately out of the active queue**. Each is too
> big to start without a clear go-decision, blocked on data we don't
> have, or a strategic fork we want to keep visible. Move into the
> backlog above when its unblock condition fires.

### FP-001 — Replace Forge with a Python-native engine

Full rules-engine port: 6–12+ months of focused engineering. Rules-lite
"goldfish" sim: 1–2 weeks but useful only as consistency-metrics
supplement. **Forge AI replacement** (Claude/Ollama at decision points,
keep Forge's rule engine): 2–4 weeks; this is Phase 4 in the original
spec. Token cost: ~$0.10–$1.00 per game.

**Status: PARKED.** The wrapper we've built IS the streamlined Python
interface. Highest-leverage move toward "fewer draws / better signal" is
Claude/Ollama-piloted Forge AI, not a new engine. The **bounded go/no-go
spike ran 2026-05-22** ([docs/fp001-llm-pilot-spike.md](docs/fp001-llm-pilot-spike.md)):
**verdict NO-GO as scoped** — Forge 2.0.12 is an unpilotable compiled-JAR
black box (no decision-injection seam; no source), so the LLM-pilot idea
can't host on Forge at all. It's **redirected to `forge_py`** (Python
decision points an LLM can stand in for) and **gated** on a precise
unblock condition: promote only when `forge_py` is present in the
workspace AND plays a full turn-by-turn + combat game that already
correlates with Forge (its own P3+P5 milestone). At that point the spike
is ~1–2 days (add an `LLMAgent` over `analyst.py` + a Pearson-r helper;
run ≥30 paired games for r ≥ 0.90). Experiment design + reusable
scaffolding are documented in the memo.

### FP-002 — Phase 3 ML predictor

`ml_dataset.py` ready (25 features, deck-level train/eval split, no
leakage). **Status: REOPENED under the margin-regression framing
(2026-05-26); first result in — see
[docs/fp002-margin-analysis.md](docs/fp002-margin-analysis.md).**

The original *kept-vs-reverted classifier* was concluded NOT VIABLE on
2026-05-22 because, after the A/B win-attribution fix (`e8777b6`), the
curator's swaps almost never made a deck *worse* (detune depths 0–10 →
11 kept / 3 neutral / **0 reverted**) — no negative class. STATUS.md
proposed the unblock: *"regress on improvement margin."*

The accumulated **40-game** soak rows deliver exactly that. Both the
blocker and the framing are now resolved:
- **Negative class exists** at high confidence: of 29 decks (≥40 games,
  11,960 games total), **kept=6 / reverted=4 / neutral=19**. Curation
  *can* hurt; it just usually doesn't.
- `scripts/margin_analysis.py` (pure stdlib — sklearn/numpy/scipy are
  NOT installed) regresses win-rate margin on pre-sim deck-health
  features. **Finding: curation is empirically ~neutral** (mean margin
  +0.0009; 19/29 neutral). **One feature clears significance:**
  `wincon_protection` `r=+0.45` (t=2.6) — curation pays off most on decks
  that already protect their win; near-useless on decks with big role
  deficits.

**Not yet a shippable predictor:** n=29 decks is too thin and only one
feature is significant. Graduation needs **more unique decks (~80+), not
more games per deck**. Actionable today (no model): point curation at
already-coherent decks; fix structure (F2 `under_built`) before curating.
Covered by `tests/test_margin_analysis.py` (13 tests).

### FP-003 — Concurrent Forge sims

✅ **SHIPPED (2026-05-22, `0f8f945`).** `forge_runner.run_ab_batch(jobs,
runners)` runs A/B sims across cwd-isolated Forge profiles in parallel
(≈2× throughput). Second profile at `vendor/forge2`, recreatable via
`scripts/setup_forge_profile.py`. The feasibility spike confirmed
separate `cwd`-isolated profiles avoid file-locking races.

### FP-004 — Forge sim seed

Forge 2.0.12 has no `--seed`. JVM bytecode-instrumentation would be
brittle. **Status: PARKED.** Watch upstream.

### FP-006 — Web GUI

✅ Shipped. All four suggestion-quality gates closed in 2026-04-27:
universal-staples exclusion, frequency labels, role categorization,
diagnosis-driven re-ranking. Backend + minimal UI live; ongoing polish
in the active backlog.

### FP-007 — Unified MTG application

Single web/desktop program consolidating deck testing + card reference
+ rules + library + replays. ~6–10 weeks. **Status: PARKED.** Substrate
ready (`mtg_cards/`); product-readiness gates not yet met. Ship FP-006
fully first.

### FP-008 / FP-009 — Card images + oracle-text store

Already-built substrate (`oracle_snapshots/`, `forge_py.cards`,
`scryfall_client.refresh_card`). Promoted to active backlog (Tier 2).
**Oracle text is authoritative; images are decorative** — a system
that *interprets* cards uses oracle, not OCR'd images.

### FP-010 — Package web app as desktop EXE

PyInstaller + pywebview, ~16 h. Bundle Forge + JRE, first-run downloader
for the 180 MB `mtg_cards/` folder. **Status: PARKED.** Don't start
until the web app demonstrably works for a full iteration cycle on real
decks (≥5 audits via the browser without touching a CLI).

### FP-011 — BYO LLM token

Per-user config file with redacted GET / permissions-restricted PUT.
Pre-commit hook scans staged diffs for `sk-ant-`, `Bearer `, JWT
prefixes. **Status: PROMOTED 2026-05-22 → Active → A1.** Secret-scan hook
shipped (`803debe`); the remaining web config GET/PUT surface is now an
active backlog item.

### FP-012 — Autonomous deck improvement agent

The everything-bagel: takes a Moxfield URL, learns the deck's intent,
converges on a better version unattended. Multi-arm bandit / Bayesian
opt for swap selection. ~120 h total across 8 components. **Status:
PARKED (full agent); slices 1+2 SHIPPED 2026-05-22.** All three gate
conditions are met — knowledge_log ≥150 rows, the proposer is
programmatic (`commander-auto-curate`), and `forge_runner` supports
concurrent JVMs (`run_ab_batch`, FP-003).
- **Slice 1 (A2):** `commander-improve` fixed-N greedy keep-if-better loop.
- **Slice 2:** `commander-improve --strategy bandit` — treats candidate
  swaps as arms and learns which move the win rate via an epsilon-greedy
  / UCB1 policy (`bandit.py`; per-arm reward = seat-attributed A/B sim
  margin; advances the base deck on improvement).
Still parked for the full agent: intent-learning, Bayesian opt, and the
unattended multi-deck orchestration. North star, not done.

### FP-013 — Project-tuned LLM (moonshot)

LoRA fine-tune Llama 3.1 8B / Qwen 2.5 7B on accumulated audit
manifests + sim outcomes + Magic rules + oracle snapshots. ~$80–$200
LoRA cost on A100. **Status: PARKED, do not promote.** Needs 2000+
iteration rows; realistic timeline 18–30 months out.

### Sister projects

- **`forge_py`** at `C:\dev\forge_py\` — Python-native goldfish + combat
  simulator. Stays independent until its turn-by-turn (P3) and combat
  (P5) produce signal that correlates with Forge. Then fold the engine
  in as a fast pre-filter. User stated intent (2026-04-27): *"when
  forge_py works well I do want it in commander_builder."*
- **`mtga_draft_helper`** — separate project family; shares
  `mtg_cards/` but no code-level dependency. No merge planned.

---

## Decisions recently made (recent context)

For older decisions see [docs/architecture.md](docs/architecture.md#key-decisions).

- **2026-05-14 — Synthetic test text is insufficient.** The chrome-
  audit follow-up caught 9 classifier bugs that all passed
  hand-written unit tests because the synthetic oracle text happened
  to match overly-permissive regexes. Real Scryfall data (with
  `\n` paragraph breaks, basic-land-type substitutions like "Forest
  card", typed alternation, etc.) exposed them. New tests pin
  byte-exact real card text; a shared real-oracle fixture is in the
  Tier 1 queue.
- **2026-05-14 — Manabase × bracket_peers dedup.** The two sources
  legitimately overlap on shock lands for 5-color decks. Without
  explicit dedup the proposed `.dck` had duplicate card lines
  (illegal in singleton Commander; Forge silently rejected).
  Dedup-by-card-name lives in `_advise_steps`, manabase wins on
  collision (prepended first + more-specific source tag).
- **2026-05-13 — Web blueprint split.** `web/app.py` 2,368 → 302
  lines via 5 Flask Blueprint factories
  (`routes_audit`/`_sim`/`_decks`/`_dashboard`/`_meta`) plus
  `_helpers.py` for Flask-independent utilities. Each blueprint is
  built by `make_<group>_blueprint(...)` closing over the deck dir +
  shared helpers; the orchestrator just creates the Flask app and
  registers the 5 factories.
- **2026-05-13 — Streaming audit pipeline.** `advise()` now wraps an
  `_advise_steps()` generator that yields `AdvicePhase` events
  (diagnosis → manabase → primary → complete). The new
  `/api/audit/stream` SSE endpoint drives the generator; the client
  renders manabase recs ~50ms in while Claude is still working.
- **2026-05-13 — Doc consolidation.** Reduced 15 markdown files to 4
  (README, STATUS, CHANGELOG, docs/architecture). Audit retrospectives,
  session-state snapshots, individual handoffs deleted as superseded.
  This file is now the single source for *operational state*.
- **2026-05-13 — Card-name validator scope.** Validate every card in
  `report.recommendations`, not just Claude-sourced ones. Heuristic
  EDHREC recs validate via cache (near-free); uniform pass means
  callers don't have to special-case.
- **2026-05-13 — `Retry-After` cap at 30s.** EDHREC's CDN occasionally
  sends `Retry-After: 300+` during incidents. Cap prevents a
  misbehaving server from pinning the audit; user prefers degraded
  result over indefinite block.
- **2026-04-29 — Sprint 1C reframed.** "JVM persistence" → "per-pod
  intra-pod abort." Original required Forge source mods and only saved
  ~10s/pod; reframed needs zero Forge changes and saves 20-50% on
  lopsided matches. See CHANGELOG.
- **2026-04-28 — Track 2 (forge_py multi-deck sim) runs
  opportunistically, not as a fast path.** forge_py's combat is shallow
  (single-attacker, no flying/reach/first-strike); systematic misjudge
  of control vs aggro is worse than waiting 3 min for Forge truth.
  Flip default only when r ≥ 0.90 per archetype across ≥30 paired rows.

---

## Stats

- **Modules**: ~30 production (advisor split into orchestrator + 7
  sub-modules; web split into orchestrator + 5 blueprints + helpers)
- **Tests**: ~1287 passing fast lane (+slow with `--run-slow`)
- **Test wall time**: ~167s offline
- **CLI entry points**: 14
- **Shared with `forge_py`**: `C:\dev\mtg_cards\` cache
  (`MTG_CARDS_DIR` env var override available); ~32k per-card snapshots
  + 180 MB bulk dump.
- **Imported decks on disk**: B3=99, B4=115, B5=56 (approx, includes
  [USER]-tagged decks).
- **Knowledge log iterations**: 300+ rows. IDs < 314 are pre-fix
  measurement artifacts of the A/B name-attribution bug (`e8777b6`),
  kept as archive; train/analyze post-fix rows only via `--min-id 314`.

---

## How to update this file

- Tree state at the top stays current — bump the test count and the
  commit list whenever new work lands.
- Open backlog is the queue. Items move out when shipped (entry added
  to CHANGELOG) or when reclassified as parked.
- Parked plans are the long-tail. Items move out only when an unblock
  condition fires.
- Don't duplicate CHANGELOG content here — link out, don't restate.
