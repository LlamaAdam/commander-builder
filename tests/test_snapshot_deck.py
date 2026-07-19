"""snapshot_deck unit tests.

Covers the version-token insertion logic, the file-copy behavior with
overwrite protection, and the Name=-stamping that keeps win attribution
working (a snapshot whose Name= still points at the source deck scores
0-0 in every compare — see dck_meta).
"""
import re
from pathlib import Path

import pytest

from commander_builder.log_parser import _normalize
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
    text = dst.read_text(encoding="utf-8")
    # Card sections copied through; a Name= is synthesized (the source had
    # no metadata section) so Forge results can attribute to this file.
    assert "[Main]\n1 Sol Ring" in text
    assert "Name=[USER] Foo v1 [B3]" in text
    # Source untouched.
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "[Main]\n1 Sol Ring"


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
    assert "v1-modified" in dst.read_text(encoding="utf-8")
    assert "v1-content" not in dst.read_text(encoding="utf-8")


# --- Name= alignment (regression) ------------------------------------------

def test_snapshot_rewrites_name_to_destination_stem(tmp_path):
    """Regression: snapshot() used to be a plain copy2, so the versioned
    file kept the SOURCE deck's Name= (e.g. 'My Deck'). Forge reports
    Name= in its Match Result lines, but compare_versions keys win
    attribution on the normalized FILENAME ('My Deck v1') — the two never
    matched and every snapshot A/B scored 0-0. The snapshot must stamp
    the destination's own stem into Name= so both sides normalize equal."""
    src = tmp_path / "[USER] My Deck [B3].dck"
    src.write_text(
        "[metadata]\nName=My Deck\nMoxfield=abc123\n"
        "[Commander]\n1 Hakbal of the Surging Soul\n"
        "[Main]\n1 Sol Ring\n",
        encoding="utf-8",
    )
    dst = snapshot("[USER] My Deck [B3].dck", "v1", base=tmp_path)
    text = dst.read_text(encoding="utf-8")

    m = re.search(r"^Name=(.+)$", text, re.MULTILINE)
    assert m, "snapshot output must carry a Name= line"
    # The invariant that makes _aggregate_pod attribution work: the Name=
    # Forge will report and the filename the caller queries by must
    # normalize to the same string.
    assert _normalize(m.group(1)) == _normalize(dst.stem) == "My Deck v1"
    # Everything else passes through: other metadata + card sections.
    assert "Moxfield=abc123" in text
    assert "1 Hakbal of the Surging Soul" in text
    assert "1 Sol Ring" in text
    # Exactly one Name= line (no duplicate synthesized on top).
    assert len(re.findall(r"^Name=", text, re.MULTILINE)) == 1
    # Source keeps its original Name=.
    assert "Name=My Deck\n" in src.read_text(encoding="utf-8")
