// Smoke 6 — SETTINGS: a collection pasted right after the dialog opens
// must survive the asynchronous state refresh and be imported on Save.
//
// Found 2026-10-02 by the full-walkthrough lane (CI run 37051424339):
// openSettings() refreshes /api/collection asynchronously, and the
// refresh used to blank the textarea unconditionally, so a paste made
// before the GET returned was wiped and Save imported nothing -- while
// the dialog still said "saved" and closed. The race is forced here by
// delaying the GET until after the paste and the click.

const { test, expect } = require("@playwright/test");
const { gotoApp } = require("./fixtures");

test("a collection pasted before the state refresh returns is still imported on Save", async ({ page }) => {
  await gotoApp(page);
  // Hold the opening GET /api/collection until the paste + Save have happened.
  let releaseGet;
  const held = new Promise((resolve) => { releaseGet = resolve; });
  let gets = 0;
  await page.route("**/api/collection", async (route) => {
    if (route.request().method() === "GET" && gets++ === 0) {
      await held;
    }
    await route.continue();
  });
  const putSeen = page.waitForRequest(
    (r) => r.url().includes("/api/collection") && r.method() === "PUT",
    { timeout: 10_000 },
  );
  await page.locator("#btn-settings").click();
  await page.locator("#settings-collection").fill("1 Sol Ring\n1 Arcane Signet\nLightning Bolt\n");
  await page.locator("#settings-save").click();
  // Now let the delayed refresh land -- before the fix this blanked the box.
  releaseGet();
  const put = await putSeen;
  expect(put.postDataJSON().text).toContain("Arcane Signet");
  await expect(page.locator("#settings-collection-state")).toHaveText(/3 cards/);
  await expect(page.locator("#settings-collection")).toHaveValue("");
  await expect(page.locator("#settings-dialog")).toBeHidden({ timeout: 5_000 });
});

test("refreshing the collection state never blanks text the user has typed", async ({ page }) => {
  await gotoApp(page);
  const firstGet = page.waitForResponse((r) => r.url().includes("/api/collection") && r.request().method() === "GET");
  await page.locator("#btn-settings").click();
  await firstGet;
  await page.locator("#settings-collection").fill("1 Sol Ring\n");
  // A second refresh (what opening/closing or a clear-state call does)
  await page.evaluate(() => fetch("/api/collection").then(() => {}));
  await page.waitForTimeout(300);
  await expect(page.locator("#settings-collection")).toHaveValue("1 Sol Ring\n");
  await page.locator("#settings-cancel").click();
});
