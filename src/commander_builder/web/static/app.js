// FP-006 minimal dashboard renderer.
// Fetches /api/decks, /api/dashboard, /api/iterations and renders the
// seven panels described in deck_dashboard.build_dashboard.

const $ = (id) => document.getElementById(id);

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

async function loadHealth() {
  try {
    const h = await fetchJSON("/api/health");
    $("health-badge").textContent = `${h.status} · ${h.deck_count} decks`;
  } catch (e) {
    $("health-badge").textContent = "API unreachable";
  }
}

async function loadDecks() {
  const list = $("deck-list");
  list.innerHTML = "";
  try {
    const { decks } = await fetchJSON("/api/decks");
    if (!decks.length) {
      list.appendChild(el("li", { class: "muted" }, "No decks found."));
      return;
    }
    for (const d of decks) {
      const li = el("li", { "data-id": d.id }, d.name);
      li.addEventListener("click", () => selectDeck(d.id, li));
      list.appendChild(li);
    }
  } catch (e) {
    list.appendChild(el("li", { class: "muted" }, "Error: " + e.message));
  }
}

function highlight(li) {
  document.querySelectorAll(".deck-list li").forEach((n) => n.classList.remove("active"));
  if (li) li.classList.add("active");
}

let _activeDeckId = null;

async function selectDeck(deckId, li) {
  _activeDeckId = deckId;
  highlight(li);
  const dash = $("dashboard");
  dash.innerHTML = '<p class="empty-state">Loading…</p>';
  try {
    const [data, iters] = await Promise.all([
      fetchJSON(`/api/dashboard?deck=${encodeURIComponent(deckId)}`),
      fetchJSON(`/api/iterations?deck=${encodeURIComponent(deckId)}`),
    ]);
    renderDashboard(data, iters.iterations || []);
  } catch (e) {
    dash.innerHTML = `<p class="empty-state">Error loading: ${e.message}</p>`;
  }
}

let _proposeMode = "ab";   // "ab" (Run A/B sim) or "save" (Edit deck)

async function openProposeModal(opts) {
  if (!_activeDeckId) return;
  _proposeMode = (opts && opts.saveOnly) ? "save" : "ab";
  const modal = $("propose-modal");
  const ta = $("propose-text");
  const status = $("propose-status");
  const result = $("propose-result");
  const runBtn = $("propose-run");
  const radios = document.querySelector(".games-radio");
  // Toggle UI between A/B sim and save-only modes.
  if (_proposeMode === "save") {
    runBtn.textContent = "Save changes";
    if (radios) radios.style.display = "none";
    modal.querySelector(".modal h2").textContent = "Edit deck";
  } else {
    runBtn.textContent = "Run A/B sim";
    if (radios) radios.style.display = "";
    modal.querySelector(".modal h2").textContent = "Propose changes";
  }
  status.textContent = "";
  result.innerHTML = "";
  ta.value = "Loading…";
  modal.hidden = false;
  try {
    const body = await fetchJSON(
      `/api/deck_text?deck=${encodeURIComponent(_activeDeckId)}`,
    );
    ta.value = body.text || "";
    ta.focus();
  } catch (e) {
    ta.value = "";
    status.textContent = `Could not load deck: ${e.message}`;
  }
}

function closeProposeModal() {
  $("propose-modal").hidden = true;
}

async function runProposeSwap() {
  if (!_activeDeckId) return;
  const ta = $("propose-text");
  const status = $("propose-status");
  const result = $("propose-result");
  const btn = $("propose-run");

  if (_proposeMode === "save") {
    // Edit-only path: PUT the new text and reload the dashboard.
    btn.disabled = true;
    status.textContent = "Saving…";
    try {
      const resp = await fetch(
        `/api/deck_text?deck=${encodeURIComponent(_activeDeckId)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: ta.value }),
        },
      );
      const body = await resp.json();
      if (!resp.ok) {
        status.textContent = `Error: ${body.error || resp.status}`;
        return;
      }
      status.textContent = "Saved.";
      $("propose-modal").hidden = true;
      // Reload dashboard so panels reflect the new deck contents.
      const li = document.querySelector(`.deck-list li[data-id="${_activeDeckId}"]`);
      selectDeck(_activeDeckId, li);
    } catch (e) {
      status.textContent = `Save failed: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
    return;
  }

  const games = parseInt(
    document.querySelector('input[name="games"]:checked').value, 10,
  );
  result.innerHTML = "";
  status.textContent = `Running ${games} games via Forge — this can take ${games === 5 ? "~15s" : games === 10 ? "~30s" : "~60s"}…`;
  btn.disabled = true;
  try {
    const resp = await fetch("/api/propose_swap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deck: _activeDeckId,
        new_text: ta.value,
        games,
        bracket: 3,
        mode: "1v1",
      }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      status.textContent = `Error: ${body.error || resp.status}${
        body.detail ? " — " + body.detail : ""
      }`;
      return;
    }
    status.textContent = `Done. ${body.total_games} games played.`;
    renderProposeResult(result, body);
  } catch (e) {
    status.textContent = `Network error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderProposeResult(container, body) {
  const wrap = el("div");
  const winnerCls = body.winner === "tie" ? "tie" : body.winner;
  wrap.appendChild(el(
    "div", { class: `propose-winner ${winnerCls}` },
    body.winner === "tie"
      ? "Tie game"
      : `Winner: ${body.winner === "new" ? "new version" : "old version"} (margin ${body.margin})`,
  ));
  const grid = el("div", { class: "propose-result" });
  grid.appendChild(rowEl("Old wins", `${body.old_wins} / ${body.old_games}`));
  grid.appendChild(rowEl("New wins", `${body.new_wins} / ${body.new_games}`));
  grid.appendChild(rowEl("Draws", String(body.draws)));
  grid.appendChild(rowEl("Mode", body.mode));
  grid.appendChild(rowEl("Bracket", String(body.bracket)));
  grid.appendChild(rowEl("Cards added", String((body.diff?.added || []).length)));
  grid.appendChild(rowEl("Cards removed", String((body.diff?.removed || []).length)));
  grid.appendChild(rowEl("Games per pod", String(body.games_per_pod)));
  wrap.appendChild(grid);
  container.appendChild(wrap);
}

function rowEl(label, value) {
  const r = el("div", { class: "row" });
  r.appendChild(el("span", { class: "muted" }, label));
  r.appendChild(el("span", {}, value));
  return r;
}

async function loadAdvise() {
  if (!_activeDeckId) return;
  const sug = $("sug-panel");
  if (!sug) return;
  sug.innerHTML = '<p class="muted">Generating suggestions…</p>';
  try {
    const body = await fetchJSON(
      `/api/advise?deck=${encodeURIComponent(_activeDeckId)}&bracket=3`,
    );
    renderSuggestions(sug, body.suggestions || []);
  } catch (e) {
    sug.innerHTML = `<p class="muted">Advise failed: ${e.message}</p>`;
  }
}

function renderDashboard(data, iterations) {
  const dash = $("dashboard");
  dash.innerHTML = "";

  // Commander hero
  const hero = el("section", { class: "commander-hero" });
  hero.appendChild(el("div", { class: "name" }, data.commander.name || "Untitled"));
  if (data.commander.type_line) {
    hero.appendChild(el("div", { class: "type-line" }, data.commander.type_line));
  }
  const pips = el("div", { class: "color-pips" });
  for (const c of data.commander.color_identity || []) {
    pips.appendChild(el("span", { class: `color-pip cp-${c}` }, c));
  }
  hero.appendChild(pips);
  // Action row — Propose / Run audit / Edit / Copy to Moxfield / Delete.
  const actions = el("div", { class: "hero-actions" });

  const proposeBtn = el("button", { class: "primary" }, "Propose changes");
  proposeBtn.addEventListener("click", openProposeModal);
  actions.appendChild(proposeBtn);

  const auditBtn = el("button", {}, "Run audit");
  auditBtn.title = "Generate swap suggestions via heuristic + EDHREC";
  auditBtn.addEventListener("click", loadAdvise);
  actions.appendChild(auditBtn);

  const editBtn = el("button", {}, "Edit deck");
  editBtn.addEventListener("click", () => openProposeModal({ saveOnly: true }));
  actions.appendChild(editBtn);

  const copyBtn = el("button", {}, "Copy to Moxfield");
  copyBtn.addEventListener("click", copyToMoxfield);
  actions.appendChild(copyBtn);

  const deleteBtn = el("button", { class: "danger" }, "Delete");
  deleteBtn.addEventListener("click", deleteDeck);
  actions.appendChild(deleteBtn);

  hero.appendChild(actions);
  dash.appendChild(hero);

  // Stat tiles
  const t = data.stat_tiles || {};
  const tiles = el("section", { class: "tile-grid" });
  tiles.appendChild(tile("Avg CMC", t.avg_cmc?.toFixed(2) ?? "—"));
  tiles.appendChild(tile("Lands", t.lands ?? "—"));
  // Bracket tile: heuristic recommendation + dropdown to override.
  // `bracket_name` carries the human label. The override re-fetches
  // /api/dashboard with the chosen bracket, so the user sees how
  // the heuristic shifts power-related fields.
  tiles.appendChild(bracketTile(t));
  tiles.appendChild(tile(
    "Est. price",
    t.est_price_usd != null ? `$${t.est_price_usd.toFixed(2)}` : "—",
    t.n_priced_cards != null ? `${t.n_priced_cards} priced cards` : null,
  ));
  tiles.appendChild(tile(
    "Deck progress",
    `${data.deck_progress?.current ?? 0} / ${data.deck_progress?.target ?? 100}`,
  ));
  dash.appendChild(tiles);

  // Mana curve
  dash.appendChild(panel("Mana curve", curveBars(data.mana_curve || [])));

  // Categories
  const catGrid = el("div", { class: "category-grid" });
  for (const [name, count] of Object.entries(data.categories || {})) {
    const cat = el("div", { class: "category" });
    cat.appendChild(el("span", {}, name.replace(/_/g, " ")));
    cat.appendChild(el("span", { class: "count" }, String(count)));
    catGrid.appendChild(cat);
  }
  dash.appendChild(panel("Categories", catGrid));

  // Theme tags
  if ((data.theme_tags || []).length) {
    const tags = el("div", { class: "tag-row" });
    for (const tg of data.theme_tags) tags.appendChild(el("span", { class: "tag" }, tg));
    dash.appendChild(panel("Theme tags", tags));
  }

  // Suggested adds — always render the panel with a "Get suggestions"
  // button so the user can request advise on demand.
  const sugPanel = el("section", { class: "panel", id: "sug-panel" });
  sugPanel.appendChild(el("h3", {}, "Suggested adds"));
  if ((data.suggested_adds || []).length) {
    renderSuggestions(sugPanel, data.suggested_adds);
  } else {
    const btn = el("button", { class: "advise-btn" }, "Get suggestions");
    btn.addEventListener("click", loadAdvise);
    sugPanel.appendChild(btn);
    sugPanel.appendChild(el(
      "p", { class: "muted" },
      "Heuristic over EDHREC + recent match history. ",
      "Skips universal staples (Sol Ring etc).",
    ));
  }
  dash.appendChild(sugPanel);

  // Iteration history
  if (iterations.length) {
    const ul = el("ul", { class: "iteration-list" });
    for (const it of iterations) {
      const row = el("li", { class: "iteration" });
      row.appendChild(el("span", { class: `verdict ${it.verdict}` }, it.verdict));
      row.appendChild(el("span", { class: "name" }, it.deck_name));
      const deltaText =
        it.margin != null
          ? `${it.margin > 0 ? "+" : ""}${it.margin}pp`
          : it.win_rate_new != null
          ? `${Math.round(it.win_rate_new * 100)}%`
          : "";
      row.appendChild(el("span", { class: "delta" }, deltaText));
      ul.appendChild(row);
    }
    dash.appendChild(panel("Iteration history", ul));
  }
}

function renderSuggestions(container, suggestions) {
  // Clear children except the <h3>.
  while (container.children.length > 1) container.removeChild(container.lastChild);
  if (!suggestions.length) {
    container.appendChild(el("p", { class: "muted" }, "No suggestions found."));
    return;
  }
  const ul = el("ul", { class: "iteration-list" });
  for (const s of suggestions) {
    const row = el("li", { class: "iteration" });
    const pct = s.match_pct != null
      ? s.match_pct
      : Math.round((s.inclusion_pct || 0) + Math.min(s.synergy_pct || 0, 20));
    row.appendChild(el("span", { class: "verdict pending" }, `${pct}%`));
    const nameWrap = el("div");
    nameWrap.appendChild(el("div", { class: "name" }, s.card));
    if (s.rationale) {
      nameWrap.appendChild(el("div", { class: "muted" }, s.rationale));
    }
    row.appendChild(nameWrap);
    row.appendChild(el(
      "span", { class: "delta" },
      s.price_usd != null ? `$${Number(s.price_usd).toFixed(2)}` : "",
    ));
    ul.appendChild(row);
  }
  container.appendChild(ul);
}

function bracketTile(t) {
  const t_node = el("div", { class: "tile" });
  t_node.appendChild(el("div", { class: "label" }, "Bracket"));
  const num = t.bracket ?? t.power_level ?? "—";
  const label = t.bracket_name ? ` (${t.bracket_name})` : "";
  t_node.appendChild(el("div", { class: "value" }, `${num}${label}`));
  if (t.n_game_changers != null) {
    t_node.appendChild(el(
      "div", { class: "sub" },
      `${t.n_game_changers} game changer${t.n_game_changers === 1 ? "" : "s"}`,
    ));
  }
  // Override dropdown — change rebuilds dashboard with the new bracket.
  const ctrl = el("div", { class: "bracket-control" });
  ctrl.appendChild(el("span", { class: "muted" }, "Override:"));
  const sel = el("select");
  for (const [v, name] of [
    [1, "1 Exhibition"], [2, "2 Core"], [3, "3 Upgraded"],
    [4, "4 Optimized"], [5, "5 cEDH"],
  ]) {
    const opt = el("option", { value: String(v) }, name);
    if (Number(v) === Number(num)) opt.setAttribute("selected", "selected");
    sel.appendChild(opt);
  }
  sel.addEventListener("change", async (ev) => {
    const newBracket = ev.target.value;
    if (!_activeDeckId) return;
    const dash = $("dashboard");
    dash.innerHTML = '<p class="empty-state">Reloading with bracket ' + newBracket + '…</p>';
    try {
      const [data, iters] = await Promise.all([
        fetchJSON(
          `/api/dashboard?deck=${encodeURIComponent(_activeDeckId)}&bracket=${newBracket}`,
        ),
        fetchJSON(`/api/iterations?deck=${encodeURIComponent(_activeDeckId)}`),
      ]);
      renderDashboard(data, iters.iterations || []);
    } catch (e) {
      dash.innerHTML = `<p class="empty-state">Error: ${e.message}</p>`;
    }
  });
  ctrl.appendChild(sel);
  t_node.appendChild(ctrl);
  return t_node;
}

async function copyToMoxfield() {
  if (!_activeDeckId) return;
  try {
    const body = await fetchJSON(
      `/api/moxfield_format?deck=${encodeURIComponent(_activeDeckId)}`,
    );
    await navigator.clipboard.writeText(body.text);
    flashStatus("Copied to clipboard — paste into Moxfield's bulk-edit.");
  } catch (e) {
    flashStatus(`Copy failed: ${e.message}`);
  }
}

async function deleteDeck() {
  if (!_activeDeckId) return;
  if (!confirm(`Delete "${_activeDeckId}"? This removes the .dck file from disk.`)) {
    return;
  }
  try {
    const resp = await fetch(
      `/api/deck_text?deck=${encodeURIComponent(_activeDeckId)}`,
      { method: "DELETE" },
    );
    if (!resp.ok) {
      flashStatus(`Delete failed: ${resp.status}`);
      return;
    }
    flashStatus("Deck deleted.");
    _activeDeckId = null;
    $("dashboard").innerHTML = '<p class="empty-state">Select a deck on the left to load its dashboard.</p>';
    loadDecks();
  } catch (e) {
    flashStatus(`Delete failed: ${e.message}`);
  }
}

function flashStatus(msg) {
  // Reuse the health badge as a transient toast — simple + visible.
  const badge = $("health-badge");
  if (!badge) { console.log(msg); return; }
  const original = badge.textContent;
  badge.textContent = msg;
  setTimeout(() => { badge.textContent = original; }, 4000);
}

// Game Changers + Illegal Cards alert modals.
async function showGameChangersAlert() {
  const modal = $("alert-modal");
  $("alert-title").textContent = "Game Changers";
  const body = $("alert-body");
  body.className = "alert-body";
  body.innerHTML = '<p class="muted">Loading…</p>';
  modal.hidden = false;
  try {
    const list = await fetchJSON("/api/game_changers");
    body.innerHTML = "";
    body.appendChild(el(
      "p", { class: "muted" },
      `Wizards' Game Changers list — ${list.count} cards. Bracket-3 ` +
      "(Upgraded) and below expect zero of these. " +
      "Bracket 4+ allows them.",
    ));
    if (_activeDeckId) {
      try {
        const audit = await fetchJSON(
          `/api/deck_audit?deck=${encodeURIComponent(_activeDeckId)}`,
        );
        if (audit.in_deck_game_changers.length) {
          body.appendChild(el(
            "p", {},
            el("span", { class: "pill warn" },
               `${audit.in_deck_game_changers.length} in this deck`),
          ));
          const ul = el("ul");
          for (const c of audit.in_deck_game_changers) {
            ul.appendChild(el("li", {}, c));
          }
          body.appendChild(ul);
        } else {
          body.appendChild(el(
            "p", {}, el("span", { class: "pill good" },
                        "None in this deck"),
          ));
        }
      } catch (e) { /* ignore */ }
    }
    body.appendChild(el(
      "details", {},
      el("summary", { class: "muted" }, "Show full Game Changers list"),
      (() => {
        const ul = el("ul");
        for (const c of list.cards) {
          ul.appendChild(el("li", {}, c));
        }
        return ul;
      })(),
    ));
  } catch (e) {
    body.innerHTML = `<p class="muted">Could not load Game Changers: ${e.message}</p>`;
  }
}

async function showIllegalAlert() {
  const modal = $("alert-modal");
  $("alert-title").textContent = "Illegal cards";
  const body = $("alert-body");
  body.className = "alert-body";
  body.innerHTML = '<p class="muted">Loading…</p>';
  modal.hidden = false;
  if (!_activeDeckId) {
    body.innerHTML = '<p class="muted">Select a deck first to check for illegal cards.</p>';
    return;
  }
  try {
    const audit = await fetchJSON(
      `/api/deck_audit?deck=${encodeURIComponent(_activeDeckId)}`,
    );
    body.innerHTML = "";
    if (audit.illegal_cards.length) {
      body.appendChild(el(
        "p", {}, el("span", { class: "pill bad" },
                    `${audit.illegal_cards.length} banned in Commander`),
      ));
      const ul = el("ul");
      for (const c of audit.illegal_cards) ul.appendChild(el("li", {}, c));
      body.appendChild(ul);
    } else {
      body.appendChild(el(
        "p", {}, el("span", { class: "pill good" },
                    "All cards are legal in Commander."),
      ));
    }
    if (audit.warnings.length) {
      body.appendChild(el("h4", {}, "Other warnings"));
      const ul = el("ul");
      for (const w of audit.warnings) ul.appendChild(el("li", {}, w));
      body.appendChild(ul);
    }
  } catch (e) {
    body.innerHTML = `<p class="muted">Audit failed: ${e.message}</p>`;
  }
}

function openNewDeckModal() {
  $("new-deck-status").textContent = "";
  $("new-mox-name").value = "";
  $("new-mox-url").value = "";
  $("new-paste-name").value = "";
  $("new-paste-text").value = "";
  $("new-deck-modal").hidden = false;
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.hidden = p.id !== `tab-${name}`;
  });
}

async function importMoxfield() {
  const name = $("new-mox-name").value.trim();
  const url = $("new-mox-url").value.trim();
  const bracket = parseInt($("new-mox-bracket").value, 10);
  const status = $("new-deck-status");
  if (!url) {
    status.textContent = "Enter a Moxfield URL or deck id.";
    return;
  }
  status.textContent = "Fetching from Moxfield…";
  try {
    const resp = await fetch("/api/import_deck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, moxfield_url: url, bracket }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      status.textContent = `Error: ${body.error || resp.status}${body.detail ? " — " + body.detail : ""}`;
      return;
    }
    status.textContent = `Imported as ${body.filename}.`;
    $("new-deck-modal").hidden = true;
    await loadDecks();
  } catch (e) {
    status.textContent = `Network error: ${e.message}`;
  }
}

async function createPasteDeck() {
  const name = $("new-paste-name").value.trim();
  const text = $("new-paste-text").value;
  const bracket = parseInt($("new-paste-bracket").value, 10);
  const status = $("new-deck-status");
  if (!name) {
    status.textContent = "Display name is required.";
    return;
  }
  if (!text.trim()) {
    status.textContent = "Paste a deck list first.";
    return;
  }
  status.textContent = "Saving…";
  try {
    const resp = await fetch("/api/import_deck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, paste_text: text, bracket }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      status.textContent = `Error: ${body.error || resp.status}`;
      return;
    }
    status.textContent = `Created ${body.filename}.`;
    $("new-deck-modal").hidden = true;
    await loadDecks();
  } catch (e) {
    status.textContent = `Network error: ${e.message}`;
  }
}

function tile(label, value, sub) {
  const t = el("div", { class: "tile" });
  t.appendChild(el("div", { class: "label" }, label));
  t.appendChild(el("div", { class: "value" }, String(value)));
  if (sub) t.appendChild(el("div", { class: "sub" }, sub));
  return t;
}

function panel(title, content) {
  const p = el("section", { class: "panel" });
  p.appendChild(el("h3", {}, title));
  p.appendChild(content);
  return p;
}

function curveBars(curve) {
  const wrap = el("div");
  const bars = el("div", { class: "curve-bars" });
  const max = Math.max(1, ...curve.map(([_, c]) => c));
  const total = curve.reduce((s, [, c]) => s + c, 0);
  for (const [bucket, count] of curve) {
    const bar = el("div", { class: "curve-bar" });
    bar.style.height = `${Math.round((count / max) * 100)}%`;
    bar.title = `CMC ${bucket >= 6 ? "6+" : bucket}: ${count} cards (${
      total > 0 ? Math.round((count / total) * 100) : 0
    }% of nonland)`;
    bar.appendChild(el("span", { class: "bar-count" }, String(count)));
    bars.appendChild(bar);
  }
  const labels = el("div", { class: "curve-labels" });
  for (const [bucket] of curve) {
    labels.appendChild(el("span", {}, bucket >= 6 ? "6+" : String(bucket)));
  }
  wrap.appendChild(bars);
  wrap.appendChild(labels);
  // Average CMC summary line.
  const sumWeighted = curve.reduce((s, [b, c]) => s + b * c, 0);
  const avg = total > 0 ? (sumWeighted / total).toFixed(2) : "—";
  wrap.appendChild(el(
    "p", { class: "muted" },
    `Avg nonland CMC: ${avg}  ·  Total: ${total} nonland cards`,
  ));
  return wrap;
}

// Wire all modal + topbar handlers.
document.addEventListener("DOMContentLoaded", () => {
  // Propose-swap modal.
  const closeBtn = $("propose-close");
  if (closeBtn) closeBtn.addEventListener("click", closeProposeModal);
  const runBtn = $("propose-run");
  if (runBtn) runBtn.addEventListener("click", runProposeSwap);

  // Generic modal-close buttons (Add-deck modal + Alert modal).
  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $(btn.dataset.close).hidden = true;
    });
  });

  // ESC closes any open modal.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      ["propose-modal", "new-deck-modal", "alert-modal"].forEach((id) => {
        const m = $(id); if (m) m.hidden = true;
      });
    }
  });

  // Backdrop click closes — for each modal-backdrop.
  document.querySelectorAll(".modal-backdrop").forEach((bd) => {
    bd.addEventListener("click", (e) => {
      if (e.target === bd) bd.hidden = true;
    });
  });

  // Topbar buttons.
  const gcBtn = $("btn-game-changers");
  if (gcBtn) gcBtn.addEventListener("click", showGameChangersAlert);
  const illegalBtn = $("btn-illegal");
  if (illegalBtn) illegalBtn.addEventListener("click", showIllegalAlert);
  const newDeckBtn = $("btn-new-deck");
  if (newDeckBtn) newDeckBtn.addEventListener("click", openNewDeckModal);

  // New-deck modal: tab switching + import buttons.
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  });
  const moxImport = $("new-mox-import");
  if (moxImport) moxImport.addEventListener("click", importMoxfield);
  const pasteCreate = $("new-paste-create");
  if (pasteCreate) pasteCreate.addEventListener("click", createPasteDeck);
});

loadHealth();
loadDecks();
