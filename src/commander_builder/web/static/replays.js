// FP-016 replay-lite: browse persisted Forge game logs + turn timeline.
//
// Left sidebar (id="section-replays"): run list -> per-run game list,
// loaded from GET /api/replays. Main pane (id="replays-main"): the
// selected game's turn-by-turn timeline from GET /api/replay/<run>/<game>.
//
// Conventions match nav.js / app.js: vanilla DOM via an el() helper with
// NO innerHTML for server data (XSS discipline — deck names are
// user-controlled), keyboard access via real <button>s and native
// <details>/<summary> disclosure for collapsible turns (PR #36 a11y
// patterns: visible focus, aria-live status, aria-expanded state).

(function () {
  "use strict";

  function $id(id) { return document.getElementById(id); }

  function el(tag, attrs, children) {
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

  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  }

  function fmtDeck(name) {
    // Strip [USER] prefix, .dck extension and [Bn] suffix for display.
    return String(name || "")
      .replace(/^\[USER\]\s*/, "")
      .replace(/\.dck$/i, "")
      .replace(/\s*\[B[0-9?]\]\s*$/, "")
      .trim() || String(name || "");
  }

  function fmtCreated(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  // -----------------------------------------------------------------------
  // Run list (sidebar)
  // -----------------------------------------------------------------------

  function gameLabel(g) {
    let outcome;
    if (g.truncated) outcome = "truncated";
    else if (g.is_draw && !g.winner_name) outcome = "draw";
    else if (g.winner_name) outcome = "won by " + fmtDeck(g.winner_name);
    else outcome = "no winner";
    const turns = g.end_turn != null ? ", " + g.end_turn + " turns" : "";
    return "Game " + g.game + " — " + outcome + turns;
  }

  function renderRunList(container, data) {
    container.innerHTML = "";
    const runs = data.runs || [];
    if (!runs.length) {
      container.appendChild(el("p", { class: "muted" }, [
        data.enabled
          ? "No recorded runs yet. Run a sim and check back."
          : "No recorded runs. Enable capture with "
            + "COMMANDER_BUILDER_KEEP_GAME_LOGS=1 (or --keep-logs), "
            + "then run a sim.",
      ]));
      return;
    }
    runs.forEach((run) => {
      const gamesWrap = el("div", {
        class: "replay-game-list",
        id: "replay-games-" + run.run,
        hidden: "hidden",
        role: "group",
        "aria-label": "Games in run " + run.run,
      }, []);
      (run.games || []).forEach((g) => {
        const btn = el("button", {
          type: "button",
          class: "advise-btn replay-game-btn",
          "data-run": run.run,
          "data-game": String(g.game),
          title: (g.decks || []).map(fmtDeck).join(" vs "),
        }, [gameLabel(g)]);
        btn.addEventListener("click", () => loadGame(run.run, g.game));
        gamesWrap.appendChild(btn);
      });
      const runBtn = el("button", {
        type: "button",
        class: "topbar-btn replay-run-btn",
        "aria-expanded": "false",
        "aria-controls": gamesWrap.id,
        title: "Recorded " + fmtCreated(run.created),
      }, [
        el("span", {}, [fmtCreated(run.created) || run.run]),
        el("span", { class: "muted" }, [
          " · " + run.count + " game" + (run.count !== 1 ? "s" : "")
          + (run.cap_reached ? " · capped" : ""),
        ]),
      ]);
      runBtn.addEventListener("click", () => {
        const open = gamesWrap.hidden;
        gamesWrap.hidden = !open;
        runBtn.setAttribute("aria-expanded", open ? "true" : "false");
      });
      container.appendChild(runBtn);
      container.appendChild(gamesWrap);
    });
  }

  async function loadRuns() {
    const container = $id("replays-run-list");
    if (!container) return;
    container.innerHTML = '<p class="muted">Loading…</p>';
    try {
      const data = await fetchJSON("/api/replays");
      renderRunList(container, data);
    } catch (err) {
      container.innerHTML =
        '<p class="muted" role="alert">Could not load replays. Please try again.</p>';
    }
  }

  // -----------------------------------------------------------------------
  // Timeline viewer (main pane)
  // -----------------------------------------------------------------------

  function describeEvent(ev) {
    switch (ev.type) {
      case "life": {
        const delta = ev.to - ev.from;
        return fmtDeck(ev.name) + ": " + ev.from + " → " + ev.to
          + " (" + (delta > 0 ? "+" : "") + delta + ")";
      }
      case "elimination":
        return fmtDeck(ev.name) + " eliminated — " + (ev.reason || "unknown reason");
      case "cast":
        return fmtDeck(ev.name) + " casts " + (ev.spell || "?");
      case "attack":
        return fmtDeck(ev.name) + " attacks" + (ev.detail ? " " + ev.detail : "");
      case "confirm_action":
        return "AI struggled with " + (ev.card || "?") + " (confirmAction)";
      case "unsupported_card":
        return "Unsupported card: " + (ev.card || "?");
      default:
        return ev.type || "event";
    }
  }

  function lifeSummary(turn, players) {
    const totals = turn.life_totals || {};
    const parts = [];
    (players || []).forEach((p) => {
      const v = totals[String(p.seat)];
      if (v != null) parts.push(fmtDeck(p.name) + " " + v);
    });
    return parts.join(" · ");
  }

  function renderTimeline(main, run, game, body) {
    main.innerHTML = "";
    const tl = body.timeline || {};
    const meta = body.meta || {};
    const result = tl.result || {};
    const players = tl.players || [];

    const wrap = el("div", { class: "replay-view" }, []);
    wrap.appendChild(el("h2", { style: "margin-top:0;" },
      ["Replay — game " + game]));

    // Seats / decks header.
    const seatList = el("ul", { class: "replay-seats" }, []);
    (meta.decks || players.map((p) => p.name)).forEach((d, i) => {
      const p = players[i];
      const bits = ["Seat " + (i + 1) + ": " + fmtDeck(d)];
      if (p && p.eliminated) {
        bits.push(" — eliminated (" + (p.loss_reason || "unknown reason") + ")");
      } else if (p && p.ending_life != null) {
        bits.push(" — " + p.ending_life + " life");
      }
      seatList.appendChild(el("li",
        { class: p && p.eliminated ? "replay-elim" : "" }, bits));
    });
    wrap.appendChild(seatList);

    // Result line.
    let resultTxt;
    if (result.winner_name) {
      resultTxt = "Winner: " + fmtDeck(result.winner_name)
        + " (seat " + result.winner_seat + ")";
    } else if (result.is_draw) {
      resultTxt = "Result: draw (turn cap)";
    } else {
      resultTxt = "Result: unknown";
    }
    if (result.end_turn != null) resultTxt += " · ended turn " + result.end_turn;
    if (result.duration_ms != null) {
      resultTxt += " · " + (result.duration_ms / 1000).toFixed(1) + "s";
    }
    wrap.appendChild(el("p", { style: "font-weight:600;" }, [resultTxt]));

    // Truncated banner — honest partial-log marker.
    if (tl.truncated) {
      wrap.appendChild(el("div", {
        class: "replay-truncated-banner",
        role: "status",
      }, [
        "Partial replay: this log was truncated (timeout, abort, or hung "
        + "game) — the timeline below covers only what Forge logged.",
      ]));
    }

    // Turns as native disclosure widgets (keyboard accessible for free).
    const turns = tl.turns || [];
    if (!turns.length) {
      wrap.appendChild(el("p", { class: "muted" },
        ["No turn data in this log."]));
    }
    turns.forEach((t) => {
      const hasElim = (t.events || []).some((ev) => ev.type === "elimination");
      const life = lifeSummary(t, players);
      const summary = el("summary", {}, [
        "Turn " + t.turn + " — " + fmtDeck(t.active),
        hasElim ? el("span", { class: "replay-elim" }, [" ☠ elimination"]) : null,
        life ? el("span", { class: "muted" }, ["  ·  " + life]) : null,
      ]);
      const list = el("ul", {}, []);
      (t.events || []).forEach((ev) => {
        list.appendChild(el("li",
          { class: ev.type === "elimination" ? "replay-elim" : "" },
          [describeEvent(ev)]));
      });
      if (!(t.events || []).length) {
        list.appendChild(el("li", { class: "muted" }, ["No logged events."]));
      }
      const details = el("details", {
        class: "replay-turn" + (hasElim ? " has-elim" : ""),
      }, [summary, list]);
      wrap.appendChild(details);
    });

    main.appendChild(wrap);
  }

  async function loadGame(run, game) {
    const main = $id("replays-main");
    if (!main) return;
    main.innerHTML = '<p class="empty-state" role="status">Loading replay…</p>';
    try {
      const body = await fetchJSON(
        "/api/replay/" + encodeURIComponent(run) + "/" + encodeURIComponent(game));
      renderTimeline(main, run, game, body);
    } catch (err) {
      main.innerHTML =
        '<p class="empty-state" role="alert">Could not load that replay.</p>';
    }
  }

  // -----------------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------------

  let loadedOnce = false;

  function wire() {
    const refresh = $id("replays-refresh-btn");
    if (refresh) refresh.addEventListener("click", loadRuns);
    // Lazy-load the run list the first time the Replays rail section is
    // opened (nav.js owns the actual section switching).
    const rail = $id("left-rail");
    if (rail) {
      rail.addEventListener("click", (e) => {
        const btn = e.target.closest ? e.target.closest(".rail-btn") : null;
        if (!btn || btn.dataset.section !== "replays") return;
        if (!loadedOnce) {
          loadedOnce = true;
          loadRuns();
        }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
