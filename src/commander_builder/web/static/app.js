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

async function openProposeModal() {
  if (!_activeDeckId) return;
  const modal = $("propose-modal");
  const ta = $("propose-text");
  const status = $("propose-status");
  const result = $("propose-result");
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
  // "Propose changes" → opens the modify-deck + A/B Forge sim modal.
  const proposeBtn = el(
    "button", { class: "advise-btn", style: "margin-top: 10px;" },
    "Propose changes",
  );
  proposeBtn.addEventListener("click", openProposeModal);
  hero.appendChild(proposeBtn);
  dash.appendChild(hero);

  // Stat tiles
  const t = data.stat_tiles || {};
  const tiles = el("section", { class: "tile-grid" });
  tiles.appendChild(tile("Avg CMC", t.avg_cmc?.toFixed(2) ?? "—"));
  tiles.appendChild(tile("Lands", t.lands ?? "—"));
  // Wizards' Commander Bracket (1..5) replaced the old 1..10 power
  // level. `bracket_name` carries the human label.
  const bracketNum = t.bracket ?? t.power_level ?? "—";
  const bracketLabel = t.bracket_name ? ` (${t.bracket_name})` : "";
  const gcSub = t.n_game_changers != null
    ? `${t.n_game_changers} game changer${t.n_game_changers === 1 ? "" : "s"}`
    : null;
  tiles.appendChild(tile("Bracket", `${bracketNum}${bracketLabel}`, gcSub));
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

// Wire propose-swap modal handlers (the elements exist regardless of
// which deck is selected — the modal is hidden until openProposeModal
// flips its `hidden` attribute).
document.addEventListener("DOMContentLoaded", () => {
  const closeBtn = $("propose-close");
  if (closeBtn) closeBtn.addEventListener("click", closeProposeModal);
  const runBtn = $("propose-run");
  if (runBtn) runBtn.addEventListener("click", runProposeSwap);
  // ESC closes the modal.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeProposeModal();
  });
  // Click outside the modal body closes it.
  const backdrop = $("propose-modal");
  if (backdrop) {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeProposeModal();
    });
  }
});

loadHealth();
loadDecks();
