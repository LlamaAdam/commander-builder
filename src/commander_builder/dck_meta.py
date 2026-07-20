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
# `.*`, NOT `.+`: an EMPTY `Name=` line still counts as "the Name= line".
# With `.+` the search missed it, so rewrite_name concluded "no Name="
# and synthesized a second one under [metadata] — leaving BOTH the empty
# line and the new one in the file, and which of the two Forge honors is
# parser-dependent. Replacing the empty line keeps exactly one.
_NAME_LINE = re.compile(r"^Name=.*$", re.MULTILINE)

# `[metadata]` section header (usually the first line of the file). Used to
# synthesize a Name= line right below it when the deck has none.
_METADATA_HEADER = re.compile(r"\[metadata\][^\n]*(?:\n|$)", re.IGNORECASE)

# `DisplayName=` metadata line — the human-facing deck name, preserved when
# `Name=` gets overwritten with the filename stem (see
# ``stamp_name_preserving_display``). Forge ignores unknown metadata keys
# (verified precedent: `Moxfield=` / `Protect=` load identically), so this
# travels with the file without affecting sims.
_DISPLAY_NAME_LINE = re.compile(r"^DisplayName=.+$", re.MULTILINE)


def rewrite_name(dck_text: str, new_name: str) -> str:
    """Return ``dck_text`` with its ``[metadata] Name=`` set to ``new_name``.

    Only the FIRST ``Name=`` line is replaced; every other metadata line
    (``Moxfield=``, ``Protect=``, ...) and all card sections pass through
    byte-identical. Decks with no ``Name=`` get one synthesized — inserted
    under an existing ``[metadata]`` header, or a whole ``[metadata]``
    section prepended when the deck has none — because a Name-less deck
    leaves Forge to invent its own display name, which the log parser can
    never map back to the file. An empty ``Name=`` line counts as PRESENT
    and is replaced in place (synthesizing next to it would leave a
    duplicate).

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


def stamp_name_preserving_display(dck_text: str, stem: str) -> str:
    """Set ``Name=`` to the final filename ``stem``, keeping the pretty name.

    WHY — ``to_dck`` renders ``Name=<raw Moxfield name>``, but
    ``safe_filename`` (and the web route's sanitizer) strips non-ASCII and
    substitutes characters like ``:``. A deck named
    "Chatterfang: Squirrel Tribal 🐿" therefore lands under a filename whose
    stem no longer normalizes to its own ``Name=``, breaking every
    name-keyed consumer at once: compare_versions pod aggregation,
    pool_curator candidate matching, and Forge's own deck picker (which
    locates the deck by the ``Name=`` it displays for the filename we pass).
    Stamping ``Name=<final stem>`` at write time holds the module invariant
    (``_normalize(stem) == _normalize(Name=)``) for EVERY importable name.

    The original pretty name is not thrown away: it moves to a
    ``DisplayName=`` line right below ``Name=`` so display surfaces
    (``status._parse_dck_metadata``) can keep showing the user's chosen
    name instead of the bracketed filename stem. Rules:

    - No prior ``Name=`` (bare paste) → one is synthesized from the stem
      and there is no pretty name to preserve.
    - Prior ``Name=`` already equals the stem (re-stamp) → nothing to do.
    - A ``DisplayName=`` already present wins — never duplicated or
      clobbered, so user edits to it survive re-imports of copied files.
      (The re-import half of that contract lives in
      ``moxfield_import._merge_local_metadata``, which carries the LOCAL
      ``DisplayName=`` into the fresh render before this stamp runs — the
      stamp alone only guards the file it is handed.)

    Everything else (``Moxfield=``, ``Protect=``, card sections) passes
    through byte-identical — same-id re-import classification and pet-card
    locks are untouched by design.
    """
    m = _NAME_LINE.search(dck_text)
    # ``m.group(0)`` is the whole "Name=<value>" line; slice off the key so
    # deck names containing "=" survive intact.
    old_name = m.group(0)[len("Name="):].strip() if m else ""
    out = rewrite_name(dck_text, stem)
    if not old_name or old_name == stem or _DISPLAY_NAME_LINE.search(out):
        return out
    # Insert DisplayName= directly after the (just-rewritten) Name= line —
    # plain string splicing, not re.sub, so a pretty name containing group
    # references (``\1``, ``\g<...>``) is inserted literally.
    nm = _NAME_LINE.search(out)
    assert nm is not None  # rewrite_name guarantees a Name= line exists
    return out[: nm.end()] + f"\nDisplayName={old_name}" + out[nm.end():]


def rewrite_name_to_stem(path: Path) -> str:
    """Rewrite ``path``'s ``Name=`` to its own filename stem, in place.

    Call this right after copying or writing a ``.dck`` under a new
    filename. Returns the stem that was written, mostly for logging.
    """
    text = path.read_text(encoding="utf-8")
    path.write_text(rewrite_name(text, path.stem), encoding="utf-8")
    return path.stem
