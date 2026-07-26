# Optimization Audit — 2026-07-25

Fresh-eyes performance audit of `src/commander_builder/`, run as four parallel
deep-dives (scoring hot path, deck-build pipeline, Forge simulation pipeline,
data-loading layer). All findings verified against source with line references;
the highest-impact ones were profiled/measured on this machine. The "IMPLEMENTED"
section at the top landed in this pass with the full test suite green; everything
below it is proposed work, ranked by impact ÷ effort.

Structural observation that explains most of the findings: before this pass
there was **not a single `functools.lru_cache`/`cache` in the entire `src/`
tree**. Every memo was hand-rolled and per-object or per-call — never
per-process — so identical work (file reads, HTTP fetches, regex compiles,
full-deck rescans) was silently repeated across the pipeline.

---

## IMPLEMENTED in this pass (all low-risk, suite green)

| Fix | File | Measured effect |
|---|---|---|
| Process memo (15-min TTL) for `load_game_changers` + folded-set cache for `is_game_changer` | `game_changers.py` | **~310 ms live HTTP per call → 0.5 µs memoized.** The WotC scrape is broken in production (see Bugs below), so the rejected-scrape path never wrote the disk cache and *every* call re-fetched. A deck build makes ~6-7 calls (2 in `deck_builder`, up to 5 via the bracket-steer loop's `estimate_bracket`); web dashboard renders make 2. Saves ~2 s per build, more per web session. |
| Precompiled `_ROLE_PATTERNS_COMPILED` + score short-circuit in `classify_role` | `staples.py` | ~42 trips through `re._compile`'s cache per classified card removed; lower-scoring patterns skipped once a better match exists. ~2-3× on `classify_role` (now ~12 µs/call), which sits under `count_deck_roles`, `role_target_report`, `card_score`, and the advisor. The raw-string `_ROLE_PATTERNS` table is unchanged — `interaction.py` imports it for its wipe patterns. |
| Land early-exit in `oracle_categories` | `interaction.py` | Lands previously matched all ~37 category patterns and then discarded 4 of 5 buckets. Now only `graveyard_hate` patterns run for lands (provably identical output). ~25-30 % off `interaction_report` on a typical 37-land deck; ~1.2 µs/land measured. |
| Single-pass card lookups in `learn_intent` | `intent.py` | The win-con pass re-looked-up every card solely for `type_line`, doubling lookup count (2 × ~99). Halved; on decks with unresolvable names each avoided lookup was a 100 ms sleep + HTTP round-trip. |
| `(path, mtime_ns, size)`-keyed cache for `load_combos` | `combo_detection.py` | Re-parse of `data/combos.json` (~1,500 combos when refreshed) removed from `estimate_bracket` (4×/build), dashboard, and web request paths. Defensive copy returned; `--refresh` picked up via mtime. |
| `lru_cache` on `archetype_curve` core | `card_score.py` | Pure function of `(archetype, bracket)` was rebuilt per scored card (`_f_curve_fit`). Cached as an immutable tuple; public wrapper returns a fresh dict. ~5 % of warm `score_card`. |
| Incremental source tally in `build_manabase` fixer loop | `deck_builder_manabase.py` | `_sources_now()` (full lands rescan, one `lookup` per land) ran once per fixing candidate — 514 `land_color_sources` calls measured for a 5-color build. Now computed once and incremented on accept: O(candidates × lands) → O(lands), ~10× on this stage. |
| Autouse memo-reset fixture | `tests/conftest.py` | Clears the game-changers memo and combos cache between tests so per-test monkeypatching of `_http_get_text`/`CACHE_PATH`/`COMBO_DATA_PATH` can never cross-contaminate. |

New regression tests: memoization + `force_refresh` bypass + defensive-copy
semantics for `load_game_changers` (`tests/test_game_changers.py`), cache
invalidation-on-rewrite + defensive copy for `load_combos`
(`tests/test_combo_detection.py`).

---

## Proposed — Tier 1 (high impact, needs design or new tests first)

### P1. `scryfall_client.lookup_card`: process-level memo + negative caching
`scryfall_client.py:181-203`. No in-memory cache — every call re-`stat`s,
re-reads, and re-parses the snapshot JSON (measured 197 µs/call warm; a dict
memo was 12.4× faster). Worse, **404s are never cached anywhere**: an
unresolvable name costs `time.sleep(0.1)` + a live HTTP round-trip on *every*
call, forever (measured: `compute_deck_health` on the golden fixture = 16.4 s,
nearly all of it repeated misses; 2.95× repeat factor measured across
deck_health's five loops). Three hand-rolled per-object copies of exactly this
memo already exist and could be deleted afterwards: `consistency._make_lookup_cache`,
`card_score.DeckContext.card`, `deck_builder._role_cache`, plus
`deck_legality._Cards`.
**Design constraints:** key on `(resolved cache dir, folded name)` because
tests monkeypatch `CACHE_DIR` per test; `cache=False` callers must bypass;
`refresh_card` must invalidate its entry; bound the memo (~8k entries) for
long-lived processes; add an autouse clear fixture. *Expected: ~12× on warm
scoring/ranking loops; seconds-to-tens-of-seconds on decks with unresolvable
names. Effort S-M. Risk medium (test isolation).*

### P2. `card_score.DeckContext.without()`: inherit deck-level memos (O(n²) cut scoring)
`card_score.py:703-735`. Every cut candidate builds a child context with an
empty `_memo`, so `manabase_report`, `interaction_report`, `curve`,
`one_piece_away`, `combo_pool` (a JSON re-parse!), `effective_lands`,
`salt_scores`, and `game_changers` are re-derived from scratch per deck card —
99× per `cut_candidates` pass. Only `role_report` got the incremental
treatment (`_role_report_minus`). Simulated fix (memo inheritance) measured
**7.8×** on a 99-card cut ordering; expect 8-15× on real oracle text.
Start with the four card-independent slots (`combo_pool`, `salt_scores`,
`game_changers`, `mdfc_lands` — sharing is mechanically identical); make
`curve`/`effective_lands` decrement-on-remove; treat `manabase`/`interaction`
as incremental-subtract with full-recompute fallback, same pattern as
`_role_report_minus`. *Effort M. Risk low → medium by slot.*

### P3. Network call inside the cut-scoring inner loop (also a contract bug)
`card_score.py:992-994` → `staples.count_deck_roles` → module-level
`staples.lookup_card` — bypassing the context's injected offline `lookup`
seam. On a cold Scryfall cache this is one live HTTP request + 100 ms sleep
**per cut candidate** (measured 28.3 s vs 0.28 s for a 99-card pass, ~100×).
Same shape in `_role_target_for` (`card_score.py:1210-1215`): a full
unmemoized 99-name recount per tutor candidate. The module's own docstring
says the whole module runs offline; the suite can't see the violation because
tests pin `staples.lookup_card`. **Fix:** classify the removed card via
`ctx.role_of(name)` (already memoized + injected) in `_role_report_minus`;
memoize the deck role-count on the context; add a regression test with
`staples.lookup_card` patched to raise. *Effort S. Risk low. Largely subsumed
by P1, but the seam violation is worth fixing regardless.*

### P4. `apply_collection_bias`: O(deck × collection) with uncached lookups
`deck_builder_personalize.py:429-462`. Inner loop over the user's whole
collection runs `ci_ok` (one `lookup_card`) before the cheap memoized role
check, once per ~99 outer cards — measured **9.6 s / 38,744 lookups** for a
5,000-card collection. **Fix:** precompute per-owned-card
`(ci_ok, role, mv, quality)` once, bucket by role, keep `owned_pool` order for
first-match semantics. Also the one-line reorder (role check before `ci_ok`).
**Write a direct unit test first — this hot spot currently has none.**
*Expected: 9.6 s → <1 s. Effort S. Risk low.*

### P5. Forge `compare()`: second parallelism axis (game chunking)
`compare_versions.py:952,1012,1059`; `forge_runner.py:405-411`. Concurrency is
capped at pod count (2-4) while each pod's `games_per_pod` games run serially
inside one JVM — on the 12-profile soak box, 8 profiles idle and a 40-game
comparison takes ~18 min regardless of cores. `forge_batch._even_chunks` +
`run_ab_parallel` already contain the chunking machinery; `_aggregate_pod`
folds by name so chunk-merge is additive. Gate on
`len(profiles) > len(pods)`. **Interactions to design:** per-chunk abort
margins (`compare_versions.py:393-461`) and pod-granular early-stop
(`:924-936`); also switch worker sizing to `forge_batch._default_max_workers`
(physical cores — `compare_versions.py:952` currently uses logical, contradicting
the repo's own 12-vs-24 benchmark). *Expected ~2.4× wall-clock (18 → 7.5 min)
on the reference box. Effort M. Risk medium; needs new chunk-merge tests.*

### P6. `meta_test`: parallelize the reference loop
`meta_test.py:503,555-580`. `filler_pairs=1` → one pod → `compare()` takes its
sequential branch, and the outer loop over references is also sequential: a
5-ref × 5-game meta-test uses exactly one core (~46 min). Dispatch refs across
Forge profiles with a free-runner queue + `ThreadPoolExecutor`, keep
`seat_parity=ref_idx % 2`, move the `user_w`/`user_l` accumulators under a
lock. *Expected 4-6× (46 → ~9 min) — best gain-per-hour in the Forge tier.
Effort S-M. Risk low; add one accumulator-race test.*

## Proposed — Tier 2 (solid wins, moderate scope)

- **P7. `run_ab_simulation` one-JVM-per-game** (`forge_batch.py:198-225`): 40-game
  A/B spawns 40 JVMs; seat alternation only needs two orders. **Measure first**
  from existing soak rows: startup cost = `SimResult.duration_sec −
  ParsedSim.total_game_ms`. Batch into 2 invocations + stall watchdog; rewrite
  `test_run_ab_simulation_alternates_seat_order_per_game`. ~10-20 % soak
  throughput. Risk medium-high (timeout semantics).
- **P8. `CardsLoader` DFC index** (`forge_cards_loader.py:222-281`): any MDFC
  miss decompresses all ~32.6k zip members to build the index, rebuilt per
  loader, and `interaction._default_loader()` builds a fresh loader per report
  (leaked ZipFile handle too). Build the index from `zf.namelist()` prefixes
  (~30 ms, no decompression) with full-scan fallback; `lru_cache` the default
  loader. 2-10 s → ~30 ms per Forge-backed `interaction_report`.
- **P9. Per-game stall watchdog in pods** (`forge_runner.py:411`;
  `compare_versions.py:581-588`): whole-pod timeout means a game-1 hang burns a
  worker/profile for up to 30-120 min. Kill on "no new `Game Result:` line in
  180 s" via the existing streaming callback; salvage machinery already exists
  and is tested.
- **P10. `deck_health` shared per-audit lookup memo** (`deck_health.py:356-364`):
  six independent per-card lookup walks per audit (473 calls / ~100 names
  measured). Thread one dict-backed memo (or `deck_legality._Cards`) through
  the signals. Mostly subsumed by P1 but zero-risk standalone.
- **P11. `knowledge_log`: `init_db` gating + single connection**
  (`knowledge_log.py:269-330`, 12 call sites): every read pays 2 connections +
  full idempotent DDL. Per-resolved-path "initialized" set + `ensure_schema`
  flag; batch `collect_user_decks_summary` into one `WHERE deck_id IN` query.
  Hundreds of ms off `commander-status`/dashboard with a 30-deck library.
- **P12. `run_match.run_matchup`**: no parallel dispatch at all, and the
  blocking `runner.run()` path loses output on timeout so its salvage block is
  a likely no-op — pass `keep_partial_output=True` (correctness) and lift
  `compare_versions`' profile dispatch into a shared helper.
- **P13. consistency Monte Carlo bundle** (`consistency.py`): `slots=True` on
  `_Card`; return a NamedTuple from `_play_out`; reuse a scratch list in
  `_draw_prefix` (undo swaps); `rng._randbelow` binding (byte-identical stream);
  visited-stamp array in `_pips_payable`. Combined ~40 % off
  `opening_hand_stats`; the determinism tests are the safety net. Note: no
  production consumer imports `consistency` today — do this only when one does.

## Proposed — Tier 3 (cleanups / latent cliffs)

- Import-time: `knowledge_log.py:73` imports `forge_runner` (→
  `concurrent.futures`, 50 ms) for one constant; `scryfall_client` eagerly
  imports `urllib.request` (~55 ms). ~80-110 ms off every CLI cold start
  (18 entry points). If moved, import `urllib.error` explicitly.
- EDHREC fetchers re-parse their disk cache per call; the salt-cache path is
  duplicated in 3 modules (`edhrec_client.py:747`, `bracket_estimator.py:292`,
  `card_score.py:903`) — consolidate behind one TTL-cached
  `salt_scores_cached()` (the `routes_oracle._PROJECTION_CACHE` pattern).
- `advisor`: two independent `DeckContext`s per audit share nothing
  (`_advisor_heuristic.py:507`, `:258`); pass one through.
- `_mod_combo` O(pool) scan per scored card (`card_score.py:1355-1360`) →
  memoized inverted index on the context.
- `lift_swaps` synergy scorer unmemoized (`deck_builder_personalize.py:204-235`,
  ~48k `lift_value` calls); `synergy_scorer` has zero direct tests.
- Log parsing: line-prefix dispatch + substring prefilters before unanchored
  `IGNORECASE` regexes (`log_parser.py:41-44,203-208`;
  `game_analyzer.py:279,299-300`); thread line lists instead of re-splitting
  multi-MB stdout blobs 3-5× (`compare_versions.py:615-616`).
- `SimResult.forge_log_tail` read (64 KB) unconditionally per sim and discarded
  in the shared-profile path (`forge_runner.py:434`; `compare_versions.py:596`)
  — gate on failure.
- `oracle_store.iter_cached_names` parses all 33,735 snapshots for one field;
  `bulk_refresh` reads each snapshot twice.
- Collection file read+parsed twice per build (`deck_builder.py:757`, `:1105-1113`).
- `deck_pricing` walks the deck twice with `lookup_card` (`web/deck_pricing.py:54`,
  `:194`).

## Bugs surfaced en route (not perf)

1. **The WotC Game Changers scrape is broken in production** — it currently
   parses 5 plausible names / 0 % overlap and is (correctly) rejected, so every
   process serves the bundled `_FALLBACK` list. The divergence alarm goes to
   stderr where nothing reads it. The parser
   (`game_changers._parse_card_names_from_html`) needs updating for the current
   page structure.
2. **`card_score` offline contract violation** (see P3): cut scoring performs
   live Scryfall HTTP on a cold cache despite the module's documented offline
   contract; invisible to the suite because tests pin `staples.lookup_card`.
3. **`run_match` timeout salvage is likely dead code** (see P12): the blocking
   subprocess path discards buffered output on a timeout kill, so the salvage
   parser has nothing to read in real runs.
4. **Mixed snapshot schema in the shared oracle cache dir**: trimmed snapshots
   written by `forge_py` lack `prices`, so those cards silently drop out of
   `deck_pricing` totals.
5. **`tests/conftest.py` had no network-blocking fixture** — any test that hits
   an unpatched `load_game_changers`/`lookup_card` path silently made live HTTP
   calls. The new memo shrinks the blast radius; an autouse socket-blocking
   fixture would eliminate it.
