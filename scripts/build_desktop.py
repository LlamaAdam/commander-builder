"""Build the Commander Builder desktop EXE (FP-010) via PyInstaller.

Installs the build deps ([desktop] + [web] extras) then freezes the app
against ``packaging/commander-builder.spec``. One-folder output lands in
``dist/CommanderBuilder/`` with ``CommanderBuilder.exe`` inside.

Usage:
  python scripts/build_desktop.py              # install deps + build
  python scripts/build_desktop.py --no-install # build only (deps present)

Notes:
- Run on Windows for a Windows EXE (PyInstaller is not a cross-compiler).
- pywebview pulls a native backend (EdgeChromium via pythonnet on Windows);
  the first install can take a few minutes.
- The resulting app expects Forge/JRE/mtg_cards on disk at the usual
  locations — they are NOT bundled (see docs/fp010-plan.md).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "packaging" / "commander-builder.spec"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="build_desktop")
    ap.add_argument("--no-install", action="store_true",
                    help="skip the pip install of build deps")
    args = ap.parse_args(argv)

    if not SPEC.exists():
        print(f"ERROR: spec not found: {SPEC}")
        return 2

    if not args.no_install:
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "pyinstaller>=6.0", "pywebview>=5.0", "flask>=3.0"],
            check=True,
        )

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)],
        cwd=str(ROOT), check=True,
    )
    out = ROOT / "dist" / "CommanderBuilder"
    print(f"\nBuilt: {out}\nRun:   {out / 'CommanderBuilder.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
