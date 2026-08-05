# Hung-loop rate investigation — closed 2026-08-03

Cross-box investigation (box1 "Llama", 9-runner desktop; box2b, 6-runner
laptop) into why gauntlet soak rows were being cut short by
unattributable combo loops, and whether the two machines differed.
Conducted 2026-07-31 → 2026-08-03 over the two boxes' mailbox exchange;
this file is the permanent record — the raw analyses live in the share's
`loop_watch_*.md` reports and `msgs/_read/` thread.

## The two loop markers (measure with BOTH)

A hung loop is recorded differently depending on writer vintage:

- **pre 2026-07-24**: `status == "done"` with `error` containing
  `"loop at game N credited to active seat None"` — the old writer
  finished the row and mis-noted the loop in the error field.
- **post 2026-07-24**: `status == "loop_unattributed"` — the honest
  writer introduced by `d07409e` ("preserve partial stdout on
  loop-abort; honest unattributed-loop rows", 2026-07-24 00:55 −0500,
  task #56): the batch is cut short, completed games are kept and
  scored, and the row is labeled.

Any rate computed from only one marker is an undercount (box2b's first
pass reported 11.3% counting only the second marker; the true folded
figure was 22.5%). `soak_inbox/tools/loop_watch.py` (share copy) folds
both markers as of 2026-08-03.

## Findings

1. **The post-07-24 "rate jump" is measurement honesty, not a play
   regression.** `d07409e` made previously invisible loops visible.
   Mean games kept on a looped row ≈ 18 of the intended 40 on both
   boxes.
2. **Era split is mandatory.** Pooling across the 07-24 writer change
   produced a wrong "contention refuted" conclusion (issued 03:20,
   retracted 03:36 the same night). Split by era:
   - **July+ (honest writer)**: box1 25.6% of rows (323/1263) vs box2b
     28.2% (31/110), p = 0.57 — **no cross-box gap**.
   - **May (old writer)**: box1 5.4% (7/130) vs box2b 17.2% (31/180),
     p = 0.0027 — a real gap, but attributable only to old-writer
     abort/attribution behavior that can no longer be observed.
   - Denominator convention: the rates above are per-ROW (May box2b
     17.2% = 31/180 rows); the share tool's comparison instruction
     quotes the same loops per-SIM (31/165 = 18.8%). Both boxes are
     folded identically within each convention, so no conclusion
     changes — but do not mix the two denominators in one comparison.
3. **Censoring is benign.** Loop rows are not clustered by deck
   (permutation test, 20k shuffles: p = 0.64 box1, p = 0.91 box2b) and
   are symmetric across base/v2 — dropping every loop row moves
   win rates ≤ 0.6 pp and reorders nothing. Soak-derived verdicts are
   unaffected.

## Conclusion

Under the honest writer the two boxes agree: cross-box contention as a
driver of loop rate is **NOT DETECTED** (deliberately not "refuted" —
the May-era gap remains unexplained and unknowable, and only two
hardware configurations were observed). A planned controlled 6-vs-9
runner experiment was **voided 2026-08-03** (retraction on the share,
consumed by box2): July-era parity at different runner counts already
answers the question with more data than the experiment would have
produced.

## If a hung loop needs investigating later

- `loop_watch.py --jsonl <file> --label <label>` for rates (folds both
  markers).
- Run soaks/searches with `COMMANDER_BUILDER_KEEP_GAME_LOGS=1` — FP-016
  replay capture (bounded 500 MB) makes each hung game individually
  inspectable in the web Replays section.
