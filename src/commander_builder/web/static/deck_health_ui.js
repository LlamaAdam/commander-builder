// Deck-health tile row + salt-warning banner.
//
// Extracted from app.js on 2026-05-19 (AGENT_BACKLOG #008 — third
// slice of the Tier-3 split after iteration_graph.js + audit_
// streaming.js). Loaded via index.html alongside the other static
// scripts; shares window globals so cross-file references to
// el (defined in app.js) resolve at user-render time.
//
// Exposes:
//   renderDeckHealthTiles(health, grade)
//                                 — 5-tile row from /api/audit's
//                                   deck_health block; when a
//                                   health_grade payload is passed,
//                                   the row is wrapped with the
//                                   letter-grade panel header.
//   renderHealthGradeHeader(g)    — big-letter grade header (A..F /
//                                   N/A + score + top reasons).
//   renderHealthTile(opts)        — single tile builder used by
//                                   renderDeckHealthTiles.
//   renderSaltWarningBanner(w)    — yellow banner over the audit
//                                   when an aggregate salt score
//                                   exceeds the bracket threshold.

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
//
// `grade` (optional) is /api/audit's health_grade payload — the
// at-a-glance letter aggregating these signals. When present the tile
// row gets a panel header with the big letter + score + top reasons;
// when absent (legacy servers) the bare row renders exactly as before.
function renderDeckHealthTiles(health, grade) {
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

  // Spell density tile. A null signal means deck_health's Scryfall
  // outage contract fired (a majority of card lookups failed) — the
  // `|| {ratio: null}` fallback deliberately routes that into the same
  // "—" / unavailable rendering as the empty-deck case, since neither
  // has a trustworthy ratio to show.
  const sd = health.spell_density || {
    non_permanent_count: 0, total_main_count: 0, ratio: null,
  };
  // Partial-outage annotation: below the outage threshold the ratio is
  // computed from the cards Scryfall COULD classify; say so when any
  // lookups missed so a slightly-off ratio is explainable.
  const sdMisses = sd.lookup_failures || 0;
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
        + (sdMisses
          ? `\n\n${sdMisses} card lookup${sdMisses === 1 ? "" : "s"} failed — `
            + `ratio computed from the cards Scryfall could classify.`
          : "")
      : "Spell-density signal unavailable (Scryfall lookup failed).",
    flavor: (sd.ratio != null && sd.ratio >= 0.20) ? "good"
          : (sd.ratio != null && sd.ratio >= 0.10) ? "neutral"
          : "muted",
  }));

  // Mana sinks tile. Unlike spell density there is no null-equivalent
  // field inside the shape ("count: 0" is a REAL, warn-worthy state),
  // so a null signal — deck_health's Scryfall outage contract — gets
  // its own explicit "unavailable" tile. Before this branch, null fell
  // into the `|| { count: 0 }` fallback and rendered a warn-flavored
  // "0 mana sinks" on decks that simply couldn't be classified.
  const ms = health.mana_sinks;
  if (ms == null) {
    row.appendChild(renderHealthTile({
      label: "Mana sinks",
      value: "—",
      tooltip: "Mana-sink signal unavailable (Scryfall lookup failed).",
      flavor: "muted",
    }));
  } else {
    const msMisses = ms.lookup_failures || 0;
    row.appendChild(renderHealthTile({
      label: "Mana sinks",
      value: ms.count,
      tooltip: (ms.cards.length
        ? `X-cost spells (mana sinks):\n${ms.cards.join("\n")}\n\n`
          + `Mana sinks scale to whatever excess mana you have — they prevent `
          + `flooding out in long games. B4 decks typically run 3-5 of these.`
        : "No X-cost spells detected. A deck with no mana sinks can flood out "
          + "in long games when you draw lands you don't need.")
        + (msMisses
          ? `\n\n${msMisses} card lookup${msMisses === 1 ? "" : "s"} failed — `
            + `count computed from the cards Scryfall could classify.`
          : ""),
      flavor: ms.count >= 3 ? "good" : (ms.count >= 1 ? "neutral" : "warn"),
    }));
  }

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

  // Role-target tile (F2). Flags roles BELOW the deck-template minimums
  // (ramp/draw/removal/wipe/protection) — the complement of the advisor's
  // saturation guard (which flags EXCESS). Value is the count of
  // under-built roles; tooltip itemizes count/target/deficit per role.
  const rt = health.role_targets || { roles: {}, under_built: [] };
  const under = rt.under_built || [];
  const roleLines = Object.entries(rt.roles || {})
    .map(([role, v]) => {
      const flag = (v.deficit || 0) > 0 ? "  ⚠ -" + v.deficit : "  ✓";
      return `${role}: ${v.count}/${v.target}${flag}`;
    });
  row.appendChild(renderHealthTile({
    label: "Role targets",
    value: under.length === 0 ? "OK" : `${under.length} low`,
    sub: under.length ? under.join(", ") : "",
    tooltip: roleLines.length
      ? `Core roles vs deck-template minimums:\n${roleLines.join("\n")}\n\n`
        + `Roles below target are under-built — the advisor's saturation `
        + `guard flags the opposite (excess). Targets: ramp 10, draw 10, `
        + `removal 8, wipe 3, protection 4.`
      : "Role-target signal unavailable (Scryfall lookup failed).",
    flavor: roleLines.length === 0 ? "muted"
          : (under.length === 0 ? "good"
          : (under.length <= 2 ? "neutral" : "warn")),
  }));

  // Grade header wrap: keep the bare-row return for legacy payloads so
  // older servers (no health_grade field) render byte-identically.
  if (grade && grade.grade) {
    const panel = el("div", { class: "deck-health-panel" });
    panel.appendChild(renderHealthGradeHeader(grade));
    panel.appendChild(row);
    return panel;
  }
  return row;
}

// Panel header for the deck-health section: one big letter grade
// (A..F, or N/A when every signal was unavailable — Scryfall outage),
// the 0-100 score beside it, and the top 2-3 reasons the deck lost
// points beneath. Letter colors reuse the tile flavor palette so the
// header and the tiles read as one system: A/B green (good), C blue
// (neutral), D amber (warn), F red (bad), N/A slate (muted).
function renderHealthGradeHeader(grade) {
  const letterColors = {
    "A": "#4ade80",
    "B": "#4ade80",
    "C": "#60a5fa",
    "D": "#f59e0b",
    "F": "#ef4444",
    "N/A": "#94a3b8",
  };
  const color = letterColors[grade.grade] || letterColors["N/A"];
  // Component breakdown in the hover tooltip — same cursor:help
  // affordance as the tiles. Unavailable components say so instead of
  // showing a score (the outage contract: excluded, not zeroed).
  const compLines = Object.entries(grade.components || {}).map(
    ([name, c]) => {
      const pct = Math.round((c.weight || 0) * 100);
      return c.available
        ? `${name}: ${c.score}/100 (weight ${pct}%)`
        : `${name}: unavailable — excluded (weight ${pct}%)`;
    },
  );
  const wrap = el("div", {
    class: "health-grade-header",
    style: "display: flex; align-items: center; gap: 14px; "
         + "margin: 10px 0 0 0; padding: 10px 14px; "
         + "background: var(--bg); "
         + "border: 1px solid var(--border); "
         + "border-radius: 6px; cursor: help;",
    title: compLines.length
      ? `Weighted components:\n${compLines.join("\n")}`
      : "",
  });
  wrap.appendChild(el(
    "div",
    {
      class: "health-grade-letter",
      style: `font-size: 34px; font-weight: 700; line-height: 1; `
           + `color: ${color};`,
    },
    grade.grade,
  ));
  const col = el("div", {});
  col.appendChild(el(
    "div",
    { style: "font-weight: 600;" },
    "Deck health"
    + (grade.score != null ? ` — ${grade.score}/100` : ""),
  ));
  const reasons = grade.reasons || [];
  if (reasons.length) {
    const ul = el("ul", {
      class: "muted",
      style: "list-style: none; padding: 0; margin: 2px 0 0 0; "
           + "font-size: 12px;",
    });
    for (const r of reasons) ul.appendChild(el("li", {}, r));
    col.appendChild(ul);
  } else if (grade.grade === "N/A") {
    col.appendChild(el(
      "div",
      { class: "muted", style: "font-size: 12px;" },
      "Signals unavailable (Scryfall unreachable or empty deck).",
    ));
  }
  wrap.appendChild(col);
  return wrap;
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

// Render the infinite/win-combo assessment from /api/audit's
// `combo_assessment` block. Two visual states:
//   * VIOLATION (red, border-left): one or more detected combos push the
//     deck above its declared bracket (WotC restricts two-card infinite
//     combos below B4). Lists the offending combos + recommended bracket.
//   * INFO (blue <details>): combos present but legal at this bracket —
//     collapsed so it doesn't shout, but visible for awareness.
// Renders nothing when no combos are detected (keeps clean decks clean).
function renderComboAssessment(assessment) {
  const combos = (assessment && assessment.combos) || [];
  if (!combos.length) return document.createDocumentFragment();
  const violations = assessment.violations || [];
  const rec = assessment.recommended_bracket || 1;

  const comboLine = (c) => {
    const li = el("li", {});
    li.appendChild(el("span", { style: "font-weight: 500;" },
      (c.cards || []).join(" + ")));
    li.appendChild(document.createTextNode(
      `  →  ${c.produces || "combo"} `));
    li.appendChild(el("span", {
      class: "pill",
      style: "padding: 1px 6px; border-radius: 4px; font-size: 11px; "
           + "font-weight: 600; background: var(--border); color: var(--text);",
      title: "Lowest WotC bracket that permits this combo.",
    }, `B${c.bracket_floor}+`));
    return li;
  };

  if (violations.length) {
    const wrap = el("div", {
      class: "combo-violation-banner",
      style: "margin: 10px 0; padding: 10px 14px; "
           + "background: rgba(239, 68, 68, 0.12); "
           + "border-left: 4px solid #ef4444; border-radius: 6px; "
           + "color: var(--text);",
    });
    wrap.appendChild(el("div",
      { style: "font-weight: 600; margin-bottom: 4px;" },
      `Bracket pressure: ${violations.length} `
      + (violations.length === 1 ? "combo" : "combos")
      + ` exceed this bracket — this deck plays as Bracket ${rec}.`));
    const ul = el("ul", {
      style: "list-style: none; padding: 0; margin: 4px 0 0 0; "
           + "font-size: 13px;",
    });
    for (const c of violations) ul.appendChild(comboLine(c));
    wrap.appendChild(ul);
    return wrap;
  }

  // Legal-at-bracket: collapsed details, informational.
  const det = el("details", {
    style: "margin: 10px 0; padding: 8px 12px; "
         + "background: rgba(96, 165, 250, 0.08); "
         + "border: 1px solid rgba(96, 165, 250, 0.4); "
         + "border-radius: 6px; font-size: 13px;",
  });
  det.appendChild(el("summary",
    { style: "cursor: pointer; font-weight: 500;" },
    `${combos.length} infinite/win combo`
    + (combos.length === 1 ? "" : "s") + " detected (legal at this bracket)"));
  const ul = el("ul", {
    style: "list-style: none; padding: 0; margin: 6px 0 0 0;",
  });
  for (const c of combos) ul.appendChild(comboLine(c));
  det.appendChild(ul);
  return det;
}

// Render the EDHREC average-deck preview as a collapsible <details>
// section. The list is grouped by EDHREC category (Creatures / Lands /
// Ramp / ...); cards present in the user's current deck are marked
// with a green check, missing cards with a "+". Click "+" to pre-fill
// the manual-add input (UI hook — the input element looks up by id
// 'audit-manual-add' if present).
