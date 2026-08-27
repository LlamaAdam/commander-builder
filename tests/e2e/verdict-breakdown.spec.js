// Smoke 4 — VERDICT BREAKDOWN ERA SUB-LINE.
//
// The per-version "kept N/M" pills pool every measurement era, and a
// 'kept' does not mean the same thing in each (era 3 = |margin| >= 4,
// era 4 = a significant binomial test). When a version's rows straddle
// eras, the pooled ratio is an average of incompatible labels, so the
// UI has to print the split underneath it. When every row is already in
// the comparable era there is nothing to disclose and the sub-line must
// stay away — a permanent caveat on clean data is noise, and noise is
// what gets ignored the day it matters.
//
// Both decks are seeded by tests/e2e/server.py through the real
// knowledge_log writer, so this exercises the actual
// /api/verdict_breakdown projection rather than a hand-rolled response.

const { test, expect } = require("@playwright/test");
const { DECKS, gotoApp, selectDeck } = require("./fixtures");

/** The "Verdict by audit version" panel, scoped away from the
 *  iteration-history list which uses the same row markup. */
function breakdownPanel(page) {
  return page
    .locator("#dashboard section.panel")
    .filter({ hasText: "Verdict by audit version" });
}

test("a deck whose rows span eras renders the era sub-line", async ({
  page,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.eraMix);

  const panel = breakdownPanel(page);
  await expect(panel).toBeVisible();

  const note = panel.locator("li.muted");
  await expect(note).toBeVisible();
  const text = await note.innerText();
  // Seeded: 2 kept / 3 total in era 3, 1 kept / 2 total in era 4.
  expect(text).toContain("era 3: 2/3 kept");
  expect(text).toContain("era 4: 1/2 kept");
  expect(text).toContain("only era 4 verdicts are significance-tested");

  // The pooled pill is still there — the sub-line sits beside it, so
  // the pooled number is never the only number on screen.
  await expect(panel.locator(".iteration .delta")).toHaveText("3/5 kept (60%)");
});

test("an all-era-4 deck renders the breakdown without an era sub-line", async ({
  page,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.eraPure);

  const panel = breakdownPanel(page);
  // The panel itself must render — otherwise "no sub-line" would pass
  // trivially on a breakdown that never loaded at all.
  await expect(panel).toBeVisible();
  await expect(panel.locator(".iteration .delta")).toHaveText("2/5 kept (40%)");
  await expect(panel.locator("li.muted")).toHaveCount(0);
});
