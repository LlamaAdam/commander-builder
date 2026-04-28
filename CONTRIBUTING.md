# Session notes — dev setup and conventions

This is a personal project. These are future-self / future-Claude-session
instructions, not "how to contribute" in a public-OSS sense. Follow them to
avoid rediscovering the same constraints when picking the project back up
after a few weeks.

## Setup

```bash
git clone <this repo>
cd commander_builder
python -m pip install -e ".[dev]"

# Optional: vendor Forge + JRE for live sim runs (sims fail gracefully if missing)
# See setup/forge/README.md for the install steps.
```

After `pip install -e .`, every CLI works without `PYTHONPATH=src`:

```
commander-import https://moxfield.com/decks/<id>
commander-snapshot "[USER] My Deck [B3].dck" --version v1
commander-curate --bracket 3 --max-candidates 12 --seed 0
commander-match --user "[USER] My Deck [B3].dck" --bracket 3
commander-compare --old "...v1.dck" --new "...v2.dck" --bracket 3
commander-iterate --old "...v1.dck" --new "...v2.dck" --bracket 3 --manifest manifest.json
```

## Running the test suite

```bash
python -m pytest tests/
```

Should pass in under a second. Suite is offline-only — Scryfall HTTP calls
are monkeypatched in tests, Forge subprocess is exercised by integration
scripts (not unit tests).

## Layout reminders

- **`src/commander_builder/`** — production code. Each file is a single
  module; new functionality usually means a new module rather than a new file
  in an existing one. Keep modules under ~400 lines.
- **`tests/`** — one `test_<module>.py` per production module.
  `conftest.py` puts `src/` on `sys.path` so the suite runs even before
  `pip install -e .`.
- **`scripts/`** — integration tests, batch runners, smoke harnesses. These
  are allowed to hit Forge and Scryfall; unit tests are not.
- **`prompts/`** — LLM workflow prompts, versioned in-repo. New versions land
  as `_v4.md`, `_v5.md` — never overwrite a prior version.
- **`docs/`** — architecture, workflow, decision logs.
- **`vendor/`** — Forge install + JRE. Mostly gitignored except `vendor/README.md`.

## Coding conventions

- **Many small files > few large ones.** Target 200–400 lines per module.
- **Immutable patterns where possible.** Prefer returning new objects to
  mutating in place. (Caller-mutated patterns like `schedule_pods`'s
  `deck_pod_count` are scoped to the function — they don't leak.)
- **Errors handled explicitly.** No silent `except: pass`. If an error means
  "skip this candidate", log and continue. If it means "abort the run",
  raise.
- **Network calls go through a cache.** See `scryfall_client` for the pattern
  — disk cache, slugified filenames, polite sleep between requests.
- **Forge subprocess paths are not unit-tested.** Mock at the boundary
  (e.g. monkeypatch `ForgeRunner.run` to return a canned `SimResult`) or
  exercise via `scripts/`.
- **CLIs use argparse.** Every module that's an entry point exposes
  `def main(argv: Optional[list[str]] = None) -> int:`.
- **Type hints required** on public APIs (anything not prefixed `_`).
  `Optional[X]` over `X | None` for now (project still supports 3.10).

## When you add a module

1. Create `src/commander_builder/<name>.py`. One file, public API at the top
   in a docstring.
2. Create `tests/test_<name>.py` with at least one test per public function.
3. Update `docs/architecture.md` (responsibility table + the layered diagram).
4. Update `BACKLOG.md` if the new module unblocks or supersedes an existing
   gap.
5. Update `CHANGELOG.md` under `[Unreleased]` → `### Added`.
6. If it's a CLI entry point, add it to `pyproject.toml`'s `[project.scripts]`.

## When you fix a bug

1. Write the failing test FIRST. Confirm it fails on current main.
2. Fix the bug. Test should pass.
3. Move the corresponding `GAP-NNN` from `BACKLOG.md` to `CHANGELOG.md`'s
   `[Unreleased] / ### Fixed` with a one-line description.
4. If the fix changed a public contract, update `docs/architecture.md`.

## When you make an architectural decision

Drop a one-page record in `docs/decisions/ADR-NNN-<slug>.md`. Format:

```
# ADR-NNN — <decision title>
**Date**: YYYY-MM-DD
**Status**: Proposed | Accepted | Deprecated | Superseded by ADR-MMM

## Context
<the problem we're solving and the constraints>

## Decision
<what we chose>

## Consequences
<what changes; tradeoffs accepted>

## Alternatives considered
<options ruled out and why>
```

This stays cheap (1 page) but durable. Future-you will not remember why
the option you ruled out was actually a bad idea.

## When you commit

The user has a global git config that disables Co-Authored-By attribution.
Don't add it back. Conventional-commits format:

```
feat: add archetype classifier (heuristic)
fix: log_parser regex order — was leaving [B<n>] suffix
refactor: extract pool_curator main() for entry-point script
docs: update STATUS to reflect Phase 2 completion
test: add integration test for iteration_loop
```

Don't commit unless the user explicitly asks. Keep changes coherent — one
GAP-NNN per commit ideally; never mix bug fixes with refactors.
