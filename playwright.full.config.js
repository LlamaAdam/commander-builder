// Playwright config for the FULL WALKTHROUGH lane
// (tests/e2e/full-walkthrough.spec.js) — 2026-10-02.
//
// WHY A SECOND CONFIG
// -------------------
// ``playwright.config.js`` is the hermetic smokes lane: 30 s per test,
// a stubbed offline server, six synthetic decks. The walkthrough is the
// opposite — two real decks, live imports, a real Forge compare that
// can run for many minutes — and it has to be able to point at an app
// that is ALREADY running on the owner's machine. Those knobs
// (per-test timeout, the webServer block, the browser channel) cannot
// be shared without making the smokes slower or less hermetic, so this
// lane owns its own file. The smokes config ignores the walkthrough
// spec for the same reason.
//
// ENVIRONMENT
// -----------
// E2E_BASE_URL  Point at an app that is already running (the owner's
//               desktop launcher / `python -m commander_builder.web`).
//               When set, no fixture server is started.
// E2E_CHROME=1  Use the installed Google Chrome (channel "chrome"),
//               headed, so a local run needs no browser download.
// E2E_OFFLINE=1 Sandbox posture: the spec marks network / Forge rows as
//               skipped and the fixture server stubs card lookups.

const os = require("node:os");
const path = require("node:path");
const { defineConfig, devices } = require("@playwright/test");

const STATE_DIR =
  process.env.CB_E2E_FULL_STATE_DIR || path.join(os.tmpdir(), "cb-e2e-full");
const PORT = Number(process.env.CB_E2E_FULL_PORT || 5299);
const BASE_URL = process.env.E2E_BASE_URL || `http://127.0.0.1:${PORT}`;
const USE_CHROME = process.env.E2E_CHROME === "1";
// The CI lane hands Forge's own deck folder in; a local fixture run
// uses a temp dir (no Forge rows there anyway).
const DECK_DIR = process.env.CB_E2E_FULL_DECK_DIR || "";

process.env.CB_E2E_FULL_STATE_DIR = STATE_DIR;
process.env.CB_E2E_FULL_PORT = String(PORT);

const serverCommand = [
  `python3 ${path.join("tests", "e2e", "server_full.py")}`,
  `--port ${PORT}`,
  `--state-dir "${STATE_DIR}"`,
  DECK_DIR ? `--deck-dir "${DECK_DIR}"` : "",
].filter(Boolean).join(" ");

module.exports = defineConfig({
  testDir: path.join(__dirname, "tests", "e2e"),
  testMatch: /full-walkthrough\.spec\.js/,
  // One test, one deliberately long budget: the real Forge compare is
  // bounded inside the spec (it records a FAIL row and moves on), so
  // this is a backstop against a hung browser, not a per-row limit.
  timeout: 40 * 60 * 1000,
  expect: { timeout: 10_000 },
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  // Never retry: a retry would re-import deck B (politeness budget) and
  // re-run Forge. The checklist is the result, not the pass/fail bit.
  retries: 0,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  outputDir: path.join(__dirname, "test-results", "full-walkthrough"),
  use: {
    baseURL: BASE_URL,
    headless: !USE_CHROME,
    channel: USE_CHROME ? "chrome" : undefined,
    // Screenshots are taken by the spec itself (one per row, viewport
    // sized, into e2e-results/); Playwright's own stay off.
    trace: "off",
    screenshot: "off",
    video: "off",
    viewport: { width: 1280, height: 800 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: USE_CHROME ? "chrome" : "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: serverCommand,
        url: `${BASE_URL}/api/health`,
        cwd: __dirname,
        reuseExistingServer: false,
        timeout: 120_000,
        stdout: "pipe",
        stderr: "pipe",
      },
});
