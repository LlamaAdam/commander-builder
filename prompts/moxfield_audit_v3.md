# Moxfield Commander Deck Audit — Reusable Prompt (v3)

> **Subprogram of commander_builder** — used as the LLM proposer step in the
> closed-loop deck improvement pipeline (see `docs/audit_workflow.md`). The
> audit modifies a Moxfield deck; `compare_versions.py` then validates whether
> the change actually improved the deck via Forge head-to-head simulation.
>
> Paste this prompt into a new Claude conversation. No setup needed — the audit
> starts with a single question.

---

## CHANGELOG

**v3 (2026-04-26)** — Improvement from a Tidus follow-up where the user manually surfaced cards I'd missed (Tekuthal in 6/7 references; Danny Pink in 5/7; Kimahri at 61% EDHREC):
8. **Step 4.5 added — mandatory consensus completeness sweep.** Before locking the ideal at Step 4, programmatically list every card hitting either (a) 5+ of 7 reference decks, OR (b) EDHREC inclusion >50% AND synergy >40%, that is NOT in the proposed ideal. For each, either add it or write a one-line justification for omission. Prevents the "section cap" failure mode where the build stops at an arbitrary card count without re-checking high-consensus cards.

**v2 (2026-04-25)** — Improvements from the v1 Tedious Tidus audit run:
1. Step 2A/2B rewritten as explicit navigate-then-scrape pattern; added recent-commander overlap rule (commanders < 6 months old: merge 2A+2B into one expanded reference pool of 6-8 decks).
2. Step 1 now includes a Scryfall set-release-date lookup so Step 2 can apply the recent-commander rule.
3. Step 2C now prefers EDHREC's `/commanders/<slug>` page over `/decks/<slug>/<bracket>` — the bracket-deck-list page is JS-blocked by cookie/query-string protection in many browser contexts, and the commanders page has the inclusion%/synergy data the audit actually uses.
4. Step 5 now includes a mandatory pre-execute math assertion: compute `current_qty - (unique_removes + basic_reductions) + adds_count`, assert it equals 99, surface in the user checkpoint.
5. Auto-bumper list reframed as a soft heuristic ("cards that *might* bump in combination") rather than a hard limit. Empirical Step 7 verification is the real test.
6. Step 5 now includes a keep-card justification list — every card the user keeps gets a 1-line note, so cards in current-but-not-in-ideal can't silently fall through.
7. Step 5.6 simulation is now opt-in by default rather than mandatory-with-skip-option.

---

## PROMPT START

You are auditing my MTG Commander decks on Moxfield. My Moxfield username is **LlamaNinja**. For each deck, follow the workflow below exactly. No budget restrictions — recommend the best cards regardless of price. Don't default to a fixed number of swaps — recommend however many are actually needed.

**Critical methodology note:** This audit uses a **blind-build-then-diff** approach. You will build the ideal version of each deck WITHOUT looking at my current list first. Only after the ideal is locked in do you pull my deck and compute the diff. This prevents anchoring on cards I already own and surfaces inclusions I should reconsider, not just gaps to fill.

---

### STEP 0 — ASK ME FIRST

Before doing anything else, your **first message** must ask me which decks to audit. Accept any of these inputs:

1. **A single Moxfield deck URL** — e.g., `https://www.moxfield.com/decks/abc123xyz`
2. **A Moxfield bookmark URL** — e.g., `https://www.moxfield.com/bookmarks/xyz789` (shareable folder of multiple decks)
3. **My profile URL** — `https://www.moxfield.com/users/LlamaNinja` (all my public decks)
4. **Multiple deck URLs** pasted as a list

**Note**: Moxfield's private "folders" are NOT shareable via URL. If I mention "my Commander folder" or similar, ask me to either (a) convert it to a bookmark and share that URL, or (b) paste the deck URLs directly.

Once I provide a URL, resolve it to a list of decks before continuing:

**For a bookmark URL** (`/bookmarks/{id}`):
```javascript
(async () => {
  const resp = await fetch('https://api2.moxfield.com/v1/bookmarks/BOOKMARK_ID');
  const data = await resp.json();
  return JSON.stringify(data.decks?.map(d => ({
    name: d.name,
    id: d.publicId || d.id,
    commander: d.commanders?.[0]?.name || d.main?.name,
    bracket: d.bracket,
    userBracket: d.userBracket,
    format: d.format,
    updatedAt: d.lastUpdatedAtUtc
  })));
})()
```
If that endpoint shape doesn't return decks, navigate to the bookmark URL in Chrome and extract deck links from the rendered page.

**For a user profile URL** (`/users/{username}`):
```javascript
(async () => {
  const resp = await fetch('https://api2.moxfield.com/v2/users/USERNAME/decks?pageNumber=1&pageSize=100');
  const data = await resp.json();
  return JSON.stringify(data.data?.filter(d => d.format === 'commander').map(d => ({
    name: d.name,
    id: d.publicId,
    commander: d.commanders?.[0]?.name,
    bracket: d.bracket,
    userBracket: d.userBracket,
    updatedAt: d.lastUpdatedAtUtc
  })));
})()
```

**For single deck URLs**: extract the deck ID from each URL (the segment after `/decks/`).

After resolving, **show me the parsed list and confirm before proceeding**:

> "Found N Commander decks. Auditing in this order:
> 1. DeckName (Commander, B#)
> 2. DeckName (Commander, B#)
> ...
> Confirm to proceed, or tell me to skip/reorder."

If a non-Commander format slipped through (e.g., the user shared a bookmark with mixed formats), filter it out and flag it.

---

### REFERENCE DATA

#### Game Changers List

WotC updates this list periodically. **At the start of every audit run, attempt to fetch the current list dynamically.** Try the Commander Brackets official source first, then fall back to the hardcoded list below if the fetch fails. Note the verification date in your summary.

**Hardcoded fallback (verify against official source — this list may be stale):**

```
WHITE: Drannith Magistrate, Enlightened Tutor, Farewell, Humility, Serra's Sanctum, Smothering Tithe, Teferi's Protection
BLUE: Consecrated Sphinx, Cyclonic Rift, Force of Will, Fierce Guardianship, Gifts Ungiven, Intuition, Mystical Tutor, Narset Parter of Veils, Rhystic Study, Thassa's Oracle
BLACK: Ad Nauseam, Bolas's Citadel, Braids Cabal Minion, Demonic Tutor, Imperial Seal, Necropotence, Opposition Agent, Orcish Bowmasters, Tergrid God of Fright, Vampiric Tutor
RED: Gamble, Jeska's Will, Underworld Breach
GREEN: Biorhythm, Crop Rotation, Gaea's Cradle, Natural Order, Seedborn Muse, Survival of the Fittest, Worldly Tutor
MULTICOLOR: Aura Shards, Coalition Victory, Grand Arbiter Augustin IV, Notion Thief
COLORLESS: Ancient Tomb, Chrome Mox, Field of the Dead, Glacial Chasm, Grim Monolith, Lion's Eye Diamond, Mana Vault, Mishra's Workshop, Mox Diamond, Panoptic Mirror, The One Ring, The Tabernacle at Pendrell Vale
```

#### Auto-Bracket Bumper Heuristic (Non-GC)

**This list is a soft heuristic, not a hard limit.** Moxfield's `autoBracket` detection sometimes flags these in combination, but a single inclusion rarely bumps a deck. The empirical Step 7 verification is the real test — if `autoBracket` matches `userBracket` after the swap, the deck is fine regardless of what's on this list. Use the list to inform user when proposing cuts at B2/B3, but don't pre-emptively reject every card on it.

```
STAX/PRISON: Blood Moon, Magus of the Moon, Meekstone, Smoke, Stasis, Winter Orb, Static Orb, Tangle Wire, Sphere of Resistance, Thorn of Amethyst, Trinisphere, Lodestone Golem, Glowrider, Vryn Wingmare, Thalia Guardian of Thraben, Archon of Emeria, Kataki War's Wage, Null Rod, Stony Silence, Collector Ouphe
COMBO ENABLERS: Aggravated Assault, Helm of the Host, Kiki-Jiki Mirror Breaker, Splinter Twin, Dockside Extortionist, Underworld Breach, Food Chain, Hermit Druid, Protean Hulk, Razaketh the Foulblooded, Yawgmoth Thran Physician
LAND DESTRUCTION/MLD: Armageddon, Ravages of War, Catastrophe, Cataclysm, Wildfire, Obliterate, Jokulhaups, Decree of Annihilation
EXTRA TURNS: Time Warp, Temporal Manipulation, Walk the Aeons, Time Stretch, Nexus of Fate, Expropriate
TUTORS (mass): Diabolic Intent, Grim Tutor, Personal Tutor, Sylvan Tutor — stacking 4+ tutors auto-bumps
POWERFUL FINISHERS (occasional bumpers in concert): Doubling Season, Cathars' Crusade, Craterhoof Behemoth, Triumph of the Hordes — empirically may not bump alone but worth flagging for the user
```

This list is empirical. Add to it only when Step 7 verification confirms an unexpected `autoBracket` jump.

#### Recent Sets (Post-Brackets-Release)

Reference decks lean toward older cards because lifetime-likes-cumulative sorting buries newer builds. **Always cross-check whether cards from these sets warrant inclusion** — the refs you find may not have integrated them yet.

```
2025: Aetherdrift (DFT), Tarkir: Dragonstorm (TDM), Final Fantasy (FIN), Final Fantasy Commander (FIC), Edge of Eternities (EOE), Marvel's Spider-Man (SPM), Avatar: The Last Airbender (TLA), Innistrad Remastered (INR)
2026: Lorwyn Eclipsed (LEC), Teenage Mutant Ninja Turtles (TMT), Secrets of Strixhaven (SOA), Marvel Super Heroes (MVL), The Hobbit (HBT), Reality Fracture, Star Trek
```

For each deck's strategy, search Scryfall for new cards from these sets that fit the archetype (Step 3 below).

---

### BRACKET RULES

| Bracket | Name | GC Limit | Auto-Bumper Tolerance |
|---------|------|----------|----------------------|
| B1 | Exhibition | 0 | 0 |
| B2 | Core | **ZERO** | Avoid heavy auto-bumpers |
| B3 | Upgraded | **Max 3** | Heuristic only — verify after edits |
| B4 | Optimized | Unlimited | No restrictions |
| B5 | cEDH | Unlimited | No restrictions |

For B2/B3, always verify `autoBracket` matches `userBracket` after edits. Trust the empirical signal over the heuristic list.

---

### TECHNICAL NOTES

- **WebFetch is BLOCKED for api2.moxfield.com** — use browser JavaScript (`javascript_tool`).
- **EDHREC bracket-deck-list pages (`/decks/<slug>/<bracket>`) often block JS execution with `BLOCKED: Cookie/query string data`** — use the commander page (`/commanders/<slug>`) instead, which has aggregated inclusion%/synergy data.
- **EDHREC commander pages can be fetched via `get_page_text`** — full data dump.
- **API response truncation**: Split into chunks (slice 0–50, 50+) for complete data. Store large objects on `window.__name` to span calls.
- **Globals are wiped on navigation** — re-fetch after any navigate call.
- **Secret Lair / alt printings**: API returns Oracle names, but bulk edit textarea may show printing-specific names. Pre-flight check is part of Step 5.
- **Rollback**: Settings → Version History on Moxfield can restore prior deck versions if a save breaks something.
- **Moxfield search**: The base64-encoded `q` parameter on `/decks/public?q=...` works only via front-end navigation, not direct API fetch. Construct the URL, navigate, then scrape rendered deck links from DOM in a separate JS call.

---

### WORKFLOW — For Each Deck

#### Step 1: Pull Commander Info ONLY — Do NOT Look at Current Card List

Get only the commander identity, target bracket, **and the commander's set release date** (used by Step 2 to detect recent commanders).

```javascript
(async () => {
  const resp = await fetch('https://api2.moxfield.com/v3/decks/all/DECK_ID');
  const data = await resp.json();
  const commanders = Object.values(data.boards.commanders.cards).map(c => ({
    name: c.card.name,
    id: c.card.id,
    colorIdentity: c.card.color_identity,
    set: c.card.set
  }));
  // Look up set release date via Scryfall for the primary commander
  const setCode = commanders[0].set;
  const setResp = await fetch('https://api.scryfall.com/sets/' + setCode);
  const setData = await setResp.json();
  const releasedAt = setData.released_at;
  const monthsOld = (Date.now() - new Date(releasedAt).getTime()) / (30 * 24 * 60 * 60 * 1000);
  return JSON.stringify({
    name: data.name,
    targetBracket: data.userBracket || data.bracket,
    commanders: commanders,
    isPartnerOrBackground: commanders.length > 1,
    commanderSet: setCode,
    commanderSetReleased: releasedAt,
    commanderMonthsOld: Math.round(monthsOld * 10) / 10,
    isRecentCommander: monthsOld < 6
  });
})()
```

**Confirm with me which strategy/archetype to optimize for** if the commander supports multiple viable builds (e.g., "Atraxa: superfriends, infect, +1/+1 counters, or proliferate?"). Don't guess from my current deck — ask.

#### Step 2: Find Reference Decks (Three Sources, Adapted for Recent Commanders)

You're building a composite ideal from three independent sources. Each has different biases — using all three reduces inheriting any single source's blind spots.

**RECENT COMMANDER RULE**: If `isRecentCommander` from Step 1 is true (commander's set < 6 months old), **merge 2A and 2B into one expanded reference pool of 6-8 lifetime-likes decks** instead of running two separate queries. The 90-day-recent and lifetime-likes will overlap heavily anyway. Note this consolidation in your output.

**2A. Moxfield — Lifetime Most-Liked at Same Bracket** (captures the established consensus)

This is a two-step process: (1) build the encoded URL, (2) navigate to it, (3) scrape rendered deck links.

```javascript
// Step 2A.1: Construct the encoded search URL
(async () => {
  // First find the commander's Moxfield card ID via card search
  const cardResp = await fetch('https://api2.moxfield.com/v2/cards/search?q=' + encodeURIComponent('COMMANDER_NAME') + '&limit=5');
  const cardData = await cardResp.json();
  const card = cardData.data.find(c => c.name === 'COMMANDER_EXACT_NAME');
  
  const query = {
    "commanderCardId": card.id, "commanderCardName": card.name,
    "bracketSetting": "equals", "bracket": BRACKET_NUMBER,
    "sortColumn": "likes", "sortDirection": "descending",
    "pageNumber": 1, "pageSize": 64, "view": "public",
    "selectedCardIds": { "commanderCardId": card.id },
    "selectedCardNames": { "commanderCardId": card.name },
    "cardIncluded": [], "cardExcluded": [], "createdAtFrom": "", "createdAtTo": "",
    "updatedAtFrom": "", "updatedAtTo": "", "format": "commander",
    "boards": ["mainboard", "sideboard", "considering", "commanders"]
  };
  const encoded = btoa(JSON.stringify(query));
  return 'https://www.moxfield.com/decks/public?q=' + encodeURIComponent(encoded);
})()
```

Then navigate the browser to the returned URL, wait ~5s for hydration, then:

```javascript
// Step 2A.2: Scrape deck links from rendered page
(async () => {
  await new Promise(r => setTimeout(r, 5000));
  const links = Array.from(document.querySelectorAll('a[href*="/decks/"]'))
    .map(a => ({href: a.getAttribute('href'), text: (a.textContent || '').trim().slice(0, 100)}))
    .filter(a => a.href.match(/\/decks\/[A-Za-z0-9_-]+$/) && !a.href.includes('/public') && !a.href.includes('/personal'));
  const seen = new Set();
  const unique = links.filter(l => seen.has(l.href) ? false : (seen.add(l.href), true));
  return JSON.stringify({count: unique.length, links: unique.slice(0, 20)});
})()
```

Pick the top 4 (or 6-8 if recent commander) by likes and fetch each via `/v3/decks/all/<id>`.

**Build `window.__refFreq` for Step 4.5** — after fetching all reference decks, compute card frequency:

```javascript
(async () => {
  // refDeckIds is the array of deck IDs fetched in 2A (and 2B if applicable)
  window.__refDecks = [];
  for (const id of refDeckIds) {
    const r = await fetch('https://api2.moxfield.com/v3/decks/all/' + id);
    const d = await r.json();
    const mc = Object.values(d.boards.mainboard.cards).map(c => c.card.name);
    window.__refDecks.push({id, name: d.name, likes: d.likeCount, mainboard: mc});
  }
  const freq = {};
  for (const deck of window.__refDecks) {
    const seen = new Set(deck.mainboard);
    for (const card of seen) freq[card] = (freq[card] || 0) + 1;
  }
  window.__refFreq = freq;
  window.__refDeckCount = window.__refDecks.length;
  return JSON.stringify({deckCount: window.__refDecks.length, uniqueCards: Object.keys(freq).length});
})()
```

**2B. Moxfield — Recently-Updated Most-Liked (Past 90 Days)** (captures current meta)

**Skip if `isRecentCommander` is true.** Otherwise same as 2A but with `"updatedAtFrom": ninetyDaysAgo` in the query (use `new Date(Date.now() - 90*24*60*60*1000).toISOString()`).

If 90-day window returns < 10 decks for non-recent commanders, widen to 180 days. If still sparse, fall back to 2A only and note this in the summary.

**2C. EDHREC — Commander Page (preferred over bracket-deck-list page)**

Navigate to: `https://edhrec.com/commanders/COMMANDER-SLUG`

Use `get_page_text` (not `javascript_tool`) — the bracket-deck-list page (`/decks/<slug>/<bracket>`) is often JS-blocked by cookie/query-string protection. The commander page is reliably fetchable and has all the inclusion%/synergy data you need.

Parse the returned text for:
- **High Synergy Cards** section (cards uniquely correlated with this commander)
- **Top Cards** section (commander-color staples)
- **Game Changers** section (already-categorized GCs, with inclusion %)
- Each category section (Creatures, Instants, Sorceries, etc.) for category coverage
- **New Cards** section (recent set inclusions)

For B2/B3 audits, weight by inclusion % (cards in 50%+ of decks). Salt is not the right signal at lower brackets.

**Build `edhrecData` array for Step 4.5** — parse the page text into a structured array:

```javascript
// Pseudocode — actual parsing depends on get_page_text output format
const edhrecData = []; // populate with {name, inclusionPct, synergyPct} objects from each section
// Save as window.__edhrecData for Step 4.5
window.__edhrecData = edhrecData;
```

**Read primers if available** — for any of the Moxfield reference decks tagged "Primer" in their card text, visit `/decks/REF_DECK_ID/primer` for strategy notes.

#### Step 3: Recent-Set Check

For the deck's strategy/archetype, search Scryfall for cards from sets in the "Recent Sets" list above. Construct queries like:

```
https://api.scryfall.com/cards/search?q=(set:spm+OR+set:eoe+OR+set:fin+OR+set:fic+OR+set:tdm+OR+set:dft+OR+set:lec+OR+set:tmt+OR+set:soa)+id<=COLOR_IDENTITY+THEME_FILTER&unique=cards
```

Adapt the oracle/type filters to the deck's themes (e.g., for landfall: `o:landfall`; for tokens: `o:create OR o:token`; for graveyard: `o:graveyard`). **Identify candidate inclusions from these sets that the older lifetime-likes refs likely don't have.** Sort results by `edhrec_rank` ascending and review the top 30-40. Filter against your Step 2 reference card pool to find genuinely novel candidates.

#### Step 4: Build the Ideal Deck (Still Without Looking at Current List)

Synthesize a 99-card ideal using the references plus recent-set candidates:

**Inclusion priority:**
- **Tier S**: In EDHREC top inclusion (>50%) AND 3+ Moxfield reference decks. Auto-include unless GC/bumper-blocked.
- **Tier A**: In EDHREC top AND 1-2 Mox refs. Strong include.
- **Tier B**: In one source + clearly fits the role.
- **Tier C**: Recent-set candidate not yet in references but clearly fits the gameplan.
- **Tier D**: Format staple by role (lands, ramp, draw, removal, win cons) chosen to fill any gaps.

**Mana base targets** (defaults unless strategy demands otherwise):
- Lands: 36–38 (lower for low-curve aggro/combo, higher for heavy ramp/landfall)
- Ramp: 8–12 pieces
- Card draw: 8–12 pieces
- Targeted removal: 5–8 pieces
- Sweepers: 2–4 pieces

**For B2/B3, check every proposed include against the GC list AND the auto-bumper heuristic.** Reject GCs entirely for B2; cap at 1-2 for B3 (heuristic, real limit is 3). For auto-bumpers, treat as a flag, not a block.

**Deliverable for Step 4:** A complete 99-card list (plus commander) with each card tagged by tier (S/A/B/C/D) and role (land, ramp, draw, removal, threat, utility, win-con). **Do NOT show this to the user yet — first run Step 4.5.**

#### Step 4.5: Consensus Completeness Sweep (NEW IN v3 — MANDATORY)

Before showing the ideal to the user, run this check to catch high-consensus cards that fell through your section caps. The failure mode this prevents: building "32 counter payoffs" and stopping, while a card in 6 of 7 reference decks sits unconsidered.

```javascript
(async () => {
  // Inputs (must be set up by Step 2):
  // - window.__refFreq: object mapping cardName -> count of reference decks it appears in
  // - window.__refDeckCount: total number of reference decks (typically 4-8)
  // - window.__edhrecData: array of {name, inclusionPct, synergyPct} parsed from Step 2C
  // - idealList: array of card names in your proposed ideal from Step 4 (define inline)
  
  const idealList = [/* your Step 4 ideal card names */];
  const idealSet = new Set(idealList.map(s => s.toLowerCase()));
  const refThreshold = Math.max(5, Math.floor(window.__refDeckCount * 0.7)); // 5+ for 7 refs, scales for other counts
  
  // Tier 1: cards in refThreshold+ of reference decks NOT in ideal
  const refMisses = Object.entries(window.__refFreq)
    .filter(([name, count]) => count >= refThreshold && !idealSet.has(name.toLowerCase()))
    .map(([name, count]) => ({name, refCount: count, source: 'mox_consensus'}));
  
  // Tier 2: EDHREC cards >50% inclusion + >40% synergy NOT in ideal
  const edhrecMisses = (window.__edhrecData || [])
    .filter(c => c.inclusionPct > 50 && c.synergyPct > 40 && !idealSet.has(c.name.toLowerCase()))
    .map(c => ({name: c.name, inclusion: c.inclusionPct, synergy: c.synergyPct, source: 'edhrec_high_synergy'}));
  
  return JSON.stringify({
    refThreshold,
    refMissesCount: refMisses.length,
    refMisses,
    edhrecMissesCount: edhrecMisses.length,
    edhrecMisses
  });
})()
```

**For every card surfaced**, take ONE of these actions and document it inline:
- **Add to ideal** + remove a weaker card from the ideal to keep the count at 99.
- **Justify omission** with a one-line reason (e.g., "off-archetype despite consensus", "redundant with [card X] in ideal", "in current deck already so will be a keep, not an add", "anti-synergy with [strategy]").

If the sweep returns 0 candidates, state that explicitly: "Completeness sweep clean — no cards in N+ refs or >50%/40% EDHREC missing from ideal."

**Show the sweep results to the user as part of the Step 4 deliverable**, then proceed to Step 5. This is the user's checkpoint to redirect the build before committing to a diff.

#### Step 5: NOW Pull My Current Deck and Compute the Diff

```javascript
(async () => {
  const resp = await fetch('https://api2.moxfield.com/v3/decks/all/DECK_ID');
  const data = await resp.json();
  const mc = Object.values(data.boards.mainboard.cards);
  const totalQty = mc.reduce((s, c) => s + c.quantity, 0);
  const riskyPrintings = mc.filter(c => {
    const set = (c.card.set || '').toLowerCase();
    return set.startsWith('sld') || set.includes('secret');
  }).map(c => ({ name: c.card.name, set: c.card.set }));
  window.__currentDeck = mc.map(c => ({name: c.card.name, qty: c.quantity, cmc: c.card.cmc, type: c.card.type_line, oracle: c.card.oracle_text || '', colors: c.card.color_identity, set: c.card.set}));
  return JSON.stringify({
    bracket: data.bracket,
    autoBracket: data.autoBracket,
    userBracket: data.userBracket,
    uniqueCards: mc.length,
    totalQuantity: totalQty,
    riskyPrintings: riskyPrintings,
    cards_0_50: mc.slice(0, 50).map(c => ({name: c.card.name, qty: c.quantity})),
    cards_50_plus: mc.slice(50).map(c => ({name: c.card.name, qty: c.quantity}))
  });
})()
```

Compute the diff:
- **Cards in ideal but not in current** = ADD list
- **Cards in current but not in ideal** = REMOVE candidate list (apply tier filter)
- **Cards in both** = KEEP list

**Apply removal-tier filter to the REMOVE list** before finalizing:
- **Tier 1 (cut)**: Strictly worse than something in the ideal doing the same job.
- **Tier 2 (cut)**: Off-strategy — doesn't fit the gameplan or any subtheme.
- **Tier 3 (evaluate)**: Marginal in the ideal but I might have a reason. If unclear, flag it for me.
- **Tier 4 (preserve unless I say otherwise)**: Pet cards, flavor picks, or known heuristic-list bumpers. Always flag explicitly with both "why I want to cut" and "why you might keep" reasoning.

**KEEP-CARD JUSTIFICATION (v2)**: For every card in the KEEP list that did NOT appear in your Step 4 ideal, list it explicitly with a 1-line note explaining whether it's de-facto kept (because not on cut list) or actively endorsed. This catches cases where a current card is neither in the ideal nor on the cut list — preventing silent passthrough.

**MANDATORY MATH ASSERTION (v2)**: Before showing the swap list, compute:
```
final_count = current_total_qty - unique_removes - basic_reductions + adds_count
```
Assert `final_count === 99`. If not, surface the discrepancy in the user checkpoint and propose specific add-drops or basic adjustments to make it match.

**Show me the proposed swap list with tier annotations, the keep-card justification list, AND the math assertion result before executing.** This is my second checkpoint — to override pet-card cuts, push back on Tier 3 calls, or correct any math drift.

#### Step 5.5: Mana Base + Curve Sanity Check

Project the post-swap state. Compare against the ideal's targets and the current deck:

| Metric | Current | Ideal | Post-Swap | Within Threshold? |
|--------|---------|-------|-----------|-------------------|
| Land count | X | Y | Z | flag if >2 off ideal |
| Avg CMC | X.XX | Y.YY | Z.ZZ | flag if >0.3 off ideal |
| Ramp pieces | X | Y | Z | flag if >2 off ideal |
| Draw pieces | X | Y | Z | flag if >2 off ideal |
| Removal | X | Y | Z | flag if >2 off ideal |

If thresholds breach, revise swap list before Step 6.

#### Step 5.6 (OPTIONAL): Statistical Simulation (Current vs. Post-Swap)

**This step is OPT-IN by default in v2.** Ask the user explicitly: "Run a 100-game goldfish simulation as a sanity check before executing? It compares mulligan rate, average commander turn, recovery after removal, and wincon reachability between current and post-swap. Useful when (a) the swap is large/risky, (b) you want a consistency check, or (c) you suspect the swap might regress the deck. Skip is fine for additive swaps."

**NOTE (commander_builder integration):** Step 5.6 goldfish simulation is largely superseded by `commander_builder/compare_versions.py`, which runs a real Forge head-to-head simulation between v1 and v2. Default to **skip** Step 5.6 if the user is running the audit as part of the commander_builder pipeline — the empirical Forge comparison is the stronger signal. Use Step 5.6 only when Forge isn't available or for very large swaps where pre-execute consistency check has value.

Default recommendation: skip if the swap is overwhelmingly additive (more ramp, more on-archetype density, no power loss) and the user has expressed preference for empirical playtesting.

**If the user opts in, run this methodology** (full code in Appendix A — kept for reference but not inlined to avoid bloating the main flow):

Tag every card by role using the regex tagger (land / ramp / fast_ramp / draw / removal / wipe / counter / protection / wincon / cmdr_dependent), then simulate 100 games of each deck against a modeled "average pod" (2 removal/game, ~70% wipe by T7, 15% counter chance). Aggregate metrics:

| Metric | Current | Post-Swap | Delta | Gate |
|--------|---------|-----------|-------|------|
| Mulligan rate | X% | Y% | ±N% | flag if post-swap > current by >5% |
| Avg commander turn | X.X | Y.Y | ±N | flag if post-swap > current by >0.5 turns |
| Recovery after cmdr removal | X% | Y% | ±N% | flag if post-swap < current by >10% |
| Avg cards in hand T7 | X.X | Y.Y | ±N | flag if post-swap < current by >1 card |
| Wincon reachability | X% | Y% | ±N% | flag if post-swap < current by >10% |

**Honest framing — include verbatim in the output**: "These numbers reflect goldfish consistency against a modeled average pod. They do not predict real game outcomes against your actual playgroup. The strongest signal is large deltas (>10%); small deltas are within noise."

**Gate logic:**
- 0 metrics regress past threshold: ✅ Proceed.
- 1 metric regresses: ⚠️ Surface to me with specifics. I decide.
- 2+ metrics regress: 🛑 Block by default. Surface and ask whether to revise / proceed / abort.

#### Step 6: Execute Mass Edit

Navigate to: `https://www.moxfield.com/decks/DECK_ID/edit`

```javascript
(async () => {
  await new Promise(r => setTimeout(r, 4000));
  const id = 'DECK_ID';
  const out = [/* cards to remove */];
  const inn = [/* cards to add */];
  const basicTargets = {/* e.g., 'Forest': 4, 'Island': 2, 'Plains': 1 — only if reducing basics */};

  const resp = await fetch('https://api2.moxfield.com/v3/decks/all/' + id);
  const data = await resp.json();
  const mc = Object.values(data.boards.mainboard.cards);
  const outSet = new Set(out.map(c => c.toLowerCase()));
  const existingNames = new Set(mc.map(c => c.card.name.toLowerCase()));

  const lines = [];
  let removed = [];
  for (const c of mc) {
    const n = c.card.name;
    const nl = n.toLowerCase();
    const nlBase = nl.split(' // ')[0];
    if (outSet.has(nl) || outSet.has(nlBase)) {
      removed.push(n);
      continue;
    }
    if (basicTargets[n] !== undefined) {
      lines.push(basicTargets[n] + ' ' + n);
    } else {
      lines.push(c.quantity + ' ' + n);
    }
  }

  let added = [];
  for (const c of inn) {
    const cBase = c.split(' // ')[0].toLowerCase();
    if (!existingNames.has(c.toLowerCase()) && !existingNames.has(cBase)) {
      lines.push('1 ' + c);
      added.push(c);
    }
  }

  const total = lines.reduce((s, l) => s + parseInt(l), 0);
  
  // PRE-SAVE MATH ASSERTION
  if (total !== 99) {
    return JSON.stringify({error: 'COUNT MISMATCH', total, expected: 99, lineCount: lines.length, lastFew: lines.slice(-10)});
  }

  const ta = document.querySelector('textarea');
  ta.focus();
  ta.select();
  document.execCommand('insertText', false, lines.join('\n'));
  ta.blur();

  await new Promise(r => setTimeout(r, 1000));
  const saveBtn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Save');
  if (saveBtn) saveBtn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));

  await new Promise(r => setTimeout(r, 3000));
  return JSON.stringify({ status: 'SAVED', totalCards: total, removed, added });
})()
```

If the script returns `COUNT MISMATCH`, fix the add/remove lists before retrying. Do not save with wrong count.

#### Step 7: Verify After Every Edit

```javascript
(async () => {
  const resp = await fetch('https://api2.moxfield.com/v3/decks/all/DECK_ID');
  const data = await resp.json();
  const mc = Object.values(data.boards.mainboard.cards);
  const totalQty = mc.reduce((s, c) => s + c.quantity, 0);
  const commanders = Object.values(data.boards.commanders.cards).map(c => c.card.name);
  const gcList = [/* full GC list */];
  const gcCount = mc.filter(c => gcList.some(gc => gc.toLowerCase() === c.card.name.toLowerCase())).length;
  return JSON.stringify({
    bracket: data.bracket,
    autoBracket: data.autoBracket,
    userBracket: data.userBracket,
    uniqueCards: mc.length,
    totalQuantity: totalQty,
    commanders: commanders,
    gcCount: gcCount
  });
})()
```

**Verify all of the following:**
- ✅ `totalQuantity` = 99
- ✅ `bracket` matches target
- ✅ `autoBracket` matches `userBracket` (this is the real bumper test — trust it over the heuristic list)
- ✅ `commanders` unchanged from Step 1
- ✅ B3: GC count ≤ 3 / B2: GC count = 0

**Recovery procedures:**
- **Card count below 99**: Cross-reference against Step 5's `riskyPrintings` list. The dropped card is likely one of those. Add a replacement.
- **autoBracket jump**: Identify the offending card from this edit, add it to the local auto-bumpers list for future runs, then remove and replace. Note in Step 8 prompt review.
- **Commander zone changed**: Use Moxfield version history to roll back. Don't try to fix with another textarea edit.
- **Wholesale breakage**: Settings → Version History → restore.

---

### CLOSING SUMMARY

Work through each deck sequentially (in the order I confirmed in Step 0). After all decks are complete, provide a summary table:

| Deck | Bracket | Cards Added | Cards Cut | Pre-GC | Post-GC | Mana Base Δ | Sweep Adds | Sim Δ | Recent-Set Adds | Status |
|------|---------|-------------|-----------|--------|---------|-------------|------------|-------|------------------|--------|
| ... | ... | ... | ... | ... | ... | ... | (cards added by Step 4.5 sweep) | ... | ... | ✅/⚠️/🛑 |

For "Sim Δ" column: report the largest delta from Step 5.6 if run; otherwise "skipped".

Note: GC list verification date, any new auto-bumpers discovered, any commanders where the recency window was sparse, recent-commander consolidation cases (2A+2B merged), Step 4.5 sweep catches, and any decks where simulation gating was overridden (and why).

**commander_builder hand-off**: After the audit completes, write the final swap manifest to a structured file the Forge harness can consume (see `docs/audit_workflow.md`).

**Use this JS snippet to download the manifest as a file** (one click, no manual copy-paste). Run in the browser console after Step 7 verification passes:

```javascript
(() => {
  // Inputs (fill these from your audit):
  const manifest = {
    deck_id: "DECK_PUBLIC_ID",            // from Step 1's Moxfield API call
    deck_name: "DECK_NAME",
    bracket: BRACKET_NUMBER,
    audit_version: "v3",
    audit_timestamp: new Date().toISOString(),
    added: [/* the cards you ADDED in Step 6 */],
    removed: [/* the cards you REMOVED in Step 6 */],
    rationale: "ONE_PARAGRAPH_STRATEGIC_INTENT",
    step_4_5_sweep_catches: [/* cards added by the consensus sweep */],
    auto_bracket_after: AUTO_BRACKET_FROM_STEP_7,
    user_bracket: USER_BRACKET_FROM_STEP_7,
  };
  // Trigger download as <deck-name>.audit_manifest.json — by convention,
  // commander_builder/proposer.py looks for this filename next to the .dck.
  const blob = new Blob([JSON.stringify(manifest, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  // Sanitize the filename the same way commander_builder.moxfield_import does.
  const safe = manifest.deck_name.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_").replace(/[^\x00-\x7f]/g, "").trim();
  a.download = `[USER] ${safe} [B${manifest.bracket}].dck.audit_manifest.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  return `Saved ${a.download}`;
})()
```

After download, move the file to your `vendor/forge/userdata/decks/commander/` directory next to the corresponding `.dck`. Then run:

```
commander-iterate --old "[USER] My Deck v1 [B3].dck" \
                  --new "[USER] My Deck v2 [B3].dck" \
                  --bracket 3
```

The `commander-iterate` CLI auto-discovers the manifest at `<new_deck>.audit_manifest.json` and feeds it to the analyst. Alternatively, pass `--manifest <path>` to point at the file directly.

The schema is also documented in `docs/audit_workflow.md` — keep them in sync if either changes.

---

### STEP 8 — PROMPT SELF-IMPROVEMENT REVIEW

After the closing summary, **review the audit run for prompt improvement opportunities**. This is how the prompt gets better over time.

Reflect honestly on:

1. **Friction points**: Where did the workflow stall, require improvisation, or produce an unclear deliverable?
2. **Missed catches**: Did anything go wrong that a workflow step *should* have caught but didn't? Specifically: did the user surface any cards manually that the Step 4.5 sweep should have caught? If yes, the sweep thresholds may need tightening.
3. **Over-engineering**: Were any steps unnecessary, redundant, or producing low-value output?
4. **New auto-bumpers discovered**: Any cards that triggered Moxfield's autoBracket jump unexpectedly during this run?
5. **Stale reference data**: Was the GC list verifiably out of date? Did Moxfield's API shape change in a way that broke a query? Are there new sets that should be added to the Recent Sets reference?
6. **Tagger errors in simulation**: Did the role tagger in Step 5.6 misclassify enough cards to materially distort the sim output? If so, what regex patterns need adjustment?

**Output format:**

> "Prompt review for this run:
> - [Issue 1]: [what happened] → [proposed fix]
> - [Issue 2]: ...
> - No issues found in: [list of areas that worked smoothly]"

If issues were found, **ask explicitly**: "Want me to apply these changes to the prompt? I can write the updated version to a file. Y/N for each, or 'all' / 'none'."

**Do NOT modify the prompt without asking.** Some "issues" are situational and don't warrant a permanent change. I make the call on each one.

If I approve changes, save the updated prompt with a version suffix (e.g., `prompts/moxfield_audit_v4.md`) and present it. Note in the changelog header which version this is and what changed.

If no improvements are warranted (rare but possible on a smooth run), just say so: "No prompt changes recommended this run." Don't invent issues to look thorough.

## PROMPT END

---

## APPENDIX A — Step 5.6 Simulation Code (for opt-in runs)

Pre-step: tag every card by role (apply to both current and post-swap deck states):

```javascript
const tagCard = (card) => {
  const text = (card.oracle_text || '').toLowerCase();
  const types = (card.type_line || '').toLowerCase();
  const cmc = card.cmc || 0;
  const tags = [];

  if (types.includes('land')) tags.push('land');
  if (/add \{[wubrgc]/.test(text) && !types.includes('land')) tags.push('ramp');
  if (/search your library.*\bland\b/.test(text)) tags.push('ramp');
  if (cmc <= 2 && tags.includes('ramp')) tags.push('fast_ramp');
  if (/draw .* cards?|draws? a card/.test(text)) tags.push('draw');
  if (/destroy target|exile target/.test(text)) tags.push('removal');
  if (/destroy all|exile all|each .* sacrifices/.test(text)) tags.push('wipe');
  if (/counter target/.test(text)) tags.push('counter');
  if (/hexproof|indestructible|protection from/.test(text)) tags.push('protection');
  if (/win the game|infinite|each opponent loses/.test(text)) tags.push('wincon');
  if (text.includes('commander') || /whenever .* attacks/.test(text)) tags.push('cmdr_dependent');

  return { name: card.name, cmc, tags, colors: card.color_identity || [] };
};
```

Simulation routine — see prior versions of this prompt for the full ~150-line `runGame` + aggregation block. Methodology summary: London mulligan keep rule (2-5 lands + early play), play ramp T1-4, cast commander when affordable, prioritize draw > removal > wincon > threats per turn, apply per-turn disruption (targeted removal, sweepers, counters), track per-game outcomes, aggregate over 100 runs.
