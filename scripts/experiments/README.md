# scripts/experiments

Retained **one-off / historical** scripts: feasibility spikes, phase-specific
drivers, smoke tests, and verification probes that were written for a single
investigation and are **not part of the active workflow**.

They are kept (not deleted) for provenance and occasional reuse. Nothing in
`src/`, `tests/`, CI (`.github/workflows/`), pre-commit, or packaging depends on
them. The active, referenced tooling lives one level up in `scripts/`.

Note: these scripts resolve the repo root relative to their own location; if you
move one back to `scripts/`, adjust its `Path(__file__).resolve().parents[...]`
depth accordingly.
