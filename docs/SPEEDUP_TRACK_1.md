# Track 1 — A/B sim speedup (no correctness loss)

> Written 2026-04-29. The propose-swap pod sim takes ~3-7 min today.
> This doc plans how to drop that to ~1-2 min without changing the
> Forge engine that produces the verdict. Track 2 (forge_py multi-deck
> simulation) is a separate, longer build documented elsewhere.

---

## Real bottleneck (measured)

`commander_builder.compare_versions.compare()` runs Forge sims for an
A/B comparison. The default pod-mode call shape is:

```
for pair in filler_pairs:                  # default 2 pairs
    runner.run(pod, num_games=5)           # ~30-60s/game × 5 = ~3 min
                                           # JVM startup ~10s per call
                                           # SEQUENTIAL — total ~7 min
```

One pod is ~3 min wall-time. Two pods = ~7 min. The pods are independent
(different filler decks, different RNG seeds), so the sequential `for`
loop is the dominant inefficiency.

**Forge already amortizes JVM startup across games via `-n <N>`.** The
JVM-persistence win we initially scoped is small (~10s per pod call,
not per game). Parallelism + early-stop are bigger wins.

## Sprint 1A — Parallel pod execution

**Goal.** Run all filler-pair pods concurrently. 2 pods → 2 cores → 2×
wall-clock speedup. 4 pods → 4 cores → 4×.

**Implementation.**
1. Replace the `for pod in pods:` loop in `compare()` with a
   `ProcessPoolExecutor` (workers = `min(len(pods), os.cpu_count())`).
2. Each worker: builds its own `ForgeRunner`, calls `runner.run(...)`,
   returns the parsed pod summary dict.
3. Aggregate results in deterministic original order so the report's
   `pods` list still matches `filler_pairs_used`.
4. Each worker writes its Forge stdout to a unique tempfile so pods
   don't interleave logs in the parent process. Print "Pod i/N done in
   Xs" lines to the parent on completion.

**Risks.**
- Forge writes to `userdata/` — check no shared mutable state between
  parallel runs. (Spot-check: each pod is read-only on the .dck files;
  Forge does NOT write to its install dir during sim.)
- Memory: each JVM is ~600MB. 4 pods on a 16GB box = 2.4GB; fine.
- File locking on the tempfile / log paths — use `mkstemp()` per pod.

**Test plan.**
- Add `test_compare_runs_pods_in_parallel`: stub `runner.run` to sleep N
  seconds then return a canned result; assert wall-time of two pods is
  closer to `N` than to `2N`.
- Add `test_compare_aggregates_parallel_pod_results_in_order`: stub two
  pods returning different stdout fingerprints; assert
  `report.pods[0]` corresponds to `pairs[0]`.
- Existing `test_compare_*` tests must still pass (sequential
  fallback when `max_workers=1`).

**Ship criteria.**
- Sequential fallback flag (`parallel=False`) preserves old behavior
  for tests / debug.
- Pod-mode 5-game sim wall-clock improves ≥1.7× on a 4-core box
  (verified by hand against a known deck pair).

**Effort.** 1 day.

---

## Sprint 1B — Adaptive early-stop

**Goal.** Stop running additional pods once the result is statistically
conclusive. If pod 1 has `old_wins=0, new_wins=5, draws=0`, running
pod 2 is unlikely to flip the verdict.

**Implementation.**
1. After each pod completes, recompute cumulative `(old_wins, new_wins,
   draws)` and the running margin.
2. Apply a simple two-proportion z-test (or even a binomial-margin
   threshold) to decide whether to continue:
   - **Decisive:** `|margin| >= ceil(remaining_pods * games_per_pod / 2)`.
     If even ALL remaining games swung the wrong way, the verdict
     wouldn't flip → stop.
   - **Confident:** Wilson-score 95% CI on win-rate excludes 0.5 →
     stop.
   - Otherwise → run next pod.
3. Mark `report.stopped_early = True` and record `pods_completed`.

**Risks.**
- Early-stop on small samples is noisy. Default to N=3 pods and only
  stop after pod 2 if margin is decisive; never stop after pod 1.
- Users may want full data — add `--no-early-stop` flag.

**Test plan.**
- `test_early_stop_when_pod1_decisive`: stub pod 1 returning 5-0;
  assert pod 2 is not invoked.
- `test_no_early_stop_when_pod1_close`: stub pod 1 returning 3-2;
  assert all configured pods run.
- `test_early_stop_disabled_runs_all_pods`.

**Ship criteria.**
- Default ON for pod mode, OFF for 1v1 (single pod can't early-stop).
- Composes with 1A: parallel pods all launch, but if pod 1 finishes
  decisive while pod 2 is mid-flight, pod 2 is canceled.

**Effort.** 1 day.

---

## Sprint 1C — Per-pod adaptive game-stop (REFRAMED, SHIPPED 2026-04-29)

**Original spec was JVM persistence** — keep one Forge JVM alive
across A/B calls. Honest reassessment: that requires Forge source
modifications (its `sim` mode runs once and exits — no batch protocol),
costs 2-3 days, and only saves ~10s per pod (5-10% wall-time after
1A+1B). Low leverage.

**Reframed Sprint 1C: per-pod adaptive game-stop.** Forge already
emits ``Game Result: Game N ended... Ai(X)-Name has won!`` per game.
By streaming stdout and parsing those incrementally, we can kill the
JVM as soon as the in-pod margin exceeds the games left in this pod.
20-50% per-pod savings on lopsided matches. No Forge modifications.
Composes with 1A (parallel pods) and 1B (skip whole pods after
decisive ones).

**What shipped.**

1. ``forge_runner._run_streaming`` gained an ``abort_check(line) ->
   bool`` parameter. Per-line callback; returns True → kills JVM.
   ``ForgeRunner.run`` forwards it. (Existing callers unaffected.)
2. ``compare_versions._make_pod_abort_check(pod, old_deck, new_deck,
   games_per_pod)`` builds a closure that:
   - Parses each ``Game Result: Game N ended... Ai(X)-Name has won!``
   - Tracks per-deck wins (state dict).
   - Returns True when ``|new_wins - old_wins| > games_remaining``.
3. ``compare_versions._synthesize_match_result(state)`` builds the
   ``Match Result:`` summary line that Forge would normally print at
   pod end. Used to feed log_parser when we kill before Forge gets
   there.
4. ``_run_one_pod`` opt-in flag ``intra_pod_abort=True`` (default on)
   wires the abort_check + synth-line. Pod result dict gains
   ``intra_pod_aborted: bool`` and ``games_actually_played: int``.
5. 7 new tests cover the abort-fires path, the don't-fire-when-close
   path, filler-deck wins not skewing the margin, the synth Match
   Result being parser-compatible, full-stack integration with the
   compare flow.

**Test count.** compare_versions tests: 33 → 40. Full suite: 593 → 601.
All green.

**Stacked savings (theoretical, on a 4-core box, decisive matchup):**

| Mode | Sequential | After 1A only | After 1A+1B+1C |
|---|---|---|---|
| 1v1 5g (decisive) | 30s | 30s | ~18s (kill at game 3) |
| Pod 5g (2 pairs, decisive) | ~7 min | ~3.5 min | ~2 min |
| Pod 20g (2 pairs, decisive) | ~28 min | ~14 min | ~5 min |
| Pod 5g (2 pairs, close) | ~7 min | ~3.5 min | ~3.5 min (no abort fires) |

Close matches don't speed up — the abort only fires when the verdict
is uncatchable. That's the design: don't trade correctness for time.

---

## Sprint 1D — Result cache (small, opportunistic)

**Goal.** Hash `(old_deck_text, new_deck_text, bracket, mode, games,
filler_pairs)` → stdout. Repeat sims read from cache.

**When this helps.** Dev reruns, CI, smoke tests. Not for the user's
typical "one swap → one sim" flow.

**Implementation.** Wrap `runner.run()` with a check against a
SHA256-keyed cache directory. Default OFF; flag-gated.

**Effort.** 0.5 day. Build alongside 1A if convenient.

---

## Order & verification

1. Land 1A (parallel pods). Measure wall-time on a known pair before
   and after; record in this doc.
2. Land 1B (adaptive early-stop). Measure mean wall-time across 5 swap
   tests with varying margin sizes.
3. Update the propose-swap UI ETA copy (`apps.js` `podSecs` table) to
   reflect the new wall-times.
4. Decide: ship 1C/1D, or pivot to Track 2 (forge_py multi-deck sim).

---

## Out of scope for Track 1

- forge_py replacing Forge (Track 2; multi-month).
- Reducing per-game Forge time (e.g. faster AI). Forge's AI is what we
  asked for; making it faster means making it dumber.
- GPU acceleration (Forge is CPU-bound, no path to GPU without rewrite).
- Distributed multi-machine sim (not warranted for personal use).
