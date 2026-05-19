"""Load Forge card scripts from disk or from the shipped ZIP bundle.

Forge ships its card-script corpus in one of two layouts depending
on the install:

  vendor/forge/res/cardsfolder.zip          (canonical: ~32k entries)
  vendor/forge/res/cardsfolder/<letter>/<slug>.txt
                                            (unzipped dev tree)

This module hides that fork behind one interface: ``CardsLoader``.
Callers get ``load_one(name)`` for single-card lookups (e.g. the
deck-library analyzer that resolves each card-name in a .dck file
to its script) and ``iter_all()`` for bulk passes (e.g. building
an effect-kind histogram across the entire corpus).

Slug rules mirror Forge's filesystem convention:
  - Lowercase
  - Letters / digits / underscores only; everything else collapses
    to ``_``
  - First letter of the slug determines the subdirectory
  - DFC slugs use the FRONT-face name only (Forge stores both faces
    in the same file)

The slug computed here matches both ``scryfall_client._cache_path``
and the actual filenames inside ``cardsfolder.zip`` — verified
against ``k/krenko_mob_boss.txt``, ``s/sol_ring.txt``, etc.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


def slug_for(name: str) -> str:
    """Forge-compatible filesystem slug for a card name.

    ``Krenko, Mob Boss`` → ``krenko_mob_boss``.
    ``Avatar of Slaughter`` → ``avatar_of_slaughter``.
    Double-faced names with ``//`` collapse to the front face's slug
    (Forge's convention is to store the whole DFC under the front-
    face filename).
    """
    if not name:
        return "unknown"
    # DFC: front face only (substring before the //).
    if "//" in name:
        name = name.split("//", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "unknown"


def _zip_member_for(slug: str) -> str:
    """Path inside ``cardsfolder.zip`` for a given slug.

    Zip uses forward slashes regardless of host OS; first character
    of the slug picks the subdirectory.
    """
    first = slug[0] if slug else "_"
    return f"{first}/{slug}.txt"


@dataclass
class LoaderSource:
    """Where the loader is reading from. Useful for log lines /
    debug output / 'why isn't my card found?' diagnostics."""
    kind: str  # "zip" | "directory"
    path: Path


class CardsLoader:
    """Read Forge card-script files from a directory tree OR a
    ``cardsfolder.zip`` bundle. Pick whichever your install has —
    the API is identical.

    ``CardsLoader.locate(forge_dir)`` is the convenience constructor
    that probes the standard Forge layout; otherwise pass the
    explicit ``zip_path`` / ``directory`` to the constructor.
    """

    def __init__(
        self,
        *,
        zip_path: Optional[Path] = None,
        directory: Optional[Path] = None,
    ) -> None:
        if (zip_path is None) == (directory is None):
            raise ValueError(
                "exactly one of zip_path / directory must be set"
            )
        self._zip_path = zip_path
        self._directory = directory
        self._zip_handle: Optional[zipfile.ZipFile] = None

    @classmethod
    def locate(cls, forge_dir: Path) -> "CardsLoader":
        """Find the cards corpus under ``forge_dir/res/cardsfolder``.

        Prefers the unzipped directory if present (faster lookups,
        easier diffs); falls back to the .zip bundle Forge ships.
        Raises FileNotFoundError if neither exists.
        """
        unzipped = forge_dir / "res" / "cardsfolder"
        zip_path = forge_dir / "res" / "cardsfolder" / "cardsfolder.zip"
        # Unzipped layout: directory contains lettered subdirs (a/, b/, ...)
        # rather than just the zip file.
        if unzipped.is_dir():
            entries = [p for p in unzipped.iterdir() if p.is_dir()]
            if entries:
                return cls(directory=unzipped)
        if zip_path.is_file():
            return cls(zip_path=zip_path)
        raise FileNotFoundError(
            f"no Forge cards corpus under {forge_dir / 'res' / 'cardsfolder'} "
            "(checked for unzipped letter subdirs and cardsfolder.zip)"
        )

    @property
    def source(self) -> LoaderSource:
        if self._zip_path is not None:
            return LoaderSource(kind="zip", path=self._zip_path)
        assert self._directory is not None
        return LoaderSource(kind="directory", path=self._directory)

    def _ensure_zip(self) -> zipfile.ZipFile:
        if self._zip_handle is None:
            assert self._zip_path is not None
            self._zip_handle = zipfile.ZipFile(self._zip_path, mode="r")
        return self._zip_handle

    def load_one(self, name: str) -> Optional[str]:
        """Return the raw script text for ``name``, or None when not
        present. Lookup is by Forge slug; DFC names round-trip via
        the front-face slug.
        """
        slug = slug_for(name)
        if self._directory is not None:
            path = self._directory / slug[0] / f"{slug}.txt"
            if not path.is_file():
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        # Zip path.
        member = _zip_member_for(slug)
        zf = self._ensure_zip()
        try:
            return zf.read(member).decode("utf-8", errors="replace")
        except KeyError:
            return None

    def iter_all(self) -> Iterator[tuple[str, str]]:
        """Yield ``(slug, raw_text)`` for every card script. Order is
        loader-dependent (alphabetical-ish) — callers that need
        deterministic order should sort.

        Skips directories / non-.txt entries inside the zip so the
        ``a/`` / ``b/`` / etc. directory markers don't confuse the
        downstream parser.
        """
        if self._directory is not None:
            for letter_dir in sorted(self._directory.iterdir()):
                if not letter_dir.is_dir():
                    continue
                for path in sorted(letter_dir.glob("*.txt")):
                    slug = path.stem
                    yield slug, path.read_text(encoding="utf-8", errors="replace")
            return
        # Zip path.
        zf = self._ensure_zip()
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not info.filename.endswith(".txt"):
                continue
            # Slug is the basename without extension.
            slug = Path(info.filename).stem
            yield slug, zf.read(info.filename).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._zip_handle is not None:
            self._zip_handle.close()
            self._zip_handle = None

    def __enter__(self) -> "CardsLoader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
