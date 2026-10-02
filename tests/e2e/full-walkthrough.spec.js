// FULL WALKTHROUGH — two real decks, every page and action, a checklist
// and screenshots as the output (2026-10-02).
//
// WHY THIS EXISTS
// ---------------
// The five smokes next to this file pin individual regressions against a
// stubbed server. The owner asked a different question: "do a full test
// of 2 decks and see the results — test every part of the pages". That
// needs the real thing end to end: a real import, a real audit over
// EDHREC, a real Forge compare, every panel and button, and a record the
// owner can READ afterwards. So this spec never throws on a row: each
// page/action becomes one row of ``e2e-results/CHECKLIST.md`` +
// ``checklist.json`` (PASS / FAIL / SOFT / SKIP, elapsed ms, notes,
// screenshot) and the run finishes regardless. The only hard failure is
// the server never coming up (Playwright's webServer timeout).
//
// ROUTE INVENTORY (complete by construction — every row below maps to a
// line here; ``inventory`` is also written into checklist.json so a new
// route without a row is visible in the artifact).
//
//   path                                   method  deck  net  forge  covered by row
//   /                                      GET     -     -    -      home: page loads
//   /api/health                            GET     -     -    -      api: status endpoints
//   /api/forge_version                     GET     -     -    -      api: status endpoints
//   /api/correlation_summary               GET     -     -    -      api: status endpoints
//   /api/log_error                         POST    -     -    -      api: log_error sink
//   /api/card_image/<size>/<name>          GET     -     Y    -      api: card image
//   /api/config                            GET/PUT -     -    -      settings: dialog round-trip
//   /api/sim_settings                      GET     -     -    -      propose: modal + sim settings
//   /api/collection                        GET/PUT -     -    -      settings: collection paste + clear
//   /api/decks                             GET     -     -    -      sidebar: deck list + filters
//   /api/import_deck                       POST    -     Y    -      deck A: paste import / deck B: web import lane
//   /api/bulk_import                       POST    -     Y    -      new deck: bulk tab validation (no live fetch)
//   /api/build_deck, /api/build_job/<id>   POST/GET -    Y    -      new deck: build from scratch
//   /api/deck_text                         GET/PUT/DELETE Y  -  -    editor: no-op save round-trip / cleanup: delete
//   /api/deck_source                       GET/PUT Y     -    -      deck_source: GET/PUT/clear
//   /api/verify_against_source             GET     Y     Y    -      verify vs source (400 path, no live fetch)
//   /api/moxfield_format                   GET     Y     -    -      export: copy to Moxfield
//   /api/game_changers                     GET     -     -    -      game changers: topbar modal
//   /api/deck_audit                        GET     Y     Y    -      illegal cards modal / game changers in deck
//   /api/dashboard, /core, /section/<n>    GET     Y     -    -      dashboard: core + deferred sections
//   /api/iterations                        GET     Y     -    -      dashboard: iteration history
//   /api/iterations/<id>/verdict           PATCH   Y     -    -      api: iteration verdict + milestone PATCH
//   /api/iterations/<id>/milestone         PATCH   Y     -    -      api: iteration verdict + milestone PATCH
//   /api/pricing_series                    GET     Y     -    -      dashboard: cost over time
//   /api/iteration_graph                   GET     Y     -    -      history: iteration graph
//   /api/verdict_breakdown                 GET     Y     -    -      dashboard: verdict breakdown
//   /api/audit_diff                        GET     Y     -    -      history: compare two versions
//   /api/audit                             GET     Y     Y    -      (same advisor as the stream; not re-fetched for politeness)
//   /api/audit/stream                      GET     Y     Y    -      audit: heuristic stream / judge path without key
//   /api/advise                            GET     Y     Y    -      api: advise (legacy)
//   /api/propose_swap                      POST    Y     -    Y      api: propose_swap validation
//   /api/propose_swap_async, /api/sim_job  POST/GET Y    -    Y      forge: A/B compare
//   /api/save_iteration                    POST    Y     -    -      seed: iteration rows / forge: save iteration
//   /api/iteration/<id>, /snapshot         GET     Y     -    -      api: iteration detail + snapshot
//   /api/compare/<old>/<new>               GET     Y     -    -      api: iteration compare
//   /api/card/<name>                       GET     -     Y    -      cards: topbar lookup
//   /api/oracle/<name>                     GET     -     Y    -      api: oracle
//   /api/library                           GET     -     -    -      cards: library search
//   /api/rules/combo, /rules/game_changers GET     -     -    -      rules: combos + game changers
//   /api/replays, /api/replay/<run>/<game> GET     -     -    Y      replays: section (before/after compare)
//
//   UI surfaces without their own route: left rail (Decks/Cards/Rules/
//   Replays), deck filters + sort, mobile drawer, settings <dialog>,
//   new-deck modal tabs, alert modal (game changers / illegal / salt /
//   sim coverage), propose/edit modal, bracket override select.
//
//   CLI-only surfaces the owner asked about (no web route): adopt
//   explanation (``commander-adopt``) — run as a row when a Python is
//   reachable; ``commander-doctor`` is not exposed in the UI (noted).
//
// POLITENESS: one web-lane import attempt + one project-lane import for
// deck B, one build, one audit per deck (EDHREC caches the commander
// page for 24 h, so the judge row is a cache hit), 0.5 s between calls
// that reach a third party. Nothing loops over commanders.

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { test, expect } = require("@playwright/test");

const REPO = path.resolve(__dirname, "..", "..");
const RESULTS = path.join(REPO, "e2e-results");
const FIXTURES = path.join(REPO, "tests", "fixtures");
const OFFLINE = process.env.E2E_OFFLINE === "1";
const LIVE_APP = !!process.env.E2E_BASE_URL;
const KEEP_DECKS = process.env.E2E_KEEP_DECKS === "1";
const PYTHON = process.env.E2E_PYTHON || (process.platform === "win32" ? "python" : "python3");
const FORGE_WAIT_MS = Number(process.env.E2E_FORGE_WAIT_MS || 25 * 60 * 1000);
// Total screenshot budget: the results folder is committed by the local
// runner, so it must stay a few tens of MB at most.
const SHOT_BUDGET_BYTES = Number(process.env.E2E_SHOT_BUDGET_BYTES || 40 * 1024 * 1024);
const POLITE_MS = 500;

const ARCHIDEKT_DECK_B_URL = "https://archidekt.com/decks/60036";
const DECK_A_NAME = "Muxus Goblins E2E";
const DECK_B_FALLBACK_NAME = "Ezuri Elves E2E";
const THROWAWAY_NAME = "Throwaway Paste E2E";

// ---------------------------------------------------------------------------
// Checklist recorder
// ---------------------------------------------------------------------------

class SoftFail extends Error {}

const rows = [];
const inventory = fs
  .readFileSync(__filename, "utf-8")
  .split("\n")
  .filter((l) => /^\/\/   \/(api)?/.test(l))
  .map((l) => l.replace(/^\/\/\s{3}/, "").trim());
let shotBytes = 0;
let shotCapped = false;
const consoleErrors = [];
const serverState = { baseURL: "", deckDir: "", forgeVersion: null };
const decks = { A: null, B: null, bSource: "unresolved", created: [], throwaway: null, build: null };
const seeded = { iterationIds: { A: [], B: [] } };

function slug(s) {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
}

function short(s, n = 220) {
  // eslint-disable-next-line no-control-regex
  const t = String(s == null ? "" : s).replace(/\x1b\[[0-9;]*m/g, "").replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function ensureDir(p) {
  fs.mkdirSync(p, { recursive: true });
}

function writeChecklist() {
  ensureDir(RESULTS);
  const totals = { PASS: 0, FAIL: 0, SOFT: 0, SKIP: 0 };
  for (const r of rows) totals[r.status] = (totals[r.status] || 0) + 1;
  const payload = {
    generated: new Date().toISOString(),
    base_url: serverState.baseURL,
    deck_dir: serverState.deckDir,
    forge_version: serverState.forgeVersion,
    offline: OFFLINE,
    live_app: LIVE_APP,
    decks: { A: decks.A, B: decks.B, B_source: decks.bSource, build: decks.build },
    totals,
    rows,
    inventory,
  };
  fs.writeFileSync(path.join(RESULTS, "checklist.json"), JSON.stringify(payload, null, 2));

  const groups = ["global", "A", "B"];
  const lines = [];
  lines.push("# Full walkthrough checklist");
  lines.push("");
  lines.push(`Generated ${payload.generated} against ${payload.base_url || "(fixture server)"}` +
    `${OFFLINE ? " — OFFLINE (network/Forge rows skipped)" : ""}.`);
  lines.push("");
  lines.push(`Deck A: \`${decks.A || "(not created)"}\` · Deck B: \`${decks.B || "(not created)"}\` (${decks.bSource})` +
    (decks.build ? ` · built: \`${decks.build}\`` : ""));
  lines.push("");
  lines.push("Status key: PASS = worked as asserted · FAIL = broken or assertion missed · " +
    "SOFT = third-party / environment outage recorded, not a product failure · SKIP = not attempted (offline or not exposed).");
  for (const g of groups) {
    const sub = rows.filter((r) => r.deck === g);
    if (!sub.length) continue;
    lines.push("");
    lines.push(`## ${g === "global" ? "Global pages and actions" : `Deck ${g}: ${decks[g] || "?"}`}`);
    lines.push("");
    lines.push("| # | page / action | status | ms | notes | screenshot |");
    lines.push("|---|---|---|---|---|---|");
    sub.forEach((r) => {
      const n = rows.indexOf(r) + 1;
      const shot = r.screenshot ? `[png](${r.screenshot})` : "";
      lines.push(`| ${n} | ${r.page} | ${r.status} | ${r.ms} | ${(r.notes || "").replace(/\|/g, "\\|")} | ${shot} |`);
    });
  }
  lines.push("");
  lines.push(`**Totals:** ${rows.length} rows — PASS ${totals.PASS} · FAIL ${totals.FAIL} · SOFT ${totals.SOFT} · SKIP ${totals.SKIP}`);
  lines.push("");
  fs.writeFileSync(path.join(RESULTS, "CHECKLIST.md"), `${lines.join("\n")}\n`);
}

async function screenshot(page, deck, name) {
  if (shotCapped) return null;
  const dir = path.join(RESULTS, deck);
  ensureDir(dir);
  const rel = path.posix.join(deck, `${slug(name)}.png`);
  const abs = path.join(RESULTS, rel);
  try {
    await page.screenshot({ path: abs, fullPage: false, timeout: 10_000 });
    shotBytes += fs.statSync(abs).size;
    if (shotBytes > SHOT_BUDGET_BYTES) shotCapped = true;
    return rel;
  } catch (_e) {
    return null;
  }
}

/**
 * Run one checklist row. ``opts.network`` / ``opts.forge`` rows are
 * skipped offline; an assertion failure becomes FAIL, a thrown SoftFail
 * becomes SOFT, and uncaught browser errors seen during the row flip a
 * PASS to FAIL with the messages in the notes. A row never throws.
 */
async function row(page, deck, name, opts, fn) {
  const t0 = Date.now();
  const r = { page: name, deck, status: "PASS", ms: 0, notes: "", screenshot: null };
  if (opts.network && OFFLINE) {
    r.status = "SKIP";
    r.notes = "offline run: needs network";
  } else if (opts.forge && OFFLINE) {
    r.status = "SKIP";
    r.notes = "offline run: needs Forge";
  } else if (opts.skip) {
    r.status = "SKIP";
    r.notes = opts.skip;
  } else {
    consoleErrors.length = 0;
    try {
      const note = await fn(r);
      if (note) r.notes = short(note, 400);
    } catch (e) {
      r.status = e instanceof SoftFail ? "SOFT" : "FAIL";
      r.notes = short(e && e.message ? e.message : String(e), 400);
    }
    if (consoleErrors.length) {
      r.notes = `${r.notes}${r.notes ? " | " : ""}browser errors: ${short(consoleErrors.join("; "), 300)}`;
      if (r.status === "PASS") r.status = "FAIL";
    }
    // Run 1: one timed-out row left the settings <dialog> open and five
    // later rows failed on click timeouts. Whatever a row leaves behind
    // is closed here so each row starts from a clean page.
    // keepUi (2026-10-02, run 3): the compare row must leave the propose
    // modal open for the save-iteration row that follows it -- the
    // recovery added after run 1 was closing it, hiding the block.
    if (!opts.keepUi) await recoverUi(page);
  }
  r.ms = Date.now() - t0;
  if (!(r.status === "SKIP" && !opts.shotOnSkip)) {
    r.screenshot = await screenshot(page, deck, name);
  }
  rows.push(r);
  writeChecklist();
  return r;
}

// ---------------------------------------------------------------------------
// API + page helpers
// ---------------------------------------------------------------------------

/** Close anything modal a row may have left open; never throws. */
async function recoverUi(page) {
  try {
    if (page.isClosed()) return;
    await page.keyboard.press("Escape").catch(() => {});
    const cancel = page.locator("dialog[open] #settings-cancel");
    if (await cancel.count()) await cancel.click({ timeout: 2_000 }).catch(() => {});
    await page.evaluate(() => {
      document.querySelectorAll("dialog[open]").forEach((d) => { try { d.close(); } catch (_e) { d.removeAttribute("open"); } });
      document.querySelectorAll(".modal-backdrop").forEach((m) => { m.hidden = true; });
      const scrim = document.getElementById("_card-ref-overlay");
      if (scrim) scrim.remove();
      document.body.classList.remove("drawer-open");
    }).catch(() => {});
  } catch (_e) {
    /* recovery is best-effort */
  }
}

async function api(request, method, url, data, timeout) {
  const resp = await request.fetch(url, {
    method,
    data,
    headers: data !== undefined ? { "Content-Type": "application/json" } : undefined,
    timeout: timeout || 30_000,
  });
  let body = null;
  const text = await resp.text();
  try {
    body = JSON.parse(text);
  } catch (_e) {
    body = { _raw: text.slice(0, 200) };
  }
  return { status: resp.status(), body, headers: resp.headers() };
}

function cssEscape(value) {
  return value.replace(/([[\]"\\])/g, "\\$1");
}

async function gotoApp(page) {
  await page.goto("/");
  await expect(page.locator("#deck-list")).toBeVisible();
  // Give the deck list its first paint (either rows or an empty-state).
  await page.waitForFunction(
    () => !/Loading/.test(document.querySelector("#deck-list")?.textContent || ""),
    null,
    { timeout: 15_000 },
  );
}

async function selectDeck(page, deckId) {
  const li = page.locator(`#deck-list li[data-id="${cssEscape(deckId)}"]`);
  await expect(li).toBeVisible();
  await li.click();
  await expect(page.locator("#dashboard .commander-hero")).toBeVisible({ timeout: 30_000 });
}

/** Wait until every deferred dashboard slot has left its skeleton. */
async function waitDashboardSettled(page) {
  await page.waitForFunction(
    () => {
      const slots = ["dash-section-pricing", "dash-section-lift_picks"]
        .map((id) => document.getElementById(id))
        .filter(Boolean);
      return slots.every((s) => !s.querySelector(".skeleton") && !/Checking|Loading/.test(s.textContent || ""));
    },
    null,
    { timeout: 60_000 },
  ).catch(() => {});
}

function runPython(args, opts = {}) {
  const res = spawnSync(PYTHON, args, {
    cwd: REPO,
    encoding: "utf-8",
    timeout: opts.timeoutMs || 120_000,
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  return {
    ok: res.status === 0,
    status: res.status,
    stdout: (res.stdout || "").trim(),
    stderr: (res.stderr || "").trim(),
    error: res.error ? String(res.error.message) : "",
  };
}

async function importPaste(request, name, text, bracket = 3) {
  // Re-runnable on a developer machine: a leftover deck from a previous
  // run is replaced, never 409'd into a dead walkthrough.
  const stem = `[USER] ${name} [B${bracket}]`;
  await api(request, "DELETE", `/api/deck_text?deck=${encodeURIComponent(stem)}`);
  const res = await api(request, "POST", "/api/import_deck", { name, paste_text: text, bracket });
  return { ...res, stem };
}

/** Store a primer sidecar beside a deck through the project's own writer. */
function storePrimer(deckId, primer) {
  const primerFile = path.join(RESULTS, `_primer_${slug(deckId)}.txt`);
  fs.writeFileSync(primerFile, primer);
  const code = [
    "import pathlib",
    "from commander_builder.primer import store_primer_sidecar",
    `dck = pathlib.Path(${JSON.stringify(serverState.deckDir)}) / ${JSON.stringify(`${deckId}.dck`)}`,
    `out = store_primer_sidecar(dck, pathlib.Path(${JSON.stringify(primerFile)}).read_text(encoding='utf-8'))`,
    "print('PRIMER=' + out.action)",
  ].join("\n");
  const py = runPython(["-c", code]);
  fs.rmSync(primerFile, { force: true });
  return /PRIMER=(\w+)/.exec(py.stdout)?.[1] || short(py.stderr || py.error, 120);
}

async function seedIterations(request, deckId, deckText, verdicts) {
  const ids = [];
  for (const verdict of verdicts) {
    const res = await api(request, "POST", "/api/save_iteration", {
      deck_id: deckId,
      deck_name: deckId.replace(/^\[USER\]\s*/, ""),
      bracket: 3,
      audit_version: "v4",
      audit_manifest: { added: [], removed: [], rationale: "walkthrough seed row" },
      verdict,
      verdict_notes: "seeded by the full walkthrough",
      deck_snapshot: deckText,
    });
    if (res.status !== 200) {
      throw new Error(`save_iteration -> ${res.status} ${JSON.stringify(res.body).slice(0, 160)}`);
    }
    ids.push(res.body.id);
  }
  return ids;
}

// ---------------------------------------------------------------------------
// The walkthrough
// ---------------------------------------------------------------------------

test.describe.configure({ mode: "serial" });

test("full walkthrough: two decks, every page and action", async ({ page, request, baseURL }) => {
  test.setTimeout(40 * 60 * 1000);
  serverState.baseURL = baseURL;
  ensureDir(RESULTS);

  page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${short(err.message, 120)}`));
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    // Resource 4xx/5xx lines are the browser echoing an HTTP status the
    // row already records (an honest 502 on a bad import is the point).
    if (/Failed to load resource/.test(text)) return;
    consoleErrors.push(short(text, 120));
  });
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
  // The dashboard auto-kicks an EDHREC audit on every deck select; the
  // audit rows below trigger it deliberately, once per deck, so the
  // automatic one is switched off (same pref the smokes set).
  await page.addInitScript(() => {
    try { localStorage.setItem("auto_audit_on_dashboard_load", "0"); } catch (_e) { /* ignore */ }
  });

  // Both decks are real lists (2026-10-02 run 1: the golden fixture's
  // "Goblin 0..39" placeholders made the price / bracket / coverage rows
  // meaningless). Deck A is the committed Muxus list with its primer;
  // deck B is the live Archidekt import, else the committed Ezuri list.
  const deckAText = fs.readFileSync(path.join(FIXTURES, "e2e_walkthrough_muxus.dck"), "utf-8");
  const deckAPrimer = fs.readFileSync(path.join(FIXTURES, "e2e_walkthrough_muxus.primer.txt"), "utf-8");
  const fallbackBText = fs.readFileSync(path.join(FIXTURES, "e2e_walkthrough_green.dck"), "utf-8");
  const fallbackBPrimer = fs.readFileSync(path.join(FIXTURES, "e2e_walkthrough_green.primer.txt"), "utf-8");

  // ----- Phase 0: boot -------------------------------------------------
  await row(page, "global", "home: page loads", {}, async () => {
    const resp = await page.goto("/");
    expect(resp.status()).toBe(200);
    await expect(page).toHaveTitle(/Commander Builder/);
    await expect(page.locator("#deck-list")).toBeVisible();
    const badge = page.locator("#health-badge");
    await expect(badge).not.toHaveText(/checking/, { timeout: 15_000 });
    const txt = await badge.textContent();
    if (/unreachable/i.test(txt)) throw new Error(`health badge: ${txt}`);
    return `health badge: ${short(txt, 80)}`;
  });

  await row(page, "global", "api: status endpoints (health / forge_version / correlation_summary)", {}, async () => {
    const h = await api(request, "GET", "/api/health");
    expect(h.status).toBe(200);
    expect(h.body.status).toBe("ok");
    serverState.deckDir = h.body.deck_dir;
    const fv = await api(request, "GET", "/api/forge_version");
    expect(fv.status).toBe(200);
    serverState.forgeVersion = fv.body.version;
    const cs = await api(request, "GET", "/api/correlation_summary");
    expect(cs.status).toBe(200);
    return `deck_dir=${h.body.deck_dir} decks=${h.body.deck_count} forge=${fv.body.version || "none"} stale=${fv.body.is_stale}`;
  });

  await row(page, "global", "api: read-only catalogue endpoints", {}, async () => {
    const checks = [
      // A fresh store is redacted to just the <secret>_set flag;
      // defaults are filled client-side (observation, not a failure).
      ["/api/config", (b) => Object.keys(b).some((k) => /_api_key_set$/.test(k))],
      ["/api/sim_settings", (b) => b.games_are_per_pod === true && b.filler_pairs >= 1],
      ["/api/game_changers", (b) => Array.isArray(b.cards) && b.cards.length > 0],
      ["/api/rules/game_changers", (b) => Array.isArray(b.cards)],
      ["/api/rules/combo?identity=R", (b) => Array.isArray(b.combos)],
      ["/api/library?card=Mountain", (b) => Array.isArray(b.decks)],
      ["/api/replays", (b) => Array.isArray(b.runs)],
      ["/api/decks?all=1", (b) => Array.isArray(b.decks)],
      ["/api/collection", (b) => "configured" in b],
    ];
    const notes = [];
    for (const [url, ok] of checks) {
      const r = await api(request, "GET", url);
      if (r.status !== 200 || !ok(r.body)) throw new Error(`${url} -> ${r.status} ${JSON.stringify(r.body).slice(0, 120)}`);
      notes.push(`${url.split("?")[0]} ok`);
      if (url === "/api/config" && !("default_bracket" in r.body)) notes.push("(config: defaults not echoed on a fresh store)");
    }
    return notes.join(", ");
  });

  // ----- Phase 1: the two decks ---------------------------------------
  await row(page, "A", "deck A: paste-import the Muxus fixture through /api/import_deck (+ primer sidecar)", {}, async () => {
    const res = await importPaste(request, DECK_A_NAME, deckAText, 3);
    expect(res.status).toBe(200);
    expect(res.body.id).toBe(res.stem);
    decks.A = res.body.id;
    decks.created.push(decks.A);
    const primer = serverState.deckDir ? storePrimer(decks.A, deckAPrimer) : "skipped (deck_dir unknown)";
    return `id=${decks.A} filename=${res.body.filename}; primer sidecar: ${primer}`;
  });

  let liveB = null;
  await row(page, "B", "deck B: web import lane with the Archidekt URL (/api/import_deck)", { network: true }, async () => {
    const res = await api(request, "POST", "/api/import_deck", {
      name: "Gobs of Goblins", moxfield_url: ARCHIDEKT_DECK_B_URL, bracket: 3,
    });
    await sleep(POLITE_MS);
    if (res.status === 200) {
      const text = (await api(request, "GET", `/api/deck_text?deck=${encodeURIComponent(res.body.id)}`)).body.text || "";
      const problems = [];
      if (!/^Archidekt=60036/m.test(text)) problems.push("no Archidekt=60036 provenance line");
      if (!/Krenko, Mob Boss/.test(text)) problems.push("commander is not Krenko, Mob Boss");
      liveB = res.body.id;
      decks.created.push(liveB);
      const mainCount = (text.split("[Main]")[1] || "").split(/\r?\n/)
        .map((l) => /^(\d+)\s/.exec(l)).filter(Boolean).reduce((n, m) => n + Number(m[1]), 0);
      if (problems.length) throw new Error(`${problems.join("; ")} (imported ${liveB})`);
      return `imported ${liveB}; main=${mainCount} cards; primer=${JSON.stringify(res.body.primer)}`;
    }
    throw new Error(`${res.status} ${short(JSON.stringify(res.body), 240)}`);
  });

  await row(page, "B", "deck B: Archidekt categories (includedInDeck flags) from the raw deck JSON; project-lane import if the web lane failed", { network: true }, async () => {
    if (!serverState.deckDir) throw new Error("deck_dir unknown (health row failed)");
    // One fetch of the raw deck JSON (archidekt_client.fetch_deck) so the
    // checklist shows WHY the imported list has the size it has:
    // ``_excluded_categories`` honours Archidekt's per-category
    // includedInDeck flag, so a sideboard flagged "included" is imported
    // faithfully (run 1 reported 108/100 for this deck). When the web
    // lane failed, the same JSON is written through the project's own
    // converters — no second fetch.
    const code = [
      "import json, pathlib",
      "from commander_builder import archidekt_client as ac",
      "from commander_builder.moxfield_import import to_dck, _insert_metadata_lines",
      "from commander_builder.dck_meta import stamp_name_preserving_display",
      "from commander_builder.primer import store_primer_sidecar",
      `raw = ac.fetch_deck(ac.parse_deck_id(${JSON.stringify(ARCHIDEKT_DECK_B_URL)}))`,
      "cats = [(c.get('name'), c.get('includedInDeck')) for c in (raw.get('categories') or []) if isinstance(c, dict)]",
      "print('CATS=' + json.dumps(cats))",
      "print('CARDS=' + str(sum(int(e.get('quantity') or 0) for e in (raw.get('cards') or []) if isinstance(e, dict))))",
      `if ${liveB ? "False" : "True"}:`,
      "    dj = ac.to_deck_json(raw)",
      "    stem = '[USER] ' + ''.join(ch for ch in (dj.get('name') or 'Archidekt 60036') if ch not in '<>:\"/\\\\|?*').strip() + ' [B3]'",
      `    target = pathlib.Path(${JSON.stringify(serverState.deckDir)}) / (stem + '.dck')`,
      "    text = _insert_metadata_lines(to_dck(dj), ['Archidekt=60036', 'Source=archidekt'])",
      "    target.write_text(stamp_name_preserving_display(text, stem), encoding='utf-8', newline='')",
      "    store_primer_sidecar(target, dj.get('description'), source_id='archidekt:60036')",
      "    print('STEM=' + stem)",
    ].join("\n");
    const res = runPython(["-c", code], { timeoutMs: 180_000 });
    await sleep(POLITE_MS);
    if (res.error && /ENOENT/.test(res.error)) throw new SoftFail(`no ${PYTHON} on PATH: ${res.error}`);
    if (!res.ok) throw new SoftFail(`archidekt fetch exit ${res.status}: ${short(res.stderr || res.stdout || res.error, 300)}`);
    const cats = JSON.parse(/CATS=(.*)$/m.exec(res.stdout)?.[1] || "[]");
    const cards = /CARDS=(\d+)/.exec(res.stdout)?.[1];
    const catNote = cats.map(([n, inc]) => `${n}${inc === false ? "(excluded)" : inc === true ? "(included)" : "(?)"}`).join(", ");
    const m = /STEM=(.+)$/m.exec(res.stdout);
    if (m) {
      liveB = m[1].trim();
      decks.created.push(liveB);
    }
    return `${cards} card entries; categories: ${catNote}${m ? `; wrote ${liveB} from the same JSON` : ""}`;
  });

  await row(page, "B", "deck B: resolve (live import or fallback fixture + primer sidecar)", {}, async () => {
    if (liveB) {
      decks.B = liveB;
      decks.bSource = "live Archidekt import";
      return `using live import ${decks.B}`;
    }
    const res = await importPaste(request, DECK_B_FALLBACK_NAME, fallbackBText, 3);
    expect(res.status).toBe(200);
    decks.B = res.body.id;
    decks.bSource = "fallback fixture (tests/fixtures/e2e_walkthrough_green.dck)";
    decks.created.push(decks.B);
    const primer = serverState.deckDir ? storePrimer(decks.B, fallbackBPrimer) : "skipped (deck_dir unknown)";
    return `fallback deck ${decks.B}; primer sidecar: ${primer}`;
  });

  for (const key of ["A", "B"]) {
    await row(page, key, `seed: five iteration rows via /api/save_iteration`, {}, async () => {
      if (!decks[key]) throw new Error("deck missing");
      const text = (await api(request, "GET", `/api/deck_text?deck=${encodeURIComponent(decks[key])}`)).body.text;
      seeded.iterationIds[key] = await seedIterations(request, decks[key], text, ["kept", "kept", "reverted", "neutral", "pending"]);
      return `ids ${seeded.iterationIds[key].join(",")}`;
    });
  }

  await row(page, "global", "sidebar: deck list, filter box, type buttons, sort", {}, async () => {
    await gotoApp(page);
    for (const id of [decks.A, decks.B].filter(Boolean)) {
      await expect(page.locator(`#deck-list li[data-id="${cssEscape(id)}"]`)).toBeVisible();
    }
    await page.locator("#deck-filter-input").fill("E2E");
    await expect(page.locator("#deck-filter-count")).toContainText(/\d/);
    const filtered = await page.locator("#deck-list li[data-id]").count();
    await page.locator("#deck-filter-input").fill("");
    await page.locator('.deck-type-btn[data-type="user"]').click();
    await page.locator('.deck-type-btn[data-type="all"]').click();
    await page.locator("#deck-sort-select").selectOption("bracket");
    await page.locator("#deck-sort-select").selectOption("name");
    const total = await page.locator("#deck-list li[data-id]").count();
    return `${total} decks listed, ${filtered} match "E2E"`;
  });

  // ----- Phase 2: per-deck pages --------------------------------------
  for (const key of ["A", "B"]) {
    const deckId = decks[key];
    if (!deckId) {
      await row(page, key, "dashboard: (deck missing — remaining rows not attempted)", { skip: "deck was never created" }, async () => {});
      continue;
    }
    const enc = encodeURIComponent(deckId);

    await row(page, key, "dashboard: core paint (hero, legality, tiles, curve, categories, panels)", {}, async () => {
      await gotoApp(page);
      await selectDeck(page, deckId);
      await waitDashboardSettled(page);
      const dash = page.locator("#dashboard");
      await expect(dash.locator(".commander-hero .name")).not.toHaveText(/^(Untitled)?$/);
      await expect(dash.locator(".legality-banner .pill").first()).toBeVisible();
      for (const label of ["Avg CMC", "Lands", "Bracket", "Est. price"]) {
        await expect(dash.locator(".tile .label", { hasText: label }).first()).toBeVisible();
      }
      for (const title of ["Mana curve", "Categories", "Suggested adds", "Iteration history"]) {
        await expect(dash.locator("section.panel h3", { hasText: title }).first()).toBeVisible();
      }
      const hero = await dash.locator(".commander-hero .name").textContent();
      const legality = await dash.locator(".legality-banner").textContent();
      return `commander=${short(hero, 40)}; ${short(legality, 120)}`;
    });

    await row(page, key, "dashboard: price tile shows a number or an honest partial/dash", {}, async () => {
      const tile = page.locator("#dashboard .tile").filter({ hasText: "Est. price" }).first();
      const value = (await tile.locator(".value").textContent()) || "";
      const sub = (await tile.locator(".sub").first().textContent().catch(() => "")) || "";
      if (!/\$\d|—/.test(value)) throw new Error(`unexpected price value ${JSON.stringify(value)}`);
      if (value.trim() === "—" && !OFFLINE) throw new SoftFail(`no price (Scryfall lookups failed): ${short(sub, 80)}`);
      return `value=${value.trim()} ${sub ? `(${short(sub, 80)})` : ""}`;
    });

    await row(page, key, "dashboard: bracket estimate renders", {}, async () => {
      const tile = page.locator("#dashboard .tile").filter({ hasText: "Bracket" }).first();
      const txt = (await tile.textContent()) || "";
      if (!/Estimated bracket/.test(txt)) throw new SoftFail(`no estimate line: ${short(txt, 120)}`);
      return short(txt.replace(/Override:.*/s, ""), 160);
    });

    await row(page, key, "dashboard: deferred sections (pricing, lift_picks) + full /api/dashboard", {}, async () => {
      const notes = [];
      for (const name of ["pricing", "lift_picks"]) {
        const r = await api(request, "GET", `/api/dashboard/section/${name}?deck=${enc}`);
        expect(r.status).toBe(200);
        notes.push(`${name}=${r.body.status}`);
      }
      const core = await api(request, "GET", `/api/dashboard/core?deck=${enc}`);
      expect(core.status).toBe(200);
      expect(Array.isArray(core.body.deferred_sections)).toBe(true);
      const full = await api(request, "GET", `/api/dashboard?deck=${enc}`);
      expect(full.status).toBe(200);
      const bad = await api(request, "GET", `/api/dashboard/section/nope?deck=${enc}`);
      expect(bad.status).toBe(404);
      const pricingSlot = page.locator("#dash-section-pricing");
      const liftSlot = page.locator("#dash-section-lift_picks");
      notes.push(`pricing slot: ${short(await pricingSlot.textContent().catch(() => "(absent)"), 60)}`);
      notes.push(`lift slot: ${short(await liftSlot.textContent().catch(() => "(absent)"), 60)}`);
      return notes.join("; ");
    });

    await row(page, key, "dashboard: iteration history + verdict breakdown + cost over time", {}, async () => {
      const dash = page.locator("#dashboard");
      const hist = dash.locator("section.panel").filter({ hasText: "Iteration history" });
      await expect(hist).toBeVisible();
      const n = await hist.locator("li.iteration").count();
      if (n < 5) throw new Error(`expected >= 5 seeded iterations, saw ${n}`);
      const breakdown = dash.locator("section.panel").filter({ hasText: "Verdict by audit version" });
      await expect(breakdown).toBeVisible({ timeout: 15_000 });
      const pill = await breakdown.locator(".iteration .delta").first().textContent();
      const vb = await api(request, "GET", `/api/verdict_breakdown?deck=${enc}`);
      expect(vb.status).toBe(200);
      const ps = await api(request, "GET", `/api/pricing_series?deck=${enc}`);
      expect(ps.status).toBe(200);
      const cost = dash.locator("section.panel").filter({ hasText: "Cost over time" });
      const costShown = (await cost.count()) > 0;
      return `${n} iterations; breakdown pill "${short(pill, 40)}"; cost-over-time panel ${costShown ? "rendered" : "absent (no priced rows yet — expected for seeded rows)"}`;
    });

    await row(page, key, "history: view iteration graph (/api/iteration_graph)", {}, async () => {
      const btn = page.getByRole("button", { name: "View iteration graph" });
      await expect(btn).toBeVisible();
      await btn.click();
      await expect(page.getByRole("button", { name: "Hide iteration graph" })).toBeVisible({ timeout: 15_000 });
      const svg = page.locator("#dashboard .iteration-graph-wrap svg");
      const r = await api(request, "GET", `/api/iteration_graph?deck=${enc}`);
      expect(r.status).toBe(200);
      return `graph svg nodes: ${await svg.count()}; api nodes=${(r.body.nodes || []).length}`;
    });

    await row(page, key, "history: compare two versions (/api/audit_diff)", {}, async () => {
      const vc = page.locator("#dashboard .version-compare");
      await expect(vc).toBeVisible();
      const picks = vc.locator("select.version-compare-pick");
      expect(await picks.count()).toBe(2);
      const opts = await picks.first().locator("option").allTextContents();
      await picks.first().selectOption({ index: 0 });
      await picks.nth(1).selectOption({ index: Math.min(1, opts.length - 1) });
      await vc.getByRole("button", { name: "Compare" }).click();
      await expect(vc.locator(".diff-col").first()).toBeVisible({ timeout: 15_000 });
      const [a, b] = seeded.iterationIds[key];
      const r = await api(request, "GET", `/api/audit_diff?from_id=${a}&to_id=${b}`);
      expect(r.status).toBe(200);
      return `diff columns: ${await vc.locator(".diff-col").count()}`;
    });

    await row(page, key, "api: iteration detail / snapshot / compare / verdict + milestone PATCH", {}, async () => {
      const [a, b] = seeded.iterationIds[key];
      const d = await api(request, "GET", `/api/iteration/${a}`);
      expect(d.status).toBe(200);
      const s = await api(request, "GET", `/api/iteration/${a}/snapshot`);
      expect(s.status).toBe(200);
      const c = await api(request, "GET", `/api/compare/${a}/${b}`);
      expect(c.status).toBe(200);
      const v = await api(request, "PATCH", `/api/iterations/${b}/verdict`, { verdict: "neutral" });
      expect(v.status).toBe(200);
      const m = await api(request, "PATCH", `/api/iterations/${b}/milestone`, { milestone: "walkthrough" });
      expect(m.status).toBe(200);
      const m2 = await api(request, "PATCH", `/api/iterations/${b}/milestone`, { milestone: null });
      expect(m2.status).toBe(200);
      const its = await api(request, "GET", `/api/iterations?deck=${enc}`);
      expect(its.status).toBe(200);
      return `iteration ${a}: ${short(JSON.stringify(d.body).slice(0, 80))}; ${its.body.iterations.length} rows listed`;
    });

    await row(page, key, "dashboard: bracket override select reloads with the new bracket", {}, async () => {
      const sel = page.locator("#dashboard .bracket-control select");
      await expect(sel).toBeVisible();
      const core = page.waitForResponse((r) => r.url().includes("/api/dashboard/core") && r.url().includes("bracket=4"));
      await sel.selectOption("4");
      expect((await core).status()).toBe(200);
      await expect(page.locator("#dashboard .commander-hero")).toBeVisible({ timeout: 30_000 });
      await waitDashboardSettled(page);
      const back = page.waitForResponse((r) => r.url().includes("/api/dashboard/core") && r.url().includes("bracket=3"));
      await page.locator("#dashboard .bracket-control select").selectOption("3");
      expect((await back).status()).toBe(200);
      await expect(page.locator("#dashboard .commander-hero")).toBeVisible({ timeout: 30_000 });
      await waitDashboardSettled(page);
      return "B4 then back to B3, both repainted";
    });

    await row(page, key, "dashboard: sim-coverage / salt pills open their modals (when shown)", {}, async () => {
      const notes = [];
      const cov = page.locator("#dashboard .pill.warn", { hasText: "Forge can't simulate" });
      if (await cov.count()) {
        await cov.first().click();
        await expect(page.locator("#alert-modal")).toBeVisible();
        await expect(page.locator("#alert-title")).toHaveText("Forge sim coverage");
        notes.push(`coverage modal: ${short(await page.locator("#alert-body").textContent(), 80)}`);
        await page.locator('#alert-modal [data-close="alert-modal"]').click();
        await expect(page.locator("#alert-modal")).toBeHidden();
      } else {
        notes.push("no sim-coverage pill (corpus unavailable or every card supported)");
      }
      const salt = page.locator("#dashboard .salt-pill-row .pill").filter({ hasNotText: "Forge can't" });
      if (await salt.count()) {
        await salt.first().click();
        await expect(page.locator("#alert-modal")).toBeVisible();
        notes.push(`salt modal: ${short(await page.locator("#alert-title").textContent(), 30)}`);
        await page.locator('#alert-modal [data-close="alert-modal"]').click();
        await expect(page.locator("#alert-modal")).toBeHidden();
      } else {
        notes.push("no salt pill");
      }
      return notes.join("; ");
    });

    await row(page, key, "audit: Run audit (heuristic SSE stream reaches a terminal frame, health tiles rendered)", { network: true }, async () => {
      const streamResp = page.waitForResponse((r) => r.url().includes("/api/audit/stream"), { timeout: 30_000 });
      await page.getByRole("button", { name: "Run audit" }).click();
      const resp = await streamResp;
      const panel = page.locator("#sug-panel");
      await expect(panel).toContainText(/Audit — full proposed deck|Audit failed/, { timeout: 180_000 });
      // Terminal: either the rendered result (a proposed-text "Use this
      // list" button) or the failure line.
      await expect(panel.locator("button", { hasText: "Use this list" }).or(panel.getByText(/Audit failed/))).toBeVisible({ timeout: 180_000 });
      await sleep(POLITE_MS);
      const text = (await panel.textContent()) || "";
      if (/Audit failed/.test(text)) throw new SoftFail(`stream status ${resp.status()}: ${short(text, 200)}`);
      // deck_health_ui.renderHealthTile builds ``.health-tile`` nodes
      // inside ``.deck-health-row`` (run 1's ``.tile`` selector matched
      // nothing).
      const tiles = panel.locator(".deck-health-row .health-tile");
      const texts = await tiles.allTextContents();
      const n = texts.length;
      const live = texts.filter((t) => !/unavailable/i.test(t)).length;
      if (n < 3) throw new Error(`expected >= 3 health tiles, saw ${n}`);
      if (live < 3) throw new SoftFail(`only ${live}/${n} health tiles carry a signal (rest unavailable)`);
      return `stream ${resp.status()}; ${n} health tiles, ${live} with a signal; ${short(text, 140)}`;
    });

    await row(page, key, "audit: Save audit to log (no sim) -> /api/save_iteration", { network: true }, async () => {
      const btn = page.locator("#sug-panel button", { hasText: "Save audit to log" });
      if (!(await btn.count())) throw new SoftFail("no audit result to save (audit row did not complete)");
      const saved = page.waitForResponse((r) => r.url().includes("/api/save_iteration"), { timeout: 30_000 });
      await btn.click();
      const resp = await saved;
      expect(resp.status()).toBe(200);
      return `save_iteration -> ${resp.status()}`;
    });

    await row(page, key, "audit: analyst path without an API key degrades honestly (SSE frames read directly)", { network: true }, async () => {
      // Run 1 never saw a terminal frame in 180 s through the UI, so this
      // row reads the stream itself (same fetch + frame parser contract
      // as audit_streaming.js) and records every event with its elapsed
      // ms: a hang or an error frame lands in the checklist with the
      // last frame and the time, instead of a bare timeout.
      const result = await page.evaluate(async ({ deck, bracket, limitMs }) => {
        const source = [..._VALID_AUDIT_SOURCES].find((x) => x !== "heuristic" && x !== "bracket_peers");
        const url = `/api/audit/stream?deck=${encodeURIComponent(deck)}&bracket=${bracket}&source=${encodeURIComponent(source)}`;
        const t0 = performance.now();
        const out = { source, status: null, events: [], complete: null, error: null, lastFrame: "", elapsed: 0, timedOut: false };
        const ctl = new AbortController();
        const timer = setTimeout(() => { out.timedOut = true; ctl.abort(); }, limitMs);
        try {
          const resp = await fetch(url, { signal: ctl.signal });
          out.status = resp.status;
          if (!resp.ok || !resp.body) {
            out.error = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`;
          } else {
            const reader = resp.body.getReader();
            const dec = new TextDecoder();
            let buf = "";
            for (;;) {
              const { value, done } = await reader.read();
              if (done) break;
              buf += dec.decode(value, { stream: true });
              let idx;
              while ((idx = buf.indexOf("\n\n")) >= 0) {
                const frame = buf.slice(0, idx);
                buf = buf.slice(idx + 2);
                const ev = /^event:\s*(.+)$/m.exec(frame)?.[1]?.trim() || "message";
                const dataLine = /^data:\s*(.*)$/m.exec(frame)?.[1] || "";
                out.events.push({ event: ev, ms: Math.round(performance.now() - t0), bytes: frame.length });
                out.lastFrame = frame.slice(0, 300);
                if (ev === "complete") { try { out.complete = JSON.parse(dataLine); } catch (_e) { out.complete = { parseError: true }; } }
                if (ev === "error") { try { out.error = JSON.stringify(JSON.parse(dataLine)).slice(0, 300); } catch (_e) { out.error = dataLine.slice(0, 300); } }
              }
            }
          }
        } catch (e) {
          out.error = out.error || String(e && e.message ? e.message : e);
        } finally {
          clearTimeout(timer);
          out.elapsed = Math.round(performance.now() - t0);
        }
        return out;
      }, { deck: deckId, bracket: 3, limitMs: 300_000 });
      await sleep(POLITE_MS);
      const trail = result.events.map((e) => `${e.event}@${e.ms}ms`).join(" > ") || "(no frames)";
      const base = `source=${result.source} HTTP ${result.status}; frames: ${trail}; total ${result.elapsed}ms`;
      if (result.timedOut) throw new Error(`stream still open after 300 s — ${base}; last frame: ${short(result.lastFrame, 160)}`);
      if (result.error) throw new Error(`stream error — ${base}; ${short(result.error, 200)}`);
      if (!result.complete) throw new Error(`no complete frame — ${base}; last frame: ${short(result.lastFrame, 160)}`);
      const warning = String(result.complete.warning || "");
      if (!/no API key|requires ANTHROPIC_API_KEY/i.test(warning)) {
        throw new Error(`complete frame lacks the no-key warning — ${base}; source=${result.complete.source}; warning=${short(warning, 160) || "(none)"}`);
      }
      return `${base}; effective source=${result.complete.source}; warning: ${short(warning, 160)}`;
    });

    await row(page, key, "api: /api/advise (legacy sync advisor)", { network: true }, async () => {
      const r = await api(request, "GET", `/api/advise?deck=${enc}`, undefined, 90_000);
      await sleep(POLITE_MS);
      if (r.status !== 200) throw new SoftFail(`${r.status} ${short(JSON.stringify(r.body), 160)}`);
      return `200; keys ${Object.keys(r.body).slice(0, 6).join(",")}`;
    });

    await row(page, key, "editor: Edit deck opens, no-op save round-trips without changing bytes", {}, async () => {
      const before = await api(request, "GET", `/api/deck_text?deck=${enc}`);
      expect(before.status).toBe(200);
      await page.getByRole("button", { name: "Edit deck" }).click();
      await expect(page.locator("#propose-modal")).toBeVisible();
      await expect(page.locator("#propose-title")).toHaveText("Edit deck");
      await expect(page.locator("#propose-text")).toHaveValue(/\[Main\]/);
      const put = page.waitForResponse((r) => r.url().includes("/api/deck_text") && r.request().method() === "PUT");
      await page.locator("#propose-run").click();
      expect((await put).status()).toBe(200);
      await expect(page.locator("#propose-status")).toContainText(/Saved|re-verified/, { timeout: 15_000 });
      const status = await page.locator("#propose-status").textContent();
      if (await page.locator("#propose-modal").isVisible()) {
        await page.locator("#propose-close").click();
      }
      const after = await api(request, "GET", `/api/deck_text?deck=${enc}`);
      expect(after.body.text).toBe(before.body.text);
      let bytesNote = "bytes equal via API";
      const file = serverState.deckDir && path.join(serverState.deckDir, `${deckId}.dck`);
      if (file && fs.existsSync(file)) {
        bytesNote = `file ${fs.statSync(file).size} bytes, content equal`;
      }
      return `${short(status, 60)}; ${bytesNote}`;
    });

    await row(page, key, "deck_source: GET / PUT / clear, and the Attach Moxfield URL prompt", {}, async () => {
      const g = await api(request, "GET", `/api/deck_source?deck=${enc}`);
      expect(g.status).toBe(200);
      const original = g.body.moxfield_url;
      const bad = await api(request, "PUT", `/api/deck_source?deck=${enc}`, { moxfield_url: "not a url at all!" });
      expect(bad.status).toBe(400);
      const put = await api(request, "PUT", `/api/deck_source?deck=${enc}`, { moxfield_url: "https://moxfield.com/decks/walkthroughE2E" });
      expect(put.status).toBe(200);
      expect(put.body.moxfield_id).toBe("walkthroughE2E");
      // UI path: the hero shows a verify button once a source exists.
      await page.reload();
      await gotoApp(page);
      await selectDeck(page, deckId);
      const verifyBtn = page.getByRole("button", { name: "Verify vs Moxfield" });
      const hasVerify = (await verifyBtn.count()) > 0;
      const clear = await api(request, "PUT", `/api/deck_source?deck=${enc}`, { moxfield_url: original || "" });
      expect(clear.status).toBe(200);
      await page.reload();
      await gotoApp(page);
      await selectDeck(page, deckId);
      page.once("dialog", (d) => d.dismiss().catch(() => {}));
      const attach = page.getByRole("button", { name: "Attach Moxfield URL" });
      const hasAttach = (await attach.count()) > 0;
      if (hasAttach) await attach.click();
      return `source was ${original || "none"}; verify button after PUT: ${hasVerify}; attach button: ${hasAttach} (prompt dismissed)`;
    });

    await row(page, key, "verify vs source: 400 without a source (no live Moxfield fetch for politeness)", {}, async () => {
      const r = await api(request, "GET", `/api/verify_against_source?deck=${enc}`);
      if (r.status === 400) return `400 ${r.body.error}`;
      if (r.status === 200) return `deck already carries a source; live verify answered 200 (matched ${r.body.matched})`;
      throw new SoftFail(`${r.status} ${short(JSON.stringify(r.body), 160)}`);
    });

    await row(page, key, "export: Copy to Moxfield (/api/moxfield_format + clipboard or manual fallback)", {}, async () => {
      const fmt = page.waitForResponse((r) => r.url().includes("/api/moxfield_format"));
      await page.getByRole("button", { name: "Copy to Moxfield" }).click();
      const resp = await fmt;
      expect(resp.status()).toBe(200);
      const body = await resp.json();
      expect(body.text).toMatch(/\d+ /);
      await sleep(500);
      const manual = await page.locator("#alert-modal").isVisible();
      if (manual) await page.locator('#alert-modal [data-close="alert-modal"]').click();
      return `${body.text.split("\n").length} lines; ${manual ? "manual-copy modal shown" : "clipboard path"}`;
    });

    await row(page, key, "game changers: topbar modal flags in-deck cards (/api/deck_audit)", { network: true }, async () => {
      await page.locator("#btn-game-changers").click();
      await expect(page.locator("#alert-modal")).toBeVisible();
      await expect(page.locator("#alert-title")).toHaveText("Game Changers");
      await expect(page.locator("#alert-body")).toContainText(/Game Changers list/, { timeout: 30_000 });
      await page.waitForTimeout(1500);
      const text = short(await page.locator("#alert-body").textContent(), 200);
      await page.locator('#alert-modal [data-close="alert-modal"]').click();
      await sleep(POLITE_MS);
      return text;
    });

    await row(page, key, "illegal cards: topbar modal (Scryfall-backed /api/deck_audit)", { network: true }, async () => {
      const audit = page.waitForResponse((r) => r.url().includes("/api/deck_audit"), { timeout: 120_000 });
      await page.locator("#btn-illegal").click();
      await expect(page.locator("#alert-modal")).toBeVisible();
      await expect(page.locator("#alert-title")).toHaveText("Illegal cards");
      const resp = await audit;
      await page.waitForTimeout(1000);
      const text = short(await page.locator("#alert-body").textContent(), 200);
      await page.locator('#alert-modal [data-close="alert-modal"]').click();
      await sleep(POLITE_MS);
      if (resp.status() !== 200) throw new SoftFail(`deck_audit ${resp.status()}: ${text}`);
      return `deck_audit 200: ${text}`;
    });

    await row(page, key, "combos: Rules section lookup for the deck's identity", {}, async () => {
      await page.locator('.rail-btn[data-section="rules"]').click();
      await expect(page.locator("#section-rules")).toBeVisible();
      await page.locator("#combo-identity-input").fill("R");
      await page.locator("#combo-search-form button[type=submit]").click();
      await expect(page.locator("#rules-results")).not.toContainText(/Loading/, { timeout: 15_000 });
      const text = short(await page.locator("#rules-results").textContent(), 120);
      await page.locator('.rail-btn[data-section="decks"]').click();
      return text;
    });

    await row(page, key, "propose: modal opens, sim settings label the games radios, cancel", {}, async () => {
      const ss = page.waitForResponse((r) => r.url().includes("/api/sim_settings")).catch(() => null);
      await page.getByRole("button", { name: "Propose changes" }).click();
      await expect(page.locator("#propose-modal")).toBeVisible();
      await expect(page.locator("#propose-title")).toHaveText("Propose changes");
      await expect(page.locator("#propose-text")).toHaveValue(/\[Main\]/);
      await ss;
      const labels = await page.locator("#propose-modal .games-label").allTextContents();
      if (!labels.some((l) => /\/pod|per pod/.test(l))) throw new Error(`games labels not rewritten: ${labels.join(" / ")}`);
      await page.locator("#propose-close").click();
      await expect(page.locator("#propose-modal")).toBeHidden();
      return labels.join(" / ");
    });

    await row(page, key, "api: propose_swap validation (no-change diff -> 400, bad games -> 400)", {}, async () => {
      const text = (await api(request, "GET", `/api/deck_text?deck=${enc}`)).body.text;
      const same = await api(request, "POST", "/api/propose_swap_async", { deck: deckId, new_text: text, games: 10, mode: "pod" });
      expect(same.status).toBe(400);
      const games = await api(request, "POST", "/api/propose_swap", { deck: deckId, new_text: `${text}\n1 Sol Ring\n`, games: 7 });
      expect(games.status).toBe(400);
      return `${same.body.error}; ${games.body.error}`;
    });

    await row(page, key, "adopt: explanation from the primer (commander-adopt CLI; no web route)", {}, async () => {
      if (!serverState.deckDir) throw new Error("deck_dir unknown");
      const dck = path.join(serverState.deckDir, `${deckId}.dck`);
      const res = runPython(["-m", "commander_builder.adopt", dck, "--json"], { timeoutMs: 180_000 });
      if (res.error && /ENOENT/.test(res.error)) throw new SoftFail(`no ${PYTHON} on PATH`);
      if (!res.ok) throw new Error(`adopt exit ${res.status}: ${short(res.stderr || res.stdout, 300)}`);
      let parsed = null;
      try { parsed = JSON.parse(res.stdout); } catch (_e) { /* report raw */ }
      const sidecar = fs.existsSync(path.join(serverState.deckDir, `${deckId}.primer.md`));
      fs.writeFileSync(path.join(RESULTS, key, "adopt.json"), res.stdout);
      const keys = parsed ? Object.keys(parsed).slice(0, 8).join(",") : short(res.stdout, 120);
      if (!sidecar) throw new Error(`deck ${key} has no primer sidecar — adopt explanation ran without the primer`);
      return `primer sidecar: ${sidecar}; adopt keys: ${keys}`;
    });
  }

  // ----- Phase 3: global surfaces -------------------------------------
  await row(page, "global", "settings: dialog round-trip (/api/config GET + PUT)", {}, async () => {
    await gotoApp(page);
    await page.locator("#btn-settings").click();
    await expect(page.locator("#settings-dialog")).toBeVisible();
    await expect(page.locator("#settings-status")).not.toHaveText(/loading/, { timeout: 15_000 });
    const keyState = await page.locator("#settings-key-state").textContent();
    await page.locator("#settings-bracket").fill("4");
    await page.locator("#settings-moxfield").fill("walkthrough-user");
    const put = page.waitForResponse((r) => r.url().includes("/api/config") && r.request().method() === "PUT");
    await page.locator("#settings-save").click();
    expect((await put).status()).toBe(200);
    await expect(page.locator("#settings-status")).toHaveText("saved");
    await expect(page.locator("#settings-dialog")).toBeHidden({ timeout: 5_000 });
    const cfg = await api(request, "GET", "/api/config");
    expect(cfg.body.default_bracket).toBe(4);
    expect(cfg.body.moxfield_user).toBe("walkthrough-user");
    const restore = await api(request, "PUT", "/api/config", { default_bracket: 3, moxfield_user: "" });
    expect(restore.status).toBe(200);
    const bad = await api(request, "PUT", "/api/config", { default_bracket: 9 });
    expect(bad.status).toBe(400);
    await page.locator("#btn-settings").click();
    await expect(page.locator("#settings-dialog")).toBeVisible();
    await page.locator("#settings-cancel").click();
    await expect(page.locator("#settings-dialog")).toBeHidden();
    return `key state ${short(keyState, 30)}; bracket 4 saved and restored; bad bracket -> 400`;
  });

  await row(page, "global", "settings: collection paste + clear (/api/collection)", {}, async () => {
    await page.locator("#btn-settings").click();
    await expect(page.locator("#settings-dialog")).toBeVisible();
    await page.locator("#settings-collection").fill("1 Sol Ring\n1 Arcane Signet\nLightning Bolt\n");
    // saveSettings PUTs /api/config first and only then the collection
    // (non-blank textarea); wait for each in turn (run 1 waited on the
    // collection alone with the default 15 s and timed out).
    const putConfig = page.waitForResponse((r) => r.url().includes("/api/config") && r.request().method() === "PUT", { timeout: 30_000 });
    const putColl = page.waitForResponse((r) => r.url().includes("/api/collection") && r.request().method() === "PUT", { timeout: 30_000 });
    await page.locator("#settings-save").click();
    const cfgStatus = (await putConfig).status();
    const collStatus = (await putColl).status();
    expect(cfgStatus).toBe(200);
    expect(collStatus).toBe(200);
    await expect(page.locator("#settings-collection-state")).toHaveText(/3 cards/);
    await expect(page.locator("#settings-dialog")).toBeHidden({ timeout: 5_000 });
    await page.locator("#btn-settings").click();
    await expect(page.locator("#settings-collection-state")).toHaveText(/3 cards/);
    await page.locator("#settings-collection-clear").click();
    await expect(page.locator("#settings-collection-state")).toHaveText("(not set)");
    await page.locator("#settings-cancel").click();
    const g = await api(request, "GET", "/api/collection");
    expect(g.body.configured).toBe(false);
    return `config PUT ${cfgStatus}, collection PUT ${collStatus}; 3-card collection registered then cleared`;
  });

  await row(page, "global", "cards: library search (/api/library)", {}, async () => {
    await page.locator('.rail-btn[data-section="cards"]').click();
    await expect(page.locator("#section-cards")).toBeVisible();
    await expect(page.locator("#nav-section-placeholder")).toBeVisible();
    await page.locator("#library-card-input").fill("Mountain");
    await page.locator("#library-search-form button[type=submit]").click();
    await expect(page.locator("#library-results")).toContainText(/deck/, { timeout: 15_000 });
    const text = short(await page.locator("#library-results").textContent(), 120);
    const first = page.locator("#library-results li[data-deck]").first();
    if (await first.count()) {
      await first.click();
      await expect(page.locator("#dashboard .commander-hero")).toBeVisible({ timeout: 30_000 });
    }
    return text;
  });

  await row(page, "global", "cards: topbar card lookup (/api/card/<name>)", { network: true }, async () => {
    await page.locator("#card-search-input").fill("Lightning Bolt");
    const resp = page.waitForResponse((r) => r.url().includes("/api/card/"), { timeout: 60_000 });
    await page.locator("#card-search-btn").click();
    const r = await resp;
    await sleep(POLITE_MS);
    // openCardReference renders into its own overlay appended to body
    // (#_card-ref-overlay), not the shared alert modal.
    const overlay = page.locator("#_card-ref-overlay");
    await expect(overlay).toBeVisible();
    await expect(overlay).toContainText(/Lightning Bolt|Lookup failed|No card found/, { timeout: 15_000 });
    const heading = await overlay.locator("h2").first().textContent().catch(() => "");
    const text = short(await overlay.textContent(), 160);
    await page.keyboard.press("Escape");
    await expect(overlay).toBeHidden();
    if (r.status() !== 200) throw new SoftFail(`card lookup ${r.status()}: ${text}`);
    if (!/Lightning Bolt/.test(heading || "")) throw new Error(`no card heading: ${text}`);
    return `heading "${short(heading, 40)}"; ${text}`;
  });

  await row(page, "global", "api: oracle + card image", { network: true }, async () => {
    const o = await api(request, "GET", "/api/oracle/Lightning%20Bolt");
    await sleep(POLITE_MS);
    const img = await request.get("/api/card_image/small/Lightning%20Bolt");
    await sleep(POLITE_MS);
    if (o.status !== 200) throw new SoftFail(`oracle ${o.status}`);
    if (img.status() !== 200) throw new SoftFail(`card_image ${img.status()}`);
    return `oracle 200 (${o.headers["x-oracle-cache"] || "miss"}), card_image ${img.status()} ${img.headers()["content-type"]}`;
  });

  await row(page, "global", "rules: Game Changers list in the Rules section", {}, async () => {
    await page.locator('.rail-btn[data-section="rules"]').click();
    await page.locator("#rules-game-changers-btn").click();
    await expect(page.locator("#rules-results")).toContainText(/Game Changers/, { timeout: 15_000 });
    const text = short(await page.locator("#rules-results p").first().textContent(), 80);
    await page.locator('.rail-btn[data-section="decks"]').click();
    return text;
  });

  await row(page, "global", "replays: section before the compare (/api/replays)", {}, async () => {
    await page.locator('.rail-btn[data-section="replays"]').click();
    await expect(page.locator("#section-replays")).toBeVisible();
    await expect(page.locator("#replays-main")).toBeVisible();
    const resp = page.waitForResponse((r) => r.url().includes("/api/replays"));
    await page.locator("#replays-refresh-btn").click();
    const body = await (await resp).json();
    await page.waitForTimeout(500);
    await page.locator('.rail-btn[data-section="decks"]').click();
    return `${body.count} runs, capture enabled=${body.enabled}`;
  });

  await row(page, "global", "api: log_error sink", {}, async () => {
    const r = await api(request, "POST", "/api/log_error", {
      kind: "error", message: "walkthrough probe", url: baseURL, stack: "probe",
    });
    expect(r.status).toBe(200);
    return `ref ${r.body.ref || JSON.stringify(r.body).slice(0, 60)}`;
  });

  await row(page, "global", "new deck: modal tabs switch; bulk tab validates an empty paste (no live fetch)", {}, async () => {
    await page.locator("#btn-new-deck").click();
    await expect(page.locator("#new-deck-modal")).toBeVisible();
    for (const tab of ["paste", "bulk", "build", "moxfield"]) {
      await page.locator(`.tab[data-tab="${tab}"]`).click();
      await expect(page.locator(`#tab-${tab}`)).toBeVisible();
    }
    await page.locator('.tab[data-tab="bulk"]').click();
    await page.locator("#new-bulk-urls").fill("");
    await page.locator("#new-bulk-import").click();
    await expect(page.locator("#new-deck-status")).toContainText(/Paste at least one/);
    await page.locator('.tab[data-tab="moxfield"]').click();
    await page.locator("#new-mox-url").fill("");
    await page.locator("#new-mox-import").click();
    await expect(page.locator("#new-deck-status")).toContainText(/Enter a Moxfield/);
    const bulk = await api(request, "POST", "/api/bulk_import", { urls: [] });
    return `tabs ok; empty bulk -> UI validation; POST bulk_import [] -> ${bulk.status}`;
  });

  await row(page, "global", "new deck: paste tab creates a throwaway deck, Delete removes it (confirm dialog)", {}, async () => {
    await api(request, "DELETE", `/api/deck_text?deck=${encodeURIComponent(`[USER] ${THROWAWAY_NAME} [B2]`)}`);
    // Every row starts with the modals closed (recoverUi), so reopen.
    await page.locator("#btn-new-deck").click();
    await expect(page.locator("#new-deck-modal")).toBeVisible();
    await page.locator('.tab[data-tab="paste"]').click();
    await page.locator("#new-paste-name").fill(THROWAWAY_NAME);
    await page.locator("#new-paste-bracket").selectOption("2");
    await page.locator("#new-paste-text").fill(fallbackBText);
    const imp = page.waitForResponse((r) => r.url().includes("/api/import_deck"));
    await page.locator("#new-paste-create").click();
    const resp = await imp;
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    decks.throwaway = body.id;
    await expect(page.locator("#new-deck-modal")).toBeHidden({ timeout: 15_000 });
    await selectDeck(page, decks.throwaway);
    page.once("dialog", (d) => d.accept().catch(() => {}));
    const del = page.waitForResponse((r) => r.url().includes("/api/deck_text") && r.request().method() === "DELETE");
    await page.getByRole("button", { name: "Delete" }).click();
    expect((await del).status()).toBe(200);
    await expect(page.locator(`#deck-list li[data-id="${cssEscape(decks.throwaway)}"]`)).toHaveCount(0, { timeout: 15_000 });
    const gone = await api(request, "GET", `/api/deck_text?deck=${encodeURIComponent(decks.throwaway)}`);
    expect(gone.status).toBe(404);
    const dup = decks.throwaway;
    decks.throwaway = null;
    return `created ${dup}, deleted via UI, deck_text now 404`;
  });

  await row(page, "global", "new deck: build from scratch (Krenko, Mob Boss, B3) job completes and loads", { network: true }, async () => {
    await page.locator("#btn-new-deck").click();
    await page.locator('.tab[data-tab="build"]').click();
    await page.locator("#new-build-commander").fill("Krenko, Mob Boss");
    await page.locator("#new-build-name").fill("Krenko Build E2E");
    await page.locator("#new-build-bracket").selectOption("3");
    const start = page.waitForResponse((r) => r.url().includes("/api/build_deck"), { timeout: 30_000 });
    await page.locator("#new-build-run").click();
    const startResp = await start;
    if (startResp.status() !== 202) throw new Error(`build_deck -> ${startResp.status()} ${short(await startResp.text(), 120)}`);
    await expect(page.locator("#new-deck-status")).toContainText(/Built|Error/, { timeout: 300_000 });
    const status = (await page.locator("#new-deck-status").textContent()) || "";
    await sleep(POLITE_MS);
    if (/Error/.test(status)) throw new SoftFail(short(status, 200));
    const summary = short(await page.locator("#new-build-summary").textContent(), 200);
    await page.locator("#new-build-summary").getByRole("button", { name: "Load deck" }).click();
    await expect(page.locator("#dashboard .commander-hero")).toBeVisible({ timeout: 30_000 });
    decks.build = await page.locator('#deck-list li[aria-current="true"]').getAttribute("data-id");
    if (decks.build) decks.created.push(decks.build);
    return `${short(status, 60)}; ${summary}`;
  });

  await row(page, "global", "mobile: drawer toggle at phone width", {}, async () => {
    await page.setViewportSize({ width: 390, height: 800 });
    await gotoApp(page);
    const toggle = page.locator("#btn-drawer-toggle");
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(page.locator("body")).toHaveClass(/drawer-open/);
    await page.locator("#drawer-scrim").click({ force: true });
    await expect(page.locator("body")).not.toHaveClass(/drawer-open/);
    return "drawer opened via hamburger and closed via scrim";
  }).finally(() => page.setViewportSize({ width: 1280, height: 800 }));

  // ----- Phase 4: ONE real Forge compare --------------------------------
  let simJobId = null;
  let simDone = false;
  await row(page, "A", "forge: A/B compare (deck A vs deck B text, 10 games per pod, pod mode) reaches a terminal status", { forge: true, keepUi: true }, async () => {
    if (!decks.A || !decks.B) throw new Error("both decks are required");
    const bText = (await api(request, "GET", `/api/deck_text?deck=${encodeURIComponent(decks.B)}`)).body.text;
    await gotoApp(page);
    await selectDeck(page, decks.A);
    await page.getByRole("button", { name: "Propose changes" }).click();
    await expect(page.locator("#propose-modal")).toBeVisible();
    await expect(page.locator("#propose-text")).toHaveValue(/\[Main\]/);
    await page.locator("#propose-text").fill(bText);
    await page.locator('#propose-modal input[name="mode"][value="pod"]').check();
    await page.locator('#propose-modal input[name="games"][value="10"]').check();
    page.on("response", (r) => {
      if (r.url().includes("/api/propose_swap_async") && r.status() === 202) {
        r.json().then((b) => { simJobId = b.job_id; }).catch(() => {});
      }
    });
    const start = page.waitForResponse((r) => r.url().includes("/api/propose_swap_async"), { timeout: 60_000 });
    await page.locator("#propose-run").click();
    const startResp = await start;
    if (startResp.status() !== 202) {
      const body = await startResp.text();
      if (startResp.status() === 503) throw new SoftFail(`Forge not available: ${short(body, 200)}`);
      throw new Error(`propose_swap_async -> ${startResp.status()} ${short(body, 200)}`);
    }
    const status = page.locator("#propose-status");
    const deadline = Date.now() + FORGE_WAIT_MS;
    let last = "";
    while (Date.now() < deadline) {
      last = (await status.textContent()) || "";
      if (/^Done\.|Error|Network error/.test(last)) break;
      if (await page.locator(".save-iteration-block").count()) break;
      await page.waitForTimeout(5_000);
    }
    await screenshot(page, "A", "forge-compare-result-detail");
    if (!/^Done\./.test(last)) throw new Error(`no terminal status after ${Math.round(FORGE_WAIT_MS / 60000)} min: ${short(last, 200)}`);
    const job = simJobId ? await api(request, "GET", `/api/sim_job/${simJobId}`) : null;
    const winner = short(await page.locator("#propose-result .propose-winner").textContent().catch(() => "?"), 80);
    simDone = true;
    return `${short(last, 60)}; ${winner}; sim_job ${job ? job.body.status : "?"} total_games=${job?.body?.report?.total_games}`;
  });

  await row(page, "A", "forge: Save iteration from the compare result", { forge: true }, async () => {
    if (!simDone) throw new SoftFail("compare did not finish; nothing to save");
    const block = page.locator(".save-iteration-block");
    await expect(block).toBeVisible();
    const suggested = short(await block.locator("p.muted", { hasText: "Suggested" }).textContent().catch(() => ""), 100);
    const saved = page.waitForResponse((r) => r.url().includes("/api/save_iteration"), { timeout: 30_000 });
    await block.getByRole("button", { name: "Save iteration" }).click();
    const resp = await saved;
    expect(resp.status()).toBe(200);
    await expect(block).toContainText(/Saved/, { timeout: 15_000 });
    await page.locator("#propose-close").click();
    return `${suggested}; save_iteration 200`;
  });

  await row(page, "A", "forge: history / verdict breakdown pick up the saved compare", { forge: true }, async () => {
    if (!simDone) throw new SoftFail("compare did not finish");
    await gotoApp(page);
    await selectDeck(page, decks.A);
    const hist = page.locator("#dashboard section.panel").filter({ hasText: "Iteration history" });
    const n = await hist.locator("li.iteration").count();
    const its = await api(request, "GET", `/api/iterations?deck=${encodeURIComponent(decks.A)}`);
    const withSim = (its.body.iterations || []).filter((it) => it.margin != null || it.win_rate_new != null).length;
    if (!withSim) throw new Error(`no iteration row carries a sim result (${n} rows)`);
    await expect(page.locator("#dashboard section.panel").filter({ hasText: "Verdict by audit version" })).toBeVisible({ timeout: 15_000 });
    return `${n} rows in history, ${withSim} with a sim margin; breakdown panel rendered`;
  });

  await row(page, "global", "replays: section after the compare lists the run and opens a game", { forge: true }, async () => {
    if (!simDone) throw new SoftFail("compare did not finish");
    await page.locator('.rail-btn[data-section="replays"]').click();
    const resp = page.waitForResponse((r) => r.url().includes("/api/replays"));
    await page.locator("#replays-refresh-btn").click();
    const body = await (await resp).json();
    if (!body.enabled) throw new SoftFail("replay capture disabled on this server (COMMANDER_BUILDER_KEEP_GAME_LOGS unset)");
    if (!body.count) throw new SoftFail("no replay runs recorded after the compare");
    const run = body.runs[0];
    const game = run.games && run.games[0];
    const g = game ? await api(request, "GET", `/api/replay/${encodeURIComponent(run.run)}/${game.game}`) : null;
    const firstGame = page.locator("#replays-run-list button, #replays-run-list li").first();
    if (await firstGame.count()) await firstGame.click().catch(() => {});
    await page.waitForTimeout(1000);
    await page.locator('.rail-btn[data-section="decks"]').click();
    return `${body.count} runs; run ${run.run} has ${run.count} games; replay API ${g ? g.status : "n/a"}`;
  });

  // ----- Phase 5: cleanup ------------------------------------------------
  await row(page, "global", "cleanup: remove the decks this run created (E2E_KEEP_DECKS=1 keeps them)", {}, async () => {
    if (KEEP_DECKS || !LIVE_APP) return `kept (${LIVE_APP ? "E2E_KEEP_DECKS=1" : "fixture server state is temporary"})`;
    const removed = [];
    for (const id of decks.created) {
      const r = await api(request, "DELETE", `/api/deck_text?deck=${encodeURIComponent(id)}`);
      if (r.status === 200) removed.push(id);
    }
    return `deleted ${removed.length}/${decks.created.length}: ${removed.join("; ")} (knowledge-log rows for them remain)`;
  });

  writeChecklist();
  const totals = rows.reduce((t, r) => ({ ...t, [r.status]: (t[r.status] || 0) + 1 }), {});
  console.log(`\n[walkthrough] ${rows.length} rows: ${JSON.stringify(totals)} -> ${path.join(RESULTS, "CHECKLIST.md")}`);
  // The checklist carries the verdict; the test itself only proves the
  // server was reachable (one PASS row on the home page).
  expect(rows.find((r) => r.page.startsWith("home:") && r.status === "PASS"), "the app never came up").toBeTruthy();
});
