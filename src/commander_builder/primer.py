"""FP-018.1 — deck primers: Quill-Delta parsing, sidecar storage, prompt caps.

A "primer" is the deck owner's own written explanation of the deck. The
two import sources carry it in ``description`` fields with DIFFERENT
shapes, pinned by real captures (the 2x25-deck primer harvest,
2026-08-27):

* **Archidekt**: a Quill Delta JSON *string* — ``{"ops": [...]}`` whose
  ops are text inserts (``{"insert": "..."}``, optionally with
  ``attributes``), card-link embeds (``{"insert": {"card-link":
  "<exact card name>"}}``) and image embeds (``{"insert": {"image":
  "<url>"}}``). ``tests/fixtures/hazel_primer.md`` pins the single-op
  case; ``tests/fixtures/archidekt_primer_delta_86888.json`` pins the
  formatted case (59 ops, 19 card-link embeds, 1 image embed).
* **Moxfield**: markdown / plain text, NEVER Delta.

:func:`parse_primer` branches on that split INTENTIONALLY: text that
parses as a Delta object is walked op by op; anything else — which is
what every Moxfield description is — passes through verbatim as plain
text. The same branch doubles as the degrade path for a malformed
Archidekt field: wrong shape yields readable text, never a crashed
import.

CARD-LINK EMBEDS ARE THE ONLY TRUSTED EXACT-NAME REFERENCES. The harvest showed
prose primers name cards with typos, nicknames and partial names
("Squirrel Girl", "cryptolith rite" — see the Hazel capture), so free
text is never mined for names (no NLP). A card-link embed, by contrast,
is the site's own exact name: :func:`parse_primer` collects them, the
sidecar records them in a marked block, and adopt reports them as
primer-vs-list evidence. They do not lock cards; only an explicit
``Protect=`` metadata line does that. Prose-only primers exist at every
length (the harvest has a 4.9k-char one with zero embeds) — for those the
exact-reference list is honestly empty.

WHY A MODULE OF ITS OWN. Three consumers, three change reasons, one
representation:

* the import lane (``moxfield_import.import_deck``) renders the primer
  and stores it BESIDE the ``.dck`` as ``<deckstem>.primer.md`` — a
  sidecar, not ``[metadata]``, because primers are paragraphs, not
  directives, and the ``.dck`` format belongs to Forge;
* the intent layer (``intent.Intent.stated`` /
  ``_deck_judge_prompt._intent_block``) feeds the rendered text into
  LLM prompts, which needs one shared length-cap-with-marker so no
  prompt builder invents its own silent truncation;
* the adopt flow (``adopt``) reads the sidecar back — text AND
  card-links — to ground its explanation of the deck.

Putting the Delta parser inside any one of those would make the others
import it sideways; ``archidekt_client`` stays a client (it captures the
field, it does not interpret it — its module docstring says exactly
that).

DEGRADE, NEVER CRASH. A primer is decoration on an import: no parse
failure here is allowed to sink ``import_deck``, and an empty render
writes nothing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

#: Sidecar suffix, appended to the deck file's STEM (not its suffix):
#: ``[USER] Hazel [B3].dck`` -> ``[USER] Hazel [B3].primer.md``. Keyed on
#: the stem so the sidecar follows the deck through the importer's
#: refuse-clobber naming (`_uniquify` gives a colliding deck a new stem,
#: and its primer lands under that same new stem — one deck, one primer,
#: never another deck's).
PRIMER_SIDECAR_SUFFIX = ".primer.md"

#: Per-field character budget for free text entering an LLM prompt.
#: Real primers run from a paragraph to several pages (the harvest's
#: formatted Sisay primer renders ~4.6k chars); the judge prompt already
#: carries full oracle text for every changed card, and an unbounded
#: primer would let one enthusiastic author drown the diff the panel is
#: there to judge. 4000 chars (~1k tokens) keeps the whole Hazel capture
#: (1613 chars) intact while bounding the pathological case. Truncation
#: is ALWAYS marked (see :func:`clip_for_prompt`) — a silently shortened
#: "stated intent" would misrepresent the author's words.
FREE_TEXT_PROMPT_CHAR_CAP = 4000

# The sidecar's machine-readable card-links block. An HTML comment — the
# same idiom as hazel_primer.md's provenance header — so the sidecar
# stays a readable markdown file while carrying the one piece of exact
# data the text render would otherwise flatten away (an embed renders as
# an ordinary word; only this block records that the SITE, not prose,
# named the card).
_CARD_LINKS_OPEN = (
    "<!-- primer-card-links (exact-name references from the source's "
    "card-link embeds)"
)
_LEGACY_CARD_LINKS_OPEN = (
    "<!-- primer-card-links (exact names from the source's card-link "
    "embeds; FP-018.3 auto-protect input)"
)
_CARD_LINKS_CLOSE = "-->"
_CARD_LINKS_RE = re.compile(
    r"(?:" + re.escape(_CARD_LINKS_OPEN) + "|"
    + re.escape(_LEGACY_CARD_LINKS_OPEN) + r")\n(.*?)\n"
    + re.escape(_CARD_LINKS_CLOSE)
    + r"\n?",
    re.DOTALL,
)


@dataclass
class ParsedPrimer:
    """One parsed ``description`` field.

    ``text`` is the rendered plain text. ``card_links`` are the EXACT
    card names from Delta card-link embeds, first-mention order, deduped
    — empty for prose-only Delta primers and for every non-Delta
    (Moxfield / degraded) description. ``was_delta`` records which
    branch parsed it, so callers can report which shape they got instead
    of guessing.
    """

    text: str = ""
    card_links: list[str] = field(default_factory=list)
    was_delta: bool = False


def parse_primer(description: Optional[str]) -> ParsedPrimer:
    """Parse a ``description`` field from EITHER source.

    THE PROVENANCE BRANCH, made intentional: Archidekt descriptions are
    Quill Delta JSON strings; Moxfield descriptions are markdown/plain
    text and never Delta (both facts capture-pinned — see the module
    docstring). So "parses as a Delta object" IS the source split, and
    the not-a-Delta branch is simultaneously the correct Moxfield
    behavior and the graceful degrade for a drifted Archidekt field.

    Delta walk: string inserts concatenate (``attributes`` are
    formatting — the consumers here want the words, not the markup); a
    card-link embed renders as its card name (dropping it would leave
    holes mid-sentence — the embed is how Archidekt shows a name inline)
    AND is collected into ``card_links``; any other non-string insert
    (image/video embed) is skipped rather than stringified —
    ``{'image': 'https://...'}`` is not primer text.

    ``None``/empty parses to an empty :class:`ParsedPrimer`. Never
    raises.
    """
    if not description:
        return ParsedPrimer()
    try:
        delta = json.loads(description)
    except (json.JSONDecodeError, TypeError):
        return ParsedPrimer(text=description)
    if isinstance(delta, str):
        # Valid JSON but a bare string — plain text that happened to be
        # JSON-encoded; the decoded form is the readable one.
        return ParsedPrimer(text=delta)
    if not isinstance(delta, dict) or not isinstance(delta.get("ops"), list):
        # Valid JSON, not a Delta (a list, a number, an alien object):
        # not text we can honestly interpret, so keep the field verbatim
        # rather than inventing a reading.
        return ParsedPrimer(text=description)
    parts: list[str] = []
    links: list[str] = []
    for op in delta["ops"]:
        if not isinstance(op, dict):
            continue
        ins = op.get("insert")
        if isinstance(ins, str):
            parts.append(ins)
        elif isinstance(ins, dict):
            name = ins.get("card-link")
            if isinstance(name, str) and name.strip():
                parts.append(name)
                if name not in links:
                    links.append(name)
    return ParsedPrimer(text="".join(parts), card_links=links,
                        was_delta=True)


def render_quill_delta(description: Optional[str]) -> str:
    """Text-only view of :func:`parse_primer` — kept as its own name so
    "give me the words" stays one call for prompt/report consumers."""
    return parse_primer(description).text


def primer_word_count(text: str) -> int:
    """Whitespace-token count, for the import lane's one-line report."""
    return len((text or "").split())


def primer_sidecar_path(dck_path: Path) -> Path:
    """The sidecar path for a deck file: ``<same dir>/<stem>.primer.md``."""
    dck_path = Path(dck_path)
    return dck_path.with_name(dck_path.stem + PRIMER_SIDECAR_SUFFIX)


def write_primer_sidecar(dck_path: Path, description: Optional[str],
                         ) -> Optional[Path]:
    """Parse ``description`` and store it beside ``dck_path``.

    Returns the sidecar path when one was written, ``None`` when there
    was nothing to write. An empty/whitespace render NEVER creates a
    file — an empty ``.primer.md`` would read as "this deck has a primer
    and it says nothing", which is a different (and false) claim from
    "no primer".

    When the source carried card-link embeds they are recorded first, in
    the marked comment block :func:`read_primer_card_links` reads back —
    without it the one machine-trustworthy list of exact names would not
    survive the trip to disk (the text render flattens an embed into an
    ordinary word). No embeds, no block.

    Overwrite semantics mirror the deck file the sidecar belongs to:
    ``import_deck`` overwrites a re-pulled deck in place, so a re-pull
    refreshes the primer too, keeping the pair in sync. A DIFFERENT
    deck can never be clobbered because the caller hands us the FINAL
    deck path — the one that already went through the importer's
    ``_uniquify`` refuse-clobber naming — and the sidecar shares its
    stem.

    Write failures degrade loudly-but-nonfatally at the CALLER (the
    import already succeeded; a sidecar I/O error must not unwind it),
    so this function raises ``OSError`` and lets the import lane decide.
    """
    parsed = parse_primer(description)
    text = parsed.text.strip()
    if not text:
        return None
    blocks: list[str] = []
    if parsed.card_links:
        blocks.append("\n".join(
            [_CARD_LINKS_OPEN, *parsed.card_links, _CARD_LINKS_CLOSE]))
    blocks.append(text)
    out = primer_sidecar_path(Path(dck_path))
    out.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return out


def read_primer_sidecar(dck_path: Path) -> Optional[str]:
    """The stored primer TEXT for ``dck_path``, or ``None`` when
    absent/unreadable. The card-links block is stripped — it is machine
    data, and prompt/report consumers want the author's words.

    Missing-file and I/O errors both read as "no primer" — the adopt
    flow treats the primer as optional evidence (the harvest put a
    usable primer on only ~25% of even top-ranked decks, so absence is
    the COMMON case), and a permission blip must degrade to the
    no-primer path rather than abort an offline explanation.
    """
    try:
        raw = primer_sidecar_path(Path(dck_path)).read_text(encoding="utf-8")
    except OSError:
        return None
    text = _CARD_LINKS_RE.sub("", raw).strip()
    return text or None


def read_primer_card_links(dck_path: Path) -> list[str]:
    """The exact card names the sidecar's card-links block records.

    These are exact-name primer references, not protection directives.
    Empty when there is no sidecar or the primer carried no embeds.
    Both the current marker and pre-correction auto-protect-era marker
    are accepted so existing imported sidecars remain readable.
    """
    try:
        raw = primer_sidecar_path(Path(dck_path)).read_text(encoding="utf-8")
    except OSError:
        return []
    m = _CARD_LINKS_RE.search(raw)
    if not m:
        return []
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]


_WIN_WORD_RE = re.compile(
    r"\b(?:win(?:s|ning)?|wincons?|combos?|infinite|loops?)\b",
    re.IGNORECASE,
)
_POSITIVE_WIN_HEADING_RE = re.compile(
    r"^(?:how\b.{0,60}\bwin(?:s|ning)?\b.{0,20}|"
    r"winning(?: the game)?|win(?:ning)? conditions?(?: and combos?)?|"
    r"wincons?|combos?(?: and loops?| lines?| packages?| in the deck)?|"
    r"loops?|finishers?)[:?]?$",
    re.IGNORECASE,
)
_EXCLUDED_WIN_HEADING_RE = re.compile(
    r"^(?:(?:to[ -]?do|cons|weakness(?:es)?|history|change ?log|updates?|"
    r"past versions?|previous versions?|old versions?|cuts?|"
    r"removed(?: cards?)?|cards? (?:cut|removed))\b.*|"
    r".*\bmaybe[ -]?board\b.*)$",
    re.IGNORECASE,
)


def _heading_kind(label: str) -> str:
    """Classify a normalized heading as positive, excluded, or neutral."""
    if _EXCLUDED_WIN_HEADING_RE.fullmatch(label):
        return "excluded"
    if _POSITIVE_WIN_HEADING_RE.fullmatch(label):
        return "positive"
    return "neutral"


def _primer_tokens(text: str) -> list[tuple[str, str, int]]:
    """Split prose into heading/paragraph tokens without losing newlines.

    Markdown headings always become tokens. Known plain-text headings do
    too, even when the following body has no blank-line separator — the
    shape emitted by rendered Quill descriptions in the harvest.
    """
    tokens: list[tuple[str, str, int]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        value = "\n".join(paragraph).strip()
        if value:
            tokens.append(("paragraph", value, 0))
        paragraph.clear()

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        markdown = re.fullmatch(r"(#{1,6})\s+(.+?)\s*#*", stripped)
        if markdown:
            flush_paragraph()
            label = markdown.group(2).strip().casefold()
            tokens.append(("heading", label, len(markdown.group(1))))
            continue

        plain_label = stripped.casefold()
        if stripped and _heading_kind(plain_label) != "neutral":
            flush_paragraph()
            tokens.append(("heading", plain_label, 1))
            continue
        if not stripped:
            flush_paragraph()
            continue
        paragraph.append(raw_line)

    flush_paragraph()
    return tokens


def quoted_win_lines(text: Optional[str], limit: int = 3) -> list[str]:
    """Return current win-line paragraphs in the author's own words.

    Moxfield page chrome and non-current sections are ignored.  Matches
    use whole win/combo words, prefer explicit win-oriented sections,
    and retain unheaded keyword paragraphs for older prose-only primers.
    Quotes stay verbatim and use the standard marked truncation.
    """
    if not text or limit <= 0:
        return []

    primer_marker = re.search(r"(?im)^\s*primer\s*$", text)
    if primer_marker:
        text = text[primer_marker.end():]

    preferred: list[str] = []
    fallback: list[str] = []
    headings: list[tuple[int, str]] = []
    for kind, value, level in _primer_tokens(text):
        if kind == "heading":
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, value))
            continue
        if any(_heading_kind(label) == "excluded" for _, label in headings):
            continue
        if not _WIN_WORD_RE.search(value):
            continue
        target = (
            preferred
            if any(_heading_kind(label) == "positive" for _, label in headings)
            else fallback
        )
        target.append(clip_for_prompt(value, 600))

    return (preferred + fallback)[:limit]


def clip_for_prompt(text: str, cap: int = FREE_TEXT_PROMPT_CHAR_CAP) -> str:
    """Bound free text for prompt use, marking any truncation explicitly.

    The marker names both halves of the honest disclosure — that the
    text was cut and how much of it survived — in the fixture corpus'
    own ``…[TRUNCATED`` idiom (``tests/fixtures/archidekt_deck_shape
    .json`` truncates its long fields the same way), so a reader of a
    logged prompt can tell a short primer from a shortened one.
    """
    text = text or ""
    if len(text) <= cap:
        return text
    return (
        text[:cap]
        + f" …[TRUNCATED — {cap} of {len(text)} chars shown]"
    )
