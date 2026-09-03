"""Stable per-deck identity for the knowledge log.

WHY THIS MODULE EXISTS (2026-09-03, R3 C-08)
============================================
``knowledge_log`` keys every per-deck surface on ``deck_id`` —
``iterations_for_deck``, ``verdict_breakdown_for_deck``, the history
trajectory, the pricing series, the iteration graph and the judge-
agreement joins. That key used to come from ``iteration_loop.
resolve_deck_id``, which read exactly one provenance line
(``Moxfield=<publicId>``) and otherwise fell back to the FILENAME STEM.
Two things went wrong at once:

  * The Archidekt import lane (decision C3) records ``Archidekt=<id>`` /
    ``Source=archidekt`` instead of a ``Moxfield=`` line, so every deck
    adopted through it was a stem-keyed deck.
  * The two unattended writers (``_proposer_sim._log_auto_curate_
    iteration`` and ``improve._log_bandit_pull``) key on the NEW deck's
    stem, which gains `` v2``, `` v3`` … on every accepted round. A
    hand-built or Archidekt deck therefore got a fresh ``deck_id`` per
    iteration: every per-deck surface saw one-row "decks", and the
    auto-curate writer — which looks up "prior iterations of this deck"
    by the just-bumped stem — wrote ``parent_id = None`` every round.

The fix is one identity function every writer AND reader routes
through, with three lanes in priority order:

  1. ``Moxfield=<publicId>``  → the bare publicId (unchanged: every
     existing Moxfield-keyed row stays valid).
  2. ``Archidekt=<id>``       → ``archidekt:<id>``. Namespaced, because
     an Archidekt id is a small integer and a bare one could collide
     with nothing today but would read as "a publicId" to every
     consumer that assumes the Moxfield lane.
  3. otherwise               → the VERSION-STRIPPED filename stem:
     ``[USER] Foo v3 [B3].dck`` → ``[USER] Foo [B3]``. The `` v<N>``
     suffix is parsed by the SAME regexes ``proposer._bump_version_
     filename`` writes it with, so the two cannot drift.

Lane 3 is still a filename — it breaks on a rename, as it always did —
but it no longer breaks on the one rename the pipeline itself performs
on every accepted swap.

Existing rows are NOT rewritten here. ``scripts/backfill_deck_ids.py``
(dry-run by default) reports and, on ``--apply``, re-keys them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# `Moxfield=<publicId>` lines in the .dck metadata block let us recover the
# durable deck identity even after the file is renamed (e.g. user changes
# the deck name on Moxfield). Same pattern ``moxfield_import._MOXFIELD_META``
# and ``knowledge_log.migrate_legacy_deck_ids`` read.
_MOXFIELD_ID = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)

# `Archidekt=<id>`: the C3 fallback lane's provenance line
# (``moxfield_import._ARCHIDEKT_META``). Deliberately a separate key in the
# .dck — see that module — and deliberately a NAMESPACED id here.
_ARCHIDEKT_ID = re.compile(r"^Archidekt=(.+)$", re.MULTILINE)

#: Prefix on every Archidekt-lane ``deck_id``.
ARCHIDEKT_ID_PREFIX = "archidekt:"

#: ``deck_id`` values that look like a filename or a filename stem — the
#: only shape the backfill may re-key. An explicit id (publicId, test id,
#: ``archidekt:``-namespaced id) never matches and is never touched.
_FILENAME_SHAPED = re.compile(
    r"(?:\.dck$)|(?:\s\[B[0-9?]\](?:\.dck)?$)|(?:\sv\d+$)"
)


def stable_deck_stem(name_or_stem: str) -> str:
    """The version-stripped stem: ``'[USER] Foo v3 [B3].dck'`` and
    ``'[USER] Foo v3 [B3]'`` both → ``'[USER] Foo [B3]'``; ``'Foo v2.dck'``
    → ``'Foo'``; a name with no version passes through as its stem.

    Reuses ``proposer._VERSION_BRACKET_RE`` / ``_VERSION_NO_BRACKET_RE``
    (imported lazily — ``proposer`` is a heavy module and this one must
    stay importable from anywhere) so the suffix this strips is exactly
    the suffix ``_bump_version_filename`` appends.
    """
    from .proposer import _VERSION_BRACKET_RE, _VERSION_NO_BRACKET_RE

    name = name_or_stem.strip()
    as_file = name if name.endswith(".dck") else f"{name}.dck"
    m = _VERSION_BRACKET_RE.match(as_file)
    if m:
        return f"{m.group('base')} [B{m.group('bracket')}]"
    m = _VERSION_NO_BRACKET_RE.match(as_file)
    if m:
        return m.group("base")
    return name[:-4] if name.endswith(".dck") else name


def deck_id_from_text(dck_text: str) -> Optional[str]:
    """The provenance-keyed id carried in a .dck's metadata block, or
    None when the deck carries neither ``Moxfield=`` nor ``Archidekt=``.
    ``Moxfield=`` wins when both are present (it is the lane every
    existing row was keyed under)."""
    m = _MOXFIELD_ID.search(dck_text)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = _ARCHIDEKT_ID.search(dck_text)
    if m and m.group(1).strip():
        return f"{ARCHIDEKT_ID_PREFIX}{m.group(1).strip()}"
    return None


def resolve_deck_id(deck_path: Path, fallback: Optional[str] = None) -> str:
    """The durable ``deck_id`` for a .dck on disk.

    Reads the provenance id (``deck_id_from_text``) first. Without one,
    returns ``fallback`` when supplied — an EXPLICIT fallback passes
    through untouched, so a caller that already holds a stable id keeps
    it — and otherwise the version-stripped stem (``stable_deck_stem``).
    Callers that pass a filename-derived fallback should pass
    ``stable_deck_stem(path.name)``, not ``path.stem``: the raw stem is
    exactly the per-version key R3 C-08 removed.

    A missing file returns ``fallback`` when one is given and raises
    ``ValueError`` otherwise, so nobody drops into filename-as-id mode by
    accident.
    """
    if not deck_path.exists():
        if fallback is not None:
            return fallback
        raise ValueError(f"deck not found and no fallback: {deck_path}")
    provenance = deck_id_from_text(deck_path.read_text(encoding="utf-8"))
    if provenance is not None:
        return provenance
    if fallback is not None:
        return fallback
    return stable_deck_stem(deck_path.name)


def is_filename_shaped_deck_id(deck_id: str) -> bool:
    """True when a stored ``deck_id`` is a filename / stem (with a
    ``[B<n>]`` suffix, a ``.dck`` extension or a `` v<N>`` version) —
    the only shape the backfill is allowed to re-key."""
    return bool(_FILENAME_SHAPED.search((deck_id or "").strip()))


def stable_deck_id_for_row(
    deck_id: str, deck_snapshot: Optional[str],
) -> Optional[str]:
    """What ``resolve_deck_id`` would produce for an existing row, or
    None when the row's current id must be left alone.

    Used by ``scripts/backfill_deck_ids.py``. Only filename-shaped ids
    are candidates (an explicit id is never second-guessed); the new id
    is the snapshot's provenance id when the snapshot carries one, else
    the version-stripped stem of the current id. Returns None when the
    result equals the current id — a no-op is not a change.
    """
    current = (deck_id or "").strip()
    if not is_filename_shaped_deck_id(current):
        return None
    new_id = deck_id_from_text(deck_snapshot or "") or stable_deck_stem(current)
    return new_id if new_id != current else None
