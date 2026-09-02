// Playwright config for the web smokes (tests/e2e/).
//
// WHY THIS EXISTS
// ---------------
// ``src/commander_builder/web/static/app.js`` is ~4,300 lines of
// browser code with zero automated tests. Two review rounds found bugs
// there that a page-load would never surface — a save-verdict radio
// pre-checked from an any-lead field, a deck-editor save that reported
// success on a 400, a ``bracket_tag_unverified`` flag the server
// computed and nothing rendered. Those are exactly the failures a
// handful of DOM-level smokes catch and a Python route test cannot.
//
// SCOPE / HERMETICITY
// -------------------
// The suite is deliberately small (~10 smokes) and fully offline. A
// real Flask server runs against a temp deck dir + temp knowledge DB
// (``tests/e2e/server.py``, which also blocks outbound sockets), so
// save / edit / breakdown flows are exercised end-to-end against real
// route code. Forge is never involved: the two sim endpoints are
// intercepted in the browser and replayed from a fixture whose
// ``suggested_verdict`` block was computed by the real server helper.
//
// BROWSERS
// --------
// Chromium only. These are smokes over app logic, not a rendering
// matrix; a second engine would double the runtime for near-zero
// added signal.

const os = require("node:os");
const path = require("node:path");
const { defineConfig, devices } = require("@playwright/test");

// Deterministic (not random) so the config, the webServer process and
// every test worker — three separate processes, each of which
// re-evaluates this file — agree on where the fixture state lives.
const STATE_DIR =
  process.env.CB_E2E_STATE_DIR || path.join(os.tmpdir(), "cb-web-smokes");
const PORT = Number(process.env.CB_E2E_PORT || 5199);
const BASE_URL = `http://127.0.0.1:${PORT}`;
// Windows may map python3 to a different installation (without Flask).
// Let virtual-environment users select an interpreter explicitly as well.
const PYTHON = process.env.CB_E2E_PYTHON || (process.platform === "win32" ? "python" : "python3");

process.env.CB_E2E_STATE_DIR = STATE_DIR;
process.env.CB_E2E_PORT = String(PORT);

module.exports = defineConfig({
  testDir: path.join(__dirname, "tests", "e2e"),
  testMatch: /.*\.spec\.js/,
  // Nothing in the suite is timing-sensitive beyond the propose-swap
  // poll (a fixed 2s first-poll delay in app.js), so a short timeout
  // keeps a genuine hang from eating the CI budget.
  timeout: 30_000,
  expect: { timeout: 7_000 },
  // The specs write to a SHARED deck dir + knowledge DB, so they must
  // not race each other. Each spec owns its own deck, but serial
  // execution is what makes that guarantee cheap and obvious.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: BASE_URL,
    // Traces only when something actually failed — no artifact tax on
    // a green run. Screenshots and video stay off by default for the
    // same reason.
    trace: "retain-on-failure",
    screenshot: "off",
    video: "off",
    actionTimeout: 7_000,
    navigationTimeout: 10_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: `"${PYTHON}" "${path.join("tests", "e2e", "server.py")}" --port ${PORT} --state-dir "${STATE_DIR}"`,
    url: `${BASE_URL}/api/health`,
    cwd: __dirname,
    // Always boot our own: reusing a stray dev server would run the
    // smokes against the developer's real decks and knowledge log.
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
