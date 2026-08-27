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
//    just closed). Since 2026-08-20 it also has to SURVIVE the next
//    click and LOOK like a warning — see the two tests below.
// 3. A 400 (body with no [Main] section) must surface as a visible
//    error. The failure mode being guarded against is a silent success:
//    status says nothing / says "Saved.", modal closes, deck unchanged.

const { test, expect } = require("@playwright/test");
const {
  DECKS,
  cssEscape,
  gotoApp,
  openEditor,
  selectDeck,
} = require("./fixtures");

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

  // SEVERITY (2026-08-20). The warning used to render in `.muted` — the
  // same grey as the "Saving…"/"Saved." chatter it replaces — so the one
  // status the user must act on was typographically identical to the two
  // they are meant to ignore. It gets its own class; `.muted` is now
  // exclusively routine.
  await expect(status).toHaveClass("status-warn");
  await expect(status).not.toHaveClass("muted");

  // PERSISTENCE (2026-08-20). THE regression this test exists for: the
  // user's most natural next click was "Save changes" again. The server
  // then compared the freshly written text against the identical
  // submitted text, found the mainboard unchanged, answered
  // bracket_tag_unverified:false — and the status flipped to "Saved."
  // and the modal closed, leaving the deck with an unverified [B3] tag
  // and no warning at all. Two clicks, because one is exactly what the
  // old per-request derivation survived.
  for (let i = 0; i < 2; i++) {
    // Wait on the PUT itself: the status line still carries the previous
    // save's warning, so a bare toContainText could pass before the new
    // response even lands (green for the wrong reason).
    const put = page.waitForResponse(
      (r) =>
        r.url().includes("/api/deck_text") &&
        r.request().method() === "PUT",
    );
    await page.locator("#propose-run").click();
    expect((await (await put).json()).bracket_tag_unverified).toBe(true);
    await expect(status).toContainText("was NOT re-verified");
    await expect(status).toHaveClass("status-warn");
    await expect(page.locator("#propose-modal")).toBeVisible();
  }

  // The soft dashboard refresh each save kicks off must not blank the
  // sidebar selection for the deck it just saved.
  await expect(
    page.locator(`#deck-list li[data-id="${cssEscape(DECKS.editorTagged)}"]`),
  ).toHaveAttribute("aria-current", "true");
});

test("the unverified bracket tag is still flagged after closing and reopening the editor", async ({
  page,
  request,
}) => {
  await gotoApp(page);
  await selectDeck(page, DECKS.editorTagged2);
  await openEditor(page);

  const original = await page.locator("#propose-text").inputValue();
  await page
    .locator("#propose-text")
    .fill(original.replace("60 Forest", "58 Forest").replace("39 Cultivate", "41 Cultivate"));
  await page.locator("#propose-run").click();
  await expect(page.locator("#propose-status")).toContainText(
    "was NOT re-verified",
  );

  // The marker lives in the deck's own [metadata], which is what lets it
  // outlive the one response that used to carry it. Read it back through
  // the API the rest of the pipeline reads.
  const body = await (
    await request.get(
      `/api/deck_text?deck=${encodeURIComponent(DECKS.editorTagged2)}`,
    )
  ).json();
  expect(body.text).toContain("BracketUnverified=3");
  expect(body.bracket_tag_unverified).toBe(true);
  // ...and outside [Main], so it can never read as a mainboard change.
  expect(body.text.indexOf("BracketUnverified=")).toBeLessThan(
    body.text.indexOf("[Main]"),
  );

  // Close the modal entirely and come back: the warning is still there,
  // still styled as a warning, before the user has touched anything.
  await page.locator("#propose-close").click();
  await expect(page.locator("#propose-modal")).toBeHidden();
  await openEditor(page);
  const status = page.locator("#propose-status");
  await expect(status).toContainText("was NOT re-verified");
  await expect(status).toHaveClass("status-warn");
});

test("routine save chatter keeps the muted styling", async ({ page }) => {
  // The other half of the severity split: an ordinary save on an
  // untagged deck must NOT borrow the warning styling.
  await gotoApp(page);
  await selectDeck(page, DECKS.editorPlain);
  await openEditor(page);

  const original = await page.locator("#propose-text").inputValue();
  await page.locator("#propose-text").fill(original);
  await page.locator("#propose-run").click();

  const status = page.locator("#propose-status");
  await expect(status).toHaveText("Saved.");
  await expect(status).toHaveClass("muted");
  await expect(status).not.toHaveClass("status-warn");
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
