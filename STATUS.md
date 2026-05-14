# Status — current state of the project

> Living tracker. Read this first to find out *"what's the project up to
> right now?"* without scrolling chat history.
>
> **Three sections** — *State of the tree* (right now), *Open backlog*
> (next work, ranked), *Parked plans* (deliberately deferred). History
> of what landed lives in [CHANGELOG.md](CHANGELOG.md); architecture +
> conventions live in [docs/architecture.md](docs/architecture.md).

**Last updated:** 2026-05-15 (overnight 7-phase enrichment session)
**Phase status:** Phase 2 complete + FP-006 web GUI shipped + 80+ commits
on `feature/2026-04-28-session` ahead of `master`. Phase 3 (ML
predictor) still data-gated.

---

## State of the tree

- **Tests:** 875 passing (+9 from the prior recap), ~111s offline. Zero
  warnings under `python -W default`.
- **Branch:** `feature/2026-04-28-session` (80+ commits ahead of
  `origin/feature/2026-04-28-session`).

### 2026-05-14/15 overnight session (7 phases)

Each phase was a self-contained commit with live-server verification:

1. **UI split for applied vs suggested adds** (`b5ab5ea`) — the
   audit panel now visually separates "drop-in" recs (in
   proposed_text) from "needs manual cut" recs.
2. **Proposed-deck pricing** (`ef33f58`) — `$X → $Y (Δ)`
   headline in the audit panel. Tier-2 backlog item.
3. **EDHREC `/tags/<tribe>` integration** (`5446e7d`) — tribal
   decks pull the broader-archetype tag page (~250-400 cards
   beyond the commander page).
4. **Theme detection + multi-tag pages** (`756a6c2`) — Tokens
   / Spellslinger / Aristocrats / Lifegain / Reanimator /
   Equipment / Artifacts / Enchantress detection. Up to 3
   tag pages per audit.
5. **Card thumbnails + click-to-zoom** (`f57151b`) — FP-008
   substrate. Lazy-loaded 60×84 inline images, full-size
   overlay on click.
6. **EDHREC salt-list integration** (`553187e`) — bracket-fit
   warnings. Cyclonic Rift / Smothering Tithe / Stasis get
   yellow pills when audit recommends them.

### Prior 2026-05-13/14 chrome-audit session (preserved for context)
- **2026-05-13/14 session deltas** (chrome-audit-driven; most-recent
  first):

  ```
  085c256 fix(staples): five more pattern misses caught by bulk audit
  afc57ac fix(advisor): dedupe adds across manabase + primary source
  b2ff2b9 fix(staples): real-Scryfall text for Cyclonic Rift / Crux / Coalition
  f190646 docs(architecture): document advisor + web blueprint refactors
  f548d28 refactor(web): final stage of blueprint split (closes #3.1)
  e4cd164 refactor(web): extract deck-edit → routes_decks (stage 4)
  2622ca5 refactor(web): extract sim → routes_sim (stage 3)
  c7788fc refactor(web): extract audit/advise → routes_audit (stage 2)
  9a7c12b refactor(web): extract pure helpers → _helpers.py (stage 1)
  c9ec4d2 fix(advisor): recalibrate saturation thresholds
  0410680 fix(dashboard): wire detect_tribal_type into theme_tags
  52f0b78 feat(ui): manabase-preview + abort-in-flight stream cancel
  e9514cd feat(ui): progressive audit render via SSE
  021dd3a feat(web): /api/audit/stream SSE endpoint
  f69ccca refactor(advisor): split advise() into _advise_steps generator
  54dd71b fix(ui): suppress 0% pill, render source badges
  c98d3a2 fix(advisor): consolidate role classifier + wipe patterns
  ```

  Net: closed the entire 2026-05-13 ranked bug list (1.1/1.2/1.3/1.4,
  2.1, 3.1) plus caught and fixed 9 production-only bugs during live
  re-verification.

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

1. `cd C:\dev\commander_builder && python -m pytest tests/ -q` — confirm
   green.
2. `git log --oneline master..HEAD` — see what's on the feature branch.
3. Skim *Open backlog* below, pick an item, or jump into the web app
   (`python -m commander_builder.web`) and run a propose-swap.

---

## Open backlog (ranked)

### Tier 1 — Worth doing soon

0. **Curated real-oracle test fixture.** The 2026-05-14 session caught
   9 production-only bugs that hid behind synthetic-text unit tests.
   Codify the "use byte-exact Scryfall oracle text" lesson into a
   shared fixture (`tests/fixtures/real_oracles.py`) and migrate
   existing classifier tests to source from it. ~1 h.

0a. **Move `_resolve_deck_path` into `web/_helpers.py`.** The blueprint
    refactor left this helper in `web/app.py` and each of the 5
    blueprint factories takes it as a parameter. Moving it to
    `_helpers.py` lets blueprints import directly and shrinks
    `app.py`'s argument-threading. ~30 min.

0b. **Structured per-recommendation debug logging.** Tier-3 #3.4 from
    the original audit. Single line per rec
    (`card=Cyclonic Rift role=wipe source=heuristic`) feeds a
    `_audit_decisions.log` file so the next misclassification surfaces
    in seconds, not via Chrome screenshot. ~45 min.

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

7. **Proposed-deck price in audit response.** `/api/audit` returns
   `proposed_text` but doesn't aggregate its price. Compute post-swap
   total so the cost-evolution chart can show per-swap delta. Needs
   threading through the audit pipeline. ~2–3 h.

8. **Card-image lazy fetcher (FP-008).** Render card images alongside
   oracle text in the suggestions panel. Scryfall image CDN, lazy-fetch
   to `mtg_cards/images/normal/<scryfall_id>.jpg`. ~3 h.

9. **Oracle-text-first card-reference store (FP-009).** Already-built
   substrate (`oracle_snapshots/`, `forge_py.cards.get()`, parity API
   in `scryfall_client`). Missing: presentation helper, errata diff
   tooling, bulk-refresh CLI. ~4 h.

### Tier 3 — Deferred until prerequisites exist

10. **`commander-iterate --auto-propose` programmatic.** Replace the
    manual audit-prompt paste with a Claude API call.
    [proposer.py](src/commander_builder/proposer.py) has the wiring
    sketch; `claude_propose` body needs filling in. ~30 lines + a few
    integration tests with mocked `anthropic.Anthropic`. Promote when
    a routine LLM workflow becomes useful.

11. **Phase 3 ML training (FP-002).** Predict swap outcomes from deck +
    swap features. `ml_dataset.py` ready (25 features, deck-level
    split). Needs 200+ logged iterations before training is honest.
    Today: a few rows.

12. **Concurrent Forge sims (FP-003).** Two JVMs in parallel could
    halve pool-curation wall time. Needs a 30-min feasibility spike
    (do separate `cwd`-isolated profiles avoid file-locking races?).
    Cheap to attempt; not yet a bottleneck.

13. **Forge sim seed (FP-004).** No `--seed` flag in Forge 2.0.12.
    Variance-via-game-count works fine today. Watch upstream releases.

14. **Settings UI + BYO LLM token (FP-011).** Per-user config at
    `%LOCALAPPDATA%\commander-builder\config.json` with redacted GET +
    permissions-restricted PUT. Promote when sharing with anyone
    beyond the original developer.

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
interface. Highest-leverage move toward "fewer draws / better signal"
is Claude/Ollama-piloted Forge AI, not a new engine.

### FP-002 — Phase 3 ML predictor

`ml_dataset.py` ready (25 features, deck-level train/eval split, no
leakage). Needs 200+ rows across 5+ unique decks. **Status: PARKED.**
Triggered automatically when row count crosses threshold —
`commander-status` reports it.

### FP-003 — Concurrent Forge sims

Two `cwd`-isolated Forge profiles in parallel. **Status: PARKED.**
Cheap to attempt; ~30-min feasibility spike. Currently nobody runs
curation enough to feel the pain.

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
prefixes. **Status: PARKED.** Architecture documented; promote when
shared with anyone beyond the original developer.

### FP-012 — Autonomous deck improvement agent

The everything-bagel: takes a Moxfield URL, learns the deck's intent,
converges on a better version unattended. Multi-arm bandit / Bayesian
opt for swap selection. ~120 h total across 8 components. **Status:
PARKED.** Promote when knowledge_log has ≥150 rows AND
iteration_loop's proposer is programmatic AND forge_runner supports
concurrent JVMs. North star, not next step.

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
- **Tests**: 820 / 820 across ~30 test files
- **Test wall time**: ~37s offline
- **CLI entry points**: 14
- **Shared with `forge_py`**: `C:\dev\mtg_cards\` cache
  (`MTG_CARDS_DIR` env var override available); ~32k per-card snapshots
  + 180 MB bulk dump.
- **Imported decks on disk**: B3=99, B4=115, B5=56 (approx, includes
  [USER]-tagged decks).
- **Knowledge log iterations**: few rows; mix of integration tests +
  recent real saves.

---

## How to update this file

- Tree state at the top stays current — bump the test count and the
  commit list whenever new work lands.
- Open backlog is the queue. Items move out when shipped (entry added
  to CHANGELOG) or when reclassified as parked.
- Parked plans are the long-tail. Items move out only when an unblock
  condition fires.
- Don't duplicate CHANGELOG content here — link out, don't restate.
