# Audit Workflow — Moxfield audit + Forge validation

This is the end-to-end flow for taking one of your decks, running the Moxfield
audit prompt against it (the LLM proposer), and validating empirically whether
the proposed swaps actually improved the deck (Forge head-to-head).

## The pipeline

```
                ┌──────────────────────────┐
                │  [USER] My Deck [B3].dck │  ← imported from Moxfield
                └──────────┬───────────────┘
                           │ 1. snapshot v1
                           ▼
        [USER] My Deck v1 [B3].dck   (frozen baseline)
                           │
                           │ 2. run Moxfield audit prompt
                           │    in a separate Claude session
                           │    (prompts/moxfield_audit_v3.md)
                           │    → audit modifies Moxfield deck in place
                           │    → audit writes audit_manifest.json
                           ▼
                ┌──────────────────────────┐
                │   Modified Moxfield deck │
                └──────────┬───────────────┘
                           │ 3. moxfield_import re-pulls
                           │    (overwrites local [USER] My Deck [B3].dck)
                           ▼
                ┌──────────────────────────┐
                │  [USER] My Deck [B3].dck │  (post-audit content)
                └──────────┬───────────────┘
                           │ 4. snapshot v2
                           ▼
        [USER] My Deck v2 [B3].dck   (post-audit)
                           │
                           │ 5. compare_versions: head-to-head sim
                           │    [v1, v2, filler1, filler2] × N games × M pods
                           ▼
                ┌──────────────────────────┐
                │  ComparisonReport JSON   │ winner, margin, card_diff, per-version stats
                └──────────────────────────┘
```

## Concrete commands

```bash
cd C:\dev\commander_builder

# 1. Snapshot the pre-audit baseline.
python -m commander_builder.snapshot_deck "[USER] My Deck [B3].dck" --version v1

# 2. Open a NEW Claude session. Paste prompts/moxfield_audit_v3.md.
#    Provide your Moxfield deck URL when prompted.
#    The audit will modify your Moxfield deck and emit an audit_manifest.json
#    documenting added/removed cards.

# 3. Re-pull the post-audit deck. Overwrites the local file.
python -m commander_builder.moxfield_import --user https://moxfield.com/decks/<id>

# 4. Snapshot the post-audit version.
python -m commander_builder.snapshot_deck "[USER] My Deck [B3].dck" --version v2

# 5. Run the head-to-head comparison.
python -m commander_builder.compare_versions \
    --old "[USER] My Deck v1 [B3].dck" \
    --new "[USER] My Deck v2 [B3].dck" \
    --bracket 3 --games 10 --filler-pairs 2
```

The final `ComparisonReport` JSON in `_compare/` answers: **did the audit's
swap actually improve the deck against bracket-matched opposition?**

## Interpreting results

- **`winner: "new"`, `margin >= 4` (out of 20 games)**: clear improvement.
- **`winner: "new"`, `margin <= 2`**: noise-level. Could be variance.
- **`winner: "tie"` or `winner: "old"`**: the audit *didn't* improve win rate
  in the tested matchup. Look at per-version stats:
  - If `new_stats.avg_ending_life` is much higher but win count is similar,
    the new version stabilizes better but doesn't close. Consider bias toward
    finisher cards on the next iteration.
  - If `new_stats.fastest_elimination_turn` is lower (eliminated faster), the
    audit may have cut too much defensive material.
  - If `new_stats.avg_turns_when_won` shifts substantially up or down, the
    deck's win pattern changed — useful signal even if win count didn't move.

## Where Ollama (or another local LLM) could plug in

The audit prompt itself currently runs on Claude — it's a complex multi-step
workflow with web fetches and structured JSON manipulation. But several
**simpler LLM tasks** in the broader pipeline are good candidates for routing
to a local Ollama model to save Claude API tokens:

| Task | Complexity | Frequency | Good fit for local? |
|------|-----------|-----------|---------------------|
| Archetype classification (one-shot, "is this aggro/midrange/control/combo/stax") | Low | Per-deck, occasional | ✅ Strong fit |
| Color identity from commander name | Low | Per-deck, occasional | ✅ Strong fit |
| Card role tagging for sim (regex first, LLM only on ambiguous cases) | Low | Per-card, batched | ✅ Strong fit |
| Card-pair synergy hint ("does X synergize with Y") | Medium | Per-swap | ⚠️ Maybe — quality-sensitive |
| Audit's blind ideal build | High | Per-deck audit | ❌ Stay on Claude |
| Audit's swap rationale generation | Medium-High | Per-deck audit | ❌ Stay on Claude |
| Phase 2 analyst verdict (kept/reverted/neutral with reasoning) | High | Per-iteration | ❌ Stay on Claude |
| Phase 2 proposer (next swap proposal from accumulated lessons) | High | Per-iteration | ❌ Stay on Claude |

**Design space (intentionally left open, not committed):**

When we're ready to integrate Ollama, the natural shape is a thin
`llm_router.py` module exposing one function:

```python
def classify(prompt: str, *, complexity: str = "auto") -> str:
    # complexity: "low" → Ollama, "high" → Claude API, "auto" → router decides
    ...
```

The "low-complexity" routes go to a local model (e.g., `llama3.2:3b` or
similar) over `http://localhost:11434/api/generate`; "high" routes hit the
existing Claude API. Decisions about routing thresholds, prompt format
(structured-output vs free-text), and quality fallbacks are deferred until
there's a concrete cost pressure.

For now, the stub classifier in `pool_curator.py` (`_stub_classifier` returns
`"midrange"` for everything) is the placeholder — replace it with a real
classifier when the pipeline starts producing volume.

## Audit manifest contract

The audit prompt (Step 6 / Closing Summary) writes `audit_manifest.json` to
the audit session's working directory. Schema:

```json
{
  "deck_id": "abc123XYZ",
  "deck_name": "My Deck",
  "bracket": 3,
  "audit_version": "v3",
  "audit_timestamp": "2026-04-26T15:30:00Z",
  "added": ["Card A", "Card B", "..."],
  "removed": ["Card X", "Card Y", "..."],
  "rationale": "One-paragraph summary of strategic intent.",
  "step_4_5_sweep_catches": ["Card Z"],
  "auto_bracket_after": 3,
  "user_bracket": 3
}
```

`compare_versions.py` doesn't currently consume this file — it computes its
own card diff from the .dck files. The manifest is for **provenance** (which
audit produced this swap?) and feeds the eventual Phase 2 knowledge log.

When Phase 2 lands, `iteration_loop.py` will:
1. Read `audit_manifest.json` for the swap intent
2. Run `compare_versions.py` for empirical verdict
3. Cross-reference: did intent match outcome?
4. Persist as a learnable example to the knowledge log

## When to skip the audit's Step 5.6 simulation

Step 5.6 of the audit prompt offers an optional 100-game JS goldfish
simulation. Skip it when running as part of this pipeline — `compare_versions`
runs a real Forge head-to-head simulation that's a stronger empirical signal.
Use Step 5.6 only:
- When Forge isn't available (e.g., remote audit session)
- For very large swaps where pre-execute consistency check (mulligan rate,
  commander-turn) has independent value before committing to a full sim run
