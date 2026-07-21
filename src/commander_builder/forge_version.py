"""Forge jar version detection — extracted verbatim from forge_runner.py
on 2026-06-12 so forge_runner keeps its "spawn one Forge sim" charter.

Canonical import path for downstream code remains
``commander_builder.forge_runner`` (which re-exports every name here).
This module imports forge_runner LAZILY (inside the function that needs
``VENDOR_FORGE`` / ``_utcnow``), never at module level — so it is safe to
import in any order and never trips the circular re-export.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Days after which the bundled Forge jar is considered stale and worth
# replacing. New MTG sets ship roughly every 4-6 weeks; 90 days gives
# enough headroom that most installs don't bounce in and out of the
# warning, but not so much that errata-sensitive cards (Sephiroth, Vivi)
# silently misbehave.
FORGE_STALE_AGE_DAYS = 90

_FORGE_JAR_VERSION_RE = re.compile(
    r"forge-gui-desktop-(\d+(?:\.\d+)+)",
)


@dataclass
class ForgeVersionInfo:
    """Snapshot of the bundled Forge jar — version, build date, age.

    ``is_stale`` is conservative: True only when ``age_days`` is known
    AND exceeds ``FORGE_STALE_AGE_DAYS``. Missing build.txt or
    malformed timestamps leave ``is_stale=False`` so we don't alarm
    the user about unknowable state.
    """
    jar_path: Optional[Path] = None
    version: Optional[str] = None
    build_date: Optional[datetime] = None
    age_days: Optional[int] = None
    is_stale: bool = False


def detect_forge_version(forge_dir: Optional[Path] = None) -> ForgeVersionInfo:
    """Inspect the vendor/forge directory and return version metadata.

    ``forge_dir`` defaults to ``forge_runner.VENDOR_FORGE`` (resolved
    lazily so this module never imports forge_runner at load time — see
    the module docstring on the circular re-export).

    Looks for ``forge-gui-desktop-*.jar`` and parses the version out of
    the filename (the only place the bundle reliably exposes it). Reads
    the optional ``build.txt`` for a real build timestamp; falls back
    to ``age_days=None`` when build.txt is missing or malformed.

    Always returns a ForgeVersionInfo — never raises. A missing jar
    surfaces as ``version=None, jar_path=None`` so callers can render a
    "Forge install not found" warning without try/except boilerplate.
    """
    # Late-bound through forge_runner so tests that pin "now" via
    # monkeypatch.setattr("commander_builder.forge_runner._utcnow", ...)
    # keep working after the 2026-06-12 module split. VENDOR_FORGE is
    # resolved here (not as a default arg) for the same reason.
    from .forge_runner import VENDOR_FORGE, _utcnow

    if forge_dir is None:
        forge_dir = VENDOR_FORGE

    info = ForgeVersionInfo()
    if not forge_dir.exists() or not forge_dir.is_dir():
        return info

    # Rank candidates by parsed version (semver-ish) — lexicographic
    # sort would put "2.0.10" before "2.0.12" because "0" < "2" at the
    # relevant position, so the prior sorted(...)[0] picked the OLDER
    # jar when a user kept both around after an upgrade.
    # Fat jars ("jar-with-dependencies") win over thin within the same
    # version because forge_runner.locate() runs the fat one.
    candidates: list[tuple[tuple[int, ...], bool, Path]] = []
    for jar_path in forge_dir.glob("forge-gui-desktop-*.jar"):
        m = _FORGE_JAR_VERSION_RE.search(jar_path.name)
        if not m:
            continue
        try:
            version_tuple = tuple(int(part) for part in m.group(1).split("."))
        except ValueError:
            continue
        is_fat = "jar-with-dependencies" in jar_path.name
        candidates.append((version_tuple, is_fat, jar_path))
    if not candidates:
        return info
    # Highest version first; within a version, fat jar first.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    _, _, jar = candidates[0]
    info.jar_path = jar
    m = _FORGE_JAR_VERSION_RE.search(jar.name)
    if m:
        info.version = m.group(1)

    build_txt = forge_dir / "build.txt"
    if build_txt.exists():
        try:
            text = build_txt.read_text(encoding="utf-8").strip()
            # Forge bundles a "YYYY-MM-DD HH:MM:SS" timestamp.
            info.build_date = datetime.strptime(
                text, "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)
        except (OSError, ValueError):
            info.build_date = None

    if info.build_date is not None:
        delta = _utcnow() - info.build_date
        info.age_days = max(0, int(delta.total_seconds() // 86400))
        info.is_stale = info.age_days > FORGE_STALE_AGE_DAYS

    return info
