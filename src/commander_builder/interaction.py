"""Interaction COVERAGE — what a deck can actually answer, not how much.

``staples.ROLE_TARGETS`` says ``removal: 8``. A deck with eight creature
kill spells and nothing else clears that target, scores a perfect 100 on
the role component of the health grade, and then loses to the first
Smothering Tithe it sees. Counting interaction is not the same as
characterizing it: in multiplayer Commander the question is not "how
many answers" but "which of the five things that beat me can I answer,
and can I do it on my opponents' turns?"

So this module produces a MATRIX, not a count:

  * ``creature_removal``    — spot answers to a creature;
  * ``artifact_enchantment``— answers to a resolved noncreature permanent
    (the Rhystic Study / Smothering Tithe / Winter Orb class, which the
    ``removal`` bucket never distinguished);
  * ``graveyard_hate``      — answers to a graveyard engine, the one
    resource no amount of creature removal touches;
  * ``stack``               — counterspells: the only answer to a card
    that says "you win the game" on resolution;
  * ``board_wipe``          — mass answers (kept as its own row, mirroring
    ``ROLE_TARGETS["wipe"]``).

Plus the row that changes how all of the above PLAY: the INSTANT-SPEED
SHARE. Eight sorcery-speed removal spells and eight instants are the
same number in every existing signal and completely different decks —
the sorcery deck must tap out on its own turn and can never punish an
opponent's threat the turn it lands. The type line needed to tell them
apart was already being parsed for other signals; nothing read it for
this.

MINIMUMS VARY BY BRACKET. A precon-level B2 pod does not need graveyard
hate maindeck; a B4 pod resolves an Underworld Breach on turn four. All
of the per-bracket numbers live in ONE documented dict,
``BRACKET_INTERACTION_MINIMUMS`` — see it for the reasoning per row.

TWO CLASSIFIERS, ONE FALLBACK (the honesty note).
=================================================
The preferred classifier is the FORGE CARD SCRIPT.
``forge_script_parser.CardScript.abilities[].effect`` is the actual
effect primitive Forge executes — ``Destroy``, ``ChangeZone``,
``Counter``, ``DestroyAll`` — together with the structured
``ValidTgts$`` / ``Origin$`` / ``Destination$`` parameters. That is a
machine-readable statement of what the card DOES, it is fully offline,
and it does not care how the oracle text is templated this year. It is
strictly more reliable than regex over prose.

The fallback is oracle-text regex, in the same shape (and for wipes, the
literal same patterns) as ``staples._ROLE_PATTERNS``. It runs when:

  * no Forge corpus is installed (``vendor/forge`` absent — the common
    case for a fresh checkout, and the reason this module can never
    REQUIRE Forge);
  * the corpus doesn't ship that card (new set, custom card, typo);
  * the parsed script produced NO verdict. ``forge_script_parser``
    deliberately does not expand SVars (see its "NOT in scope" list), so
    a card whose real work happens inside an SVar-expanded sub-ability
    can parse fine and still look inert. An empty Forge verdict therefore
    means "no opinion", not "no interaction", and defers to the regex —
    Forge only ever ADDS precision here, it never silently removes a
    card from the matrix.

Every entry point takes injected ``lookup`` / ``loader`` callables, so
the whole module is exercised offline in both configurations.

Unavailability follows the ``deck_health`` contract: when more than half
the deck can't be classified by EITHER path, ``interaction_report``
returns ``None`` — never a fabricated all-zero matrix, which would read
as "this deck has no interaction" on a deck we simply couldn't see.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, Optional

from .staples import _ROLE_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_FORGE = REPO_ROOT / "vendor" / "forge"  # mirrors forge_runner's path

# The coverage matrix's rows, in report order (most-common answer first).
INTERACTION_CATEGORIES: tuple[str, ...] = (
    "creature_removal",
    "artifact_enchantment",
    "graveyard_hate",
    "stack",
    "board_wipe",
)

# Human labels for the gap messages. Kept next to the category tuple so a
# new row can't be added without one.
_CATEGORY_LABELS: dict[str, str] = {
    "creature_removal": "Creature removal",
    "artifact_enchantment": "Artifact/enchantment answers",
    "graveyard_hate": "Graveyard hate",
    "stack": "Stack interaction (counterspells)",
    "board_wipe": "Board wipes",
}

# What each gap actually costs you, in one clause. The audit surfaces
# these verbatim, so they name the failure rather than restating the
# count ("you have 0 of 2" is not advice).
_CATEGORY_CONSEQUENCE: dict[str, str] = {
    "creature_removal": "no answer to a commander that must die on sight",
    "artifact_enchantment": (
        "a resolved Smothering Tithe / Rhystic Study stays resolved"
    ),
    "graveyard_hate": (
        "a reanimator or Underworld Breach deck rebuilds unopposed"
    ),
    "stack": "nothing stops a spell that reads 'you win the game'",
    "board_wipe": "no reset once a board gets ahead of you",
}

# ---------------------------------------------------------------------------
# THE TUNING DICT — per-bracket per-category minimums.
# ---------------------------------------------------------------------------
#
# Keys are WotC bracket numbers 1-5 as used everywhere else in this repo
# (see bracket_estimator's rule transcription). Values are the MINIMUM
# count for each row of the matrix, plus ``instant_speed_share`` — a
# FRACTION (0..1) of the deck's interaction that must be castable at
# instant speed, not a card count.
#
# WHERE THE NUMBERS COME FROM. The bracket-3 column is the reference
# column and is transcribed from the coverage-matrix table in
# docs/archive/REVIEW-2026-07-24.md
# (creature 4, artifact/enchantment 2, graveyard 1, stack 0, wipe 2,
# instant share 40%); the others are that column moved along the two
# axes that actually change with bracket:
#
#   * WHAT THE POD PLAYS. B1/B2 are precon-level: the threats are
#     creatures and the occasional value engine, so the creature and
#     artifact/enchantment rows carry the weight and graveyard hate is
#     optional (B1) or a single copy (B2). By B4 an unanswered
#     Underworld Breach or Bolas's Citadel ends the game, so graveyard
#     hate becomes 2 and stack interaction stops being optional.
#   * HOW FAST IT ENDS. Stack interaction is 0 at B1-B3 on purpose:
#     counterspells are a blue tax, and at those brackets you get another
#     turn to answer a resolved permanent. At B4/B5 you frequently do
#     not, which is also why the instant-speed share climbs — sorcery-
#     speed answers are dead cards against a combo that goes off on
#     someone else's turn. B5 (cEDH) inverts the wipe row: the format is
#     fast enough that mass removal is usually too slow, and the slots go
#     to permission instead.
#
# These are FLOORS for a well-rounded list, not rules — an archetype can
# defensibly miss one (a stax deck answers artifacts with a lock, not
# with Naturalize). The report states gaps; it doesn't fail a deck.
BRACKET_INTERACTION_MINIMUMS: dict[int, dict[str, float]] = {
    1: {"creature_removal": 2, "artifact_enchantment": 1,
        "graveyard_hate": 0, "stack": 0, "board_wipe": 1,
        "instant_speed_share": 0.20},
    2: {"creature_removal": 3, "artifact_enchantment": 2,
        "graveyard_hate": 1, "stack": 0, "board_wipe": 2,
        "instant_speed_share": 0.30},
    3: {"creature_removal": 4, "artifact_enchantment": 2,
        "graveyard_hate": 1, "stack": 0, "board_wipe": 2,
        "instant_speed_share": 0.40},
    4: {"creature_removal": 5, "artifact_enchantment": 3,
        "graveyard_hate": 2, "stack": 2, "board_wipe": 2,
        "instant_speed_share": 0.50},
    5: {"creature_removal": 4, "artifact_enchantment": 3,
        "graveyard_hate": 2, "stack": 6, "board_wipe": 1,
        "instant_speed_share": 0.60},
}

#: Bracket used when the caller doesn't say — the repo's default
#: "upgraded deck" assumption, matching the audit route's bracket=3.
DEFAULT_BRACKET = 3


def clamp_bracket(bracket: Optional[int]) -> int:
    """The bracket the minimums are actually read from, clamped to 1-5.

    An out-of-range or unparseable bracket clamps rather than raising:
    this feeds an audit panel, and a weird ``?bracket=`` query param must
    not cost the user the whole signal. The report echoes the clamped
    value so the substitution is visible rather than silent.
    """
    try:
        b = int(bracket)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        b = DEFAULT_BRACKET
    return max(1, min(5, b))


def minimums_for_bracket(bracket: Optional[int]) -> dict[str, float]:
    """Per-category minimums for ``bracket`` (see ``clamp_bracket``)."""
    return dict(BRACKET_INTERACTION_MINIMUMS[clamp_bracket(bracket)])


# ---------------------------------------------------------------------------
# Classifier 1 (preferred) — Forge card scripts.
# ---------------------------------------------------------------------------
#
# Forge effect primitives, from the DSL reference in
# ``forge_script_parser`` and the corpus histogram
# ``deck_library_analyzer`` builds. ``AB$``/``SP$`` categories share the
# same effect vocabulary, so we dispatch on the effect alone.

# Mass effects — the board_wipe row.
_FORGE_MASS_EFFECTS: frozenset[str] = frozenset({
    "DestroyAll", "DamageAll", "ChangeZoneAll",
})
# Single-target answers. ChangeZone covers both exile (Swords) and bounce
# (Cyclonic Rift's targeted mode); Origin$ Battlefield is what makes it an
# ANSWER rather than a tutor or a reanimation spell.
_FORGE_SPOT_EFFECTS: frozenset[str] = frozenset({
    "Destroy", "ChangeZone", "DealDamage",
})
_FORGE_COUNTER_EFFECTS: frozenset[str] = frozenset({"Counter"})

# Parameters whose values name what an ability can hit. Forge is not
# perfectly consistent about which one a given card uses, so we search
# them all as one blob.
_FORGE_TARGET_PARAMS: tuple[str, ...] = (
    "ValidTgts", "ValidCards", "ChangeType", "Defined", "TargetType",
    "ValidDescription", "ValidCard",
)


def _forge_target_blob(params: dict) -> str:
    return " ".join(
        str(params.get(key, "")) for key in _FORGE_TARGET_PARAMS
    )


def forge_categories(script) -> set[str]:
    """Interaction categories a parsed Forge ``CardScript`` proves.

    Walks the parent face AND every alternate face, so a DFC whose back
    side is the removal spell is classified from it.

    Returns an EMPTY set for "no verdict" as well as for "genuinely no
    interaction" — the two are indistinguishable from a non-SVar-expanding
    parse, which is exactly why the caller treats empty as "defer to the
    oracle regex" (see the module docstring).
    """
    cats: set[str] = set()
    if script is None:
        return cats
    for face in (script, *(getattr(script, "faces", None) or [])):
        for ability in getattr(face, "abilities", []) or []:
            params = getattr(ability, "params", {}) or {}
            effect = getattr(ability, "effect", "") or ""
            origin = str(params.get("Origin", ""))
            destination = str(params.get("Destination", ""))
            raw = str(getattr(ability, "raw", "") or "")
            targets = _forge_target_blob(params)

            # Graveyard answers first: an exile-from-graveyard is a
            # ChangeZone/ChangeZoneAll like any other, and reading it as
            # spot removal or a board wipe would be plainly wrong.
            if "Graveyard" in origin and "Exile" in destination:
                cats.add("graveyard_hate")
                continue
            # Rest in Peace class: a replacement effect that redirects
            # what would hit a graveyard into exile.
            if ability.kind == "R" and "Graveyard" in raw and "Exile" in raw:
                cats.add("graveyard_hate")
                continue
            if effect in _FORGE_COUNTER_EFFECTS:
                cats.add("stack")
                continue
            if effect in _FORGE_MASS_EFFECTS:
                if effect == "ChangeZoneAll" and "Battlefield" not in origin:
                    continue  # mass mill / mass recursion, not a wipe.
                cats.add("board_wipe")
                continue
            if effect in _FORGE_SPOT_EFFECTS:
                if effect == "ChangeZone" and "Battlefield" not in origin:
                    continue  # tutor / reanimation, not an answer.
                if "Creature" in targets or "Permanent" in targets:
                    cats.add("creature_removal")
                if any(
                    word in targets
                    for word in ("Artifact", "Enchantment", "Permanent")
                ):
                    cats.add("artifact_enchantment")
    return cats


def _default_loader():
    """A ``CardsLoader`` over the vendored Forge corpus, or None.

    Fail-quiet by design: no ``vendor/forge`` (a fresh checkout, CI, most
    user machines) simply means the oracle-regex classifier does all the
    work. Never raises — an absent optional corpus is not an error.
    """
    try:
        from .forge_cards_loader import CardsLoader
        return CardsLoader.locate(VENDOR_FORGE)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Classifier 2 (fallback) — oracle-text regex.
# ---------------------------------------------------------------------------
#
# Same conventions as ``staples._ROLE_PATTERNS``: lowercase oracle text,
# ``re.search``, patterns kept narrow enough that a false positive is
# visible in the card list the report emits.

# NOTE the ``\b`` before every type word. Without it "destroy target
# NONcreature permanent" reads as creature removal and "destroy target
# NONartifact creature" (Go for the Throat) reads as an artifact answer --
# both false positives that would paper over exactly the gap this module
# exists to surface. ``\b`` cannot match inside "noncreature", so the
# negation comes for free.
_CREATURE_REMOVAL_PATTERNS: tuple[str, ...] = (
    r"destroy target[^.]{0,40}\bcreature",
    # ``(?![^.]*graveyard)`` keeps Scavenging Ooze ("exile target
    # creature card FROM A GRAVEYARD") in the graveyard row only -- it
    # answers a graveyard, not a creature on the battlefield.
    r"exile target(?![^.]*graveyard)[^.]{0,40}\bcreature",
    # ``(?!...noncreature)`` keeps "destroy target NONcreature permanent"
    # (Vandalblast-adjacent templating) out of the creature row while
    # leaving it in the artifact/enchantment row below, where it belongs.
    r"destroy target(?![^.]*\bnoncreature\b)[^.]{0,30}\bpermanent",
    r"exile target(?![^.]*\bnoncreature\b)[^.]{0,30}\bpermanent",
    r"owner of target[^.]{0,20}\bpermanent shuffles it",  # Chaos Warp class
    r"target creature gets -\d+/-\d+",
    r"deals \d+ damage to target creature",
    r"deals \d+ damage to any target",
    r"return target creature[^.]{0,40}owner'?s hand",
    # Edicts -- the answer to a hexproof commander. Narrow to the
    # templated "<each|target> <player|opponent> sacrifices" so an
    # aristocrats payoff ("whenever you sacrifice a creature") doesn't
    # register as interaction.
    r"(?:each|target) (?:opponent|player) sacrifices a creature",
)

_ARTIFACT_ENCHANTMENT_PATTERNS: tuple[str, ...] = (
    r"destroy target[^.]{0,40}\b(?:artifact|enchantment)",
    r"exile target(?![^.]*graveyard)[^.]{0,40}\b(?:artifact|enchantment)",
    r"destroy target[^.]{0,30}\bpermanent",
    r"exile target[^.]{0,30}\bpermanent",
    r"owner of target[^.]{0,20}\bpermanent shuffles it",
    r"destroy all \bartifacts",
    r"destroy all \benchantments",
    r"return target[^.]{0,30}\b(?:artifact|enchantment)[^.]{0,40}owner'?s hand",
)

# Deliberately templated rather than a loose "exile ... graveyard":
# escape / flashback / delve cards all mention exiling cards from YOUR
# graveyard as a COST, and counting those as hate would tell a dredge
# deck it is well defended against dredge decks.
_GRAVEYARD_HATE_PATTERNS: tuple[str, ...] = (
    r"exile target player'?s graveyard",
    r"exile [^.]{0,40}\bfrom (?:a|target player'?s|each|all|their) graveyards?",
    r"exile all (?:cards|creature cards)[^.]{0,40}graveyards?",
    r"exile (?:each|all) (?:opponents?'? )?graveyards?",
    r"would be put into a graveyard[^.]{0,60}exile it instead",
    r"cards? in graveyards? can'?t",
    r"exile target card from a graveyard",
)

_STACK_PATTERNS: tuple[str, ...] = (
    r"counter target spell",
    r"counter target[^.]{0,40}(?:spell|ability)",
    r"counter it unless",
)

# Board wipes reuse ``staples``' OWN wipe patterns verbatim rather than
# growing a second, drifting copy: those regexes carry a trail of live
# audit fixes (Cyclonic Rift's overload paragraph, Crux of Fate's typed
# sweep, Toxic Deluge's -X/-X) that this module has no business
# re-deriving. If a wipe is added there it appears here for free.
_WIPE_PATTERNS: tuple[str, ...] = tuple(
    pattern
    for role, patterns in _ROLE_PATTERNS if role == "wipe"
    for pattern, _type_req, _score in patterns
)

_PATTERNS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "creature_removal": _CREATURE_REMOVAL_PATTERNS,
    "artifact_enchantment": _ARTIFACT_ENCHANTMENT_PATTERNS,
    "graveyard_hate": _GRAVEYARD_HATE_PATTERNS,
    "stack": _STACK_PATTERNS,
    "board_wipe": _WIPE_PATTERNS,
}

_COMPILED: dict[str, tuple] = {
    category: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for category, patterns in _PATTERNS_BY_CATEGORY.items()
}


def oracle_categories(oracle_text: str, type_line: str = "") -> set[str]:
    """Interaction categories from oracle text — the documented fallback.

    A card may land in several rows at once, and should: "destroy target
    permanent" answers a creature AND an artifact, and the matrix would
    lie if it had to pick one.
    """
    text = (oracle_text or "").lower()
    if not text:
        return set()
    types = (type_line or "").lower()
    # A land whose text mentions graveyard exile (Bojuka Bog, Scavenger
    # Grounds) is real graveyard hate; nothing else about a land's text
    # should read as spot removal, so the other rows stay off. Lands are
    # the one type where oracle text routinely describes an ability of a
    # permanent rather than a spell's effect. Branching early skips the
    # other four categories' patterns entirely (~30 wasted searches per
    # land per report) — the result is identical to computing all five
    # and intersecting with {"graveyard_hate"}.
    if "land" in types:
        if any(p.search(text) for p in _COMPILED["graveyard_hate"]):
            return {"graveyard_hate"}
        return set()
    return {
        category
        for category, patterns in _COMPILED.items()
        if any(p.search(text) for p in patterns)
    }


def classify_interaction(
    oracle_text: str, type_line: str = "", *, script=None,
) -> set[str]:
    """Categories for one card. Forge verdict when it has one, else regex.

    See the module docstring for why an empty Forge verdict defers to the
    oracle regex instead of standing as "no interaction".
    """
    if script is not None:
        forge = forge_categories(script)
        if forge:
            return forge
    return oracle_categories(oracle_text, type_line)


def is_instant_speed(oracle_text: str, type_line: str) -> bool:
    """True when the card can be cast on an opponent's turn.

    Instant type line OR the Flash keyword — the two ways a card lets you
    hold mana up and answer a threat the turn it appears. This is the
    distinction that makes eight instants a different deck from eight
    sorceries, and nothing in the codebase drew it before.
    """
    if "instant" in (type_line or "").lower():
        return True
    return "flash" in (oracle_text or "").lower()


# ---------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------

def _iter_deck_cards(deck_text: str) -> Iterable[tuple[int, str]]:
    """[Commander] + [Main], via the canonical walker.

    The commander is included for the same reason the health grade now
    credits it: it is in play in most games, so a commander that IS the
    deck's repeatable artifact answer genuinely covers that row.
    """
    from .deck_library_analyzer import iter_deck_cards
    return iter_deck_cards(deck_text)


def _default_lookup(name: str) -> Optional[dict]:
    """Scryfall lookup, fail-quiet, imported at call time."""
    try:
        from .scryfall_client import lookup_card
        return lookup_card(name)
    except Exception:  # noqa: BLE001
        return None


def _load_script(loader, name: str):
    """Parse ``name``'s Forge script, or None. Never raises."""
    if loader is None:
        return None
    try:
        from .forge_script_parser import parse_card_script
        raw = loader.load_one(name)
        if not raw:
            return None
        return parse_card_script(raw)
    except Exception:  # noqa: BLE001
        return None


def _type_line_from_script(script) -> str:
    """Forge ``Types:`` line rendered as a Scryfall-ish type line, so the
    instant-speed check works with no Scryfall data at all."""
    if script is None:
        return ""
    parts = list(getattr(script, "types", []) or [])
    keywords = getattr(script, "keywords", []) or []
    if any(str(kw).split(" ", 1)[0] == "Flash" for kw in keywords):
        parts.append("Flash")
    return " ".join(parts)


def interaction_report(
    deck_text: str,
    *,
    bracket: Optional[int] = DEFAULT_BRACKET,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    loader=None,
    use_forge: bool = True,
) -> Optional[dict]:
    """Coverage matrix for a deck, against ``bracket``'s minimums.

    ``lookup`` is a Scryfall-shaped resolver (injected in tests);
    ``loader`` is a ``forge_cards_loader.CardsLoader``. Passing
    ``use_forge=False`` (or having no corpus installed) runs the
    documented oracle-regex path alone.

    Returns::

        {
          "bracket": 3,
          "categories": {
            "creature_removal": {"count": 6, "minimum": 4, "gap": 0,
                                 "cards": [...]},
            ...
          },
          "interaction_total": 11,          # distinct interaction cards
          "instant_speed": {"count": 5, "share": 0.45, "minimum": 0.40,
                            "gap": 0.0},
          "gaps": ["Artifact/enchantment answers: 0 (bracket 3 wants 2)..."],
          "classified_by": {"forge": 0, "oracle": 78},
          "lookup_failures": 3,
        }

    ``None`` when the deck has no parseable cards, or when MORE than half
    of its lines could be classified by NEITHER path — the deck_health
    outage contract. "We couldn't read your deck" and "your deck has no
    interaction" are opposite conclusions and must never render the same.
    ``instant_speed["share"]`` is likewise ``None`` (not 0.0) for a deck
    with no interaction at all: a share of nothing is undefined.
    """
    resolve = lookup or _default_lookup
    if loader is None and use_forge:
        loader = _default_loader()

    entries = [(qty, name) for qty, name in _iter_deck_cards(deck_text) if name]
    if not entries:
        return None

    effective_bracket = clamp_bracket(bracket)
    minimums = minimums_for_bracket(effective_bracket)
    counts: dict[str, int] = {c: 0 for c in INTERACTION_CATEGORIES}
    cards: dict[str, list[str]] = {c: [] for c in INTERACTION_CATEGORIES}
    seen: dict[str, set[str]] = {c: set() for c in INTERACTION_CATEGORIES}
    classified_by = {"forge": 0, "oracle": 0}
    unresolved = 0
    interaction_total = 0
    instant_count = 0

    for qty, name in entries:
        try:
            card = resolve(name)
        except Exception:  # noqa: BLE001
            card = None
        script = _load_script(loader, name) if loader is not None else None
        if card is None and script is None:
            unresolved += 1
            continue
        card = card or {}
        oracle_text = card.get("oracle_text") or ""
        type_line = card.get("type_line") or ""
        if not type_line:
            type_line = _type_line_from_script(script)
        if not oracle_text:
            oracle_text = getattr(script, "oracle", "") or ""

        # Inlined ``classify_interaction`` so the report can record WHICH
        # classifier decided each card without paying for a second parse.
        forge_cats = forge_categories(script) if script is not None else set()
        cats = forge_cats or oracle_categories(oracle_text, type_line)
        if not cats:
            continue
        classified_by["forge" if forge_cats else "oracle"] += 1
        interaction_total += qty
        if is_instant_speed(oracle_text, type_line):
            instant_count += qty
        key = name.lower()
        for category in cats:
            counts[category] += qty
            if key not in seen[category]:
                seen[category].add(key)
                cards[category].append(name)

    if unresolved * 2 > len(entries):
        return None  # outage contract: most of the deck is unreadable.

    categories = {}
    gaps: list[str] = []
    for category in INTERACTION_CATEGORIES:
        minimum = int(minimums.get(category, 0))
        count = counts[category]
        gap = max(0, minimum - count)
        categories[category] = {
            "count": count, "minimum": minimum, "gap": gap,
            "cards": cards[category],
        }
        if gap > 0:
            gaps.append(
                f"{_CATEGORY_LABELS[category]}: {count} "
                f"(bracket {effective_bracket} wants {minimum}) — "
                f"{_CATEGORY_CONSEQUENCE[category]}"
            )

    share = (instant_count / interaction_total) if interaction_total else None
    share_min = float(minimums.get("instant_speed_share", 0.0))
    if share is not None and share < share_min:
        gaps.append(
            f"Instant-speed interaction: {share:.0%} of "
            f"{interaction_total} answers (bracket "
            f"{effective_bracket} wants {share_min:.0%}) — "
            f"sorcery-speed answers are dead against anything that "
            f"happens on someone else's turn"
        )

    return {
        "bracket": effective_bracket,
        "categories": categories,
        "interaction_total": interaction_total,
        "instant_speed": {
            "count": instant_count,
            "share": share,
            "minimum": share_min,
            "gap": (
                max(0.0, share_min - share) if share is not None else None
            ),
        },
        "gaps": gaps,
        "classified_by": classified_by,
        "lookup_failures": unresolved,
    }
