// Smoke 2 — DECK EDITOR SAVE PATH.
//
// Three behaviours that the browser is the only place to observe:
//
// 1. The PUT restamps ``Name=`` to the file's own stem. Pasting deck
//    A's text into deck B's editor used to store ``Name=A`` under B's
//    filename, breaking the filename<->Name win-attribution invariant
//    every aggregation depends on. The editor must show the restamped
//    text on reopen, not the text the user typed.
// 2. ``bracket_tag_unverified`` — the server has computed this since
//    the deck_text PUT hardening and nothing rendered it, so a
//    hand-edited B4 list under a [B3] filename stayed invisible. It has
//    to reach the save-status line, and the modal has to STAY OPEN (a
//    warning that asks the user to act cannot ride on a modal that
//    just closed).
// 3. A 400 (body with no [Main] section) must surface as a visible
//    error. The failure mode being guarded against is a silent success:
//    status says nothing / says "Saved.", modal closes, deck unchanged.

const { test, expect } = require("@playwright/test");
const { DECKS, gotoApp, openEditor, selectDeck } = require("./fixtures");

test("saving edited text restamps Name= to the deck's own filename", async ({
  page,
  request,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.editorPlain);
  await openEditor(page);

  const original = await page.locator("#propose-text").inputValue();
  // Simulate the paste-from-another-deck case the restamp exists for.
  const foreign = original.replace(
    /^Name=.*$/m,
    "Name=[USER] Some Other Deck [B5]",
  );
  expect(foreign).toContain("Name=[USER] Some Other Deck [B5]");
  await page.locator("#propose-text").fill(foreign);
  await page.locator("#propose-run").click();

  // Untagged filename => no bracket warning => plain success + close.
  await expect(page.locator("#propose-status")).toHaveText("Saved.");
  await expect(page.locator("#propose-modal")).toBeHidden();

  // Reopen: the UI must reflect the restamped text, not what was typed.
  await expect(page.locator("#dashboard .commander-hero")).toBeVisible();
  await openEditor(page);
  const reopened = await page.locator("#propose-text").inputValue();
  expect(reopened).toContain(`Name=${DECKS.editorPlain}`);
  expect(reopened).not.toContain("Some Other Deck");

  // And the same on disk, via the API the rest of the pipeline reads.
  const body = await (
    await request.get(
      `/api/deck_text?deck=${encodeURIComponent(DECKS.editorPlain)}`,
    )
  ).json();
  expect(body.text).toContain(`Name=${DECKS.editorPlain}`);
});

test("a mainboard change under a [B3] filename warns in the save-status line and keeps the modal open", async ({
  page,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.editorTagged);
  await openEditor(page);

  const original = await page.locator("#propose-text").inputValue();
  // Change the mainboard composition (not just whitespace) so the
  // server's quantity-map comparison actually differs.
  const edited = original
    .replace("60 Forest", "59 Forest")
    .replace("39 Cultivate", "40 Cultivate");
  expect(edited).not.toBe(original);
  await page.locator("#propose-text").fill(edited);
  await page.locator("#propose-run").click();

  const status = page.locator("#propose-status");
  await expect(status).toContainText("was NOT re-verified");
  await expect(status).toContainText("Re-estimate the bracket");
  // Deliberately still open — the user is being asked to do something.
  await expect(page.locator("#propose-modal")).toBeVisible();
});

test("a body with no [Main] section surfaces the 400 as a visible error", async ({
  page,
  request,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.editorPlain);

  const before = await (
    await request.get(
      `/api/deck_text?deck=${encodeURIComponent(DECKS.editorPlain)}`,
    )
  ).json();

  await openEditor(page);
  // A partial paste: metadata + commander, mainboard truncated away.
  await page
    .locator("#propose-text")
    .fill("[metadata]\nName=whatever\n\n[Commander]\n1 Test Cmdr\n");
  await page.locator("#propose-run").click();

  const status = page.locator("#propose-status");
  await expect(status).toContainText("Error:");
  await expect(status).toContainText("no [Main] section");
  // The silent-success failure mode, spelled out: not "Saved.", modal
  // still open, deck untouched.
  await expect(status).not.toHaveText("Saved.");
  await expect(page.locator("#propose-modal")).toBeVisible();

  const after = await (
    await request.get(
      `/api/deck_text?deck=${encodeURIComponent(DECKS.editorPlain)}`,
    )
  ).json();
  expect(after.text).toBe(before.text);
});
