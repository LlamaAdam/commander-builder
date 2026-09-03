"""Crash-safe text writes for the core layer (2026-09-03, R3 W-08/W-09).

``web/_helpers.atomic_write_text`` (the M2 fix, 2026-08) had the right
shape but lived in the web package, which the core layer cannot import
(``collection.py``'s layering note), so ``dck_meta.rewrite_name_to_stem``,
the import route, the build worker and ``deck_source`` kept doing bare
``write_text`` — a crash mid-write truncated the deck, and
``config_store`` wrote the API key non-atomically and chmod'd after.
The primitive now lives here; ``web._helpers`` re-exports it.

Two things every caller gets for free:

* ``newline=""`` — the text is written BYTE-FOR-BYTE. Python's default
  newline translation would turn a CRLF deck into CRCRLF on Windows and
  silently LF-normalize on POSIX (R3 W-10).
* ``mode`` — the replacement keeps the original file's mode, or takes an
  explicit one (``config_store`` wants 0o600 from the first byte, never a
  0o644 window before a chmod).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def atomic_write_text(path: Path, text: str, *,
                      mode: Optional[int] = None,
                      encoding: str = "utf-8") -> None:
    """Replace ``path``'s contents with ``text`` atomically.

    Writes a temp file in the SAME directory (``os.replace`` is only
    atomic within one filesystem) and renames it over the target, so a
    crash / full disk mid-write leaves the previous file intact instead
    of a truncated one. The temp name is dot-prefixed and ends in
    ``.tmp`` — never ``.dck`` — so the deck enumerators that glob
    ``*.dck`` can never pick up a half-written file.

    ``fsync`` before the rename so the rename cannot be reordered ahead
    of the data on a crash. The replacement inherits the ORIGINAL file's
    mode unless ``mode`` is given — ``mkstemp`` creates 0600, and
    silently narrowing a deck file to owner-only on every save would be
    an invisible side effect of an unrelated fix. Raises ``OSError`` like
    ``write_text``.
    """
    path = Path(path)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = None
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        # Only reachable when the rename did NOT happen (a successful
        # os.replace consumes tmp_name), so this never deletes live data.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
