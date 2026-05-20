// Audit-streaming SSE client (extracted from app.js on 2026-05-19
// per AGENT_BACKLOG #007 — second slice of the Tier-3 app.js split
// after iteration_graph.js / c94a3e0). Same loading pattern: plain
// <script> tag in index.html before app.js; shares window globals
// so the cross-file references (el, _perCardSourceBadge, the
// _AUDIT_*/getAnthropicKey/getClaudeModel preference helpers) still
// resolve at call time.
//
// Exposes:
//   streamAuditEvents(url, headers, callbacks) — drive /api/audit/stream
//   _parseSseFrame(frame)                       — one SSE frame parser
//   updateAuditProgress(sug, phase, data, ...) — render the progress UI
//   renderManabasePreview(sug, recs)            — early manabase preview

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
