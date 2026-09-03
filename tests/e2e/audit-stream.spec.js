// Smoke 5 — THE SSE AUDIT STREAM (R3 W-13, 2026-09-03).
//
// Decision B3 promised "verdict/save/SSE paths"; the four earlier specs
// covered the first two and never touched ``/api/audit/stream``, which
// ``app.js`` drives through ``audit_streaming.js``'s hand-rolled SSE
// reader (fetch + frame parser, because EventSource cannot carry the
// BYO-key header). This spec intercepts the stream in the browser and
// replays a scripted sequence of frames — diagnosis → manabase →
// primary → complete — so the parser, the per-phase progress text and
// the final render are exercised with no advisor, no EDHREC and no
// network. A second test replays an ``error`` frame and checks the
// failure lands in the panel instead of vanishing.
//
// The frames below use the exact wire format ``routes_audit._sse``
// emits (``event: <name>\ndata: <one-line JSON>\n\n``).

const { test, expect } = require("@playwright/test");
const { DECKS, gotoApp, selectDeck } = require("./fixtures");

function sse(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** The minimal ``complete`` payload ``renderAuditResult`` renders. */
function completeBody() {
  return {
    source: "heuristic",
    audit_version: "v3",
    bracket: 3,
    diagnosis: "Stub diagnosis: the stream reached the renderer.",
    weakness_signals: [],
    added: [{ card: "Cultivate", rationale: "stub add", match_pct: 50 }],
    removed: [{ card: "Forest", rationale: "stub cut" }],
    proposed_text: "[Main]\n59 Forest\n41 Cultivate\n",
    warning: null,
    salt_warning: null,
    deck_score: null,
    deck_health: null,
    unknown_card_count: 0,
    protected_cards: [],
    main_count: 99,
    basics_padded: 0,
    average_deck_preview: null,
    combo_assessment: null,
    original_price_usd: null,
    proposed_price_usd: null,
  };
}

async function mockStream(page, frames) {
  const seen = { urls: [] };
  await page.route("**/api/audit/stream**", async (route) => {
    seen.urls.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: frames.join(""),
    });
  });
  return seen;
}

test.beforeEach(async ({ page }) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.verdict);
});

test("a scripted SSE stream drives the progress phases and the final render", async ({
  page,
}) => {
  const seen = await mockStream(page, [
    sse("diagnosis", { commander_names: ["Test Cmdr"], diagnosis: {} }),
    sse("manabase", { recommendations: [], tribe: null }),
    sse("primary", { source: "heuristic", recommendations: [] }),
    sse("complete", completeBody()),
  ]);

  await page.getByRole("button", { name: "Run audit" }).click();

  const panel = page.locator("#sug-panel");
  // The final render replaces the progress paragraph; the stream is
  // replayed in one chunk, so assert on the END state and on the
  // request that produced it.
  await expect(panel.locator("h3")).toHaveText("Audit — full proposed deck");
  await expect(panel).toContainText("Stub diagnosis: the stream reached the renderer.");
  await expect(panel).toContainText("Cultivate");
  expect(seen.urls).toHaveLength(1);
  expect(seen.urls[0]).toContain("/api/audit/stream?deck=");
  expect(seen.urls[0]).toContain(encodeURIComponent(DECKS.verdict));
  expect(seen.urls[0]).toContain("source=heuristic");
});

test("an error frame ends the stream and the panel says why", async ({
  page,
}) => {
  await mockStream(page, [
    sse("diagnosis", { commander_names: ["Test Cmdr"], diagnosis: {} }),
    sse("error", { error: "stub advisor failure", detail: "RuntimeError" }),
  ]);

  await page.getByRole("button", { name: "Run audit" }).click();

  const panel = page.locator("#sug-panel");
  await expect(panel).toContainText("Audit failed: stub advisor failure");
  // A failed stream must never leave the "Generating…" status behind.
  await expect(panel).not.toContainText("Generating ideal deck");
});

test("a stream that ends without a complete frame is reported, not hung", async ({
  page,
}) => {
  await mockStream(page, [
    sse("diagnosis", { commander_names: ["Test Cmdr"], diagnosis: {} }),
    sse("manabase", { recommendations: [], tribe: null }),
  ]);

  await page.getByRole("button", { name: "Run audit" }).click();

  const panel = page.locator("#sug-panel");
  await expect(panel).toContainText(
    "Audit failed: audit stream ended without complete event",
  );
});
