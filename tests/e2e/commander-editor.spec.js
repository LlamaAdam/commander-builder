// Missing-commander import repair: the browser must expose a direct path from
// a commanderless dashboard to a valid [Commander] section without asking a
// non-technical user to edit Forge's .dck format by hand.

const { test, expect } = require("@playwright/test");
const { DECKS, gotoApp, selectDeck } = require("./fixtures");

test.beforeEach(async ({ request }) => {
  const reset = await request.post("/api/e2e/reset_commanderless", {
    data: {},
  });
  expect(reset.ok()).toBeTruthy();
});

test("a commanderless imported deck can choose its commander from the dashboard", async ({
  page,
  request,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.commanderless);

  await page.getByRole("button", { name: "Change commander" }).click();
  const modal = page.locator("#commander-modal");
  await expect(modal).toBeVisible();
  await expect(page.locator("#commander-select")).toContainText(
    "Dragon Candidate",
  );
  await page.locator("#commander-select").selectOption({
    label: "Dragon Candidate",
  });

  const update = page.waitForResponse(
    (r) =>
      r.url().includes("/api/deck_commander") &&
      r.request().method() === "PUT",
  );
  await page.locator("#commander-save").click();
  expect((await update).status()).toBe(200);

  await expect(modal).toBeHidden();
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText(
    "Dragon Candidate",
  );

  const body = await (
    await request.get(
      `/api/deck_text?deck=${encodeURIComponent(DECKS.commanderless)}`,
    )
  ).json();
  expect(body.text).toContain(
    "[Commander]\n1 Dragon Candidate|TST|1\n",
  );
  expect(body.text.match(/Dragon Candidate/g)).toHaveLength(1);
});

test("closing and reopening ignores the first commander request", async ({
  page,
}) => {
  let commanderGets = 0;
  let releaseFirst;
  const firstCanFinish = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  await page.route("**/api/deck_commander?*", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    commanderGets += 1;
    if (commanderGets === 1) {
      await firstCanFinish;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "x-e2e-stale-commander": "1" },
        body: JSON.stringify({
          deck: DECKS.commanderless,
          commanders: [],
          candidates: ["Stale Card"],
        }),
      });
      return;
    }
    await route.continue();
  });

  await gotoApp(page);
  await selectDeck(page, DECKS.commanderless);
  await page.getByRole("button", { name: "Change commander" }).click();
  await page.locator("#commander-modal .modal-close").click();
  await page.getByRole("button", { name: "Change commander" }).click();
  await expect(page.locator("#commander-select")).toContainText(
    "Dragon Candidate",
  );

  const staleResponse = page.waitForResponse(
    (response) => response.headers()["x-e2e-stale-commander"] === "1",
  );
  releaseFirst();
  await staleResponse;
  await page.evaluate(() => new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }));
  await expect(page.locator("#commander-select")).not.toContainText(
    "Stale Card",
  );
  await expect(page.locator("#commander-modal")).toBeVisible();
});
