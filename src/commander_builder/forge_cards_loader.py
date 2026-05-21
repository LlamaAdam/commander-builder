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
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# Apostrophe variants that Forge strips entirely (no underscore
# substitute). Includes ASCII straight + Unicode curly + grave-
# accent backtick that occasionally appears in scraped card names.
_APOSTROPHE_RE = re.compile(r"['‘’‛`]")


def slug_for(name: str) -> str:
    """Forge-compatible filesystem slug for a card name.

    ``Krenko, Mob Boss`` → ``krenko_mob_boss``.
    ``Avatar of Slaughter`` → ``avatar_of_slaughter``.
    ``Aang's Defense`` → ``aangs_defense`` (apostrophes STRIPPED,
    not converted to underscore — Forge's actual convention).
    ``Andúril`` → ``anduril`` (diacritics folded to ASCII).

    Forge's filesystem rules, confirmed against the shipped
    ``cardsfolder.zip`` on 2026-05-19:
      1. Apostrophes (', ', ', `) are removed entirely.
      2. Diacritics get NFKD-stripped → plain ASCII.
      3. Everything else lowercase + non-alnum runs → single
         underscore.

    DFC NAMING (corrected 2026-05-19): Forge stores DFCs under
    the FULL ``front_back`` slug — e.g. Bala Ged Recovery //
    Bala Ged Sanctuary lives at ``b/bala_ged_recovery_bala_ged_
    sanctuary.txt``, NOT under the front-face-only slug.

    Deck files (.dck) typically reference cards by front-face
    name only ("Bala Ged Recovery"). For those, ``slug_for``
    returns the front-face slug — the loader then falls back to
    a DFC index that maps front-face → full-DFC filename when a
    direct lookup misses.
    """
    if not name:
        return "unknown"
    # 1. NFKD normalize + strip combining marks → fold diacritics.
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    # 2. Strip apostrophes entirely (Forge's convention; "Aang's"
    #    becomes "aangs" not "aang_s").
    folded = _APOSTROPHE_RE.sub("", folded)
    # 3. Collapse remaining non-alnum runs to underscore.
    slug = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")
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
        # DFC fallback index. Lazily built on first lookup miss so
        # the loader pays no cost when callers only look up regular
        # cards. Maps ``front_face_slug`` → ``full_dfc_slug``
        # (e.g. ``bala_ged_recovery`` → ``bala_ged_recovery_bala_
        # ged_sanctuary``). Forge's filenames embed the full DFC
        # name but .dck files reference cards by front face only,
        # so this index bridges the two conventions.
        self._dfc_index: Optional[dict[str, str]] = None

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
        present.

        Lookup strategy:
          1. Try the direct slug (works for regular cards + DFCs
             that were passed by full ``Front // Back`` name).
          2. On miss, consult the DFC fallback index — Forge stores
             DFCs under their full slug, so a .dck file's front-
             face-only reference needs a second lookup.

        Returns None when neither attempt finds a script.
        """
        slug = slug_for(name)
        text = self._read_slug(slug)
        if text is not None:
            return text
        # Fallback: maybe ``name`` is a front-face reference to a
        # DFC stored under ``front_back`` slug.
        index = self._get_dfc_index()
        full_slug = index.get(slug)
        if full_slug:
            return self._read_slug(full_slug)
        return None

    def _read_slug(self, slug: str) -> Optional[str]:
        """Read a script blob by exact slug (no DFC fallback). Returns
        None if the slug isn't present in the corpus.

        Tries both the standard ``<first-letter>/<slug>.txt`` path
        AND the ``upcoming/<slug>.txt`` staging directory Forge uses
        for preview cards from sets that haven't released yet. The
        upcoming/ tree gets promoted to the lettered tree on set
        release, so newly-spoiled cards live there transiently.
        """
        candidates = (
            f"{slug[0]}/{slug}.txt",
            f"upcoming/{slug}.txt",
        )
        if self._directory is not None:
            for candidate in candidates:
                path = self._directory / candidate
                if path.is_file():
                    return path.read_text(encoding="utf-8", errors="replace")
            return None
        zf = self._ensure_zip()
        for candidate in candidates:
            try:
                return zf.read(candidate).decode("utf-8", errors="replace")
            except KeyError:
                continue
        return None

    def _get_dfc_index(self) -> dict[str, str]:
        """Build (lazily, once) a map from front-face slug → full
        DFC slug for every two-face card in the corpus.

        Detected by parsing each script's ``Name:`` lines: a single-
        face card has exactly one ``Name:`` line, a DFC has two.
        We only read the first two ``Name:`` lines per file so the
        scan is cheap (no full parse). Production corpus is ~32k
        files; total scan is ~1-2s on a warm zip / SSD.

        Result lives on the instance — subsequent lookups are O(1).
        """
        if self._dfc_index is not None:
            return self._dfc_index
        index: dict[str, str] = {}
        for full_slug, raw in self.iter_all():
            # Cheap scan for two ``Name:`` lines.
            first_name: Optional[str] = None
            for line in raw.splitlines():
                if line.startswith("Name:"):
                    name_value = line[5:].strip()
                    if first_name is None:
                        first_name = name_value
                    else:
                        # Second Name: → this is a DFC. Index the
                        # front face slug → full slug.
                        front_slug = slug_for(first_name)
                        if front_slug != full_slug:
                            index[front_slug] = full_slug
                        break
        self._dfc_index = index
        return index

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
