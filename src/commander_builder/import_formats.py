"""Foreign paste-format parsers: MTGA/Arena exports and CSV card lists.

The web paste-import flow historically accepted two shapes: a Forge
``.dck`` blob (has ``[Main]``-style section headers) and the plain
Moxfield bulk-paste line list (``<qty> <Name>`` per line). This module
adds the two formats ManaFoundry accepts on top of those — the MTG
Arena export format and CSV collection/deck exports — plus a
conservative auto-detector so the paste box stays a single textarea.

Layering: this is a CORE module (sibling of ``dck_utils``), not a web
helper, for two reasons:

* ``dck_utils``'s documented scope is "canonical primitives for parsing
  Forge .dck files". Arena/CSV are *foreign* formats being converted
  INTO .dck — the same concern as ``moxfield_import`` (JSON → .dck),
  which also lives in core. Folding them into dck_utils would blur the
  one module whose job is to be the single source of .dck truth.
* Core placement keeps the parsers importable by any CLI surface
  without reaching into ``commander_builder.web`` (web imports core,
  never the reverse).

The web layer's ``deck_text_ops._normalize_pasted_deck`` stays the
single dispatch point the import route calls; it consults
``detect_paste_format`` and delegates here. Contract for both parsers:
they produce ONLY the intermediates the existing paste path already
produces —

* ``arena_to_dck``  → a ``.dck`` text blob (``[Commander]``/``[Main]``/
  ``[Sideboard]`` sections), i.e. exactly what a .dck paste hands
  downstream. Name= stamping, role prefixes, and file writing stay in
  the existing writers untouched.
* ``csv_to_lines``  → a plain ``<qty> <Name>`` line list, which the
  caller feeds through the SAME plain-paste wrap the Moxfield bulk
  list uses. That reuse is deliberate: CSV exports carry no commander
  column, and the plain-paste path's commander behavior today is
  "none — every card goes to [Main]"; routing CSV through it inherits
  that behavior (and any future improvement to it) for free.

Malformed input in a *detected* format raises ``ImportFormatError``
naming the offending line — the route turns that into a 400, never a
stacktrace. Ambiguous input is never an error: ``detect_paste_format``
only claims a format on a strong signal and otherwise answers
``"plain"``, so the worst case is the historical plain-lines behavior.
"""
from __future__ import annotations

import csv
import io
import re

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ImportFormatError(ValueError):
    """A paste was positively detected as Arena/CSV but a line inside it
    doesn't parse.

    Carries the 1-based line number and the raw line so the web route
    can show the user exactly what to fix. Subclasses ValueError so
    pre-existing broad ``except ValueError`` handlers (none today on
    this path, but cheap insurance) degrade sanely.
    """

    def __init__(self, message: str, line_no: int, line: str):
        # The str() form is user-facing (the route serves it verbatim in
        # the 400 body), so bake the location into the message.
        super().__init__(f"{message} (line {line_no}: {line!r})")
        self.line_no = line_no
        self.line = line


# ---------------------------------------------------------------------------
# MTGA / Arena export format
# ---------------------------------------------------------------------------

# One Arena card line: ``<qty> <Name> [(SET) [CN]]``. The set/collector
# tail is optional (decklists copied without printings are legal Arena
# paste). Anchored at end-of-line so a non-greedy name capture can't
# swallow the tail; ``\S+`` for the collector number because Arena
# promos use non-numeric CNs ("GR6", "306a") and being strict here
# would silently misparse the tail INTO the card name instead.
_ARENA_LINE_RE = re.compile(
    r"^(?P<qty>\d+)\s+(?P<name>.+?)"
    r"(?:\s+\((?P<set>[A-Za-z0-9]{2,7})\)(?:\s+(?P<cn>\S+))?)?\s*$"
)

# Tail-only probe for detection: does a line END in ``(SET)`` or
# ``(SET) CN``? Used to score "most lines carry printing tails".
_ARENA_TAIL_RE = re.compile(r"\s\([A-Za-z0-9]{2,7}\)(?:\s+\S+)?\s*$")

# Arena section headers, lowercased. Values are the .dck section each
# maps to; None means "discard the section's content".
#   Deck      → [Main]
#   Commander → [Commander]
#   Sideboard → [Sideboard]  (matches the existing .dck-paste behavior:
#               sideboards are PRESERVED as their own section, which
#               downstream consumers — count_main_cards, the swap
#               splicer — already ignore/pass through)
#   Companion → [Sideboard]  (the companion lives outside the 100; the
#               sideboard is the .dck section with the same "not in the
#               main deck" meaning)
#   About     → discarded (holds ``Name <deck name>`` lines, not cards;
#               the import route derives the deck name from the user's
#               form field, same as every other paste)
_ARENA_HEADERS: dict[str, str | None] = {
    "deck": "main",
    "commander": "commander",
    "sideboard": "sideboard",
    "companion": "sideboard",
    "about": None,
}


def _looks_like_arena(lines: list[str]) -> bool:
    """Conservative Arena signal on stripped, order-preserved lines.

    Two independent signals, either sufficient:

    * an exact bare ``Deck`` or ``About`` header line. These two are
      Arena-specific; bare ``Commander``/``Sideboard`` are deliberately
      NOT sufficient alone because Moxfield-style pastes carry similar
      words ("Commander (1)") and the plain path already handles those
      — misclassifying a plain paste would be worse than missing a
      header-only Arena paste, which still parses fine as plain lines.
    * a majority (>50%, minimum 2) of the quantity-prefixed lines carry
      a ``(SET) CN`` printing tail. A plain list never has these; a
      single decorated line among many plain ones stays plain.
    """
    qty_lines = 0
    tailed = 0
    for s in lines:
        if s.lower() in ("deck", "about"):
            return True
        if re.match(r"^\d+\s+\S", s):
            qty_lines += 1
            if _ARENA_TAIL_RE.search(s):
                tailed += 1
    return tailed >= 2 and tailed * 2 > qty_lines


def arena_to_dck(text: str) -> str:
    """Convert an MTGA/Arena export paste to Forge ``.dck`` text.

    Output shape mirrors ``moxfield_import.to_dck`` minus the metadata
    block (Name= stamping happens downstream in the existing writers,
    exactly as for a plain paste): ``[Commander]`` section when the
    paste has one, then ``[Main]``, then ``[Sideboard]`` if present.

    Duplicate lines for the same card AGGREGATE within a section
    (``1 Shock`` twice → ``2 Shock``): Arena decks legally run 4-ofs
    that some exporters emit as repeated singles, and collapsing them
    keeps the .dck one-line-per-name like every other import path.

    Raises ``ImportFormatError`` for any non-blank line that is neither
    a known header nor a parseable card line — the caller only invokes
    this after positive detection, so a bad line is a real user error
    worth naming, not a reason to fall back silently.
    """
    # Per-section name → qty, insertion-ordered (dict preserves order),
    # plus name casing of first occurrence.
    sections: dict[str, dict[str, int]] = {
        "commander": {}, "main": {}, "sideboard": {},
    }
    # Headerless Arena pastes start straight into card lines → main.
    current: str | None = "main"
    for line_no, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if not s:
            # Blank lines end ONLY the About section. About is the one
            # section whose content we discard, so its scope is kept as
            # narrow as possible (header through the next blank); for
            # kept sections a stray blank line is cosmetic and must not
            # silently reroute the cards that follow it.
            if current is None:
                current = "main"
            continue
        if s.lower() in _ARENA_HEADERS:
            current = _ARENA_HEADERS[s.lower()]
            continue
        if current is None:
            # Inside About: metadata like ``Name My Deck`` — not cards.
            continue
        m = _ARENA_LINE_RE.match(s)
        if not m:
            raise ImportFormatError(
                "unrecognized MTG Arena line — expected "
                "'<count> <card name> [(SET) <number>]' or a section "
                "header (Deck / Commander / Sideboard / About)",
                line_no, s,
            )
        qty = int(m.group("qty"))
        name = m.group("name").strip()
        if not name:
            raise ImportFormatError(
                "MTG Arena line has no card name", line_no, s,
            )
        bucket = sections[current]
        bucket[name] = bucket.get(name, 0) + qty

    out: list[str] = []
    # Section order matches to_dck: Commander first (Forge convention),
    # then Main; Sideboard last, only when non-empty so a plain "Deck"
    # export doesn't grow an empty section.
    if sections["commander"]:
        out.append("[Commander]")
        out.extend(f"{q} {n}" for n, q in sections["commander"].items())
    out.append("[Main]")
    out.extend(f"{q} {n}" for n, q in sections["main"].items())
    if sections["sideboard"]:
        out.append("[Sideboard]")
        out.extend(f"{q} {n}" for n, q in sections["sideboard"].items())
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CSV card lists (deck exports / collection exports)
# ---------------------------------------------------------------------------

# Recognized header column names, lowercased/stripped. Kept deliberately
# small — detection keys on these, and a generous list would start
# claiming non-CSV pastes.
_CSV_NAME_COLS = frozenset({"name", "card", "card name", "card_name"})
_CSV_COUNT_COLS = frozenset({"count", "quantity", "qty"})

# Comma first (the overwhelmingly common export dialect), semicolon
# second (European-locale Excel re-saves).
_CSV_DELIMITERS = (",", ";")


def _csv_header_columns(text: str) -> tuple[str, int, int | None] | None:
    """Sniff the first non-blank line as a CSV header row.

    Returns ``(delimiter, name_index, count_index_or_None)`` when the
    line parses (with either supported delimiter) into 2+ columns, one
    of which is a recognized NAME column. Otherwise ``None`` — not CSV.

    Requiring 2+ columns means a delimiter must actually be present:
    a plain-paste first line like ``1 Krenko, Mob Boss`` does contain a
    comma, but its fields ("1 Krenko" / "Mob Boss") match no recognized
    column name, so it stays plain. The count column is OPTIONAL —
    some deck exports are name-only-plus-extras — and defaults each row
    to quantity 1, mirroring how a bare name line would be entered by
    hand.
    """
    first = ""
    for raw in text.splitlines():
        if raw.strip():
            # Strip a UTF-8 BOM: Excel-authored CSVs routinely carry
            # one, and it would glue itself to the first column name.
            first = raw.strip().lstrip("\ufeff")
            break
    if not first:
        return None
    for delim in _CSV_DELIMITERS:
        try:
            fields = next(csv.reader([first], delimiter=delim))
        except (csv.Error, StopIteration):
            continue
        if len(fields) < 2:
            continue
        lowered = [f.strip().lower() for f in fields]
        name_idx = next(
            (i for i, f in enumerate(lowered) if f in _CSV_NAME_COLS), None,
        )
        if name_idx is None:
            continue
        count_idx = next(
            (i for i, f in enumerate(lowered) if f in _CSV_COUNT_COLS), None,
        )
        return delim, name_idx, count_idx
    return None


def _looks_like_csv(text: str) -> bool:
    """True when the first non-blank line sniffs as a CSV header row."""
    return _csv_header_columns(text) is not None


def csv_to_lines(text: str) -> str:
    """Convert a CSV card list to the plain ``<qty> <Name>`` line list.

    The return value is deliberately the PLAIN-PASTE shape, not .dck:
    the caller routes it through the same ``[Main]``-wrapping the
    Moxfield bulk list gets. CSV exports carry no commander column, so
    commander handling falls back to exactly what the plain-paste path
    does today (nothing — all cards to [Main]); if that path ever grows
    commander detection, CSV inherits it with zero changes here.

    Duplicate names AGGREGATE (collection exports emit one row per
    printing/foil variant of the same card). Extra columns (set, price,
    foil, ...) are ignored; quoting is handled by the csv module.

    Raises ``ImportFormatError`` on a data row with an empty/missing
    name or a non-integer count — detection already confirmed the
    header, so a broken row is a genuine error to surface, not a
    fall-back-to-plain case.
    """
    sniffed = _csv_header_columns(text)
    if sniffed is None:
        # Defensive: callers should only get here after detection, but
        # a direct caller deserves a real error, not an IndexError.
        raise ImportFormatError("no recognizable CSV header row", 1,
                                text.splitlines()[0] if text.splitlines() else "")
    delim, name_idx, count_idx = sniffed

    # csv.reader over the raw text handles quoted fields spanning
    # delimiters. Track physical line numbers for error messages via
    # the reader's own line_num (accounts for quoted embedded newlines).
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delim)
    cards: dict[str, int] = {}
    header_seen = False
    for row in reader:
        # Skip fully blank rows (trailing newline artifacts).
        if not row or all(not f.strip() for f in row):
            continue
        if not header_seen:
            # First non-blank row is the header we already sniffed.
            header_seen = True
            continue
        line_no = reader.line_num
        raw_line = delim.join(row)
        if name_idx >= len(row) or not row[name_idx].strip():
            raise ImportFormatError(
                "CSV row is missing a card name", line_no, raw_line,
            )
        name = row[name_idx].strip()
        qty = 1
        if count_idx is not None and count_idx < len(row):
            count_raw = row[count_idx].strip()
            if count_raw:
                try:
                    qty = int(count_raw)
                except ValueError:
                    raise ImportFormatError(
                        "CSV count column is not a whole number",
                        line_no, raw_line,
                    ) from None
                if qty <= 0:
                    raise ImportFormatError(
                        "CSV count must be positive", line_no, raw_line,
                    )
        cards[name] = cards.get(name, 0) + qty
    return "\n".join(f"{q} {n}" for n, q in cards.items()) + ("\n" if cards else "")


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def detect_paste_format(text: str) -> str:
    """Classify pasted deck text: ``"dck"``, ``"arena"``, ``"csv"``, or
    ``"plain"``.

    Order matters and encodes precedence:

    1. ``.dck`` — any ``[section]`` header line. Checked first and with
       the EXACT same test ``_normalize_pasted_deck`` has always used,
       so every paste that worked before this feature still routes the
       same way (backward compatibility is the hard constraint here).
    2. Arena — bare ``Deck``/``About`` header or a majority of
       ``(SET) CN`` printing tails (see ``_looks_like_arena``).
    3. CSV — first non-blank line sniffs as a header row with a
       recognized name column and 2+ columns.
    4. ``"plain"`` — everything else. Never an error: ambiguity always
       degrades to the historical plain-lines behavior.
    """
    stripped = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(s.startswith("[") and s.endswith("]") for s in stripped):
        return "dck"
    if _looks_like_arena(stripped):
        return "arena"
    if _looks_like_csv(text):
        return "csv"
    return "plain"
