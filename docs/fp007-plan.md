# FP-007 — Unified MTG application (implementation plan)

**Decision (2026-05-26):** start FP-007. North star: one app consolidating
deck testing + card reference + rules lookup + a deck library + (later)
replays, instead of the current pile of CLIs + the audit web GUI.

**Reality:** ~6–10 weeks of work. This doc is the plan + the first slice;
it is NOT done. It stays on `feature` as the living spec; slices land
incrementally behind the existing web app so nothing regresses.

## What already exists (the substrate — don't rebuild)

- **Deck testing:** the Flask web app (`web/`) — audit, propose/sim, dashboard,
  combos, role-targets, image cache. This is the natural shell to grow into.
- **Card reference:** `oracle_store.py` + `scryfall_client` snapshot cache +
  `mtg_cards/` shared image/oracle data.
- **Combos / rules-ish:** `combo_detection.py`, `game_changers.py`, bracket
  enforcement, `staples.classify_role*`.
- **Library:** the `.dck` deck dir + `knowledge_log` iteration history +
  pricing series.
- **Engine:** Forge (via `forge_runner`) + the parked `forge_py` goldfish sim.

The unification is mostly **navigation + a shared card-reference surface**
over substrate that's 80% built — not a green-field rewrite.

## Gating (from STATUS.md)

"Ship FP-006 fully first." FP-006 (web GUI) is shipped and was just
exercised end-to-end in Chrome (every button, the full audit→propose flow).
The practical gate — "the web app works for a full iteration cycle on real
decks without touching a CLI" — is **met**. So FP-007 is unblocked to *start*,
incrementally.

## Slices (each independently shippable, behind the existing app)

1. **Card reference panel (first slice — scoped below).** A `/card/<name>`
   view + a search box in the topbar: oracle text, type line, mana cost,
   legality, price, printings — all from `oracle_store` / `scryfall_client`
   (no new datastore). This is the biggest missing leg and the cleanest to
   add to the existing Flask shell.
2. **Unified nav shell.** Left-rail sections: Decks (current) / Cards (slice 1)
   / Rules. Keep the deck dashboard as the Decks section.
3. **Rules / combo lookup.** Surface `combo_detection` + bracket rules as a
   browsable reference (what combos exist for a color identity, what pushes a
   bracket) rather than only inline in the audit.
4. **Library view.** Cross-deck search over the `.dck` set + knowledge_log
   history (which decks run a card, verdict history, price trend).
5. **Replays (last, gated on `forge_py`).** Turn-by-turn game review — only
   meaningful once `forge_py` produces inspectable game state; parked with FP-001.

## First slice — Card reference panel (concrete, ~1–2 sessions)

- **Backend:** `GET /api/card/<name>` in a new `web/routes_cards.py` blueprint:
  returns `{name, type_line, mana_cost, oracle_text, color_identity,
  legalities, prices, printings, image_url}` from `oracle_store.card_reference`
  + `scryfall_client.lookup_card` (cache-first; `cache=False` refetch on miss).
  Degrades to a clean 404 on unknown card.
- **Frontend:** a topbar "Cards" search input → `/card/<name>` overlay reusing
  the existing card-image overlay + a details pane. No framework change (same
  vanilla `el()` helpers).
- **Tests:** route returns shape on a stubbed lookup; 404 on miss; search
  input wired (verified in Chrome like the other buttons).

## Risks / notes

- Don't fork state: the unified app must keep using the same `deck_dir`,
  `knowledge_log`, and `mtg_cards/` cache — no parallel datastores.
- Keep each slice behind the working app so `feature` + CI stay green; this
  doc + slice-1 tests are the contract.
- FP-013 (project-tuned LLM) and replays remain parked; FP-007 does not
  depend on them.

## Status

Plan committed; **slice 1 (card reference) is the next concrete build.**
Tracked as ACTIVE in STATUS.md.
