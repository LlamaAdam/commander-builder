# Future plans — big bets, parked decisions, architectural questions

> Items here are **deliberately out of the active queue**. They're either too
> big to start without a clear go-decision, blocked on data we don't have,
> or strategic forks we want to keep visible without committing to. Move
> something out of here into `BACKLOG.md` when it's ready to actually ship.
>
> Each entry has: **What** (the proposal), **Why it might matter**, **Cost**
> (honest scope), **What would unblock it**, **My current take**.

---

## Project relationships (as of 2026-04-27)

- **`commander_builder` (this project)** is the canonical Forge-driven
  Commander deck-testing tool. Its scope is fixed and proven.
- **`forge_py`** is a **separate spike today**, but the user has stated
  intent (2026-04-27): _"when forge_py works well I do want it in
  commander_builder."_ Once forge_py's Phase 1 (turn-by-turn) produces
  useful signal that correlates with Forge sims, plan to fold the
  goldfish + turn-by-turn engine into this repo as a fast pre-filter
  before the JVM Forge runs. Until then, the two stay independent so
  forge_py can iterate without blast radius into this project.
- **`mtga_draft_helper`** (the MTG Arena draft + game advisor) is a
  separate project family. It already shares the `mtg_cards/` data
  folder for card-text reference but has no code-level dependency on
  this project. No merge planned.
- **`mtg_cards/`** at `C:\dev\mtg_cards\` is the shared card-data
  substrate consumed by all three project trees (out of repo).

---

## FP-001 — Replace Forge with a Python-native MTG engine

**What.** Stop calling out to Forge's headless `sim` JVM and instead build /
adopt a Python-native simulator for Commander games. Either a full
rules-engine + card database in Python, or a "rules-lite" abstraction that
captures only the metrics we care about for win-rate measurement.

**Why it might matter.**
- JVM startup is ~3–4s per sim; ~800MB RAM/process
- Forge's rule-based AI is functionally complete but plays decks badly
  (real signal: 18-of-20 draws when neither deck has a finisher)
- No `--seed` flag in Forge 2.0.12 — sim variance is unavoidable
- Forge's 4-player Game Outcome attribution bug we work around in
  `log_parser`
- Self-contained Python stack — no JRE in `vendor/`

**Cost (honest).**
- **Full rules engine + card DB**: 6–12+ months of focused engineering for
  one experienced developer. MTG's comprehensive rules are 200+ pages;
  layers, replacement effects, the stack, priority, modal cards, partner
  pairs, alternate costs, etc. Card DB alone is the bulk of the work —
  Forge has ~25,000 card implementations.
- **Rules-lite "goldfish" sim**: 1–2 weeks. Simulates only mulligan rate +
  early-game card sequencing + commander-turn arrival. Useful as a
  consistency-metrics supplement, not a replacement for the head-to-head
  signal we get today.
- **Forge AI replacement only** (Claude/Ollama at decision points, keep
  Forge's rule engine): 2–4 weeks. Addresses the "AI plays decks badly"
  pain without rewriting rules. Token cost: ~$0.10–$1.00 per game with
  caching. This is Phase 4 in `PROJECT.md`.

**What would unblock the full replacement.**
- A specific deal-breaking requirement: license problem with bundling Forge,
  deployment to a platform that can't run JVM, embedding into another
  product, multiplayer real-time use case
- Or: confidence that the project's actual goal has shifted from "test
  whether deck swaps improve win rate" to "build an MTG simulator"
- Or: a year of solo bandwidth to spend on it

**My current take (2026-04-26).**
- The wrapper layer we've built (`commander_builder`, 20 modules, 9 CLI
  entry points) **is** the streamlined Python interface. Users never touch
  Forge's GUI in the current pipeline.
- Highest-leverage near-term move toward "fewer draws / better sim signal"
  is **Option A from the discussion**: Claude/Ollama-piloted Forge AI,
  not a new engine. That's already in `PROJECT.md` as Phase 4.
- Replacing the rules engine spends a year detouring around a problem
  (AI quality) that has a 2–4 week direct fix.
- If the user's underlying motivation is curiosity / "I could build this",
  the rules-lite goldfish sim is a satisfying middle-ground — it's a real
  build but bounded scope.

**Status: PARKED.** Re-evaluate when one of the unblock conditions appears.

---

## FP-002 — Phase 3 ML training (the learned predictor)

**What.** Train a model that predicts swap outcomes (`kept` /
`reverted` / `neutral`) from deck features + swap features, replacing or
augmenting the LLM analyst. Already scaffolded in `ml_dataset.py` (25
features, deck-level train/eval split, no leakage).

**Why it might matter.** LLM analyst tokens cost money per call. A trained
model is free at inference time and could hit a useful precision/recall
trade-off at the noise band where the heuristic is uncertain.

**Cost.** Depends on data volume. Realistic minimum to attempt: 200+ logged
iterations across 5+ unique decks. Today: 1 row. Roughly 50+ deck audits
worth of empirical data.

**What would unblock it.** Volume. Just running the audit + iteration loop
on real decks for a few months. The ml_dataset module is ready to consume
that data the moment it exists.

**My current take.** Genuinely premature. Don't think about training until
the iteration log is at least 100 rows.

**Status: PARKED.** Triggered automatically when row count crosses
threshold — `commander-status` already reports it.

---

## FP-003 — Concurrent Forge sims for 2× pool curation throughput

**What.** Two JVMs running in parallel against separate `cwd`-isolated
Forge profiles. Halves curation wall time on multi-pod workloads.

**Cost.** Unknown — needs a 30-min spike testing whether two Forge
processes with separate `forge.profile.properties` directories actually run
without interfering on shared deck files / log files.

**What would unblock it.** A single afternoon of feasibility testing.

**My current take.** Worth doing if the user starts running multiple
back-to-back curations. Currently nobody runs curation enough to feel the
pain. `--max-candidates 12` already keeps individual curations under 40
minutes.

**Status: PARKED.** Cheap to attempt; just hasn't surfaced as a real
bottleneck yet.

---

## FP-004 — Forge sim seed for reproducibility

**What.** A `--seed` flag (or JVM agent that intercepts `Random` seeding)
so the same sim run produces the same outcome. Lets us A/B test specific
swaps under controlled conditions.

**Cost.** If Forge upstream adds `--seed`: trivial — pass it through. If
not: JVM agent work, brittle.

**What would unblock it.** Forge upstream releasing a seed flag, OR
sufficient pain from sim variance to justify the bytecode-instrumentation
cost.

**My current take.** Variance-via-game-count works fine for our scale (10
games × 2 pods averages out reasonably). Watch Forge release notes; not
worth building ourselves.

**Status: PARKED.** Watching upstream.

---

## FP-005 — Moxfield API push ❌ WON'T-DO (resolved 2026-04-26)

**Decision.** Personal-project scope decision: clipboard textarea workflow
is the final design, not a stepping stone. `_api_push` stays as
`NotImplementedError` permanently. No Moxfield write API exists, capturing
auth tokens is fragile, and the manual paste is one click.

**Closed in BACKLOG.md as GAP-022.** Kept here as a record of the rejected
alternative.

---

## FP-006 — Local web-app GUI

**What.** A browser-based UI replacing the CLI for the common workflow:

```
[radio: paste URL  |  paste decklist]   [text input]
[Run simulation] → progress feed
[Recommended cards added/removed] (rendered with Scryfall card images)
[Run again with these improvements] → loop
```

**Why it might matter.** The CLI works but each iteration is many keystrokes
across snapshot / import / compare / iterate / advise. A single-page app
collapses that into one form-and-button flow.

**Cost.** Two viable paths:
- **Path A (Tkinter)**: ~500 lines, no new deps. Looks dated but works
  cross-platform. Threading required to avoid blocking the UI during 30-min
  sims.
- **Path B (Flask + HTML, recommended)**: ~500 lines Python + ~300 HTML/JS.
  Adds Flask as a dep. Server-Sent Events stream `forge_runner.run(stream=True)`
  output cleanly. Card images via Scryfall's public CDN — much nicer reports.

**Status: PARKED 2026-04-26 per user request.** Re-evaluate once the
suggestion engine is meaningfully better than the current state. The user's
exact framing: "Shelve it for once the program seems to be more functional.
Especially with the deck suggestions."

**Gate progress (updated 2026-04-27, evening).** **All four "more functional
deck suggestions" gates now satisfied**:

- ✅ Universal-staples exclusion — `staples.UNIVERSAL_STAPLES_LC` shared
  across `improvement_advisor` and `meta_test`. Sol Ring no longer
  pollutes must-add lists.
- ✅ Reference-frequency labels — `staples.render_frequency_label()`
  produces "unanimous (5/5 refs)" / "majority (3/5 refs)" / etc.
  `meta_test` rendering uses them.
- ✅ Role categorization — `staples.classify_role(oracle_text, type_line)`
  tags adds with ramp/draw/removal/wipe/protection/tutor/finisher/threat.
  `improvement_advisor` groups adds by role tag in `evidence.role`.
- ✅ **Diagnosis-driven re-ranking** — `_signals_to_priority_roles()`
  maps weakness phrases ("high draw rate", "early aggression",
  "offense, not defense") to role priorities. The heuristic
  recommender re-ranks adds so the diagnosis steers which bucket
  surfaces first. AdviceReport rendering groups adds by role with a
  ★ marker on diagnosis-prioritized roles. 6 new tests added.

**FP-006 is now unblocked from the suggestion-quality side.** The
remaining gate is purely empirical: "the user has run a few real
iterations to validate the system shape." That needs real iteration
data accumulating in `knowledge_log.sqlite`, not engineering work.

When iteration data exists, revisit Path A (Tkinter, ~500 lines) vs
Path B (Flask + HTML, recommended; ~500 Python + ~300 HTML/JS).
Likely path forward: ship Path B as a single-deck workflow GUI first,
then expand into FP-007 by adding card-reference / rules-reference
panels.

---

## FP-007 — Unified MTG application (browser/desktop with cards + rules + tester)

**What.** A single program — eventually web or desktop — that consolidates:

- Deck testing (today: `commander_builder` + `forge_py`)
- Live card reference (oracle text + images, current per Scryfall)
- Rules reference (Magic Comprehensive Rules)
- Saved deck library + iteration history
- Game playback / replay viewer

The user's framing (2026-04-27): "the eventual plan is to have a program
for all of it and the rules and card images will be in that folder."

**Why it might matter.** Today the workflow is split across two CLI projects
plus manual Moxfield browser interaction. A unified app:
- Removes the "which CLI do I run?" cognitive load
- Lets card-reference panels render alongside deck-edit / sim-result panels
- Gives a single canonical knowledge_log + match history view
- Provides the surface for all the FP-006 GUI improvements

**Cost.** Significantly larger than FP-006 alone:
- FP-006 (web GUI for current commander_builder workflow): ~2 weeks
- FP-007 (above + card-reference + rules-reference + library views):
  ~6–10 weeks. Most of the additional cost is in the card / rules
  rendering UI and the data-model unification.

**What would unblock it.**
1. **Substrate exists** ✅ — `C:\dev\mtg_cards\` is the data root for the
   future app (rules, images, oracle snapshots all live there).
2. **Suggestion quality validated** — same gate as FP-006. No point
   building a UI around recommendations that don't help.
3. **A user committed to using it daily** — without that, the CLI is
   sufficient. Worth waiting until the iteration log has 50+ rows so
   the app has real data to render.

**My current take.** The shared `mtg_cards/` folder is the right
foundation; everything else is downstream of "do the CLIs produce
something worth wrapping." When that's clear (probably after FP-006
ships in either path), this becomes a roadmap, not just a daydream.

**Status: PARKED 2026-04-27.** Substrate ready; product readiness
gates not yet met. Likely path forward: ship FP-006 Path B (Flask)
first as a single-deck workflow GUI, then expand into FP-007 by
adding card-reference / rules-reference panels once that ships.

---

## FP-008 — Card-image lazy fetcher

**What.** When the future GUI needs card images, lazily fetch them from
Scryfall's image CDN and cache to `C:\dev\mtg_cards\images\normal\<scryfall_id>.jpg`.
Lazy because pre-downloading every Magic card would be ~25GB and most
decks reference <100 cards.

**Why.** The user's directive: "rules and card images will be in that
folder." `mtg_cards/images/` already exists empty for this. The fetcher
needs:
- Image URL extraction from the existing oracle snapshot
  (Scryfall returns `image_uris.normal` / `image_uris.small` etc.)
- On-demand fetch + write to the canonical path
- Stable filename keyed by scryfall_id (not card name) so reprints don't
  collide

**Cost.** ~3h. Mirror the existing `cards.refresh()` pattern. New module
`forge_py.images` or `commander_builder.card_images` (or a shared one
when consolidation comes).

**Important caveat (added 2026-04-27 per user observation).** Card
*images* show the originally-printed text, which can differ from the
*current legal Oracle text* after errata. For a system that
**interprets** card behavior (a future engine, a rules-aware analyzer),
images are unreliable — the oracle text from Scryfall is authoritative.
See **FP-009** for the oracle-text-first strategy, which moves ahead of
this work in priority.

**What would unblock it.** A consumer that needs images for *display*
(not interpretation) — for example, a "show me the card art on the
deck-detail page" feature in the eventual GUI. Until then, oracle text
covers the functional need.

**Status: DEFERRED 2026-04-27.** Lower priority than FP-009.

---

## FP-009 — Oracle-text-first card-reference store

**What.** Treat the existing `oracle_snapshots/` directory as the
canonical card-reference store, and build out a presentation layer
that renders oracle text (with cost, types, P/T) as primary —
images as secondary or optional.

**Why this matters (added 2026-04-27 per user observation).**

> "as the text of the cards is always changing try to reference what
> the card currently says... it might be better to do a card text file
> collection rather than actual images. Scryfall has the legal text of
> a card and that might actually differ from the image."

That's correct, and it's a meaningful architectural call. Magic's
*comprehensive rules* and *Oracle text* are the authoritative sources
of card behavior — printed images are a snapshot of how the card
looked when it was set-published. Errata happens. Examples:

- **Lightning Bolt** — printed text said "deals 3 damage to target
  creature or player"; current Oracle text uses "any target" to
  encompass planeswalkers.
- **Mind Twist** — has had errata to clarify random discard.
- **Banding / banding-related abilities** — heavily errata'd over
  decades.

So images are useful for *displaying the card aesthetically* but bad
for *interpreting card behavior*. A text-first reference store:

- Always renders the current legal text (refresh via `cards.refresh()`).
- Costs ~1KB per card vs ~150KB per image — full corpus is ~40MB vs
  ~25GB. We can ship the entire bulk dump on disk realistically.
- Drives any future card-text interpreter, rules-aware analyzer, or
  validator without relying on OCR / image processing.

**What's already in place.**

- `C:\dev\mtg_cards\oracle_snapshots\` — populated with ~32,000 per-card
  snapshots after the 2026-04-27 bulk re-prime.
- `C:\dev\mtg_cards\bulk_data\default_cards.json` — full bulk dump.
- `forge_py.cards.get(name)` — cached-with-freshness lookup.
- `forge_py.cards.refresh(name)` — force-fetch live oracle text.
- `forge_py.bulk_index` — in-memory index over all 32k+ cards.
- `commander_builder.scryfall_client.refresh_card(name)` — parity API.

**What's missing.**

- A presentation helper: `format_card_for_display(name)` returning
  a formatted block (name, mana cost, types, P/T, oracle text,
  flavor text optional).
- A card-by-card "compare to last refresh" diff so we can detect
  errata between snapshots and surface them to the user.
- Tooling to bulk-refresh stale snapshots in the corpus
  (`forge-py refresh-stale --max-age-days 30`).
- Documentation calling out that **oracle text is authoritative,
  images are decorative**, so future engineering decisions don't
  drift back toward image-based parsing.

**Cost.** ~4h for the presentation helper + diff + bulk-refresh CLI.
Tests with mocked Scryfall responses.

**What would unblock it.** Already unblocked — the substrate exists.
This is just incremental work on top of `forge_py.cards` and the
oracle_snapshots store. Reasonable to ship in the next session.

**My current take.** This is the right architectural shape for a
"unified MTG application" (FP-007). Doing this *before* the GUI work
ensures the GUI is built on the canonical reference rather than
having to retrofit later.

**Status: PARKED 2026-04-27.** High-priority within the
parked-items queue — promote to BACKLOG.md when GUI work starts.

---

## How this file relates to BACKLOG.md

- **`BACKLOG.md`**: prioritized, numbered, all items are go-able with a
  clear effort estimate. Things we plan to actually do.
- **`FUTURE_PLANS.md`** (this file): bigger bets, blocked items, strategic
  forks. Things we want to remember without committing.
- Items move OUT of `FUTURE_PLANS.md` and INTO `BACKLOG.md` when an
  unblock condition fires. Items move from `BACKLOG.md` to here when
  they turn out to be bigger than the original effort estimate.
