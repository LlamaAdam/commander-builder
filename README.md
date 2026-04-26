# Commander Builder

Closed-loop MTG Commander deck improvement: Forge headless simulation + LLM analyst that judges what changes actually helped, plus a knowledge log so future iterations get smarter.

See [PROJECT.md](PROJECT.md) for the full design, phase plan, and working principles. Read it first before any session.

## Status

**Phase 1A — Forge verifier.** Goal is to confirm Forge headless runs on this machine and surface what its output looks like. Nothing else is implemented yet.

## Phase 1A: run the verifier

Requires Python 3.12+ and Java (JRE 8+). Forge must be installed.

From the project root:

```bash
python -m src.commander_builder.verify_forge
```

Or from `src/commander_builder/`:

```bash
python verify_forge.py
```

The script:

1. Locates Java, Forge install, the Forge jar, and the userdata `decks/` directory.
2. Lists existing constructed and commander decks.
3. Runs a 3-game 2-player constructed sim.
4. Runs a 3-game 4-player commander sim if 4+ commander decks exist.
5. Writes everything (stdout, stderr, Forge's log, structured findings) to `verify_output/`.

What you do next: paste back `verify_output/findings.json` and the `*_stdout.txt` files. We'll use the actual output to design the Phase 1B log parser.

## What's NOT here yet

- Moxfield pull / convert / push
- The `sim` log parser
- Opponent meta selection
- The LLM analyst loop
- Anything ML

All of those are downstream phases — see `PROJECT.md`.
