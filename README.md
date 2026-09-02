# Commander Builder

Closed-loop MTG Commander deck improvement. Forge headless simulation
empirically validates whether LLM-proposed swaps actually improve win rate;
a SQLite knowledge log accumulates iterations so future runs learn from
the past.

The primary use case: *"I have a Commander deck. Make it better, prove
it's better, and learn what kinds of changes actually move the needle so
future audits get smarter."*

It now also **assembles a first-cut deck from a commander**
(`commander-build`, FP-014) — EDHREC-seeded, given a real color-source
manabase, then empirically tuned via the improve loop. It is **not** a
from-atoms synthesizer: coherence is borrowed from EDHREC's community
aggregate and the improve loop does the tuning (see FP-014 in
[docs/future-plans.md](docs/future-plans.md)). It's not a Moxfield clone,
not a real-time game client. At its core it's still an iteration engine
where Forge provides ground-truth simulation and Claude acts as the
analyst that reads sim deltas and decides what to try next. A local
model (Ollama) can be enabled for narrow tagging work — deck archetype
and card roles, with the oracle text supplied — but it does not produce
proposals or verdicts; see `local_model.py`.

**What that ground truth is, exactly.** The opponents are bots. A `kept`
verdict certifies "this version wins more against Forge's AI" — an AI
that misplays whole card classes and, in our soak runs, grinds roughly a
quarter of games into turn-cap loops — not "this version is better at
your table". Politics, threat assessment, deal-making, and who the table
decides to kill on turn six are outside what the sim can measure at all,
and they decide plenty of real Commander games. It is a real measurement
of a real thing; it just isn't your pod.

**Source-of-truth docs:**
- [STATUS.md](docs/STATUS.md) — current state, open backlog, parked plans
- [CHANGELOG.md](docs/CHANGELOG.md) — what landed, in reverse chronological order
- [docs/architecture.md](docs/architecture.md) — module map, data flow,
  conventions, working principles

## Setup

```bash
git clone <this repo>
cd commander_builder
python -m pip install -e ".[dev]"
commander-init            # guided first-run setup (see below)
```

After the install, every CLI entry point works without `PYTHONPATH=src`.

### `commander-init` — guided first run

`commander-init` sequences the first-run pipeline in the order the steps
actually depend on each other, checking state before each one and
skipping whatever is already done:

1. **Dependencies** — Forge jar (~120 MB) + a Temurin JRE, via
   `bootstrap.check_dependencies` / `download_forge` / `ensure_jre`.
2. **Oracle card store** — one ~150 MB rate-limit-exempt bulk GET
   (`commander-oracle-refresh --from-bulk --everything`) instead of one
   Scryfall request per card.
3. **Decks** — harvest ~60 community decks at your bracket, pull
   `[PREMADE]` popularity decks, or skip and import your own later.
4. **Opponent pool** — `commander-curate`, after an explicit cost
   warning: measured ~35 min (B3) / ~55 min (B5) of JVM time.

```bash
commander-init --dry-run              # print the plan + current state, run nothing
commander-init --bracket 3            # interactive; asks before anything expensive
commander-init --yes --decks harvest  # unattended (authorizes the downloads AND the curation)
```

It is **resumable and stateless**: each step probes the artifact it
produces (jar on disk, snapshot count, candidate `.dck` files,
`_pools/B<n>.json`), so re-running picks up where you stopped. No new
state file to go stale.

Prefer to do it by hand? Every step is just the standalone command it
prints — `commander-init` adds ordering, not logic.

For live Forge sims, drop a portable Forge release + JRE into
`vendor/forge/` and `vendor/jre/` (see `setup/forge/README.md`), or let
step 1 above fetch them. The system runs without Forge — only modules
that hit the JVM (`forge_runner`, `pool_curator`, `run_match`,
`compare_versions`, `iteration_loop`) need it.

For live LLM analyst, configure `ANTHROPIC_API_KEY` via one of:

- `commander-config init` → edit `~/.commander-builder/credentials`
  (the credentials file lives **outside the repo** so it can never be
  committed by accident). See [docs/SECRETS.md](docs/SECRETS.md).
- Or set the env var directly in your shell (overrides the file).
- Or provide a key through the web UI's BYO-key flow (per-request,
  never persisted server-side).

## Run the web app

```bash
python -m commander_builder.web
# → http://127.0.0.1:5000
```

Sidebar deck list + dashboard with hero / stat tiles / mana curve /
categories / suggested adds. Propose-swap drives A/B sims through the
parallel-pod harness; "Save iteration" persists to
`knowledge_log.sqlite`. The Claude analyst is opt-in per request via the
LLM toggle row. A **"Build from scratch"** tab assembles a first-cut deck
from a commander + bracket (FP-014) — it kicks off an async build job
(`POST /api/build_deck` → poll `GET /api/build_job/<id>`) and drops the
finished legal-99 into the deck list, ready to improve.

The dashboard and audit also surface (ManaFoundry-parity additions):

- **Cheaper-printing savings** — the Est. price tile lists cards where a
  legal cheaper printing of the same card saves money ("Save up to $X").
- **Estimated bracket** — an explainable 1–5 bracket estimate with the
  reasons behind it, flagged when it disagrees with the declared bracket.
- **Health grade** — the deck-health signals compressed into one A–F
  letter grade with the top reasons points were lost.
- **Lift picks** — "pairs well with your deck" candidate adds from
  co-occurrence analysis over the harvested corpus.
- **MTGA / CSV paste import** — the paste-import textarea now auto-detects
  MTG Arena exports and CSV card lists in addition to `.dck` / Moxfield.
- **Replays** (FP-016) — set `COMMANDER_BUILDER_KEEP_GAME_LOGS=1` (or pass
  `--keep-logs` to `run_match` / `compare_versions`) to persist each game's
  Forge log (capped ~500MB, oldest-run eviction), then browse turn-by-turn
  replays — life totals, eliminations, winner — under the Replays rail section.

## CLI commands

### `commander` — one front door

Every command below is also reachable as a subcommand of `commander`,
grouped by task area. It is an alias layer, not a migration: the
hyphenated scripts all still work, and `commander improve ...` runs the
identical code with identical flags and exit codes as
`commander-improve ...`.

```bash
commander                 # the grouped menu: build / import / sim / analyze / web / maintenance
commander improve --help  # the target command's own --help
commander init            # guided first-run setup
```

Use it when you can't remember which of ~30 hyphenated names does the
thing you want; use the hyphenated scripts when you can (they're shorter,
and shell completion already knows them).

### The commands themselves

```bash
# Build a first-cut deck from scratch: commander + target bracket → a legal
# exactly-99 (EDHREC-seeded, color-source manabase, then personalized).
# --improve N hands the assembled deck straight to the empirical improve loop.
commander-build --commander "Krenko, Mob Boss" --bracket 3
# --collection PATH biases fill toward owned cards; --no-lift / --no-steer
# toggle personalization stages; --improve 3 runs 3 improve rounds after build.

# Import a Moxfield deck as your baseline
commander-import --user https://moxfield.com/decks/<id>

# Snapshot a version (frozen baseline)
commander-snapshot "[USER] My Deck [B3].dck" --version v1

# Heuristic/Claude swap recommendations (no browser session needed)
commander-advise --user "[USER] My Deck v1 [B3].dck" --bracket 3
# --show-lift prints the deck's strongest in-deck card pairs + top
# lift-scored candidate adds from the harvested corpus. --collection PATH
# + --owned-only filter recs to cards you own (also on commander-auto-curate;
# register your collection at ~/.commander-builder/collection.txt, plain or CSV).

# End-to-end auto-curate: advisor -> Claude curator -> apply -> optional
# A/B sim with empirical kept/reverted/neutral verdict written back to
# the knowledge_log. ~$0.20-$0.50 in Anthropic + ~5-15 min Forge per run.
commander-auto-curate "[USER] My Deck [B3].dck" --bracket 3 --run-sim
# Polish (default, 5+5 swaps), overhaul (15+15), free (unbounded),
# rebuild (30+30 + optional Karsten manabase rebuild), or auto — the
# adaptive change budget: the deck's 0-100 health score picks the tier
# (>=75 keep, 55-74 polish, 35-54 overhaul, <35 rebuild). Opt-in on
# purpose (bigger budgets multiply curator + Forge cost). The score
# only sizes the budget; the A/B verdict still decides what stays.
# --mode auto also works on commander-advise and commander-improve.
commander-auto-curate "[USER] My Deck [B3].dck" --bracket 3 --mode overhaul
commander-auto-curate "[USER] My Deck [B3].dck" --bracket 3 --mode auto

# The unattended improve loop: N auto-curate rounds, advancing the base
# deck only on a REPLICATED 'kept' A/B verdict. Read "The improve loop is
# a screen, not a background improver" below before running it overnight.
commander-improve --deck <publicId> --rounds 10
commander-improve "[USER] My Deck [B3].dck" --rounds 5 --no-replicate
# --strategy bandit explores individual swaps as bandit arms instead.
commander-improve --deck <publicId> --rounds 20 --strategy bandit
# FP-013 gate progress (no deck, no rounds needed)
commander-improve --health

# Old-vs-new head-to-head A/B sim
commander-compare \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --games 10 --filler-pairs 2

# Wrap as one iteration with verdict + persistence
commander-iterate \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --manifest audit_manifest.json

# Bulk-harvest decks at a bracket for the curator's candidate pool
commander-import --harvest 3      # ~60 B3 decks via the multi-axis recipe

# Pull popular community builds as [PREMADE] decks: Moxfield's top decks by
# likes (Likes= recorded) + EDHREC average decks for the top commanders
# (Salt= recorded). Skips commanders already represented on disk; listed in
# the web UI as type "premade"; never used as sim opponents/filler.
commander-import --premade                                # 10 + 10
commander-import --premade-moxfield 20 --premade-edhrec 20  # per-source counts

# Curate the canonical opponent pool from candidates on disk
commander-curate --bracket 3 --max-candidates 12 --seed 0

# Run a user deck against the curated pool
commander-match --user "[USER] My Deck [B3].dck" --bracket 3 --games 5 --pods 3

# Push a local .dck back to Moxfield via clipboard
commander-push "[USER] My Deck v2 [B3].dck"

# Compare your deck to consensus meta-references at a bracket
commander-meta-test "[USER] My Deck [B3].dck" --bracket 3

# Import cEDH tournament results from edhtop16 (FP-017): top commanders by
# conversion rate, or one commander's winning decklists + per-card presence.
# BRACKET-5 data only — exploratory source, NOT a validated predictor.
commander-tournament                                  # top commanders
commander-tournament "Kinnan, Bonder Prodigy" -n 20   # lists + card presence

# Inspect or revert any historical iteration (revert backs up the live
# deck first and prints the backup path)
commander-history --deck-id <publicId>
commander-revert --to-deck <publicId> --version 3

# Health-check Forge install + caches
commander-doctor

# Status snapshot for cold pickup
commander-status

# Full menu of everything installed, grouped by task area
commander
```

## The improve loop is a screen, not a background improver

`commander-improve` runs unattended, and on the unattended path a first
`kept` verdict does not advance the deck on its own: a second
independent A/B over the same old-vs-new pairing has to say `kept` too
(`--replicate`, default ON for the round loop, OFF for `--strategy
bandit`). That gate works, and it costs. Both halves of the trade, in
the same place, because quoting only the first one sells this as
something it isn't:

- **False positives.** A truly neutral swap clears one significance test
  about 1 run in 40 at α = 0.05; two independent runs in the same
  direction cut the per-advance false-positive rate to **~1 in 1,600**.
  That matters because the loop *chains* — an unconfirmed lucky split
  becomes the base every later round is measured against, so the error
  compounds rather than merely being recorded.
- **True positives.** At the shipped settings (45 pod games, a
  20-decisive gate, an exact two-sided binomial at α = 0.05) a genuinely
  good **+5pp** swap advances with probability **0.13% per round** —
  about **1.3% over a 10-round overnight run**. The likelihood ratio of
  an advance rises from 3.0 single-shot to **9.2** replicated.

So **"nothing advanced" is the EXPECTED outcome of an overnight run**,
including when the curator is proposing genuinely good swaps. That is
the screen behaving correctly, not a failure and not a stall. When
something *does* advance, treat it as a rare lead worth investigating
rather than a proven improvement: at LR ≈ 9.2, advances only become
majority-true once the curator's true-hit rate clears ~10%, and FP-002
measured curation net-neutral over 37,120 games.

Buying more power by raising `--sim-games` was considered and declined —
honest power costs real hours per round, and the Forge sim is positioned
as a deep-dive instrument for questions worth real game counts, not a
per-swap arbiter. If you want a swap decided, spend the games on that
one question deliberately.

**Every cycle that changes a deck is one row in
`knowledge_log.sqlite`.** Round loops (`--strategy greedy`) write a row
per round whatever the verdict, via the auto-curate pipeline.
`--strategy bandit` writes a row per **accepted** pull — a pull is an
"iteration" exactly when it advances the deck, so measured-but-rejected
pulls stay in the run's CLI/JSON output and out of the log. Every row
carries its audit manifest, deck snapshot, sim report and parent link,
which is what makes `commander-history` / `commander-revert` work.

## The audit cycle (manual workflow)

The full closed-loop iteration cycle when you want maximum control. See
[docs/architecture.md](docs/architecture.md) for the data-flow diagram.

```bash
# 1. Import a Moxfield deck as your "version 1" baseline
commander-import --user https://moxfield.com/decks/<id>

# 2. Snapshot v1 (frozen baseline)
commander-snapshot "[USER] My Deck [B3].dck" --version v1

# 3. Either:
#    (a) Run the web app's audit flow, OR
#    (b) Open a Claude session, paste prompts/moxfield_audit_v3.md.
#        The audit modifies your Moxfield deck and emits audit_manifest.json.

# 4. Re-pull the post-audit deck (same Moxfield id → overwrites the local
#    file in place; local Protect= lines are preserved)
commander-import --user https://moxfield.com/decks/<id>

# 5. Snapshot v2 and run head-to-head A/B (see commands above)
```

The web app collapses steps 3–5 into a single propose-swap flow.

## Project layout

```
src/commander_builder/   ~30 production modules; key subsystems split:
  improvement_advisor.py  orchestrator (advise + _advise_steps generator)
  _advisor_*.py          7 sub-modules: models, heuristic, bracket_peers,
                         claude, manabase, filters, role_helpers
  web/
    app.py               Flask orchestrator (registers 5 blueprints)
    _helpers.py          pure functions (deck format, evidence scoring)
    routes_audit.py      /api/audit + /api/audit/stream (SSE) + /api/advise
    routes_sim.py        /api/propose_swap + iteration CRUD
    routes_decks.py      deck text/source/import + game_changers + deck_audit
    routes_dashboard.py  /api/dashboard + pricing + verdict breakdown
    routes_meta.py       root + health + forge_version + log_error
tests/                   1,700+ unit tests, all offline (~90s)
scripts/                 integration tests + batch runners (hit Forge)
prompts/                 versioned LLM workflow prompts
docs/                    architecture, current handoff, sprint specs
vendor/                  Forge install + JRE (gitignored)
```

Companion repo at `C:\dev\forge_py\` — Python-native simulator that
emits Forge-compatible stdout. Used as a fast pre-filter for ranking
decks. Optional correlation harness in `forge_py_correlation.py` runs
both engines side-by-side; opt in via
`COMMANDER_BUILDER_CORRELATE_FORGE_PY=1`.

Shared card data at `C:\dev\mtg_cards\` (out-of-repo, ~180MB Scryfall
bulk + per-card snapshots + Magic Comp Rules). Both projects read via
`MTG_CARDS_DIR` env var with a sensible default. On machines without
that folder, snapshots fall back to the repo-local `.cache/scryfall/`
(a stderr line says so at startup). To populate a cold snapshot store,
prefer the bulk path over per-card fetching:

```
# Snapshot every card named in the configured deck dir from Scryfall's
# oracle_cards bulk export (one rate-limit-exempt ~150MB GET):
python -m commander_builder.oracle_store --from-bulk --all
# or for one deck: ... --from-bulk --deck "path\to\deck.dck"
```

## Where to start when picking this up cold

1. `docs/STATUS.md` — current state, open backlog, parked plans
2. `docs/architecture.md` — how the pieces fit
3. `python -m pytest tests/` — confirm the suite is green
4. `git log --oneline -10` — what landed most recently

Use `python -m pytest --run-slow` to include the slower regression tests.
Live-service checks are excluded even when a provider CLI is installed;
they require explicit `--run-live --run-slow`, their optional dependencies
(such as `[claude]`), and a configured provider. Enabling them can consume
subscription/API usage.

Then either pick an item from STATUS's open backlog or jump into the
web app and run a propose-swap end-to-end on one of your decks.

## Working principles

These are how the project expects sessions to operate. They live in full
in [docs/architecture.md](docs/architecture.md#working-principles); the
short version:

1. **Verify before assuming.** Wrong assumptions wrapped in try/except
   rot quietly.
2. **Honest pushback over compliant building.** Say so if the spec
   doesn't make sense.
3. **Small, validated steps.** Each component built, tested, integrated
   before the next.
4. **Modularity over cleverness.** Clean interfaces > clever
   inheritance; backends swap at known seams.
5. **No silent failures.** Forge can fail many ways; surface errors
   loudly with actionable messages.
6. **Log everything that could become training data.** Phase 3 ML wants
   structured logs from day one.

## License

MIT — see [LICENSE](LICENSE). Free to use, modify, and distribute with
attribution.
