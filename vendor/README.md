# vendor/

Drop your portable Forge install and (optionally) a portable Java runtime here. The verifier (and downstream pipeline) checks `vendor/` **before** falling back to system-wide installs, so this is the recommended way to run Commander Builder — no system PATH changes needed and the project stays self-contained.

Everything inside this directory except this README is gitignored. **Do not commit binaries.**

## Layout the verifier expects

```
vendor/
├── README.md              # this file (committed)
├── jre/                   # OPTIONAL portable JRE (gitignored)
│   └── bin/
│       └── java.exe       # ← verifier looks here first for Java
├── forge/                 # Forge install (gitignored)
│   └── forge-gui-desktop-X.Y.Z/   # OR Forge's contents directly under forge/
│       ├── forge-gui-desktop-X.Y.Z-jar-with-dependencies.jar
│       ├── res/           # required — contains card data
│       └── ...
```

The verifier looks for a `forge-gui-*.jar` under `vendor/forge/` or any single subdirectory of it, so both these layouts work:

```
vendor/forge/forge-gui-desktop-1.6.62/         # extracted release directory
vendor/forge/forge-gui-desktop-1.6.62-jar-with-dependencies.jar  # plus res/, etc.
```

## How to populate

### Forge (required)

1. Go to [Card-Forge releases on GitHub](https://github.com/Card-Forge/forge/releases) and download the latest portable archive (typically a `.tar.bz2` or `.zip` named like `forge-gui-desktop-X.Y.Z.tar.bz2`).
2. Extract it.
3. Move the extracted directory (or its contents) into `vendor/forge/`.
4. **Launch Forge once interactively** so it creates its userdata directory and the bundled sample decks become visible. Then close it.

### Java (optional but recommended)

If you'd rather not put Java on system PATH, drop a portable JRE here:

1. Download a portable Temurin JRE 21 zip from [adoptium.net](https://adoptium.net/temurin/releases/?version=21&os=windows&arch=x64) — pick the `.zip` (not `.msi`).
2. Extract it.
3. Move the contents (or rename the extracted folder) so that `vendor/jre/bin/java.exe` exists.

If you skip this step, the verifier falls back to whatever `java` is on PATH.

## Why this layout

- **Self-contained:** `git clone` + drop these two folders + `python verify_forge.py` works on any Windows machine.
- **Versioned:** the exact Forge version you tested with is the one you ship. No "works on my machine" because Forge updated and broke a card.
- **No PATH pollution:** if you have multiple Java versions for other projects, this one doesn't fight them.
