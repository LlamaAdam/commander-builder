"""snapshot_deck unit tests.

Covers the version-token insertion logic and the file-copy behavior with
overwrite protection.
"""
from pathlib import Path

import pytest

from commander_builder.snapshot_deck import snapshot, versioned_path


# --- versioned_path --------------------------------------------------------

def test_versioned_path_inserts_before_user_bracket(tmp_path):
    out = versioned_path("[USER] Hakbal of the Surging Soul [B3].dck", "v1", base=tmp_path)
    assert out == tmp_path / "[USER] Hakbal of the Surging Soul v1 [B3].dck"


def test_versioned_path_inserts_before_plain_bracket(tmp_path):
    out = versioned_path("Allies [B3].dck", "v2", base=tmp_path)
    assert out == tmp_path / "Allies v2 [B3].dck"


def test_versioned_path_handles_unknown_bracket(tmp_path):
    out = versioned_path("Foo [B?].dck", "v1", base=tmp_path)
    assert out == tmp_path / "Foo v1 [B?].dck"


def test_versioned_path_falls_back_when_no_bracket(tmp_path):
    # Decks without [B<n>] still get a versioned name.
    out = versioned_path("RawDeck.dck", "v1", base=tmp_path)
    assert out == tmp_path / "RawDeck v1.dck"


def test_versioned_path_handles_arbitrary_version_token(tmp_path):
    out = versioned_path("Foo [B3].dck", "post_audit", base=tmp_path)
    assert out == tmp_path / "Foo post_audit [B3].dck"


# --- snapshot --------------------------------------------------------------

def test_snapshot_copies_file_to_versioned_path(tmp_path):
    src = tmp_path / "[USER] Foo [B3].dck"
    src.write_text("[Main]\n1 Sol Ring", encoding="utf-8")
    dst = snapshot("[USER] Foo [B3].dck", "v1", base=tmp_path)
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "[Main]\n1 Sol Ring"
    # Source untouched.
    assert src.exists()


def test_snapshot_raises_when_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        snapshot("Missing [B3].dck", "v1", base=tmp_path)


def test_snapshot_raises_when_destination_exists(tmp_path):
    src = tmp_path / "Foo [B3].dck"
    src.write_text("a")
    snapshot("Foo [B3].dck", "v1", base=tmp_path)
    # Re-snapshotting at the same version should refuse to clobber.
    with pytest.raises(FileExistsError):
        snapshot("Foo [B3].dck", "v1", base=tmp_path)


def test_snapshot_overwrite_replaces_existing(tmp_path):
    src = tmp_path / "Foo [B3].dck"
    src.write_text("v1-content")
    snapshot("Foo [B3].dck", "v1", base=tmp_path)
    # Modify source and re-snapshot with overwrite.
    src.write_text("v1-modified")
    dst = snapshot("Foo [B3].dck", "v1", base=tmp_path, overwrite=True)
    assert dst.read_text(encoding="utf-8") == "v1-modified"
