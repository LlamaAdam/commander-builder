# e2e-results/ — run artifacts of the full walkthrough

Everything in this folder except this README is written by
`tests/e2e/full-walkthrough.spec.js` (2026-10-02) and is a **record of one
run**, not source:

- `CHECKLIST.md` — one table per deck (global / A / B), one row per page or
  action with PASS / FAIL / SOFT / SKIP, elapsed ms, notes and a link to
  the screenshot; a totals line at the bottom.
- `checklist.json` — the same rows as data, plus the route inventory the
  spec was written against, the server's deck dir and Forge version.
- `global/`, `A/`, `B/` — one viewport-sized PNG per row (never
  full-page); `A/adopt.json` / `B/adopt.json` hold the adopt CLI output.

The local runner (`scripts/walkthrough_local.cmd` / `.sh`) commits this
folder so the results reach the reviewer by git push. **Delete the run
files after reading** (or let the next run overwrite them) — they are
not meant to accumulate in history. The CI lane (`.github/workflows/
e2e-full.yml`) uploads the same folder as a workflow artifact instead
and prints `CHECKLIST.md` into the job log.
