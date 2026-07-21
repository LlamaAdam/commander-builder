# Changelog

All notable changes to this project will be documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; semver
applies once we tag a 1.0.

## [Unreleased]

### 2026-07-21 — FP-014 build-from-scratch, first cut (4 commits, UNMERGED)

Lives on `feature/fp014-build-from-scratch`, pending PR. The first vertical
slice of build-from-scratch deck assembly: commander + bracket → a legal
exactly-99. Coherence is borrowed from EDHREC's average-deck aggregate (not
from-atoms synthesis) and the improve loop is the intended quality path — see
FP-014 in [future-plans.md](future-plans.md) for the honest limitations. One
bullet per commit, oldest first. Fast lane after this branch: 2155 passed /
155 skipped.

#### Added

- **`feat(build)`** (`76f1ca7`): core assembler + `commander-build` CLI. New
  `deck_builder.py` orchestrator — seeds a legal 99 from
  `edhrec_client.fetch_average_deck` (or a role-target-filled shell from the
  commander page when no average deck exists), enforces
  commander/singleton/exactly-99/color-identity, and renders the `.dck`.
  `commander-build --commander "<name>" --bracket <n>` console script.
- **`feat(build)`** (`d02fc62`): color-source manabase (the "hard 20%"). New
  `deck_builder_manabase.py` replaces the FP-014.1 basics-only placeholder —
  keeps the seed's tuned duals/fetches, tops up fixing from the advisor's
  land tiers, and sizes basics to a per-color source target from a
  simplified Karsten-anchored pip model; land count from the curve. Degrades
  to basics-only when card/land data can't be resolved.
- **`feat(build)`** (`dd818b1`): personalization stages. New
  `deck_builder_personalize.py` — three net-zero like-for-like passes over
  the nonland spells (lift co-occurrence picks, bracket-steer toward the
  target, owned-collection bias), each preserving exactly-99 / singleton /
  color-identity. Lift stage skips without a harvested corpus (≥10 decks).
- **`feat(build)`** (`545b2db`): web build-from-scratch flow + improve
  hand-off. Async `POST /api/build_deck` → `GET /api/build_job/<id>` job
  endpoints, a "Build from scratch" tab in the web UI, and
  `commander-build --improve N` handing the assembled deck to the existing
  `commander-improve` empirical loop.

### 2026-07-21 — ManaFoundry-parity features (6 commits, UNMERGED)

Lives on `feature/manafoundry-parity`, pending merge. Six user-facing
features that close the gap with from-scratch builders like ManaFoundry.gg
(the assembly angle itself is planned as FP-014). One bullet per commit,
oldest first. Fast lane after this branch: 2089 passed / 155 skipped.

#### Added

- **`feat(pricing)`** (`9d16fb2`): cheaper-printing savings. `/api/dashboard`
  attaches a `printing_savings` block and the Est. price tile grows a "Save
  up to $X with cheaper printings (N cards)" list. New
  `scryfall_client.lookup_card_prints` (lazily-cached `/cards/search?unique=prints`
  walk) + `deck_pricing.printing_savings_for_deck_text`; legality-filtered,
  qty-aware, biggest-saving-first.
- **`feat(bracket)`** (`6b00ef5`): explainable bracket estimator. New core
  `bracket_estimator.py` estimates a deck's Commander bracket (1–5) from its
  list using the repo's existing rule encodings (Game-Changer / combo / MLD
  floors + weighted signals) and reports estimate/floor/confidence/reasons;
  `|est−declared| ≥ 1` soft-flags, `≥ 2` hard-flags a mismatch. Surfaced in
  the dashboard payload.
- **`feat(import)`** (`c110041`): MTGA + CSV paste import. New core
  `import_formats.py` (`arena_to_dck`, `csv_to_lines`, conservative
  `detect_paste_format`) lets the single paste textarea accept MTG Arena
  exports and CSV card lists on top of `.dck` / Moxfield-bulk paste;
  ambiguity always falls back to plain, malformed lines in a positively
  detected format raise a named error.
- **`feat(health)`** (`194dba1`): at-a-glance letter grade.
  `deck_health.compute_health_grade` aggregates the existing health signals
  into one A–F grade (0–100 score + worst-first reasons) via one documented
  weight dict; unavailable signals renormalize out, all-unavailable grades
  N/A. Rendered as the deck-health panel header + shipped in `/api/audit`.
- **`feat(collection)`** (`95a6ee7`): owned-card registry. New
  `collection.py` reads `~/.commander-builder/collection.txt` (plain or CSV,
  reusing `import_formats.csv_to_lines`); `commander-advise` /
  `commander-auto-curate` gain `--collection PATH` + `--owned-only` to
  exclude or flag unowned adds through the advisor's disclosure contract.
- **`feat(lift)`** (`e51f9cf`): co-occurrence lift analysis. New stdlib-only
  `lift_analysis.py` computes pairwise lift over the harvested deck corpus
  (support-floored, band sub-matrices, sha256-keyed cache) and surfaces
  "pairs well with your deck" candidate adds — as a dashboard "Lift picks"
  panel, `source='lift'` advisor evidence, and `commander-advise --show-lift`.

### 2026-07-20 — adversarial-review round 3 (5 commits, UNMERGED)

Third pass: an audit of round 2's own commits plus a completeness check
that no finding from either round was dropped (none was). Fast lane
after this round: 1838 passed / 147 skipped. One bullet per commit,
oldest first.

#### Fixed

- **`fix(import)`** (`10ece14`): same-id matching respects the
  `[USER]`/pool role boundary — a deck harvested into the opponent pool
  no longer blocks importing a `[USER]` copy; bracket drift renames the
  file (with `Name=` restamp) so `_bracket_from_filename` stays truthful.
- **`fix(verdict)`** (`69ab9a5`): sim warnings speak decisive-vs-total
  units (fillers win ~half of 4-player pod games); `commander-improve`
  default sim-games 25 → 45 so the 20-decisive verdict gate is actually
  reachable; post-sim note reports actual decisive shortfalls.
- **`fix(stats)`** (`bd63da8`): win-rate columns are head-to-head
  decisive in all four writers, for real this time; schema docstring
  carries a dated three-era convention history; cross-writer identity
  test now includes filler wins (fails against the old code).
- **`fix(misc)`** (`11001dc`): absorbed-pod seat-balance telemetry +
  note; meta-test aggregate seat line instead of per-reference spam;
  FP2 v2-rename fallback restamps `Name=`; `[CONTROL]` calibration
  leftovers excluded from pool-curation candidates; real-log evidence
  recorded that Forge cap-stops never mark survivors "has lost".
- **`docs`** (`23b7ea8`): round-2 catalog (listed here for completeness).

### 2026-07-20 — adversarial-review round 2 (14 commits, UNMERGED)

Second review pass over the same branch: an audit of round 1's own
16 commits plus deeper sweeps of previously light areas. Same branch/
worktree as below. Fast lane after this round: 1814 passed / 146
skipped (~95s). One bullet per commit, oldest first.

#### Fixed

- **`fix(revert)`** (`e0708cf`): restamp `Name=` when revert restores a
  historical snapshot — closes a regression round 1's own `f74913a` made
  deterministic; dck_meta empty-`Name=` and `DisplayName=` re-import
  hardening.
- **`fix(verdict)`** (`1cec8d4`): `save_iteration` accepts
  `inconclusive` (the web UI's default below 20 decisive games no longer
  400s); `commander-improve` default sim-games 5 → 25 so it can actually
  advance; loud sub-threshold warnings; JS margin/verdict rendering.
- **`fix(sim)`** (`e2e5b87`): compare pods alternate head-to-head seat
  order by pod index (meta-test alternates per reference) — removes the
  systematic seat-1 first-player bias toward the old deck.
- **`fix(curator)`** (`bf700ba`): preflight blame isolation (rotated
  fillers + one-retry exoneration — one bad deck can no longer zero the
  candidate pool); per-deck confirm-action accounting; typed
  `InsufficientSurvivorsError` instead of an uncaught traceback.
- **`fix(analyzer)`** (`d356132`): parse Forge's `Game Outcome: ... has
  lost <reason>` lines — commander-damage/poison/mill eliminations at
  positive life are now excluded from turn-cap draw resolution; run_match
  draws no longer inflate `avg_turns_when_lost`.
- **`fix(stats)`** (`611feff`): one win-rate convention (wins/decisive)
  across all four knowledge_log writers; meta-test totals include
  filler wins; `run_ab_parallel` turn averages weighted by real sample
  counts; every report shape carries a `draw_policy` label.
- **`fix(import)`** (`fc54986`): same-id re-import matching scans
  uniquified siblings — re-pulls of collision-renamed decks overwrite in
  place instead of minting duplicates.
- **`fix(web)`** (`c5e7af6`): JSON content-type gate on all mutating
  endpoints (closes the no-preflight cross-origin/DNS-rebinding vector);
  bracket validation everywhere; game-changer case-fold consistency;
  5 MB error-log cap; image-cache slug hashing.
- **`fix(ui)`** (`13f16de`): stale-response guards in `selectDeck` and
  the bracket-override handler; deck-size pill copy for >100-card decks;
  iteration-graph forked chains stack instead of overlapping.
- **`fix(soak)`** (`6d1c3cb`): `merge_soak --to-knowledge-log` is
  idempotent (content-identity dedupe) and skips gauntlet-schema rows
  instead of folding them as bogus 0-0 iterations.
- **`fix(degrade)`** (`6f89c6c`): `http.client.HTTPException` joins the
  EDHREC retry/degrade path; deck-health signals return None (rendered
  "unavailable") on Scryfall outage instead of misleading zeros.
- **`fix(parallel)`** (`bac47c1`): early-stop keeps draining in-flight
  pods so paid-for games are absorbed; `forge_log_tail` replaced with an
  honest marker under shared-profile parallel dispatch.
- **`fix(misc)`** (`6689566`): nine low-severity fixes — null-rationale
  render crash, draw saturation-vs-target invariant, Skyclave Cleric
  MDFC, evidence-score clamping, filler-pair duplication warning,
  `_is_decisive` docstring, soak_throughput div-zero/`--force`, batch
  SystemExit(0) status, honest name-based archetype classification.
- **`fix(guards)`** (`5a3266f`): dry-run honors the 99-card guard;
  Moxfield drift check detects commander swaps.

### 2026-07-19 — adversarial-review fix branch (16 commits, UNMERGED)

Lives on `fix/adversarial-review-2026-07-19` (worktree
`C:\dev\cb-review-fixes`), pending merge to `feature`. One bullet per
commit, oldest first. Fast lane: 1719 passed / 146 skipped (~90s).

#### Fixed

- **`fix(sim)`** (`f74913a`): rewrite `Name=` to the filename stem in the
  snapshot / proposer-apply / meta-test deck writers — new `dck_meta.py`
  holds the filename↔`Name=` win-attribution invariant; original name
  preserved as `DisplayName=`.
- **`fix(sim)`** (`1ae44f9`): surface pod failures in
  compare/run_match/iteration_loop instead of silently diluting stats.
- **`fix(tests)`** (`4dc4f2e`): knowledge_log DB path resolves at call
  time so the test isolation fixture actually isolates.
- **`fix(proposer)`** (`8d5f5e0`): validate cuts/adds against the real
  decklist (singleton + exactly-99 mainboard invariant); never write an
  illegal deck.
- **`fix(llm)`** (`543bc2f`): shared robust JSON extractor
  (`_llm_json.py`); garbage/truncated LLM replies raise loud
  `LLMJsonError`s instead of misleading fallbacks.
- **`fix(secrets)`** (`3fcde0a`): thread the BYO key explicitly; scrub
  `ANTHROPIC_API_KEY` from subprocess env.
- **`fix(security)`** (`b617678`): token-level placeholder check in the
  secret scanner; space-safe pre-commit hook; scanner runs in CI.
- **`fix(cli)`** (`304e4c8`): flag-aware batch argv rewriting (safe with
  `.dck` flag values); `SystemExit` no longer aborts the batch.
- **`fix(edhrec)`** (`5c7b3d6`): never cache empty parses; warn loudly on
  challenge pages.
- **`fix(import)`** (`6ccf3f0`): same-id re-import overwrites in place;
  name collisions uniquify inside the bracket tag.
- **`fix(revert)`** (`a3660fb`): back up the live deck (and print the
  backup path) before overwriting it.
- **`fix(export)`** (`99e8b53`): content-identity dedupe on
  knowledge-log import; no silent export truncation (10k cap removed).
- **`fix(web)`** (`7693136`): uid-suffix staged sim filenames to prevent
  same-second clobber races.
- **`fix(subprocess)`** (`a465d4e`): UTF-8 decoding everywhere
  Forge/CLI output is read.
- **`fix(misc)`** (`45d1d36`): None-safe ML features; deterministic
  cuts; URLError degradation; case-folded game-changer lookup.
- **`fix(import)`** (`4c79068`): stamp `Name=` from the final filename
  stem; drop the dead A/B name matcher.

### 2026-05-27 — FP-002/007/008-009/010 slices landed (4 review branches merged)

Built in parallel isolated worktrees, reviewed (green fast lane + scope audit +
a line-by-line correctness pass), and merged to `feature` (`--no-ff` each):
- **FP-002**: `scripts/build_fp002_deckset.py` — acquire top-liked Moxfield base
  + Claude-curated v2 for ~30 commanders toward the 80-deck margin-regression
  goal (resumable; smoke-tested live).
- **FP-007**: unified nav shell (Decks/Cards/Rules left-rail), `/api/library`
  cross-deck card search (backed by `decks_containing_card`), and
  `/api/rules/combo` + `/api/rules/game_changers`.
- **FP-008/009**: `image_url` added to the `/api/oracle` projection (rest of the
  oracle/card surface was already shipped).
- **FP-010**: first-run downloader + JRE extraction (`extract_jre`/`ensure_jre`)
  + deck-dir picker + window chrome (icon/single-instance/graceful shutdown) + a
  Windows CI build job. Review caught + fixed a latent crash — the app icon is a
  `webview.start()` kwarg, not a `create_window()` param (tests had masked it).
Fast lane: 1548 passed / 141 skipped on merged `feature`. (Installer slice for
FP-010 deferred — needs Inno Setup/NSIS tooling.)

### 2026-05-27 — orchestrator worklist landed (4 items, reviewed + folded)

The autonomous commander-orchestrator cleared the `orch/worklist` queue;
each fix was reviewed against its pinned contract and the worklist tests
were folded into permanent per-module homes (coverage now rides with the
code on `feature`, not the disposable branch). Full `--run-slow` suite green
(1618 passed).

#### Added

- **`feat(fp-002)`: `margin_analysis.single_feature_ols(samples, feature)`** —
  pure-stdlib single-feature OLS (slope/intercept/r²) plus a leave-one-out
  cross-validated RMSE (the honest out-of-sample error for the
  analysis→predictor step); constant-feature safe (slope 0.0, no ZeroDiv).
- **`feat(fp-010)`: `bootstrap._pick_jre_asset(release, system, machine)`** —
  picks the Temurin/Adoptium JRE archive for the caller's OS+arch from a
  GitHub release payload (mirrors `_pick_forge_jar_asset`); `None` when no
  asset matches. Enables first-run JRE auto-fetch.
- **`feat(fp-007)`: `web/_helpers.decks_containing_card(deck_dir, card_name)`** —
  cross-deck library search ("which of my decks run this card?"): sorted deck
  IDs whose `[Commander]`/`[Main]` runs the card, case-insensitive, leading
  quantity and `|SET|CN` edition tail stripped.

#### Fixed

- **`fix(game_changers)`: don't cache a failed/empty scrape.**
  `fetch_game_changers` now persists `.cache/game_changers.json` only when the
  WotC scrape actually produced names; a failed/empty scrape degrades to the
  bundled fallback *without* writing the cache, so it no longer masquerades as
  "fresh" for the full TTL and blocks a retry.

### 2026-05-26 — FP-002 reopened + cross-validated, FP-007/FP-010 started, deep audit + fixes

#### Added

- **`feat(fp-002)`: margin-regression analysis on the 40-game soak rows.**
  `scripts/margin_analysis.py` (pure stdlib — sklearn/numpy/scipy absent)
  regresses curator win-rate margin on pre-sim deck-health features in two
  designs: `--mode ab` (v1-vs-v2 in-pod) and `--mode gauntlet` (each deck vs a
  fixed gauntlet, unconfounded). **Finding (robust across both): curation is
  empirically ~neutral** (mean margin +0.0009 A/B, −0.0108 gauntlet); the one
  A/B "significant" feature (`wincon_protection`) did NOT replicate in the
  cleaner design → confound artifact. Only `deficit_total`/`under_built_roles`
  agree across designs (curation adds least to structurally-deficient decks).
  18 tests; `docs/future-plans.md` + deck-gen plan
  (`docs/future-plans.md`).
- **`feat(combo)`: infinite-combo → bracket enforcement.**
  `combo_detection.assess_deck_brackets()` maps each detected combo to its WotC
  bracket floor (two-card infinite/win → B4, 3+-card → B3), flags combos
  exceeding the declared bracket, recommends the bracket the deck actually
  plays at. Surfaced in `/api/audit` (+ stream) as `combo_assessment` and
  rendered as a bracket-pressure banner / collapsed panel.
- **`feat(advisor)`: EDHREC `/top` trending as a recency boost.** The heuristic
  recommender takes a `trending` set from `fetch_top_cards("month")`; trending
  candidates float up (secondary to diagnosis priority) and are annotated.
  Re-rank only — never introduces off-archetype/off-color cards.
- **`feat(web)`: role-targets deck-health tile** (under-built-role deficits) +
  `combo_assessment` in the audit payload.
- **`feat(fp-007)`: card-reference panel (slice 1).** `GET /api/card/<name>`
  (identity / commander-legality / price / printing over the cached
  `scryfall_client`) + a topbar "Cards" search + reference overlay. FP-007
  flipped PARKED → STARTED (`docs/future-plans.md`); the FP-006 gate is met.
- **`feat(fp-010)`: desktop EXE (started).** `commander_builder/desktop.py`
  (Flask on a daemon thread + native pywebview window), `packaging/`
  PyInstaller spec + `scripts/build_desktop.py` (verified: builds
  `dist/CommanderBuilder/CommanderBuilder.exe` with Flask assets bundled),
  `[desktop]` extra. `bootstrap.py` first-run dependency detection +
  Forge-jar downloader (`commander-builder-bootstrap`). `docs/future-plans.md`.
- **`feat(forge)`: `run_ab_parallel`** — split one A/B matchup's games into even
  chunks across cwd-isolated Forge profiles, run concurrently (capped at
  physical cores via `psutil`), and aggregate per-seat wins into one ABResult.

#### Changed

- **`feat(sim)`: A/B sim game-count options `5/10/20` → `10/40/100`** (default
  40, the soak high-confidence standard) with a live per-bracket time-hint next
  to the selector. 5-game sims are below the noise floor.
- **`feat(sim)`: low-N verdict gating.** Below 20 decisive games an A/B result
  records as a new `inconclusive` verdict (distinct from a genuine `neutral`
  tie) rather than a confident kept/reverted that a single-game flip could
  invert. Wired through `_proposer_sim`, `knowledge_log`, the dashboard
  PATCH allow-list, and the web save-verdict radios.

#### Fixed

- **`fix(web)`: security batch** — `_resolve_deck_path` `?path=` now `.dck`-locked
  (no clobbering non-deck files); image cache validates image magic before
  caching (no HTML-error-body served as `.jpg`) + TOCTOU-guarded read; BYO
  Anthropic key staged into `os.environ` under a serializing lock that always
  restores (sync + SSE paths); 3 unguarded route reads → clean 500s.
- **`fix`: 7 correctness bugs from a deep subsystem audit** — advisor default
  path dropped `diagnosis=`; `classify_role_extended` misclassified lands;
  subscription-CLI env scrub widened to all `ANTHROPIC_*` + bedrock/vertex
  toggles; `fetch_commander_page` Optional + guarded `__main__`;
  `_resolve_life_leader` no longer crowns an eliminated seat; `forge_runner`
  timeout-salvage preserves `avg_turns`; `analyst` tolerates fenced/prose LLM
  output + non-numeric confidence (degrades to heuristic).
- **`fix`: ResourceWarning leaks** — file/socket handles closed in
  `margin_analysis`, `combo_detection.refresh_combos`, `game_analyzer`.

### 2026-05-22 — FP promotions: A2 commander-improve (FP-012 slice 1), A1 web config (FP-011), A3 FP-001 spike memo

#### Added — bandit swap-selection strategy (FP-012 slice 2)

- **`feat(improve)`: `commander-improve --strategy bandit`.** The next
  slice past A2's fixed-N greedy loop: instead of accepting whatever the
  curator proposes, treat individual candidate swaps as multi-armed-bandit
  arms and learn — across A/B sims — which ones actually move the win
  rate. New `bandit.py` is the pure core: an `Arm` model, two policies
  (`EpsilonGreedy` + `UCB1`), and a `run_bandit` loop driven by an
  injected evaluator (no Forge/Anthropic/disk — fully unit-tested). The
  CLI builds arms from the advisor's candidate `(add, cut)` swaps; each
  pull applies one swap to the current best deck, A/B-sims it
  (seat-attributed), and rewards the win margin, advancing the base deck
  on improvement. New flags `--strategy {greedy,bandit}` (default greedy,
  so A2 behavior is unchanged), `--bandit-policy {epsilon_greedy,ucb1}`,
  `--epsilon`, `--ucb-c`. The full FP-012 agent (intent-learning,
  Bayesian opt, multi-deck orchestration) stays parked. 21 new tests
  (15 core + 6 integration).

#### Changed — unified the two Anthropic-key entry points (FP-011)

- **`feat(web)`: config.json is now the single source of truth for the
  BYO Anthropic key.** Previously there were two disconnected stores: the
  audit panel's "Set API key" wrote the key to browser `localStorage`
  (sent as the `X-Anthropic-API-Key` header), while the new Settings
  panel wrote it server-side to `config.json` — but nothing read the
  latter for audits. Now:
  - **Server:** `routes_audit._resolve_byo_key()` resolves the audit key
    in precedence `header → config.json → env`, so the key saved in
    Settings actually drives Claude audits. The "no key" fallback warning
    now points to Settings.
  - **Client:** the audit panel's key button is relabeled "Set API key…"
    and **opens the Settings dialog** instead of a `window.prompt`; once a
    key is configured it reads "API key ✓ (Settings)". The browser no
    longer stores the raw key — `loadAdvise` dropped the prompt/localStorage
    path, and any legacy `cb.audit.anthropic_key` is cleared on load.
  4 new/updated tests (config-key fallback + header-override-config +
  shared web `client` fixture isolates `COMMANDER_BUILDER_CONFIG`).
  Verified end-to-end in Chrome.

#### Docs — FP-008 confirmed already shipped (stale backlog entry)

- **`docs`: mark FP-008 (card-image lazy fetcher) done in STATUS.** No
  code change — the feature was already fully implemented: `app.js`
  `renderAddRow` renders lazy/async thumbnails through `cardImageUrl()` →
  the local `/api/card_image/<size>/<name>` route, with a click-to-expand
  overlay (`openCardImageOverlay`); the route disk-caches Scryfall bytes
  (`web/_image_cache.py`, with quota eviction + transient retry) and
  serves immutable `Cache-Control`. Covered by `test_image_cache.py`. The
  STATUS Tier-2 entry had simply never been struck through.

#### Added — iteration-graph milestone glyph (#012 UI remainder)

- **`feat(web)`: ⚑ milestone flag on iteration-graph nodes.** Finishes
  the one deferred piece of #012 (milestone backend shipped earlier in
  `c5ab6bd`). `iteration_graph_for_deck` now includes `milestone` on each
  node; `iteration_graph.js` renders a ⚑ glyph at the node's top-center
  when set and adds the label to the hover tooltip. 1 new test
  (`test_node_carries_milestone_when_set`); JS `node --check` clean.

#### Added — oracle-text card-reference store (FP-009, backlog #014)

- **`feat(oracle_store)`: `commander-oracle-refresh` + errata-drift
  detection.** New `oracle_store.py` is a thin surface over the existing
  `scryfall_client` snapshot cache (`mtg_cards/oracle_snapshots/`) — no
  second datastore (the #014 architectural call). Adds the three pieces
  FP-009 was missing: `card_reference()` (stable presentation alias for
  `format_card_for_display`), `check_errata(name)` (compares the cached
  snapshot's oracle text against a fresh, non-caching Scryfall fetch to
  detect WotC re-wordings), and `bulk_refresh()` driving the
  `commander-oracle-refresh` CLI (`--deck` / `--name` / `--all`,
  `--stale-days`, `--write`, `--json`). Read-only by default; only
  `--write` rewrites drifted snapshots. New console entry point. 17 tests
  (network stubbed via `CACHE_DIR` redirect + monkeypatched client).

#### Added — Pearson r for the forge_py correlation harness

- **`feat(correlation)`: `pearson_r()` + `correlation_summary` reports
  `pearson_r`/`pearson_n`.** New pure (numpy-free) Pearson helper; the
  summary now correlates the two engines' per-row win margins
  (`new_wins - old_wins`) and reports r against the 2026-04-28 "flip
  default only when r ≥ 0.90 across ≥30 paired rows" rule (the CLI prints
  a ✓/below-gate marker). `None` when undefined (<2 rows / flat series).
  The A3 spike's one noted scaffolding gap. 9 new tests.

#### Docs — FP-001 LLM-pilot feasibility spike (go/no-go memo)

- **`docs(fp001)`: LLM-piloted Forge AI spike → NO-GO (as scoped) /
  redirected + gated.** New `docs/fp001-llm-pilot-spike.md` records the
  A3 spike result: you cannot pilot Forge 2.0.12's AI with an LLM — it's
  a vendored compiled JAR run as a fire-and-forget `java -jar … sim`
  subprocess with no decision-injection seam (only `on_line` /
  `abort_check` = read-stdout / kill-process) and no Forge source to
  patch. The real seam is `forge_py`'s Python decision points, but that
  engine is absent here and not yet mature. The ≥30-paired-game,
  Pearson-r ≥ 0.90 experiment is fully designed and the scaffolding
  (`analyst.py` LLM client, `run_ab_simulation`/`run_ab_batch`,
  `forge_py_correlation` log) is ready, but there's no pilotable player
  to run it against today. FP-001 stays parked with a precise unblock
  condition. A valuable negative result that avoids a 2–4 wk dead-end.
  (No code change; one optional follow-up noted: a Pearson-r helper
  beside `correlation_summary`.)

#### Added — web config GET/PUT + Settings panel (FP-011 finished)

- **`feat(web)`: `GET`/`PUT /api/config` + per-user config store.** New
  `config_store.py` reads/writes a per-user `config.json`
  (`%LOCALAPPDATA%\commander-builder\config.json` on Windows,
  `~/.commander-builder/config.json` elsewhere; `COMMANDER_BUILDER_CONFIG`
  overrides). New `web/routes_config.py` blueprint:
  - `GET /api/config` returns the config with secrets **redacted** — an
    `anthropic_api_key_set` boolean + last-4 `anthropic_api_key_hint`; the
    raw token is never serialized, so a GET→render→PUT round trip can't
    leak or re-submit it.
  - `PUT /api/config` validates a sparse update (token shape mirrors the
    `scripts/scan_secrets.py` anthropic pattern; unknown keys, malformed
    tokens, out-of-range brackets → 400 with **nothing persisted**),
    merges into the stored config, and writes the file owner-only
    (0o600). The app binds 127.0.0.1, so the PUT surface is local-only.
  - Minimal Settings panel: a native `<dialog>` + `static/settings.js`
    wired to a topbar **Settings** button. The token field starts blank
    and shows a "(set · …last4)" hint; leaving it blank keeps the
    existing key.
  This was the last open piece of FP-011 (the pre-commit secret scanner
  shipped earlier in `803debe`). 32 tests (`test_config_store.py` +
  `test_routes_config.py`, endpoints via Flask test client).

#### Added — `commander-improve` greedy single-deck improve loop

- **`feat(improve)`: `commander-improve --deck <id> --rounds N`** — the
  bounded first slice of FP-012 (the autonomous deck-improvement agent).
  New `commander_builder/improve.py` runs the existing
  `commander-auto-curate --run-sim` pipeline for N rounds on ONE deck and
  advances **greedily**: a round's proposed deck becomes the next round's
  base only when the seat-attributed A/B verdict is `kept`;
  `reverted`/`neutral`/`pending` rounds keep the current base. Stops early
  on a no-op round (curator proposed no changes → converged) or an errored
  round. Bracket is inferred from the `[B<n>]` filename suffix when
  `--bracket` is omitted; `--deck <id>` resolves against the Commander
  deck dir (or pass a `.dck` path positionally). Pass-through curation/sim
  flags mirror auto-curate (`--mode`, `--sim-games`, `--sim-margin`,
  `--sim-fillers`, `--model`, `--source`, `--protect*`, `--db-path`).
  Composes the auto-curate machinery rather than reimplementing it, so
  every round inherits seat attribution, color-identity filtering,
  protected-card handling, and knowledge_log rows. The full multi-arm
  bandit / Bayesian agent stays parked. New `commander-improve` console
  entry point. 15 tests (loop logic driven by an injected `round_fn` —
  no Forge/Anthropic in the suite).

### 2026-05-21/22 — FP-003 shipped, A/B win-attribution fix, FP-002 concluded, FP-002 data-gen substrate

Commits `33536d7`…`7cef5a7` on `feature/2026-04-28-session`. Two future
plans moved off "parked": **FP-003 shipped**, **FP-002 concluded NOT
VIABLE** via this pipeline. A correctness fix to A/B win attribution
invalidated the prior FP-002 training labels.

#### Fixed — A/B sim win attribution (the bug that mattered)

- **`fix(forge)`: attribute A/B sim wins by SEAT, not deck name.**
  `run_ab_simulation` credited wins by deck *name*, but deck A and deck B
  routinely share the same internal `Name=` (a curated deck keeps its
  parent's; a detuned deck keeps the original's) → Forge emitted identical
  seat tokens → wins funnelled to one side. Now attributed by seat.
  **Consequence:** the prior `knowledge_log` FP-002 labels (78 kept / 153
  reverted) are **measurement artifacts** — train only on post-fix rows
  with `--min-id 314`. Pre-fix rows are kept in the DB as archive, never
  deleted. (`e8777b6`)

#### Added — concurrent Forge sims (FP-003 SHIPPED)

- **`feat(forge)`: concurrent A/B sims via cwd-isolated profile pool.**
  New `forge_runner.run_ab_batch(jobs, runners)` runs A/B sims across
  cwd-isolated Forge profiles in parallel (≈2× throughput). Second profile
  lives at `vendor/forge2`, recreatable via
  `scripts/setup_forge_profile.py`. Resolves the FP-003 feasibility spike:
  separate `cwd`-isolated profiles do avoid file-locking races. (`0f8f945`)
- **`feat(curator)`: hint `--parallelism` when batch + `--run-sim` default
  to 1.** (`33536d7`)

#### Concluded — FP-002 Phase-3 ML predictor: NOT VIABLE via this pipeline

- With correct (post-`e8777b6`) attribution, the curator's swaps almost
  never make a deck *worse* than its input — verified across detune depths
  0–10 → **11 kept / 3 neutral / 0 reverted**. The kept-vs-reverted
  classifier therefore has no negative class to learn. A future FP-002
  would need a different framing (e.g. regress on improvement margin), not
  more sim hours.
- Supporting data-gen substrate that led to the conclusion:
  - **`feat`: `scripts/detune_deck.py`** positive-example generator +
    `9a22240` unit tests. (the orchestrator holds the generators +
    `train_fp002.py`.)
  - **`ml_dataset`: pre-sim deck-composition features + regression tests**
    (`114122e`); **fix `extract_features` reading stale sim schema; add
    detuner** (`6822352`).

#### Added — subscription-CLI curator routing + secret scanner (FP-011 piece)

- **`feat`: route curator through the subscription `claude` CLI when no
  API key is present** (`12d7f2c`); `1682ada` unit-tests the adapter;
  `d38bb4c` documents why `claude_propose` stays SDK-only. Never inherit
  `ANTHROPIC_API_KEY` when invoking `claude`.
- **`feat(security)`: pre-commit secret scanner** — scans staged diffs for
  key prefixes (FP-011 piece; web config GET/PUT still TODO). (`803debe`)
- **`docs`: action plans for actionable future plans** (FP-003/011/001/010)
  at `docs/future-plans-action.md` (`af83510`); **`HANDOFF.md`** added as
  orientation + FP snapshot, split from the orchestrator (`8ed4cc1`,
  `7cef5a7`); `0bb2aa3` unbreaks `python -m commander_builder.proposer`.

### 2026-05-19 — post-PR-#3 work: CI fix, image cache, Forge slug fix, app.js splits, knowledge_log milestones, secret-scan hook

14 commits landed on `feature/2026-04-28-session` after PR #3 merged.
Tests: 1194 → 1256 fast lane (additionally exercised at 1366 with
`--run-slow`).

#### Fixed — master CI red badge

- **`fix(advisor)`: disambiguate unresolvable vs colorless commander
  in CI filter.** PR #3 introduced an autouse fixture that masked a
  latent bug — `_advise_steps` treated test stubs (dicts without a
  `color_identity` key) as a colorless commander and rejected every
  colored add. Adds `_safe_ci_lookup` that requires
  `"color_identity" in card` before considering the commander
  resolved; mirrors the pattern already in `proposer.auto_propose`.
  Cleared master's red CI badge. (`0eae7ae`)

#### Added — image cache reliability (FP-008 follow-through)

- **`feat(web)`: image cache retries once on transient Scryfall
  failure (#003).** Wraps `_default_http_get` with one retry on
  `URLError` / 5xx; explicitly skips 404 to avoid wasted round-trips.
  500ms backoff between attempts; single retry only (this is
  interactive user traffic, not a batch job). (`1f255b2`)
- **`feat(web)`: disk-quota eviction policy for image cache (#002).**
  `MTG_IMAGE_CACHE_QUOTA_BYTES` env override (default 500 MB) +
  LRU-by-mtime eviction triggered after each `fetch_and_cache` write.
  Stat-and-bail when under quota keeps the hot path cheap. (`7df7510`)

#### Added — Forge corpus integration (FP-001 slices 1-3)

- **`feat(forge)`: read-only Forge card-script parser (#017).** New
  `forge_script_parser.py` turns one Forge `.txt` card script into a
  structured `CardScript` AST (`name`, `mana_cost`, `types`, `pt`,
  `loyalty`, `keywords`, `abilities`, `svars`, `oracle`, plus DFC
  via `faces`). Parses 129 distinct `AB$` effect kinds across 32,626
  Forge cards. Read-only by design — interpretation is the future
  engine's job; the parser is the cheapest first slice of FP-001
  that's useful by itself. 8 byte-exact fixtures pin the contract;
  17 tests across vanilla creatures, lands with mana abilities,
  sorceries with chained sub-abilities, planeswalker loyalty,
  battle defense, channel-ability legendary lands, DFC handling,
  malformed input tolerance. (`c99cf59`)
- **`feat(forge)`: deck-library static-analysis CLI (#018).** Dual-mode
  `forge_cards_loader.py` (auto-detects unzipped letter-tree vs the
  canonical `cardsfolder.zip`), plus `deck_library_analyzer.py` that
  walks `.dck` files, parses each card's script, and folds into a
  `LibraryReport` (effect-kind histogram, ability-category histogram,
  keyword histogram, SVar reference counts, DeckHints frequency,
  unresolved-cards list). CLI at `scripts/analyze_deck_library.py`
  with `--json` / `--max-decks` / `--top` / `--per-deck` flags. Smoke
  run on 50 of 345 real decks: confirmed `Mana` / `ChangesZone` /
  `Continuous` are the first primitives a Python engine would need.
  (`3dba38b`)
- **`feat(forge)`: oracle-text drift detector + DFC loader fix (#019).**
  New `oracle_diff.py` cross-references Forge's `Oracle:` field
  against Scryfall's `oracle_text` per card and surfaces mismatches
  for human review. `normalize_oracle` handles Forge's literal `\\n`,
  `CARDNAME`/`NICKNAME` placeholders, Unicode minus signs, and
  planeswalker `[-N]` loyalty brackets. DFC support via per-face
  oracle concatenation. CLI at `scripts/oracle_diff_report.py` with
  `--by-pattern` triage that buckets diffs into the known errata
  patterns. Smoke run on 10 decks: WotC did a massive `X` → `this
  <type>` errata sweep Forge is uniformly stale on; ~200 cards in
  just 10 decks lean on the stale text. (`9257ae5`)
- **`feat(oracle_diff)`: data-driven bucket rules (#020).** Bucket
  rules moved from hardcoded lambdas in the CLI into
  `data/oracle_diff_buckets.json`. Maintainers can add new
  errata-pattern buckets without touching code; next WotC errata
  sweep just gets a new row in the JSON. (`dc219c6`)
- **`fix(forge)`: slug rule + `upcoming/` fallback — 88% → 99.82%
  resolve.** Forge stores card scripts by a slug derived from the
  card name. Two corpus quirks were under-resolving the deck-library
  analyzer: (1) apostrophes are removed not underscore-converted
  (Geist's Dominion → `geists_dominion.txt`); (2) diacritics are
  NFKD-normalized to ASCII (Lim-Dûl's Vault → `lim_duls_vault.txt`);
  (3) the active Forge install ships preview cards in `upcoming/`
  that the loader didn't search. All three fixed; deck-library
  resolve rate jumped from 88% to 99.82% on the real 345-deck
  library. (`7acf2c4`)

#### Added — Tier-3 app.js refactor (slices 2, 3, 4)

- **`refactor(web)`: extract `audit_streaming.js` out of app.js
  (#007).** Pulled the SSE streaming cluster (`streamAuditEvents`,
  `_parseSseFrame`, `updateAuditProgress`, `renderManabasePreview`)
  into its own 226-line module. (`764b5cd`)
- **`refactor(web)`: extract `deck_health_ui.js` +
  `avg_deck_preview.js` (#008 + #009).** Pulled the deck-health tile
  cluster (`renderDeckHealthTiles`, `renderHealthTile`,
  `renderSaltWarningBanner`) into a 216-line module and the average
  deck preview cluster (`renderAverageDeckPreview`,
  `bracketSlugToInt`, `buildAverageDeckBody`, `renderAverageDeckCard`)
  into a 142-line module. **Cumulative app.js: 3738 → 2829 lines
  (-909).** (`c203464`)

#### Added — knowledge_log milestone tagging (#012, backend only)

- **`feat(knowledge_log)`: milestone column + PATCH endpoint.** Schema
  v1 → v2 migration adds `milestone TEXT` to `iterations` with a
  partial index; migration runs unconditionally on every `init_db`
  and is idempotent. New `set_milestone(iteration_id, label,
  db_path)` truncates to 64 chars and treats empty/None as clear.
  `PATCH /api/iterations/<id>/milestone` accepts `{"milestone": str
  | null}`. `Iteration.milestone` dataclass field is defensively
  read for legacy DBs. UI flag glyph in the iteration-graph node
  deferred — needs a browser smoke test that the autonomous run
  can't reliably validate. 16 new tests (9 knowledge_log + 6 web +
  1 migration test). (`c5ab6bd`)

#### Added — refresh_card_lists self-mill auto-suggest (#010)

- **`feat(scripts)`: auto-suggest self-mill candidates via Scryfall.**
  `parse_self_mill_from_response` + `fetch_self_mill_candidates` in
  `_card_list_refresh.py`. CLI `--only self-mill` now hits Scryfall
  instead of printing the manual-only stub. Filters: requires
  movement involving "your library" + "your graveyard" OR the bare
  `\bmill\b` keyword; excludes "target opponent/player" and "each
  opponent/player mill" so opponent-mill cards (e.g. Glimpse the
  Unthinkable) don't slip in as self-mill. (`b50142c`)

#### Added — pre-commit secret scan hook (#006)

- **`feat(security)`: pure-stdlib pre-commit secret scanner.** Catches
  Anthropic / OpenAI / GitHub PAT / AWS / Bearer / PEM private-key
  shapes in staged files and aborts `git commit` if any fire. Pure
  stdlib chosen over `detect-secrets` / `gitleaks` so the hook works
  the moment a contributor runs the installer — zero install friction
  blocking the first commit. Two install paths:
  `python scripts/install_git_hooks.py` writes a no-deps
  `.git/hooks/pre-commit` shim; `pre-commit install` via the shipped
  `.pre-commit-config.yaml` wires the same scanner as a `local` hook.
  Three opt-out layers: placeholder filter (`...`, `YOUR_`,
  `<token>`), inline `# pragma: secret-scan-allow`, and
  `.secrets-baseline` fingerprints. 25 tests. (`342e1b5`)

#### Process

- **`docs(status)`: prune stale per-commit blocks; point to CHANGELOG
  (#004).** STATUS.md 411 → 390 lines; replaced verbose 2026-05-14/15
  + 2026-05-13/14 commit listings with one-paragraph pointers to
  this CHANGELOG. (`521f1ae`)
- **`ci`: opt into Node 24 runtime to clear deprecation warnings.**
  Added `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` env block to
  `.github/workflows/test.yml` ahead of the September 2026 Node 20
  cutoff. (`58ef9b3`)

---

### 2026-05-15/16 — auto-curate pipeline + audit polish + Tier-3 refactor

29 commits landed on `feature/2026-04-28-session`. Tests: 875 → 1194.

#### Added — `commander-auto-curate` end-to-end loop

- **`feat(curator)`: `commander-auto-curate` — advisor + Claude curator
  + apply.** New CLI that runs the improvement advisor on a deck, hands
  the AdviceReport to a Claude curator, and writes a versioned `.dck`
  with the proposed swaps. Replaces the manual prompt-paste workflow
  for unattended overnight runs. (`b859463`)
- **`feat(curator)`: `--run-sim` closes the loop with Forge A/B verdict.**
  After the proposal lands, runs a head-to-head Forge A/B sim between
  the old and new decks (default 5 games). The empirical verdict
  (`kept` / `reverted` / `neutral`) plus full `ABResult` metrics
  populate the knowledge_log row that previously stayed at
  `verdict='pending'` forever. (`023134e`)
- **`feat(curator)`: `--mode` preset (polish / overhaul / free).**
  `polish` = 5 adds + 5 cuts (safe overnight default), `overhaul` =
  15 + 15 (deliberate revision), `free` = unbounded. Per-CLI
  `--max-adds` / `--max-cuts` override the preset. (`a080901`)
- **`feat(curator)`: protect pet cards from being proposed for cuts.**
  `[metadata] Protect=` in the `.dck` plus `--protect` CLI flags
  defines a pet-card list. The curator strips matching cuts pre-prompt
  and post-response so user favorites survive every iteration. (`fe54c4b`)
- **`feat(curator)`: bracket-aware filler picking for `--run-sim`.**
  Filler decks for the 4-player A/B pod are picked by bracket distance
  to the user's deck, defeating the noise-dominated verdicts a B4 deck
  got when matched against B5 cEDH + B2 casual fillers. (`8e84962`)
- **`fix(curator)`: post-filter Claude's off-color adds via Scryfall
  CI check.** Defensive filter rejects any add whose Scryfall
  `color_identity` isn't a subset of the commander's CI. Hybrid mana
  is required to fit fully. None CI = couldn't resolve commander →
  skip filter (test fixtures, custom commanders). (`962776a`)
- **`feat(curator)`: writes a pending iteration row.** Each
  auto-curate run lands a row in the knowledge_log with
  `verdict='pending'` so the iteration chain stays threaded even
  before `--run-sim` fills in the empirical result. (`f0e06a0`)

#### Added — audit panel signals

- **`feat(audit)`: deck-health tile row — 5 construction-quality
  signals.** MDFC count, spell density, mana sinks, wincon
  protection, self-mill enablement. Surfaces shape problems the
  advisor's narrative diagnosis doesn't. (`68a420c`)
- **`feat(audit)`: detect activated-ability mana sinks (Tier-2.1).**
  `count_mana_sinks` now picks up `{R}: ...`-style pure-mana
  activations (Spikeshot Goblin, Inkmoth Nexus) and self-untap loops
  (Staff of Domination), in addition to the existing `{X}`-cost
  spell heuristic. (`a06e5dc`)
- **`feat(audit)`: back-fill EDHREC categories from Scryfall
  type_line (Tier-2.2).** Roughly 21% of average-deck preview cards
  weren't bucketed by EDHREC and fell into the UI's catch-all
  'Other' pile. The advisor now back-fills via Scryfall `type_line`
  with priority-ordered mapping (Artifact Creature → Creatures,
  Legendary Planeswalker → Planeswalkers, etc.). (`7c72993`)
- **`feat(ui)`: EDHREC average-deck preview `<details>` in audit
  panel.** Collapsible section ranking what a typical deck for this
  commander+bracket includes; cards in the user's deck are
  highlighted, missing categories surface holes. (`a061d10`)
- **`feat(ui)`: salt-warning banner above audit at B1-B3.** Aggregate
  EDHREC salt-score signal flagged at the top of the panel for casual
  brackets so the user can swap out hostile cards before play. (`dc3b5ee`)
- **`feat(ui)`: iteration-graph SVG view in dashboard history panel.**
  Nodes (iterations) + edges (swap rationales) projected as an SVG
  graph from the knowledge_log; verdict tinting at a glance. (`51c7b04`)
- **`feat(web)`: manual verdict UI for pending iterations
  (Tier-1.3).** New `PATCH /api/iterations/<id>/verdict` endpoint plus
  Kept/Reverted/Neutral buttons in the iteration-graph view. Manual
  web iterations no longer stay at `verdict='pending'` forever. (`be6a659`)
- **`fix(audit)`: quantity-aware cuts in `_apply_swaps_to_dck`.**
  Cutting Mountain twice from a 27-Mountain stack now decrements to
  25 instead of dropping the whole line. (`fb628eb`)
- **`fix(audit)`: quantity-aware adds in `_apply_swaps_to_dck`
  (Tier-1.1).** Symmetric to the cut fix — adds for cards already in
  the deck increment the existing line (preserving the `|SET|CN`
  edition tail) instead of appending a duplicate `1 <Name>` line.
  Duplicate add entries collapse to one summed line. (`6e0836f`)

#### Added — secrets + bulk import

- **`feat(secrets)`: external credentials file outside the repo.**
  `~/.commander-builder/credentials` loaded by every commander-* CLI
  entry; keeps `ANTHROPIC_API_KEY` out of repo + dotfiles. (`41e1b3d`)
- **`feat(import)`: bulk Moxfield import — CLI + API + UI tab.**
  Paste 50 Moxfield URLs, import them all to `vendor/forge/userdata/
  decks/commander/` in one go. (`36f08b0`)

#### Added — tooling

- **`feat(scripts)`: `refresh_card_lists` for hardcoded `deck_health`
  staleness checks (Tier-2.3).** New CLI diffs `_MDFC_LANDS` against
  current Scryfall (`layout:modal_dfc` + at-least-one-Land filter)
  and prints stale + candidate reports. `_WINCON_PROTECTION` and
  `_SELF_MILL_ENABLERS` get manual-curation reminders. Pure helpers
  in `_card_list_refresh.py` with 15 tests. (`4b57e79`)
- **`tool`: `scripts/compare_curator_modes` — A/B verification for
  `--mode`.** Same deck through polish / overhaul / free with
  side-by-side curator output. (`62e3e41`)

#### Added — refactor

- **`refactor(proposer)`: split into filters / sim / cli modules
  (Tier-3).** `proposer.py` 1766 → 944 lines. New private modules:
  `_proposer_filters.py` (post-response filters), `_proposer_sim.py`
  (A/B sim helpers + knowledge_log writer), `_proposer_cli.py`
  (`auto_curate_main` + argparse). All public symbols re-exported
  from `proposer.py`; zero behavior change. (`8ba5b0a`)

#### Fixed — live-auto-curate smoke bugs

- **`fix(curator)`: four bugs from live auto-curate smoke.** Cp1252
  encoding handling for `.dck` reads, deck-path doubling on Windows,
  empty-env-var-shadow over the credentials file, JSON extraction
  for prose-prefixed Claude responses. (`82b3dd0`)
- **`fix(curator)`: balance + pad to produce legal decks.** Curator
  output is balanced via `min(adds, cuts)` and padded to 99 main
  with basic lands. Forge would refuse short decks; now every
  auto-curate output is legal. (`5b2e52b`)
- **`fix(audit)`: `main_count` counts proposed_text directly (was
  inflated).** UI badge now matches the actual deck size after
  swaps. (`03dc8c8`)
- **`fix(ui)`: audit button passed PointerEvent as source — Audit
  failed 400.** Defensive arrow-function wrap; pinned with backend
  validation. (`5c96e3d`, `bbdd327`)

#### Process

- **`prompt(curator)`: caps are ceilings, not targets.** System
  prompt explicitly tells Claude not to fill `--max-adds` just
  because it can — quality beats quantity. (`fb07599`)
- **`test(isolation)`: autouse fixture redirects `DEFAULT_DB_PATH` to
  `tmp_path`.** Prevents test runs from polluting the real
  knowledge_log. (`4e77154`)


### 2026-05-13 — doc consolidation + EDHREC retry polish

#### Added
- **`feat(edhrec)`: honor `Retry-After` header + log each retry.** RFC 7231
  parsing for both `delta-seconds` and HTTP-date forms; falls back to exp
  backoff on malformed input. `MAX_RETRY_AFTER_SEC=30` caps the honored
  delay so a CDN incident sending "wait 5 min" can't pin the audit. Each
  retry emits a `[edhrec] retry N/3 after HTTP 503 — sleeping 1.0s` line;
  happy path stays quiet. 6 new tests cover seconds form, HTTP-date,
  capping, malformed fallback, log emission, no-log-on-success.

#### Project management
- **Docs consolidated from 15 files to 4.** README + STATUS + CHANGELOG
  (this file) + `docs/architecture.md`. Removed: `PROJECT.md`,
  `BACKLOG.md`, `FUTURE_PLANS.md`, `CONTRIBUTING.md`,
  `docs/audit_workflow.md`, `docs/SPEEDUP_TRACK_1.md`,
  `docs/HANDOFF_2026-05-06.md`. Content was synthesized into the
  surviving four rather than concatenated. Earlier session snapshots
  (`HANDOFF_2026-04-26`, `HANDOFF_2026-04-27_afk`,
  `docs/session_state_2026-04-28`, `docs/evaluation_retrospective_2026-04-28`)
  were deleted as superseded in a separate commit.

### 2026-05-12/13 — Track 1 speedup + LLM analyst + Track 2 prep + audit polish

8 commits landed on `feature/2026-04-28-session`. Tests: 559 → 674.

#### Added — Track 1 (A/B sim wall-time)
- **`feat(speedup)`: parallel pods + adaptive early-stop + intra-pod abort.**
  `compare_versions.compare()` now dispatches pods through
  `ThreadPoolExecutor` (workers = `min(len(pods), cpu_count())`);
  `parallel=False` preserves sequential behavior for tests. Adaptive
  early-stop cancels queued pod futures when `|margin| > games_remaining`;
  `report.stopped_early` + `pods_planned` reflect what actually ran.
  Sprint 1C (originally "JVM persistence") **reframed to per-pod
  intra-pod abort** — `forge_runner._run_streaming` gained an
  `abort_check(line) -> bool` callback that parses `Game Result:` lines
  and kills the JVM as soon as the in-pod margin exceeds games-left.
  `_synthesize_match_result(state)` builds a `Match Result:` summary so
  `log_parser` sees a complete record. Pod result dict gains
  `intra_pod_aborted` + `games_actually_played`. Auto-scaled
  `auto_filler_pairs()` returns `min(4, max(2, cpu_count() or 2))`.

  **Measured impact** (theoretical on 4-core box, decisive matchup):

  | Mode | Sequential | After 1A only | After 1A+1B+1C |
  |---|---|---|---|
  | 1v1 5g (decisive) | 30 s | 30 s | ~18 s (kill at game 3) |
  | Pod 5g (2 pairs, decisive) | ~7 min | ~3.5 min | ~2 min |
  | Pod 20g (2 pairs, decisive) | ~28 min | ~14 min | ~5 min |
  | Pod 5g (2 pairs, close) | ~7 min | ~3.5 min | ~3.5 min (no abort) |

  Close matches don't speed up — abort only fires when the verdict is
  uncatchable. Sprint 1D (result cache) skipped as low-leverage; real-use
  cache hits are rare (each propose-swap stages a fresh proposed file).

#### Added — LLM analyst + BYO key
- **`feat(web)`: `/api/audit?llm=claude` with BYO key.** Key arrives via
  `X-Anthropic-API-Key` header, injected into env for the call's lifetime,
  restored in `finally` so it never leaks across requests. Per
  FP-011-shaped plan.
- **Claude model dropdown** (Haiku 4.5 / Sonnet 4.5 / Opus 4.5) stored in
  `localStorage`. Default Sonnet. `advise(claude_model=...)` plumbs to
  the Anthropic SDK call.
- **`AdviceReport.fallback_reason`** threads the actual exception cause
  through. UI now surfaces e.g. *"Reason: claude advisor failed
  (BadRequestError: Error code: 400 - credit balance too low)"* instead
  of a generic "unavailable" string.
- **`feat(audit)`: card-name validator flags Claude hallucinations.** New
  `_validate_card_names(recs)` cross-checks every recommended card
  against the Scryfall cache. `SwapRecommendation.name_known` is
  `True` (Scryfall returned a card), `False` (404 — fake), or `None`
  (lookup raised; never accuse a real card on transient failure).
  `/api/audit` surfaces `name_known` per add/remove entry plus
  `unknown_card_count`. UI renders a `⚠ not in Scryfall` pill inline +
  a summary near the headline.

#### Added — knowledge log
- **`feat(klog)`: POST `/api/save_iteration`** persists audit_manifest +
  sim_report + verdict to `knowledge_log.sqlite`. Verdict dropdown
  (kept/reverted/neutral/pending) under propose-swap result; "Save
  audit (no sim)" button on the audit panel persists pending rows for
  Phase 3 ML data even without a sim.
- **`feat(klog)`: pricing snapshot in iteration manifest.** Optional
  top-level `total_price_usd` in payload → merged as
  `audit_manifest.pricing = {total_price_usd, captured_at}`. Caller-
  supplied pricing wins; zero is legal; non-numeric and bool both 400.
  UI captures `stat_tiles.est_price_usd` in `renderDashboard` and forwards
  via both save payloads.

#### Added — Track 2 prep (forge_py multi-deck sim)
- **`feat(track2)`: forge_py correlation harness (opt-in).** New module
  `forge_py_correlation` runs `forge_py.combat.run_multiplayer_game`
  alongside Forge for paired-verdict logging.
  `run_forge_py_ab(old, new, games, mode)` returns `ForgePyABResult`.
  `log_correlation_row(...)` appends CSV.
  `correlation_summary(log_path)` reports `{rows, agree, disagree,
  agreement_rate, errors}`. CLI: `python -m
  commander_builder.forge_py_correlation` + `--json` flag. Endpoint:
  `/api/correlation_summary`. Topbar surface in `loadHealth()` shows
  `forge_py 75% agree (12)` when rows exist. Opt-in via
  `COMMANDER_BUILDER_CORRELATE_FORGE_PY=1`. `forge_py` imported
  lazily — missing install returns `ForgePyABResult(error="forge_py not
  importable")`.

#### Added — forge_runner + edhrec_client robustness
- **`feat(forge)`: detect bundled jar version + warn when stale.** New
  `detect_forge_version(forge_dir)` parses the version out of the jar
  filename and reads `vendor/forge/build.txt` for the build timestamp.
  Returns `ForgeVersionInfo(version, build_date, age_days, is_stale)`;
  `is_stale=True` only when age > 90 d AND build_date is known.
  `create_app` prints `[startup] Forge jar 2.0.12 (19d old)` or
  `[startup] WARN: Forge jar 2.0.12 is 134d old — consider updating
  from github.com/Card-Forge/forge/releases`. New `/api/forge_version`
  endpoint.
- **`feat(edhrec)`: retry transient HTTP failures with exponential
  backoff.** New `_http_get_text_with_retry` retries 5xx, 429,
  URLError, TimeoutError with `base_delay * 2 ** attempt` (1s/2s/4s);
  skips 404 + other 4xx (deterministic). Plumbed into both
  `fetch_commander_page` and `fetch_average_deck`. Sephiroth's 503
  from 2026-05-06 now self-heals instead of falling through to None.

#### Added — UX polish
- **`feat(ux)`: bracket auto-inference + modal scroll fix.** Dashboard
  emits `inferred_bracket` alongside declared `bracket`. UI warns when
  heuristic suggests higher than declared. `.modal { overflow-y: auto }`
  + sticky header keeps close button reachable on tall content.
- **Soft refresh.** New `selectDeck(deckId, li, { soft: true })` keeps
  prior dashboard rendered while next data fetches. Used by Edit-deck
  save and Attach-Moxfield. Avoids 5+s "Loading…" blank on Scryfall
  lookups for new cards.
- **Sub-100 source padding.** `_pad_main_to_99()` tops up short decks
  (e.g. Goblin at 71 main) with basic lands matching the deck's
  existing color distribution. Audit response includes `basics_padded`
  + `basics_padded_breakdown`.
- **Stale-file cleanup at boot.** `_cleanup_stale_staged_files()`
  sweeps interrupted `*_proposed_<ts>.dck` / `*_converted_<ts>.dck`
  files older than 60 s. Runs in `create_app`.
- **JS error collector.** New `/api/log_error` endpoint + browser
  bootstrap on `window.error` / `unhandledrejection`. Appends to
  `vendor/_js_errors.log`; returns a ref token (`20260429023316-a1b2`)
  the user can paste into chat.

#### Fixed
- **`fix(moxfield)`: convert pipe-delimited lines to parens format.**
  Moxfield rejects Forge's `1 Arcane Signet|MIC|157` format. New
  `to_moxfield_line()` helper converts to `1 Arcane Signet (MIC) 157`.
  `dck_to_textarea` runs every emitted line through it.
- **TDZ ReferenceError on `runProposeSwap`** — `mode` was read before
  declaration; now hoisted.

### Earlier 2026-04-28 — project-manager session

- New web app: Flask scaffold + 7-panel dashboard. Routes:
  `/api/health`, `/api/decks`, `/api/dashboard?deck=<id>`,
  `/api/iterations[?deck=<id>]`. Path-traversal guard validates deck
  inputs against `deck_dir`. `pyproject.toml` adds `[web]` extra
  (`flask>=3.0`). 21 tests cover route shapes + traversal guard.
- Knowledge-log demo seeder writes a 4-iteration arc
  (pending → kept → reverted → neutral) for a fictional Omnath deck. Lets
  the UI's version-history strip develop end-to-end before real Forge
  data exists. 6 tests.

### Earlier 2026-04-27 — autonomous-improvement session

- **Shared `mtg_cards/` folder** at `C:\dev\mtg_cards\`. Both
  commander_builder and forge_py resolve their card cache via
  `MTG_CARDS_DIR` env var.
- **`scryfall_client.refresh_card()`** + `forge_py.cards.refresh()` —
  force-fetch bypassing cache (live-text directive).
- **`staples.py`** canonical universal-staples + `classify_role` +
  frequency labels + confidence tiers. Deduplicated
  `meta_test.UNIVERSAL_STAPLES`.
- **Suggestion-quality pass**: staples-exclusion, role-tagged adds,
  diagnosis-driven re-ranking. All four FP-006 suggestion-quality gates
  closed.

### Earlier 2026-04-26 — initial project management

- `BACKLOG.md`, `STATUS.md`, `CHANGELOG.md`, `docs/architecture.md`,
  `pyproject.toml` (`pip install -e .` works; `PYTHONPATH=src` no
  longer required). CLI entry points: `commander-import`,
  `commander-snapshot`, `commander-curate`, `commander-match`,
  `commander-compare`, `commander-iterate`, `commander-push`.
- `CONTRIBUTING.md` — dev setup walkthrough, conventions, ADR template.
- README.md rewritten to reflect Phase 2 workflow.
- `.gitignore` extended to cover `*.log`, `.cache/`, `*.sqlite`.

### Added
- New `archetype.py` module with heuristic classifier (filename hint →
  keyword content scan → midrange fallback). Replaces `_stub_classifier` in
  `pool_curator`. `claude_archetype` and `ollama_archetype` stubs in place
  for future LLM escalation. Closes GAP-001.
- `pool_curator` CLI `--max-candidates` (default 12) with seed-stable
  sampling — pool curation can now actually run end-to-end without 4+ hour
  wall times. Closes GAP-002.
- New `iteration_loop.resolve_deck_id()` — reads `Moxfield=<publicId>` from
  .dck metadata so iteration lineage survives Moxfield deck renames. Falls
  back to filename for legacy decks. Closes GAP-003.

### Fixed
- `archetype` regex bug: `+1/+1 counter` keyword had unescaped `+` causing
  `re.PatternError`. Caught immediately by the test suite — 173/173 passing
  after the fix.
- `pool_curator._filename_for_match` collision-suffix gap (GAP-004): now
  strips both `[USER]` prefix and `_uniquify` `(N)` suffix before matching;
  prefers exact stem over de-uniquified to disambiguate when both forms
  exist on disk. Was silently dropping wins for any deck whose filename had
  been uniquified.
- `pool_curator._split_into_slices` persistent-violation case (GAP-006):
  bounded swap search (5 swaps + default) replaces the prior one-shot 3↔4
  swap. If all candidates violate, ships default with a `WARN`. Also no
  longer mutates the caller's `top6` list.

### Tests (Tier 1 hardening pass)
- 173 → 183. Added `test_iteration_loop` (5 new tests for `run_one_iteration`
  with mocked `compare`), 3 new `_filename_for_match` tests (collision
  suffix, [USER] prefix, exact-over-deuniquified ordering), 3 new
  `_split_into_slices` tests (no-mutation, search-finds-non-violating,
  WARN-when-no-arrangement).

### Added (Tier 2 round)
- New `proposer.py` module + `commander-iterate --auto-propose` flag.
  Three-backend router (manual / Claude / Ollama) with graceful fallback to
  manual when LLM backends aren't available. Closes GAP-005.
- New `status.py` module + `commander-status` entry point. Reports decks
  per bracket, curated pools, recent matches/compares, knowledge_log stats.
  `--json` flag for scripting. Closes GAP-014.
- `ForgeRunner.run` accepts `stream=True` and `on_line=callback` for
  long-running sims. Default behavior unchanged (battle-tested blocking
  path). Closes GAP-008.
- New `propose_then_iterate()` in `iteration_loop` ties proposer +
  run_one_iteration into one call. The seam where the manual paste loop
  closes once `claude_propose` body is filled in.

### Tests (Tier 2 round)
- 183 → 218. New: `test_proposer` (15), `test_status` (13),
  `test_forge_runner` (7). All offline; LLM backends mocked or stubbed.

### Added (Tier 3 round — closing the iteration cycle)
- New `revert_to.py` module + `commander-revert` CLI. Restores a deck to a
  previous iteration's `deck_snapshot` blob and generates a Moxfield push
  blob ready for paste. Records the revert as its own iteration row by
  default. Closes GAP-017.
- New `edhrec_client.py` module. Fetches `edhrec.com/commanders/<slug>`
  pages and parses the embedded `__NEXT_DATA__` blob for top cards / high
  synergy / new cards / related commanders. 24-hour disk cache. Tolerant
  of EDHREC schema shifts. Closes GAP-009.
- New `game_changers.py` module. Fetches WotC's Game Changers list with
  HTML-list parsing + heuristic filtering, 7-day cache, fallback to bundled
  list on parse/network failure. Closes GAP-018.
- New `migrate_legacy_deck_ids()` in `knowledge_log`. Walks rows whose
  `deck_id` looks like a filename, looks up the publicId from the row's
  `deck_snapshot` `Moxfield=` line, updates. Dry-run mode supported.
  Closes GAP-024.
- `prompts/moxfield_audit_v3.md` Closing Summary now embeds a JS snippet
  using `URL.createObjectURL` to one-click download the
  `audit_manifest.json` with the right filename. Closes GAP-011.

### Tests (Tier 3 round)
- 218 → 260. New: `test_revert_to` (8), `test_edhrec_client` (13),
  `test_game_changers` (8), `test_compare_versions` integration tests (4),
  4 new `test_knowledge_log` tests for `migrate_legacy_deck_ids`. All
  offline; HTTP mocked, no external dependencies.

### Added (Tier 4 round — LLM bodies + ops)
- `analyst.claude_verdict` body wired with full anthropic SDK. Builds a
  system prompt describing the verdict taxonomy + JSON schema, calls
  `messages.create`, normalizes the response. Falls back to
  `NotImplementedError` cleanly without API key / SDK. Closes the
  remaining half of GAP-007.
- `analyst.ollama_verdict` body wired via `urllib` POST to
  `localhost:11434/api/generate` with `format: "json"`. Same fallback
  semantics on unreachable daemon.
- `proposer.claude_propose` body wired the same way, using
  `prompts/moxfield_audit_v3.md` as the system prompt. Strips markdown
  code fences from responses. Finishes GAP-005.
- `proposer.ollama_propose` body wired identically.
- New `doctor.py` module + `commander-doctor` CLI. 10 environment
  checks (Python, package, Forge, Java, decks dir, knowledge_log, two
  cache dirs, Anthropic key, Anthropic SDK, optional Ollama).
  GREEN/YELLOW/RED status with mapped exit codes. `--json` output.
  Verified GREEN on real env.
- `archetype._AGGRO_KEYWORDS` expanded with 14 tribal types + more
  aggressive keywords. Closes GAP-025.
- New `.github/workflows/test.yml` — matrix runs on Ubuntu+Windows,
  Python 3.10/3.11/3.12. Closes GAP-015.
- New `FUTURE_PLANS.md` — 5 parked architectural questions including the
  Forge replacement discussion (FP-001).

### Tests (Tier 4 round)
- 260 → 288. New `test_doctor` (13), 11 new tests across `test_analyst`
  and `test_proposer` for the LLM body success paths (mocked anthropic SDK
  via `types.ModuleType` injection, mocked `urlopen` for Ollama).
- Bug surfaced and fixed: existing `claude_verdict_is_unimplemented` test
  was leaking the dev-environment's stale `ANTHROPIC_API_KEY` and
  installed `anthropic` package, causing real API calls to leak through.
  Replaced with explicit `monkeypatch.delenv` and clearer module-injection
  pattern.

### Added (Tier 5 round — reporting + export + scope cuts)
- New `report.py` + `commander-history` CLI. Markdown rendering of a deck's
  full iteration lineage with per-iteration card-diff tables, win-rate
  trajectory line, verdict badges, and rationale + analyst notes.
  `--recent` mode gives a cross-deck summary table. Closes long-standing
  GAP-010 from PROJECT.md's Phase 1B component list.
- New `export.py` + `commander-export` CLI. JSON export/import of the
  knowledge log with full / per-deck / recent-N filtering. Skip-existing
  semantics on import so re-runs don't duplicate. Closes GAP-026.
- Personal-project scope decisions:
  - **GAP-022 / FP-005** (Moxfield API push) closed as WON'T-DO. The
    clipboard textarea workflow is the final design.
  - **GAP-023** (LICENSE) marked LOW-PRIORITY. `pyproject.toml` keeps
    `license = "TBD"` for personal use.
  - **CI simplified** to Windows + Python 3.12 only (drop multi-OS /
    multi-Python matrix that didn't match the actual dev environment).
  - **`CONTRIBUTING.md`** reframed as "session notes" rather than
    open-source contribution guide.

### Tests (Tier 5 round)
- 288 → 319. New: `test_report` (20), `test_export` (11). All offline.

### Added (Tier 6 round — improvement advisor + meta-reference benchmark)
- New `improvement_advisor.py` module + `commander-advise` CLI. Generates
  swap recommendations without needing a browser-Claude session. Pulls
  EDHREC inclusion-% / synergy data via `edhrec_client`, prior match
  history from `_matches/`, and synthesizes either a heuristic proposal
  (default) or a Claude-LLM-aided one (`--use-claude`). Output mirrors
  `audit_manifest.json` so it feeds `commander-iterate`. Closes GAP-027.
- New `meta_test.py` module + `commander-meta-test` CLI. Auto-fetches
  Moxfield top-likes deck + EDHREC "Average Deck" for a commander, imports
  both with `[REF]` prefix, runs `compare_versions` against each. Set-arith
  card diff identifies "must-add" (in all references, not user),
  "consider" (in some), "off-meta" (only in user). Closes GAP-028.
- New `moxfield_import.find_top_liked_deck_for_commander()` helper that
  uses the public read-API search endpoint (the same one `search_decks`
  already uses) with exact-name filtering.

### Tests (Tier 6 round)
- 319 → 355. New: `test_improvement_advisor` (18), `test_meta_test` (13),
  3 new `test_moxfield_import` cases for `find_top_liked_deck_for_commander`,
  2 new `test_compare_versions` integration tests for the runner-injection
  path. All offline; HTTP mocked.

### Fixed (Tier 7 round — meta-test bugs surfaced by live Hakbal run)
- `edhrec_client.fetch_average_deck()` — new function. EDHREC's "Average
  Deck" lives at `/average-decks/<slug>/<bracket>/<budget>`, not as a
  Moxfield link inside the commander page. Old logic was looking for
  `moxfield.com/decks/...` strings in EDHREC's `__NEXT_DATA__` blob; that
  data isn't there. New function constructs the canonical URL from
  bracket+budget, falls back to less-specific URLs if the most-specific
  404s. Closes GAP-029.
- `meta_test._fetch_edhrec_average_deck` rewired to use the new fetcher.
- `--reference-url` now smart-routes: EDHREC URLs go through
  `fetch_average_deck`, Moxfield URLs go through `fetch_deck` (existing).
- `find_top_liked_deck_for_commander` now uses two-step lookup
  (card-search → ID → deck-search by `commanderCardId`) instead of the
  unsupported `commanderName` query param. Old approach silently returned
  empty results.

### Added (Tier 7 round — suggestion quality)
- New `UNIVERSAL_STAPLES` frozenset in meta_test. Sol Ring, Arcane Signet,
  basic lands, Command Tower etc. are filtered from both must-add and
  off-meta because they're noise in either direction. The user's first
  meta-test run had Arcane Signet in off-meta (false signal); this fixes
  that class of bug.
- New `CardSuggestion` dataclass replacing flat `list[str]`. Each entry
  carries `in_n_references` / `total_references` / `role` so callers
  can rank by confidence and group by role.
- New `_classify_card_role()` heuristic: tags adds as
  finisher / lord / tutor / wipe / removal / counter / draw / ramp / other.
- `CardDiffReport.must_add_by_role()` groups suggestions in priority order
  (finisher first, since "deck can't close" is the common diagnosis).
- "All draws" framing: 0-0-N output now says "NEITHER deck could close —
  add a finisher", not "roughly even".

### Tests (Tier 7 round)
- 355 → 370. +5 new tests for universal-staples filter, frequency labels,
  role grouping. +5 new tests in `test_edhrec_client` for
  `fetch_average_deck`. +3 in `test_moxfield_import` for the two-step
  card-id lookup. +2 in `test_meta_test` for smart URL routing.

## [0.2.0] — 2026-04-26 (Phase 2 scaffolding)

### Added
- `prompts/moxfield_audit_v3.md` — Moxfield deck-audit prompt as the LLM
  proposer step, versioned in-repo
- `snapshot_deck.py` — pre/post-audit `.dck` versioning
- `compare_versions.py` — head-to-head A/B Forge sim with two modes (4-player
  same-pod default; 1v1 constructed)
- `scryfall_client.py` — disk-cached commander color identity lookups
- `knowledge_log.py` — SQLite iteration history (audit_manifest + sim_report
  + verdict + lineage chain via parent_id)
- `analyst.py` — verdict router with heuristic / Claude / Ollama backends
  (LLM backends stubbed pending API access)
- `iteration_loop.py` — orchestrator wiring compare → analyst → knowledge_log
- `moxfield_push.py` — clipboard-based "push to Moxfield" helper; `_api_push`
  stub for future authenticated API access
- `ml_dataset.py` — Phase 3 scaffolding: 25-feature schema, deck-level
  train/eval split, `dataset_summary()`
- `docs/audit_workflow.md` — end-to-end pipeline doc with Ollama design space
- `scripts/integration_test_b3.py` — full Phase 2 smoke against the 6 B3
  user decks

### Changed
- `log_parser.py`: added `Phase: Ai(N)-...` line tracking → real per-deck
  `confirm_action_by_deck` attribution; replaces the `/pod_size` even-split
  stopgap in `pool_curator`
- `pool_curator._read_color_identity` now calls `scryfall_client` instead of
  returning the `""` stub
- `pool_curator.curate_bracket` writes a second JSON
  (`_pools/B<n>_analysis.json`) with per-pod `MatchAnalysis`
- `pool_curator.preflight_candidate` rejects on timeout / non-zero exit /
  no-games-completed (was passing crashed sims through)
- `pool_curator.CuratedPool.to_dict` now persists computed properties
  (`win_rate`, `confirm_action_per_game`, `suspected_inflated`) that
  `asdict()` was silently dropping
- `moxfield_import._uniquify` raises after 99 collisions instead of silently
  overwriting
- `prompts/moxfield_audit_v3.md` Step 5.6 reframed as superseded-by-Forge for
  in-pipeline runs

### Fixed
- `log_parser._normalize` regex order (was `[B<n>]$` before `.dck$`, so the
  `$` anchor on the bracket regex never matched). Decorated names like
  `[USER] Foo [B3].dck` were leaving `[B3]` in the output, silently breaking
  match attribution everywhere downstream.

### Live runs
- B3 batch preflight: 6/6 pass
- B4 batch preflight: 6/6 pass (3 of 6 hit slow-match cutoff — useful real
  signal)
- Hakbal vs Hash 20-game smoke: passed end-to-end (18 of 20 games drew —
  exposed `analyst`'s "decks_drew_too_often" lesson)
- Integration test on the 6 B3 decks: full Phase 2 stack validated against
  real data

### Tests
- 81 → 144 (added `test_scryfall_client`, `test_knowledge_log`,
  `test_analyst`, `test_moxfield_push`, `test_ml_dataset`, plus active-player
  attribution cases in `test_log_parser`)

## [0.1.0] — 2026-04-26 (Phase 1B foundation)

Documented in `HANDOFF_2026-04-26.md`. Highlights:

- `forge_runner` (Forge headless harness)
- `log_parser` (sim stdout extraction)
- `game_analyzer` (per-game telemetry: turns, life curves, eliminations)
- `moxfield_import` (Moxfield → Forge `.dck` conversion + bulk-by-bracket
  harvest)
- `pool_curator` (tournament-style opponent meta selection)
- `run_match` (user deck vs pool with `MatchupReport`)
- 41 → 81 tests; suite under 1s

## [0.0.1] — 2026-04-26 (Phase 1A verifier)

Initial Forge verifier — surfaced the actual `sim` log format on Windows so
Phase 1B parser had a real schema to target. Documented authoritative parse
points (Match Result, Game Result) and the 4-player Game Outcome bug.
