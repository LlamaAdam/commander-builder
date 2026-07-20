// Iteration-graph SVG renderer.
//
// Extracted from app.js on 2026-05-19 as the first slice of the Tier-3
// app.js split. Plain script tag (loaded via index.html alongside
// app.js); shares window globals so the cross-file reference to
// el() (defined in app.js) still resolves at call time.
//
// Exposes renderIterationGraph(data) which the dashboard's
// "View iteration graph" toggle invokes. Returns a wrapper element
// containing the SVG plus the manual verdict-control panel for any
// pending iterations (the Tier-1.3 feature).

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
    // Milestone flag (#012) — a small ⚑ glyph at the top-center when
    // this iteration is milestone-tagged, so notable versions stand out
    // in the flow chart. The label itself lives in the hover tooltip to
    // keep the node face uncluttered.
    if (n.milestone) {
      g.appendChild(svgEl("text", {
        x: x + _GRAPH_NODE_W / 2, y: y + 18,
        "text-anchor": "middle",
        "font-size": 13,
        fill: "var(--accent)",
      }, "⚑"));  // ⚑
    }
    // Hover tooltip — show created_at + verdict (the parts not on
    // the rect face) + the milestone label when present.
    const title = svgEl("title", {},
      `Iteration ${n.iteration_n}\n`
      + `Verdict: ${n.verdict}\n`
      + (n.milestone ? `Milestone: ${n.milestone}\n` : "")
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
    // 'inconclusive' (sim finished but < 20 decisive games) must not
    // share pending's amber: pending means "no result yet", while
    // inconclusive means "result recorded, sample too small to call".
    // Gray-blue (indigo-400, matching the tailwind-400 tints above)
    // keeps it visually muted but distinct from neutral's slate gray.
    case "inconclusive": return "rgba(129, 140, 248, 0.10)"; // gray-blue
    case "pending":
    default:         return "rgba(245, 158, 11, 0.08)";   // amber
  }
}
