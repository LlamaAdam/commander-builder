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

Companion `test_*.py` files here belong to the script they sit next to. They
are NOT collected by the main suite (pytest `testpaths = ["tests"]`); run them
directly with `pytest scripts/experiments/test_<name>.py` if you revive a
script.

`ml_dataset.py` is a special case: it is the executable record of the FP-002
Phase-3 25-feature schema (FP-002 closed-refuted 2026-07-30), moved here from
`src/commander_builder/` when the package dropped it.
