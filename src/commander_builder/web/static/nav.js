// FP-007: Unified nav shell + Library + Rules/Combo UI.
//
// Three left-rail sections wired here so app.js (the existing deck
// dashboard) stays untouched:
//
//   Decks  -- shows the existing sidebar deck list + deck dashboard (default)
//   Cards  -- library search: which of my decks run this card?
//             Uses GET /api/library?card=<name>
//   Rules  -- combo lookup by color identity + Game Changers list
//             Uses GET /api/rules/combo?identity=<WUBRG>
//                  GET /api/rules/game_changers
//
// DOM contract: elements defined in index.html under id="left-rail",
// id="section-decks", id="section-cards", id="section-rules".

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // Helpers shared with nav.js scope.
  // -----------------------------------------------------------------------

  function $id(id) { return document.getElementById(id); }

  function navEl(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      // No innerHTML escape hatch — see el() in app.js.
      else node.setAttribute(k, v);
    }
    (children || []).forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  async function navFetch(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  }

  // -----------------------------------------------------------------------
  // Section switching
  // -----------------------------------------------------------------------

  const SECTIONS = ["decks", "cards", "rules"];

  function activateSection(name) {
    // Toggle rail buttons + sync aria-pressed for screen readers.
    document.querySelectorAll(".rail-btn").forEach((btn) => {
      const isActive = btn.dataset.section === name;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    // Show/hide sidebar sections.
    SECTIONS.forEach((s) => {
      const el = $id("section-" + s);
      if (el) el.hidden = (s !== name);
    });
    // Hide the main dashboard area when not in Decks; show an
    // instructional placeholder instead so the content pane isn't blank.
    const dash = $id("dashboard");
    if (!dash) return;
    if (name === "decks") {
      // Restore: remove any nav placeholder and show the real dashboard.
      const ph = $id("nav-section-placeholder");
      if (ph) ph.remove();
      dash.hidden = false;
    } else {
      // Park the dashboard and put up a minimal notice.
      dash.hidden = true;
      let ph = $id("nav-section-placeholder");
      if (!ph) {
        ph = navEl("section", {
          id: "nav-section-placeholder",
          class: "dashboard",
          style: "padding:24px;",
        }, []);
        dash.parentNode.insertBefore(ph, dash.nextSibling);
      }
      const label = name === "cards"
        ? "Use the search box on the left to find which of your decks run a card."
        : "Use the form on the left to look up combos or view Game Changers.";
      ph.innerHTML = '<p class="empty-state">' + label + "</p>";
    }
  }

  // Wire rail buttons on DOMContentLoaded (or immediately if already ready).
  function wireRail() {
    const rail = $id("left-rail");
    if (!rail) return;
    rail.addEventListener("click", (e) => {
      const btn = e.target.closest(".rail-btn");
      if (!btn || !btn.dataset.section) return;
      activateSection(btn.dataset.section);
    });
    // Decks is active by default.
    activateSection("decks");
  }

  // -----------------------------------------------------------------------
  // Slice 2 -- Library search (Cards section)
  // -----------------------------------------------------------------------

  function renderLibraryResults(container, data) {
    container.innerHTML = "";
    const card = data.card || "";
    const decks = data.decks || [];
    if (!card) {
      container.appendChild(navEl("p", { class: "muted" }, ["Enter a card name above."]));
      return;
    }
    const header = navEl("p", { style: "font-size:13px;font-weight:600;margin:0 0 6px;" },
      [decks.length + " deck" + (decks.length !== 1 ? "s" : "") + " run " + card]);
    container.appendChild(header);
    if (!decks.length) {
      container.appendChild(navEl("p", { class: "muted" }, ["No decks run this card."]));
      return;
    }
    const list = navEl("ul", { style: "list-style:none;padding:0;margin:0;" }, []);
    decks.forEach((id) => {
      // Strip [USER] prefix and [Bn] suffix for display.
      const display = id.replace(/^\[USER\]\s*/, "").replace(/\s*\[B\d\]\s*$/, "").trim() || id;
      const item = navEl("li", {
        style: "padding:6px 8px;border-radius:4px;font-size:13px;cursor:pointer;",
        "data-deck": id,
        title: "Click to open this deck",
      }, [display]);
      item.addEventListener("mouseenter", () => { item.style.background = "var(--panel-2)"; });
      item.addEventListener("mouseleave", () => { item.style.background = ""; });
      // Clicking a deck result switches back to Decks section and selects it.
      item.addEventListener("click", () => {
        activateSection("decks");
        // Trigger deck selection in app.js if available.
        const li = document.querySelector('.deck-list li[data-id="' + CSS.escape(id) + '"]');
        if (li && typeof selectDeck === "function") {  // eslint-disable-line no-undef
          selectDeck(id, li);
        }
      });
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function wireLibrarySearch() {
    const form = $id("library-search-form");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $id("library-card-input");
      const results = $id("library-results");
      const card = (input ? input.value : "").trim();
      if (!card) {
        results.innerHTML = '<p class="muted">Enter a card name to search.</p>';
        return;
      }
      results.innerHTML = '<p class="muted" aria-live="polite">Loading…</p>';
      try {
        const data = await navFetch("/api/library?card=" + encodeURIComponent(card));
        renderLibraryResults(results, data);
      } catch (err) {
        const status = err.message.match(/-> (\d+)/);
        const msg = status && status[1] === "400"
          ? "Invalid search — please enter a card name."
          : "Could not load library results. Please try again.";
        results.innerHTML = '<p class="muted" role="alert">' + msg + "</p>";
      }
    });
  }

  // -----------------------------------------------------------------------
  // Slice 3 -- Rules / Combo lookup (Rules section)
  // -----------------------------------------------------------------------

  function renderComboResults(container, data) {
    container.innerHTML = "";
    const combos = data.combos || [];
    const identity = data.identity || "";
    if (!combos.length) {
      container.appendChild(navEl("p", { class: "muted" },
        [identity ? "No combos for that color identity." : "No combos found."]));
      return;
    }
    const header = navEl("p", { style: "font-size:13px;font-weight:600;margin:0 0 6px;" },
      [combos.length + " combo" + (combos.length !== 1 ? "s" : "")
       + (identity ? " in " + identity : "")]);
    container.appendChild(header);
    combos.forEach((c) => {
      const cards = (c.cards || []).join(" + ");
      const produces = c.produces || "";
      const floor = c.bracket_floor != null ? "B" + c.bracket_floor + "+" : "";
      const item = navEl("div", {
        style: "padding:6px 8px;border:1px solid var(--border);border-radius:6px;"
             + "margin-bottom:6px;font-size:12px;",
      }, [
        navEl("div", { style: "font-weight:600;" }, [cards]),
        navEl("div", { class: "muted" }, [produces + (floor ? "  (" + floor + ")" : "")]),
      ]);
      container.appendChild(item);
    });
  }

  function renderGameChangers(container, data) {
    container.innerHTML = "";
    const cards = data.cards || [];
    if (!cards.length) {
      container.appendChild(navEl("p", { class: "muted" }, ["No Game Changers loaded."]));
      return;
    }
    const header = navEl("p", { style: "font-size:13px;font-weight:600;margin:0 0 6px;" },
      [cards.length + " Game Changers"
       + (data.source === "cache" ? " (cached)" : data.source === "fallback" ? " (offline)" : "")]);
    container.appendChild(header);
    const grid = navEl("div", { style: "display:flex;flex-wrap:wrap;gap:4px;" }, []);
    cards.sort().forEach((name) => {
      grid.appendChild(navEl("span", {
        style: "background:var(--panel-2);border:1px solid var(--border);"
             + "border-radius:4px;padding:2px 7px;font-size:11px;",
      }, [name]));
    });
    container.appendChild(grid);
  }

  function wireRulesSearch() {
    const form = $id("combo-search-form");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const input = $id("combo-identity-input");
        const results = $id("rules-results");
        const identity = (input ? input.value : "").trim().toUpperCase();
        results.innerHTML = '<p class="muted" aria-live="polite">Loading…</p>';
        try {
          const url = "/api/rules/combo" + (identity ? "?identity=" + encodeURIComponent(identity) : "");
          const data = await navFetch(url);
          renderComboResults(results, data);
        } catch (err) {
          results.innerHTML =
            '<p class="muted" role="alert">Could not load combos. Please try again.</p>';
        }
      });
    }

    const gcBtn = $id("rules-game-changers-btn");
    if (gcBtn) {
      gcBtn.addEventListener("click", async () => {
        const results = $id("rules-results");
        results.innerHTML = '<p class="muted" aria-live="polite">Loading…</p>';
        try {
          const data = await navFetch("/api/rules/game_changers");
          renderGameChangers(results, data);
        } catch (err) {
          results.innerHTML =
            '<p class="muted" role="alert">Could not load Game Changers. Please try again.</p>';
        }
      });
    }
  }

  // -----------------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------------

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      wireRail();
      wireLibrarySearch();
      wireRulesSearch();
    });
  } else {
    wireRail();
    wireLibrarySearch();
    wireRulesSearch();
  }
})();
