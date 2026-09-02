const fs = require("node:fs");
const path = require("node:path");
const { test, expect } = require("@playwright/test");
const { DECKS, gotoApp, selectDeck, simFixtures } = require("./fixtures");

async function overrideLegality(page, legality) {
  await page.route("**/api/dashboard/core**", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ json: { ...data, legality: { ...data.legality, ...legality } } });
  });
}

test("unknown legality never inherits an old green all_legal flag", async ({ page }) => {
  await gotoApp(page);
  await overrideLegality(page, {
    status: "unverified", all_legal: true, violations: [],
    unverified: [{ code: "UNKNOWN_CARD", message: "Card data is unavailable.", cards: ["Mystery Dragon"] }],
    data_warning: "Rules snapshot is stale.",
  });
  await selectDeck(page, DECKS.verdict);
  const badge = page.getByRole("button", { name: "Rules check: unverified", exact: true });
  await expect(badge).toBeVisible();
  await expect(page.locator(".legality-banner .good")).toHaveCount(0);
  await badge.click();
  await expect(page.locator("#alert-body")).toContainText("Card data is unavailable.");
  await expect(page.locator("#alert-body")).toContainText("Mystery Dragon");
  await expect(page.locator("#alert-body")).toContainText("Rules snapshot is stale.");
});

test("deck-wide violations show an issue even with zero named illegal cards", async ({ page }) => {
  await gotoApp(page);
  await overrideLegality(page, {
    status: "illegal", all_legal: false, n_illegal: 0,
    violations: [{ code: "DECK_SIZE", message: "Expected 100 cards, found 101.", cards: [] }],
    unverified: [],
  });
  await selectDeck(page, DECKS.verdict);
  await page.getByRole("button", { name: "Rules check: 1 issue", exact: true }).click();
  await expect(page.locator("#alert-body")).toContainText("Expected 100 cards, found 101.");
});

test("rules details name the affected cards for generic violation messages", async ({ page }) => {
  await gotoApp(page);
  await overrideLegality(page, {
    status: "illegal", all_legal: false,
    violations: [{ code: "BANNED_CARD", message: "Banned in Commander.", cards: ["Banned Foil Test Card"] }],
    unverified: [],
  });
  await selectDeck(page, DECKS.verdict);
  await page.getByRole("button", { name: "Rules check: 1 issue", exact: true }).click();
  await expect(page.locator("#alert-body")).toContainText("Banned in Commander.");
  await expect(page.locator("#alert-body")).toContainText("Banned Foil Test Card");
});

test("only explicit legal status earns a green badge", async ({ page }) => {
  await gotoApp(page);
  await overrideLegality(page, {
    status: "legal", all_legal: true, violations: [], unverified: [],
    data_warning: "Rules snapshot is stale.",
  });
  await selectDeck(page, DECKS.verdict);
  await expect(page.getByRole("button", { name: "Commander-legal deck", exact: true })).toHaveClass(/good/);
  await expect(page.locator(".legality-banner")).toContainText("Rules data warning");
});

test("the user's 100-card foil export imports with a selected commander", async ({ page, request }) => {
  const deckText = fs.readFileSync(path.join(__dirname, "..", "fixtures", "ur_dragon_moxfield.txt"), "utf8");
  await gotoApp(page);
  await page.getByRole("button", { name: "+ New deck" }).click();
  await page.getByRole("tab", { name: "Paste deck list" }).click();
  await page.locator("#new-paste-name").fill("Ur Dragon Export Smoke");
  await page.locator("#new-paste-text").fill(deckText);
  await page.getByLabel("Commander (optional if included in the list)", { exact: true }).fill("The Ur-Dragon");
  await page.getByRole("button", { name: "Create deck", exact: true }).click();
  await expect(page.locator("#new-deck-modal")).toBeHidden();
  const deckId = "[USER] Ur Dragon Export Smoke [B3]";
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText("The Ur-Dragon");
  const saved = await (await request.get(`/api/deck_text?deck=${encodeURIComponent(deckId)}`)).json();
  expect(saved.text).toMatch(/\[Commander\]\s+1 The Ur-Dragon(?:\||\r?\n)/);
  expect(saved.text).not.toMatch(/\*[FE]\*/);
  const quantities = [...saved.text.matchAll(/^(\d+) /gm)].map((match) => Number(match[1]));
  expect(quantities.reduce((sum, quantity) => sum + quantity, 0)).toBe(100);
  expect(saved.text).toContain("Klauth, Unrivaled Ancient");
  expect(saved.text).toContain("Mother of Runes");
});

async function seedCommanderDeck(request, name) {
  const response = await request.post("/api/import_deck", { data: {
    name,
    paste_text: "[Commander]\n1 Old Commander\n[Main]\n1 New Commander\n1 Partner Commander\n97 Forest\n",
  } });
  expect(response.ok()).toBeTruthy();
  return (await response.json()).id;
}

test("commander controls move a partner pair without losing cards", async ({ page, request }) => {
  const deckId = await seedCommanderDeck(request, "Commander Controls Smoke");
  await gotoApp(page);
  await selectDeck(page, deckId);
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Change commander", exact: true });
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("Old Commander");
  await modal.getByLabel("Commander", { exact: true }).fill("New Commander");
  await modal.getByLabel("Partner / second commander (optional)", { exact: true }).fill("Partner Commander");
  await modal.getByRole("button", { name: "Save commander", exact: true }).click();
  await expect(modal.getByRole("status")).toContainText("Saved");
  const saved = await (await request.get(`/api/deck_text?deck=${encodeURIComponent(deckId)}`)).json();
  expect(saved.text).toMatch(/\[Commander\]\s+1 New Commander\s+1 Partner Commander/);
  expect(saved.text.split("[Main]")[1]).toContain("1 Old Commander");
  expect([...saved.text.matchAll(/^(\d+) /gm)].reduce((sum, match) => sum + Number(match[1]), 0)).toBe(100);
  await modal.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("button", { name: "Change commander", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("New Commander");
  await expect(modal.getByLabel("Partner / second commander (optional)", { exact: true })).toHaveValue("Partner Commander");
});

test("a failed commander save stays open and leaves the deck unchanged", async ({ page, request }) => {
  const deckId = await seedCommanderDeck(request, "Commander Error Smoke");
  const url = `/api/deck_text?deck=${encodeURIComponent(deckId)}`;
  const before = await (await request.get(url)).json();
  await gotoApp(page);
  await selectDeck(page, deckId);
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Change commander", exact: true });
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("Old Commander");
  await page.route("**/api/deck_commander?**", async (route) => {
    if (route.request().method() === "PUT") {
      await route.fulfill({ status: 400, json: { error: "Invalid commander selection" } });
    } else await route.continue();
  });
  await modal.getByRole("button", { name: "Save commander", exact: true }).click();
  await expect(modal.getByRole("status")).toContainText("Invalid commander selection");
  await expect(modal).toBeVisible();
  expect((await (await request.get(url)).json()).text).toBe(before.text);
});

test("garbage import displays an error and creates no deck", async ({ page, request }) => {
  await gotoApp(page);
  await page.getByRole("button", { name: "+ New deck" }).click();
  await page.getByRole("tab", { name: "Paste deck list" }).click();
  await page.locator("#new-paste-name").fill("Invalid Empty Smoke");
  await page.locator("#new-paste-text").fill("Count,Name\n");
  await page.getByRole("button", { name: "Create deck", exact: true }).click();
  await expect(page.locator("#new-deck-status")).toContainText("Error:");
  await expect(page.locator("#new-deck-modal")).toBeVisible();
  const response = await request.get(`/api/deck_text?deck=${encodeURIComponent("[USER] Invalid Empty Smoke [B3]")}`);
  expect(response.status()).toBe(404);
});

test("closing during a commander save still refreshes the active deck", async ({ page, request }) => {
  const deckId = await seedCommanderDeck(request, "Close During Save Smoke");
  let releaseResponse;
  let signalResponse;
  const held = new Promise((resolve) => { releaseResponse = resolve; });
  const ready = new Promise((resolve) => { signalResponse = resolve; });
  await gotoApp(page);
  await selectDeck(page, deckId);
  await page.route("**/api/deck_commander?**", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const response = await route.fetch();
    signalResponse();
    await held;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Change commander", exact: true });
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("Old Commander");
  await modal.getByLabel("Commander", { exact: true }).fill("New Commander");
  await modal.getByRole("button", { name: "Save commander", exact: true }).click();
  await ready;
  await modal.getByRole("button", { name: "Close", exact: true }).click();
  releaseResponse();
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText("New Commander");
  await expect(modal).toBeHidden();
  await expect(page.getByRole("button", { name: "Change commander", exact: true })).toBeFocused();
});

test("a late import does not replace a newer deck selection", async ({ page }) => {
  let releaseResponse;
  let signalResponse;
  const held = new Promise((resolve) => { releaseResponse = resolve; });
  const ready = new Promise((resolve) => { signalResponse = resolve; });
  await gotoApp(page);
  await page.route("**/api/import_deck", async (route) => {
    const response = await route.fetch();
    signalResponse();
    await held;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "+ New deck" }).click();
  await page.getByRole("tab", { name: "Paste deck list" }).click();
  await page.locator("#new-paste-name").fill("Late Import Smoke");
  await page.locator("#new-paste-text").fill("[Commander]\n1 Imported Commander\n[Main]\n99 Forest\n");
  await page.getByRole("button", { name: "Create deck", exact: true }).click();
  await ready;
  await page.getByRole("dialog", { name: "Add a deck" }).getByRole("button", { name: "Close", exact: true }).click();
  await selectDeck(page, DECKS.editorPlain);
  const refreshed = page.waitForResponse((response) => response.url().includes("/api/decks?") && response.ok());
  releaseResponse();
  await refreshed;
  await expect(page.locator('#deck-list li[aria-current="true"]')).toHaveAttribute("data-id", DECKS.editorPlain);
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText("Test Cmdr");
  await expect(page.locator("#alert-modal")).toBeHidden();
});

test("changing commander invalidates an in-flight simulation", async ({ page, request }) => {
  const deckId = await seedCommanderDeck(request, "Stale Commander Sim Smoke");
  let releaseResponse;
  let signalResponse;
  const held = new Promise((resolve) => { releaseResponse = resolve; });
  const ready = new Promise((resolve) => { signalResponse = resolve; });
  await gotoApp(page);
  await selectDeck(page, deckId);
  await page.route("**/api/propose_swap_async", (route) => route.fulfill({
    json: { job_id: "old-commander-sim" },
  }));
  await page.route("**/api/sim_job/old-commander-sim", async (route) => {
    signalResponse();
    await held;
    await route.fulfill({ json: { status: "done", report: simFixtures().split_21_20 } });
  });
  await page.getByRole("button", { name: "Propose changes", exact: true }).click();
  await expect(page.locator("#propose-text")).toHaveValue(/1 Old Commander/);
  await page.locator("#propose-run").click();
  await ready;
  await page.getByRole("dialog", { name: "Propose changes", exact: true })
    .getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Change commander", exact: true });
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("Old Commander");
  await modal.getByLabel("Commander", { exact: true }).fill("New Commander");
  await modal.getByRole("button", { name: "Save commander", exact: true }).click();
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText("New Commander");
  await modal.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Propose changes", exact: true }).click();
  await expect(page.locator("#propose-text")).toHaveValue(/\[Commander\]\s+1 New Commander/);
  await expect(page.locator("#propose-run")).toBeEnabled();
  const returned = page.waitForResponse("**/api/sim_job/old-commander-sim");
  releaseResponse();
  await (await returned).finished();
  await expect(page.locator(".save-iteration-block")).toHaveCount(0);
  await expect(page.locator("#propose-status")).not.toContainText("Done.");
});

test("an inactive commander save cannot revive a simulation after returning to the deck", async ({ page, request }) => {
  const deckId = await seedCommanderDeck(request, "Inactive Commander Sim Smoke");
  let releaseSim;
  let signalSim;
  let releaseSave;
  let signalSave;
  const simHeld = new Promise((resolve) => { releaseSim = resolve; });
  const simReady = new Promise((resolve) => { signalSim = resolve; });
  const saveHeld = new Promise((resolve) => { releaseSave = resolve; });
  const saveReady = new Promise((resolve) => { signalSave = resolve; });
  await gotoApp(page);
  await selectDeck(page, deckId);
  await page.route("**/api/propose_swap_async", (route) => route.fulfill({
    json: { job_id: "inactive-commander-sim" },
  }));
  await page.route("**/api/sim_job/inactive-commander-sim", async (route) => {
    signalSim();
    await simHeld;
    await route.fulfill({ json: { status: "done", report: simFixtures().split_21_20 } });
  });
  await page.route("**/api/deck_commander?**", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    const response = await route.fetch();
    signalSave();
    await saveHeld;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "Propose changes", exact: true }).click();
  await expect(page.locator("#propose-text")).toHaveValue(/1 Old Commander/);
  await page.locator("#propose-run").click();
  await simReady;
  await page.getByRole("dialog", { name: "Propose changes", exact: true })
    .getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Change commander", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Change commander", exact: true });
  await expect(modal.getByLabel("Commander", { exact: true })).toHaveValue("Old Commander");
  await modal.getByLabel("Commander", { exact: true }).fill("New Commander");
  await modal.getByRole("button", { name: "Save commander", exact: true }).click();
  await saveReady;
  await modal.getByRole("button", { name: "Close", exact: true }).click();
  await selectDeck(page, DECKS.editorPlain);
  const saved = page.waitForResponse((response) => response.url().includes("/api/deck_commander?")
    && response.request().method() === "PUT");
  releaseSave();
  await (await saved).finished();
  await selectDeck(page, deckId);
  await expect(page.locator("#dashboard .commander-hero .name")).toHaveText("New Commander");
  await page.getByRole("button", { name: "Propose changes", exact: true }).click();
  await expect(page.locator("#propose-text")).toHaveValue(/\[Commander\]\s+1 New Commander/);
  await expect(page.locator("#propose-run")).toBeEnabled();
  const returned = page.waitForResponse("**/api/sim_job/inactive-commander-sim");
  releaseSim();
  await (await returned).finished();
  await expect(page.locator(".save-iteration-block")).toHaveCount(0);
  await expect(page.locator("#propose-status")).not.toContainText("Done.");
});
