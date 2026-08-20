// Smoke 1 — VERDICT DEFAULT + SAVE PAYLOAD.
//
// The regression this pins down (2026-08-20): the save-verdict radio
// used to be defaulted in JavaScript from ComparisonReport.winner, an
// ANY-lead field. A 21-20 split over 41 decisive games (exact two-sided
// p = 1.000) therefore pre-checked "Kept (apply changes)", and
// save_iteration writes the checked radio verbatim into a row stamped
// with the CURRENT measurement era — so accepting the default quietly
// fed pre-significance-fix labels into the post-fix training pool. The
// default now comes from the server's ``suggested_verdict``.
//
// The mocked reports carry a ``suggested_verdict`` block computed by
// the REAL ``web._helpers.suggested_verdict`` at server boot, so these
// assertions track production's rule rather than a hand-copied p-value.

const { test, expect } = require("@playwright/test");
const {
  DECKS,
  gotoApp,
  mockSim,
  runMockedSim,
  selectDeck,
  simFixtures,
} = require("./fixtures");

const FIXTURES = simFixtures();

test.beforeEach(async ({ page }) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.verdict);
});

test("a 21-20 split defaults the verdict radio to Neutral, not Kept", async ({
  page,
}) => {
  await mockSim(page, FIXTURES.split_21_20);
  await runMockedSim(page);

  const block = page.locator(".save-iteration-block");
  await expect(
    block.locator('input[name="save-verdict"][value="neutral"]'),
  ).toBeChecked();
  // The exact shape of the old bug: any-lead said "old deck ahead", the
  // pre-fix code turned that into a kept/reverted pre-selection.
  await expect(
    block.locator('input[name="save-verdict"][value="kept"]'),
  ).not.toBeChecked();
  await expect(
    block.locator('input[name="save-verdict"][value="reverted"]'),
  ).not.toBeChecked();
});

test("the Neutral default is justified with a p-value hint", async ({
  page,
}) => {
  await mockSim(page, FIXTURES.split_21_20);
  await runMockedSim(page);

  // A pre-checked label with no stated basis is how the old default
  // went unnoticed; the hint is what makes overriding it deliberate.
  const hint = page.locator(".save-iteration-block p.muted", {
    hasText: "Suggested:",
  });
  await expect(hint).toBeVisible();
  const text = await hint.innerText();
  expect(text).toContain("Suggested: neutral");
  expect(text).toContain("p=1.000");
  expect(text).toContain("α=0.05");
  expect(text).toContain("41 decisive games");
});

test("a 15-30 split defaults the verdict radio to Kept", async ({ page }) => {
  await mockSim(page, FIXTURES.split_15_30);
  await runMockedSim(page);

  const block = page.locator(".save-iteration-block");
  await expect(
    block.locator('input[name="save-verdict"][value="kept"]'),
  ).toBeChecked();
  const hint = page.locator(".save-iteration-block p.muted", {
    hasText: "Suggested:",
  });
  expect(await hint.innerText()).toContain("p=0.036");
});

test("saving the suggested verdict persists verdict_params and does not flag an override", async ({
  page,
  request,
}) => {
  await mockSim(page, FIXTURES.split_21_20);
  await runMockedSim(page);

  const block = page.locator(".save-iteration-block");
  const posted = page.waitForRequest(
    (req) =>
      req.url().includes("/api/save_iteration") && req.method() === "POST",
  );
  await block.getByRole("button", { name: "Save iteration" }).click();

  // The client half of the contract: it echoes the server's suggestion
  // back inside sim_report, which is what lets save_iteration decide
  // whether the human overrode it instead of re-scoring blind.
  const sent = JSON.parse((await posted).postData() || "{}");
  expect(sent.verdict).toBe("neutral");
  expect(sent.sim_report.suggested_verdict.verdict).toBe("neutral");

  const status = block.locator("div.muted").last();
  await expect(status).toContainText("Saved iteration #");
  const id = await savedIterationId(status);

  // ...and the server half. ``verdict_params`` /
  // ``verdict_overrides_suggestion`` are stamped INSIDE save_iteration,
  // so they exist nowhere in the outbound payload — the row has to be
  // read back to know what actually landed in knowledge_log.
  const row = await (await request.get(`/api/iteration/${id}`)).json();
  expect(row.verdict).toBe("neutral");
  const sim = row.sim_report;
  expect(sim.verdict_params).toBeTruthy();
  expect(sim.verdict_params).toMatchObject({ alpha: 0.05, min_decisive: 20 });
  expect(sim.suggested_verdict.verdict).toBe("neutral");
  expect(sim.verdict_overrides_suggestion).toBe(false);
});

test("overriding the suggested verdict flags verdict_overrides_suggestion", async ({
  page,
  request,
}) => {
  await mockSim(page, FIXTURES.split_21_20);
  await runMockedSim(page);

  const block = page.locator(".save-iteration-block");
  // The user disagrees with "neutral" and adopts the swap anyway. That
  // is allowed — it just has to be recorded as a human override rather
  // than pooled with the significance-tested labels.
  await block.locator('input[name="save-verdict"][value="kept"]').check();
  await block.locator("#save-iteration-notes").fill("smoke override");
  await block.getByRole("button", { name: "Save iteration" }).click();

  const status = block.locator("div.muted").last();
  await expect(status).toContainText("Saved iteration #");
  const id = await savedIterationId(status);

  const row = await (await request.get(`/api/iteration/${id}`)).json();
  expect(row.verdict).toBe("kept");
  expect(row.verdict_notes).toBe("smoke override");
  expect(row.sim_report.verdict_params).toBeTruthy();
  expect(row.sim_report.verdict_overrides_suggestion).toBe(true);
});

/** Pull the row id out of "Saved iteration #12 (verdict: neutral). …". */
async function savedIterationId(statusLocator) {
  const text = await statusLocator.innerText();
  const m = text.match(/Saved iteration #(\d+)/);
  expect(m, `no iteration id in save status: ${text}`).not.toBeNull();
  return Number(m[1]);
}
