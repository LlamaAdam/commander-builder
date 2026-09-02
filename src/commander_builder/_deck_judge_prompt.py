"""Prompt construction for the FP-016 LLM deck judge — blinded + diff-focused.

Split out of ``deck_judge.py`` on 2026-08-20 following the repo's
``_<module>_<part>.py`` convention (``_advisor_claude``, ``_proposer_sim``):
the panel/reconciliation logic and the prompt text are two different jobs
that change for two different reasons, and keeping them together would put
one module over the 800-line ceiling before the feature was finished.

TWO INVARIANTS THIS MODULE EXISTS TO ENFORCE
============================================

**1. Blinding.** The judge is never told which deck is the incumbent
(FP-016 §3). Status-quo bias is otherwise free to masquerade as judgment,
and it would be indistinguishable from judgment in the output. So the
prompt is built from CARD NAMES ONLY — never from raw ``.dck`` text, whose
``[metadata] Name=`` line carries "[USER] My Deck v2 [B3]" and leaks the
whole answer. There is no code path here that accepts a filename, and
:func:`build_judge_prompt` takes deck *text* precisely so the caller cannot
accidentally hand it a path. The decks are labeled ``DECK A`` / ``DECK B``
by presentation position, which is the only thing the judge is allowed to
know about them.

**2. Retrieval, never recall** (FP-016 §4). Every card whose text the
judgment turns on is HANDED to the model with its oracle text. A card the
oracle snapshot cannot resolve is named as unresolvable rather than
silently dropped, because the failure mode of a quiet drop is a judge
confidently reasoning from a hallucinated card.

**Prompt budget — diff-focused (decision D5).** Full oracle text for ~200
cards across two decks runs tens of thousands of tokens *per judgment*,
and the panel is six judgments per pairing. It is also worse judging: the
changed cards are the question and they drown in 190 lines of unchanged
context. So the prompt carries full oracle text for the CHANGED cards only
plus a compact role-tagged name list for the shared remainder (role tags
come free from ``staples``).

CACHE-ONLY, ALWAYS
==================
Every Scryfall lookup here goes through ``lookup_card(..., cache_only=True)``.
A judge call runs inside the improve loop's round path; it must never hang
on a network round-trip per card, and it must never be the reason a round
takes an extra ten minutes. An unresolvable card degrades the prompt
honestly (see above) rather than blocking.

ALSO HERE: SWAP-DIRECTION LABELING (2026-08-27)
===============================================
:func:`classify_swap_direction` labels a pairing's changed-card sets
staple-ward vs intent-ward. It is not prompt text and the judge never sees
it — it is the input FP-016 §7's G3 kill criterion was pre-registered
against and nothing produced, so ``scripts/judge_agreement.py`` could only
print G3 as NOT COMPUTED. It lives beside :func:`changed_cards` (which it
consumes) rather than in ``deck_judge``; the long WHY is on the section
banner above the function.
"""
from __future__ import annotations

from typing import Callable, Optional

from . import dck_utils

#: The five dimensions of FP-016 §3, in the order they are reported.
#: ``politics_table`` is the one Forge's AI is structurally blind to
#: (decision C2 / ``staples.politics_tags``) and is therefore the reason
#: the judge covers a real gap rather than a redundant one.
DIMENSIONS: tuple[str, ...] = (
    "plan_coherence",
    "interaction_density",
    "resilience",
    "mana_curve_realism",
    "politics_table",
)

#: Human gloss per dimension, rendered into the system prompt so the
#: panel scores the same five things every time. One sentence each —
#: a longer rubric invites the model to grade the rubric.
_DIMENSION_GLOSS: dict[str, str] = {
    "plan_coherence": (
        "does the deck have one plan its cards actually execute, or a pile "
        "of individually fine cards pulling in different directions?"
    ),
    "interaction_density": (
        "enough removal / counterplay to survive three opponents' best "
        "cards, without so much that the deck has no plan of its own."
    ),
    "resilience": (
        "can it rebuild after a board wipe or the commander being answered "
        "repeatedly? Redundancy and recursion, not just raw power."
    ),
    "mana_curve_realism": (
        "does the curve match the ramp actually in the deck, at a real "
        "four-player table where nobody is drawing perfectly?"
    ),
    "politics_table": (
        "the multiplayer axis: goad, monarch, votes, taxes, deterrents, and "
        "whether the deck gives opponents reasons to attack somebody else. "
        "This is the dimension a game simulator cannot see."
    ),
}

#: The refusal that makes this instrument honest, stated in the judge's
#: own instructions rather than only in our docs. FP-016 §1: "better" was
#: always two questions and this panel answers exactly one of them.
_QUESTION_ONE_REFUSAL = """\
THE QUESTION YOU MAY NOT ANSWER
------------------------------
You may NOT answer "which deck wins more games". You have no access to
game outcomes, no simulation results, and no match data — a claim about
win rates from you would be invented, and inventing one is the single
worst thing you can do here. A separate empirical instrument owns that
question and it is the only thing allowed to answer it.

Do not write "deck B would win more", "this improves the win rate",
"stronger in practice", or any other outcome claim, in any field. If you
cannot make your point without predicting games, say less.

THE QUESTION YOU ARE ANSWERING
------------------------------
Which of these two decks is better BUILT FOR ITS STATED INTENT, at a real
four-player Commander table? That is a construction question, and it is
the one you are qualified for.
"""


def judge_system_prompt() -> str:
    """The panel's system prompt. Deterministic — the same bytes every
    call, so the six judgments differ only by presentation order and by
    the model's own non-determinism (which is what the panel measures).
    """
    dims = "\n".join(
        f"  - {name}: {_DIMENSION_GLOSS[name]}" for name in DIMENSIONS
    )
    schema_fields = ",\n".join(f'    "{name}": <integer -2..2>' for name in DIMENSIONS)
    return f"""\
You are one judge on a blind panel comparing two Magic: the Gathering
Commander decklists.

{_QUESTION_ONE_REFUSAL}
WHAT YOU ARE AND ARE NOT TOLD
-----------------------------
You are shown DECK A and DECK B. You are NOT told which came first, which
is anyone's current build, or which one a human is hoping wins. That is
deliberate: if you could tell, you would favour the familiar one and call
it judgment. Judge only what is in front of you.

The two decks are mostly identical. The cards that DIFFER are given to you
in full, with their oracle text. The cards they SHARE are listed by name
with a role tag. Reason from the oracle text you are handed. If a card's
text is not in this prompt, you do not know what it does — say so rather
than recalling it. A judgment built on a misremembered card is worthless.

The deck's stated INTENT is supplied. Judge against that intent, not
against generic Commander power level: "is this better at what this deck
is trying to do". A change that makes the deck more average is not
automatically an improvement.

SCORE THESE FIVE DIMENSIONS
---------------------------
{dims}

Each score is an INTEGER from -2 to +2 and is SIGNED TOWARD DECK B:
  +2 = deck B is clearly better on this dimension
  +1 = deck B is slightly better
   0 = no meaningful difference
  -1 = deck A is slightly better
  -2 = deck A is clearly better

OUTPUT — STRICT JSON, NOTHING ELSE
----------------------------------
Your entire response must be one JSON object. No prose before it, no
markdown fences, no commentary after it. First character ``{{``, last
character ``}}``.

{{
  "preferred": "A" | "B" | "neither",
  "dimensions": {{
{schema_fields}
  }},
  "reasoning": "at most three sentences, about construction only"
}}

"preferred" is your overall answer. "neither" is a real answer and you
should use it when the two decks are genuinely comparable for this
deck's intent — a panel that always picks a winner is measuring
agreeableness, not quality.
"""


def _lookup_cache_only(name: str) -> Optional[dict]:
    """Default resolver: the on-disk oracle snapshot, never the network.

    Lazily imported so importing this module (which ``cli.resolve`` does
    for the ``judge`` subcommand's help) costs nothing.
    """
    from .scryfall_client import lookup_card
    try:
        return lookup_card(name, cache_only=True)
    except Exception:  # noqa: BLE001 — a bad name must not sink a judgment
        return None


def changed_cards(
    deck_a_text: str, deck_b_text: str,
) -> tuple[list[str], list[str], list[str]]:
    """``(only_in_a, only_in_b, shared)`` over the two decks' [Main] names.

    Set-difference on names, deck order preserved. Commander cards are
    excluded (they live in ``[Commander]`` and are reported separately —
    a swap never changes them, and if one did, that is the pairing's
    headline rather than a diff line).
    """
    a_names = dck_utils.main_card_names(deck_a_text)
    b_names = dck_utils.main_card_names(deck_b_text)
    a_set, b_set = set(a_names), set(b_names)
    only_a = [n for n in dict.fromkeys(a_names) if n not in b_set]
    only_b = [n for n in dict.fromkeys(b_names) if n not in a_set]
    shared = [n for n in dict.fromkeys(a_names) if n in b_set]
    return only_a, only_b, shared


# ---------------------------------------------------------------------------
# Swap-direction labeling — FP-016 G3's missing input (added 2026-08-27)
# ---------------------------------------------------------------------------
#
# G3 ("consensus bias") was pre-registered as: does the judge's preference
# track generic inclusion rate more strongly than deck-specific fit,
# "tested by scoring swaps that are staple-ward vs. intent-ward". Nothing
# produced that label, so ``scripts/judge_agreement.py`` printed G3 as NOT
# COMPUTED — honest, but a gate that can never run is not a gate.
#
# The label has to be attached HERE, at judge time, for one reason: it is
# the only moment the pairing's changed-card sets, the resolved oracle text
# and the learned intent are all in hand at once. Reconstructing it later
# from a knowledge_log row would mean re-reading two .dck snapshots and
# re-learning intent for every historical row — and would silently label
# old rows with a NEWER intent than the panel actually judged against,
# which is the one thing that would make the G3 number meaningless.
#
# It lives in THIS module rather than in ``deck_judge`` because it consumes
# ``changed_cards`` (defined right above) and ``staples`` (already used by
# ``_role_tagged_names``, with the same lazy-import convention), and
# because deck_judge.py is near the repo's 800-line ceiling. It is not
# prompt text, and it is deliberately NOT shown to the judge: a panel told
# "this swap is staple-ward" would be answering a leading question, and G3
# would then measure the label rather than the bias.

#: A bucket must hold this share of the classifiable added cards before the
#: swap gets a direction. 0.60 = "predominantly", not "at all": a 3-card
#: swap containing one Sol Ring is not a staple-ward swap.
#:
#: PRE-REGISTERED 2026-08-27, and still before any results exist — the
#: judge flag is default-off and the knowledge log holds zero paired rows,
#: so this number cannot have been tuned to a result. Changing it once
#: pairings land is moving the goalposts; ``tests/test_deck_judge.py``
#: pins it for that reason.
SWAP_LABEL_DOMINANCE = 0.60

#: Below this many resolvable added cards the shares are noise (a 1-card
#: swap is 0% or 100% and nothing in between), so the pairing is labeled
#: ``unknown`` and G3 skips it rather than counting a coin flip.
SWAP_LABEL_MIN_CARDS = 2

#: The direction vocabulary. ``unknown`` is a real answer and the common
#: one on day zero — no intent supplied, too few cards, or nothing
#: resolvable. It must never be collapsed into ``neither``: "we could not
#: label this swap" and "this swap is neither staple-ward nor intent-ward"
#: are different facts, and only the second one is evidence.
SWAP_DIRECTIONS: tuple[str, ...] = (
    "staple_ward", "intent_ward", "mixed", "neither", "unknown",
)


def _generic_staple_names() -> frozenset[str]:
    """Case-folded names that count as "generically included" for G3.

    Two shipped lists, unioned, because G3's target is EDHREC-inclusion
    bias and each covers a different half of it:

    * ``staples.UNIVERSAL_STAPLES_LC`` — "well over 50% of ALL decks
      regardless of commander". Sol Ring, Arcane Signet, Command Tower.
    * the Commander Brackets Game Changers list — the high-power cards
      every list that CAN play them does. FP-016 §7's own example of the
      failure ("if it just recommends Rhystic Study to everyone") is on
      this list and on neither of the others, so omitting it would leave
      the gate blind to the exact case it was written for.

    Read through ``game_changers.offline_game_changers`` — this runs on
    the judge's cache-only-always path and must not open a socket. A
    missing/untrusted cache degrades to the bundled list, which is a
    slightly stale label rather than a stalled round.

    Basic lands are NOT here: they are in every deck by construction, but
    a swap that adds a Forest is a manabase edit, not a consensus signal.
    """
    from .game_changers import offline_game_changers
    from .staples import UNIVERSAL_STAPLES_LC

    names = set(UNIVERSAL_STAPLES_LC)
    names.update(c.casefold() for c in offline_game_changers())
    return frozenset(names)


def _matches_intent(name: str, card: dict, intent) -> bool:
    """Does this one card pull toward the deck's DECLARED intent?

    Three signals, any one sufficient, each read off an attribute
    ``intent.Intent`` actually carries — no new taxonomy invented for the
    label, because a taxonomy only this function understands could not be
    checked against anything:

    1. **Theme.** A slug the card's oracle matches is in ``intent.themes``.
       Uses ``staples.card_theme_slugs``, the per-card half of the same
       ``_THEME_PATTERNS`` that produced ``intent.themes`` in the first
       place — so "on theme" and "the deck has this theme" agree by
       construction.
    2. **Tribe.** ``intent.tribal_type`` appears in the card's type line or
       oracle text (Goblin lord, changeling, "Goblins you control").
    3. **Declared win route.** The card is literally one of
       ``intent.key_wincons``. Rare in an ADDED set, but exact when it
       fires and free to check.

    Deliberately NOT a signal: colour identity. Every legal card in the
    deck matches it, so it would label every swap intent-ward.
    """
    themes = {str(t).casefold() for t in (getattr(intent, "themes", None) or [])}
    oracle = (card.get("oracle_text") or "")
    type_line = (card.get("type_line") or "")

    if themes:
        from .staples import card_theme_slugs
        if {s.casefold() for s in card_theme_slugs(oracle)} & themes:
            return True

    tribe = (getattr(intent, "tribal_type", None) or "").strip().casefold()
    if tribe and (tribe in type_line.casefold() or tribe in oracle.casefold()):
        return True

    wincons = {
        str(w).casefold() for w in (getattr(intent, "key_wincons", None) or [])
    }
    return name.casefold() in wincons


def _bucket_cards(
    names: list[str],
    intent,
    lookup: Callable[[str], Optional[dict]],
) -> dict[str, int]:
    """Sort card names into the four G3 buckets plus ``unresolved``.

    Buckets are mutually exclusive by explicit precedence so the shares
    sum to 1 and no card is double-counted:

      ``both``       — generic staple AND an intent match (Rhystic Study in
                       a spellslinger deck). Evidence for NEITHER side, and
                       counted separately rather than assigned to one, so a
                       swap full of these reads as ``mixed`` instead of
                       being silently credited to whichever test ran first.
      ``staple``     — generic only.
      ``intent``     — intent only.
      ``neither``    — an ordinary card that is neither. The most common
                       bucket in a real swap, and the honest default.
      ``unresolved`` — no oracle text AND not on a name list, so it cannot
                       be tested for intent fit. Excluded from the shares
                       entirely (never quietly folded into ``neither``:
                       that would read "we tested it and it was plain"
                       when we did not test it at all).

    Staple membership is a NAME test, so an unresolvable card can still be
    labeled a staple; intent fit needs the oracle text, so it cannot.
    """
    generic = _generic_staple_names()
    counts = {"staple": 0, "intent": 0, "both": 0, "neither": 0, "unresolved": 0}
    for name in names:
        card = lookup(name) or {}
        resolved = bool(card.get("oracle_text") or card.get("type_line"))
        is_staple = name.casefold() in generic
        if not resolved and not is_staple:
            counts["unresolved"] += 1
            continue
        fits_intent = (
            _matches_intent(name, card, intent)
            if (intent is not None and resolved) else False
        )
        if is_staple and fits_intent:
            counts["both"] += 1
        elif is_staple:
            counts["staple"] += 1
        elif fits_intent:
            counts["intent"] += 1
        else:
            counts["neither"] += 1
    return counts


def classify_swap_direction(
    deck_a_text: str,
    deck_b_text: str,
    *,
    intent=None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Label the pairing's swap ``staple_ward`` / ``intent_ward`` /
    ``mixed`` / ``neither`` / ``unknown``, with the counts behind it.

    ORIENTATION. The label is computed on what the swap ADDS — the cards
    in deck B and not in deck A — because that is the direction a swap
    points. Deck A is the incumbent and deck B the candidate everywhere in
    this feature (see ``deck_judge.judge_pairing``), and a ``kept`` verdict
    means the panel preferred B, so "did the judge approve staple-ward
    swaps more than intent-ward ones" is a question about the added set.
    The removed set is bucketed too and returned under ``removed`` — it is
    the natural next refinement (a swap that CUTS the theme is staple-ward
    in effect) and recording it now costs one extra dict, but it does NOT
    drive the label, because folding two directions into one word before
    anyone has looked at a single pairing would be guessing.

    NO INTENT, NO LABEL. When ``intent`` is None the intent-fit test can
    never fire, so every swap would come back ``staple_ward`` or
    ``neither`` — a fabricated result pointing at exactly the bias G3
    tests for. Those pairings are ``unknown`` and G3 skips them.

    FREE TEXT IS NOT INTENT — for labeling (FP-018.2, 2026-08-27). An
    ``Intent`` whose only content is ``stated`` / ``pilot_preferences``
    (no themes, no tribe, no key wincons) is the same situation as
    ``intent=None`` here: ``_matches_intent`` reads exactly those three
    structured signals, so with all of them empty the intent-fit test
    can never fire and every swap would again come back ``staple_ward``
    or ``neither``. Without this guard, the adopt flow (which routinely
    builds free-text-only intents) would silently move those pairings
    from "unlabelable" into G3's population with a fabricated direction.
    Structured signals drive labeling; free text never does.

    Returns a plain dict (JSON-serializable, stored verbatim in
    ``JudgeReport.swap_label`` and thus in the ``judge_report`` column)::

        {"direction": ..., "reason": ..., "threshold": 0.6,
         "added": {...counts...}, "removed": {...counts...},
         "added_classifiable": int,
         "staple_share": float|None, "intent_share": float|None}
    """
    lookup = lookup or _lookup_cache_only
    _only_a, only_b, _shared = changed_cards(deck_a_text, deck_b_text)

    added = _bucket_cards(only_b, intent, lookup)
    removed = _bucket_cards(_only_a, intent, lookup)
    classifiable = (
        added["staple"] + added["intent"] + added["both"] + added["neither"]
    )
    label = {
        "threshold": SWAP_LABEL_DOMINANCE,
        "min_cards": SWAP_LABEL_MIN_CARDS,
        "added": added,
        "removed": removed,
        "added_classifiable": classifiable,
        "staple_share": None,
        "intent_share": None,
    }

    if intent is None:
        return {**label, "direction": "unknown",
                "reason": "no intent supplied; intent-fit cannot be tested"}
    has_structured_signal = bool(
        (getattr(intent, "themes", None) or [])
        or (getattr(intent, "tribal_type", None) or "").strip()
        or (getattr(intent, "key_wincons", None) or [])
    )
    if not has_structured_signal:
        # Free-text-only (or empty) intent — see the docstring: the
        # intent-fit test cannot fire, so a computed label would be
        # fabricated exactly like the intent=None case.
        return {**label, "direction": "unknown",
                "reason": (
                    "intent carries no structured signals (themes / tribe "
                    "/ key wincons); free text does not drive labeling"
                )}
    if classifiable < SWAP_LABEL_MIN_CARDS:
        return {**label, "direction": "unknown",
                "reason": (
                    f"only {classifiable} classifiable added card(s) "
                    f"(need {SWAP_LABEL_MIN_CARDS}); "
                    f"{added['unresolved']} unresolved"
                )}

    staple_share = added["staple"] / classifiable
    intent_share = added["intent"] / classifiable
    label["staple_share"] = staple_share
    label["intent_share"] = intent_share

    if staple_share >= SWAP_LABEL_DOMINANCE and staple_share > intent_share:
        direction, reason = "staple_ward", (
            f"{staple_share:.0%} of classifiable added cards are generic "
            f"staples / game changers with no intent fit"
        )
    elif intent_share >= SWAP_LABEL_DOMINANCE and intent_share > staple_share:
        direction, reason = "intent_ward", (
            f"{intent_share:.0%} of classifiable added cards match the "
            f"deck's declared themes / tribe / win route"
        )
    elif added["staple"] and added["intent"]:
        direction, reason = "mixed", (
            f"{added['staple']} staple-ward and {added['intent']} "
            f"intent-ward added card(s); neither reaches "
            f"{SWAP_LABEL_DOMINANCE:.0%}"
        )
    elif added["both"] and not (added["staple"] or added["intent"]):
        direction, reason = "mixed", (
            f"{added['both']} added card(s) are both generic staples and "
            f"an intent match — evidence for neither side"
        )
    else:
        direction, reason = "neither", (
            f"neither share reaches {SWAP_LABEL_DOMINANCE:.0%} "
            f"(staple {staple_share:.0%}, intent {intent_share:.0%})"
        )
    return {**label, "direction": direction, "reason": reason}


def _oracle_block(names: list[str], lookup: Callable[[str], Optional[dict]]) -> str:
    """Full oracle text + type line for every name, one entry per card.

    An unresolvable name is NAMED as unresolvable rather than dropped:
    a silent drop would leave the judge free to fill the gap from memory,
    which is exactly the failure this whole retrieval discipline exists to
    prevent.
    """
    if not names:
        return "  (none)"
    out: list[str] = []
    for name in names:
        card = lookup(name) or {}
        type_line = (card.get("type_line") or "").strip()
        oracle = (card.get("oracle_text") or "").strip()
        mana = (card.get("mana_cost") or "").strip()
        if not type_line and not oracle:
            out.append(
                f"  - {name}\n"
                f"      ORACLE TEXT UNAVAILABLE — this card is not in the "
                f"local snapshot. Do not reason about it from memory; treat "
                f"it as unknown."
            )
            continue
        header = f"  - {name}"
        if mana:
            header += f"  {mana}"
        body = [header]
        if type_line:
            body.append(f"      {type_line}")
        for line in (oracle or "(no rules text)").splitlines():
            body.append(f"      {line}")
        out.append("\n".join(body))
    return "\n".join(out)


def _role_tagged_names(
    names: list[str], lookup: Callable[[str], Optional[dict]],
) -> str:
    """Compact ``Name [role]`` list for the shared remainder.

    Role tags come free from ``staples.classify_role_extended`` — the
    canonical classifier both the dashboard and the advisor route through,
    so the judge's view of "what this card is for" matches the rest of the
    app's. Politics tags are appended where they fire so the
    ``politics_table`` dimension has something concrete to read.
    """
    from .staples import classify_role_extended, politics_tags

    if not names:
        return "  (none)"
    lines: list[str] = []
    for name in names:
        card = lookup(name) or {}
        oracle = card.get("oracle_text") or ""
        type_line = card.get("type_line") or ""
        if not oracle and not type_line:
            lines.append(f"  {name} [unknown]")
            continue
        role = classify_role_extended(oracle, type_line)
        tags = politics_tags(oracle, type_line)
        suffix = f", politics: {'/'.join(tags)}" if tags else ""
        lines.append(f"  {name} [{role}{suffix}]")
    return "\n".join(lines)


def _intent_block(intent) -> str:
    """The standard the decks are judged against (FP-016 §4).

    ``intent`` is an ``intent.Intent`` (or anything with the same
    attributes, or None). Without this anchor an LLM panel converges every
    deck toward the EDHREC average, which is the single most likely way
    this feature makes the app worse.

    FREE TEXT (FP-018.2, 2026-08-27 — the Phase-1 boundary this block
    used to pin has moved, and the two boundary tests moved with it).
    ``intent.stated`` (the deck's own primer, rendered from the source's
    Quill Delta by ``primer.render_quill_delta`` — never the raw field,
    see ``tests/fixtures/hazel_primer.md`` for the trap) and
    ``intent.pilot_preferences`` (the adopting player's words) are
    rendered as clearly labeled quoted sections. Both are clipped by
    ``primer.clip_for_prompt`` — primers run long, and an unbounded one
    would drown the changed-card diff the panel is there to judge; the
    clip marks any truncation explicitly rather than silently shortening
    the author's words. The grounding rule is stated to the judge in the
    block itself: free text steers attention, it never establishes card
    facts — those come only from the oracle text in this prompt.
    """
    if intent is None:
        return (
            "  (not supplied — judge against ordinary Commander construction "
            "for the commander shown, and say in your reasoning that no "
            "stated intent was available)"
        )
    parts = [f"  archetype: {getattr(intent, 'archetype', None) or 'unknown'}"]
    themes = list(getattr(intent, "themes", None) or [])
    if themes:
        parts.append(f"  themes: {', '.join(themes)}")
    tribal = getattr(intent, "tribal_type", None)
    if tribal:
        parts.append(f"  tribal: {tribal}")
    wincons = list(getattr(intent, "key_wincons", None) or [])
    if wincons:
        parts.append(f"  key win-conditions: {', '.join(wincons)}")
    colors = list(getattr(intent, "color_identity", None) or [])
    if colors:
        parts.append(f"  color identity: {''.join(colors)}")
    stated = (getattr(intent, "stated", None) or "").strip()
    prefs = (getattr(intent, "pilot_preferences", None) or "").strip()
    if stated or prefs:
        from .primer import clip_for_prompt
        parts.append(
            "  (The free text below steers what to pay attention to. It "
            "does not establish card facts — cards do only what the "
            "oracle text in this prompt says they do.)"
        )
        if stated:
            parts.append("  deck's own primer (the builder's words):")
            parts.append(f'    """{clip_for_prompt(stated)}"""')
        if prefs:
            parts.append("  pilot preferences (the player's words):")
            parts.append(f'    """{clip_for_prompt(prefs)}"""')
    return "\n".join(parts)


def _primer_kb_block(commanders) -> str:
    """Community-primer context for the judge (FP-019.6), or "".

    When the bundled primer KB holds profiles for the deck's primary
    commander, render them (clipped) under the same grounding rule the
    intent block states: this steers attention, it never establishes
    card facts. Identical for both A/B orderings — the commander is the
    same in both decks — so it cannot bias the swap-pair design.
    Empty string (rendering as a blank line) when the KB has nothing or
    fails: the prompt must not change shape on a KB outage.
    """
    if not commanders:
        return ""
    try:
        from .primer_kb import prompt_block_for_commander
        rendered = prompt_block_for_commander(commanders[0])
    except Exception:  # noqa: BLE001 — context only, never break a prompt
        return ""
    if not rendered:
        return ""
    return (
        "\nCOMMUNITY PRIMER CONTEXT — how experienced pilots build this "
        "commander\n(Attention-steering only, same grounding rule as the "
        "intent block: cards\ndo only what the oracle text in this prompt "
        f"says they do.)\n{rendered}\n"
    )


def build_judge_prompt(
    *,
    deck_a_text: str,
    deck_b_text: str,
    intent=None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
) -> str:
    """The user message for ONE judgment, with ``deck_a_text`` shown as A.

    Order swapping is the caller's job (``deck_judge`` builds the pair by
    calling this twice with the arguments transposed) — this function has
    no idea a pairing has two orders, which is what keeps the two prompts
    structurally identical apart from the swap.

    Takes deck TEXT, never paths: see this module's blinding note. Nothing
    in the returned string identifies either deck beyond its position.
    """
    lookup = lookup or _lookup_cache_only
    only_a, only_b, shared = changed_cards(deck_a_text, deck_b_text)
    commanders = dck_utils.section_card_names(deck_a_text, "Commander")

    commander_block = (
        _oracle_block(commanders, lookup) if commanders
        else "  (no commander section found)"
    )
    return f"""\
COMMANDER (identical in both decks)
{commander_block}

STATED INTENT — the standard to judge against
{_intent_block(intent)}
{_primer_kb_block(commanders)}
ONLY IN DECK A ({len(only_a)} card(s), full oracle text)
{_oracle_block(only_a, lookup)}

ONLY IN DECK B ({len(only_b)} card(s), full oracle text)
{_oracle_block(only_b, lookup)}

SHARED BY BOTH DECKS ({len(shared)} card(s), names + role tags)
{_role_tagged_names(shared, lookup)}

Both decks are otherwise the same list. Judge the difference above
against the stated intent, score the five dimensions, and return the
strict JSON object. No outcome claims.
"""
