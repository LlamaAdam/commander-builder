# FP-001 Path-B experiment -- implementation handoff

**Date:** 2026-05-27 · **Status:** ready to start · **Owner:** TBD
**Companions:** [fp001-llm-pilot-spike.md](fp001-llm-pilot-spike.md) (why) ·
[fp001-path-comparison.md](fp001-path-comparison.md) (A-vs-B + scoping result)

> Build a **frozen fork** of Forge 2.0.12 with one seat piloted by an LLM, run
> paired games vs the stock AI, and report the **win-rate delta + cost/game**.
> This answers the only open FP-001 question: *does LLM-piloting Forge's AI
> actually move win rate enough to be worth it?* Do NOT start Path A
> (native-engine) work unless this delta proves worthwhile.

---

## 0. Success criterion (what "done" means)

- **Primary metric:** win-rate delta of the LLM-piloted seat vs the stock-AI
  seat over **>=30 paired games**, **seat-balanced** (alternate which seat is
  LLM-piloted so the delta isn't a play/draw artifact), one fixed archetype
  pair + bracket to hold deck variance constant.
- **Report alongside:** tokens + $/game, mean decision latency, and a
  significance read on the delta (binomial / margin CI).
- **GO for FP-001** = the delta is positive, significant, and large enough to
  justify the per-game token cost (FP-001 budgeted $0.10-$1.00/game). Then -- and
  only then -- consider Path A to get it natively/cheaply.
- **Note on the r>=0.90 gate:** that gate was the *forge_py-vs-Forge correlation*
  test. Path B runs **inside** Forge, so there is no second engine to correlate
  -- correlation risk is zero and the gate does not apply. The metric here is the
  win-rate delta, not r.

## 1. Prerequisites

- **JDK 17** (Forge runtime is "Java 17 or later"; build with JDK 17 -- confirm
  against the fork's root `pom.xml` `<maven.compiler.*>` before building).
- **Maven 3.8+**.
- Disk: a full Forge source tree + build (~GB-scale with deps).
- Our existing Python env (for the sidecar; reuses `analyst.py`).

## 2. Repo setup -- frozen fork (do not track upstream)

```bash
# clone upstream and pin to the version we vendor + sim against
git clone https://github.com/Card-Forge/forge.git C:/dev/forge-llm
cd C:/dev/forge-llm
git checkout tags/forge-2.0.12 -b llm-pilot   # confirm exact tag name: `git tag | findstr 2.0.12`
# baseline build (produces the same fat jar forge_runner already runs):
mvn -U -B clean -P windows-linux install
# fat jar lands at: forge-gui-desktop/target/forge-gui-desktop-2.0.12-jar-with-dependencies.jar
```

GPL: this fork is **internal/experimental** -- we do not redistribute the
modified JAR, so the source-sharing obligation never triggers. Keep it frozen on
2.0.12; do not rebase on upstream.

## 3. The seam (recap -- detail in the comparison memo)

`forge.game.player.PlayerController` is abstract with ~60+ decision methods;
`forge.ai.PlayerControllerAi` is the stock AI subclass. We add a third subclass
and inject it on one seat in headless `sim` mode.

## 4. Phased plan (each phase has a hard acceptance check -- stop if it fails)

**M0 -- reproducible baseline build (0.5-1.5 d).**
Build from source; run the *unmodified* fork's headless sim and confirm it plays
an AI-vs-AI game and emits the same `Phase:/Turn:/Life:` stdout `forge_runner`
parses. *Accept:* `forge_runner.run_ab_simulation` works against the
source-built jar exactly as against the vendored one.

**M1 -- pass-through controller (0.5-1 d).**
Add `PlayerControllerLLM extends PlayerControllerAi` that overrides **nothing**
(pure delegation) and patch the one sim-setup spot to assign it to seat 0 when
`FORGE_LLM_SEAT` env/flag is set. *Accept:* with the flag on, games are valid
and results are statistically identical to stock (proves injection is correct
and side-effect-free). With the flag off, default sim is unchanged.

**M2 -- one real decision via Ollama (1-1.5 d).**
Override **`declareAttackers(Player, Combat)`** only. Serialize a minimal game
state (the LLM seat's hand + board + life totals + the legal attacker set) to
JSON, POST to the Python sidecar, parse the chosen attacker subset, apply it;
fall back to `super.declareAttackers(...)` on any error/timeout. Use the **free
local Ollama** model first. *Accept:* games stay valid over 10+ runs; the LLM's
attacker choices appear in logs; zero crashes (fallback covers malformed
replies).

**M3 -- add spell choice + first delta (1-2 d).**
Also override the which-spell-to-play decision (`getAbilityToPlay` /
`chooseSpellAbilityToPlay` -- pick whichever is the per-turn main-phase entry;
verify it isn't `final`). Run **>=30 paired, seat-balanced games** on Ollama.
Emit one row per pair into the correlation log (repurpose
`forge_py_correlation.log_correlation_row`: `forge_*` -> `stock_*`, `py_*` ->
`llm_*`). *Accept:* a computed win-rate delta + the run is reproducible.

**M4 -- Claude confirmation + verdict (0.5-1 d + machine).**
Re-run the >=30-game batch with Claude (subscription-routed; see invariants),
record delta + tokens + $/game + latency. Write the verdict into
[fp001-path-comparison.md](fp001-path-comparison.md) (measured delta -> go/no-go
on funding Path A).

## 5. Java changes (two files)

1. **`forge-ai/src/main/java/forge/ai/PlayerControllerLLM.java`** -- the new
   subclass. Extends `PlayerControllerAi`; overrides only the 2 high-leverage
   methods; everything else inherits stock AI so games are always valid. Each
   override: build state JSON -> `LlmBridge.choose(stateJson)` -> parse ->
   validate against legal options -> apply, else `super`.
2. **The controller-assignment spot in sim setup** -- find where sim mode `new
   PlayerControllerAi(...)` per seat (grep `new PlayerControllerAi(` under
   `forge-ai` / the sim/match init path). Gate a swap to `PlayerControllerLLM`
   behind `System.getenv("FORGE_LLM_SEAT")` so default `sim` is untouched.

Keep a tiny `LlmBridge` helper (Java) that does the localhost HTTP POST +
timeout + JSON parse; no LLM logic in Java.

## 6. Python sidecar (reuse, don't reinvent)

A small localhost HTTP server (`scripts/llm_pilot_sidecar.py`) that:
- accepts `{state, legal_options, decision_type}`,
- calls the **existing `analyst.py`** client (so model routing, JSON parse, and
  graceful fallback are inherited), and
- returns `{choice}` constrained to `legal_options`.

Start it before a sim run; `PlayerControllerLLM` points at its port. Ollama
path is free (shake out the harness); flip to Claude for the M4 batch.

## 7. Measurement / harness

- Reuse `forge_runner.run_ab_simulation` / `run_ab_batch` for paired,
  seat-attributed games (already seat-balances).
- Log into `forge_py_correlation` (schema already pairs two players' wins).
- Compute the delta + a significance read; `pearson_r` helper is present if you
  want a per-archetype correlation view across decks.

## 8. Risks & open checks (verify early)

- **JDK exact version** -- confirm from the fork's `pom.xml` before M0.
- **Methods not `final`** -- confirm `declareAttackers` + the spell-choice
  method are overridable on `PlayerControllerAi` (M1/M3).
- **Build friction** -- first green Maven build is the top schedule risk; M0
  exists to retire it before any LLM work.
- **Game-state extraction completeness** -- the prompt must carry enough legal
  context for a valid choice; start minimal, expand only if the LLM picks
  illegal options (the validate-or-`super` guard makes this safe).
- **Latency/cost** -- only 2 decision points are piloted, by design, to bound
  tokens; watch $/game against the budget.

## 9. Invariants (non-negotiable)

- **Subscription:** the Claude path must use our subscription-safe routing.
  Reuse `analyst.py`; never inherit `ANTHROPIC_API_KEY` / set
  `ANTHROPIC_*` / `CLAUDE_CODE_USE_BEDROCK/VERTEX` when it shells the `claude`
  CLI.
- **ASCII-only** console output (cp1252 console).
- **Machine identity:** box1 = `Llama` -> `--label Llama`; never a `box2b` label
  on box1.
- This fork is a **separate repo** (`C:/dev/forge-llm`); it does NOT touch
  `commander-builder` working trees. The only commander-builder additions are
  the Python sidecar + harness glue, landed via the normal Channel-B worktree
  flow.

## 10. Definition of done

A committed verdict in `fp001-path-comparison.md`: measured win-rate delta over
>=30 seat-balanced paired games (Ollama + a Claude batch), with tokens/$/latency,
and a go/no-go recommendation on funding Path A. Total budget ~1-1.5 weeks.
