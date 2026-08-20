// Shared helpers for the web smokes.
//
// Everything here is about getting the page into a known state cheaply:
// the app auto-kicks a heuristic audit on deck select and lazily fetches
// card art, neither of which any smoke asserts on, so both are switched
// off up front. What a spec DOES assert on is left strictly alone.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { expect } = require("@playwright/test");

const STATE_DIR =
  process.env.CB_E2E_STATE_DIR || path.join(os.tmpdir(), "cb-web-smokes");

/** Deck ids seeded by tests/e2e/server.py. Keep in sync with its DECKS. */
const DECKS = {
  verdict: "[USER] Smoke Alpha [B3]",
  editorTagged: "[USER] Smoke Bravo [B3]",
  editorPlain: "[USER] Smoke Charlie",
  eraMix: "[USER] Era Mix [B3]",
  eraPure: "[USER] Era Pure [B3]",
};

/**
 * The prepared /api/propose_swap reports, written at server boot with a
 * ``suggested_verdict`` block from the real ``web._helpers`` code.
 */
function simFixtures() {
  const p = path.join(STATE_DIR, "sim-fixtures.json");
  if (!fs.existsSync(p)) {
    throw new Error(
      `missing ${p} — the e2e fixture server writes it at boot; is ` +
        "CB_E2E_STATE_DIR consistent between the webServer and the workers?",
    );
  }
  return JSON.parse(fs.readFileSync(p, "utf-8"));
}

/**
 * Open the app with the noisy background work disabled.
 *
 * - auto-audit-on-load is a localStorage pref; off means selecting a
 *   deck doesn't fire /api/advise (which no smoke asserts on).
 * - card art is aborted at the browser: the fixture server has no
 *   network, so every /api/card_image request would otherwise burn a
 *   round-trip to fail.
 */
async function gotoApp(page) {
  await page.addInitScript(() => {
    try {
      localStorage.setItem("auto_audit_on_dashboard_load", "0");
    } catch (_e) {
      /* ignore */
    }
  });
  await page.route("**/api/card_image/**", (route) => route.abort());
  await page.goto("/");
  await expect(page.locator("#deck-list li").first()).toBeVisible();
}

/** Click a deck in the sidebar and wait for its dashboard to paint. */
async function selectDeck(page, deckId) {
  await page.locator(`#deck-list li[data-id="${cssEscape(deckId)}"]`).click();
  await expect(page.locator("#dashboard .commander-hero")).toBeVisible();
}

/**
 * Replay a prepared A/B report through the async sim contract.
 *
 * app.js POSTs /api/propose_swap_async for a job id, then polls
 * /api/sim_job/<id>. Both are intercepted, so Forge is never involved
 * and the browser sees exactly the body a completed real run produces.
 * Returns a getter for the request body app.js actually posted, so a
 * spec can assert on what the UI sent.
 */
async function mockSim(page, report) {
  const seen = { startBody: null };
  await page.route("**/api/propose_swap_async", async (route) => {
    seen.startBody = JSON.parse(route.request().postData() || "{}");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ job_id: "smoke-job-1" }),
    });
  });
  await page.route("**/api/sim_job/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ status: "done", report }),
    });
  });
  return seen;
}

/** Run the mocked A/B sim from an already-open dashboard. */
async function runMockedSim(page) {
  await page.getByRole("button", { name: "Propose changes" }).click();
  await expect(page.locator("#propose-modal")).toBeVisible();
  // openProposeModal seeds the textarea from /api/deck_text; wait for
  // the real text so the POST isn't sent while it still says "Loading…".
  await expect(page.locator("#propose-text")).toHaveValue(/\[Main\]/);
  await page.locator("#propose-run").click();
  // app.js waits a fixed 2s before its first poll, so give the
  // save-block render room beyond the default expect timeout.
  await expect(page.locator(".save-iteration-block")).toBeVisible({
    timeout: 20_000,
  });
}

/** Open the Edit-deck (save-only) modal with its text loaded. */
async function openEditor(page) {
  await page.getByRole("button", { name: "Edit deck" }).click();
  await expect(page.locator("#propose-modal")).toBeVisible();
  await expect(page.locator("#propose-title")).toHaveText("Edit deck");
  await expect(page.locator("#propose-text")).toHaveValue(/\[Main\]/);
}

/** Minimal CSS.escape for the bracketed deck ids used as data-id values. */
function cssEscape(value) {
  return value.replace(/([[\]"\\])/g, "\\$1");
}

module.exports = {
  DECKS,
  STATE_DIR,
  cssEscape,
  gotoApp,
  mockSim,
  openEditor,
  runMockedSim,
  selectDeck,
  simFixtures,
};
