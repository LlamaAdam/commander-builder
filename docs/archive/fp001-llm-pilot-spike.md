# FP-001 LLM-Piloted Forge AI — Feasibility Spike (go/no-go memo)

**Date:** 2026-05-22 · **Author:** spike A3 (time-boxed) · **Status:**
**NO-GO as scoped; GO redirected + gated** (see Recommendation).

> This is the deliverable for backlog item **A3** (the bounded FP-001
> spike). It is a memo, not production code. The spike asked: *wire
> Claude/Ollama at a few Forge decision points, run ≥30 paired games vs
> the stock-AI baseline, and report whether win-rate signal correlates
> (target r ≥ 0.90).* The honest finding is that the experiment **cannot
> be run against Forge 2.0.12 at all** with the assets we have, for a
> structural reason worth documenting so nobody burns the 2–4 weeks
> discovering it the hard way.

---

## TL;DR

- **You cannot pilot Forge 2.0.12's AI with an LLM.** Forge is a vendored
  **compiled JAR** run as a **fire-and-forget subprocess** (`java -jar …
  sim`). There is **no decision-injection seam** — the only interactive
  hooks are *read a stdout line* (`on_line`) and *kill the process*
  (`abort_check`). You cannot pause the JVM mid-game, supply a decision,
  and resume. There is **no Forge source** (`.java`) in either repo, so
  patching its AI is off the table without forking Forge — which *is* the
  6–12-month full-engine effort that FP-001 already keeps parked.
- **The real LLM-pilot seam is `forge_py`**, the sister Python-native
  engine, where decision points are ordinary Python calls an LLM can
  stand in for. But `forge_py` is **not present on this machine**
  (`C:\dev\forge_py` missing; not importable) and, per its own status, is
  not yet mature enough (turn-by-turn P3 / combat P5 incomplete).
- **Therefore the ≥30-paired-game correlation experiment has no
  pilotable player to run against, in either engine, today.** It is not
  a "we ran it and the signal was weak" no-go; it's a "there is nothing
  to wire the LLM into yet" blocked.
- **Recommendation:** keep FP-001 **parked**, but record a *precise
  unblock condition* and the fact that the experiment harness + LLM
  client + correlation log are already built, so the spike is a 1–2 day
  start the moment `forge_py` can play a full game.

---

## What the spike asked vs. what's structurally possible

The spike's premise — "wire Claude/Ollama at a few Forge decision
points" — assumes a decision-point seam exists. It does not, for the
Forge we ship.

### Forge is a black-box subprocess

`src/commander_builder/forge_runner.py` builds the command line
(`forge_runner.py:400`):

```python
cmd = [
    str(self.java_path), "-jar", str(self.forge_jar),
    "sim", "-f", game_format, "-n", str(num_games), "-d", *deck_filenames,
]
```

That's it: launch Forge in `sim` mode, let it play *N* AI-vs-AI games,
read the result off stdout afterward. The two interactive hooks
(`forge_runner.py:208`, used by the Sprint-1C abort path) are:

- `on_line(line)` — observe each stdout line as it's emitted, and
- `abort_check(line) -> bool` — if it returns True, **terminate** Forge.

Neither can *inject* a decision. Forge reads nothing from stdin during a
game; it emits `Phase:` / `Turn:` / `Life:` lines as **side effects**,
not as a structured "what should I do?" prompt awaiting a reply. Once a
game starts, the JVM owns it until it ends or is killed.

### No Forge source, only the JAR

`vendor/forge/forge-gui-desktop-2.0.12-jar-with-dependencies.jar` is a
compiled artifact. There are **no `.java` files** in `commander-builder`
or `forge_py`. Modifying Forge's AI decision code would mean forking and
maintaining a Forge build — explicitly the heavyweight branch FP-001
parks ("full rules-engine port / source fork: 6–12+ months"). JVM
bytecode instrumentation to intercept decisions is the same brittleness
trap already noted for FP-004 (the seed problem) and is not worth it.

### The seam that *would* work: `forge_py`

The only place an LLM can sit *at a decision point* is an engine whose
decision points are callable from our side — i.e. the Python-native
`forge_py`. There the natural shape is an `Agent`/`Policy` abstraction
(`choose_play`, `declare_attackers`, `declare_blockers`, …) where a stock
heuristic agent and an "LLM agent" are interchangeable, and the harness
plays one against the other. That is the correct, low-friction home for
this idea. But:

- `C:\dev\forge_py` is **missing on this machine** and `import forge_py`
  fails — verified this session.
- Per `STATUS.md` (sister projects), `forge_py` "stays independent until
  its turn-by-turn (P3) and combat (P5) produce signal that correlates
  with Forge." Its combat is still shallow (single-attacker, no
  flying/reach/first-strike), so even if it were here, an LLM agent's
  wins wouldn't yet be trustworthy as a *Forge* proxy.

So both engines are dead ends for the experiment **right now**: Forge
because it's unpilotable, `forge_py` because it isn't here / isn't ready.

---

## What IS already built (so the eventual spike is cheap)

The blocker is the pilotable player, not the scaffolding. When
`forge_py` can play a full game, these are ready to drop in:

| Need | Status | Where |
|------|--------|-------|
| LLM client (Claude + Ollama, JSON I/O, graceful fallback) | ✅ built | `analyst.py:227` `claude_verdict`, `:309` `ollama_verdict` — the exact request/parse/fallback pattern an LLM agent reuses |
| Paired-game harness, **seat-attributed** wins | ✅ built | `forge_runner.run_ab_simulation` (`:546`), `run_ab_batch` (`:713`, concurrent, FP-003) |
| Paired-comparison result log | ✅ built | `forge_py_correlation.log_correlation_row` (`:162`) — schema already pairs `forge_*_wins` vs `py_*_wins`; reusable as `stock_*` vs `llm_*` |
| Agreement summary | ✅ built | `forge_py_correlation.correlation_summary` (`:215`) — returns `agreement_rate` |
| **Pearson r** for the "r ≥ 0.90" gate | ❌ gap | `correlation_summary` only computes agreement rate; the r ≥ 0.90 rule (2026-04-28) needs a small Pearson helper added |

The only net-new code the experiment needs (beyond `forge_py` itself) is
(a) an `LLMAgent` implementing `forge_py`'s policy interface on top of
`analyst.py`, and (b) a Pearson-r helper next to `correlation_summary`.

---

## Experiment design (ready to run when unblocked)

Recorded so the eventual spike starts from a spec, not a blank page:

1. **Engine:** `forge_py` (NOT Forge). Stock heuristic agent = baseline.
2. **Treatment:** `LLMAgent` (Claude `claude-sonnet-4-5` first; Ollama
   `llama3.2:3b` as the cheap local control) at a *few* high-leverage
   decision points only — start with **declare-attackers** and
   **which-spell-to-play-this-turn** (the two that most move win rate),
   not every micro-decision (cost/latency).
3. **Matchup:** one fixed archetype pair, one bracket, to hold deck
   variance constant.
4. **Volume:** ≥30 paired games, alternating seat order (the harness
   already does this), so the LLM-vs-stock delta isn't a seat artifact.
5. **Metric:** per-archetype **Pearson r** between LLM-agent and Forge
   outcomes on the same decks; **go if r ≥ 0.90** (the 2026-04-28
   flip-default rule). Also report raw win-rate delta + tokens/game cost.
6. **Cost guardrail:** FP-001 estimates $0.10–$1.00/game; 30 games ≈
   $3–$30 on Claude. Run Ollama first to shake out the harness for free,
   then a single Claude confirmation batch.
7. **Output:** update this memo with the measured r + a flip/keep
   decision.

---

## Recommendation

**Keep FP-001 parked. Do not start the LLM-pilot build against Forge.**

- The high-leverage variant (LLM at decision points) is sound *in
  principle* but has **no host engine today**: Forge is unpilotable
  without a source fork, and `forge_py` is absent/immature.
- **Precise unblock condition:** promote FP-001's LLM-pilot slice only
  when `forge_py` (a) is present in the workspace and (b) plays a full
  multiplayer game turn-by-turn with combat that already correlates with
  Forge (its own P3+P5 milestone). At that point this spike is a 1–2 day
  job: add an `LLMAgent` over `analyst.py`, add a Pearson helper, run the
  experiment above.
- **Cheap interim step (optional, not required):** add the Pearson-r
  helper beside `correlation_summary` now — it's useful for the existing
  forge_py-correlation work regardless of FP-001, and removes the one
  scaffolding gap.

**Net:** the spike's value was the negative result — it prevents
spending 2–4 weeks trying to pilot an AI that lives inside an opaque JAR.
The idea isn't dead; it's correctly relocated to `forge_py` and gated on
that engine's maturity.
