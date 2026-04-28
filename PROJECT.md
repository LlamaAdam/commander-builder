# PROJECT.md — Commander Builder

This document is the source of truth for the project. Every Claude Code session should read this first to recover context. When something here becomes outdated, update it — don't let drift accumulate.

---

## What this project is

A command-line tool that takes an MTG Commander deck, runs it through scripted Forge headless 4-player pod matches, measures performance, then iterates: propose modifications → re-simulate → record what helped and what didn't → feed those learnings into the next iteration.

The primary use case is: "I have a Commander deck. Make it better, prove it's better, and learn what kinds of changes actually move the needle so future audits get smarter."

It is **not** a deck builder from scratch, not a Moxfield clone, not a real-time game client. It's a closed-loop deck improvement engine where Forge provides ground-truth simulation and an LLM (initially Claude) acts as the analyst that reads sim deltas and decides what to try next.

## What problem this solves

A separate tool — the **Moxfield Commander Audit prompt** — already proposes card swaps based on community reference decks and a statistical hand-and-disruption sampler (Step 5.6 of that prompt). The statistical sampler catches consistency regressions but cannot tell you whether the post-swap deck *actually wins more games* in real games against real opponents.

Forge has a working rules engine and built-in AI capable of piloting both sides of a match. Running scripted 4-player Commander pods gives a more grounded answer to "is this deck actually better." Better still: feeding the swap-vs-delta history back into an LLM analyst lets future modification proposals be informed by what worked on past iterations — not just generic "include staples" advice.

The honest limitation: any answer is bounded by the AI pilot's quality. Phase 1 uses Forge's heuristic AI; Phase 2 (optional) swaps in Claude as the in-game pilot to raise the ceiling.

## How the pieces fit together

```
audit prompt (Moxfield, separate project)
  proposes initial swaps
        ↓
Commander Builder
  ├── forge orchestrator: simulates current vs. post-swap (real games, 4-player pods)
  ├── results parser: extracts win rate, game length, key events
  ├── LLM analyst: reads sim deltas + swap diff, judges whether swap was good,
  │                writes a short "what worked / what didn't" note
  ├── modification proposer: given current deck + analyst notes,
  │                          proposes the next iteration's swaps
  └── knowledge log: SQLite of (deck_id, iteration, swap, deltas, analyst_note)
        ↓
After enough iterations: retire LLM analyst in favor of a learned model
                         trained on the knowledge log (Phase 3)
```

## Phased plan

The project is split into phases so each phase delivers value independently and can be validated before committing to the next.

### Phase 1A — Forge verifier ✅ COMPLETE (2026-04-26)

**Outcome:**
- Java + Forge installed unattended into `vendor/`
- 2-player constructed sim: exit 0, ~30s for 3 games, "Match Result: Ai(1)-...: 2 Ai(2)-...: 1" parseable
- 4-player commander sim: exit 0, ~5min for 3 games, full per-player score line parseable
- Findings + stdout/stderr captured in `verify_output/` for Phase 1B parser design

**Phase 1B can now design the log parser against real captured output rather than guessing.**

### Phase 1A — Forge verifier (original brief)

**Deliverable:** A standalone Python script that confirms Forge headless works on the user's machine and surfaces what Forge's output actually looks like.

**What it does:**
- Locates Forge install, JAR file, Java runtime, userdata directory
- Lists existing sample decks
- Runs a small (3-game) 2-player constructed match — minimum viable test
- Runs a small (3-game) 4-player commander match if 4+ commander decks exist
- Captures stdout, stderr, and Forge's `forge.log`
- Saves everything to `verify_output/` for human review

**What it explicitly does NOT do:**
- Touch Moxfield
- Convert any decks
- Parse results — the goal is to *see* the output format before writing a parser

**Why this comes first:** The Forge wiki documents a syntax (`java -jar forge.jar sim -d deck1 deck2 ... -f commander -n 100`) but doesn't show what the output looks like. Writing a parser without seeing real output is guesswork. Verify, then build on verified ground.

**Done criteria:** User runs the verifier, both tests complete (or commander test skipped with a clear reason), user pastes back the output files. We then know:
- Does headless work on this machine?
- What's the log/stdout format?
- Does 4-player commander work, or only 2-player?
- Are there errors (missing cards, AI hangs, JavaFX issues) we need to handle?

### Phase 1B — Forge orchestrator pipeline

**Deliverable:** End-to-end Forge pipeline. Input: a `[USER]`-tagged deck and a target bracket. Output: win-rate report against a curated opponent pool at that bracket.

**Components:**
- ✅ `moxfield_import.py` — pulls Moxfield decks via v3 API, converts to Forge `.dck`, supports bulk-by-bracket and `--user` tagging. (Done 2026-04-26.)
- `edhrec_client.py` — discover commander pages, scrape "Average Deck" and tier-preset Moxfield URLs, hand them to `moxfield_import` for the actual fetch. EDHREC is a *URL discoverer*, not a fetcher.
- ✅ `forge_runner.py` — invoke Forge headless from `cwd=vendor/forge/`, capture output. Hard requirement: cwd must be the install dir or `forge.profile.properties` is ignored and Forge crashes during init. Captures stdout, returncode, duration, timeout flag, and (defensive) `forge.log` tail.
- ✅ `log_parser.py` — parse the captured stdout. Authoritative parse points discovered in Phase 1A:
  - `Match Result: Ai(N)-<deck>: <wins>` — per-deck match record (use this, ignore the buggy per-player `Game Outcome:` lines that report all 4 players as "won" in 4-player games).
  - `Game Result: Game N ended in <ms>` — per-game wall time.
  - `An unsupported card was requested:` — DB coverage gaps (count per deck; useful as a deck-quality flag).
  - `default implementation of confirmAction is used by <card>` — AI-struggle indicator (high counts mean Forge AI couldn't pilot the deck well, e.g. cEDH).
  - `_normalize` strips `[USER]` prefix, `[B<n>]` suffix, and `.dck` so result names match query names regardless of decoration. Order matters: strip `.dck` before the bracket regex.
- ✅ `game_analyzer.py` — sits on top of `log_parser` and adds per-game telemetry: end turn, winner attribution (from `Game Result: ... has won!`), draw detection (`Stopping slow match as draw`), per-deck life curves (starting/ending/min/max, damage taken, life gained), elimination turn, per-game confirmAction count. Aggregates across games into `MatchAnalysis.per_deck_summary()` for "fastest_elimination_turn", "avg_ending_life", "wins", "eliminations".
- ✅ `pool_curator.py` — **tournament-style opponent curation.** Pre-flights each candidate (1-game smoke; rejects on timeout/non-zero exit/no games). Runs round-robin pods (`schedule_pods` greedy with seed-stable tie-break, ~3 pods per deck). Aggregates wins per candidate. Splits top 6 into Pool A (ranks 1/3/5) and Pool B (ranks 2/4/6) with archetype/color diversity check + one-shot swap. Persists `_pools/B<n>.json` (with `suspected_inflated` flag, win_rate, confirm_action_per_game persisted via overridden `to_dict`) and `_pools/B<n>_analysis.json` (per-pod `MatchAnalysis`).
- ✅ `run_match.py` — main user-facing CLI. `python -m commander_builder.run_match --user "[USER] Foo [B3].dck" --bracket 3 --games 3 --pods 2`. Loads the curated pool (or falls back to first N on-disk B<n> opponents). Builds pods rotating opponents across pods. Per-game user telemetry → `MatchupReport` with avg ending life, damage taken, fastest elimination, per-opponent record. Persists `_matches/<deck>_B<n>_<timestamp>.json`.
- ✅ `compare_versions.py` — old-vs-new deck comparison (Phase 2 prep). Two modes: `pod` (4-player same-table, recommended) puts both versions in one pod with 2 fillers for a direct head-to-head signal that preserves multiplayer dynamics; `1v1` uses Forge's constructed format for pure efficiency comparison. Filler-pair rotation averages out single-pair bias. Outputs `ComparisonReport` with head-to-head record, per-version VersionStats, and card-level diff parsed from the .dck [Main] sections. Persists `_compare/<old>__vs__<new>_B<n>_<timestamp>.json`.

**Test coverage (`tests/`):**
- 144/144 passing across 11 test files (Phase 1B + Phase 2 scaffolding). Suite runs in ~0.6s.
- `test_log_parser` (13 — incl. active-player attribution), `test_game_analyzer` (10), `test_pool_curator` (11), `test_run_match` (10), `test_moxfield_import` (17), `test_compare_versions` (14), `test_snapshot_deck` (9), `test_scryfall_client` (16, mocked HTTP), `test_knowledge_log` (11), `test_analyst` (12), `test_moxfield_push` (8), `test_ml_dataset` (13).
- Pure-Python helpers covered; Forge subprocess paths exercised via the real harness in `scripts/preflight_b4_batch.py` etc.; Scryfall HTTP mocked via monkeypatch so unit tests stay offline.

**Opponent selection logic (revised):**
- **Bracket-locked.** User decks are built for specific brackets; opponents must match. Cross-bracket sims are noise.
- **Tournament-curated.** Pull a candidate pool from Moxfield at the bracket, play them against each other in pods, retain the top performers. Re-run periodically to refresh against current meta. Hand-curated opponents introduce my biases and miss meta shifts.
- **EDHREC discovery on top of Moxfield fetch.** EDHREC's "Average Deck" and tier-preset links resolve to Moxfield URLs; pull those URLs and run the existing import path. One fetch backend, two discovery sources.
- **Diverse commanders.** Filter the curated pool so no two opponents share a commander.

**Run shape:**
- Default: 10 games per matchup × 3 opponents = 30 games. Drops the 100-game default in favor of faster iteration; raise when statistical confidence matters.
- Phase 2 comparison: 30 games × 3 opponents × 2 deck versions = 180 games, ~6 hours wall time at observed pace (73-120s/game).
- RNG seeds: Forge 2.0.12 has no documented `--seed` flag. Mitigation: compare current vs post-swap by running both against the *same* curated opponent pool with enough games that variance averages out (10+ per matchup).

### Phase 2 — LLM analyst + iteration loop (the core of "Commander Builder")

**Deliverable:** Closed-loop deck improvement. Input: a Commander deck. Output: an iterated deck plus a log of every change tried and how it performed.

**The current LLM proposer is the Moxfield audit prompt** (`prompts/moxfield_audit_v3.md`) — a manually-paste-into-Claude workflow with web fetches against Moxfield/EDHREC/Scryfall, a blind-build-then-diff methodology, and a structured swap manifest output. It's the upstream of `compare_versions.py` in the Phase 1B pipeline. Full integration documented in `docs/audit_workflow.md`. The audit prompt, snapshot helper, and compare_versions together form the "manual one-iteration loop"; `iteration_loop.py` below automates the loop.

**Components added on top of Phase 1B:**
- ✅ `prompts/moxfield_audit_v3.md` — Moxfield audit prompt (the LLM proposer). Versioned in-repo so prompt drift is tracked and Step 8 self-improvements land as `_v4.md`, `_v5.md`, etc.
- ✅ `snapshot_deck.py` — pre/post-audit `.dck` snapshot for compare_versions.
- ✅ `docs/audit_workflow.md` — end-to-end workflow doc, plus a deferred design space for routing simple LLM tasks to a local Ollama model when there's cost pressure.
- ✅ `scryfall_client.py` — cached Scryfall card-metadata lookups. Powers the previously-stubbed color identity in `pool_curator._read_color_identity` (commander → WUBRG identity, partner pairs merged). Disk-cached so repeat queries don't hit the network.
- ✅ `knowledge_log.py` — SQLite store of every iteration. Schema: `iterations` table with deck_id, parent_id (lineage chain), audit_manifest JSON, sim_report JSON, verdict, win_rates, margin, deck_snapshot text. Public API: `record_iteration`, `update_verdict`, `get_iteration`, `iterations_for_deck`, `recent_iterations`, `stats_summary`. Schema versioned at module level for explicit migration when fields evolve.
- ✅ `analyst.py` — verdict router. `AnalystInput` (audit_manifest + sim_report) → `Verdict` (label/confidence/reasoning/lessons). Three backends: `heuristic_verdict` (deterministic, no LLM, runs by default), `claude_verdict` (stub for Claude API), `ollama_verdict` (stub for local Ollama). Router escalates from heuristic to LLM only when confidence is low — saves tokens on obvious cases. The 18-of-20-draws case from the Hakbal/Hash smoke test is handled explicitly (returns `neutral` with low confidence and an `decks_drew_too_often` lesson).
- ✅ `iteration_loop.py` — orchestrator. `run_one_iteration(deck, bracket, audit_manifest, ...)` runs compare → analyst → record_iteration → return next_action. CLI accepts a JSON manifest path. Multi-iteration automation deferred until the programmatic LLM proposer replaces the manual paste step.
- ✅ `moxfield_push.py` — closes the loop's "push to Moxfield" step. `dck_to_textarea` renders a .dck as the bulk-edit format Moxfield accepts; `prepare_push` writes to clipboard (via optional pyperclip) for paste, or falls back to stdout. `_api_push` is a stub raising NotImplementedError until token auth is wired.
- ✅ `ml_dataset.py` — Phase 3 scaffolding. Defines `FEATURE_NAMES` (25 columns), `extract_features(Iteration) → FeatureRow`, `build_dataset(...)`, `split_train_eval(...)` with deck-level splits (no leakage), and `dataset_summary()`. No trainer — that lands when iteration count clears the documented minimum (200+ logs, 5+ unique decks).
- `proposer.py` — programmatic LLM proposer that replaces the manual audit-prompt paste step. Initially a wrapper that calls Claude with the audit prompt as a system message; later, can route simpler proposals through Ollama. Not yet built.

**Iteration termination:**
- Max iterations (default 5) reached
- Win rate plateaus (3 consecutive iterations with delta < 2%)
- User aborts

**Cost honesty:** LLM analyst calls are cheap (a few dollars per full iteration). The simulation cost is the wall time of Forge games, not API dollars. Phase 2 with Forge AI piloting both sides is realistic to run overnight on the user's machine.

**What gets logged for Phase 3:**
- Every swap with full card-level details (name, role, CMC, color)
- Pre-swap and post-swap simulation metrics
- Analyst verdict and reasoning
- Whether the swap was kept

This log becomes the training data for Phase 3.

### Phase 3 — Learned modification predictor (replaces or augments LLM proposer)

**Deliverable:** A model that predicts which swaps are likely to improve a deck, trained on the Phase 2 knowledge log.

**Hard prerequisite:** Phase 2 has produced enough iterations that there's actual training data. Realistic minimum: 200+ logged swaps across diverse decks. Below that, the LLM analyst is better than any model we'd train.

**Model shape (to be designed when we get there):**
- Likely a feature-engineered classifier or gradient boosting model rather than a deep model — the dataset will be small.
- Features: current deck composition vector, proposed swap (cards in, cards out), commander identity, target bracket.
- Target: did the swap result in measurable sim improvement (binary or continuous delta).

**What Phase 3 does NOT do:**
- Replace the LLM analyst's reasoning text. The analyst still produces human-readable explanations; the model just handles the proposal step.
- Train on synthetic data. Real iteration data only.

**Design implication for Phase 2:** Log everything in a structured, model-friendly format from day one. Don't store free-form text where a structured field would do.

### Phase 4 (optional) — Claude as in-game pilot

**Deliverable:** Replace Forge's heuristic AI with Claude API calls at decision points during simulation. Decision quality jumps significantly; cost rises significantly.

**Cost honesty:** Estimated $200–400 in API charges for a full audit comparison (3 opponents × 2 deck versions × 100 games). Wall time 24–60 hours. This is for decks the user genuinely cares about, not routine audits.

**Hard prerequisite:** Phases 1A, 1B, and 2 working cleanly. Phase 4 also requires reading Forge's `forge-ai` Java module to understand how to expose decision points externally — this may be a significant engineering lift.

This phase is optional because Phase 2's loop produces real value with Forge's built-in AI. Phase 4 is only worth pursuing if the user wants the highest-fidelity answer for high-stakes decks.

---

## Environment

The user's local environment, to the extent it's been documented:

- **OS:** Windows 11 Home
- **Hardware:** RTX 3060 laptop, 6GB VRAM
- **Python:** 3.12 (per memory; verify on first run)
- **Java:** Temurin 21.0.10 LTS JRE installed at `vendor/jre/` (2026-04-26). Repo-local, no PATH pollution.
- **Forge:** 2.0.12 installed at `vendor/forge/` (2026-04-26) via unattended IzPack install — see `setup/forge/README.md` for the auto-install XML and the gotchas it took to get there. Userdata is project-local at `vendor/forge/userdata/` (`forge.profile.properties` redirects it). Bundled 505 precon + 167 commander precon decks copied into `userdata/decks/{constructed,commander}/`.
- **Working directory location:** `C:\dev\commander_builder` — moved out of OneDrive on 2026-04-26 to avoid the reparse-point issues that broke Next.js builds in adjacent projects. Pushed to `github.com/LlamaAdam/commander-builder` (public).
- **Anthropic API key:** not yet documented — needed for Phase 2.

The verifier (Phase 1A) starts by surfacing the Forge install state. Don't assume — check.

---

## Verified vs. unverified assumptions

Honest accounting of what's known versus assumed. Update this as items move from one column to the other.

### Verified (from documentation or prior research)

- Forge is open source, actively maintained (Card-Forge/forge on GitHub)
- Forge has a documented headless `sim` mode: `java -jar forge.jar sim -d deck1 deck2 ... -f commander -n 100`
- All decks must be listed after a single `-d` flag (multiple `-d` flags break it — confirmed gotcha from a 2020 forum post)
- Forge `.dck` format is plaintext: `[metadata]`, `[Commander]`, `[Main]` sections with `<qty> <cardname>` lines
- Set codes are optional in `.dck` files
- A Moxfield → Forge converter exists: `andreamanfroi/moxfield-2-forge-parser` (Python, GitHub) — useful as reference but should be verified before depending on
- Moxfield API endpoints are stable: `/v3/decks/all/{id}`, `/v2/users/{username}/decks`, deck search with bracket and `updatedAtFrom` filters
- Forge AI is rule-based heuristics. Decent at aggro, weaker at combo and slow control
- Forge has 99%+ card coverage but very recent sets may have gaps

### Unverified (must be checked or accepted as risk)

- **~~Whether Forge's headless mode runs on Windows without launching JavaFX/GUI components.~~** ✅ Verified 2026-04-26 — runs cleanly, no GUI launched, both 2-player constructed and 4-player commander complete with exit 0.
- **~~Forge's exact log format.~~** ✅ Captured 2026-04-26 in `verify_output/{constructed,commander}_stdout.txt`. Match results print as `Match Result: Ai(N)-<deck>: <wins> ...` and per-game outcomes as `Game Outcome: Ai(N)-<deck> has lost because life total reached 0`.
- **Whether Forge supports deterministic RNG seeds** for reproducible runs. If not, accept higher variance and run more games.
- **~~Forge's userdata directory location.~~** ✅ Pinned via `forge.profile.properties` to `<install>/userdata/`. Forge's documented `-D` flag is broken in 2.0.12 (silently ignored) — userdata redirection is the only reliable path.
- **Card coverage for sets released in the last 60 days.** If test decks rely on these, conversion may fail or substitute incorrectly.
- **~~Whether 4-player Commander headless actually works.~~** ✅ Verified 2026-04-26 — 4-player pod with bundled commander precons runs to completion, all per-player scores reported.
- **Forge AI behavior on Commander-format games.** Heuristics tuned for 60-card constructed may underperform in 100-card singleton.

### Decisions made (with reasoning)

- **Python over Node.js** — better stdlib subprocess management for invoking a Java CLI on Windows; the existing Moxfield→Forge converter is also Python.
- **Standalone verifier first** — see Phase 1A rationale.
- **Forge over XMage** — Forge has a documented and known-working headless `sim` mode. XMage's headless capabilities are less documented.
- **LLM-as-analyst before ML** — generates training data while delivering value; small datasets favor reasoning over learning.
- **SQLite for the knowledge log** — single-file, no server, easy to inspect, easy to dump as CSV when training the Phase 3 model.
- **Three meta opponents** — one gives no diversity signal; five is wall-time prohibitive; three captures matchup variation at a reasonable cost.
- **60-day recency window for opponent selection** — long enough to find decks reflecting current meta, short enough to avoid stale lists. Same window used in the audit prompt.
- **Same-RNG-seed comparison preferred over independent runs** — if Forge supports it, this controls a major variance source between current and post-swap deck tests.
- **Tournament-curated opponent pools, not hand-picked** (2026-04-26) — instead of selecting opponents by reputation or my taste, pull a wide candidate pool from Moxfield/EDHREC at the requested bracket and let head-to-head sims rank them. Top finishers become the canonical pool for that bracket. Reasoning: hand-picking imports my biases, misses meta shifts, and doesn't scale across brackets. Tournament selection is reproducible and self-updating.
- **`[USER] ` filename prefix for the deck under test** (2026-04-26) — the deck the user is iterating on lands in the same flat `decks/commander/` directory as opponents (Forge sim doesn't recurse subfolders). The `[USER] ` prefix plus `[B<n>]` bracket suffix lets the orchestrator distinguish the candidate from the opponent pool by filename alone, with no separate manifest.
- **Drop bundled precons from the opponent pool** (2026-04-26) — Forge's 167 bundled commander precons are essentially all bracket-2. Useful as a smoke test, useless as opponents for B3+ user decks. Retired to `_retired_precons/` so they're still on disk but invisible to sim.
- **Bracket-locked sims, no cross-bracket** (2026-04-26) — user decks are built for specific brackets; pitting a B3 deck against B5 opponents (or vice versa) produces noise, not signal. The `--bracket` flag on `run_match.py` is mandatory, and the curated pool only contains decks whose Moxfield-confirmed bracket matches.

---

## Code structure (proposed)

This is a starting point, not gospel. If a better structure emerges during implementation, change it and update this doc.

```
commander_builder/
├── PROJECT.md                  # This file
├── README.md                   # User-facing run instructions
├── pyproject.toml              # Or requirements.txt — pick one based on preference
├── config.example.json         # Template for user config (Forge path, Moxfield username, API key, etc.)
├── .gitignore
├── src/
│   └── commander_builder/
│       ├── __init__.py
│       ├── verify_forge.py     # Phase 1A
│       ├── moxfield_client.py  # Phase 1B
│       ├── forge_converter.py  # Phase 1B
│       ├── forge_runner.py     # Phase 1B
│       ├── log_parser.py       # Phase 1B (driven by what verifier reveals)
│       ├── forge_orchestrator.py  # Phase 1B
│       ├── report.py           # Phase 1B
│       ├── analyst.py          # Phase 2
│       ├── proposer.py         # Phase 2
│       ├── knowledge_log.py    # Phase 2 (SQLite)
│       ├── iteration_loop.py   # Phase 2
│       └── ml/                 # Phase 3 — kept empty until needed
│           ├── __init__.py
│           ├── features.py
│           ├── train.py
│           └── predict.py
├── tests/
│   ├── test_converter.py
│   ├── test_parser.py
│   ├── test_analyst.py
│   └── fixtures/
│       └── sample_forge_log.txt
└── verify_output/              # Generated by Phase 1A; gitignored
```

---

## Working principles for any session

These are how the user wants Claude Code to operate on this project. Follow them.

1. **Verify before assuming.** If you're not sure how Forge does X, write a small test or read the source rather than guessing. Wrong assumptions wrapped in try/except blocks rot quietly.

2. **Honest pushback over compliant building.** If something in the spec doesn't make sense, say so. The user explicitly wants this kind of feedback.

3. **Small, validated steps.** Don't write 500 lines as the first deliverable. Phase 1A is intentionally tiny. Phase 1B should be built component by component, each verified in isolation before integration.

4. **Modularity over cleverness.** Phase 3 will swap part of Phase 2 for a learned model. Phase 4 may swap Forge's AI for Claude. Clean interfaces > clever inheritance.

5. **Document drift.** When something in this PROJECT.md becomes wrong, update the document in the same commit. Don't let the doc drift from reality.

6. **No silent failures.** Forge can fail in many ways (missing cards, AI hangs, JavaFX issues). Surface failures loudly with actionable error messages, not generic exceptions.

7. **Minimum viable first.** Better a slow, ugly pipeline that runs end-to-end than a beautiful component that hasn't been integrated.

8. **Log everything that could become training data.** Phase 3 wants structured, complete logs from day one. Don't lose data we'd want later.

---

## Open questions / TODO

These need resolution at some point. When one is answered, move it to "Decisions made" above and remove it from here.

- Does Forge support deterministic RNG seeds via CLI flag or config?
- What is Forge's exact log output schema? (Phase 1A will surface this)
- Should the tool support non-Commander formats eventually, or stay Commander-only?
- How should the tool handle cards Forge doesn't recognize? (Substitute? Abort? Warn and skip?)
- Should opponent decks be cached per-bracket-per-month, or re-fetched every run?
- For Phase 2: how many iterations is the right default? (Currently 5 — revisit after first runs.)
- For Phase 2: should the analyst be allowed to *propose* swaps, or only *evaluate* them? (Currently split: proposer proposes, analyst evaluates. Could merge.)
- For Phase 3: what's the smallest dataset that's worth training on?
- **Tournament curation algorithm specifics** — candidate pool size per bracket (12? 16?), pod scheduling (round-robin? Swiss? random pods?), ranking criterion (raw win count, aggregate win rate across pods, or Elo-like rating), and how often to refresh (every run? weekly? on user request?). See "Testing strategy" below for the current proposal.

---

## Testing strategy — how we actually evaluate a deck

This is the concrete plan for what `run_match.py` and `pool_curator.py` do under the hood. It exists because "run some sims and see who wins" hides a lot of decisions that bias the answer.

### Layer 1 — Pool curation (run rarely, cached per bracket)

Goal: produce a canonical 3-deck opponent pool for each bracket the user cares about, so subsequent matchups against those opponents are comparable across user-deck iterations.

1. **Candidate harvest.** Pull ~12 decks per bracket from Moxfield (recent + `bracket=N`, strictly verified client-side). Optionally enrich with 2-3 EDHREC tier-preset URLs that resolve to Moxfield. Filter so no two candidates share a commander.
2. **Round-robin qualifier.** Schedule the 12 candidates into 4-player pods such that each deck plays in ~3 pods (12 decks × 3 pods ÷ 4 seats = 9 pods). Each pod runs N=3 games.
3. **Rank by aggregate win rate** across all games, not pod wins — pod variance with 3 games is too noisy to rank on. Total games per candidate: ~9.
4. **Top 3 advance** as the canonical pool for that bracket. Persist the pool (deck filenames + win-rate metadata) to `vendor/forge/userdata/decks/commander/_pools/B<n>.json`.
5. **Refresh trigger.** Re-run curation when the user asks (`run_match.py --recurate --bracket N`) or when the cached pool is older than 30 days. Not every run — curation costs ~30-60 minutes per bracket and the pool shouldn't drift between user-deck iterations.

Wall-time budget: 9 pods × 3 games × ~80s/game ≈ 35 min for B3, ~55 min for B5 (cEDH games are slower). One-time cost per bracket per refresh.

### Layer 2 — User-deck matchup (run frequently, the main loop)

Goal: measure how well the user's deck performs against the canonical bracket pool, with enough games that win-rate has signal.

1. **Compose the pod.** User deck + 3 opponents from the cached pool for that bracket. Seat order randomized per game (Forge handles seating).
2. **Run N=10 games.** Single Forge invocation, all 10 games in one sim call (avoids JVM startup × 10).
3. **Parse `Match Result:` lines** from stdout. Ignore `Game Outcome:` (buggy 4-player attribution).
4. **Report.** Win rate, average game length, count of `unsupported card` warnings, count of `confirmAction` AI-struggle markers per deck. The last two flag whether Forge actually piloted the deck competently — high counts on the user deck mean the win rate is suspect.

Wall-time budget: 10 games × ~80s ≈ 13 min for B3, ~20 min for B5. Acceptable for an iteration loop the user runs interactively.

### Layer 3 — Pre/post swap comparison (Phase 2)

Goal: tell whether a proposed swap actually moved the win rate.

1. **Run Layer 2 twice** — once with the current deck, once with the post-swap deck — against the *same* cached pool.
2. **Compare aggregate win rates** with a simple binomial test (10 vs 10 is too few; bump to 20-30 games per matchup when stakes are higher).
3. **Analyst reads the deltas** plus the swap diff and writes a verdict + lessons to the knowledge log.

Why same pool, not same RNG seed: Forge 2.0.12 has no documented seed flag. The next-best variance control is fixing the opponents and running enough games that hand-of-cards variance averages out.

### Curator rules (concrete, baked into `pool_curator.py`)

These are the rules the curator enforces. Each addresses a specific failure mode.

- **AI-pilotability gate.** Reject any candidate whose qualifier games average >50 `confirmAction` log lines per game. Forge can't pilot it, so its win rate is meaningless as a benchmark. Same metric on the user deck flags the final report's confidence.
- **Two-pool rotation.** Curate top-6, not top-3. User matchup runs against two 3-deck slices (Pool A: ranks 1/3/5, Pool B: ranks 2/4/6), 10 games each. Result: 20 games across 6 distinct opponents at the same wall-time per game.
- **Archetype diversity.** One-shot Claude classification of each candidate's archetype (aggro / midrange / control / combo / stax) from its decklist, persisted in the pool JSON. The final 6 must span at least 3 archetypes, and no two pool-mates in the same 3-deck slice share an archetype.
- **Color-identity overlap cap.** Hard rule on top of archetype: no two opponents in the same slice share ≥3 colors of identity. Cheap structural diversity even if archetype tagging is wrong.
- **Strict bracket source.** For curation only, use `deck_json["bracket"]` (Moxfield-confirmed). Drop `userBracket` / `autoBracket` fallbacks. User-deck imports keep the looser fallback chain because the user knows their deck's intended bracket.
- **Bracket-inflation flag.** Any curated B<n> candidate that wins >75% across the qualifier gets a `suspected_inflated` tag in the pool JSON. Surface in the report so the user can manually demote and re-curate.

### Other issues (and resolutions)

Beyond the four mitigations above, several risks deserve explicit handling:

- **Seating-order bias.** In a 4-player pod, going first is a meaningful edge. With 10 games and Forge's random seating, the user deck might land in seat 1 disproportionately. **Resolution:** rotate seats deterministically — run 10 games as 4 sets of 2-3 with the user deck cycling through seats 1→2→3→4. Forge's `sim` doesn't expose seat assignment directly, so emulate by listing decks in different orders across batches.
- **Threat-targeting bias.** Forge's political AI gangs up on the perceived strongest threat. A flashy commander (Atraxa, Yuriko) gets archenemied independent of actual power. **Resolution:** flag commanders with high `archenemy_factor` from EDHREC's "salt score" or threat reputation; track per-pod elimination order in the log so the analyst can see whether the user deck's losses correlate with going-out-first.
- **Game-length / turn-limit handling.** Forge has a turn cap; games hitting it count as draws or get attributed to whoever has highest life. This skews toward control / lifegain decks artificially. **Resolution:** parse `Game Result: ... ms` plus turn count; tag games >turn-30 as `inconclusive` and exclude from win rate (report separately as "would-have-drawn" rate).
- **Sample-size honesty.** 10 games × 4 seats = ~2.5 expected wins for the user deck. Win-rate confidence intervals are wide (a 30% sample win rate has a 95% CI of roughly 7-65%). **Resolution:** report Wilson CI alongside point estimate; default Phase 2 swap-comparison games to 30 (not 10) so deltas have signal; require analyst to acknowledge CI overlap before declaring a swap "kept".
- **Card-DB gaps mid-batch.** A candidate with a card Forge doesn't recognize crashes or substitutes mid-sim, polluting the qualifier. **Resolution:** pre-flight every candidate with a 1-game smoke sim and parse `An unsupported card was requested:` count. Reject candidates with >0 unsupported cards before the round-robin; substitutions are silent and we don't trust them as benchmarks.
- **Forge crash mid-batch.** A 10-game sim that crashes on game 7 currently loses everything. **Resolution:** invoke Forge per-game (or in batches of 2-3) instead of all-10-at-once; persist partial results to a per-matchup JSON checkpoint after each game so a crash costs at most one game. Cost is JVM startup × N — measure the overhead before committing to per-game; batches of 5 may be the sweet spot.
- **Iteration-log poisoning (Phase 2).** A swap that won by variance gets verdict=kept and becomes the new baseline; future iterations build on noise. **Resolution:** when CI overlap is high, mark the verdict `provisional`; require a confirmation rerun before the swap is locked in. Cheap insurance against compounding bad calls.
- **Cross-bracket non-comparability.** Win rate is anchored to a specific bracket's curated pool; the same 60% means very different things at B3 vs B5. **Resolution:** report headline win rate with bracket and pool-version stamp (e.g. "62% vs B3 pool v2026-04"); never compute cross-bracket aggregates; if the user wants to test a flexible deck across brackets, run separate matchups and report side-by-side, not summed.

These are tracked as concrete deliverables for `pool_curator.py` and `forge_runner.py`, not aspirational future work.

### What this strategy does NOT do (yet)

- No matchup-specific reporting ("you beat deck X 80%, deck Y 20%"). The aggregate win rate is the headline. Per-opponent breakdown is in the structured log for the analyst to read but not in the user-facing summary.
- No Elo or rating system. Aggregate win rate is sufficient with a fixed pool. Elo would matter if we were ranking many user decks against each other, which is out of scope.
- No statistical significance gating before the analyst makes a call. The analyst sees raw deltas and can downgrade its confidence when sample sizes are small. Adding a hard p-value gate is a Phase 2 polish item.

---

## Things explicitly out of scope

To keep the project from sprawling:

- **No GUI.** CLI tool only.
- **No real-time multiplayer.** Batch testing only.
- **No deck-building from scratch.** Iterates on an input deck; doesn't invent one.
- **No formats other than Commander** in Phase 1–2. Could revisit later.
- **No competitive matchmaking or ladder.** Just iterative improvement.
- **No automatic deck import from sources other than Moxfield + EDHREC.** EDHREC is in scope as a *URL discoverer* (its "Average Deck" and tier-preset links resolve to Moxfield URLs that go through the existing `moxfield_import` fetch path). MTGGoldfish, Archidekt, etc. remain out of scope unless a concrete reason emerges.
- **No replacement for the Moxfield audit prompt.** The audit prompt is the front-end "should I make these swaps" tool; Commander Builder is the back-end "did the swaps actually help and what should we try next" engine. Both can be used independently.
