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

CARD-LINK EMBEDS ARE THE ONLY TRUSTED CARD NAMES. The harvest showed
prose primers name cards with typos, nicknames and partial names
("Squirrel Girl", "cryptolith rite" — see the Hazel capture), so free
text is never mined for names (no NLP). A card-link embed, by contrast,
is the site's own exact name: :func:`parse_primer` collects them, the
sidecar records them in a marked block, and FP-018.3's auto-protect
trusts nothing else. Prose-only primers exist at every length (the
harvest has a 4.9k-char one with zero embeds) — for those the list is
honestly empty and adopt says protection is unavailable.

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

import hashlib
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
    "<!-- primer-card-links (exact names from the source's card-link "
    "embeds; FP-018.3 auto-protect input)"
)
_CARD_LINKS_CLOSE = "-->"
_CARD_LINKS_RE = re.compile(
    re.escape(_CARD_LINKS_OPEN) + r"\n(.*?)\n" + re.escape(_CARD_LINKS_CLOSE)
    + r"\n?",
    re.DOTALL,
)

# The sidecar's IDENTITY header (2026-09-03, R3 F-07). Before it, a
# sidecar's only identity was its filename stem, so deleting a deck in the
# web UI and importing a DIFFERENT deck whose name sanitized to the same
# stem explained (and auto-Protected) deck B with deck A's primer. The
# header records which source deck the words came from — the same
# namespaced id the ``.dck``'s ``Moxfield=``/``Archidekt=`` line carries
# (``deck_identity.deck_id_from_text``) — plus a hash of the raw
# ``description`` so a re-pull can tell "upstream changed" from "same
# words" (R3 F-08: an unchanged upstream no longer clobbers hand edits).
_SOURCE_OPEN = (
    "<!-- primer-source (which deck these words belong to; readers refuse "
    "a mismatch)"
)
_SOURCE_RE = re.compile(
    re.escape(_SOURCE_OPEN) + r"\n(.*?)\n" + re.escape(_CARD_LINKS_CLOSE)
    + r"\n?",
    re.DOTALL,
)
#: ``source=`` value written when the importing lane had no id to record.
SOURCE_UNKNOWN = "unknown"


@dataclass
class SidecarWrite:
    """Outcome of :func:`store_primer_sidecar`.

    ``action`` is one of ``written`` (new file), ``refreshed`` (existing
    sidecar for the SAME source overwritten because the upstream words
    changed), ``unchanged`` (same source, same words — file left alone,
    hand edits survive), ``refused`` (an existing sidecar records a
    DIFFERENT source: never clobbered, ``reason`` says which), ``empty``
    (nothing to write). ``path`` is the sidecar path except for
    ``empty``.
    """

    action: str
    path: Optional[Path] = None
    reason: str = ""


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
        # Valid JSON, not a Delta (a list, a number, ``null``, an alien
        # object): NOT primer text. It used to pass through verbatim, so
        # ``[1, 2, 3]`` became a sidecar claiming "this deck has a primer
        # and it says [1, 2, 3]" (2026-09-03, R3 F-14). Refusing yields
        # the honest "no primer" — a JSON literal is never the author's
        # words, and a drifted Archidekt shape is a capture to pin, not
        # a primer to guess at.
        return ParsedPrimer()
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


def _description_sha256(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def _source_header(source_id: Optional[str], description: str) -> str:
    return "\n".join([
        _SOURCE_OPEN,
        f"source={(source_id or '').strip() or SOURCE_UNKNOWN}",
        f"sha256={_description_sha256(description)}",
        _CARD_LINKS_CLOSE,
    ])


def _encode_link(name: str) -> str:
    """One card-link per line, JSON-encoded (R3 F-14): a name carrying a
    newline or the literal ``-->`` used to break the block's regex."""
    return json.dumps(name, ensure_ascii=False)


def _decode_link(line: str) -> Optional[str]:
    """Inverse of :func:`_encode_link`; a bare (pre-R3) line is taken
    verbatim so sidecars written before 2026-09-03 still read."""
    line = line.strip()
    if not line:
        return None
    if line.startswith('"'):
        try:
            value = json.loads(line)
        except ValueError:
            return line
        return value if isinstance(value, str) and value.strip() else None
    return line


def store_primer_sidecar(
    dck_path: Path,
    description: Optional[str],
    *,
    source_id: Optional[str] = None,
) -> SidecarWrite:
    """Parse ``description`` and store it beside ``dck_path``, with the
    identity header — the full-information writer the import lane uses.

    An empty/whitespace render NEVER creates a file — an empty
    ``.primer.md`` would read as "this deck has a primer and it says
    nothing", which is a different (and false) claim from "no primer".

    When the source carried card-link embeds they are recorded in the
    marked comment block :func:`read_primer_card_links` reads back —
    without it the one machine-trustworthy list of exact names would not
    survive the trip to disk (the text render flattens an embed into an
    ordinary word). No embeds, no block.

    OVERWRITE SEMANTICS (2026-09-03, R3 F-07/F-08 — the docstring used
    to promise "refuse-clobber" and mean only stem-following naming):

    * no sidecar on disk → ``written``;
    * existing sidecar whose header names the SAME source (or a pre-R3
      sidecar with no header, which cannot be told apart) → ``refreshed``
      when the upstream words changed (hash differs), ``unchanged`` when
      they did not — the file is left alone, so hand annotations survive
      until the author actually edits the primer upstream;
    * existing sidecar whose header names a DIFFERENT source → ``refused``:
      the file on disk is another deck's primer (a deleted deck's stem
      re-used by a colliding import) and is never overwritten; the caller
      prints the reason loudly.

    Write failures degrade loudly-but-nonfatally at the CALLER (the
    import already succeeded; a sidecar I/O error must not unwind it),
    so this function raises ``OSError`` and lets the import lane decide.
    """
    parsed = parse_primer(description)
    text = parsed.text.strip()
    if not text:
        return SidecarWrite(action="empty")
    out = primer_sidecar_path(Path(dck_path))
    incoming = (source_id or "").strip() or SOURCE_UNKNOWN
    new_hash = _description_sha256(description or "")
    existing = sidecar_identity(dck_path)
    if out.exists():
        if existing is not None:
            recorded = existing.get("source") or SOURCE_UNKNOWN
            if (recorded != SOURCE_UNKNOWN and incoming != SOURCE_UNKNOWN
                    and recorded != incoming):
                return SidecarWrite(
                    action="refused", path=out,
                    reason=(f"{out.name} records source {recorded!r}; this "
                            f"import is {incoming!r}. Left untouched — "
                            f"delete or rename it if it is stale."),
                )
            if existing.get("sha256") == new_hash and recorded == incoming:
                return SidecarWrite(action="unchanged", path=out)
        action = "refreshed"
    else:
        action = "written"
    blocks: list[str] = [_source_header(source_id, description or "")]
    if parsed.card_links:
        blocks.append("\n".join(
            [_CARD_LINKS_OPEN,
             *(_encode_link(n) for n in parsed.card_links),
             _CARD_LINKS_CLOSE]))
    blocks.append(text)
    out.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return SidecarWrite(action=action, path=out)


def write_primer_sidecar(dck_path: Path, description: Optional[str],
                         *, source_id: Optional[str] = None,
                         ) -> Optional[Path]:
    """Path-or-None view of :func:`store_primer_sidecar` (kept for the
    callers and tests that only ask "was a file written"). ``None`` for
    an empty render AND for a refused write — check
    :func:`store_primer_sidecar` when the two must be told apart."""
    result = store_primer_sidecar(dck_path, description, source_id=source_id)
    if result.action in ("written", "refreshed", "unchanged"):
        return result.path
    return None


def _strip_machine_blocks(raw: str) -> str:
    return _CARD_LINKS_RE.sub("", _SOURCE_RE.sub("", raw))


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
    text = _strip_machine_blocks(raw).strip()
    return text or None


def sidecar_identity(dck_path: Path) -> Optional[dict]:
    """The sidecar's identity header as ``{"source": ..., "sha256": ...}``,
    or ``None`` when there is no sidecar or it predates the header
    (2026-09-03, R3 F-07). Callers must treat ``None`` as "identity
    unknown", never as "different"."""
    try:
        raw = primer_sidecar_path(Path(dck_path)).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _SOURCE_RE.search(raw)
    if not m:
        return None
    out: dict = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out if "source" in out else None


def sidecar_identity_warning(dck_path: Path,
                             deck_text: Optional[str] = None,
                             ) -> Optional[str]:
    """A warning sentence when the sidecar beside ``dck_path`` records a
    source other than the deck's own ``Moxfield=``/``Archidekt=`` id, else
    ``None`` (2026-09-03, R3 F-07).

    ``None`` also for: no sidecar, a pre-R3 sidecar without a header,
    or a sidecar whose source was unknown at write time — in each case
    there is nothing to compare, and "cannot tell" must not read as
    "wrong deck". A deck that carries NO id while the sidecar names one
    IS a mismatch: the sidecar was copied or the deck's metadata was
    stripped, and either way the words cannot be confirmed as this
    deck's.
    """
    ident = sidecar_identity(dck_path)
    if ident is None:
        return None
    recorded = ident.get("source") or SOURCE_UNKNOWN
    if recorded == SOURCE_UNKNOWN:
        return None
    if deck_text is None:
        try:
            deck_text = Path(dck_path).read_text(encoding="utf-8")
        except OSError:
            return None
    from .deck_identity import deck_id_from_text
    deck_source = deck_id_from_text(deck_text)
    if deck_source == recorded:
        return None
    return (
        f"primer sidecar {primer_sidecar_path(Path(dck_path)).name} records "
        f"source {recorded!r} but this deck "
        + (f"is {deck_source!r}" if deck_source else
           "carries no Moxfield=/Archidekt= id")
        + " — the primer may belong to another deck; it is not used."
    )


def read_primer_card_links(dck_path: Path) -> list[str]:
    """The exact card names the sidecar's card-links block records.

    Empty when there is no sidecar OR the primer carried no embeds —
    callers that must tell those apart (adopt's "auto-protection
    unavailable" disclosure) check :func:`read_primer_sidecar` first.
    """
    try:
        raw = primer_sidecar_path(Path(dck_path)).read_text(encoding="utf-8")
    except OSError:
        return []
    m = _CARD_LINKS_RE.search(raw)
    if not m:
        return []
    out: list[str] = []
    for ln in m.group(1).splitlines():
        name = _decode_link(ln)
        if name and name not in out:
            out.append(name)
    return out


#: Word-bounded win vocabulary (2026-09-03, R3 F-12). The old scan was
#: ``"win" in paragraph`` — ``winds``, ``window``, ``Twincast``,
#: ``Growing`` and ``Loophole`` all quoted as "how it wins".
_WIN_KEYWORD_RE = re.compile(
    r"\b(?:wins?|winning|win[- ]?cons?|win[- ]?conditions?|combos?|"
    r"infinite|loops?)\b"
)
#: A heading-shaped line naming the win section ("Win Conditions", "How
#: it wins", "Combos", "Game plan"). The paragraph UNDER such a heading is
#: the author's win line even when no keyword recurs inside it.
_WIN_HEADING_RE = re.compile(
    r"^\W*(?:how (?:it|we|i|this deck) wins?|win(?:ning)?|"
    r"win[- ]?cons?|win[- ]?conditions?|combos?|infinite|game ?plan|"
    r"finishers?)\b",
    re.IGNORECASE,
)
_HEADING_MAX_WORDS = 6


def _is_heading_line(line: str) -> bool:
    """Short, punctuation-free, not a card-link list: a section title.

    Only the SHAPE of the line is consulted (2026-09-03, R3 F-12 —
    reconciled with the PR #85 rewrite's over-silencing, PR-03): there is
    no word list that turns an in-sentence "maybeboard" mention into a
    heading, so prose can never be silenced by what it happens to say.
    """
    t = line.strip()
    if not t or t.startswith("[[") or len(t.split()) > _HEADING_MAX_WORDS:
        return False
    return t[-1] not in ".!?,;" and not t.endswith("]]")


def quoted_win_lines(text: Optional[str], limit: int = 3) -> list[str]:
    """Paragraphs where the primer explains HOW IT WINS, verbatim.

    The harvest showed real primers carry step-by-step win lines (plus
    dated update logs and budget variants — deliberately NOT parsed
    structurally; out of scope). The adopt explanation QUOTES the
    author's own win-line paragraphs rather than paraphrasing them: a
    paraphrase of a combo line is exactly where a deterministic
    explainer would start inventing card behavior.

    A paragraph is a blank-line-separated block. When its first line is
    heading-shaped (:func:`_is_heading_line`) the heading names the
    section and the REST is the paragraph: a block under a win-section
    heading is quoted, a heading with nothing under it never is, and a
    body is otherwise quoted when it carries a word-bounded win keyword.
    First ``limit`` hits, each clipped with the standard marked
    truncation.
    """
    if not text:
        return []
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p:
            continue
        lines = p.splitlines()
        # A WIN heading only ever heads a MULTI-line block (Archidekt
        # renders "Win Conditions" and its paragraph in one block,
        # newline-joined): the body under it is quoted whole. Any other
        # first line — "A game winning combo is" in the Hazel capture —
        # is prose and stays in the paragraph. A one-line bare win title
        # ("Win Conditions" alone) is a title, not a win line.
        under_win_heading = (
            len(lines) > 1 and _is_heading_line(lines[0])
            and _WIN_HEADING_RE.match(lines[0]) is not None
        )
        if under_win_heading:
            body = "\n".join(lines[1:]).strip()
        elif (_is_heading_line(p) and len(p.split()) <= 3
              and _WIN_HEADING_RE.match(p)):
            continue
        else:
            body = p
        if body and (under_win_heading
                     or _WIN_KEYWORD_RE.search(body.casefold())):
            out.append(clip_for_prompt(body, 600))
            if len(out) >= limit:
                break
    return out


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
