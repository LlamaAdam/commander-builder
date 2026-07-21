# Moxfield bookmarklet → bulk import

A one-click way to grab all of your public Moxfield decks and feed them
into commander-builder's existing `Bulk URLs` import tab. Avoids the
need for a server-side user-listing endpoint (Moxfield's public API
doesn't expose deck listings by username — see [HANDOFF.md][1]).

## What it does

1. Runs on any `https://moxfield.com/users/<name>` page.
2. Scrapes every `/decks/<id>` link from the rendered DOM.
3. De-duplicates and copies the URL list to your clipboard.
4. Tells you to paste it into commander-builder's `+ New deck → Bulk URLs` tab.

## Install

Create a new bookmark in your browser (any name, e.g. "Moxfield → CB").
Set the URL field to the single-line `javascript:` payload below.

```
javascript:(function(){if(!/moxfield\.com\/users\//.test(location.href)){alert("Run this on a Moxfield user page (https://moxfield.com/users/<name>).");return;}var anchors=Array.from(document.querySelectorAll('a[href*="/decks/"]'));var urls=Array.from(new Set(anchors.map(function(a){return a.href;}).filter(function(h){return!h.includes("?")&&!/\/decks\/(public|personal)$/.test(h);}).filter(function(h){return /\/decks\/[A-Za-z0-9_-]{8,}$/.test(h);})));if(urls.length===0){alert("No deck URLs found. Make sure you're on a user page with public decks visible.");return;}var text=urls.join("\n");if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(function(){alert("Copied "+urls.length+" deck URL(s) to clipboard.\n\nNext: open commander-builder, click \"+ New deck\" → \"Bulk URLs\" → paste → \"Import all\".");},function(){_fallback(text);});}else{_fallback(text);}function _fallback(t){var ta=document.createElement("textarea");ta.value=t;ta.style.cssText="position:fixed;top:50px;left:50px;width:640px;height:400px;z-index:99999;background:#222;color:#fff;padding:10px;border:2px solid #555;font-family:monospace;font-size:12px;";document.body.appendChild(ta);ta.select();alert("Clipboard write blocked. Select-all & copy from the textarea, then close it (Esc or click outside).");}})();
```

## Use it

1. Navigate to your Moxfield user page (e.g. `https://moxfield.com/users/LlamaNinja`).
2. Click the bookmark.
3. Alert pops up with the count of URLs copied.
4. Open commander-builder (`http://127.0.0.1:5050`).
5. `+ New deck` → `Bulk URLs` tab → paste → `Import all`.
6. The existing `/api/bulk_import` endpoint takes it from there with a 1s polite spacing.

## Limits

- Caps at whatever Moxfield renders on the page. Their "All Decks" view
  typically loads everything for a user (no pagination), but if a user
  has 100+ decks the page may paginate — scroll to the bottom before
  clicking the bookmarklet.
- `/api/bulk_import` rejects > 50 URLs per request. Split into batches
  if needed.
- Doesn't fetch private decks (you'd need to be signed into Moxfield
  AND the bookmarklet still only sees what the page renders).

## Source (un-minified)

```js
(function () {
  if (!/moxfield\.com\/users\//.test(location.href)) {
    alert("Run this on a Moxfield user page (https://moxfield.com/users/<name>).");
    return;
  }

  const anchors = Array.from(document.querySelectorAll('a[href*="/decks/"]'));
  const urls = Array.from(new Set(
    anchors
      .map(a => a.href)
      .filter(h => !h.includes("?") && !/\/decks\/(public|personal)$/.test(h))
      .filter(h => /\/decks\/[A-Za-z0-9_-]{8,}$/.test(h))
  ));

  if (urls.length === 0) {
    alert("No deck URLs found. Make sure you're on a user page with public decks visible.");
    return;
  }

  const text = urls.join("\n");

  function fallback(t) {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.cssText =
      "position:fixed;top:50px;left:50px;width:640px;height:400px;" +
      "z-index:99999;background:#222;color:#fff;padding:10px;" +
      "border:2px solid #555;font-family:monospace;font-size:12px;";
    document.body.appendChild(ta);
    ta.select();
    alert("Clipboard write blocked. Select-all & copy from the textarea, then close it.");
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => alert(
        "Copied " + urls.length + " deck URL(s) to clipboard.\n\n" +
        "Next: open commander-builder, click \"+ New deck\" → " +
        "\"Bulk URLs\" → paste → \"Import all\"."
      ),
      () => fallback(text),
    );
  } else {
    fallback(text);
  }
})();
```

## Why not a one-click server-side flow?

Moxfield's `/v2/users/<name>/decks` endpoint returns 404 to public
clients, and their `/v2/decks/search-sfw` endpoint silently ignores
every user-filter param we tried (`authorUserName`, `userName`,
`author`, `creator`, etc.). The username-filtered listing the Moxfield
UI uses must be hitting an authenticated endpoint or using the user's
internal GUID (which `/v1/users/<name>` doesn't expose).

The bookmarklet runs in your browser's authenticated session against
already-rendered DOM, sidestepping all of that. CORS-free, no API key,
no server-side scraping infrastructure.

[1]: HANDOFF.md
