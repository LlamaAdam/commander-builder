# Session state — 2026-04-28

> Single-page snapshot of what's done / what's open in the FP-006
> web-app build-out. Read this before resuming so context starts
> minimal.

## Pipeline status: end-to-end working

Propose-swap flow (Hakbal example) returns real games:
`old_wins: 4/10, new_wins: 6/10, winner: new (margin 2)`. Five
coordinated fixes were needed — all landed:

1. `b2afbfc` — balance adds == cuts (legal 99 main)
2. `be27bfc` + `b225fc7` — resolve added cards' set/cn (cache-bypass
   when projected snapshots strip them)
3. `30ba475` — distinct metadata `Name=` per staged deck
4. `54aa6e9` — `kept_count` sums quantities, not lines
5. `83cdf26` — stage 1v1 decks under `userdata/decks/constructed/`
   (the actual blocker — Forge `-f constructed` ONLY looks there)
6. `4a5ec46` — thread `deck_dir` through `compare()` to match (5)
7. `8390ebe` — pod is the new propose-swap default (better commander
   signal); 1v1 stays as the fast option via UI radio
8. `fa9d490` — cache-bust static assets (`?v=<token>` per process)
9. `d8452f4` — DFC commander slug fix + EDHREC 404 graceful path

## What's working in Chrome

- Sidebar filters to `[USER]`-only decks (transient `_proposed_*` /
  `_converted_*` files hidden)
- Click deck → all 7 dashboard panels render
- Commander hero + color pips + 5-action row (Propose / Run audit /
  Edit / Copy to Moxfield / Delete)
- Legality banner: legal/illegal pill, GC count pill, Moxfield link
  + Verify-vs-Moxfield button (or Attach button if no source)
- Bracket override dropdown defaults to filename `[B?]`, override
  works through `/api/dashboard?bracket=N`
- Run audit: produces full proposed deck (set/cn resolved) + diff
  + diagnosis + "Use this list" → drops into Propose modal
- Propose modal: Mode radio (Pod / 1v1) + Games radio (5/10/20),
  Run A/B sim returns real wins/losses
- Game Changers / Illegal Cards modals
- Add-deck modal: Moxfield URL tab + Paste tab
- Edit deck (save-only mode), Delete (confirm)

## What's still un-verified (per the eval retrospective)

From `docs/evaluation_retrospective_2026-04-28.md`:
- Delete button against a throwaway deck (destructive — never
  tested)
- Verify-vs-Moxfield in the *drift-detected* path (only the
  in-sync path was tested)
- Browser-console error scan during all of the above
- Bracket override dropdown live-click (no select API in
  browser-use; verified via curl only)

## Latest improvements (this commit)

- **GC count excludes universal staples.** `_count_game_changers`
  filters Sol Ring / Arcane Signet / Command Tower out of the
  Wizards' GC list before counting. The bracket-tile sub-line and
  the legality-banner GC pill both use the same filtered count, so
  vanilla bracket-3 decks no longer show "3 game changers" from
  baseline ramp/fixing.
- **Deck-size warning.** `legality.deck_size_ok` flips to `false`
  when `total_main + commanders != 100`. The legality banner
  surfaces a red pill: `Deck is 91/100 — needs 9 more`. Catches
  the user's BlackPanther / Goblin / Hakbal source decks that ship
  short of legal Commander size.

## Active failure modes (low priority)

- **Audits produce sub-100 proposed decks** when source is short.
  The balance rule preserves any input deficit (97 main + 1 cmdr =
  98 → -2 +2 = 98 still). Banner now warns; padding is deferred.
- **EDHREC slugs for newly-released commanders** can still 404
  silently — graceful degradation (returns []), but no UI cue
  beyond an empty audit list.
- **No client-side optimistic UI** — every state change requires
  a full dashboard re-fetch.

## File map for the web app (under
`src/commander_builder/web/`)

```
app.py        — Flask routes (~1500 lines). All endpoints + helpers
                (_apply_swaps_to_dck, _to_constructed_format,
                _format_added_line, _list_decks).
templates/index.html  — single-page layout, three modals.
static/app.css        — dark editorial theme + modal styles.
static/app.js         — DOM rendering, event wiring, fetch calls.
```

## Test counts

- Top-level + game_advisor (mtga_draft_helper): 196 + 122
- forge_py: 360
- commander_builder: 559

All green. Run `pytest tests/` per repo (don't mix the two
mtga_draft_helper subtrees in one invocation — the documented
`config.py` collision still applies).

## Outstanding TODOs visible in code

`grep -rn TODO src/` returns:
- `forge_py/triggers.py` — P10 second-pass dies/attacks already
  shipped; older TODO comment is stale
- otherwise clean

## Next session priorities (rank-ordered)

1. Test Delete + Verify-drift via throwaway deck.
2. Optimistic UI on Edit deck / Attach Moxfield URL (avoid full
   reloads).
3. Pad sub-100 source decks with basic lands matching color
   identity, so `propose-swap` can run on Goblin (71-card source)
   without manual editing.
4. Surface JS console errors via a `/api/log_error` collector so
   the user can paste me an error code instead of describing it.
