"""FP-012 Slice A -- intent-learning for the deck-improvement agent.

``learn_intent(deck_path)`` composes the existing classifiers to
produce an ``Intent`` dataclass that captures what a deck is trying to
do -- its archetype, themes, key win-conditions, and commander color
identity.  The result is used by ``run_improve_loop`` to:

  1. **Soft-bias** the advisor's candidate adds toward the intent's
     themes (lower-priority signal -- the win-margin objective stays
     primary).
  2. **Auto-protect** the intent's key win-cons / signature synergy
     pieces by extending the per-round protected-card list so the
     curator can't accidentally cut the deck's identity.

Why soft-bias + protect, not a hard constraint (reject swaps that
change archetype): hard constraints risk stalling the loop on noisy
sims; soft-bias + protect keeps the optimizer in control while giving
the intent a meaningful voice.  See ``docs/archive/fp012-next-slices.md``
(Slice A design decision).

Callers
-------
- ``improve.py`` threads ``Intent`` through ``run_improve_loop`` and
  appends ``intent.key_wincons`` to the per-round protected list.
- ``improve_main`` exposes ``--learn-intent <dck>`` to the CLI.
- Tests inject a stub ``classify_fn`` / ``themes_fn`` / ``lookup_fn``
  so no real Forge / Anthropic / Scryfall is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import dck_utils


@dataclass
class Intent:
    """Captured intent for one deck.

    Attributes
    ----------
    archetype:
        One of ``aggro | midrange | control | combo | stax``
        (from ``archetype.classify``).
    themes:
        EDHREC tag slugs that the deck strongly cares about
        (from ``staples.detect_themes``). Up to 3.
    key_wincons:
        Card names detected as win-conditions or high-synergy
        pieces via ``staples.classify_role_extended``.  These are
        added to the per-round protected-card list.
    color_identity:
        WUBRG letter list for the primary commander, e.g.
        ``["W", "U"]``.  Empty list when no commander is found or
        Scryfall is unavailable.
    tribal_type:
        Commander's primary tribal type (e.g. ``"Goblin"``), or
        ``None`` for non-tribal decks.
    commander_name:
        Canonical name of the primary commander card, or ``None``.
    stated:
        FP-018.2 — the deck's OWN primer text (rendered from the
        source's Quill Delta by ``primer.render_quill_delta``), or
        ``None`` when the deck has none. Free text: it steers
        ATTENTION in the judge prompt and soft-biases the advisor,
        but never invents card facts and never drives swap-direction
        labeling (see ``_deck_judge_prompt.classify_swap_direction``).
    pilot_preferences:
        FP-018.2 — the adopting player's own words about what they
        like doing, or ``None``. Same free-text contract as
        ``stated``.
    """

    archetype: str = "midrange"
    themes: list[str] = field(default_factory=list)
    key_wincons: list[str] = field(default_factory=list)
    color_identity: list[str] = field(default_factory=list)
    tribal_type: Optional[str] = None
    commander_name: Optional[str] = None
    stated: Optional[str] = None
    pilot_preferences: Optional[str] = None

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_deck_text(deck_path: Path) -> str:
    """Read a .dck file, returning '' on any I/O error."""
    try:
        return dck_utils.read_deck_text(deck_path)
    except OSError:
        return ""


def _parse_commander_names(deck_text: str) -> list[str]:
    """Extract card names from the [Commander] section of a .dck file.

    Thin wrapper over ``dck_utils.section_card_names``."""
    return dck_utils.section_card_names(deck_text, "Commander")


def _parse_main_card_names(deck_text: str) -> list[str]:
    """Extract card names from the [Main] section of a .dck file.

    Thin wrapper over ``dck_utils.main_card_names``."""
    return dck_utils.main_card_names(deck_text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def learn_intent(
    deck_path: Path,
    *,
    # Injectable classifiers for tests (default to the real implementations).
    classify_fn: Optional[Callable[[Path], str]] = None,
    themes_fn: Optional[Callable[[list[tuple[str, str]]], list[str]]] = None,
    lookup_fn: Optional[Callable[[str], Optional[dict]]] = None,
    role_fn: Optional[Callable[[str, str], str]] = None,
    tribal_fn: Optional[Callable[[str, str], Optional[str]]] = None,
    pilot_preferences: Optional[str] = None,
    read_primer: bool = True,
) -> Intent:
    """Compose existing classifiers to learn a deck's intent.

    FREE TEXT (2026-09-03, R3 F-01). ``stated`` is filled from the deck's
    own ``<stem>.primer.md`` sidecar (``primer.read_primer_sidecar``) —
    the production writer FP-018.2 shipped without: every caller of this
    function (``commander judge``, ``commander improve``) used to get
    ``stated=None`` no matter what was on disk, so the judge's free-text
    block and the advisor's free-text bias were dead on every production
    path. A sidecar whose recorded source does not match the deck's
    ``Moxfield=``/``Archidekt=`` id is NOT read (R3 F-07: a deck must
    never be judged against another deck's primer); ``read_primer=False``
    skips the sidecar entirely. ``pilot_preferences`` is the adopting
    player's own words, threaded from ``--preferences`` on the CLIs.

    Steps
    -----
    1. Archetype: ``archetype.classify`` (filename hint -> content scan
       -> midrange fallback).
    2. Themes: ``staples.detect_themes`` over the deck's oracle texts.
    3. Win-cons: scan each card in [Main] with
       ``staples.classify_role_extended``; cards that return
       ``"win_condition"`` or ``"finisher"`` become ``key_wincons``.
    4. Color identity: Scryfall lookup of the primary commander.
    5. Tribal type: ``staples.detect_tribal_type`` on the commander's
       oracle text.

    All injectable so tests never need real Forge / Anthropic /
    Scryfall.  The default implementations are imported lazily to keep
    the module importable even when optional extras are absent.

    Parameters
    ----------
    deck_path:
        Absolute path to a local ``.dck`` file.
    classify_fn:
        ``(deck_path) -> archetype_str`` -- defaults to
        ``archetype.classify``.
    themes_fn:
        ``(list[(name, oracle_text)]) -> list[str]`` -- defaults to
        ``staples.detect_themes``.
    lookup_fn:
        ``(card_name) -> Optional[dict]`` -- defaults to
        ``scryfall_client.lookup_card``.  Returns the Scryfall card
        object or ``None``/exception on failure.
    role_fn:
        ``(oracle_text, type_line) -> role_str`` -- defaults to
        ``staples.classify_role_extended``.
    tribal_fn:
        ``(oracle_text, type_line) -> Optional[str]`` -- defaults to
        ``staples.detect_tribal_type``.
    """
    # ------------------------------------------------------------------
    # 1. Resolve real implementations (lazy imports keep startup fast).
    # ------------------------------------------------------------------
    if classify_fn is None:
        from .archetype import classify as _classify
        classify_fn = _classify
    if themes_fn is None:
        from .staples import detect_themes as _detect_themes
        themes_fn = _detect_themes
    if lookup_fn is None:
        try:
            from .scryfall_client import lookup_card as _lookup
            lookup_fn = _lookup
        except Exception:  # noqa: BLE001
            def _no_lookup(name: str) -> Optional[dict]:
                return None
            lookup_fn = _no_lookup
    if role_fn is None:
        from .staples import classify_role_extended as _role
        role_fn = _role
    if tribal_fn is None:
        from .staples import detect_tribal_type as _tribal
        tribal_fn = _tribal

    # ------------------------------------------------------------------
    # 2. Read the deck file.
    # ------------------------------------------------------------------
    deck_text = _read_deck_text(deck_path)
    main_cards = _parse_main_card_names(deck_text)
    commander_names = _parse_commander_names(deck_text)
    commander_name: Optional[str] = commander_names[0] if commander_names else None

    # ------------------------------------------------------------------
    # 3. Archetype classification.
    # ------------------------------------------------------------------
    try:
        archetype = classify_fn(deck_path)
    except Exception:  # noqa: BLE001
        archetype = "midrange"

    # ------------------------------------------------------------------
    # 4. Theme detection -- needs oracle texts.  Fetch each card lazily;
    #    skip cards that fail Scryfall lookup (offline / unknown names).
    # ------------------------------------------------------------------
    # Capture the type_line in the same pass — step 5 below needs it, and a
    # second lookup_fn per card doubled the lookup count (a real cost when
    # the lookup falls through to disk or network).
    deck_cards: list[tuple[str, str, str]] = []
    for card_name in main_cards:
        try:
            data = lookup_fn(card_name)
            oracle = (data.get("oracle_text") or "") if data else ""
            type_line = (data.get("type_line") or "") if data else ""
        except Exception:  # noqa: BLE001
            oracle = ""
            type_line = ""
        deck_cards.append((card_name, oracle, type_line))
    deck_oracles: list[tuple[str, str]] = [
        (name, oracle) for name, oracle, _tl in deck_cards
    ]

    try:
        themes = themes_fn(deck_oracles)
    except Exception:  # noqa: BLE001
        themes = []

    # ------------------------------------------------------------------
    # 5. Win-con detection -- any main-deck card whose extended role is
    #    "win_condition" or "finisher" is a key win-con to protect.
    # ------------------------------------------------------------------
    key_wincons: list[str] = []
    for card_name, oracle, type_line in deck_cards:
        if not oracle:
            continue
        try:
            role = role_fn(oracle, type_line)
        except Exception:  # noqa: BLE001
            role = "other"
        if role in ("win_condition", "finisher"):
            key_wincons.append(card_name)

    # ------------------------------------------------------------------
    # 6. Color identity -- Scryfall lookup of the primary commander.
    # ------------------------------------------------------------------
    color_identity: list[str] = []
    tribal_type: Optional[str] = None
    if commander_name:
        try:
            cmd_data = lookup_fn(commander_name)
            if cmd_data:
                color_identity = list(cmd_data.get("color_identity") or [])
                # 7. Tribal type from commander oracle text.
                cmd_oracle = cmd_data.get("oracle_text") or ""
                cmd_type = cmd_data.get("type_line") or ""
                try:
                    tribal_type = tribal_fn(cmd_oracle, cmd_type)
                except Exception:  # noqa: BLE001
                    tribal_type = None
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 8. Free text (R3 F-01): the deck's own primer sidecar, if any and if
    #    it belongs to THIS deck; plus the pilot's words from the caller.
    # ------------------------------------------------------------------
    stated: Optional[str] = None
    if read_primer:
        from . import primer as _primer
        if _primer.sidecar_identity_warning(deck_path, deck_text) is None:
            stated = _primer.read_primer_sidecar(deck_path)
    prefs = (pilot_preferences or "").strip() or None

    return Intent(
        archetype=archetype,
        themes=themes,
        key_wincons=key_wincons,
        color_identity=color_identity,
        tribal_type=tribal_type,
        commander_name=commander_name,
        stated=stated,
        pilot_preferences=prefs,
    )


# ---------------------------------------------------------------------------
# FP-018.2 — free text as a SOFT theme signal (2026-08-27; matching rewritten
# 2026-09-03, R3 F-04)
# ---------------------------------------------------------------------------

#: Prose pattern -> EDHREC tag slug. The slugs are EXACTLY the vocabulary
#: of ``staples._THEME_PATTERNS`` — the advisor fetches ``/tags/<slug>``
#: pages for whatever this yields, so an invented slug would be a dead
#: fetch, and a slug outside the deck-detection vocabulary would let free
#: text claim themes the rest of the app cannot recognize. Patterns are
#: WORD-BOUNDED regexes over casefolded prose (R3 F-04: the old table was
#: bare substring containment — ``token`` matched inside any word, and
#: ``enchantment`` fired on Baba Lysaga's "Giving an enchantment creature
#: a 3rd type"). Stems (``sacrific``, ``reanimat``) keep the prose
#: inflections the table was built for: "I like sacrificing creatures"
#: has to hit ``sacrifice`` even though no oracle regex would.
_FREE_TEXT_THEME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\btokens?\b", "tokens"),
    (r"\bspellslingers?\b", "spellslinger"),
    (r"\binstants? and sorcer(?:y|ies)\b", "spellslinger"),
    (r"\bsacrific\w*", "sacrifice"),           # sacrifice / sacrificing
    (r"\baristocrats?\b", "sacrifice"),
    (r"\bdeath triggers?\b", "sacrifice"),
    (r"\+1/\+1", "plus-1-plus-1-counters"),
    (r"\blandfall\b", "landfall"),
    (r"\blands matter\b", "landfall"),
    (r"\blifegain\b", "lifegain"),
    (r"\bgain(?:ing)? life\b", "lifegain"),
    (r"\breanimat\w*", "reanimator"),          # reanimate / reanimator
    (r"\bgraveyard\b", "reanimator"),
    (r"\bequipment\b", "equipment"),
    (r"\bvoltron\b", "equipment"),
    # Plural, or the singular with a theme word: "an artifact creature" /
    # "an enchantment creature" is a card description, not a preference.
    (r"\bartifacts\b|\bartifact (?:deck|theme|synerg\w*|strateg\w*)",
     "artifacts"),
    (r"\benchantress\b", "enchantress"),
    (r"\benchantments\b|\benchantment (?:deck|theme|synerg\w*|strateg\w*)",
     "enchantress"),
)

#: Backward-compatible view of the table for the vocabulary test
#: (``tests/test_intent.py`` checks every emitted slug is a known theme).
_FREE_TEXT_THEME_KEYWORDS: tuple[tuple[str, str], ...] = tuple(
    (pat, slug) for pat, slug in _FREE_TEXT_THEME_PATTERNS
)

#: Negation cues (R3 F-04). A theme mention is DROPPED when one of these
#: appears within ``_NEGATION_WINDOW`` words before it, or one of the
#: post-cues within the window after it, inside the same clause. "no
#: tokens please", "not a lifegain deck", "I dislike graveyard
#: strategies", "spellslinger is boring", and the real Baba Lysaga
#: sentence "Lifegain is brutal against this deck" all read as the
#: pilot/author saying what the deck is NOT. Preferences are read as
#: AFFIRMATIVE keywords; a negated mention simply contributes nothing
#: (there is no "anti-theme" channel to steer away from — see
#: ``adopt.main``'s ``--preferences`` help).
_NEGATION_PRE = re.compile(
    r"\b(?:no|not|never|nothing|without|avoid\w*|hate\w*|dislike\w*|"
    r"don'?t|doesn'?t|isn'?t|aren'?t|can'?t|won'?t|wouldn'?t|"
    r"against|anti|tired of|sick of|bored of|skip\w*|drop\w*|zero|"
    r"afraid of|scared of|fear\w*|weak to|lose\w* to)\b"
)
_NEGATION_POST = re.compile(
    r"\b(?:against|boring|bad|weak|nothing|not (?:my|for me|fun|"
    r"interesting)|isn'?t (?:my|for me)|aren'?t (?:my|for me)|"
    r"is brutal|are brutal)\b"
)
_NEGATION_WINDOW = 4
_CLAUSE_SPLIT = re.compile(r"[.;!?\n]+")


def _mention_is_negated(clause: str, start: int, end: int) -> bool:
    """Is the match at ``clause[start:end]`` inside a negation window?"""
    before = clause[:start].split()[-_NEGATION_WINDOW:]
    after = clause[end:].split()[:_NEGATION_WINDOW + 1]
    if _NEGATION_PRE.search(" ".join(before)):
        return True
    return bool(_NEGATION_POST.search(" ".join(after)))


def free_text_theme_slugs(text: Optional[str]) -> list[str]:
    """Theme slugs a piece of FREE TEXT (primer / pilot preferences)
    talks about AFFIRMATIVELY. Word-bounded match, negation-aware,
    deduped, first-mention order (table order).

    Deliberately a pattern table and not ``staples.card_theme_slugs``:
    that function's regexes are tuned to ORACLE wording ("sacrifice a
    creature", "create ... token") and mostly miss prose. The slugs the
    two produce are the same vocabulary, so downstream consumers cannot
    tell the signals apart — which is the point: free text gets a voice
    in the SAME channel themes already use, not a new one.
    """
    if not text:
        return []
    folded = text.casefold()
    out: list[str] = []
    for pattern, slug in _FREE_TEXT_THEME_PATTERNS:
        if slug in out:
            continue
        rx = re.compile(pattern)
        for clause in _CLAUSE_SPLIT.split(folded):
            hit = False
            for m in rx.finditer(clause):
                if not _mention_is_negated(clause, m.start(), m.end()):
                    hit = True
                    break
            if hit:
                out.append(slug)
                break
    return out


#: How many free-text slugs may ride along with the derived themes (R3
#: F-03). The advisor fetches at most 4 derived tag pages (intent themes
#: + tribe + detected); free-text pages are fetched IN ADDITION, never in
#: place of them, and this cap bounds that addition (each page is an
#: HTTP round-trip on a cold cache).
FREE_TEXT_SLUG_CAP = 2


def free_text_bias_slugs(intent: Optional["Intent"]) -> list[str]:
    """Slugs the intent's FREE TEXT adds beyond its derived themes, capped
    at :data:`FREE_TEXT_SLUG_CAP`. This is the advisor's
    ``--free-text-themes`` input: pages fetched for these are ADDITIVE
    (they never evict the tribe or a derived theme page) and never feed
    cut-protection — see ``improvement_advisor._fetch_tag_pages_lazy``.
    """
    if intent is None:
        return []
    derived = {s.strip() for s in (intent.themes or []) if s.strip()}
    out: list[str] = []
    for text in (intent.stated, intent.pilot_preferences):
        for s in free_text_theme_slugs(text):
            if s not in derived and s not in out:
                out.append(s)
    return out[:FREE_TEXT_SLUG_CAP]


def soft_bias_theme_slugs(intent: Optional["Intent"]) -> list[str]:
    """The combined soft-bias slug list: structured themes first, then the
    (capped) slugs the intent's free text mentions.

    Order matters — earlier slugs get the louder voice. The DERIVED
    themes (evidence from the actual list) stay ahead of what the free
    text merely says. Soft by construction for the derived half; the
    free-text half is soft only because the advisor fetches it as EXTRA
    pages (``free_text_bias_slugs``) — ``improve`` passes the two halves
    through two flags precisely so the advisor can tell them apart (R3
    F-03: as one list, three derived themes plus one free-text slug
    filled the 4-page cap and evicted the tribe page, whose cards feed
    cut-protection).
    """
    if intent is None:
        return []
    out: list[str] = []
    for slug in list(intent.themes or []):
        s = slug.strip()
        if s and s not in out:
            out.append(s)
    for s in free_text_bias_slugs(intent):
        if s not in out:
            out.append(s)
    return out


def resolve_preferences(text: Optional[str],
                        file_path: Optional[str] = None) -> Optional[str]:
    """The ``--preferences`` / ``--preferences-file`` pair, resolved the
    same way on every CLI that offers it (adopt / judge / improve — R3
    F-01). The file wins when given. Raises ``OSError`` for an unreadable
    file so the caller can print its own clean error; blank text is
    ``None``."""
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
    text = (text or "").strip()
    return text or None


def intent_protect_cards(intent: Optional["Intent"]) -> list[str]:
    """Extract the protect-list extension implied by ``intent``.

    Returns ``intent.key_wincons`` when an intent is present, else an
    empty list.  Used by ``improve.py`` to extend the per-round
    ``--protect`` list without coupling the loop logic to the ``Intent``
    internals.
    """
    if intent is None:
        return []
    return list(intent.key_wincons)
