# FP-001 unblock — Path A (mature forge_py) vs Path B (fork Forge)

**Date:** 2026-05-27 · **Status:** decision memo (no build committed) ·
**Companion to:** [fp001-llm-pilot-spike.md](fp001-llm-pilot-spike.md)

> The 2026-05-22 spike concluded FP-001's LLM-pilot is sound in principle but
> has **no host engine today**: Forge 2.0.12 is an unpilotable compiled JAR,
> and `forge_py` (the natural Python seam) is absent + immature. All experiment
> scaffolding is built (LLM client, paired harness, correlation log, **and now
> the Pearson r>=0.90 gate** -- the one gap the spike flagged is closed). So the
> remaining question is narrow: **how do we get a pilotable engine?** Two paths.

## Verified state (2026-05-27)
- `C:\dev\forge_py` still **absent** on this box; `import forge_py` fails.
- No Forge `.java` source anywhere under `C:\dev` (0 files) -- no local fork material.
- `forge_py_correlation.pearson_r` / `pearson_n` present -- scaffolding complete.

---

## Path A -- mature `forge_py`
Bring `forge_py` into the workspace, advance it to turn-by-turn (P3) + real
combat (P5), prove it correlates with Forge (r>=0.90), then add an `LLMAgent`.

| Axis | Assessment |
|------|-----------|
| **Upfront effort** | **Large + uncertain.** Reaching full-game fidelity in a hand-rolled MTG engine is deep (comprehensive rules + thousands of card interactions). Cannot size precisely without the repo in hand. |
| **Time to a usable answer** | **Long.** Gated on the engine reaching P3+P5 *and then* clearing its own correlation milestone -- both open-ended. |
| **Key risk** | **High, and compounding.** (1) Can it reach sufficient fidelity without becoming the 6-12mo full port FP-001 parks? (2) Even at full games, *will it correlate with Forge*? Correlation is unproven; a simplified engine may never clear r>=0.90. |
| **Maintenance** | You own 100% of the engine forever -- but it's your code, no upstream drift. |
| **Signal trustworthiness** | **Low until correlation is demonstrated.** An LLM piloting a simplified engine proves it's good at *that engine*, not at Forge/Magic. |
| **Strategic value** | **High independent of FP-001.** A Python-native engine is the stated north star ("fold forge_py into commander_builder"): fast pre-filter, full introspection. The work isn't wasted if FP-001 specifically stalls. |

## Path B -- fork Forge upstream
Forge is **open-source** (the Card-Forge/forge project; GPL; a large Maven/JVM
build) -- the spike's "no source" meant *none checked out locally*, not that it
doesn't exist. Clone upstream, add a decision-injection seam (an in-process RPC
or stdin/stdout protocol the AI consults at choose-play / declare-attackers),
rebuild the JAR.

| Axis | Assessment |
|------|-----------|
| **Upfront effort** | **Moderate-to-large but bounded + front-loaded.** The hard part is comprehending a large unfamiliar Java codebase + standing up its build, not implementing rules (rules already correct). |
| **Time to a usable answer** | **Shorter + more predictable** than A -- no engine to grow, no correlation milestone to clear first. |
| **Key risk** | **Medium.** (1) Forge's AI may be deeply coupled / hard to intercept cleanly; (2) build/dependency friction; (3) GPL: a *redistributed* modified JAR carries source-sharing obligations (fine for internal/experimental use). **Correlation risk is zero** -- a forked Forge *is* Forge for rules. |
| **Maintenance** | Carry a fork. But you can **freeze** it on the version you correlate against (2.0.12) -- no need to track upstream for an experiment. |
| **Signal trustworthiness** | **High -- the decisive advantage.** Results are directly meaningful for the engine you actually sim with. No proxy gap. |
| **Strategic value** | Lower -- it's experiment infrastructure, not the forge_py north star. May have a short product life. |
| **Shrink-it option** | The scoping spike should first check whether a newer Forge exposes an existing AI-scripting / simulation hook upstream -- if so, B may not need a maintained source fork at all. |

---

## The decisive framing

The two paths answer **different questions**:

- **Path B answers "is LLM-piloting even worth it?" cheaply and trustworthily.**
  Freeze a Forge fork, wire the seam, run the >=30-game spike, get a real
  win-rate delta against the engine we actually use. Low risk of a meaningless
  result.
- **Path A is a product bet on the long-term north star** (a native engine).
  FP-001 is the *wrong forcing function* for it: if you want forge_py, mature it
  on its own merits and the LLM-pilot falls out for free at P3+P5. Driving that
  engine work *through* FP-001 inverts cost (expensive) and value (uncertain).

**Recommendation: sequence them -- B de-risks A.**
Run Path B as the experiment to learn whether the LLM-pilot delta is real and
worth having. Only invest in Path A's engine work if B shows the delta is large
enough to want natively. Don't pay for the expensive engine bet before the cheap
experiment has answered the underlying question.

**Concrete next step either way:** a time-boxed Path-B scoping spike (confirm
upstream repo + license + build; locate the AI decision entry points; identify
where a seam goes; check for an existing AI/sim hook; produce a real effort
number). Memo only, no commitment -- mirrors how the original A3 spike was run.
