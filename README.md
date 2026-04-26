# Commander Builder

Closed-loop MTG Commander deck improvement: Forge headless simulation + LLM analyst that judges what changes actually helped, plus a knowledge log so future iterations get smarter.

See [PROJECT.md](PROJECT.md) for the full design, phase plan, and working principles. Read it first before any session.

## Status

**Phase 1A — Forge verifier: ✅ complete (2026-04-26).** Java + Forge install unattended, both 2-player constructed and 4-player commander sims run end-to-end and produce parseable match results. See `verify_output/` for captured logs and `setup/forge/README.md` for the install recipe.

**Next: Phase 1B — Forge orchestrator pipeline** (Moxfield pull/convert/push, log parser).

## Setup

Requires Python 3.12+. Forge needs JRE 17+ to run.

**Recommended (self-contained):** drop a portable Forge release and JRE into `vendor/`. See [`vendor/README.md`](vendor/README.md) for exact paths and download links. The verifier checks `vendor/` first and falls back to system installs if it's empty.

**Alternative (system-wide):** install Java and Forge with their normal installers. Make sure `java` is on PATH.

After Forge is set up, **launch it once interactively** so it creates its userdata directory and the bundled sample decks become visible.

## Phase 1A: run the verifier

From `C:\dev\commander_builder`:

```bash
python src/commander_builder/verify_forge.py
```

The script:

1. Locates Java (preferring `vendor/jre/`, then PATH).
2. Locates Forge (preferring `vendor/forge/`, then standard install paths).
3. Finds the userdata `decks/` directory and lists existing decks.
4. Runs a 3-game 2-player constructed sim.
5. Runs a 3-game 4-player commander sim if 4+ commander decks exist.
6. Writes everything (stdout, stderr, Forge's log, structured findings) to `verify_output/`.

What you do next: paste back `verify_output/findings.json` and the `*_stdout.txt` files. We'll use the actual output to design the Phase 1B log parser.

## What's NOT here yet

- Moxfield pull / convert / push
- The `sim` log parser
- Opponent meta selection
- The LLM analyst loop
- Anything ML

All of those are downstream phases — see `PROJECT.md`.
