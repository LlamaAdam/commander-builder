# FP-010 — Desktop EXE (status + how to build)

**Decision (2026-05-26):** package the web app as a double-click desktop EXE.
~16h total; this is the first pass — a working launcher + freeze pipeline +
tests. Gate ("web app proven via browser for a full cycle") is met (verified
in Chrome this session).

## What shipped

- **`commander_builder/desktop.py`** — runs `web.app.create_app` on a daemon
  thread and shows it in a native window via **pywebview** at
  `http://127.0.0.1:<free-port>/`. One process, no browser, no manual server.
  Injectable `webview` / `serve` hooks make the wiring unit-testable
  (`tests/test_desktop.py`, 6 tests). Entry point: `commander-builder-desktop`.
- **`packaging/commander-builder.spec`** + **`packaging/desktop_entry.py`** —
  PyInstaller one-folder freeze; bundles the Flask `templates/` + `static/`
  as data files (so `create_app()` finds them inside `_MEIPASS`).
- **`scripts/build_desktop.py`** — installs the `[desktop]` extra and runs the
  freeze. Output: `dist/CommanderBuilder/CommanderBuilder.exe`.
- **pyproject**: `[desktop]` extra (`pywebview`, `pyinstaller`, `flask`).

## Build it

```powershell
python scripts/build_desktop.py          # installs deps + freezes
# -> dist/CommanderBuilder/CommanderBuilder.exe
```
Run on Windows for a Windows EXE (PyInstaller doesn't cross-compile). First
build is slow (pywebview pulls a native EdgeChromium/pythonnet backend).

## Deliberately external (NOT bundled)

The EXE bundles only the Python app + Flask assets. These stay on disk and the
app locates them like the dev setup:

| Data | Size | Why external |
|------|------|--------------|
| Forge JAR | ~120 MB | huge; updated every set; user already has `vendor/forge/` |
| JRE | ~150 MB | huge; platform-specific |
| `mtg_cards/` (images + oracle) | ~180 MB | huge; grows over time |

When Forge/JRE are absent the app still runs — only the audit/sim calls that
shell out to Forge error per-request (same as a dev box without Forge). Card
images lazy-fetch from Scryfall through the existing cache.

## Remaining slices (the rest of the ~16h)

1. **First-run data bootstrap** — on first launch, detect missing
   `vendor/forge/` + `mtg_cards/` and offer a downloader (Forge release from
   GitHub, JRE, and prime the card cache) instead of silently degrading.
2. **Deck-dir picker** — a first-run prompt / setting for where `.dck` files
   live (today it defaults to the Forge userdata path; a packaged app may want
   `%USERPROFILE%\Documents\CommanderBuilder\decks`).
3. **Icon + window chrome** — app icon, single-instance guard, graceful
   shutdown of the Flask thread on window close.
4. **Installer** — wrap the one-folder dist in an installer (Inno Setup /
   NSIS) or ship a zip; optional code-signing.
5. **CI build job** — a Windows GitHub Actions runner that produces the EXE
   artifact on tag.

## Status

Launcher + freeze pipeline + tests done and on `feature`. Producing the actual
`.exe` is a local `python scripts/build_desktop.py` (deps are heavy); the
first-run downloader (slice 1) is the next meaningful build.
