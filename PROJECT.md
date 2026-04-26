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

### Phase 1A — Forge verifier (current target)

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

**Deliverable:** End-to-end Forge pipeline. Input: two Moxfield deck IDs (current + post-swap). Output: comparison report.

**Components:**
- `moxfield_client.py` — pull a deck and find opponent meta decks via Moxfield API
- `forge_converter.py` — convert Moxfield card list to Forge `.dck` format
- `forge_runner.py` — invoke Forge headless, capture output
- `log_parser.py` — extract win/loss, game length, and key events from Forge logs
- `forge_orchestrator.py` — main pipeline tying it all together
- `report.py` — generate the comparison report

**Opponent selection logic:**
- Three opponents at the same bracket as the test deck
- Sorted by likes, filtered to decks updated in the past 60 days
- Different commanders from each other and from the test deck
- Cached per-bracket so re-runs use the same field

**Run shape:**
- Default: 100 games per matchup × 3 opponents × 2 deck versions = 6 matchups, ~600 games total
- Reduced for iteration: 30 games per matchup
- Use the same RNG seed across the current/post-swap pair if Forge supports it (TBD — verify in Phase 1A)

### Phase 2 — LLM analyst + iteration loop (the core of "Commander Builder")

**Deliverable:** Closed-loop deck improvement. Input: a Commander deck. Output: an iterated deck plus a log of every change tried and how it performed.

**Components added on top of Phase 1B:**
- `analyst.py` — Claude API wrapper. Takes (current deck, swap proposal, sim results before, sim results after) and returns a structured judgment: `{verdict: "kept" | "reverted" | "neutral", reasoning: "...", lessons: [...]}`
- `proposer.py` — Claude API wrapper. Takes (current deck, accumulated lessons, target bracket) and returns a swap proposal: `{add: [...], remove: [...], hypothesis: "..."}`
- `knowledge_log.py` — SQLite store of every iteration: deck snapshot, swap, deltas, analyst note, verdict
- `iteration_loop.py` — orchestrates: propose → simulate → analyze → commit-or-revert → loop

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
- **Java:** NOT installed as of 2026-04-26. Verifier surfaced this — `where java` finds nothing, no install in standard locations. Recommend Temurin 21 LTS JRE; current Forge releases need JRE 17+.
- **Forge:** NOT installed as of 2026-04-26. Verifier surfaced this — none of `%PROGRAMFILES%\Forge`, `%LOCALAPPDATA%\Forge`, etc. exist. Get from `Card-Forge/forge` GitHub releases.
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

- **Whether Forge's headless mode runs on Windows without launching JavaFX/GUI components.** Some past Forge builds had issues. Phase 1A's first job is to confirm.
- **Forge's exact log format.** The wiki says "results are printed at the end" but doesn't show the schema. Cannot write a parser without seeing real output.
- **Whether Forge supports deterministic RNG seeds** for reproducible runs. If not, accept higher variance and run more games.
- **Forge's userdata directory location on the user's specific Windows install.** Could be `%APPDATA%\Forge`, could be alongside the install. The verifier checks multiple candidates.
- **Card coverage for sets released in the last 60 days.** If test decks rely on these, conversion may fail or substitute incorrectly.
- **Whether 4-player Commander headless actually works.** Documented to work, but not verified on this machine.
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

---

## Things explicitly out of scope

To keep the project from sprawling:

- **No GUI.** CLI tool only.
- **No real-time multiplayer.** Batch testing only.
- **No deck-building from scratch.** Iterates on an input deck; doesn't invent one.
- **No formats other than Commander** in Phase 1–2. Could revisit later.
- **No competitive matchmaking or ladder.** Just iterative improvement.
- **No automatic deck import from sources other than Moxfield.** EDHREC, MTGGoldfish, etc. could be added later if there's a reason.
- **No replacement for the Moxfield audit prompt.** The audit prompt is the front-end "should I make these swaps" tool; Commander Builder is the back-end "did the swaps actually help and what should we try next" engine. Both can be used independently.
