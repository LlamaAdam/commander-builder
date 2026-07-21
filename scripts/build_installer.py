"""Build the Commander Builder Windows installer (FP-010 slice 4) via Inno Setup.

Locates ISCC.exe (checks PATH first, then standard install paths) and invokes
it against ``packaging/commander-builder.iss``.  Output lands at
``dist/installer/CommanderBuilder-Setup.exe``.

Usage:
  python scripts/build_installer.py
  python scripts/build_installer.py --iscc "C:/path/to/ISCC.exe"   # explicit override

The PyInstaller one-folder dist at ``dist/CommanderBuilder/`` must already
exist before running this script (run ``scripts/build_desktop.py`` first).

Mockable seam
-------------
All subprocess calls go through ``_run_iscc(iscc_exe, iss_file)``.  Tests can
monkeypatch that function to avoid actually invoking ISCC.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "commander-builder.iss"

# Candidate install paths, checked in order when ISCC is not on PATH.
_CANDIDATE_PATHS: list[str] = [
    # Per-user install (winget default on consumer Windows)
    r"C:\Users\{username}\AppData\Local\Programs\Inno Setup 6\ISCC.exe",
    # System-wide installs
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def _expand_candidates() -> list[Path]:
    """Return _CANDIDATE_PATHS with {username} expanded to the current user."""
    import getpass
    username = getpass.getuser()
    return [Path(p.replace("{username}", username)) for p in _CANDIDATE_PATHS]


def find_iscc(override: str | None = None) -> Path:
    """Return the path to ISCC.exe.

    Search order:
    1. *override* (explicit --iscc CLI flag / caller-supplied path).
    2. PATH (``shutil.which``).
    3. Standard install directories (see ``_CANDIDATE_PATHS``).

    Raises ``FileNotFoundError`` with a helpful message if Inno Setup is not found.
    """
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"ISCC.exe not found at the specified path: {override}"
        )

    on_path = shutil.which("ISCC")
    if on_path:
        return Path(on_path)

    for candidate in _expand_candidates():
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "Inno Setup (ISCC.exe) was not found.\n"
        "Install it from https://jrsoftware.org/isinfo.php or via:\n"
        "  winget install --id JRSoftware.InnoSetup -e\n"
        "Then re-run this script."
    )


def _run_iscc(iscc_exe: Path, iss_file: Path) -> None:
    """Invoke ISCC.exe.  Extracted as a separate function for easy mocking in tests."""
    subprocess.run([str(iscc_exe), str(iss_file)], check=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="build_installer",
                                 description="Build the CommanderBuilder Windows installer via Inno Setup.")
    ap.add_argument("--iscc", metavar="PATH", default=None,
                    help="Explicit path to ISCC.exe (optional; auto-detected if omitted).")
    args = ap.parse_args(argv)

    if not ISS.exists():
        print(f"ERROR: Inno Setup script not found: {ISS}", file=sys.stderr)
        return 2

    dist_dir = ROOT / "dist" / "CommanderBuilder"
    if not dist_dir.is_dir():
        print(
            f"ERROR: PyInstaller dist not found at {dist_dir}\n"
            "Run 'python scripts/build_desktop.py' first.",
            file=sys.stderr,
        )
        return 2

    try:
        iscc = find_iscc(args.iscc)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Using ISCC: {iscc}")
    print(f"Building installer from: {ISS}")

    _run_iscc(iscc, ISS)

    out = ROOT / "dist" / "installer" / "CommanderBuilder-Setup.exe"
    if out.exists():
        print(f"\nInstaller: {out}  ({out.stat().st_size:,} bytes)")
    else:
        print(f"\nISCC exited successfully; expected output at {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
