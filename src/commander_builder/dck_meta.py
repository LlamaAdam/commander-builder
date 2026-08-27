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

SECOND RESPONSIBILITY (2026-08-20) — the ``BracketUnverified=`` marker.
The filename's ``[B<n>]`` suffix is the OTHER piece of deck identity
encoded outside the card lists, and it has the same "a writer can
silently invalidate it" problem. It lives here for the same reason
``Name=`` does: one ``[metadata]`` read/write implementation, shared by
the web routes and any future non-web writer. See the marker section at
the bottom of this module.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# `BracketUnverified=` — the durable "this [B<n>] tag has no measurement
# behind it" marker (2026-08-20).
#
# WHY IT EXISTS AT ALL. ``web/routes_decks.deck_text`` PUT already noticed
# when a save changed the mainboard of a bracket-tagged deck and answered
# ``bracket_tag_unverified: true``, which the editor renders as "the [B]
# tag was NOT re-verified". But that answer was computed FRESH per request
# from "did THIS save change the mainboard?", so it was true for exactly
# one response. Clicking "Save changes" a second time without touching
# anything compared the just-written text against itself, came back false,
# and the warning vanished while the deck kept its still-unverified [B3]
# filename — the pool-poisoning path the hint exists to close reopened via
# the single most natural next click (proved empirically by the 2026-08-20
# Playwright smokes). A flag derived from one request cannot describe a
# state that outlives it; the state has to be WRITTEN DOWN.
#
# WHY IN `[metadata]`. Same precedent as ``Protect=`` (pet-card locks,
# web/_helpers.read_protected_cards), ``PoliticsGuard=`` (staples) and
# ``Moxfield=``: a plain ``Key=Value`` line in the block Forge parses for
# its own keys and IGNORES for unknown ones, so the marker travels with
# the deck across copies, snapshots and version bumps, and Forge loads the
# file exactly as before. It is NOT a card line and lives outside
# ``[Main]``, so ``dck_utils.main_card_quantities`` — the very comparison
# that sets the marker — cannot see it: writing the marker can never look
# like a mainboard change on the next save.
#
# WHY THE VALUE IS THE BRACKET DIGIT, not `1`/`true`. The marker means
# "bracket N, as declared by this filename, is unverified". Storing N is
# what makes the tag-rename case self-clearing: the user's answer to the
# warning may well be "fine, it IS a B4 deck" — they rename the file to
# ``[B4]`` and the OLD marker (``=3``) no longer describes the declared
# bracket, so it is stale by construction and stops flagging. There is no
# rename route in the web layer to hook (decks are renamed on disk), so a
# self-invalidating value is the only mechanism that can honor a rename at
# all. A boolean would have kept warning about a bracket nobody declares
# any more.
# ---------------------------------------------------------------------------

#: ``[metadata]`` key carrying the unverified declared bracket.
#: ``UPPER_SNAKE_CASE`` module constant, ``PascalCase`` on-disk key —
#: matching ``staples.POLITICS_GUARD_META_KEY`` / ``"PoliticsGuard"``.
BRACKET_UNVERIFIED_META_KEY: str = "BracketUnverified"

# Marker line INCLUDING its trailing newline so removal leaves no blank
# line behind. Case-insensitive to match how ``politics_guard_enabled``
# and ``read_protected_cards`` read their keys — a user who hand-types
# ``bracketunverified=3`` gets the same behavior.
_BRACKET_UNVERIFIED_LINE = re.compile(
    rf"^{BRACKET_UNVERIFIED_META_KEY}=.*$\n?",
    re.MULTILINE | re.IGNORECASE,
)


def read_bracket_unverified(dck_text: Optional[str]) -> Optional[int]:
    """Return the bracket the marker declares unverified, or ``None``.

    Only the ``[metadata]`` block is consulted and the key is matched
    case-insensitively — the same two rules ``read_protected_cards`` and
    ``staples.politics_guard_enabled`` enforce, so there is one
    ``[metadata]`` syntax to learn, not three.

    A value outside 1..5 (or non-numeric) reads as ABSENT rather than as
    "some unverified bracket": the caller's only use for the number is
    comparing it against the filename's ``[B<n>]`` tag, and a value that
    can't be compared can't tell a live marker from one left behind by a
    rename. Such a line is rewritten or dropped by the next
    ``set_``/``clear_`` call, so it never accumulates.
    """
    if not dck_text:
        return None
    in_metadata = False
    for raw in dck_text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            in_metadata = s.lower() == "[metadata]"
            continue
        if not in_metadata or "=" not in s:
            continue
        key, _, value = s.partition("=")
        if key.strip().lower() != BRACKET_UNVERIFIED_META_KEY.lower():
            continue
        try:
            n = int(value.strip())
        except ValueError:
            continue
        if 1 <= n <= 5:
            return n
    return None


def clear_bracket_unverified(dck_text: str) -> str:
    """Return ``dck_text`` with every ``BracketUnverified=`` line removed.

    Every line, not just the first: a hand-edited file (or two writers
    racing) could hold more than one, and leaving a second copy behind
    would make "cleared" depend on which line a reader stopped at.
    Everything else passes through byte-identical.
    """
    return _BRACKET_UNVERIFIED_LINE.sub("", dck_text)


def set_bracket_unverified(dck_text: str, bracket: int) -> str:
    """Return ``dck_text`` carrying exactly one ``BracketUnverified=<n>``.

    Idempotent by construction — any existing marker is removed first, so
    re-saving an already-marked deck never grows a second line (the
    editor round-trips the file through a textarea, so the marker is in
    the text the user PUTs back).

    Placement: after the last non-empty line of the ``[metadata]`` block,
    i.e. before the first card-section header — the same rule
    ``moxfield_import._insert_metadata_lines`` documents, which is what
    keeps the line inside the block every metadata parser (this module's
    reader included) actually scans. A deck with no ``[metadata]`` header
    gets one synthesized, mirroring ``rewrite_name``.
    """
    out = clear_bracket_unverified(dck_text)
    line = f"{BRACKET_UNVERIFIED_META_KEY}={int(bracket)}"
    m = _METADATA_HEADER.search(out)
    if not m:
        return f"[metadata]\n{line}\n\n" + out
    lines = out.splitlines()
    # Line index of the `[metadata]` header itself: the number of
    # newlines in the text preceding it.
    head_idx = out[: m.start()].count("\n")
    insert_at = head_idx + 1
    for i in range(head_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        if stripped:
            insert_at = i + 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + "\n"
