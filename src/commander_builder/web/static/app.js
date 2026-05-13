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

async function selectDeck(deckId, li, opts) {
  // ``opts.soft`` (default false) skips the blanking step: keeps the
  // existing dashboard rendered while the new data is fetched, then
  // swaps it in. Used by Edit-deck saves so the UI doesn't flicker
  // through 5+ seconds of "Loading…" while Scryfall resolves any
  // newly-added cards.
  const soft = opts && opts.soft;
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
      total_price_usd: _lastDashboardPriceUsd,
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

async function loadAdvise() {
  if (!_activeDeckId) return;
  const sug = $("sug-panel");
  if (!sug) return;
  const sourcePref = getAuditLLMPref();   // heuristic | bracket_peers | claude
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
    } else {
      setAuditLLMPref("heuristic");
    }
  }
  const sourceFinal = getAuditLLMPref();
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
  try {
    let url =
      `/api/audit?deck=${encodeURIComponent(_activeDeckId)}`
      + `&bracket=${auditBracket}`
      + `&source=${encodeURIComponent(sourceFinal)}`;
    if (sourceFinal === "claude") {
      url += `&model=${encodeURIComponent(getClaudeModel())}`;
    }
    const headers = {};
    if (keyFinal) headers["X-Anthropic-API-Key"] = keyFinal;
    const resp = await fetch(url, { headers });
    if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
    const body = await resp.json();
    _lastAuditProposed = body.proposed_text || null;
    _lastAuditManifest = {
      deck_id: _activeDeckId,
      bracket: auditBracket,
      audit_version: body.audit_version || (body.source === "claude" ? "v3-claude" : "v3"),
      source: body.source || "heuristic",
      added: (body.added || []).map((a) => ({
        card: a.card,
        rationale: a.rationale || "",
        match_pct: a.match_pct,
        price_usd: a.price_usd,
      })),
      removed: (body.removed || []).map((r) => ({
        card: r.card,
        rationale: r.rationale || "",
      })),
      diagnosis: body.diagnosis || "",
      weakness_signals: body.weakness_signals || [],
    };
    renderAuditResult(sug, body);
  } catch (e) {
    sug.innerHTML = "";
    sug.appendChild(el("h3", {}, "Audit"));
    sug.appendChild(el("p", { class: "muted" }, `Audit failed: ${e.message}`));
  }
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
      total_price_usd: _lastDashboardPriceUsd,
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

  // Adds list (with rationale + price).
  if (body.added.length) {
    container.appendChild(el(
      "h4", { style: "margin-top: 14px;" }, "Cards to add",
    ));
    const ul = el("ul", { class: "iteration-list" });
    for (const a of body.added) {
      const row = el("li", { class: "iteration" });
      row.appendChild(el("span", { class: "verdict pending" }, `${a.match_pct}%`));
      const wrap = el("div");
      const nameDiv = el("div", { class: "name" }, a.card);
      // Per-card hallucination flag — pill renders only on confirmed
      // Scryfall miss (name_known === false). null/undefined means
      // unchecked, do not flag.
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
      wrap.appendChild(nameDiv);
      if (a.rationale) {
        wrap.appendChild(el("div", { class: "muted" }, a.rationale));
      }
      row.appendChild(wrap);
      row.appendChild(el(
        "span", { class: "delta" },
        a.price_usd != null ? `$${Number(a.price_usd).toFixed(2)}` : "",
      ));
      ul.appendChild(row);
    }
    container.appendChild(ul);
  }

  // Removed list.
  if (body.removed.length) {
    container.appendChild(el(
      "h4", { style: "margin-top: 14px;" }, "Cards to cut",
    ));
    const ul = el("ul", { class: "iteration-list" });
    for (const r of body.removed) {
      const row = el("li", { class: "iteration" });
      row.appendChild(el("span", { class: "verdict reverted" }, "cut"));
      const wrap = el("div");
      const nameDiv = el("div", { class: "name" }, r.card);
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
