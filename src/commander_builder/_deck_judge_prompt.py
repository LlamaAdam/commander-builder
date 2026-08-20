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

    NOT WIRED IN PHASE 1: the owner's *written* primer. FP-016 §4 pins
    the standard to ``intent.learn_intent`` — derived from the decklist
    itself — and this block deliberately carries only that, so the judge
    is anchored to the SAME intent the improve loop already protects
    cards with rather than to a second, differently-derived one.
    ``tests/fixtures/hazel_primer.md`` is the real stated-intent capture
    waiting for whoever wires the richer anchor, and it also carries the
    trap: an Archidekt ``description`` is a Quill Delta JSON *string*
    (``{"ops": [{"insert": ...}]}``), not prose. Anything that reads a
    primer must parse the Delta; the fixture's section 2 is labeled
    DERIVED precisely so nobody mistakes the rendered text for the field.
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
    return "\n".join(parts)


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
