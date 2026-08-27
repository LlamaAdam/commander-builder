// Smoke 3 — ERROR PATHS.
//
// Two things that are invisible on a happy path:
//
// 1. The innerHTML hardening. Five error paths used to build their
//    message with ``node.innerHTML = `<p class="muted">… ${e.message}</p>` ``,
//    interpolating a caught Error straight into markup. They now go
//    through ``setMessage``, which builds a text node. Asserted both
//    ways: the real failed-dashboard path renders a plain <p>, and
//    ``setMessage`` itself refuses to materialise markup handed to it.
// 2. The JS error collector. A silent uncaught error is precisely the
//    class of bug this whole suite exists for, so the sink that catches
//    them must itself be covered — POST to /api/log_error, and the
//    returned ref stashed where a devtools session can find it.

const { test, expect } = require("@playwright/test");
const { DECKS, cssEscape, gotoApp } = require("./fixtures");

test("a failed dashboard load renders a plain-text error, not markup", async ({
  page,
}) => {
  await gotoApp(page);
  await page.route("**/api/dashboard/core**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "boom" }),
    }),
  );

  await page
    .locator(`#deck-list li[data-id="${cssEscape(DECKS.verdict)}"]`)
    .click();

  const msg = page.locator("#dashboard p.empty-state");
  await expect(msg).toBeVisible();
  await expect(msg).toContainText("Error loading:");
  // The whole dashboard is that one paragraph — no half-rendered panels,
  // and nothing else got injected alongside it.
  await expect(page.locator("#dashboard > *")).toHaveCount(1);

  // Direct check on the helper the path now uses: markup in the message
  // must stay text. ``setMessage`` is a top-level function in a classic
  // script, so it is reachable on window.
  const probe = await page.evaluate(() => {
    if (typeof window.setMessage !== "function") {
      return { missing: true };
    }
    const host = document.createElement("div");
    document.body.appendChild(host);
    const payload = '<img src=x onerror="window.__pwned = 1">';
    window.setMessage(host, "muted", `Error loading: ${payload}`);
    const out = {
      missing: false,
      imgCount: host.querySelectorAll("img").length,
      childCount: host.children.length,
      tag: host.firstElementChild && host.firstElementChild.tagName,
      text: host.textContent,
      pwned: !!window.__pwned,
    };
    host.remove();
    return out;
  });
  expect(probe.missing, "app.js no longer exposes setMessage").toBe(false);
  expect(probe.imgCount).toBe(0);
  expect(probe.pwned).toBe(false);
  expect(probe.childCount).toBe(1);
  expect(probe.tag).toBe("P");
  expect(probe.text).toContain("<img src=x");
});

test("an uncaught JS error is POSTed to /api/log_error and the ref is stashed", async ({
  page,
}) => {
  await gotoApp(page);

  const posted = page.waitForRequest(
    (req) =>
      req.url().includes("/api/log_error") && req.method() === "POST",
  );
  // Thrown from a timer so it escapes the evaluate() call and lands in
  // window.onerror — the real shape of the failures this sink exists
  // for (the "Run A/B did nothing" TDZ ReferenceError).
  await page.evaluate(() => {
    setTimeout(() => {
      throw new Error("smoke-boom-uncaught");
    }, 0);
  });

  const req = await posted;
  const body = JSON.parse(req.postData() || "{}");
  expect(body.kind).toBe("error");
  expect(body.message).toContain("smoke-boom-uncaught");
  expect(body.url).toContain("127.0.0.1");
  expect(typeof body.stack).toBe("string");

  // The real route answered with a ref token, and app.js kept it where
  // a later devtools session can read it back.
  await expect
    .poll(() => page.evaluate(() => window.__lastJsErrorRef || null), {
      timeout: 7_000,
    })
    .not.toBeNull();
  const ref = await page.evaluate(() => window.__lastJsErrorRef);
  // "<timestamp>-<4 hex>" as built by routes_meta.log_error.
  expect(ref).toMatch(/^\d{8}T\d+-[0-9a-f]{4}$/);
});
