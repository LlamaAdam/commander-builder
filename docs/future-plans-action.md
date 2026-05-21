# Future-plans action plans (drafted 2026-05-20)

Concrete, scoped plans for the most actionable parked items in STATUS.md,
written after a code-level survey. Ordered by leverage × readiness. Each is a
proposal for review — not yet started.

---

## FP-003 — Concurrent Forge sims  ★ highest leverage right now

**Why now:** the FP-002 data generation is sim-bound (~12 kept/hour at 4-game
sims; one Forge JVM at a time). Running 2 sims in parallel ~halves wall-clock
for both curation batches AND the positives campaign.

**The blocker (confirmed in forge_runner.py):** Forge keys everything off the
install-dir `cwd` — it reads decks from `<install>/userdata/decks/commander/`
(ignores the `-D` flag in 2.0.12) and writes a single shared `forge.log` next
to `forge.profile.properties`. Two concurrent runs in the same install dir
collide on the deck dir + log.

**Plan (~30-60 min spike):**
1. Create a second cwd-isolated profile, e.g. `vendor/forge2/`:
   - Hard-link or junction the heavy read-only bits (`res/`, the
     `forge-gui-desktop-*.jar`) to avoid duplicating ~hundreds of MB.
   - Give it its OWN `forge.profile.properties` + `userdata/decks/commander/`
     (the decks must be present in each profile's userdata).
2. Parameterize `ForgeRunner` / `run_ab_simulation` with a `forge_dir` (cwd)
   so a caller can target profile 1 or 2.
3. A small pool driver: hand each pending sim to a free profile; cap at 2.
4. Verify: run two A/B sims simultaneously, confirm distinct `forge.log`s and
   no deck-dir contention; compare verdicts to serial baseline for sanity.

**Risks:** disk for a second userdata deck copy (small, .dck are ~1-2KB);
JVM memory for 2 concurrent Forge instances (RTX box has headroom); seat-order
RNG must stay per-run (already is).

**Effort:** ~1-2 hrs incl. a smoke test. **Reversible** (additive `forge_dir`
param + a sibling profile dir).

---

## FP-011 — BYO LLM token (sharing prep)

**State:** the credentials *file* layer already exists in `_secrets.py`
(`load_credentials`, `credentials_path`, `write_credentials_template`,
`config_main` CLI, chmod-600 warnings). **Missing:** a web config surface and
a secret-leak guard.

**Plan:**
1. `GET /api/config` — return settings with the API key **redacted**
   (`sk-ant-…last4` or just a `has_key: bool`). Never return the raw key.
2. `PUT /api/config` — accept a new key, write via `_secrets` to
   `~/.commander-builder/credentials` (chmod 600). Validate prefix
   (`sk-ant-`). Restrict to localhost (the app already binds 127.0.0.1).
3. Pre-commit hook: scan staged diffs for `sk-ant-`, `Bearer `, JWT (`eyJ`)
   prefixes; block the commit on a hit. Ship as `setup/hooks/pre-commit` +
   a `.pre-commit-config.yaml` (or a plain git hook installer script).
4. Tests: redaction never leaks the key; PUT rejects malformed keys; the hook
   catches a planted `sk-ant-` in a staged diff.

**Risk:** a config PUT is a "modify settings / write secret" action — keep it
localhost-only and never echo the key back. **Effort:** ~2-3 hrs.

**Unblock condition (per STATUS):** promote when shared beyond the original
dev. If sharing isn't imminent, the pre-commit hook alone is worth doing now
(cheap insurance against committing a key).

---

## FP-001 — Forge AI replacement (Claude/Ollama at decision points)  ★ orchestrator synergy

**Why interesting:** this is the natural payoff of the local+Claude
orchestrator — pilot Forge's in-game decisions with an LLM instead of Forge's
heuristic AI, for higher-signal sims. STATUS scopes it at 2-4 weeks (Phase 4).

**Reality check from forge_runner.py:** we currently treat Forge as a black-box
subprocess (run jar, parse stdout). Forge's AI decision points are *inside* the
Java engine — there's no Python hook. Piloting them needs either (a) a Forge
plugin/patch exposing decision callbacks (Java work, upstream-dependent), or
(b) Forge's scripting interface if one exists. This is a genuine multi-week
research effort, not a spike.

**Recommended first step (1-day spike, not the full thing):** investigate
whether Forge 2.0.12 exposes ANY AI-decision hook / scriptable interface /
external-controller protocol. If yes → prototype one decision (e.g. mulligan)
routed through the orchestrator router. If no → it requires forking Forge;
defer until that's worth it. **Don't promote to active without this spike.**

---

## FP-010 — Package web app as desktop EXE

**State/gate (per STATUS):** PyInstaller + pywebview, ~16h, bundle Forge+JRE,
first-run downloader for the 180MB mtg_cards. **Gate:** don't start until the
web app demonstrably works for a full iteration cycle on real decks (≥5 audits
via the browser without touching a CLI).

**Reality check:** we just confirmed the web app serves cleanly and the curator
runs under the subscription. But "5 browser audits without a CLI" hasn't been
demonstrated. Also: the deep-path / MAX_PATH issues (sklearn DLL load failure)
hint that bundling will need care on Windows path lengths.

**Recommendation:** keep parked. The gate isn't met, and the deep-path
fragility makes packaging premature. Revisit after a real browser-only
iteration loop is exercised.

---

## Suggested sequence
1. **FP-003 concurrent sims** — do first; directly accelerates the data work
   that's already in flight. Highest leverage, lowest risk.
2. **FP-011 pre-commit secret hook** — cheap insurance, do alongside.
   (Full BYO-token web config only when sharing is imminent.)
3. **FP-001 Forge-AI feasibility spike** — 1 day to learn if it's even
   hookable without forking Forge. Decision-gating, not a commitment.
4. **FP-010 desktop EXE** — stays parked until the browser-only loop + path
   fragility are addressed.
