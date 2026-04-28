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

### Canonical UI design (provided 2026-04-27)

The user shared a polished mockup that should be treated as the
target UI shape, not a strawman. Reference image saved with this
session's notes; the surface elements decompose as follows.

**Page header**
- App brand "Commander Builder" + breadcrumb ("My decks / <deck name>")
- Right-side actions: `Export`, `Playtest ↗`

**Commander hero card** (top section)
- Stylized card preview with name, type line, and color-identity dots
  (W/U/B/R/G as colored circles).
- Theme tags: `Landfall`, `Counters` (multi-select pills, derived from
  archetype detection — see `archetype.py`).
- Right-side: "Deck progress" with `94/100` count + thin progress bar.

**Stat tiles** (4 across)
- Avg CMC (e.g. `2.84`)
- Lands count (e.g. `37`)
- Power level (e.g. `7/10`) — driven by EDHREC bracket / heuristic
- Est. price (e.g. `$284`) — sum of card prices from Scryfall

**Mana curve** (left half) — histogram with 0..6+ buckets, count under
each bar. Subtitle "Nonland spells" makes the scope explicit.

**Categories** (right half) — labelled colored progress bars with
counts:
- Ramp (green)
- Card draw (blue)
- Removal (red)
- Board wipes (orange)
- Land payoffs (purple) — archetype-specific category
- Win conditions (teal)

**Suggested adds** (bottom card)
- Section title: "Suggested adds (based on commander synergy)" + `More ↗`
- Each suggestion row:
  - Card name + one-line rationale
    (e.g. "Lotus Cobra — Mana on landfall — accelerates Omnath triggers")
  - Match% pill (98% green / 89% yellow / 85% amber)
  - Price ($)
  - `Add` button (mutates the deck; should be undoable)

**Visual style**
- Dark mode background (#0e0f12-ish), panels at #1a1c20-ish.
- Color identity dots use Scryfall-canonical colors (W ivory, U blue,
  B purple-charcoal, R orange-red, G mint).
- Small radii, generous spacing.

### Backend prerequisites for the UI

Most of these already exist; some need surfacing:

| UI element | Backend status |
|---|---|
| Avg CMC | Exists — `synergy.build_metrics` + `ratings.get_cmc` |
| Lands count | Exists — `card_db.get_type_line` |
| Power level | **Missing** — needs heuristic. EDHREC publishes "bracket"; could derive from `archetype` + Game Changers list. |
| Est. price | **Missing in surface** — Scryfall responses include `prices.usd`; need to project this field in `bulk_index` + `cards.py` and aggregate. |
| Mana curve histogram | Exists — `synergy.DeckMetrics.{two_drops,four_drops,…}` and `goldfish` reports |
| Categories (ramp / draw / removal / wipes) | Partial — `staples.classify_role` has these. **Missing:** "land_payoff" + "win_condition" roles. |
| Theme tags ("Landfall", "Counters") | Exists — `archetype.py` heuristic classifier |
| Match% on suggestions | Partial — `improvement_advisor` produces synergy-pct + inclusion-pct; need a single "match score" derived from those. |
| Suggestion rationale (one-line) | Exists — `SwapRecommendation.reason` |
| Add button (mutate deck) | **Missing** — needs a deck-mutation API + persistence. |
| Card-image preview | **Deferred** (FP-008) — Scryfall image CDN, lazy-fetched into `mtg_cards/images/`. |

### Implementation path (recommended)

Path B (Flask + HTML/JS) remains the right choice. Concrete plan:

1. **Backend prep** (~6h):
   - Surface `prices.usd` in `forge_py.cards.get()` + `bulk_index`
     projections.
   - Expand `staples.classify_role` taxonomy: add `land_payoff`,
     `win_condition`. Per-archetype category tables.
   - Power-level heuristic (deck CMC + game-changer count + speed
     archetype + bracket fitting).
   - Deck-mutation API in `iteration_loop` for "Add this card" /
     "Cut this card" without re-running a full audit.
2. **Flask scaffold** (~4h):
   - Single-page route serving the deck dashboard.
   - SSE endpoint for sim progress streaming.
   - JSON endpoints feeding each panel (curve, categories, suggestions).
3. **HTML/CSS** (~6h):
   - Dark theme matching the mockup.
   - Component library: card hero, stat tile, progress bar, suggestion
     row.
   - Animation: graceful enter/leave on suggestion list.
4. **Wire-up** (~4h):
   - State management (vanilla JS or Alpine.js — no React build step).
   - Optimistic UI for `Add` button with undo.

Total estimate: ~20h to ship a usable v1 matching the mockup.

### Original spec (kept for context)

A browser-based UI replacing the CLI for the common workflow:

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

## FP-010 — Package the web app as a desktop EXE

**What.** Once FP-006 (web GUI) is feature-complete and stable, ship
it as a single-file Windows executable that bundles:

- The Flask backend (Python interpreter + all deps + your `src/`)
- The HTML/CSS/JS front-end as embedded static assets
- A small native launcher that:
  1. Starts the Flask server on `127.0.0.1:<random_free_port>`
  2. Opens the system default browser to that URL, OR opens the
     embedded `pywebview` window directly inside the EXE
  3. Shuts the server down cleanly when the window closes
- A bundled `vendor/forge/` Forge install + JRE (or a first-run
  download step that fetches them and caches under `%LOCALAPPDATA%`)
- A reference to the shared `mtg_cards/` folder, configurable on
  first run (default: `%LOCALAPPDATA%\commander-builder\mtg_cards`)

**Why it might matter.** Right now the user-facing path requires a
Python install + `pip install -e ".[web]"` + a manual
`python -m commander_builder.web` invocation. An EXE collapses that
to "double-click `CommanderBuilder.exe`, browser pops open." That's
the difference between a project anyone can run and a project only
the maintainer runs.

**Cost.** ~12-20h depending on packaging route:

| Path | Tool | Cost | Pros | Cons |
|---|---|---|---|---|
| A | **PyInstaller** | ~12h | Mature, single-file output | Slow startup (extracts to temp); antivirus false positives |
| B | **PyInstaller + pywebview** | ~16h | Embedded Chromium-style window — no browser needed | Adds ~50MB to EXE size for the webview runtime |
| C | **Briefcase** (BeeWare) | ~16h | Real native installers (.msi) | Less mature; Flask integration is custom |
| D | **Nuitka** | ~20h | Faster startup than PyInstaller; harder to reverse-engineer | Build complexity; less common patterns |

Recommend **Path B (PyInstaller + pywebview)** — gives the user a
real desktop window that feels like an app, not "open this URL in
Chrome."

**What would unblock it.** All of:

1. ✅ FP-006 backend: Flask routes are live and stable
2. ✅ FP-006 minimal UI: at least the seven dashboard panels render
3. ⏳ FP-006 polish: the HTML/CSS feels like an app, not a debug page
4. ⏳ Workflow completeness: the user can drive a full audit cycle
   (snapshot → propose swap → run A/B sim → record verdict) without
   ever touching a CLI
5. ⏳ Forge bundling decision: ship Forge in the EXE (fat install,
   ~150MB) vs. first-run downloader (lean install, online required)

**What's required when packaging starts:**

- A `pyproject.toml` `[project.gui-scripts]` entry pointing at a new
  `commander_builder.launcher:main` that boots Flask + opens the
  webview
- A `commander_builder/launcher.py` that:
  - Picks a free port via `socket.bind(("", 0))`
  - Launches Flask in a thread
  - `webview.create_window("Commander Builder", url)` (Path B)
- Excludes `tests/`, `scripts/`, `docs/` from the bundle (already
  the case under setuptools `packages.find`)
- Code-signs the EXE before distribution to dodge antivirus FP
  (signing certificate ~$200/yr — defer until first user reports
  a SmartScreen warning)
- A simple GitHub Actions workflow `release.yml` that builds the
  EXE on a Windows runner and attaches it to a tagged release.

**Honest scope warnings.**

- A self-contained Python EXE is roughly 80-100MB before bundling
  Forge. With Forge + JRE the install is ~250MB. That's normal for
  this class of tool but worth setting expectations.
- The mtg_cards folder is large (~180MB after bulk_data + 32k
  oracle snapshots). Cannot reasonably ship inside the EXE; the
  first-run flow downloads it.
- Auto-update is out of scope for v1. Users redownload from
  GitHub Releases.

**My current take.** Right shape; wrong time. Don't start until
the web app demonstrably works for a full iteration cycle on real
decks. Premature packaging means re-packaging after every UX
change. Promote to BACKLOG.md when the user has run ≥5 full audit
cycles via the web GUI without touching a CLI.

**Status: PARKED 2026-04-28.** Re-evaluate after FP-006 polish
ships and the workflow is genuinely browser-only.

---

## FP-011 — BYO LLM token (per-user, never committed)

**What.** Once the project is shared with other users (locally or
via the FP-010 EXE), each user needs to bring their own LLM token
for the Claude / Ollama / OpenAI escalation paths. Today the system
reads `ANTHROPIC_API_KEY` from the process environment, which works
for a single dev box but fails the moment we ship a binary.

The plan:

- **First-run UI flow** in the web app: a "Settings" panel under
  the topbar that asks for the user's API key the first time the
  app boots. Stored to a per-user config file at:
  - **Windows:** `%LOCALAPPDATA%\commander-builder\config.json`
  - **macOS:** `~/Library/Application Support/commander-builder/config.json`
  - **Linux:** `$XDG_CONFIG_HOME/commander-builder/config.json`
  
  Same shape as the existing dev-mode `.env` keys
  (`ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL`, `OPENAI_API_KEY`).
- **Backend resolution order** stays consistent with today's:
  1. Process env var (still wins — useful for CI / dev)
  2. Per-user config file (the new path)
  3. None → endpoints that need an LLM degrade to heuristic
- **Server endpoint**: `GET/PUT /api/settings`
  - GET returns the current config keys with values **redacted**
    (`{"ANTHROPIC_API_KEY": "sk-ant-***...***last4"}`) so the UI
    can show "configured" / "missing" without exposing the secret.
  - PUT accepts a JSON body and writes to the config file with
    `0600` permissions (Windows: ACL restricted to current user).
- **Validation**: on save, ping the corresponding API with a
  zero-token sanity request; surface "401 invalid key" / "200 ok"
  to the user before persisting.
- **Forge_py mirror**: same path resolution in
  `forge_py.cards._scryfall_get` (only Scryfall today, but the
  shape stays consistent for future LLM-backed forge_py features).

**Critical safety constraints.**

- The committed `.gitignore` files in all three repos already cover
  `.env`, `.env.local`, and `config.json`. **Never** check in:
  - `.env` / `.env.local` / `.env.production` etc.
  - `config.json` containing real keys
  - SQLite files that might log API keys verbatim (none today,
    but worth scanning before any commit)
  - Test fixtures with real keys baked in (use `sk-ant-XXXXX`
    placeholders only)
- Pre-commit safety net (FP-011 implementation requirement): add a
  hook (`scripts/pre-commit-secrets-scan.sh`) that greps staged
  diffs for `sk-ant-`, `sk-`, `Bearer `, `xoxb-`, common token
  prefixes; aborts on hit. The hook should be optional (won't break
  for contributors who skip it) but documented in CONTRIBUTING.md.
- The web UI's GET /api/settings response **must** redact —
  responding with the literal key opens it to network capture even
  on localhost.
- The PUT endpoint **must not** log the body. Flask's default
  request logging is OK (path-only), but any custom audit logging
  added later needs explicit redaction.

**Cost.** ~6h:
- 1h: backend `commander_builder.user_config` module (read/write
  with permissions, redaction helper, resolution order)
- 1h: GET/PUT `/api/settings` endpoints + tests
- 2h: Settings UI panel (HTML/CSS/JS) + the
  validate-on-save round-trip
- 1h: Pre-commit hook + CONTRIBUTING.md docs
- 1h: Smoke-test on a fresh machine with no env vars set

**What would unblock it.** The project being shared with anyone
beyond the original developer. Right now there's exactly one user
with one key in their local `.env` — no urgency. Promote to
BACKLOG.md when:
1. FP-010 (EXE packaging) starts, OR
2. A second user actually wants to run the project, OR
3. The audit prompt becomes a routine workflow (currently
   uses heuristic-only because nobody's set ANTHROPIC_API_KEY).

**Status: PARKED 2026-04-28.** Architecture documented; not yet
needed.

---

## FP-012 — Autonomous deck improvement agent (the everything-bagel)

> ⚠ **Hardest plan in this document.** Combines almost every other
> FP into a single cohesive system. Months of work, multi-component,
> requires real iteration data accumulating first. Documented in
> detail because the pieces individually are tractable; only the
> assembly is hard.

**What.** A long-running agent that takes a Moxfield URL, learns
the deck's intent, and *autonomously* converges on a better version
without human intervention. The user types one URL and walks away;
hours later they get back a deck list with empirically-validated
improvements + a written rationale + a knowledge-log lineage that
shows the path from v1 to vN.

End-to-end loop, each iteration:

```
  ┌────────────────────────────────────────────────────────────────┐
  │                                                                │
  │  ┌─ Propose ─┐    ┌─ Validate ─┐    ┌─ Verdict ──┐    ┌─ Decide ┐
  │  │ candidate │ →  │ Forge sim  │ →  │ analyst    │ →  │  next   │
  │  │  swaps    │    │ + forge_py │    │ verdict +  │    │  swap   │
  │  │           │    │ pre-filter │    │ confidence │    │ batch   │
  │  └───────────┘    └────────────┘    └────────────┘    └─────────┘
  │       ↑                                                    │
  │       └──────────── learn from outcome ────────────────────┘
  │
  └─ stop when: (a) no candidate beats baseline by ≥4 wins/20,
                (b) iteration budget hit, or (c) human review tag
```

**Why it might matter.** Today the audit-iterate-validate cycle
takes ~30 minutes of attention per round (run audit → eyeball
suggestions → spin up A/B sim → wait → record verdict → repeat).
At 10 rounds per deck and 13+ user decks that's 60+ hours of
hands-on time. An autonomous agent collapses the loop to a single
"train me overnight" click and returns a deck the user can either
accept or reject without intermediate babysitting.

**Composed of (each is a sub-FP that's already partially built):**

| Component | Sub-FP | Status today | Gap |
|---|---|---|---|
| LLM proposer | FP-001 / audit prompt | Manual paste-into-Claude | Programmatic Claude API call |
| Forge validator | iteration_loop | Working | Concurrency (FP-003) for throughput |
| forge_py pre-filter | forge_py.rank | Working (r=0.898) | Gate Forge runs on r-score (skip clear losers) |
| ML verdict | FP-002 | Stubbed (data-blocked) | Train when 200+ iterations exist |
| Decision policy | New | Not built | Multi-arm bandit / Bayesian opt to choose next swap |
| Knowledge log | knowledge_log.py | Working | Add agent-run grouping field |
| Convergence detection | New | Not built | "no further beat-baseline swaps" stop rule |
| UI integration | FP-006 | Backend ready | "Train agent" button + live progress stream |

**Decision policy in detail.** The non-trivial new piece. Naive
audit produces 5-15 candidate swaps per round; testing each in
isolation (5 games × 2 decks per swap × 10 swaps = 100 Forge games)
costs ~30 minutes per round. The agent needs a *policy* to pick the
single most-informative swap to test next. Options:

1. **Greedy by predicted win-rate gain.** ML predictor (FP-002)
   ranks each candidate; test the top-1; record outcome; retrain
   the predictor; repeat. Simplest. Risk: stuck in local optima
   if the predictor's wrong about a high-value swap.
2. **Thompson sampling over swap categories.** Treat each role
   (ramp / draw / removal / threat) as an arm. Sample which role to
   improve next based on per-role posterior over win-rate gains.
   Better exploration. Maps cleanly to staples.classify_role.
3. **Genetic / hill-climbing.** Maintain a population of N candidate
   decks; mutate by swapping; cross-breed by sharing high-value
   cards. Most expressive but expensive — N×iterations Forge runs.
   Probably overkill until ML predictor is data-rich.

**Recommended path: start with (1), add (2) when 50+ iterations
exist per archetype, defer (3) indefinitely.**

**Convergence detection.**
- Stop when N consecutive swaps fail to beat baseline by ≥4 wins/20
  (margin threshold from analyst.py).
- Stop when overall Pearson correlation of predictor against actual
  outcomes drops below 0.5 (signal that the model needs retraining,
  not more swaps).
- Stop on manual `commander-iterate --halt` flag the user can drop
  in the working dir as a kill-switch.

**Cost (honest).** ~120 hours total, distributed across:
- 8h: Programmatic audit prompt via Claude API + JSON schema
  validation. Replaces the manual paste.
- 12h: Concurrency in forge_runner (FP-003 spike) — running 4
  Forge JVMs in parallel. Without this, a 10-iteration run takes
  5+ hours wall-time.
- 16h: forge_py pre-filter integration. Gate each candidate swap
  on a quick forge_py rank delta; only swaps that produce a
  ≥0.05 forge_py win-rate lift get the expensive Forge validation.
  Cuts Forge runs ~3x.
- 24h: Decision policy implementation + tests. Multi-arm bandit
  state lives in the knowledge_log so partial runs resume.
- 16h: Convergence detector + stop criteria + telemetry dashboard.
- 8h: Web UI integration. "Train this deck overnight" button on
  the dashboard → SSE progress stream → final deck + verdict.
- 12h: Phase 3 ML predictor (FP-002 unblocked) once 200+ rows
  accumulate. Train, validate, deploy.
- 24h: Real-deck pilot run + drift / failure-mode analysis.

**What would unblock it.**

1. **iteration_loop automation:** the manual proposer step (paste
   audit prompt into Claude, save JSON) becomes programmatic. This
   is the single biggest blocker — without it the loop has a human
   in the middle.
2. **knowledge_log has 200+ rows.** Phase 3 predictor needs data.
   Today: 1 row. Realistic timeline: 2-3 months of real iteration
   work before training is honest.
3. **Forge concurrency works** (FP-003). Without it, a single
   agent run takes overnight; with it, ~2 hours.
4. **A user committed to leaving a deck "in the oven."** The agent
   is only useful if the user is willing to hand off control for
   hours. If they want to babysit every swap, the manual workflow
   is sufficient.

**Honest scope warnings.**

- **The audit prompt isn't deterministic.** Claude returns
  different JSON manifests on different runs of the same deck.
  An agent needs to either (a) sample N runs and pick the modal
  swap set, or (b) fix temperature=0 + seed for reproducibility.
  Either way the manifest can drift between iterations on the same
  deck — which is fine for exploration but bad for convergence
  detection. Need a "manifest stability" metric.
- **Forge sim variance is real.** 5 games of Forge between the
  same two decks can differ by 1-2 wins purely from RNG. Margins
  ≤2 are noise. The agent needs to either run more games per
  candidate (expensive) or use forge_py's deterministic rank as
  a tiebreaker. Recommended: use forge_py r-score as primary
  signal, Forge as confirmation only on swaps where forge_py
  predicts ≥0.05 lift.
- **EDHREC bias.** The audit prompt's recommendations come from
  EDHREC consensus, which represents median play patterns, not
  optimal play. Agent runs that converge on the EDHREC consensus
  will produce decks that *look like the median*, not necessarily
  *win the most*. Need a counterweight — possibly per-card
  win-rate data from the knowledge_log itself, or empirical
  outperformance against bracket-matched fillers.
- **Cost ceiling.** Each Claude audit call is ~$0.05–0.20 in
  tokens. Each Forge sim is ~5 minutes wall-time per pod. A
  10-iteration agent run with 5 games per candidate costs ~$2 in
  API tokens and ~3 hours of compute. Multiply by 13 user decks =
  $26 + 40 hours of compute. Within reason; worth telegraphing.
- **Gameability.** If the agent's reward signal is "Forge win-rate
  vs filler pool," it can game the metric by stacking
  filler-counters rather than building a *good* deck. Mitigation:
  rotate filler pools per iteration so the agent can't overfit
  to a specific opponent.

**My current take.** This is the natural endpoint of the
project's design. Every component except the decision policy is
already built and working in some form. The 120-hour estimate is
honest *given* the prerequisites are met (FP-001 manual proposer
becomes API call, FP-002 has data, FP-003 concurrency works);
each prerequisite is a separate multi-week project on its own.
Realistic ETA: **6–9 months from today** if the user starts
running the manual loop weekly to accumulate iteration data, and
the FP-001/FP-002/FP-003 prereqs land in parallel.

**Status: PARKED 2026-04-28.** All prerequisites tracked
separately. Promote to BACKLOG.md when knowledge_log.sqlite has
≥150 iteration rows AND iteration_loop's proposer step is
automated AND forge_runner supports concurrent JVMs. Until then
this is the north star, not the next step.

---

## FP-013 — Project-tuned LLM (the moonshot)

> ⚠ **More speculative than FP-012.** Documented because it's the
> logical extension once FP-012 has accumulated enough data, but
> nothing about it is realistic in the next 12 months.

**What.** Fine-tune a small open-weights LLM on the project's
accumulated artifacts:

- Every audit manifest in knowledge_log + the sim outcome that
  followed
- Magic Comprehensive Rules (already in `mtg_cards/rules/`)
- 32k oracle snapshots (already in `mtg_cards/oracle_snapshots/`)
- All 13 user decks + their iteration histories
- EDHREC top-cards / synergy data per commander
- Bracket / Game Changers definitions

The output is an MTG-aware model that can:
- Run audits in <1 second on local hardware (vs 5-10s + $0.05 for
  Claude API)
- Run inside the FP-010 EXE without external API dependencies
  (no token leakage risk — closes FP-011 from the other side)
- Be retrained whenever the user adds 50+ new iterations, so it
  drifts toward the user's empirical preferences over time
- Generate verdicts (kept/reverted/neutral) against new sim data
  without hitting Claude

**Cost.** Depends entirely on which base model. Realistic options:

| Model | Params | Train cost | Inference | Quality |
|---|---|---|---|---|
| Llama 3.1 8B Instruct | 8B | $200 LoRA | RTX 3060 ok | ~70% Claude |
| Qwen 2.5 7B Coder | 7B | $150 LoRA | RTX 3060 ok | ~75% Claude |
| Phi-3.5-mini | 3.8B | $80 LoRA | CPU possible | ~50% Claude |
| Custom from scratch | — | $50k+ | A100 | unknown |

LoRA fine-tuning on a single A100 instance for ~12 hours costs
$80–$200 depending on rank/epochs. Doable; not weekend money but
not catastrophic either.

**What unblocks it.**

1. **2000+ iteration rows** in knowledge_log (current: 1). At ~5
   iterations per audit cycle and ~13 user decks, realistic
   accumulation is years, not months — unless the FP-012 agent
   amplifies the rate.
2. **Synthetic data pipeline.** Augment real iterations with
   synthetic ones generated by re-running historical audits with
   Claude at temperature=1 to produce manifest variants.
3. **Eval harness.** Need a held-out set of "known-good" audits
   to compare the fine-tuned model against Claude on identical
   inputs. Without this we can't tell if fine-tuning helped or
   hurt.

**Honest scope warning.** This is the kind of plan that sounds
great in roadmap form and crumbles when you actually try it.
Three failure modes:

- **Catastrophic forgetting.** Fine-tuning a small LLM on
  domain-specific data often degrades general reasoning. The
  result might know every Atraxa archetype but lose the ability
  to write a coherent rationale paragraph.
- **Data is too narrow.** 2000 rows of (manifest, outcome) pairs
  might not be enough for the model to learn the *causal*
  relationship between swap and outcome — it could just memorize
  manifest patterns and parrot them back.
- **Maintenance burden.** A locally-finetuned model needs
  retraining every time the meta shifts, the rules update, or
  Wizards releases a new set. That's a permanent operational cost.

**My current take.** Worth keeping on the roadmap as the logical
endpoint of FP-002 + FP-012. **Do not start until FP-012 is
producing data at >100 iterations/month for ≥6 months.** That
puts it at minimum 18 months out from today, more likely 24-30.
The right move *today* is to make sure the data we're collecting
in knowledge_log is shaped well enough for future training (clean
manifest schemas, complete sim_report blobs, parent_id chains
intact).

**Status: PARKED 2026-04-28, do not promote.** Notional plan
only; revisit when FP-012 has been live for 6+ months and
knowledge_log shows organic data accumulation.

---

## How this file relates to BACKLOG.md

- **`BACKLOG.md`**: prioritized, numbered, all items are go-able with a
  clear effort estimate. Things we plan to actually do.
- **`FUTURE_PLANS.md`** (this file): bigger bets, blocked items, strategic
  forks. Things we want to remember without committing.
- Items move OUT of `FUTURE_PLANS.md` and INTO `BACKLOG.md` when an
  unblock condition fires. Items move from `BACKLOG.md` to here when
  they turn out to be bigger than the original effort estimate.
