# Future plans (consolidated)

> Consolidated 2026-05-26 from the per-FP plan docs. **STATUS.md -> Parked
> plans is the authoritative status**; this file collects the detailed
> findings/plans (FP-002 margin analysis, FP-002 deck-gen, FP-007, FP-010)
> in one place.

---

# FP-002 (reframed) — curator margin regression

**Status: REOPENED → first result in (2026-05-26).** The original FP-002 was a
*kept-vs-reverted classifier*. It was concluded NOT VIABLE on 2026-05-22 for a
specific reason: after the A/B seat-attribution fix (`e8777b6`), the curator's
swaps almost never made a deck strictly *worse*, so there was **no negative
class** to learn. STATUS.md proposed the unblock itself: *"regress on improvement
margin, not more sim hours."*

The accumulated **40-game** A/B soak rows reopen it under exactly that framing.
Two things changed:

1. **The negative-class blocker is gone.** Across high-confidence (≥40-game)
   pairs we now see both winners and losers among the curated decks
   (**kept=6, reverted=4, neutral=19** of 29 decks). Curation *can* hurt — it
   just usually doesn't.
2. We can regress a **signed, continuous target** (win-rate margin) on
   **pre-sim features of the original deck** — the honest predictive substrate
   (no sim outcome leaks in; we ask *"from the deck alone, can we tell whether
   curation will help it?"*).

## Tooling

- `scripts/margin_analysis.py` — pure-stdlib (numpy/sklearn/scipy are **not**
  installed on the soak boxes). Two designs:
  - `--mode ab` (default): aggregates `*throughput*.jsonl`, margin =
    `(wins_b - wins_a) / decisive` (v1-vs-v2 *in the same pod*).
  - `--mode gauntlet`: aggregates `*gauntlet*.jsonl`, margin =
    `winrate(v2) - winrate(base)` where base and v2 *each* play the **same
    fixed 3-deck gauntlet** — no head-to-head pod confound (the cleaner test).
  - Both join each deck to its original `.dck` for `deck_health` features and
    report per-feature Pearson `r` + a two-sided t-stat (df = n−2).
  - `python scripts/margin_analysis.py [--mode gauntlet] --min-games 40`
    (text) or `--json`; `--decks DIR` (repeatable) overrides the search path.
- `tests/test_margin_analysis.py` — 18 pure-logic tests (A/B + gauntlet
  aggregation, margin banding, Pearson edge cases, the deck-file join,
  end-to-end `analyze`).

## Result (min_games=40, n=29 decks, 11,960 games)

```
mean curator margin: +0.0009   (per-deck win-rate delta; >0 = curation helps)
per-deck verdicts:   kept=6  reverted=4  neutral=19

feature -> margin correlation (|r| desc):
  wincon_protection    r=+0.447  t= 2.60  *   <- only feature past |t|>=2 (~p<.05)
  mana_sinks           r=-0.328  t=-1.80
  deficit_total        r=-0.303  t=-1.65
  spell_density        r=+0.292  t= 1.59
  under_built_roles    r=-0.282  t=-1.53
  basic_lands          r=-0.259  t=-1.40
  bracket              r=+0.256  t= 1.37
  main_count           r=+0.093  t= 0.49
  self_mill            r=+0.066  t= 0.34
  mdfc                 r=-0.063  t=-0.33
```

## Reading

- **Curation is empirically ~neutral.** Mean margin is +0.0009 and 19 of 29
  decks land in the ±0.05 neutral band. On the population of decks we've curated,
  the v2 is a coin-flip against the original. (This corroborates the earlier
  ad-hoc finding: all-rows mean ≈ −0.009.) A blanket "always curate" policy is
  **not** supported by the sim data.
- **One robust signal:** decks that *already* carry more **wincon-protection**
  benefit more from curation (`r=+0.45`, the only feature past the significance
  flag). Intuition: when the deck can already protect its win, the curator's
  consistency/interaction tweaks convert into wins rather than being wasted on a
  deck that loses the wincon anyway.
- **Weaker, sub-threshold hints** (don't over-read at n=29): curation helps
  decks with *fewer* mana sinks and *smaller* role deficits (negative `r` on
  `mana_sinks`, `deficit_total`, `under_built_roles`) — i.e. it adds the most to
  decks that are already coherent, and adds little to decks with large structural
  holes. This is the opposite of the "curation rescues weak decks" hypothesis.

## Cross-validation: the gauntlet design (min_games=40, n=26 decks, 5,760 games)

The A/B design has a confound: base and v2 play *in the same pod*, so they take
wins directly off each other and share two filler opponents. The **gauntlet**
soak removes it — base and v2 *each* play the same fixed 3-deck gauntlet
independently, so their win-rates are measured against identical opposition.
Running the same regression there:

```
mean curator margin: -0.0108   (winrate(v2) - winrate(base) vs fixed gauntlet)
per-deck verdicts:   kept=5  reverted=4  neutral=17

feature -> margin correlation (|r| desc):
  deficit_total        r=-0.359  t=-1.88     <- closest to significance
  mana_sinks           r=+0.332  t=+1.72     (sign FLIPS vs A/B -> noise)
  self_mill            r=-0.280  t=-1.43
  under_built_roles    r=-0.261  t=-1.32
  ...
  wincon_protection    r=+0.223  t=+1.12     (A/B's "significant" feature: NOT replicated)
```

**What the cleaner design tells us:**

1. **"Curation is ~neutral" is robust.** Two independent experimental designs
   agree: mean margin +0.0009 (A/B) and −0.0108 (gauntlet), both ≈ 0, with the
   large majority of decks (19/29 and 17/26) in the neutral band. This is the
   finding to trust.
2. **The A/B `wincon_protection` result was a confound artifact.** It dropped
   from r=+0.45 (t=2.6, "significant") to r=+0.22 (t=1.1, not significant) once
   the head-to-head pod confound is removed. **Do not build on it.** This is
   exactly why the cleaner design was worth running.
3. **The one directionally-consistent signal** across both designs is
   `deficit_total` / `under_built_roles` — *negative* in A/B (−0.30 / −0.28)
   and in gauntlet (−0.36 / −0.26). Curation adds the **least** to decks with
   large structural role deficits. Neither crosses significance alone, but the
   agreement across designs makes it the most credible (weak) lever.

## Verdict & next step

The reframing **works as an analysis** and the negative-class obstacle is
resolved. But **no feature survives cross-validation at significance** — the
one A/B winner (`wincon_protection`) failed to replicate in the unconfounded
gauntlet design — and **n is too thin** (29 / 26 decks). This is exploratory
evidence, not a shippable model.

To graduate from "analysis" to "predictor":

- **More unique decks**, not more games per deck — the unit of analysis is the
  deck. ~30 → ~80+ decks would let the directionally-consistent `deficit_total`
  signal be validated out-of-sample without sklearn.
- Run *both* designs and only trust features that agree across them — the
  gauntlet/A/B disagreement on `wincon_protection` shows single-design "hits"
  are unreliable here.
- Then optionally a tiny pure-stdlib regression on the features that survive
  cross-validation. sklearn is still unnecessary at this scale.

The actionable takeaway *today* (no model needed): **the curator's expected
improvement is ~0** (confirmed by two independent designs). The only credible
(if weak, cross-validated) lever is **structural deficit**: curation adds the
least to decks with big role-target shortfalls. That argues for using the
deck-health `under_built` signal (F2) to **fix structure first, then curate** —
and for not assuming curation is a free win on an already-coherent deck.

---

# FP-002 deck-generation plan — toward a real margin predictor

**Goal (your call, 2026-05-26):** grow the soak deck set from ~13 unique
commanders to **~80+ unique decks**, so the margin regression in
`scripts/margin_analysis.py` has enough rows (the unit of analysis is the
*deck*, not the game) to attempt an out-of-sample predictor on the one
cross-validated signal (`deficit_total` / `under_built_roles`).

## Why this is a campaign, not a step

- 80 decks × **40-game** gauntlet (operator directive: never 5-game) × 2 roles
  (base + v2) ≈ **6,400 games**. At ~40s/game that's ~70h single-runner, or
  **~12–18h** on the autoscaling `soak_pool` (the box1 Ryzen 3900X did ~200
  games/hr in prior soaks). It is a soak you launch deliberately, like the
  prior gauntlet runs — not a single command that returns.
- The acquisition + curation phase (below) is network/Claude-CLI bound and
  unattended-able, but still ~1–2h for 30+ commanders.

## Pipeline

**Phase 1 — acquire base decks (network-bound, safe to run anytime)**
1. Pick ~30 more commanders spanning brackets B3/B4/B5 and color identities
   (diversity matters more than raw count — avoid 30 mono-red goblin decks).
   Source options, in preference order:
   - EDHREC average deck per commander (`edhrec_client.fetch_average_deck`) —
     coherent, no Moxfield dependency.
   - Top-liked Moxfield build (`moxfield_import.find_top_liked_deck_for_commander`)
     — what `scripts/pull_popular_decks.py` already does for existing decks.
2. Write each as a `[USER] <Name> [B<n>].dck` into the shared inbox so
   `soak_pool` discovers it. Keep names unique.

**Phase 2 — curate a v2 per deck (Claude-CLI bound, unattended)**
- `commander-auto-curate <base>.dck` (subscription CLI path; scrubs API keys)
  writes the curated `... v2 ...dck`. `soak_pool` pairs base + ` v2 ` by name.
- Resumable: skip any base that already has a v2.

**Phase 3 — soak (the long Forge run; launch deliberately)**
- Gauntlet mode, 40 games, the unconfounded design margin_analysis prefers:
  ```
  python scripts/soak_pool.py --mode gauntlet --games 40 --append \
      --label Llama --out C:/Users/pilot/soak_inbox/Llama_gauntlet.jsonl
  ```
- **Machine-identity invariant:** box1 is `Llama` — use `--label Llama` and the
  `Llama_*` output; never a box2b label on box1.
- Run in short blocks during dev; bump to 24h when stable (per memory).

**Phase 4 — analyze**
- `python scripts/margin_analysis.py --mode gauntlet --min-games 40`
  now reports over ~80 decks. With n≈80, validate the `deficit_total`
  single-feature OLS out-of-sample (pure stdlib; sklearn still unneeded).

## What to build (small, optional)

A driver `scripts/build_fp002_deckset.py` that automates Phase 1+2 from a
commander list: fetch base deck → write `.dck` → `commander-auto-curate` →
emit progress, resumable on the count of paired decks. ~1 file; the per-step
calls already exist (`fetch_average_deck`, `find_top_liked_deck_for_commander`,
`auto_curate_main`). Left unbuilt pending go-ahead because the commander LIST
is a curation choice (which 30 commanders) better made with you.

## Status / honesty

- Acquisition + curation: ready to run with existing tooling.
- The soak is a multi-hour Forge campaign — best launched when box1 is free
  (you stop soaks when actively editing the program), not unattended mid-dev.
- Caveat from the completed analysis: curation is ~neutral and only one feature
  cross-validated, so even at n=80 the predictor may stay weak. The value is a
  definitive answer, not a guaranteed model.

---

# FP-007 — Unified MTG application (implementation plan)

**Decision (2026-05-26):** start FP-007. North star: one app consolidating
deck testing + card reference + rules lookup + a deck library + (later)
replays, instead of the current pile of CLIs + the audit web GUI.

**Reality:** ~6–10 weeks of work. This doc is the plan + the first slice;
it is NOT done. It stays on `feature` as the living spec; slices land
incrementally behind the existing web app so nothing regresses.

## What already exists (the substrate — don't rebuild)

- **Deck testing:** the Flask web app (`web/`) — audit, propose/sim, dashboard,
  combos, role-targets, image cache. This is the natural shell to grow into.
- **Card reference:** `oracle_store.py` + `scryfall_client` snapshot cache +
  `mtg_cards/` shared image/oracle data.
- **Combos / rules-ish:** `combo_detection.py`, `game_changers.py`, bracket
  enforcement, `staples.classify_role*`.
- **Library:** the `.dck` deck dir + `knowledge_log` iteration history +
  pricing series.
- **Engine:** Forge (via `forge_runner`) + the parked `forge_py` goldfish sim.

The unification is mostly **navigation + a shared card-reference surface**
over substrate that's 80% built — not a green-field rewrite.

## Gating (from STATUS.md)

"Ship FP-006 fully first." FP-006 (web GUI) is shipped and was just
exercised end-to-end in Chrome (every button, the full audit→propose flow).
The practical gate — "the web app works for a full iteration cycle on real
decks without touching a CLI" — is **met**. So FP-007 is unblocked to *start*,
incrementally.

## Slices (each independently shippable, behind the existing app)

1. **Card reference panel (first slice — scoped below).** A `/card/<name>`
   view + a search box in the topbar: oracle text, type line, mana cost,
   legality, price, printings — all from `oracle_store` / `scryfall_client`
   (no new datastore). This is the biggest missing leg and the cleanest to
   add to the existing Flask shell.
2. **Unified nav shell.** Left-rail sections: Decks (current) / Cards (slice 1)
   / Rules. Keep the deck dashboard as the Decks section.
3. **Rules / combo lookup.** Surface `combo_detection` + bracket rules as a
   browsable reference (what combos exist for a color identity, what pushes a
   bracket) rather than only inline in the audit.
4. **Library view.** Cross-deck search over the `.dck` set + knowledge_log
   history (which decks run a card, verdict history, price trend).
5. **Replays (last, gated on `forge_py`).** Turn-by-turn game review — only
   meaningful once `forge_py` produces inspectable game state; parked with FP-001.

## First slice — Card reference panel (concrete, ~1–2 sessions)

- **Backend:** `GET /api/card/<name>` in a new `web/routes_cards.py` blueprint:
  returns `{name, type_line, mana_cost, oracle_text, color_identity,
  legalities, prices, printings, image_url}` from `oracle_store.card_reference`
  + `scryfall_client.lookup_card` (cache-first; `cache=False` refetch on miss).
  Degrades to a clean 404 on unknown card.
- **Frontend:** a topbar "Cards" search input → `/card/<name>` overlay reusing
  the existing card-image overlay + a details pane. No framework change (same
  vanilla `el()` helpers).
- **Tests:** route returns shape on a stubbed lookup; 404 on miss; search
  input wired (verified in Chrome like the other buttons).

## Risks / notes

- Don't fork state: the unified app must keep using the same `deck_dir`,
  `knowledge_log`, and `mtg_cards/` cache — no parallel datastores.
- Keep each slice behind the working app so `feature` + CI stay green; this
  doc + slice-1 tests are the contract.
- FP-013 (project-tuned LLM) and replays remain parked; FP-007 does not
  depend on them.

## Status

**Slices 1–4 SHIPPED** (confirmed 2026-07-04; this entry was stale):
slice 1 card-reference panel (`30def0d` — `/api/card` + topbar Cards
search), nav shell + `/api/rules` + `/api/library` (merged via
`dac2ed6`), plus loading/empty/error-state polish and keyboard
accessibility (`ff8395a`, `e006f7c`, PR #5). Only slice 5 (replays)
remains, parked on `forge_py` game-state (with FP-001).

---

# FP-010 — Desktop EXE (status + how to build)

**Decision (2026-05-26):** package the web app as a double-click desktop EXE.
~16h total; this is the first pass — a working launcher + freeze pipeline +
tests. Gate ("web app proven via browser for a full cycle") is met (verified
in Chrome this session).

## What shipped

- **`commander_builder/desktop.py`** — runs `web.app.create_app` on a daemon
  thread and shows it in a native window via **pywebview** at
  `http://127.0.0.1:<free-port>/`. One process, no browser, no manual server.
  Injectable `webview` / `serve` hooks make the wiring unit-testable
  (`tests/test_desktop.py`, 6 tests). Entry point: `commander-builder-desktop`.
- **`packaging/commander-builder.spec`** + **`packaging/desktop_entry.py`** —
  PyInstaller one-folder freeze; bundles the Flask `templates/` + `static/`
  as data files (so `create_app()` finds them inside `_MEIPASS`).
- **`scripts/build_desktop.py`** — installs the `[desktop]` extra and runs the
  freeze. Output: `dist/CommanderBuilder/CommanderBuilder.exe`.
- **pyproject**: `[desktop]` extra (`pywebview`, `pyinstaller`, `flask`).

## Build it

```powershell
python scripts/build_desktop.py          # installs deps + freezes
# -> dist/CommanderBuilder/CommanderBuilder.exe
```
Run on Windows for a Windows EXE (PyInstaller doesn't cross-compile). First
build is slow (pywebview pulls a native EdgeChromium/pythonnet backend).

## Deliberately external (NOT bundled)

The EXE bundles only the Python app + Flask assets. These stay on disk and the
app locates them like the dev setup:

| Data | Size | Why external |
|------|------|--------------|
| Forge JAR | ~120 MB | huge; updated every set; user already has `vendor/forge/` |
| JRE | ~150 MB | huge; platform-specific |
| `mtg_cards/` (images + oracle) | ~180 MB | huge; grows over time |

When Forge/JRE are absent the app still runs — only the audit/sim calls that
shell out to Forge error per-request (same as a dev box without Forge). Card
images lazy-fetch from Scryfall through the existing cache.

## Remaining slices (the rest of the ~16h)

1. **First-run data bootstrap** — on first launch, detect missing
   `vendor/forge/` + `mtg_cards/` and offer a downloader (Forge release from
   GitHub, JRE, and prime the card cache) instead of silently degrading.
2. **Deck-dir picker** — a first-run prompt / setting for where `.dck` files
   live (today it defaults to the Forge userdata path; a packaged app may want
   `%USERPROFILE%\Documents\CommanderBuilder\decks`).
3. **Icon + window chrome** — app icon, single-instance guard, graceful
   shutdown of the Flask thread on window close.
4. **Installer** — wrap the one-folder dist in an installer (Inno Setup /
   NSIS) or ship a zip; optional code-signing.
5. **CI build job** — a Windows GitHub Actions runner that produces the EXE
   artifact on tag.

## Status

**All five slices SHIPPED** (confirmed 2026-07-04; this entry was
stale): downloader + deck-dir picker + window chrome + JRE extraction
(merged via `d13db07`), Windows CI build job (`bc4d101`), and the Inno
Setup installer + `build_installer.py` driver (`8146450`, PR #7).
Producing the `.exe`/installer remains a local
`python scripts/build_desktop.py` / `build_installer.py` run (deps are
heavy); CI builds the artifact on tag.

---

# FP-014 — Build-from-scratch deck assembly

**Status: PARKED / future.** Sized rough at **2–3 sessions for a first
cut**. Unusually ready to start vs a cold plan — most of the ingredients
already exist on disk (listed under *Substrate* below), so this is
assembly + one genuinely hard research step, not a green-field build.

## Motivation

ManaFoundry.gg (and similar tools) assemble a **full deck from a chosen
commander** in one shot. commander-builder deliberately does the opposite
today: it is an *iteration engine* that improves an **existing** deck (the
README's own framing — "not a deck builder from scratch"). This plan
reverses that — take a commander (+ target bracket / archetype) and emit a
complete, legal 99 — with an angle the competitors structurally lack:
**assembled decks get Forge-VALIDATED, not just heuristically scored.**
Every other from-scratch builder stops at a static power heuristic; we can
hand the assembled list straight to the existing empirical
improve-loop and prove it out in simulation.

## Scope sketch (cite what already exists)

Seed and fill the shell from modules that are already built and tested:

- **Seed the skeleton** from the EDHREC average deck for the commander —
  `edhrec_client.fetch_average_deck` (coherent, no Moxfield dependency) —
  shaped by **archetype templates** (`archetype.py`) and **role targets**
  (`staples.ROLE_TARGETS`) so the ramp/draw/removal/wipe/protection counts
  land in-band from the start.
- **Synergy-driven picks** from the new **`lift_analysis.py`** — the
  co-occurrence matrix over the harvested corpus surfaces "pairs well with
  this commander/shell" candidates with empirical support, exactly the
  pick-selection signal a from-scratch builder needs.
- **Hit a target power level** with the new **`bracket_estimator.py`** —
  estimate the assembled list's bracket and steer picks (Game Changers /
  fast mana / combo density) up or down until the estimate matches the
  requested bracket.
- **Prefer owned cards** via **`collection.py`** — bias the fill toward
  what the user already owns (the same exclude/flag machinery the advisor
  now uses), so the first cut is buildable, not a wishlist.
- **Validate legality** with the guards the adversarial-review fix
  campaign hardened: the singleton / exactly-99-mainboard / drop-reporting
  checks in `web/deck_text_ops._apply_swaps_to_dck`, plus
  `_proposer_filters.enforce_color_identity` for color-identity legality.
- **Empirically tune** by handing the assembled `.dck` to the existing
  `commander-improve` loop — Forge A/B sims + knowledge_log verdicts turn
  a plausible pile into a measured one. **This is the validation moat.**

## The honest hard part

The assembler above is the **easy 80%**. Going from *"a pile of
role-appropriate, high-lift, in-color cards"* to *"a coherent 99 with a
real manabase"* — curve, color-source counts, the actual land base, and
the non-obvious glue that makes a deck *function* rather than merely
satisfy per-role quotas — is the **hard 20%** and the real research. Role
targets and lift scores get you a defensible shell; they do **not** get you
coherence. Expect the first cut to produce legal-but-mediocre decks that
the improve-loop then has to do heavy lifting on, and treat
"curated-coherence" as the open problem this plan actually has to solve,
not a detail.

## Substrate that already exists (why it's cheap to start)

`fetch_average_deck`, `archetype.py`, `staples.ROLE_TARGETS`,
`lift_analysis.py`, `bracket_estimator.py`, `collection.py`, the
legality/color-identity guards (`_apply_swaps_to_dck`,
`enforce_color_identity`), and the whole `commander-improve` empirical
loop are all shipped and tested. What's missing is (a) the orchestrator
that composes them into a from-scratch builder and (b) the
manabase/coherence step — i.e. the hard 20%.
