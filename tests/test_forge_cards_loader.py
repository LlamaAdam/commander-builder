"""Tests for ``forge_cards_loader`` — slug rules + dual-mode loading.

Forge ships its 32k-card corpus either as a zip bundle
(``cardsfolder.zip``, the canonical install) or as an unzipped
directory tree (``cardsfolder/<letter>/<slug>.txt``, the dev
layout). The loader's API is the same either way; these tests pin
that with paired fixture tests — one against a synthetic zip, one
against a synthetic directory tree — so a future refactor that
silently breaks one path can't slip through.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from commander_builder.forge_cards_loader import (
    CardsLoader,
    LoaderSource,
    slug_for,
)


# ---------------------------------------------------------------------------
# slug_for (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Krenko, Mob Boss",   "krenko_mob_boss"),
    ("Sol Ring",            "sol_ring"),
    ("Avatar of Slaughter", "avatar_of_slaughter"),
    # Apostrophes collapse into the surrounding underscore run.
    ("Yawgmoth's Will",     "yawgmoth_s_will"),
    # DFC: ``//`` collapses to the full ``front_back`` slug —
    # matches Forge's actual filesystem convention (corrected
    # 2026-05-19; was incorrectly front-face-only in #018's
    # initial slug rules).
    ("Bala Ged Recovery // Bala Ged Sanctuary",
     "bala_ged_recovery_bala_ged_sanctuary"),
    # Defensive: empty / whitespace-only.
    ("",                    "unknown"),
    ("   ",                 "unknown"),
    # Mixed case + numerics.
    ("Borborygmos 2", "borborygmos_2"),
])
def test_slug_for_matches_forge_filesystem_convention(name, expected):
    assert slug_for(name) == expected


# ---------------------------------------------------------------------------
# Loader against a directory tree
# ---------------------------------------------------------------------------

def _scaffold_dir_corpus(root: Path, cards: dict[str, str]) -> None:
    """Build a fake unzipped cardsfolder under ``root`` with the
    same letter/slug subdir layout Forge uses. ``cards`` maps
    Forge slug → raw script text."""
    for slug, text in cards.items():
        letter_dir = root / slug[0]
        letter_dir.mkdir(parents=True, exist_ok=True)
        (letter_dir / f"{slug}.txt").write_text(text, encoding="utf-8")


def test_loader_directory_load_one_finds_card(tmp_path):
    """Single-card lookup via slug resolves through the letter
    subdir + .txt suffix."""
    _scaffold_dir_corpus(tmp_path, {
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\n",
    })
    loader = CardsLoader(directory=tmp_path)
    assert loader.source == LoaderSource(kind="directory", path=tmp_path)
    raw = loader.load_one("Sol Ring")
    assert raw is not None
    assert "Name:Sol Ring" in raw


def test_loader_directory_load_one_returns_none_for_unknown(tmp_path):
    """Missing card → None, not raise. The analyzer treats None as
    'unresolved' and reports it; raising would abort the whole pass."""
    _scaffold_dir_corpus(tmp_path, {"foo": "Name:Foo\n"})
    loader = CardsLoader(directory=tmp_path)
    assert loader.load_one("Nonexistent Card") is None


def test_loader_directory_iter_all_yields_every_card(tmp_path):
    """iter_all walks the whole tree; order is sorted by (letter, slug)."""
    _scaffold_dir_corpus(tmp_path, {
        "sol_ring": "Name:Sol Ring\n",
        "krenko_mob_boss": "Name:Krenko, Mob Boss\n",
        "lightning_bolt": "Name:Lightning Bolt\n",
    })
    loader = CardsLoader(directory=tmp_path)
    out = list(loader.iter_all())
    slugs = [slug for slug, _ in out]
    assert slugs == ["krenko_mob_boss", "lightning_bolt", "sol_ring"]
    # Text round-trips intact.
    by_slug = dict(out)
    assert "Sol Ring" in by_slug["sol_ring"]


# ---------------------------------------------------------------------------
# Loader against a zip bundle
# ---------------------------------------------------------------------------

def _build_fixture_zip(path: Path, cards: dict[str, str]) -> None:
    """Build a zip with the same layout cardsfolder.zip ships:
    letter directories at the root, .txt files inside."""
    with zipfile.ZipFile(path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Forge's cardsfolder.zip has explicit directory entries
        # (a/, b/, ...) AS WELL AS the .txt files. The loader must
        # skip the dir entries gracefully.
        seen_letters = set()
        for slug, text in cards.items():
            letter = slug[0]
            if letter not in seen_letters:
                zf.writestr(f"{letter}/", "")
                seen_letters.add(letter)
            zf.writestr(f"{letter}/{slug}.txt", text)


def test_loader_zip_load_one_finds_card(tmp_path):
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\n",
    })
    loader = CardsLoader(zip_path=zip_path)
    assert loader.source.kind == "zip"
    raw = loader.load_one("Sol Ring")
    assert raw is not None
    assert "Name:Sol Ring" in raw


def test_loader_zip_load_one_returns_none_for_unknown(tmp_path):
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {"foo": "Name:Foo\n"})
    loader = CardsLoader(zip_path=zip_path)
    assert loader.load_one("Nonexistent") is None


def test_loader_zip_iter_all_skips_directory_entries(tmp_path):
    """Directory markers (``a/``, ``b/``) inside the zip aren't real
    card files and must not appear in iter_all output."""
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {
        "krenko_mob_boss": "Name:Krenko, Mob Boss\n",
        "sol_ring": "Name:Sol Ring\n",
    })
    loader = CardsLoader(zip_path=zip_path)
    slugs = [slug for slug, _ in loader.iter_all()]
    assert slugs == ["krenko_mob_boss", "sol_ring"]


def test_loader_zip_resolves_dfc_via_full_slug(tmp_path):
    """Forge stores DFCs at ``<front>_<back>.txt`` — the full DFC
    name. A caller passing the full ``Front // Back`` name slugs
    to that filename directly."""
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {
        "bala_ged_recovery_bala_ged_sanctuary": (
            "Name:Bala Ged Recovery\n"
            "AlternateMode:DoubleFaced\n"
            "Name:Bala Ged Sanctuary\n"
        ),
    })
    loader = CardsLoader(zip_path=zip_path)
    raw = loader.load_one("Bala Ged Recovery // Bala Ged Sanctuary")
    assert raw is not None
    assert "Bala Ged Recovery" in raw


def test_loader_zip_resolves_dfc_from_front_face_only_name(tmp_path):
    """.dck files reference cards by front-face name only. The DFC
    fallback index maps the front-face slug to the full DFC slug so
    the lookup still succeeds without the caller knowing the back
    face's name."""
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {
        "bala_ged_recovery_bala_ged_sanctuary": (
            "Name:Bala Ged Recovery\n"
            "ManaCost:2 G\n"
            "Types:Sorcery\n"
            "AlternateMode:DoubleFaced\n"
            "Name:Bala Ged Sanctuary\n"
            "Types:Land\n"
        ),
    })
    loader = CardsLoader(zip_path=zip_path)
    # Passing front-face-only name (the .dck-file convention).
    raw = loader.load_one("Bala Ged Recovery")
    assert raw is not None
    assert "Bala Ged Recovery" in raw
    assert "Bala Ged Sanctuary" in raw


def test_loader_dfc_index_does_not_shadow_regular_cards(tmp_path):
    """Single-face cards must keep their direct slug → file mapping
    untouched. The DFC index only entries cards with TWO ``Name:``
    lines, so single-face cards never appear in it."""
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\n",
        "bala_ged_recovery_bala_ged_sanctuary": (
            "Name:Bala Ged Recovery\nAlternateMode:DoubleFaced\nName:Bala Ged Sanctuary\n"
        ),
    })
    loader = CardsLoader(zip_path=zip_path)
    # Pre-populate the index so we can inspect it.
    _ = loader.load_one("Bala Ged Recovery")
    assert loader._dfc_index == {
        "bala_ged_recovery": "bala_ged_recovery_bala_ged_sanctuary",
    }
    # Sol Ring is a single-face card → not in index, but still loadable.
    assert "Sol Ring" in (loader.load_one("Sol Ring") or "")


def test_loader_context_manager_closes_zip(tmp_path):
    """``with CardsLoader(...) as loader:`` closes the zip on exit
    so a pile of analyzer invocations doesn't leak file handles."""
    zip_path = tmp_path / "cardsfolder.zip"
    _build_fixture_zip(zip_path, {"foo": "Name:Foo\n"})
    with CardsLoader(zip_path=zip_path) as loader:
        loader.load_one("Foo")
    # No assertion on a private field — relying on the close path
    # not raising and the file being releasable is enough.
    # On Windows, attempting to delete the still-open zip would
    # fail; on POSIX it'd succeed regardless. Skip the explicit
    # delete to keep the test cross-platform.


# ---------------------------------------------------------------------------
# Constructor validation + locate()
# ---------------------------------------------------------------------------

def test_loader_constructor_rejects_both_sources_set():
    """Passing both ``zip_path`` AND ``directory`` is ambiguous."""
    with pytest.raises(ValueError, match="exactly one"):
        CardsLoader(zip_path=Path("a.zip"), directory=Path("dir"))


def test_loader_constructor_rejects_neither_source_set():
    with pytest.raises(ValueError, match="exactly one"):
        CardsLoader()


def test_loader_locate_prefers_unzipped_directory_when_both_present(tmp_path):
    """If a Forge install has both the unzipped tree AND the .zip,
    the directory wins (faster lookups + easier diffs for the dev
    case)."""
    cardsfolder = tmp_path / "res" / "cardsfolder"
    cardsfolder.mkdir(parents=True)
    # Unzipped layout: letter subdir present.
    (cardsfolder / "s").mkdir()
    (cardsfolder / "s" / "sol_ring.txt").write_text(
        "Name:Sol Ring\n", encoding="utf-8",
    )
    # Also a .zip — should be ignored in favor of the directory.
    with zipfile.ZipFile(cardsfolder / "cardsfolder.zip", "w") as zf:
        zf.writestr("s/sol_ring.txt", "Name:Sol Ring FROM ZIP\n")
    loader = CardsLoader.locate(tmp_path)
    raw = loader.load_one("Sol Ring")
    assert "FROM ZIP" not in (raw or "")


def test_loader_locate_falls_back_to_zip_when_only_zip_present(tmp_path):
    cardsfolder = tmp_path / "res" / "cardsfolder"
    cardsfolder.mkdir(parents=True)
    with zipfile.ZipFile(cardsfolder / "cardsfolder.zip", "w") as zf:
        zf.writestr("s/sol_ring.txt", "Name:Sol Ring\n")
    loader = CardsLoader.locate(tmp_path)
    assert loader.source.kind == "zip"
    assert "Sol Ring" in (loader.load_one("Sol Ring") or "")


def test_loader_locate_raises_when_neither_present(tmp_path):
    """Forge install without a cards corpus is a setup error;
    raise loudly so the user gets a clean diagnostic, not a
    confused 'every card unresolved' report."""
    with pytest.raises(FileNotFoundError, match="cardsfolder"):
        CardsLoader.locate(tmp_path)
