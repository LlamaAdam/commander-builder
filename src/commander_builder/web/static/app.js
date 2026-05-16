// FP-006 minimal dashboard renderer.
// Fetches /api/decks, /api/dashboard, /api/iterations and renders the
// seven panels described in deck_dashboard.build_dashboard.

const $ = (id) => document.getElementById(id);

// JS error collector — POSTs uncaught errors to /api/log_error so
// silent failures (TDZ ReferenceError, async network errors, etc.)
// land in a server-side log the user can grep / paste into chat.
// Borrowed from prior session: the "Run A/B did nothing" bug was
// exactly this category and took a code dive to find.
function _reportJsError(kind, message, stack) {
  try {
    fetch("/api/log_error", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind,
        message: String(message || ""),
        stack: String(stack || ""),
        url: location.href,
        user_agent: navigator.userAgent,
      }),
      keepalive: true,        // survives page-unload
    }).then((r) => r.ok && r.json()).then((j) => {
      if (j && j.ref) {
        // Stash the ref token where a future devtools session can find
        // it. Don't alert() — that's user-hostile.
        window.__lastJsErrorRef = j.ref;
        console.warn("[js-error reported, ref=" + j.ref + "]");
      }
    }).catch(() => { /* never let the reporter itself raise */ });
  } catch (_e) { /* silent fail */ }
}

window.addEventListener("error", (e) => {
  _reportJsError("error", e.message, (e.error && e.error.stack) || "");
});
window.addEventListener("unhandledrejection", (e) => {
  const reason = e.reason || {};
  _reportJsError(
    "unhandledrejection",
    reason.message || String(e.reason),
    reason.stack || "",
  );
});

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
    let txt = `${h.status} · ${h.deck_count} decks`;
    // Append Forge jar version + age. Best-effort — never block the
    // health badge if the version probe fails.
    let forgeIsStale = false;
    try {
      const fv = await fetchJSON("/api/forge_version");
      if (fv && fv.version) {
        const age = (fv.age_days != null)
          ? `${fv.age_days}d`
          : "age unknown";
        txt += ` · Forge ${fv.version} (${age})`;
        if (fv.is_stale) {
          txt += " ⚠";
          forgeIsStale = true;
        }
      } else if (fv) {
        txt += ` · no Forge jar found ⚠`;
        forgeIsStale = true;
      }
    } catch (_e) { /* ignore */ }
    // Append correlation summary if the harness has produced rows.
    try {
      const c = await fetchJSON("/api/correlation_summary");
      if (c && c.rows > 0) {
        txt += ` · forge_py ${(c.agreement_rate * 100).toFixed(0)}% agree (${c.rows})`;
      } else if (c && c.enabled) {
        txt += ` · correlation: collecting`;
      }
    } catch (_e) { /* ignore */ }
    const badge = $("health-badge");
    badge.textContent = txt;
    badge.title = forgeIsStale
      ? "Forge install is stale — consider updating from "
        + "github.com/Card-Forge/forge/releases"
      : "";
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
// AbortController for the currently in-flight audit stream. Reset on
// every loadAdvise() call so switching decks / re-running the audit
// cancels the previous Claude call instead of letting the stream
// keep running in the background (consuming SSE bandwidth and, more
// importantly, an Anthropic API charge for the user's BYO key).
let _auditAbortController = null;

async function selectDeck(deckId, li, opts) {
  // ``opts.soft`` (default false) skips the blanking step: keeps the
  // existing dashboard rendered while the new data is fetched, then
  // swaps it in. Used by Edit-deck saves so the UI doesn't flicker
  // through 5+ seconds of "Loading…" while Scryfall resolves any
  // newly-added cards.
  const soft = opts && opts.soft;
  // Cancel any in-flight audit stream for the previous deck. We do
  // this BEFORE updating ``_activeDeckId`` so the audit's own
  // mid-stream "did the deck change?" check still sees the old id
  // when the abort fires. Soft refresh (re-select the same deck)
  // doesn't need to cancel — the stream's deck-id pin keeps it
  // pointed at the right panel.
  if (!soft && _activeDeckId !== deckId && _auditAbortController) {
    try { _auditAbortController.abort(); } catch (_e) { /* ignore */ }
    _auditAbortController = null;
  }
  _activeDeckId = deckId;
  highlight(li);
  const dash = $("dashboard");
  if (!soft) {
    dash.innerHTML = '<p class="empty-state">Loading…</p>';
  } else {
    // Add a translucent loading badge in-corner so the user knows
    // a refresh is in flight without losing their place.
    let badge = document.getElementById("_soft-refresh-badge");
    if (!badge) {
      badge = el(
        "div",
        {
          id: "_soft-refresh-badge",
          style: "position: fixed; top: 12px; right: 16px; "
               + "background: var(--panel); color: var(--muted); "
               + "border: 1px solid var(--border); border-radius: 6px; "
               + "padding: 4px 10px; font-size: 12px; z-index: 200;",
        },
        "Refreshing…",
      );
      document.body.appendChild(badge);
    }
  }
  try {
    const [data, iters] = await Promise.all([
      fetchJSON(`/api/dashboard?deck=${encodeURIComponent(deckId)}`),
      fetchJSON(`/api/iterations?deck=${encodeURIComponent(deckId)}`),
    ]);
    renderDashboard(data, iters.iterations || []);
    // Auto-kick a fast heuristic audit so the user sees recs
    // immediately instead of hunting for the "Run audit" button.
    // Forced to "heuristic" regardless of the user's source pref:
    // the auto-kick fires without user input, so it must never
    // consume an Anthropic API quota. The "Run with Claude" button
    // inside the audit panel lets the user upgrade on demand.
    //
    // Gated by the auto-audit-on-load preference so power users on
    // slow networks can disable. Also gated on the deck staying
    // selected (the auto-kick races a possible deck switch).
    if (getAutoAuditPref() && _activeDeckId === deckId) {
      // ``loadAdvise("heuristic")`` is intentionally not awaited —
      // it runs in the background while the user reads the
      // dashboard. The Suggested-adds panel updates when it
      // resolves; if the user switches decks first, the existing
      // AbortController-based cancellation halts the in-flight
      // call.
      loadAdvise("heuristic");
    }
  } catch (e) {
    dash.innerHTML = `<p class="empty-state">Error loading: ${e.message}</p>`;
  } finally {
    const badge = document.getElementById("_soft-refresh-badge");
    if (badge) badge.remove();
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
      // Soft-refresh: keep the prior dashboard visible while the new
      // data fetches in background. Avoids the 5+s "Loading…" blank
      // when Edit added cards Scryfall hasn't cached yet.
      const li = document.querySelector(`.deck-list li[data-id="${_activeDeckId}"]`);
      selectDeck(_activeDeckId, li, { soft: true });
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
  // Resolve mode first — used to pick the ETA copy below AND posted
  // to /api/propose_swap. Reading it before declaration was a TDZ
  // ReferenceError that silently killed the click handler.
  const modeEl = document.querySelector('input[name="mode"]:checked');
  const mode = modeEl ? modeEl.value : "pod";
  // Pull the bracket from the filename's [B?] suffix; fall back to 3.
  // Resolved up here (not down in the try block) so the ETA copy can
  // factor it in — B4/B5 games run 2-3x longer than B3, so a flat
  // estimate misled users about how long to wait. Real datapoint that
  // forced this fix: a B4 10g pod sim took ~700s vs. the old flat
  // 150s estimate.
  const bracketMatch = (_activeDeckId || "").match(/\[B(\d)\]/);
  const bracket = bracketMatch ? parseInt(bracketMatch[1], 10) : 3;
  // Per-game wall-time in seconds, keyed by bracket. Conservative
  // (slow side of observed distribution) so users don't get
  // over-optimistic ETAs that breed "is it stuck?" questions.
  // Pod = 4-player commander; duel = 1v1 constructed (~3-5x faster).
  // With parallel-pod dispatch (Sprint 1A) + intra-pod abort (1C),
  // wall-time tracks the SLOWEST pod, not the sum — these figures
  // already account for that. 1B early-stop is effectively a no-op
  // when filler_pairs == cpu_count (all pods run concurrently,
  // nothing queued to cancel), so we don't discount for it.
  const podSecPerGame = { 1: 15, 2: 22, 3: 30, 4: 55, 5: 75 };
  const duelSecPerGame = { 1: 4, 2: 6, 3: 8, 4: 14, 5: 22 };
  const perGame = (mode === "pod")
    ? (podSecPerGame[bracket] ?? podSecPerGame[3])
    : (duelSecPerGame[bracket] ?? duelSecPerGame[3]);
  const etaSec = games * perGame;
  const etaStr = etaSec >= 90
    ? `~${Math.round(etaSec / 60)} min`
    : `~${etaSec}s`;
  status.textContent = `Running ${games} ${mode === "pod" ? "pod" : "1v1"} `
    + `games on B${bracket} via Forge — ${etaStr}…`;
  btn.disabled = true;
  try {
    const resp = await fetch("/api/propose_swap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deck: _activeDeckId,
        new_text: ta.value,
        games,
        bracket,
        mode,
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
    _lastSimReport = body;
    renderProposeResult(result, body);
  } catch (e) {
    status.textContent = `Network error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderProposeResult(container, body) {
  const wrap = el("div");
  const totalGames = (body.old_games || 0) + (body.new_games || 0) + (body.draws || 0);
  if (!body.total_games || totalGames === 0) {
    // Forge ran but no games completed — usually means the proposed
    // deck failed Forge's legality check (wrong card count, illegal
    // cards, missing commander). Don't pretend it was a tie.
    wrap.appendChild(el(
      "div", { class: "propose-winner tie" },
      "No games completed",
    ));
    wrap.appendChild(el(
      "p", { class: "muted" },
      "Forge ran but reported zero games. Most common cause: the " +
      "proposed deck has the wrong card count (Commander needs " +
      "exactly 99 mainboard + 1 commander = 100). Check the textarea " +
      "above and re-run.",
    ));
    container.appendChild(wrap);
    return;
  }
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
  // Sprint 1B telemetry: show pod count and an early-stop note when
  // adaptive early-stop cut the run short. The verdict is robust
  // because the remaining pods couldn't have flipped it; we just
  // saved wall-time.
  if (body.pods_planned && body.pods_planned > 1) {
    grid.appendChild(rowEl(
      "Pods",
      `${body.pods_completed} / ${body.pods_planned}`
      + (body.stopped_early ? " (stopped early — verdict locked)" : ""),
    ));
  }
  wrap.appendChild(grid);

  // Sprint 1C telemetry: list any pods whose intra-pod abort fired,
  // so the user understands why some pods reported < games_per_pod
  // games. This is informational — the verdict is unaffected.
  if (Array.isArray(body.pod_summaries)) {
    const aborted = body.pod_summaries.filter((p) => p.intra_pod_aborted);
    if (aborted.length > 0) {
      const note = el(
        "p",
        { class: "muted", style: "margin-top: 4px; font-size: 12px;" },
      );
      const pieces = aborted.map(
        (p) => `Pod ${p.pod_index} stopped at ${p.games_actually_played}/${body.games_per_pod}`,
      );
      note.textContent =
        "Intra-pod stop (verdict locked mid-pod): " + pieces.join("; ");
      wrap.appendChild(note);
    }
  }

  // Save-to-knowledge-log block. Persists the audit_manifest + sim_report
  // + a manual verdict so Phase 3 ML has training rows. The default
  // verdict is suggested by the sim outcome (winner: new → kept,
  // winner: old → reverted, tie → neutral) but the user can override.
  wrap.appendChild(renderSaveIterationBlock(body));

  container.appendChild(wrap);
}

function renderSaveIterationBlock(body) {
  const block = el("div", { class: "save-iteration-block" });
  block.appendChild(el("h4", { style: "margin-top: 16px;" }, "Save to knowledge log"));
  if (!_lastAuditManifest || _lastAuditManifest.deck_id !== _activeDeckId) {
    block.appendChild(el(
      "p", { class: "muted" },
      "Run an audit first so the saved iteration includes the full add/cut manifest. "
      + "(You can still save without it; just the sim_report will be persisted.)",
    ));
  }
  // Verdict radios.
  const fs = el("fieldset", { class: "games-radio" });
  fs.appendChild(el("legend", {}, "Verdict:"));
  const defaultVerdict =
    body.winner === "new" ? "kept"
    : body.winner === "old" ? "reverted"
    : "neutral";
  for (const [val, label] of [
    ["kept", "Kept (apply changes)"],
    ["reverted", "Reverted (discard)"],
    ["neutral", "Neutral (inconclusive)"],
    ["pending", "Pending (decide later)"],
  ]) {
    const lbl = el("label");
    const inp = el("input", { type: "radio", name: "save-verdict", value: val });
    if (val === defaultVerdict) inp.checked = true;
    lbl.appendChild(inp);
    lbl.appendChild(document.createTextNode(" " + label));
    fs.appendChild(lbl);
  }
  block.appendChild(fs);

  // Notes textarea.
  const notesLabel = el("label", { class: "muted" }, "Notes (optional)");
  const notes = el("textarea", {
    id: "save-iteration-notes",
    rows: "2",
    placeholder: "Why did you keep / revert? (free text)",
    style: "width: 100%; margin-top: 4px;",
  });
  block.appendChild(notesLabel);
  block.appendChild(notes);

  // Save button + status line.
  const saveBtn = el("button", { class: "advise-btn" }, "Save iteration");
  const saveStatus = el("div", { class: "muted", style: "margin-top: 6px;" });
  saveBtn.addEventListener("click", async () => {
    const verdictEl = block.querySelector('input[name="save-verdict"]:checked');
    const verdict = verdictEl ? verdictEl.value : "pending";
    const notesText = (notes.value || "").trim();
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";

    // Compose audit_manifest from the most-recent audit (if any) plus
    // the diff returned by propose_swap. The diff is always reliable;
    // the manifest's rationale lines fall back to "" when no audit ran.
    const manifest = _lastAuditManifest && _lastAuditManifest.deck_id === _activeDeckId
      ? {
          added: _lastAuditManifest.added,
          removed: _lastAuditManifest.removed,
          diagnosis: _lastAuditManifest.diagnosis,
          weakness_signals: _lastAuditManifest.weakness_signals,
          diff_added: (body.diff && body.diff.added) || [],
          diff_removed: (body.diff && body.diff.removed) || [],
        }
      : {
          added: [],
          removed: [],
          diff_added: (body.diff && body.diff.added) || [],
          diff_removed: (body.diff && body.diff.removed) || [],
        };

    const deckName = (_activeDeckId || "")
      .replace(/^\[USER\]\s*/, "")
      .replace(/\s*\[B\d\]$/, "")
      .trim() || _activeDeckId;

    const payload = {
      deck_id: _activeDeckId,
      deck_name: deckName,
      bracket: body.bracket || (_lastAuditManifest && _lastAuditManifest.bracket) || 3,
      audit_version: (_lastAuditManifest && _lastAuditManifest.audit_version) || null,
      audit_manifest: manifest,
      sim_report: body,
      verdict,
      verdict_notes: notesText || null,
      // Prefer the audit response's post-swap price (captured into
      // _lastAuditManifest above) since it reflects the deck the user
      // is about to persist. Fall back to the dashboard snapshot
      // when no audit ran or it omitted pricing.
      total_price_usd:
        (_lastAuditManifest
          && _lastAuditManifest.deck_id === _activeDeckId
          && _lastAuditManifest.total_price_usd != null)
          ? _lastAuditManifest.total_price_usd
          : _lastDashboardPriceUsd,
    };
    try {
      const resp = await fetch("/api/save_iteration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const out = await resp.json();
      if (!resp.ok) {
        saveStatus.textContent = `Error: ${out.error || resp.status}${
          out.detail ? " — " + out.detail : ""
        }`;
        saveBtn.disabled = false;
        return;
      }
      const total = (out.stats && out.stats.total) || "?";
      saveStatus.textContent =
        `Saved iteration #${out.id} (verdict: ${out.verdict}). `
        + `knowledge_log now has ${total} rows.`;
      // Leave button disabled — the row is persisted; clicking again
      // would write a duplicate.
      // Auto-refresh the dashboard so the new iteration row appears
      // in the history panel (+ verdict breakdown + pricing sparkline
      // pick up the new data point) without a manual reload.
      // Soft-refresh keeps the existing rendered dashboard visible
      // while the new fetch runs in background.
      const li = document.querySelector(
        `.deck-list li[data-id="${_activeDeckId}"]`,
      );
      if (li) selectDeck(_activeDeckId, li, { soft: true });
    } catch (e) {
      saveStatus.textContent = `Network error: ${e.message}`;
      saveBtn.disabled = false;
    }
  });
  block.appendChild(saveBtn);
  block.appendChild(saveStatus);
  return block;
}

function rowEl(label, value) {
  const r = el("div", { class: "row" });
  r.appendChild(el("span", { class: "muted" }, label));
  r.appendChild(el("span", {}, value));
  return r;
}

// Most recent audit's proposed full-deck text — kept so the
// "Use this list" button can hand it to the Edit modal without
// re-fetching.
let _lastAuditProposed = null;

// Most recent audit's structured manifest (added/removed/diagnosis/bracket).
// Captured here so the post-sim "Save iteration" button can persist the
// full audit→sim record to knowledge_log.sqlite without a second API call.
// Keyed by deck id so switching decks mid-flow doesn't cross-contaminate.
let _lastAuditManifest = null;

// Most recent propose-swap response — needed by the "Save iteration"
// button on the result panel so it can persist the sim_report blob.
let _lastSimReport = null;

// Snapshot of the dashboard's est_price_usd at the time the user last
// loaded a deck. Travels into save_iteration as total_price_usd so the
// knowledge log accumulates a cost-over-time series we can chart later.
// Reset to null on deck switch so we never persist a stale price for
// a deck the user moved away from.
let _lastDashboardPriceUsd = null;

// Audit-backend preference + BYO API key. Stored in localStorage
// (browser-local; never sent anywhere except the active /api/audit
// request as the X-Anthropic-API-Key header). Per FP-011.
// The localStorage key keeps its legacy name "cb.audit.llm" so users
// who already toggled Claude don't lose their preference, but the
// accepted values expanded from "heuristic"/"claude" to also include
// "bracket_peers" (top-N highest-liked Moxfield decks at the same
// commander + bracket — see improvement_advisor._bracket_peers_recommendations).
const _LLM_PREF_KEY = "cb.audit.llm";
const _ANTHROPIC_KEY = "cb.audit.anthropic_key";
const _CLAUDE_MODEL_KEY = "cb.audit.claude_model";
const _BUDGET_KEY = "cb.audit.budget";

function getBudgetPref() {
  try { return localStorage.getItem(_BUDGET_KEY) === "1"; }
  catch (_e) { return false; }
}
function setBudgetPref(v) {
  try { localStorage.setItem(_BUDGET_KEY, v ? "1" : "0"); }
  catch (_e) { /* ignore */ }
}

const _AUDIT_SOURCE_OPTIONS = [
  {
    value: "heuristic",
    label: "EDHREC heuristic (default, free, all brackets averaged)",
  },
  {
    value: "bracket_peers",
    label: "Bracket-peers (top-5 same-bracket Moxfield decks, archetype-aware)",
  },
  {
    value: "claude",
    label: "Claude analyst (LLM, costs Anthropic tokens)",
  },
];

function _auditSourceLabel(value) {
  const opt = _AUDIT_SOURCE_OPTIONS.find((o) => o.value === value);
  return opt ? opt.label : value;
}

function _sourcePill(source) {
  // Pill colors:
  //   bracket_peers — good (the highest-quality default for tuned decks)
  //   claude        — good (LLM, but expensive)
  //   heuristic     — neutral (free baseline)
  if (source === "claude") return { cls: "pill good", text: "Claude analyst" };
  if (source === "bracket_peers")
    return { cls: "pill good", text: "Bracket-peers" };
  return { cls: "pill", text: "EDHREC heuristic" };
}

// Click a card thumbnail in the audit panel → full-size overlay.
// Pure JS overlay, no library — single img on top of a translucent
// scrim. Esc or click-outside closes. The full-size Scryfall variant
// is ~700×980 (200-400KB) so this fetch only fires on explicit click,
// not during scroll.
function openCardImageOverlay(cardName) {
  // Remove any prior overlay (defensive — click-spam can race).
  const prior = document.getElementById("_card-overlay");
  if (prior) prior.remove();
  const scrim = el(
    "div",
    {
      id: "_card-overlay",
      style: "position: fixed; inset: 0; background: rgba(0,0,0,0.7); "
           + "z-index: 1000; display: flex; align-items: center; "
           + "justify-content: center; cursor: pointer;",
    },
  );
  const img = el(
    "img",
    {
      src: cardImageUrl(cardName, "normal"),
      alt: cardName,
      style: "max-width: 90vw; max-height: 90vh; "
           + "border-radius: 8px; box-shadow: 0 4px 32px rgba(0,0,0,0.5);",
    },
  );
  scrim.appendChild(img);
  function close() {
    scrim.remove();
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  scrim.addEventListener("click", close);
  document.addEventListener("keydown", onKey);
  document.body.appendChild(scrim);
}

// Build a Scryfall image URL for a card name. The
// ``/cards/named?exact=<name>&format=image&version=<size>`` endpoint
// is a server-side redirect to the actual image asset — no API key
// or pre-fetch needed; the browser follows the redirect when the
// <img> tag loads. ``version=small`` is 146×204px (~15KB); use it
// for inline thumbnails. The ``loading="lazy"`` attr on the <img>
// element defers fetching until the row scrolls into view, so a
// 30-card audit panel doesn't fire 30 simultaneous redirects.
//
// FP-008 substrate from STATUS.md — card images alongside oracle
// text in the suggestions panel.
function cardImageUrl(name, size) {
  size = size || "small";
  return (
    "https://api.scryfall.com/cards/named"
    + `?exact=${encodeURIComponent(name)}`
    + `&format=image&version=${size}`
  );
}

// Per-card source badge for the "Cards to add" rows. The audit
// payload now carries a ``source`` string per rec; when match_pct
// is null (manabase essentials, vanilla Claude recs with no peer
// data), we render a compact badge here in place of the misleading
// "0%" pill that used to leak through. Bracket-peers and EDHREC
// heuristic recs DO have a numeric match_pct so they keep the
// percentage pill; we only fall back to a badge when match_pct is
// null. Returns null when no badge should render (the caller then
// shows the numeric pill instead).
//
// Keep the values in sync with evidence.source strings emitted by:
//   - _advisor_manabase    → "manabase_essentials" / "tribal_essentials"
//   - _advisor_claude      → "claude"
//   - _advisor_bracket_peers → "bracket_peers"
//   - _advisor_heuristic   → "edhrec.high_synergy" / "edhrec.top_cards" / "edhrec.absence"
function _perCardSourceBadge(source) {
  if (!source) return null;
  if (source === "manabase_essentials") {
    return {
      cls: "verdict pending",
      text: "Manabase",
      title: "Curated color-fixing essential — no inclusion% data applies",
    };
  }
  if (source === "tribal_essentials") {
    return {
      cls: "verdict pending",
      text: "Tribal",
      title: "Tribal-essential land (e.g. Cavern of Souls) for this deck's tribe",
    };
  }
  if (source === "claude") {
    return {
      cls: "verdict pending",
      text: "Claude",
      title: "Recommended by Claude analyst — no statistical signal score",
    };
  }
  // EDHREC heuristic / bracket_peers DO carry a numeric match_pct,
  // so we don't need a fallback badge for them. Caller renders the
  // percentage pill in that case.
  return null;
}

const _CLAUDE_MODEL_OPTIONS = [
  { value: "claude-haiku-4-5", label: "Haiku 4.5 (cheap, ~3-5× less than Sonnet)" },
  { value: "claude-sonnet-4-5", label: "Sonnet 4.5 (default, balanced)" },
  { value: "claude-opus-4-5", label: "Opus 4.5 (deepest reasoning, $$$)" },
];

function getClaudeModel() {
  try {
    return localStorage.getItem(_CLAUDE_MODEL_KEY) || "claude-sonnet-4-5";
  } catch (_e) { return "claude-sonnet-4-5"; }
}
function setClaudeModel(m) {
  try { localStorage.setItem(_CLAUDE_MODEL_KEY, m); }
  catch (_e) { /* ignore */ }
}

function getAuditLLMPref() {
  // Returns one of "heuristic" | "bracket_peers" | "claude".
  // Unknown values (legacy or corrupt localStorage) fall back to
  // heuristic so a bad write can't lock the user out of the audit.
  let stored;
  try { stored = localStorage.getItem(_LLM_PREF_KEY) || "heuristic"; }
  catch (_e) { return "heuristic"; }
  const valid = _AUDIT_SOURCE_OPTIONS.map((o) => o.value);
  return valid.includes(stored) ? stored : "heuristic";
}

// Auto-audit-on-dashboard-load preference. Default true: most users
// want to see a fast heuristic audit immediately rather than hunt
// for the "Run audit" button. Power users on slow networks can
// disable via the topbar checkbox (or by setting the localStorage
// key directly to "0"). Tier-2 issue #2.2 from the 2026-05-13
// ranked list — the streaming-audit infrastructure makes this
// cheap enough to enable by default since heuristic returns in
// ~100ms-1s.
const _AUTO_AUDIT_KEY = "auto_audit_on_dashboard_load";
function getAutoAuditPref() {
  try {
    const v = localStorage.getItem(_AUTO_AUDIT_KEY);
    // null = first-time user → default on. Explicit "0" → off.
    return v === null ? true : v !== "0";
  } catch (_e) { return true; }
}
function setAutoAuditPref(enabled) {
  try {
    localStorage.setItem(_AUTO_AUDIT_KEY, enabled ? "1" : "0");
  } catch (_e) { /* ignore */ }
}

function setAuditLLMPref(v) {
  try { localStorage.setItem(_LLM_PREF_KEY, v); } catch (_e) { /* ignore */ }
}
function getAnthropicKey() {
  try { return localStorage.getItem(_ANTHROPIC_KEY) || ""; }
  catch (_e) { return ""; }
}
function setAnthropicKey(k) {
  try {
    if (k) localStorage.setItem(_ANTHROPIC_KEY, k);
    else localStorage.removeItem(_ANTHROPIC_KEY);
  } catch (_e) { /* ignore */ }
}

// Whitelist of valid audit source values. Must mirror the server-side
// validation in routes_audit.py and _AUDIT_SOURCE_OPTIONS above.
// Anything not in this set is treated as no-override so the user's
// stored preference applies. Belt-and-suspenders against the kind of
// bug discovered 2026-05-15: ``button.addEventListener("click",
// loadAdvise)`` passes the PointerEvent as the first positional arg,
// which serialized as "[object PointerEvent]" in the URL and made
// the server reject the audit. Even with the binding fixed (callers
// now wrap in `() => loadAdvise()`), the type check here keeps a
// future regression from reaching the server.
const _VALID_AUDIT_SOURCES = new Set(["heuristic", "claude", "bracket_peers"]);

async function loadAdvise(sourceOverride) {
  // ``sourceOverride`` (optional) bypasses the user's stored
  // preference for THIS call only — used by the dashboard's
  // auto-kick (always forces "heuristic" since it runs without
  // a user click and shouldn't consume a Claude API quota), and
  // by the "Run with Claude" upgrade button (forces "claude").
  // When omitted, the user's stored preference applies as before.
  //
  // Defensive: only honor sourceOverride if it's a known-good string.
  // A DOM event (PointerEvent etc.) accidentally passed by a careless
  // binding is truthy but not a valid source — coerce to undefined so
  // the stored pref applies instead of letting "[object PointerEvent]"
  // reach the server.
  if (typeof sourceOverride !== "string"
      || !_VALID_AUDIT_SOURCES.has(sourceOverride)) {
    sourceOverride = undefined;
  }
  if (!_activeDeckId) return;
  const sug = $("sug-panel");
  if (!sug) return;
  const sourcePref = sourceOverride || getAuditLLMPref();
  // Only the Claude path needs a BYO key. Bracket-peers + heuristic
  // are key-free, so don't prompt the user for one.
  const byoKey = sourcePref === "claude" ? getAnthropicKey() : "";
  if (sourcePref === "claude" && !byoKey) {
    const k = window.prompt(
      "Paste your Anthropic API key (stored only in this browser; "
      + "sent only on audit requests). Cancel to skip and use heuristic.",
      "",
    );
    if (k && k.trim().startsWith("sk-")) {
      setAnthropicKey(k.trim());
    } else if (!sourceOverride) {
      // Only mutate the user's stored preference when this isn't an
      // explicit override — the auto-kick / upgrade button mustn't
      // silently change the persisted setting.
      setAuditLLMPref("heuristic");
    }
  }
  const sourceFinal = sourceOverride || getAuditLLMPref();
  const keyFinal = sourceFinal === "claude" ? getAnthropicKey() : "";

  // Restore the panel header (renderSuggestions strips children
  // beyond the <h3>; we want the header back when re-rendering).
  sug.innerHTML = "";
  sug.appendChild(el("h3", {}, "Audit — full proposed deck"));
  let statusMsg;
  if (sourceFinal === "claude") {
    statusMsg = "Generating ideal deck via Claude analyst (10–30s, "
              + "hits EDHREC + Anthropic)…";
  } else if (sourceFinal === "bracket_peers") {
    statusMsg = "Generating ideal deck from top-5 Moxfield decks at "
              + "this bracket (10–20s, hits Moxfield + Scryfall)…";
  } else {
    statusMsg = "Generating ideal deck (5–15s, hits EDHREC live)…";
  }
  sug.appendChild(el("p", { class: "muted" }, statusMsg));
  // Scroll the audit panel into view immediately so the user sees the
  // 'Generating…' status, not just an unresponsive Run audit button.
  sug.scrollIntoView({ behavior: "smooth", block: "start" });
  // Use the filename's [B?] suffix as the audit bracket.
  const bm = (_activeDeckId || "").match(/\[B(\d)\]/);
  const auditBracket = bm ? parseInt(bm[1], 10) : 3;
  // Cancel any previous in-flight audit stream. Without this, a
  // user who re-runs the audit (or switches decks) leaves the
  // previous Claude call running on the server — wasting tokens
  // and racing the new request's render.
  if (_auditAbortController) {
    try { _auditAbortController.abort(); } catch (_e) { /* ignore */ }
  }
  _auditAbortController = new AbortController();
  const auditSignal = _auditAbortController.signal;
  // Pin the deck this audit was kicked off for. If the user switches
  // decks mid-stream, the late ``complete`` event for the previous
  // deck shouldn't clobber the current panel.
  const auditDeckId = _activeDeckId;
  try {
    // Stream phases from /api/audit/stream so the user sees
    // intermediate progress (diagnosis → manabase → primary →
    // complete) rather than staring at "Generating…" for 6-8s
    // while Claude runs. The complete event carries the same
    // payload shape as the legacy /api/audit, so renderAuditResult
    // works unchanged once the final event arrives.
    let url =
      `/api/audit/stream?deck=${encodeURIComponent(auditDeckId)}`
      + `&bracket=${auditBracket}`
      + `&source=${encodeURIComponent(sourceFinal)}`;
    if (sourceFinal === "claude") {
      url += `&model=${encodeURIComponent(getClaudeModel())}`;
    }
    if (getBudgetPref()) {
      url += `&budget=1`;
    }
    const headers = {};
    if (keyFinal) headers["X-Anthropic-API-Key"] = keyFinal;
    // EventSource doesn't support custom headers (the BYO Claude
    // key case needs X-Anthropic-API-Key) so we use fetch + a
    // manual SSE reader. ``streamAuditEvents`` returns an async
    // iterator of ``{event, data}`` pairs.
    const completeBody = await streamAuditEvents(url, headers, {
      signal: auditSignal,
      onDiagnosis: (d) => updateAuditProgress(sug, "diagnosis", d, sourceFinal),
      onManabase: (m) => updateAuditProgress(sug, "manabase", m, sourceFinal),
      onPrimary: (p) => updateAuditProgress(sug, "primary", p, sourceFinal),
    });
    // If the user switched decks while we were streaming, the
    // current ``_activeDeckId`` differs from what we kicked off
    // with — drop the result silently rather than clobbering the
    // now-correct panel.
    if (_activeDeckId !== auditDeckId) return;
    _lastAuditProposed = completeBody.proposed_text || null;
    _lastAuditManifest = {
      deck_id: _activeDeckId,
      bracket: auditBracket,
      audit_version: completeBody.audit_version
        || (completeBody.source === "claude" ? "v3-claude" : "v3"),
      source: completeBody.source || "heuristic",
      added: (completeBody.added || []).map((a) => ({
        card: a.card,
        rationale: a.rationale || "",
        match_pct: a.match_pct,
        price_usd: a.price_usd,
      })),
      removed: (completeBody.removed || []).map((r) => ({
        card: r.card,
        rationale: r.rationale || "",
      })),
      diagnosis: completeBody.diagnosis || "",
      weakness_signals: completeBody.weakness_signals || [],
      // Post-swap deck price snapshot from the audit response.
      // Prefer proposed_price_usd (the price the user is about to
      // commit if they Save iteration); fall back to
      // original_price_usd when the audit produced no swap (heuristic
      // mode often leaves proposed null). Either flows through to
      // save_iteration as total_price_usd so the cost-over-time
      // series tracks what was actually persisted.
      total_price_usd: completeBody.proposed_price_usd != null
        ? completeBody.proposed_price_usd
        : completeBody.original_price_usd,
    };
    renderAuditResult(sug, completeBody);
  } catch (e) {
    // AbortError = user switched decks or re-ran the audit; the
    // previous stream's failure isn't an actual error worth
    // showing. The replacement audit (or a different panel) has
    // already taken over the DOM.
    if (e && e.name === "AbortError") return;
    sug.innerHTML = "";
    sug.appendChild(el("h3", {}, "Audit"));
    sug.appendChild(el("p", { class: "muted" }, `Audit failed: ${e.message}`));
  }
}

// Drive the /api/audit/stream SSE endpoint. Yields ``{event, data}``
// for each chunk to the supplied callbacks and resolves with the
// final ``complete`` event's payload. Throws on ``error`` events.
//
// We can't use the browser's native ``EventSource`` because it
// doesn't support custom request headers — and the Claude path
// needs ``X-Anthropic-API-Key``. So we parse SSE manually from the
// fetch response body. Buffering note: most browsers expose ReadableStream
// over the response body; we decode chunks as they arrive.
//
// ``callbacks.signal`` (optional) is an ``AbortSignal``; when it
// fires (because the user switched decks or re-triggered the
// audit), this function rejects with a sentinel ``"aborted"``
// error so the caller can silently drop the result instead of
// rendering a toast. We pass the signal to ``fetch`` so the
// underlying connection is closed too — important when the
// Claude path is the slow phase and the user shouldn't be billed
// for a stream they no longer want.
async function streamAuditEvents(url, headers, callbacks) {
  const signal = callbacks.signal;
  let resp;
  try {
    resp = await fetch(url, { headers, signal });
  } catch (e) {
    if (e && (e.name === "AbortError" || signal?.aborted)) {
      const err = new Error("aborted");
      err.name = "AbortError";
      throw err;
    }
    throw e;
  }
  if (!resp.ok) {
    // Input-validation errors (404 deck-not-found, 400 bad source)
    // return plain JSON, not SSE. Surface the server's error
    // message if available.
    let detail = `${url} -> ${resp.status}`;
    try {
      const errBody = await resp.json();
      if (errBody && errBody.error) detail = errBody.error;
    } catch (_e) { /* ignore JSON parse failure */ }
    throw new Error(detail);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let completeData = null;
  while (true) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (e) {
      if (e && (e.name === "AbortError" || signal?.aborted)) {
        const err = new Error("aborted");
        err.name = "AbortError";
        throw err;
      }
      throw e;
    }
    const { value, done } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line (\n\n). Parse and
    // dispatch any complete frames in the buffer; keep the tail
    // for the next read.
    let sepIdx;
    while ((sepIdx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      const parsed = _parseSseFrame(frame);
      if (!parsed) continue;
      const { event, data } = parsed;
      if (event === "error") {
        throw new Error(data.error || data.detail || "audit error");
      }
      if (event === "diagnosis" && callbacks.onDiagnosis) {
        callbacks.onDiagnosis(data);
      } else if (event === "manabase" && callbacks.onManabase) {
        callbacks.onManabase(data);
      } else if (event === "primary" && callbacks.onPrimary) {
        callbacks.onPrimary(data);
      } else if (event === "complete") {
        completeData = data;
      }
    }
  }
  if (!completeData) {
    throw new Error("audit stream ended without complete event");
  }
  return completeData;
}

// Parse a single SSE frame (text between two blank-line separators)
// into ``{event, data}``. Returns null when the frame has no
// ``event:`` line (heartbeat, comment-only frame). Tolerates
// multi-line ``data:`` continuations per the spec.
function _parseSseFrame(frame) {
  let event = null;
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!event || !dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch (_e) {
    return null;
  }
}

// Update the audit-in-progress panel as each phase arrives. Shows
// a live progress line so the user can tell what's happening
// during the 6-8s Claude call instead of staring at a static
// "Generating…" message.
//
// In addition to the status line, the manabase phase renders a
// preview list of the curated essentials (Sacred Foundry, Cavern
// of Souls, etc.) immediately — these don't depend on the slow
// primary source and the user benefits from seeing them right
// away rather than waiting 6-8s for the Claude path to finish.
// ``renderAuditResult`` rebuilds the panel with innerHTML="" once
// the complete event arrives, so the preview gets replaced
// cleanly without leaving stale DOM.
function updateAuditProgress(sug, phase, data, sourceFinal) {
  // Find or create the dedicated progress paragraph. We replace
  // its text per phase rather than appending so the panel doesn't
  // accumulate stale status lines.
  let prog = sug.querySelector(".audit-progress");
  if (!prog) {
    prog = el("p", { class: "muted audit-progress" }, "");
    sug.appendChild(prog);
  }
  if (phase === "diagnosis") {
    const commander = (data.commander_names || [])[0] || "this deck";
    prog.textContent = `Analyzing ${commander}…`;
  } else if (phase === "manabase") {
    const n = (data.recommendations || []).length;
    const tribe = data.tribe ? ` (${data.tribe} tribal lands included)` : "";
    if (n > 0) {
      prog.textContent =
        `Manabase scan: ${n} essential land${n === 1 ? "" : "s"} `
        + `missing${tribe}. Now fetching ${
          sourceFinal === "claude" ? "Claude analyst" :
          sourceFinal === "bracket_peers" ? "bracket-peer references"
          : "EDHREC heuristic"
        }…`;
      renderManabasePreview(sug, data.recommendations);
    } else {
      prog.textContent =
        `Manabase looks complete${tribe}. Now fetching ${
          sourceFinal === "claude" ? "Claude analyst" :
          sourceFinal === "bracket_peers" ? "bracket-peer references"
          : "EDHREC heuristic"
        }…`;
    }
  } else if (phase === "primary") {
    const n = (data.recommendations || []).length;
    const eff = data.effective_source;
    const fallback = data.fallback_reason
      ? ` (fell back to ${eff} — ${data.fallback_reason})`
      : "";
    prog.textContent =
      `Source returned ${n} candidate${n === 1 ? "" : "s"} from ${eff}${
        fallback}. Finalizing…`;
  }
}

// Render an early-preview sublist of the manabase essentials the
// stream's ``manabase`` event ships. Each rec carries
// ``evidence.source`` ("manabase_essentials" or "tribal_essentials")
// — we surface that via ``_perCardSourceBadge`` so the row's
// verdict cell is the same "Manabase" / "Tribal" badge the final
// render uses. This visual continuity is intentional: the user
// shouldn't see the preview pills change shape when the complete
// event lands.
//
// Idempotent: re-rendering replaces any existing preview rather
// than appending. The streaming layer only fires the manabase
// event once per audit, but this guards against duplicate
// emissions in test/mock scenarios.
function renderManabasePreview(sug, recs) {
  const existing = sug.querySelector(".audit-manabase-preview");
  if (existing) existing.remove();
  if (!recs || !recs.length) return;
  const wrap = el("div", { class: "audit-manabase-preview" });
  wrap.appendChild(el(
    "h4",
    { style: "margin-top: 12px;" },
    `Manabase essentials (${recs.length} preview)`,
  ));
  // Tooltip explaining why these appear before the rest — keeps
  // the streaming behavior discoverable to first-time users.
  const note = el(
    "p", { class: "muted", style: "font-size: 12px; margin: 0 0 6px;" },
    "Curated color-fixing essentials — surfaced early; "
    + "rest of audit loading…",
  );
  wrap.appendChild(note);
  const ul = el("ul", { class: "iteration-list" });
  // Filter to adds only; the manabase phase never emits cuts but
  // be defensive in case the contract changes.
  for (const a of recs.filter((r) => r.action === "add")) {
    const row = el("li", { class: "iteration" });
    const source = (a.evidence || {}).source || null;
    const badge = _perCardSourceBadge(source);
    if (badge) {
      const badgeEl = el("span", { class: badge.cls }, badge.text);
      if (badge.title) badgeEl.title = badge.title;
      row.appendChild(badgeEl);
    } else {
      row.appendChild(el("span", { class: "verdict pending" }, "—"));
    }
    const wrap2 = el("div");
    wrap2.appendChild(el("div", { class: "name" }, a.card));
    if (a.reason) {
      wrap2.appendChild(el("div", { class: "muted" }, a.reason));
    }
    row.appendChild(wrap2);
    ul.appendChild(row);
  }
  wrap.appendChild(ul);
  sug.appendChild(wrap);
}

function renderAuditBackendRow(body) {
  const row = el("div", { class: "audit-backend-row",
    style: "display: flex; gap: 8px; align-items: center; "
         + "flex-wrap: wrap; margin: 4px 0 10px;" });
  // Source pill — shows what actually ran (independent of the
  // *requested* backend, so a silent fallback to heuristic is visible).
  const source = body.source || "heuristic";
  const pill = _sourcePill(source);
  // Disclose peer-enrichment when Claude actually shipped bracket-peer
  // references in its prompt. Lets users tell "Claude saw 5 tuned
  // same-bracket decks" apart from "Claude only had EDHREC averages."
  const peerRefs = body.bracket_peer_ref_count || 0;
  const pillText = (source === "claude" && peerRefs > 0)
    ? `${pill.text} (${peerRefs} peer ref${peerRefs === 1 ? "" : "s"})`
    : pill.text;
  const pillEl = el("span", { class: pill.cls }, pillText);
  if (source === "claude" && peerRefs > 0) {
    pillEl.title = `Claude's prompt included ${peerRefs} top-liked `
      + `Moxfield deck${peerRefs === 1 ? "" : "s"} at this bracket as `
      + `archetype-specific reference data.`;
  }
  row.appendChild(pillEl);

  // Requested-vs-actual disclosure: when the user asked for X but
  // got Y (e.g. bracket_peers found no references → fell back to
  // heuristic), show a small note next to the pill. The full reason
  // is also surfaced as body.warning above the diff list.
  const requested = body.requested_source || body.requested_llm;
  if (requested && requested !== source) {
    row.appendChild(el(
      "span",
      { class: "muted", style: "font-size: 11px;" },
      `(requested ${requested})`,
    ));
  }

  // "Run with Claude" upgrade button — only when the current result
  // is a non-Claude source. The auto-kick on dashboard load runs
  // heuristic (since it can't consume a Claude API quota
  // unilaterally), so this is the path the user takes to upgrade
  // the result with the better backend. Tier-2 issue #2.2.
  if (source !== "claude") {
    const upgradeBtn = el(
      "button",
      {
        class: "advise-btn",
        style: "padding: 4px 10px; font-size: 12px;",
        title: "Re-run this audit with the Claude analyst "
             + "(slower, ~6-8s; needs an Anthropic API key).",
      },
      "↗ Run with Claude",
    );
    upgradeBtn.addEventListener("click", () => loadAdvise("claude"));
    row.appendChild(upgradeBtn);
  }

  // Selector — switch backend for the *next* audit. This run already
  // produced `body`; rerunning with the new pref re-fetches.
  const selectLabel = el("label",
    { style: "font-size: 12px; display: flex; gap: 4px; "
           + "align-items: center;" });
  selectLabel.appendChild(document.createTextNode("Source:"));
  const selector = el("select",
    { style: "font-size: 12px; padding: 2px 6px;" });
  const currentPref = getAuditLLMPref();
  for (const opt of _AUDIT_SOURCE_OPTIONS) {
    const o = el("option", { value: opt.value }, opt.label);
    if (opt.value === currentPref) o.selected = true;
    selector.appendChild(o);
  }
  selector.addEventListener("change", () => {
    setAuditLLMPref(selector.value);
  });
  selectLabel.appendChild(selector);
  row.appendChild(selectLabel);

  // Model dropdown — only meaningful when Claude is selected, but
  // always visible so users discover it. The Haiku option is the
  // cost-conscious default for routine audits.
  const modelSelect = el("select",
    { style: "font-size: 12px; padding: 2px 6px;" });
  const currentModel = getClaudeModel();
  for (const opt of _CLAUDE_MODEL_OPTIONS) {
    const o = el("option", { value: opt.value }, opt.label);
    if (opt.value === currentModel) o.selected = true;
    modelSelect.appendChild(o);
  }
  modelSelect.addEventListener("change", () => {
    setClaudeModel(modelSelect.value);
  });
  row.appendChild(modelSelect);

  // Budget toggle — skips ABU duals + fetches from the manabase
  // safety net for users who explicitly opted out of $200+ cards.
  // Shocks + bond lands + utility fixers still surface.
  const budgetLabel = el("label",
    { style: "font-size: 12px; display: flex; gap: 4px; "
           + "align-items: center;",
      title: "Skip $200+ ABU duals + $25-60 fetch lands. "
           + "Shocks, bond lands, and utility fixers still recommended." });
  const budgetBox = el("input", { type: "checkbox" });
  budgetBox.checked = getBudgetPref();
  budgetBox.addEventListener("change", () => {
    setBudgetPref(budgetBox.checked);
  });
  budgetLabel.appendChild(budgetBox);
  budgetLabel.appendChild(document.createTextNode(" Budget mode"));
  row.appendChild(budgetLabel);

  // Manage-key button — clears stored key or prompts for a new one.
  const keyBtn = el("button",
    { class: "advise-btn", style: "padding: 4px 10px; font-size: 12px;" },
    getAnthropicKey() ? "Replace API key" : "Set API key",
  );
  keyBtn.addEventListener("click", () => {
    const k = window.prompt(
      "Anthropic API key (leave empty to forget). "
      + "Stored in this browser only; sent only as the "
      + "X-Anthropic-API-Key header on audit requests.",
      "",
    );
    if (k === null) return;       // cancel
    if (k.trim() === "") {
      setAnthropicKey("");
      keyBtn.textContent = "Set API key";
    } else if (k.trim().startsWith("sk-")) {
      setAnthropicKey(k.trim());
      keyBtn.textContent = "Replace API key";
    } else {
      window.alert("API key should start with 'sk-'.");
    }
  });
  row.appendChild(keyBtn);
  return row;
}

function renderAuditResult(container, body) {
  // Rebuild the panel — header + LLM toggle + source pill +
  // diagnosis + diff lists + "Use this list" + collapsible preview.
  container.innerHTML = "";
  container.appendChild(el("h3", {}, "Audit — full proposed deck"));

  // Backend toggle row: shows current source + lets the user switch
  // between EDHREC heuristic and Claude analyst (Moxfield audit prompt).
  container.appendChild(renderAuditBackendRow(body));

  if (body.warning) {
    container.appendChild(el(
      "p", { class: "pill bad", style: "display: inline-block;" },
      body.warning,
    ));
  }

  // Salt-warning banner — fires at B1/B2/B3 when the user's CURRENT
  // deck carries cards on EDHREC's salt list. The advisor's per-rec
  // salt annotations elsewhere in the panel handle the recommended
  // cards; this banner is the aggregate view: "your deck has N
  // salty picks at bracket B — consider cutting these." Hidden at
  // B4/B5 where salt is expected, or when the server didn't ship
  // the field (legacy clients / no salt data).
  if (body.salt_warning) {
    container.appendChild(renderSaltWarningBanner(body.salt_warning));
  }

  // Deck-health tile row -- compact at-a-glance signals the advisor's
  // narrative diagnosis doesn't directly surface: MDFC count, spell
  // density, mana sinks, wincon-specific protection, self-mill
  // enablement. Each tile is clickable to expand a tooltip with the
  // contributing card names. Renders only when the server shipped a
  // deck_health payload (legacy clients without the field stay clean).
  if (body.deck_health) {
    container.appendChild(renderDeckHealthTiles(body.deck_health));
  }

  if (body.diagnosis) {
    container.appendChild(el(
      "p", {}, el("span", { class: "muted" }, "Diagnosis: "),
      body.diagnosis,
    ));
  }
  if (body.weakness_signals && body.weakness_signals.length) {
    const ul = el("ul", { class: "muted", style: "font-size: 12px;" });
    for (const w of body.weakness_signals) ul.appendChild(el("li", {}, w));
    container.appendChild(ul);
  }

  // Counts headline.
  const headline = el(
    "p", {},
    `Proposed: ${body.main_count ?? "?"} mainboard cards `,
    el("span", { class: "pill good" }, `${body.added.length} added`),
    " ",
    el("span", { class: "pill bad" }, `${body.removed.length} removed`),
  );
  container.appendChild(headline);

  // Price-delta headline. Shows "$420 → $537 (+$117)" when the
  // audit produced any applied swaps. Null prices (Scryfall down /
  // all-digital deck) render as "—" with a tooltip explaining why.
  // Tier-2 backlog item: feeds the cost-evolution chart's per-swap
  // delta and lets budget-mode users see audit cost impact at a
  // glance.
  if (body.original_price_usd != null || body.proposed_price_usd != null) {
    const orig = body.original_price_usd;
    const prop = body.proposed_price_usd;
    const delta = body.price_delta_usd;
    const fmt = (v) => v == null ? "—" : `$${Number(v).toFixed(2)}`;
    const deltaText = delta == null
      ? ""
      : (delta >= 0 ? `(+${fmt(delta).slice(1)})` : `(-${fmt(-delta).slice(1)})`);
    const deltaCls = delta == null
      ? "muted"
      : (delta > 5 ? "pill bad" : (delta < -5 ? "pill good" : "muted"));
    const priceP = el(
      "p", { style: "margin: 4px 0 6px; font-size: 13px;" },
      el("span", { class: "muted" }, "Cost: "),
      `${fmt(orig)} → ${fmt(prop)} `,
    );
    if (delta != null) {
      priceP.appendChild(el(
        "span",
        {
          class: deltaCls,
          style: "display: inline-block; padding: 1px 6px;",
          title:
            delta > 0 ? "Audit raises deck cost"
            : delta < 0 ? "Audit lowers deck cost"
            : "Audit is cost-neutral",
        },
        deltaText,
      ));
    }
    // Footnote when not all cards have prices — keeps the user
    // from misreading a partial total as authoritative.
    const origN = body.n_priced_cards_original ?? 0;
    const propN = body.n_priced_cards_proposed ?? 0;
    if (origN < 99 || propN < 99) {
      priceP.appendChild(el(
        "span",
        { class: "muted",
          style: "margin-left: 8px; font-size: 11px;",
          title: `${origN}/${propN} cards have Scryfall prices in `
               + `the original/proposed deck respectively.`,
        },
        `(${Math.min(origN, propN)} cards priced)`,
      ));
    }
    container.appendChild(priceP);
  }

  // Hallucination summary — non-zero when the Claude analyst invented
  // card names that Scryfall doesn't recognize. Individual rows below
  // get a ⚠ pill; this gives an at-a-glance count.
  if (body.unknown_card_count && body.unknown_card_count > 0) {
    container.appendChild(el(
      "p", { class: "pill bad", style: "display: inline-block;" },
      `⚠ ${body.unknown_card_count} card name`
      + (body.unknown_card_count === 1 ? "" : "s")
      + ` not in Scryfall — likely Claude hallucination`,
    ));
  }

  // Saturation-guard summary — when the deck already has enough cards
  // in a role bucket (ramp/draw/etc.) the advisor drops redundant adds.
  // Show the user what got filtered so a short list isn't mistaken for
  // "the advisor gave up". Grouped by role for readability.
  const skipped = body.skipped_for_saturation || [];
  if (skipped.length > 0) {
    // Group by role: { ramp: [{card,deck_count,threshold},...], draw: [...] }
    const byRole = {};
    for (const s of skipped) {
      const r = s.role || "other";
      if (!byRole[r]) byRole[r] = [];
      byRole[r].push(s);
    }
    const parts = Object.entries(byRole).map(([role, items]) => {
      // Every item in the same role bucket shares the same deck_count
      // and threshold; just read the first.
      const dc = items[0].deck_count;
      const th = items[0].threshold;
      return `${items.length} ${role} (you have ${dc}, threshold ${th})`;
    });
    const block = el(
      "p",
      { class: "muted", style: "font-size: 12px; margin-top: 4px;" },
      `Skipped ${skipped.length} redundant add`
      + (skipped.length === 1 ? "" : "s")
      + `: ${parts.join("; ")}.`,
    );
    // Tooltip lists the actual card names that got filtered so power
    // users can review without expanding the rec list.
    block.title = skipped.map((s) => `${s.card} (${s.role})`).join("\n");
    container.appendChild(block);
  }

  // Sub-100 padding warning. When the source deck was short of legal
  // size, we top up with basic lands mirroring its color distribution
  // so Forge will load it. Tell the user that synthetic basics landed.
  if (body.basics_padded && body.basics_padded > 0) {
    const breakdown = body.basics_padded_breakdown || {};
    const parts = Object.entries(breakdown)
      .filter(([_, n]) => n > 0)
      .map(([name, n]) => `${n} ${name}`)
      .join(", ");
    container.appendChild(el(
      "p",
      { class: "muted", style: "font-size: 12px;" },
      `Source deck was sub-100; padded with `
      + (parts || `${body.basics_padded} basics`)
      + ` to reach 99 mainboard.`,
    ));
  }

  // "Use this list" button → drops the proposed text into the Edit
  // modal so the user can preview / tweak before running A/B sim.
  const useBtn = el("button", { class: "advise-btn" }, "Use this list (open editor)");
  useBtn.addEventListener("click", () => {
    if (!_lastAuditProposed) return;
    openProposeModal({ saveOnly: false }).then(() => {
      // openProposeModal pre-fills the textarea from /api/deck_text;
      // overwrite with the proposed text after that returns.
      $("propose-text").value = _lastAuditProposed;
      $("propose-status").textContent =
        "Proposed deck loaded. Pick a game count and run the A/B sim.";
    });
  });
  container.appendChild(useBtn);

  // "Save audit (no sim)" — persists the manifest as a pending row
  // so Phase 3 ML has data even when the user inspects the audit
  // and reverts before running an A/B sim. The verdict can be
  // promoted later via /api/iteration/<id> if the user changes their
  // mind. Composes with Save iteration on the propose-swap result
  // panel — same row, just no sim_report yet.
  const saveAuditBtn = el(
    "button",
    {
      class: "advise-btn",
      style: "margin-left: 8px; background: var(--bg); "
           + "border: 1px solid var(--border); color: var(--text);",
    },
    "Save audit to log (no sim)",
  );
  const saveAuditStatus = el(
    "span", { class: "muted", style: "margin-left: 8px; font-size: 12px;" },
  );
  saveAuditBtn.addEventListener("click", async () => {
    if (!_lastAuditManifest || _lastAuditManifest.deck_id !== _activeDeckId) {
      saveAuditStatus.textContent = "Run an audit first.";
      return;
    }
    saveAuditBtn.disabled = true;
    saveAuditStatus.textContent = "Saving…";
    const deckName = (_activeDeckId || "")
      .replace(/^\[USER\]\s*/, "")
      .replace(/\s*\[B\d\]$/, "")
      .trim() || _activeDeckId;
    const payload = {
      deck_id: _activeDeckId,
      deck_name: deckName,
      bracket: _lastAuditManifest.bracket || 3,
      audit_version: _lastAuditManifest.audit_version || null,
      audit_manifest: {
        added: _lastAuditManifest.added,
        removed: _lastAuditManifest.removed,
        diagnosis: _lastAuditManifest.diagnosis,
        weakness_signals: _lastAuditManifest.weakness_signals,
      },
      sim_report: null,
      verdict: "pending",
      verdict_notes: "Audit-only save (no A/B sim run).",
      // Audit just ran (early-return above guarantees the manifest
      // matches the active deck), so its post-swap price is the
      // freshest signal. Fall back to the dashboard snapshot only
      // when the audit response omitted pricing fields.
      total_price_usd:
        _lastAuditManifest.total_price_usd != null
          ? _lastAuditManifest.total_price_usd
          : _lastDashboardPriceUsd,
    };
    try {
      const resp = await fetch("/api/save_iteration", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const out = await resp.json();
      if (!resp.ok) {
        saveAuditStatus.textContent = `Error: ${out.error || resp.status}`;
        saveAuditBtn.disabled = false;
        return;
      }
      const total = (out.stats && out.stats.total) || "?";
      saveAuditStatus.textContent =
        `Saved audit #${out.id} (pending). knowledge_log: ${total} rows.`;
      // Auto-refresh so the new row appears in the dashboard's
      // iteration history without a manual reload. Same soft-refresh
      // pattern as the post-sim save flow.
      const li = document.querySelector(
        `.deck-list li[data-id="${_activeDeckId}"]`,
      );
      if (li) selectDeck(_activeDeckId, li, { soft: true });
    } catch (e) {
      saveAuditStatus.textContent = `Network error: ${e.message}`;
      saveAuditBtn.disabled = false;
    }
  });
  container.appendChild(saveAuditBtn);
  container.appendChild(saveAuditStatus);

  // Adds list. Split into two visual buckets so the user sees at
  // a glance which recs are drop-in (in the proposed deck — safe
  // to ship to Forge) vs which are suggestions that need a manual
  // cut to absorb. The split is driven by the ``applied`` flag the
  // server now ships on every add entry; entries without the flag
  // (older payloads) default to applied=true so behavior is
  // unchanged for them.
  //
  // Why split rather than inline-tag? The two buckets ARE
  // semantically different — one set is part of an audit-applied
  // diff, the other is recommendation-only. Grouping makes that
  // distinction visually obvious without forcing the user to read
  // pills row-by-row.
  if (body.added.length) {
    const applied = body.added.filter(
      (a) => a.applied !== false,
    );
    const suggested = body.added.filter(
      (a) => a.applied === false,
    );

    // Helper to render one card row — shared between both buckets
    // so styling stays consistent.
    function renderAddRow(a, opts) {
      const dim = opts && opts.dim;
      const row = el(
        "li",
        {
          class: "iteration",
          style: dim ? "opacity: 0.78;" : "",
        },
      );
      // Verdict cell: percentage pill when the rec has a numeric
      // signal, otherwise a source-specific badge.
      if (typeof a.match_pct === "number") {
        row.appendChild(el(
          "span", { class: "verdict pending" }, `${a.match_pct}%`,
        ));
      } else {
        const badge = _perCardSourceBadge(a.source);
        if (badge) {
          const badgeEl = el("span", { class: badge.cls }, badge.text);
          if (badge.title) badgeEl.title = badge.title;
          row.appendChild(badgeEl);
        } else {
          row.appendChild(el("span", { class: "verdict pending" }, "—"));
        }
      }
      const wrap = el("div");
      const nameDiv = el("div", { class: "name" }, a.card);
      if (a.name_known === false) {
        nameDiv.appendChild(el(
          "span",
          {
            class: "pill bad",
            style: "margin-left: 6px; font-size: 11px;",
            title: "Not found in Scryfall — likely a hallucinated card name",
          },
          "⚠ not in Scryfall",
        ));
      }
      // EDHREC salt-score pill — surfaces when a rec is in the
      // top-100 saltiest cards (Cyclonic Rift, Smothering Tithe,
      // Rhystic Study, Stasis, ...). The pill's color escalates
      // with the score; B1-B3 users see this as a "are you sure?"
      // signal when the audit recommends a high-salt card.
      if (typeof a.salt === "number" && a.salt >= 2.0) {
        // Salt score 2.0-2.4 = warn (yellowish), 2.5+ = bad (red).
        const cls = a.salt >= 2.5 ? "pill bad" : "pill warn";
        nameDiv.appendChild(el(
          "span",
          {
            class: cls,
            style: "margin-left: 6px; font-size: 11px;",
            title: `EDHREC salt score ${a.salt.toFixed(2)}/5.0. `
                 + `Top-100 saltiest cards are often unpopular with `
                 + `opponents at lower brackets — consider whether `
                 + `your playgroup is okay with this pick.`,
          },
          `salt ${a.salt.toFixed(1)}`,
        ));
      }
      wrap.appendChild(nameDiv);
      if (a.rationale) {
        wrap.appendChild(el("div", { class: "muted" }, a.rationale));
      }
      row.appendChild(wrap);
      // Card thumbnail (FP-008). Only render for recs we expect
      // Scryfall to know about; ``name_known === false`` means the
      // validator already confirmed Scryfall doesn't have the
      // card. ``loading="lazy"`` defers the fetch until the row
      // scrolls into view — a 30-card audit panel doesn't fire
      // 30 simultaneous HTTPS round-trips. ``decoding="async"``
      // keeps image decoding off the main thread so the panel
      // scrolls smoothly even on slow hardware.
      if (a.name_known !== false) {
        const thumb = el(
          "img",
          {
            src: cardImageUrl(a.card, "small"),
            loading: "lazy",
            decoding: "async",
            alt: a.card,
            title: `${a.card} — click to view full size`,
            style: "width: 60px; height: 84px; "
                 + "border-radius: 3px; "
                 + "object-fit: cover; cursor: pointer; "
                 + "margin-left: 8px;",
          },
        );
        // Click to expand to a modal-ish overlay with the full
        // image. Keeps the row compact while still letting users
        // read the card text without leaving the audit panel.
        thumb.addEventListener("click", () => openCardImageOverlay(a.card));
        row.appendChild(thumb);
      }
      row.appendChild(el(
        "span", { class: "delta" },
        a.price_usd != null ? `$${Number(a.price_usd).toFixed(2)}` : "",
      ));
      return row;
    }

    if (applied.length) {
      container.appendChild(el(
        "h4", { style: "margin-top: 14px;" },
        `Cards to add (${applied.length} in proposed deck)`,
      ));
      const ul = el("ul", { class: "iteration-list" });
      for (const a of applied) ul.appendChild(renderAddRow(a, { dim: false }));
      container.appendChild(ul);
    }

    if (suggested.length) {
      container.appendChild(el(
        "h4",
        {
          style: "margin-top: 14px; color: var(--muted, #888);",
          title: "These recs aren't in the proposed deck text because "
               + "the auditor couldn't find a high-confidence cut to "
               + "balance them. Use 'Run with Claude' for a fuller swap "
               + "list, or cherry-pick what you want and choose cuts "
               + "manually.",
        },
        `Also suggested (${suggested.length} — needs manual cut)`,
      ));
      const note = el(
        "p",
        { class: "muted", style: "font-size: 12px; margin: 0 0 6px;" },
        "Strong recommendations the auto-balancer couldn't fit. "
        + "Add to your deck manually + pick a cut yourself.",
      );
      container.appendChild(note);
      const ul = el("ul", { class: "iteration-list" });
      for (const a of suggested) ul.appendChild(renderAddRow(a, { dim: true }));
      container.appendChild(ul);
    }
  }

  // Removed list.
  if (body.removed.length) {
    container.appendChild(el(
      "h4", { style: "margin-top: 14px;" }, "Cards to cut",
    ));
    // Build a case-folded set of protected card names so the
    // cuts-list renderer can badge them with a 🔒 pill. Protected
    // cards live in the .dck file's [metadata] Protect= entries
    // (read server-side, shipped in body.protected_cards) and tell
    // the user "the curator won't act on this even if the advisor
    // suggested it." Empty set when no protection is configured.
    const protectedSet = new Set(
      (body.protected_cards || []).map((n) => (n || "").toLowerCase()),
    );
    const ul = el("ul", { class: "iteration-list" });
    for (const r of body.removed) {
      const isProtected = protectedSet.has((r.card || "").toLowerCase());
      const row = el("li", {
        class: "iteration",
        style: isProtected ? "opacity: 0.55;" : "",
      });
      row.appendChild(el("span", { class: "verdict reverted" }, "cut"));
      const wrap = el("div");
      const nameDiv = el("div", { class: "name" }, r.card);
      if (isProtected) {
        nameDiv.appendChild(el(
          "span",
          {
            class: "pill",
            style: "margin-left: 6px; font-size: 11px; "
                 + "background: rgba(96, 165, 250, 0.15); "
                 + "color: #60a5fa; border: 1px solid #60a5fa; "
                 + "padding: 1px 6px; border-radius: 4px; "
                 + "font-weight: 600;",
            title: "Protected by [metadata] Protect= — "
                 + "commander-auto-curate will skip this cut. "
                 + "Remove the Protect= entry from the .dck to unlock.",
          },
          "🔒 protected",
        ));
      }
      if (r.name_known === false) {
        nameDiv.appendChild(el(
          "span",
          {
            class: "pill bad",
            style: "margin-left: 6px; font-size: 11px;",
            title: "Not found in Scryfall — likely a hallucinated card name",
          },
          "⚠ not in Scryfall",
        ));
      }
      // Cutting a salty card is good news — surface the score
      // as a "good" pill so the user sees the audit reduced
      // table-talk-problematic picks.
      if (typeof r.salt === "number" && r.salt >= 2.0) {
        nameDiv.appendChild(el(
          "span",
          {
            class: "pill good",
            style: "margin-left: 6px; font-size: 11px;",
            title: `Cutting a salty card (EDHREC salt ${r.salt.toFixed(2)}/5.0). `
                 + `Lower-bracket playgroups will appreciate the reduction.`,
          },
          `cut salt ${r.salt.toFixed(1)}`,
        ));
      }
      wrap.appendChild(nameDiv);
      if (r.rationale) {
        wrap.appendChild(el("div", { class: "muted" }, r.rationale));
      }
      row.appendChild(wrap);
      row.appendChild(el("span", { class: "delta" }, ""));
      ul.appendChild(row);
    }
    container.appendChild(ul);
  }

  // EDHREC bracket-specific average-deck preview. Renders a
  // collapsible <details> grouped by EDHREC category (Creatures /
  // Lands / Ramp / ...) so users can compare their list to the
  // bracket archetype at a glance without leaving the audit panel.
  // Lazily — DOM only builds when the user opens the section.
  if (body.average_deck_preview && body.average_deck_preview.card_count > 0) {
    container.appendChild(renderAverageDeckPreview(body.average_deck_preview));
  }

  // Collapsible full-deck preview.
  const details = el("details", { style: "margin-top: 14px;" });
  details.appendChild(el(
    "summary", { class: "muted" }, "Show full proposed deck text",
  ));
  const pre = el("pre", {
    style: "background: var(--bg); border: 1px solid var(--border); "
         + "border-radius: 6px; padding: 10px; max-height: 320px; "
         + "overflow: auto; font-size: 12px;",
  });
  pre.textContent = body.proposed_text || "(empty)";
  details.appendChild(pre);
  container.appendChild(details);
}

// ---------------------------------------------------------------------------
// Iteration-graph SVG renderer
// ---------------------------------------------------------------------------
//
// Renders the {nodes, edges} payload from /api/iteration_graph as a
// left-to-right SVG flow chart. Each node is a rounded rect labeled
// with the iteration number, bracket, verdict pill, and card count.
// Each edge is a directed arrow labeled with "+N -M" (adds/cuts) and
// the $-delta when both endpoints have pricing.
//
// Vanilla SVG (no D3) — keeps the dependency footprint zero. Layout
// is a simple horizontal chain ordered by iteration_n; forked chains
// stack vertically so all components stay visible.

const _GRAPH_NODE_W = 160;
const _GRAPH_NODE_H = 64;
const _GRAPH_NODE_GAP_X = 80;
const _GRAPH_NODE_GAP_Y = 24;
const _GRAPH_PAD = 16;

function svgEl(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    node.setAttribute(k, String(v));
  }
  for (const c of children) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function renderIterationGraph(data) {
  const nodes = data.nodes || [];
  const edges = data.edges || [];
  if (!nodes.length) {
    return el("p", { class: "muted" }, "No iterations yet.");
  }

  // Compute the row layout: each connected component gets its own
  // row so forked chains don't overlap. parentMap: child id → parent id.
  const parentMap = new Map();
  for (const e of edges) parentMap.set(e.to_id, e.from_id);

  // Walk forward from each chain root (no parent) until the chain ends.
  // Roots get assigned a row; children inherit their parent's row.
  const rowOf = new Map();
  const componentRoots = [];
  for (const n of nodes) {
    if (!parentMap.has(n.id)) {
      componentRoots.push(n.id);
      rowOf.set(n.id, componentRoots.length - 1);
    }
  }
  // BFS from each root assigning row + column. col is the depth from
  // the root; we sort by iteration_n within the row to keep the
  // chronological order intact.
  const colOf = new Map();
  for (const rootId of componentRoots) {
    colOf.set(rootId, 0);
  }
  // Propagate row/col by walking edges in input order. The edges list
  // is already id-ordered so chains advance one hop at a time.
  for (const e of edges) {
    if (rowOf.has(e.from_id) && !rowOf.has(e.to_id)) {
      rowOf.set(e.to_id, rowOf.get(e.from_id));
      colOf.set(e.to_id, (colOf.get(e.from_id) || 0) + 1);
    }
  }
  // Orphans we didn't catch (no parent + no chain root entry) drop to
  // a final row so they still render.
  for (const n of nodes) {
    if (!rowOf.has(n.id)) {
      rowOf.set(n.id, componentRoots.length);
      colOf.set(n.id, 0);
      componentRoots.push(n.id);
    }
  }

  // Compute SVG dimensions.
  let maxCol = 0;
  for (const c of colOf.values()) if (c > maxCol) maxCol = c;
  const rowCount = new Set(rowOf.values()).size;
  const width =
    _GRAPH_PAD * 2 + (maxCol + 1) * _GRAPH_NODE_W + maxCol * _GRAPH_NODE_GAP_X;
  const height =
    _GRAPH_PAD * 2 + rowCount * _GRAPH_NODE_H + (rowCount - 1) * _GRAPH_NODE_GAP_Y;

  function nodeXY(id) {
    const col = colOf.get(id) || 0;
    const row = rowOf.get(id) || 0;
    const x = _GRAPH_PAD + col * (_GRAPH_NODE_W + _GRAPH_NODE_GAP_X);
    const y = _GRAPH_PAD + row * (_GRAPH_NODE_H + _GRAPH_NODE_GAP_Y);
    return { x, y };
  }

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    style: "max-width: 100%; height: auto; "
         + "background: var(--bg); border: 1px solid var(--border); "
         + "border-radius: 6px;",
    width, height,
  });

  // Arrowhead marker — defined once, referenced by every edge.
  const defs = svgEl("defs");
  defs.appendChild(svgEl(
    "marker",
    {
      id: "iter-graph-arrow",
      viewBox: "0 0 10 10",
      refX: 8, refY: 5,
      markerWidth: 7, markerHeight: 7,
      orient: "auto-start-reverse",
    },
    svgEl("path", {
      d: "M 0 0 L 10 5 L 0 10 z",
      fill: "var(--accent)",
    }),
  ));
  svg.appendChild(defs);

  // Edges first so they render under the nodes.
  for (const e of edges) {
    const from = nodeXY(e.from_id);
    const to = nodeXY(e.to_id);
    const x1 = from.x + _GRAPH_NODE_W;
    const y1 = from.y + _GRAPH_NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y + _GRAPH_NODE_H / 2;
    // Cubic bezier so cross-row edges curve cleanly.
    const cx1 = x1 + (x2 - x1) * 0.5;
    const cx2 = x2 - (x2 - x1) * 0.5;
    svg.appendChild(svgEl("path", {
      d: `M ${x1} ${y1} C ${cx1} ${y1}, ${cx2} ${y2}, ${x2} ${y2}`,
      stroke: "var(--accent)",
      "stroke-width": 1.6,
      fill: "none",
      "marker-end": "url(#iter-graph-arrow)",
    }));
    // Edge label: "+N -M [Δ $±X]"
    const labelParts = [
      `+${e.applied_adds.length} -${e.applied_cuts.length}`,
    ];
    if (e.price_delta_usd != null) {
      const sign = e.price_delta_usd >= 0 ? "+" : "";
      labelParts.push(`Δ ${sign}$${Number(e.price_delta_usd).toFixed(0)}`);
    }
    if (e.bracket_delta) {
      labelParts.push(`B${e.bracket_delta > 0 ? "+" : ""}${e.bracket_delta}`);
    }
    const lx = (x1 + x2) / 2;
    const ly = (y1 + y2) / 2 - 6;
    const label = svgEl("text", {
      x: lx, y: ly,
      "text-anchor": "middle",
      "font-size": 11,
      fill: "var(--text-muted, var(--text))",
    }, labelParts.join(" · "));
    label.appendChild(svgEl("title", {}, e.rationale || ""));
    svg.appendChild(label);
  }

  // Nodes on top.
  for (const n of nodes) {
    const { x, y } = nodeXY(n.id);
    const g = svgEl("g", {
      class: "iteration-graph-node",
      style: "cursor: pointer;",
      "data-id": n.id,
    });
    // Rounded rect
    g.appendChild(svgEl("rect", {
      x, y, width: _GRAPH_NODE_W, height: _GRAPH_NODE_H,
      rx: 8, ry: 8,
      fill: _verdictBgColor(n.verdict),
      stroke: "var(--accent)",
      "stroke-width": 1.2,
    }));
    // Iteration label (top-left)
    g.appendChild(svgEl("text", {
      x: x + 10, y: y + 18,
      "font-size": 13, "font-weight": "600",
      fill: "var(--text)",
    }, `v${n.iteration_n} · B${n.bracket}`));
    // Verdict pill text (top-right)
    g.appendChild(svgEl("text", {
      x: x + _GRAPH_NODE_W - 10, y: y + 18,
      "text-anchor": "end",
      "font-size": 11,
      fill: "var(--text-muted, var(--text))",
    }, n.verdict));
    // Card count + price (bottom)
    const priceLabel = n.price_usd != null
      ? `$${Number(n.price_usd).toFixed(0)}`
      : "—";
    g.appendChild(svgEl("text", {
      x: x + 10, y: y + _GRAPH_NODE_H - 10,
      "font-size": 11,
      fill: "var(--text-muted, var(--text))",
    }, `${n.card_count} cards · ${priceLabel}`));
    // Audit version (above bottom-right)
    if (n.audit_version) {
      g.appendChild(svgEl("text", {
        x: x + _GRAPH_NODE_W - 10, y: y + _GRAPH_NODE_H - 10,
        "text-anchor": "end",
        "font-size": 10,
        fill: "var(--text-muted, var(--text))",
        "font-style": "italic",
      }, n.audit_version));
    }
    // Hover tooltip — show created_at + verdict (the parts not on
    // the rect face).
    const title = svgEl("title", {},
      `Iteration ${n.iteration_n}\n`
      + `Verdict: ${n.verdict}\n`
      + `Created: ${n.created_at || "?"}`,
    );
    g.appendChild(title);
    svg.appendChild(g);
  }

  // Wrap the SVG in a container so we can attach a verdict-control
  // panel beneath it. Manual web iterations land with verdict=pending
  // and the CLI's --run-sim path is the only auto-writer; before this
  // panel, those pending rows stayed pending forever (Tier-1.3).
  const wrap = el("div", { class: "iteration-graph-wrap" });
  wrap.appendChild(svg);
  const verdictPanel = _renderVerdictPanel(nodes);
  if (verdictPanel) {
    wrap.appendChild(verdictPanel);
  }
  return wrap;
}

function _renderVerdictPanel(nodes) {
  // Surface every pending iteration as a row of buttons so the user
  // can mark it kept / reverted / neutral after manual play. Hidden
  // when nothing is pending — non-pending rows are read-only here so
  // we don't accidentally walk over a CLI-sim verdict.
  const pending = (nodes || []).filter((n) => n.verdict === "pending");
  if (!pending.length) return null;
  const panel = el("div", { class: "iteration-verdict-panel" });
  panel.appendChild(el(
    "p", { class: "muted" },
    `Mark verdict for ${pending.length} pending iteration${pending.length === 1 ? "" : "s"}:`,
  ));
  // Sort by iteration_n so v2 lands before v3.
  pending.sort((a, b) => (a.iteration_n || 0) - (b.iteration_n || 0));
  for (const node of pending) {
    panel.appendChild(_renderVerdictRow(node));
  }
  return panel;
}

function _renderVerdictRow(node) {
  const row = el("div", {
    class: "iteration-verdict-row",
    "data-iteration-id": node.id,
    style: "display: flex; gap: 8px; align-items: center; margin: 6px 0;",
  });
  row.appendChild(el(
    "span", { style: "min-width: 90px; font-weight: 600;" },
    `v${node.iteration_n} · B${node.bracket}`,
  ));
  const status = el("span", { class: "verdict pending" }, "pending");
  const makeBtn = (label, verdict) => {
    const btn = el("button", {
      type: "button",
      class: "btn-sm",
      "data-verdict": verdict,
    }, label);
    btn.addEventListener("click", () => _patchVerdict(node.id, verdict, row, status));
    return btn;
  };
  row.appendChild(makeBtn("Kept", "kept"));
  row.appendChild(makeBtn("Reverted", "reverted"));
  row.appendChild(makeBtn("Neutral", "neutral"));
  row.appendChild(status);
  return row;
}

async function _patchVerdict(iterationId, verdict, row, status) {
  // Disable buttons during the PATCH so a fast double-click doesn't
  // race two updates. Status starts as "saving…" so the user sees
  // immediate feedback even on slow networks.
  const buttons = row.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  status.textContent = "saving…";
  status.className = "verdict pending";
  try {
    const resp = await fetch(`/api/iterations/${iterationId}/verdict`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verdict }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    status.textContent = verdict;
    status.className = `verdict ${verdict}`;
    // Re-enable only the non-active buttons so the user can change
    // their mind without re-rendering the whole graph.
    buttons.forEach((b) => {
      b.disabled = b.dataset.verdict === verdict;
    });
  } catch (err) {
    status.textContent = `error: ${err.message}`;
    status.className = "verdict pending";
    buttons.forEach((b) => (b.disabled = false));
  }
}

function _verdictBgColor(verdict) {
  // Subtle tint per verdict so the graph reads at a glance.
  switch (verdict) {
    case "kept":     return "rgba(74, 222, 128, 0.10)";   // green
    case "reverted": return "rgba(248, 113, 113, 0.10)";  // red
    case "neutral":  return "rgba(148, 163, 184, 0.10)";  // gray
    case "pending":
    default:         return "rgba(245, 158, 11, 0.08)";   // amber
  }
}

// Deck-health tile row — at-a-glance construction signals not directly
// surfaced by the advisor's narrative diagnosis:
//
//   MDFC count               — modal double-faced lands (Boseiju, etc.)
//   Spell density            — non-permanent / total ratio
//   Mana sinks               — X-cost spells for late-game flood
//   Wincon protection        — Silence / Veil / Grand Abolisher class
//   Self-mill enablement     — Stitcher's Supplier / Satyr Wayfinder
//
// Each tile shows a count + label and surfaces the contributing card
// names in a hover tooltip. Tiles with zero entries render dimmed so
// the visual weight tracks signal strength.
function renderDeckHealthTiles(health) {
  const row = el("div", {
    class: "deck-health-row",
    style: "display: grid; "
         + "grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); "
         + "gap: 8px; margin: 10px 0; "
         + "padding: 8px; "
         + "background: var(--bg); "
         + "border: 1px solid var(--border); "
         + "border-radius: 6px;",
  });

  // MDFC tile.
  const mdfc = health.mdfc || { count: 0, cards: [] };
  row.appendChild(renderHealthTile({
    label: "MDFCs",
    value: mdfc.count,
    tooltip: mdfc.cards.length
      ? `Modal double-faced lands:\n${mdfc.cards.join("\n")}\n\n`
        + `A deck with 6+ MDFCs effectively plays more lands and more spells `
        + `than the printed count suggests.`
      : "No modal double-faced lands. Cards like Boseiju, Who Endures or "
        + "Bala Ged Recovery let you play one card as either a spell or a land "
        + "depending on what the game state needs.",
    flavor: mdfc.count >= 6 ? "good" : (mdfc.count >= 3 ? "neutral" : "muted"),
  }));

  // Spell density tile.
  const sd = health.spell_density || {
    non_permanent_count: 0, total_main_count: 0, ratio: null,
  };
  const sdLabel = sd.ratio == null
    ? "—"
    : `${Math.round(sd.ratio * 100)}%`;
  row.appendChild(renderHealthTile({
    label: "Spells (non-perm)",
    value: sdLabel,
    sub: sd.ratio != null
      ? `${sd.non_permanent_count}/${sd.total_main_count}`
      : "",
    tooltip: sd.ratio != null
      ? `${sd.non_permanent_count} instants/sorceries out of ${sd.total_main_count}`
        + ` mainboard cards (${Math.round(sd.ratio * 100)}%).\n\n`
        + `Spellslinger archetypes (Storm, Magecraft, Prowess) need 20-30%+ `
        + `non-permanents to keep their payoffs live.`
      : "Spell-density signal unavailable (Scryfall lookup failed).",
    flavor: (sd.ratio != null && sd.ratio >= 0.20) ? "good"
          : (sd.ratio != null && sd.ratio >= 0.10) ? "neutral"
          : "muted",
  }));

  // Mana sinks tile.
  const ms = health.mana_sinks || { count: 0, cards: [] };
  row.appendChild(renderHealthTile({
    label: "Mana sinks",
    value: ms.count,
    tooltip: ms.cards.length
      ? `X-cost spells (mana sinks):\n${ms.cards.join("\n")}\n\n`
        + `Mana sinks scale to whatever excess mana you have — they prevent `
        + `flooding out in long games. B4 decks typically run 3-5 of these.`
      : "No X-cost spells detected. A deck with no mana sinks can flood out "
        + "in long games when you draw lands you don't need.",
    flavor: ms.count >= 3 ? "good" : (ms.count >= 1 ? "neutral" : "warn"),
  }));

  // Wincon-specific protection tile.
  const wp = health.wincon_protection || { count: 0, cards: [] };
  row.appendChild(renderHealthTile({
    label: "Wincon protection",
    value: wp.count,
    tooltip: wp.cards.length
      ? `Wincon-specific protection:\n${wp.cards.join("\n")}\n\n`
        + `Cards like Silence, Veil of Summer, Grand Abolisher, Defense Grid, `
        + `Pact of Negation, Allosaurus Shepherd — protect a combo turn from `
        + `interaction. Distinct from generic hexproof / ward.`
      : "No wincon-specific protection detected. Combo decks at B3+ usually "
        + "need 2-4 Silence-class cards to land their wincon through opposing "
        + "instants/counterspells.",
    flavor: wp.count >= 3 ? "good" : (wp.count >= 1 ? "neutral" : "warn"),
  }));

  // Self-mill enablement tile.
  const sm = health.self_mill || { count: 0, cards: [] };
  row.appendChild(renderHealthTile({
    label: "Self-mill",
    value: sm.count,
    tooltip: sm.cards.length
      ? `Self-mill enablers:\n${sm.cards.join("\n")}\n\n`
        + `Cards that put your library into your graveyard. The fuel side of `
        + `graveyard strategies — distinct from reanimation/payoff cards.`
      : "No self-mill enablers detected. If the deck has graveyard payoffs "
        + "(reanimation, dredge), it needs Stitcher's Supplier / Satyr "
        + "Wayfinder / Mesmeric Orb-class cards to feed them.",
    // Self-mill is only relevant for graveyard decks, so "0" isn't
    // automatically bad. Default to "muted" not "warn".
    flavor: sm.count >= 4 ? "good" : (sm.count >= 1 ? "neutral" : "muted"),
  }));

  return row;
}

function renderHealthTile(opts) {
  // Color-mapping per flavor. Subtle backgrounds so the row reads as
  // a cohesive band rather than a stoplight.
  const flavorBg = {
    good:    "rgba(74, 222, 128, 0.08)",
    neutral: "rgba(96, 165, 250, 0.08)",
    warn:    "rgba(245, 158, 11, 0.10)",
    muted:   "rgba(148, 163, 184, 0.06)",
  };
  const flavorBorder = {
    good:    "rgba(74, 222, 128, 0.4)",
    neutral: "rgba(96, 165, 250, 0.4)",
    warn:    "rgba(245, 158, 11, 0.4)",
    muted:   "rgba(148, 163, 184, 0.3)",
  };
  const bg = flavorBg[opts.flavor] || flavorBg.muted;
  const border = flavorBorder[opts.flavor] || flavorBorder.muted;
  const tile = el("div", {
    class: `health-tile flavor-${opts.flavor || "muted"}`,
    style: `background: ${bg}; border: 1px solid ${border}; `
         + `border-radius: 6px; padding: 8px 10px; `
         + `cursor: help;`,
    title: opts.tooltip || "",
  });
  tile.appendChild(el(
    "div",
    { class: "muted", style: "font-size: 11px; margin-bottom: 2px;" },
    opts.label,
  ));
  tile.appendChild(el(
    "div",
    { style: "font-size: 18px; font-weight: 600;" },
    String(opts.value),
  ));
  if (opts.sub) {
    tile.appendChild(el(
      "div",
      { class: "muted", style: "font-size: 11px;" },
      opts.sub,
    ));
  }
  return tile;
}

// Render the salt-warning banner that fires above the recommendations
// when the user's current deck carries salty picks at a low bracket.
// Yellow/orange treatment (warn, not bad) — the user CAN keep the
// cards; the banner is advisory, not blocking. Cards listed inline so
// the user doesn't have to scroll to see what's being flagged.
function renderSaltWarningBanner(warning) {
  const wrap = el("div", {
    class: "salt-warning-banner",
    style: "margin: 10px 0; padding: 10px 14px; "
         + "background: rgba(245, 158, 11, 0.12); "
         + "border-left: 4px solid #f59e0b; "
         + "border-radius: 6px; "
         + "color: var(--text);",
  });
  const headline = el(
    "div",
    { style: "font-weight: 600; margin-bottom: 4px;" },
    `Salt warning: ${warning.count} `
    + (warning.count === 1 ? "card scores" : "cards score")
    + ` ≥ ${warning.threshold} on EDHREC's salt list — `
    + `consider cutting at B${warning.bracket}.`,
  );
  wrap.appendChild(headline);
  const list = el("ul", {
    style: "list-style: none; padding: 0; margin: 4px 0 0 0; "
         + "display: flex; flex-wrap: wrap; gap: 6px 12px; "
         + "font-size: 13px;",
  });
  for (const c of warning.cards) {
    const li = el("li", {});
    li.appendChild(el(
      "span",
      { style: "font-weight: 500;" },
      c.name,
    ));
    li.appendChild(document.createTextNode(" "));
    li.appendChild(el(
      "span",
      {
        class: "pill",
        style: "background: #f59e0b; color: #1a1a1a; "
             + "padding: 1px 6px; border-radius: 4px; "
             + "font-size: 11px; font-weight: 600;",
        title: `EDHREC salt score (0..5). Higher = more socially salty.`,
      },
      `${c.salt.toFixed(1)}`,
    ));
    list.appendChild(li);
  }
  wrap.appendChild(list);
  return wrap;
}

// Render the EDHREC average-deck preview as a collapsible <details>
// section. The list is grouped by EDHREC category (Creatures / Lands /
// Ramp / ...); cards present in the user's current deck are marked
// with a green check, missing cards with a "+". Click "+" to pre-fill
// the manual-add input (UI hook — the input element looks up by id
// 'audit-manual-add' if present).
function renderAverageDeckPreview(preview) {
  const bracketLabel = preview.bracket_slug
    ? ` (B${bracketSlugToInt(preview.bracket_slug) ?? "?"})`
    : "";
  const summary = `EDHREC average deck${bracketLabel} — ${preview.card_count} cards`;

  const details = el("details", {
    class: "avg-deck-preview",
    style: "margin-top: 14px; border: 1px solid var(--border); "
         + "border-radius: 6px; padding: 8px 12px; background: var(--bg);",
  });
  details.appendChild(el(
    "summary",
    { class: "muted", style: "cursor: pointer; font-weight: 600;" },
    summary,
  ));

  // Build the body lazily on first open — for a 100-card preview the
  // DOM cost isn't huge, but the helper keeps the initial audit
  // render snappy when many <details> coexist on the page.
  let built = false;
  details.addEventListener("toggle", () => {
    if (!details.open || built) return;
    built = true;
    details.appendChild(buildAverageDeckBody(preview));
  });

  return details;
}

function bracketSlugToInt(slug) {
  // EDHREC's slug names → bracket integers. Stable enough to hard-code
  // because the bracket schema is fixed at 5 tiers by WotC.
  const map = {
    "exhibition": 1, "core": 2, "upgraded": 3, "optimized": 4, "cedh": 5,
  };
  return map[(slug || "").toLowerCase()] ?? null;
}

function buildAverageDeckBody(preview) {
  // Group cards by category. Cards missing a category land in 'Other'
  // so the list stays exhaustive without inventing a label per card.
  const groups = new Map();
  for (const card of preview.cards) {
    const cat = card.category || "Other";
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(card);
  }

  // Sort categories by typical Commander build order: lands, ramp,
  // draw, removal, finishers, then everything else alphabetically.
  // The first-class headers match EDHREC's commander-page section
  // labels; anything not in the priority list falls through
  // alphabetically.
  const priority = [
    "Lands", "Ramp", "Mana Artifacts",
    "Draw", "Card Advantage",
    "Removal", "Interaction",
    "Creatures", "Finishers",
    "Other",
  ];
  const orderedCats = Array.from(groups.keys()).sort((a, b) => {
    const ai = priority.indexOf(a);
    const bi = priority.indexOf(b);
    if (ai === -1 && bi === -1) return a.localeCompare(b);
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });

  const wrap = el("div", { style: "margin-top: 10px;" });
  for (const cat of orderedCats) {
    const cards = groups.get(cat);
    wrap.appendChild(el(
      "h5",
      { style: "margin: 10px 0 4px 0; color: var(--accent); font-size: 13px;" },
      `${cat} (${cards.length})`,
    ));
    const ul = el("ul", {
      style: "list-style: none; padding: 0; margin: 0; "
           + "display: grid; grid-template-columns: repeat(auto-fill, "
           + "minmax(220px, 1fr)); gap: 4px;",
    });
    for (const card of cards) {
      ul.appendChild(renderAverageDeckCard(card));
    }
    wrap.appendChild(ul);
  }
  return wrap;
}

function renderAverageDeckCard(card) {
  const li = el("li", {
    style: "display: flex; align-items: center; gap: 6px; "
         + "padding: 2px 4px; font-size: 12px;",
  });
  const marker = card.in_user_deck
    ? el(
        "span",
        {
          style: "color: var(--good, #4ade80); font-weight: bold; width: 14px;",
          title: "Already in your deck",
        },
        "✓",
      )
    : el(
        "span",
        {
          class: "add-from-avg-deck",
          style: "color: var(--accent); cursor: pointer; "
               + "width: 14px; font-weight: bold;",
          title: "Add this card (click to copy name)",
          "data-card-name": card.name,
        },
        "+",
      );
  if (!card.in_user_deck) {
    marker.addEventListener("click", () => {
      // Best-effort: copy the name to clipboard so the user can paste
      // into whatever add-card flow they use. Falls back silently if
      // clipboard API is unavailable.
      if (navigator.clipboard) {
        navigator.clipboard.writeText(card.name).catch(() => {});
      }
    });
  }
  li.appendChild(marker);
  const name = el(
    "span",
    { style: card.in_user_deck ? "" : "color: var(--text-muted, inherit);" },
    card.name,
  );
  li.appendChild(name);
  const pct = el(
    "span",
    { class: "muted", style: "margin-left: auto; font-size: 11px;" },
    `${Math.round(card.inclusion_pct)}%`,
  );
  li.appendChild(pct);
  return li;
}

function renderDashboard(data, iterations) {
  const dash = $("dashboard");
  dash.innerHTML = "";

  // Snapshot pricing for save_iteration. Captured here (not at fetch
  // time) so the value mirrors what the user is actually looking at —
  // soft-refresh re-renders this function with fresh data.
  _lastDashboardPriceUsd =
    (data.stat_tiles && typeof data.stat_tiles.est_price_usd === "number")
      ? data.stat_tiles.est_price_usd
      : null;

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
  // Legality banner (always visible above action row).
  if (data.legality) {
    hero.appendChild(legalityBanner(data.legality, data));
  }

  // Action row — Propose / Run audit / Edit / Copy to Moxfield / Delete.
  const actions = el("div", { class: "hero-actions" });

  const proposeBtn = el("button", { class: "primary" }, "Propose changes");
  // Wrap so the PointerEvent isn't passed as ``opts`` -- the truthy
  // check ``(opts && opts.saveOnly)`` makes this work by accident
  // today (event has no saveOnly), but the binding's INTENT is
  // "open in A/B mode" so we make that explicit.
  proposeBtn.addEventListener("click", () => openProposeModal());
  actions.appendChild(proposeBtn);

  const auditBtn = el("button", {}, "Run audit");
  auditBtn.title = "Generate swap suggestions via heuristic + EDHREC";
  // Wrap in an arrow so the PointerEvent isn't passed as a positional
  // arg to loadAdvise (where it would be misread as sourceOverride
  // and serialize as "[object PointerEvent]" in the URL).
  auditBtn.addEventListener("click", () => loadAdvise());
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
  // Salt-cards pill: surfaces EDHREC-ranked "high salt" picks the
  // deck is running. Complements the Game Changers count — GCs are
  // power signals; salt is table-talk signal. Click to inspect.
  const saltCount = data.legality?.salt_cards_count ?? 0;
  if (saltCount > 0) {
    const saltRow = el("div", { class: "salt-pill-row" });
    const pill = el("button", {
      class: "pill warn",
      style: "cursor: pointer; border: none;",
    }, `Salt cards: ${saltCount}`);
    pill.title = (data.legality.salt_cards || [])
      .map((c) => `${c.name} (${c.score.toFixed(2)})`).join("\n");
    pill.addEventListener("click", () => showSaltCardsAlert(data.legality.salt_cards || []));
    saltRow.appendChild(pill);
    catGrid.appendChild(saltRow);
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
    // Wrap so the PointerEvent isn't passed as sourceOverride -- same
    // bug shape as the "Run audit" button above.
    btn.addEventListener("click", () => loadAdvise());
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
    const histPanel = panel("Iteration history", ul);
    // "View graph" button — toggles a sibling <details> with the
    // SVG iteration chain. Only useful when ≥2 iterations exist
    // (one node alone is a dot, not a graph).
    if (iterations.length >= 2) {
      const graphWrap = el("div", { class: "iteration-graph-wrap" });
      const toggleBtn = el(
        "button",
        {
          class: "advise-btn",
          style: "margin-top: 8px; font-size: 12px; padding: 4px 10px;",
        },
        "View iteration graph",
      );
      let loaded = false;
      const graphContainer = el("div", {
        style: "margin-top: 10px;",
        hidden: "true",
      });
      toggleBtn.addEventListener("click", async () => {
        if (graphContainer.hidden && !loaded) {
          loaded = true;
          toggleBtn.disabled = true;
          toggleBtn.textContent = "Loading…";
          try {
            const data = await fetchJSON(
              `/api/iteration_graph?deck=${encodeURIComponent(_activeDeckId)}`,
            );
            graphContainer.appendChild(renderIterationGraph(data));
          } catch (e) {
            graphContainer.appendChild(el(
              "p", { class: "muted" },
              `Graph fetch failed: ${e.message}`,
            ));
          }
          toggleBtn.disabled = false;
        }
        graphContainer.hidden = !graphContainer.hidden;
        toggleBtn.textContent = graphContainer.hidden
          ? "View iteration graph"
          : "Hide iteration graph";
      });
      graphWrap.appendChild(toggleBtn);
      graphWrap.appendChild(graphContainer);
      histPanel.appendChild(graphWrap);
    }
    dash.appendChild(histPanel);
  }

  // Per-audit-version verdict breakdown. Only show when the deck has
  // ≥5 iterations — below that, sample sizes are too small to draw
  // any per-version conclusion. Fires after the rest of the dashboard
  // renders so a slow knowledge_log query never blocks the first paint.
  if (iterations.length >= 5) {
    loadVerdictBreakdown(_activeDeckId, dash);
  }

  // Cost-evolution sparkline. Renders only when ≥2 iterations have
  // captured pricing snapshots (one point is a single dot, not a
  // line). Best-effort background fetch — never blocks the dashboard.
  if (iterations.length >= 2) {
    loadPricingSparkline(_activeDeckId, dash);
  }
}


async function loadPricingSparkline(deckId, dashContainer) {
  if (!deckId) return;
  try {
    const body = await fetchJSON(
      `/api/pricing_series?deck=${encodeURIComponent(deckId)}`,
    );
    const points = body.points || [];
    if (points.length < 2) return;  // can't draw a line from one point

    const W = 280;
    const H = 60;
    const PAD = 4;
    const prices = points.map((p) => p.total_price_usd);
    const minP = Math.min(...prices);
    const maxP = Math.max(...prices);
    const range = Math.max(1e-6, maxP - minP);

    // Map each point to (x, y) in the SVG viewBox.
    const coords = prices.map((p, i) => {
      const x = PAD + (i / (prices.length - 1)) * (W - PAD * 2);
      const y = H - PAD - ((p - minP) / range) * (H - PAD * 2);
      return [x, y];
    });
    const pathD = coords
      .map(([x, y], i) => `${i === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`)
      .join(" ");

    // Build the SVG. Using innerHTML for the path keeps the code
    // compact — `el()` doesn't have ergonomic SVG support.
    const svg = document.createElementNS(
      "http://www.w3.org/2000/svg", "svg",
    );
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", String(W));
    svg.setAttribute("height", String(H));
    svg.setAttribute("style", "display: block;");
    // Line
    const path = document.createElementNS(
      "http://www.w3.org/2000/svg", "path",
    );
    path.setAttribute("d", pathD);
    path.setAttribute("stroke", "var(--accent, #88c0ff)");
    path.setAttribute("stroke-width", "1.5");
    path.setAttribute("fill", "none");
    svg.appendChild(path);
    // Dots at each point with tooltip showing price + captured_at.
    for (let i = 0; i < coords.length; i++) {
      const [cx, cy] = coords[i];
      const c = document.createElementNS(
        "http://www.w3.org/2000/svg", "circle",
      );
      c.setAttribute("cx", cx.toFixed(1));
      c.setAttribute("cy", cy.toFixed(1));
      c.setAttribute("r", "2.5");
      c.setAttribute("fill", "var(--accent, #88c0ff)");
      const title = document.createElementNS(
        "http://www.w3.org/2000/svg", "title",
      );
      title.textContent =
        `$${prices[i].toFixed(2)}`
        + (points[i].captured_at
            ? ` @ ${String(points[i].captured_at).slice(0, 10)}`
            : "");
      c.appendChild(title);
      svg.appendChild(c);
    }

    const first = prices[0];
    const last = prices[prices.length - 1];
    const delta = last - first;
    const deltaPct = first > 0 ? (delta / first) * 100 : 0;
    const trend = delta >= 0 ? "▲" : "▼";
    const label = el(
      "p", { class: "muted", style: "font-size: 12px; margin: 4px 0;" },
      `Cost: $${first.toFixed(2)} → $${last.toFixed(2)} `
      + `(${trend} $${Math.abs(delta).toFixed(2)}, `
      + `${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(1)}% over `
      + `${points.length} iteration${points.length === 1 ? "" : "s"})`,
    );

    const container = el("div", {});
    container.appendChild(label);
    container.appendChild(svg);
    dashContainer.appendChild(panel("Cost over time", container));
  } catch (_e) {
    // Best-effort — never block the dashboard on the pricing query.
  }
}


async function loadVerdictBreakdown(deckId, dashContainer) {
  if (!deckId) return;
  try {
    const body = await fetchJSON(
      `/api/verdict_breakdown?deck=${encodeURIComponent(deckId)}`,
    );
    const breakdown = body.breakdown || {};
    const versions = Object.keys(breakdown);
    if (versions.length === 0) return;
    const ul = el("ul", { class: "iteration-list" });
    // Sort by total descending so the most-sampled version reads first.
    versions.sort((a, b) =>
      (breakdown[b].total || 0) - (breakdown[a].total || 0));
    for (const v of versions) {
      const b = breakdown[v];
      const total = b.total || 0;
      const kept = b.kept || 0;
      const reverted = b.reverted || 0;
      const neutral = b.neutral || 0;
      const row = el("li", { class: "iteration" });
      row.appendChild(el("span", { class: "name" }, v));
      // Color the verdict count pills like the iteration list.
      const pills = el("div", { style: "display: flex; gap: 4px;" });
      if (kept) pills.appendChild(el(
        "span", { class: "verdict kept" }, `${kept} kept`,
      ));
      if (reverted) pills.appendChild(el(
        "span", { class: "verdict reverted" }, `${reverted} reverted`,
      ));
      if (neutral) pills.appendChild(el(
        "span", { class: "verdict neutral" }, `${neutral} neutral`,
      ));
      row.appendChild(pills);
      const keptPct = total ? Math.round((kept / total) * 100) : 0;
      row.appendChild(el(
        "span", { class: "delta" },
        `${kept}/${total} kept (${keptPct}%)`,
      ));
      ul.appendChild(row);
    }
    dashContainer.appendChild(panel("Verdict by audit version", ul));
  } catch (_e) {
    // Best-effort — never block the dashboard on the breakdown query.
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

function legalityBanner(legality, data) {
  const wrap = el("div", { class: "legality-banner" });
  // Legality pill.
  if (legality.all_legal) {
    wrap.appendChild(el("span", { class: "pill good" },
      "✓ All cards legal in Commander"));
  } else {
    wrap.appendChild(el("span", { class: "pill bad" },
      `✗ ${legality.n_illegal} illegal card${legality.n_illegal === 1 ? "" : "s"}`));
  }
  // Deck-size pill — only show when the deck isn't 100 cards.
  // Universal staples-aware audit produces sub-100 proposed decks
  // when source is short; the warning prevents the user from
  // chasing 0-games Forge runs.
  if (legality.deck_size_ok === false) {
    const total = legality.deck_total ?? "?";
    const target = legality.deck_target ?? 100;
    wrap.appendChild(el(
      "span", { class: "pill bad" },
      `Deck is ${total}/${target} — needs ${target - total} more`,
    ));
  }
  // Game Changers pill.
  const gcCount = legality.n_game_changers || 0;
  if (gcCount > 0) {
    const pill = el("span", { class: "pill warn" },
      `${gcCount} Game Changer${gcCount === 1 ? "" : "s"}`);
    pill.title = (legality.in_deck_game_changers || []).join("\n");
    wrap.appendChild(pill);
  }
  // Source / Moxfield link.
  if (data.moxfield_url) {
    const link = el("a", {
      href: data.moxfield_url, target: "_blank",
      rel: "noopener noreferrer",
      class: "pill",
      style: "background: var(--panel-2); color: var(--accent); text-decoration: none;",
    }, "↗ View on Moxfield");
    wrap.appendChild(link);

    // Verify-against-source button — diffs local vs live Moxfield.
    const verifyBtn = el("button", {
      class: "pill",
      style: "background: var(--panel-2); color: var(--text); border: 1px solid var(--border); cursor: pointer;",
    }, "Verify vs Moxfield");
    verifyBtn.addEventListener("click", verifyAgainstSource);
    wrap.appendChild(verifyBtn);
  } else {
    // No source attached — offer to attach one.
    const attachBtn = el("button", {
      class: "pill",
      style: "background: var(--panel-2); color: var(--muted); border: 1px solid var(--border); cursor: pointer;",
    }, "Attach Moxfield URL");
    attachBtn.addEventListener("click", attachMoxfieldUrl);
    wrap.appendChild(attachBtn);
  }
  return wrap;
}

async function attachMoxfieldUrl() {
  if (!_activeDeckId) return;
  const url = window.prompt(
    "Paste the Moxfield URL for this deck:\n" +
    "(e.g. https://moxfield.com/decks/abc123)",
    "",
  );
  if (!url) return;
  try {
    const resp = await fetch(
      `/api/deck_source?deck=${encodeURIComponent(_activeDeckId)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ moxfield_url: url }),
      },
    );
    const body = await resp.json();
    if (!resp.ok) {
      flashStatus(`Couldn't attach: ${body.error || resp.status}`);
      return;
    }
    flashStatus(`Linked to ${body.moxfield_url}`);
    // Soft-refresh so the banner reflects the new link without
    // blanking the rest of the dashboard.
    const li = document.querySelector(`.deck-list li[data-id="${_activeDeckId}"]`);
    selectDeck(_activeDeckId, li, { soft: true });
  } catch (e) {
    flashStatus(`Network error: ${e.message}`);
  }
}

async function verifyAgainstSource() {
  if (!_activeDeckId) return;
  $("alert-title").textContent = "Verify vs Moxfield source";
  const body = $("alert-body");
  body.className = "alert-body";
  body.innerHTML = '<p class="muted">Fetching live Moxfield deck and diffing…</p>';
  $("alert-modal").hidden = false;
  try {
    const resp = await fetchJSON(
      `/api/verify_against_source?deck=${encodeURIComponent(_activeDeckId)}`,
    );
    body.innerHTML = "";
    body.appendChild(el(
      "p", {},
      el("a", {
        href: resp.source_url, target: "_blank",
        rel: "noopener noreferrer",
        style: "color: var(--accent);",
      }, resp.source_url),
    ));
    if (resp.in_local_only.length === 0 && resp.in_remote_only.length === 0) {
      body.appendChild(el(
        "p", {}, el("span", { class: "pill good" },
          `In sync — ${resp.matched} cards match`),
      ));
      return;
    }
    body.appendChild(el(
      "p", {},
      el("span", { class: "pill warn" },
        `Drift detected — ${resp.matched} matched, ` +
        `${resp.in_local_only.length} only-local, ` +
        `${resp.in_remote_only.length} only-remote`),
    ));
    if (resp.in_local_only.length) {
      body.appendChild(el("h4", {}, "In your local copy but not on Moxfield"));
      const ul = el("ul");
      for (const c of resp.in_local_only) ul.appendChild(el("li", {}, c));
      body.appendChild(ul);
    }
    if (resp.in_remote_only.length) {
      body.appendChild(el("h4", {}, "On Moxfield but not in your local copy"));
      const ul = el("ul");
      for (const c of resp.in_remote_only) ul.appendChild(el("li", {}, c));
      body.appendChild(ul);
    }
  } catch (e) {
    body.innerHTML = `<p class="muted">Verify failed: ${e.message}</p>`;
  }
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
  // Bracket auto-inference divergence warning. When the heuristic
  // disagrees with the user's declared bracket, surface that so they
  // can update the filename or accept the mismatch knowingly. Only
  // warn when inferred is HIGHER (deck is more powerful than declared)
  // — under-declaring power level is the foot-gun; over-declaring is
  // not.
  if (
    t.inferred_bracket != null
    && t.bracket != null
    && t.inferred_bracket > t.bracket
  ) {
    t_node.appendChild(el(
      "div",
      { class: "sub", style: "color: var(--warn, #d4a017);" },
      `Heuristic suggests B${t.inferred_bracket} (this deck looks `
      + `more powerful than declared)`,
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
  let text;
  try {
    const body = await fetchJSON(
      `/api/moxfield_format?deck=${encodeURIComponent(_activeDeckId)}`,
    );
    text = body.text || "";
  } catch (e) {
    flashStatus(`Couldn't fetch deck text: ${e.message}`);
    return;
  }

  if (!text) {
    flashStatus("Deck has no card lines to copy.");
    return;
  }

  // Try the modern Clipboard API. In some browsers it rejects when
  // the document isn't focused or the page isn't a secure context.
  // Fall back to a hidden textarea + execCommand('copy') which works
  // on http://127.0.0.1 even when the Clipboard API doesn't.
  const ok = await tryClipboardWrite(text);
  if (ok) {
    flashStatus("Copied to clipboard — paste into Moxfield's bulk-edit.");
    return;
  }

  // Last resort: open a fallback dialog with the text selected so the
  // user can Ctrl+C manually.
  showFallbackCopyDialog(text);
}

async function tryClipboardWrite(text) {
  // Path 1: navigator.clipboard.writeText (requires secure context +
  // window focus).
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      // Fall through to legacy path.
    }
  }
  // Path 2: legacy execCommand. Build a temp textarea, select, copy.
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Off-screen but selectable.
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.left = "-1000px";
    ta.style.opacity = "0";
    ta.setAttribute("readonly", "");
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand && document.execCommand("copy");
    document.body.removeChild(ta);
    return !!ok;
  } catch (_) {
    return false;
  }
}

function showFallbackCopyDialog(text) {
  // Reuse the alert modal as a generic "select-this-text" dialog.
  $("alert-title").textContent = "Copy this text manually";
  const body = $("alert-body");
  body.className = "alert-body";
  body.innerHTML = "";
  body.appendChild(el(
    "p", { class: "muted" },
    "Browser blocked the clipboard write. Select the box below " +
    "(Ctrl+A) and copy with Ctrl+C, then paste into Moxfield's " +
    "bulk-edit page.",
  ));
  const ta = el("textarea", {
    spellcheck: "false",
    style: "width: 100%; min-height: 240px; font-family: ui-monospace, "
         + "SFMono-Regular, Consolas, monospace; font-size: 12px; "
         + "background: var(--bg); color: var(--text); border: "
         + "1px solid var(--border); border-radius: 6px; padding: 10px;",
  });
  ta.value = text;
  body.appendChild(ta);
  $("alert-modal").hidden = false;
  // Pre-select the textarea content so a single Ctrl+C copies it.
  setTimeout(() => { ta.focus(); ta.select(); }, 50);
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

function showSaltCardsAlert(saltCards) {
  const modal = $("alert-modal");
  $("alert-title").textContent = "Salt cards";
  const body = $("alert-body");
  body.className = "alert-body";
  body.innerHTML = "";
  body.appendChild(el(
    "p", { class: "muted" },
    "EDHREC ranks these as the most-disliked cards across Commander " +
    "tables. Higher scores = more table-talk friction. B1-B3 decks " +
    "should keep these to a minimum.",
  ));
  if (!saltCards || !saltCards.length) {
    body.appendChild(el(
      "p", {}, el("span", { class: "pill good" }, "None in this deck"),
    ));
  } else {
    body.appendChild(el(
      "p", {}, el("span", { class: "pill warn" },
                  `${saltCards.length} in this deck`),
    ));
    const ul = el("ul");
    for (const c of saltCards) {
      ul.appendChild(el(
        "li", {},
        `${c.name} `,
        el("span", { class: "muted" }, `(score ${c.score.toFixed(2)})`),
      ));
    }
    body.appendChild(ul);
  }
  modal.hidden = false;
}

function openNewDeckModal() {
  $("new-deck-status").textContent = "";
  $("new-mox-name").value = "";
  $("new-mox-url").value = "";
  $("new-paste-name").value = "";
  $("new-paste-text").value = "";
  const bulkUrls = $("new-bulk-urls");
  if (bulkUrls) bulkUrls.value = "";
  const bulkResult = $("new-bulk-result");
  if (bulkResult) bulkResult.innerHTML = "";
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

async function bulkImportFromTextarea() {
  const textarea = $("new-bulk-urls");
  const result = $("new-bulk-result");
  const status = $("new-deck-status");
  const raw = (textarea.value || "").split(/\r?\n/);
  const urls = raw.map((s) => s.trim()).filter((s) => s.length > 0);

  result.innerHTML = "";
  if (urls.length === 0) {
    status.textContent = "Paste at least one Moxfield URL.";
    return;
  }
  if (urls.length > 50) {
    status.textContent =
      `Too many URLs (${urls.length}). Max 50 per batch — split it up.`;
    return;
  }

  status.textContent =
    `Importing ${urls.length} deck${urls.length === 1 ? "" : "s"}…`;
  // Disable the button while the batch runs so the user doesn't fire
  // a second concurrent batch by accident — the backend would dedupe
  // but the UX gets confused.
  const btn = $("new-bulk-import");
  btn.disabled = true;
  try {
    const resp = await fetch("/api/bulk_import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, is_user: true }),
    });
    const body = await resp.json();
    status.textContent =
      `Done: ${body.success_count} imported, `
      + `${body.duplicate_count} duplicates, `
      + `${body.failure_count} failed.`;
    renderBulkResult(result, body);
    if (body.success_count > 0) {
      // Refresh the deck list so newly-imported decks appear without a
      // page reload. The modal stays open so the user can review the
      // per-URL outcomes.
      await loadDecks();
    }
  } catch (e) {
    status.textContent = `Network error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

function renderBulkResult(container, body) {
  container.innerHTML = "";
  const summary = el(
    "p",
    { class: "muted", style: "margin-top: 8px;" },
    `${body.success_count}/${body.total} succeeded`,
  );
  container.appendChild(summary);

  function listBlock(label, entries, cls) {
    if (!entries.length) return;
    container.appendChild(el(
      "h5",
      { style: "margin: 8px 0 4px 0; font-size: 13px;" },
      `${label} (${entries.length})`,
    ));
    const ul = el("ul", {
      style: "list-style: none; padding: 0; margin: 0; font-size: 12px;",
    });
    for (const e of entries) {
      const li = el("li", { class: cls, style: "padding: 1px 0;" });
      if (e.path) {
        // Success row: deck filename
        const fname = e.path.split(/[\\/]/).pop();
        li.appendChild(el("span", {}, `✓ ${fname}`));
      } else if (e.existing_path || e.reason) {
        // Duplicate row: deck_id + reason
        li.appendChild(el(
          "span",
          { class: "muted" },
          `↺ ${e.deck_id || e.url} (${e.reason || "duplicate"})`,
        ));
      } else if (e.error) {
        // Failure row: URL + error
        li.appendChild(el(
          "span",
          { class: "pill bad", style: "display: inline-block;" },
          `✗ ${e.url}`,
        ));
        li.appendChild(el(
          "div",
          { class: "muted", style: "margin-left: 14px;" },
          e.error,
        ));
      }
      ul.appendChild(li);
    }
    container.appendChild(ul);
  }

  listBlock("Imported", body.successes, "");
  listBlock("Duplicates", body.duplicates, "muted");
  listBlock("Failed", body.failures, "");
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

  // Auto-audit-on-load toggle (Tier-2 #2.2). Persists to
  // localStorage; default on. When off, selecting a deck loads the
  // dashboard without firing the background heuristic audit.
  const autoAuditToggle = $("auto-audit-toggle");
  if (autoAuditToggle) {
    autoAuditToggle.checked = getAutoAuditPref();
    autoAuditToggle.addEventListener("change", () => {
      setAutoAuditPref(autoAuditToggle.checked);
    });
  }

  // New-deck modal: tab switching + import buttons.
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  });
  const moxImport = $("new-mox-import");
  if (moxImport) moxImport.addEventListener("click", importMoxfield);
  const pasteCreate = $("new-paste-create");
  if (pasteCreate) pasteCreate.addEventListener("click", createPasteDeck);
  const bulkImport = $("new-bulk-import");
  if (bulkImport) bulkImport.addEventListener("click", bulkImportFromTextarea);
});

loadHealth();
loadDecks();

// Refresh the topbar health badge every 60s so the Forge-version
// staleness indicator updates when the user installs a new Forge
// jar without reloading the page. Also picks up forge_py
// correlation-log growth + deck-count changes for free since
// loadHealth re-fetches all three endpoints. Tier-2 issue #2.4
// from the 2026-05-13 ranked list.
//
// 60s is a deliberate trade-off: short enough that an operator who
// drops a new Forge jar sees the badge update within a minute,
// long enough that the polling cost is negligible (3 cheap JSON
// endpoints, all served from cache after warm-up).
const _HEALTH_REFRESH_MS = 60_000;
setInterval(() => { loadHealth(); }, _HEALTH_REFRESH_MS);
