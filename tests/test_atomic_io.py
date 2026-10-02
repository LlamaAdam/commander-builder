"""``atomic_io.atomic_write_text`` — the core crash-safe writer (R3 W-08).

R4-FU B-11 (2026-10-02): a symlinked target is written THROUGH, not
replaced by a regular file.
"""
from __future__ import annotations

import os

import pytest

from commander_builder.atomic_io import atomic_write_text


def test_atomic_write_text_replaces_contents_and_leaves_no_temp(tmp_path):
    target = tmp_path / "deck.dck"
    target.write_text("old\r\n", encoding="utf-8")
    atomic_write_text(target, "new\r\n")
    assert target.read_bytes() == b"new\r\n"          # byte-for-byte, no newline translation
    assert [p.name for p in tmp_path.iterdir()] == ["deck.dck"]


def test_atomic_write_text_writes_through_a_symlink(tmp_path):
    """R4-FU B-11: ``os.replace(tmp, link)`` used to swap the LINK for a
    regular file, so a dotfiles-managed ``config.json`` silently stopped
    being the dotfiles copy on the first save. The real path is resolved
    first: the link survives and the file it points at gets the bytes."""
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "config.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "config.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this filesystem")

    atomic_write_text(link, '{"api_key": "x"}', mode=0o600)

    assert link.is_symlink(), "the link was replaced by a regular file"
    assert os.path.realpath(link) == os.path.realpath(real)
    assert real.read_text(encoding="utf-8") == '{"api_key": "x"}'
    assert (real.stat().st_mode & 0o777) == 0o600
    # The temp file was created beside the REAL file (the only
    # directory os.replace can rename into atomically) and is gone.
    assert sorted(p.name for p in real_dir.iterdir()) == ["config.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json", "dotfiles"]


def test_atomic_write_text_does_not_narrow_an_existing_parent(tmp_path):
    """Documented choice (R4-FU B-11): a pre-existing parent directory's
    mode is the operator's and is never tightened by a file write."""
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    before = parent.stat().st_mode & 0o777
    atomic_write_text(parent / "deck.dck", "[Main]\n")
    assert (parent.stat().st_mode & 0o777) == before
