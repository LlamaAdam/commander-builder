// Average-deck preview renderer (EDHREC reference build).
//
// Extracted from app.js on 2026-05-19 (AGENT_BACKLOG #009 — fourth
// slice of the Tier-3 split). Loaded via index.html alongside the
// other static scripts. Same plain-script-tag pattern as
// iteration_graph.js / audit_streaming.js / deck_health_ui.js.
//
// Exposes:
//   renderAverageDeckPreview(preview) — entry point, called from
//                                       renderAuditResult.
//   bracketSlugToInt(slug)            — slug → bracket number.
//   buildAverageDeckBody(preview)     — body of the <details> panel.
//   renderAverageDeckCard(card)       — one card row inside the body.

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

