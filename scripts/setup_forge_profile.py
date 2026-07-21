"""Create a second cwd-isolated Forge profile for concurrent sims (FP-003).

Forge keys everything off the install-dir cwd: it reads decks from
``<cwd>/userdata/decks/commander/`` and writes ``<cwd>/userdata/forge.log``.
Two sims in the same dir collide. This script materializes a sibling profile
(default ``vendor/forge2``) that:

  * junctions ``res/`` to the source profile (shares ~300MB read-only game
    data — no duplication),
  * copies the small loose startup files (forge.profile.properties etc.),
  * gives the profile its OWN ``userdata/`` (own cache + own forge.log) but
    junctions ``userdata/decks`` back to the source so both profiles always
    see the same .dck files (campaign-written decks stay in sync),

so a ForgeRunner pointed at the new dir runs fully isolated except for the
shared, safe-to-share read-only bits.

``vendor/`` is gitignored, so this profile isn't committed — re-run this
script on any machine to recreate it. Windows-only (uses ``mklink /J``).

Usage:
  python scripts/setup_forge_profile.py                 # vendor/forge -> vendor/forge2
  python scripts/setup_forge_profile.py <src> <dst>
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Small loose files Forge reads from cwd at startup. Best-effort copy.
_LOOSE_FILES = ("forge.profile.properties", ".installationinformation", "build.txt")


def _junction(link: Path, target: Path) -> None:
    """Create a directory junction link -> target (Windows, no admin needed)."""
    if link.exists():
        return
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
        # text=True alone decodes with the locale codec (cp1252) in STRICT
        # mode; a localized cmd.exe message or a non-cp1252 byte in the
        # (user-controlled) link/target paths echoed back would raise
        # UnicodeDecodeError here instead of surfacing the mklink error.
        # UTF-8+replace matches every other launch site in the project.
        encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"mklink failed for {link} -> {target}: {res.stderr.strip()}")


def setup_profile(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise SystemExit(f"source profile not found: {src}")
    if not (src / "res").is_dir():
        raise SystemExit(f"source has no res/ dir: {src}")
    if dst.exists():
        print(f"profile already exists: {dst} (leaving as-is)")
        return

    dst.mkdir(parents=True)
    _junction(dst / "res", src / "res")  # share read-only game data
    for name in _LOOSE_FILES:
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)
    (dst / "userdata").mkdir()
    (dst / "userdata" / "cache").mkdir()
    _junction(dst / "userdata" / "decks", src / "userdata" / "decks")  # shared decks
    print(f"created isolated Forge profile: {dst}")
    print("  res/           -> junction to source (shared, read-only)")
    print("  userdata/decks -> junction to source (shared .dck files)")
    print("  userdata/cache + forge.log: profile-local (isolated)")


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "vendor" / "forge"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else REPO / "vendor" / "forge2"
    setup_profile(src, dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
