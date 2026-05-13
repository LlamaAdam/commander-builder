# Status — current state of the project

> Living tracker. Read this first to find out *"what's the project up to
> right now?"* without scrolling chat history.
>
> **Three sections** — *State of the tree* (right now), *Open backlog*
> (next work, ranked), *Parked plans* (deliberately deferred). History
> of what landed lives in [CHANGELOG.md](CHANGELOG.md); architecture +
> conventions live in [docs/architecture.md](docs/architecture.md).

**Last updated:** 2026-05-13 (doc consolidation session)
**Phase status:** Phase 2 complete + FP-006 web GUI shipped + 11 commits
on `feature/2026-04-28-session` ahead of `master`. Phase 3 (ML
predictor) still data-gated.

---

## State of the tree

- **Tests:** 674 passing, ~25s offline.
- **Branch:** `feature/2026-04-28-session`
- **Recent commits** (oldest first, on this feature branch):

  ```
  95e3197 feat(speedup): Track 1 — parallel pods, early-stop, intra-pod abort
  db89514 feat(track2): forge_py correlation harness (opt-in)
  8564195 fix(moxfield): convert pipe-delimited lines to parens format
  728be3f feat(ux): bracket auto-inference + modal scroll fix
  49f0e5c feat(web): LLM analyst, knowledge log, padding, error collector, UI surface
  e3e5a88 feat(audit): card-name validator flags Claude hallucinations
  8d2b694 feat(edhrec): retry transient HTTP failures with exponential backoff
  4ebb8a9 feat(forge): detect bundled jar version + warn when stale
  6d92fd1 feat(klog): capture deck pricing snapshot in iteration manifest
  5fa772c feat(edhrec): honor Retry-After header + log each retry
  <doc consolidation commit>
  ```

- **Modules:** ~30 production. Recent additions: `forge_py_correlation`,
  `improvement_advisor` (Claude analyst path), web app expansion.
- **CLI entry points:** 14. See [README.md](README.md#cli-commands).
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

- **Modules**: ~30 production
- **Tests**: 674 / 674 across ~30 test files
- **Test wall time**: ~25s offline
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
