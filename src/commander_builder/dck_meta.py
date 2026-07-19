"""Keep a `.dck` file's `[metadata] Name=` field aligned with its filename.

WHY THIS EXISTS — win attribution is name-keyed in several pipelines.
Forge's ``Match Result:`` log lines report each seat as ``Ai(N)-<Name>``
where ``<Name>`` is the deck's internal ``[metadata] Name=`` field, NOT its
filename. But the query side of the comparison starts from the FILENAME:

- ``compare_versions._aggregate_pod`` / ``_make_pod_abort_check`` key on
  ``log_parser._normalize(<filename>)``,
- ``run_match`` does the same for the user deck,
- ``pool_curator._filename_for_match`` maps the reported name back to a
  filename via ``_candidate_match_keys`` (equivalent stripping rules).

The two sides only meet when

    _normalize(<filename stem>) == _normalize(<Name= field>)

``_normalize`` strips the ``[USER] `` prefix, the ``.dck`` extension and the
`` [B<n>]`` bracket suffix — nothing else. Deck writers that copy or splice
an EXISTING .dck under a NEW filename (``snapshot_deck``, the proposer's v2
writer, ``meta_test``'s [REF] importer) inherit the source deck's ``Name=``,
silently breaking the invariant: Forge then reports a name no filename
normalizes to, and every game the deck wins is attributed to nobody (a
snapshot A/B reads 0-0 forever) — or, worse, to the *other* version when
both share the stale name. Writing ``Name=<filename stem>`` holds the
invariant trivially: ``_normalize`` is applied to both sides at match time,
so identical inputs always agree. (``pool_curator._candidate_match_keys``
strips the same prefix/suffix from the filename, so ``Name= == stem``
satisfies its most-specific exact-stem match too.)

The regex-rewrite logic originated in ``web/routes_sim.py``'s deck-staging
path — the first place this misattribution was diagnosed. It lives here (a
non-web module) so the web route and the library-level deck writers share
one implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

# First `Name=` line anywhere in the file. .dck metadata keys only appear in
# the [metadata] section, which by convention leads the file, so "first
# Name= line" is the metadata name. count=1 in the substitution keeps a
# hypothetical later `Name=` inside a comment or odd section untouched.
_NAME_LINE = re.compile(r"^Name=.+$", re.MULTILINE)

# `[metadata]` section header (usually the first line of the file). Used to
# synthesize a Name= line right below it when the deck has none.
_METADATA_HEADER = re.compile(r"\[metadata\][^\n]*(?:\n|$)", re.IGNORECASE)


def rewrite_name(dck_text: str, new_name: str) -> str:
    """Return ``dck_text`` with its ``[metadata] Name=`` set to ``new_name``.

    Only the FIRST ``Name=`` line is replaced; every other metadata line
    (``Moxfield=``, ``Protect=``, ...) and all card sections pass through
    byte-identical. Decks with no ``Name=`` get one synthesized — inserted
    under an existing ``[metadata]`` header, or a whole ``[metadata]``
    section prepended when the deck has none — because a Name-less deck
    leaves Forge to invent its own display name, which the log parser can
    never map back to the file.

    The replacement uses a callable so ``new_name`` is inserted literally
    (deck names can contain characters ``re.sub`` would otherwise treat as
    group references).
    """
    if _NAME_LINE.search(dck_text):
        return _NAME_LINE.sub(lambda _m: f"Name={new_name}", dck_text, count=1)
    m = _METADATA_HEADER.search(dck_text)
    if m:
        head = dck_text[: m.end()]
        if not head.endswith("\n"):
            # Degenerate case: file ends exactly at `[metadata]` with no
            # trailing newline — add one so Name= lands on its own line.
            head += "\n"
        return head + f"Name={new_name}\n" + dck_text[m.end():]
    return f"[metadata]\nName={new_name}\n\n" + dck_text


def rewrite_name_to_stem(path: Path) -> str:
    """Rewrite ``path``'s ``Name=`` to its own filename stem, in place.

    Call this right after copying or writing a ``.dck`` under a new
    filename. Returns the stem that was written, mostly for logging.
    """
    text = path.read_text(encoding="utf-8")
    path.write_text(rewrite_name(text, path.stem), encoding="utf-8")
    return path.stem
