# FP-002 deck-generation plan — toward a real margin predictor

**Goal (your call, 2026-05-26):** grow the soak deck set from ~13 unique
commanders to **~80+ unique decks**, so the margin regression in
`scripts/margin_analysis.py` has enough rows (the unit of analysis is the
*deck*, not the game) to attempt an out-of-sample predictor on the one
cross-validated signal (`deficit_total` / `under_built_roles`).

## Why this is a campaign, not a step

- 80 decks × **40-game** gauntlet (operator directive: never 5-game) × 2 roles
  (base + v2) ≈ **6,400 games**. At ~40s/game that's ~70h single-runner, or
  **~12–18h** on the autoscaling `soak_pool` (the box1 Ryzen 3900X did ~200
  games/hr in prior soaks). It is a soak you launch deliberately, like the
  prior gauntlet runs — not a single command that returns.
- The acquisition + curation phase (below) is network/Claude-CLI bound and
  unattended-able, but still ~1–2h for 30+ commanders.

## Pipeline

**Phase 1 — acquire base decks (network-bound, safe to run anytime)**
1. Pick ~30 more commanders spanning brackets B3/B4/B5 and color identities
   (diversity matters more than raw count — avoid 30 mono-red goblin decks).
   Source options, in preference order:
   - EDHREC average deck per commander (`edhrec_client.fetch_average_deck`) —
     coherent, no Moxfield dependency.
   - Top-liked Moxfield build (`moxfield_import.find_top_liked_deck_for_commander`)
     — what `scripts/pull_popular_decks.py` already does for existing decks.
2. Write each as a `[USER] <Name> [B<n>].dck` into the shared inbox so
   `soak_pool` discovers it. Keep names unique.

**Phase 2 — curate a v2 per deck (Claude-CLI bound, unattended)**
- `commander-auto-curate <base>.dck` (subscription CLI path; scrubs API keys)
  writes the curated `... v2 ...dck`. `soak_pool` pairs base + ` v2 ` by name.
- Resumable: skip any base that already has a v2.

**Phase 3 — soak (the long Forge run; launch deliberately)**
- Gauntlet mode, 40 games, the unconfounded design margin_analysis prefers:
  ```
  python scripts/soak_pool.py --mode gauntlet --games 40 --append \
      --label Llama --out C:/Users/pilot/soak_inbox/Llama_gauntlet.jsonl
  ```
- **Machine-identity invariant:** box1 is `Llama` — use `--label Llama` and the
  `Llama_*` output; never a box2b label on box1.
- Run in short blocks during dev; bump to 24h when stable (per memory).

**Phase 4 — analyze**
- `python scripts/margin_analysis.py --mode gauntlet --min-games 40`
  now reports over ~80 decks. With n≈80, validate the `deficit_total`
  single-feature OLS out-of-sample (pure stdlib; sklearn still unneeded).

## What to build (small, optional)

A driver `scripts/build_fp002_deckset.py` that automates Phase 1+2 from a
commander list: fetch base deck → write `.dck` → `commander-auto-curate` →
emit progress, resumable on the count of paired decks. ~1 file; the per-step
calls already exist (`fetch_average_deck`, `find_top_liked_deck_for_commander`,
`auto_curate_main`). Left unbuilt pending go-ahead because the commander LIST
is a curation choice (which 30 commanders) better made with you.

## Status / honesty

- Acquisition + curation: ready to run with existing tooling.
- The soak is a multi-hour Forge campaign — best launched when box1 is free
  (you stop soaks when actively editing the program), not unattended mid-dev.
- Caveat from the completed analysis: curation is ~neutral and only one feature
  cross-validated, so even at n=80 the predictor may stay weak. The value is a
  definitive answer, not a guaranteed model.
