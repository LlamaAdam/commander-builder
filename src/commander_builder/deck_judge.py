"""FP-016 Phase 1 — the LLM deck judge. An OPINION panel, never a verdict.

THE CONTRACT, STATED ONCE (mirrors ``forge_py_screen``'s)
========================================================

    The judge answers "is this deck better BUILT for its stated intent".
    Forge decides which deck is BETTER. Only Forge.

``forge_py_screen`` is the precedent for a module that never becomes a
judge; this is the precedent for a *judge* that never becomes a verdict.
FP-016 §1 sets out why "better" was always two questions and why the sim
only answers one: Forge's AI does not negotiate and loops ~25% of soak
games, so on politics, threat assessment and the incentive to pay a tax
its margin is not weak evidence — it is *no* evidence. This module is the
first instrument that can evaluate that second question. It is also an
opinion, and it says so everywhere it prints.

**It must never answer question 1.** No "the LLM thinks deck B wins more
games". It has no access to game outcomes and would be inventing them.
The refusal is written into the judge's own system prompt
(``_deck_judge_prompt._QUESTION_ONE_REFUSAL``), not just into this
docstring.

PHASE 1 IS OBSERVE-ONLY
=======================
* It does not advance decks and does not gate anything.
* It writes ``judge_verdict`` / ``judge_report`` BESIDE the sim verdict
  (``knowledge_log`` schema v4), never instead of it. A row's judge
  fields stay NULL unless the judge actually ran.
* It ships behind ``COMMANDER_BUILDER_DECK_JUDGE``, default OFF, same
  truthy spelling as ``card_score.is_enabled`` — the repo's convention
  for unvalidated machinery. While it is off, nothing is spent.
* Phase 2 (``scripts/judge_agreement.py``) produces the agreement table
  that decides whether there is a Phase 3.
* Each pairing also records a SWAP DIRECTION (staple-ward / intent-ward /
  mixed / neither / unknown) from
  ``_deck_judge_prompt.classify_swap_direction``. That is G3's population
  membership — the input the consensus-bias kill criterion was
  pre-registered against and, until 2026-08-27, the reason G3 could not be
  computed at all. The judge is never shown the label; being told "this
  swap is staple-ward" would turn G3 into a measurement of the label.

BORROWING THE SIM'S DISCIPLINE, NOT SKIPPING IT
===============================================
The failure mode to avoid is a confident single opinion dressed as a
measurement. The sim earns its verdicts with a significance test; this
earns its verdicts with a panel:

* **Panel of 6, three per presentation order** (decision D2). Six
  INDEPENDENT judgments — six separate calls, no shared context. Five
  cannot split evenly across two orders, which would confound position
  bias with ordinary judge variance, the one thing this design exists to
  keep separate.
* **Agreement is counted on the DECK, never the position.** Position bias
  is the best-documented LLM-judge failure mode. The two triads see the
  pairing transposed; a preference is normalized back onto the deck it
  actually names before anything is counted.
* **Order-flip is inconclusive BY DEFINITION.** If both triads prefer
  whichever deck was shown first (or shown second), the panel has
  reported its own seating chart, not a judgment. That is
  ``inconclusive`` — and it is also the G1 kill-criterion's counter.
* **Blinded** — see ``_deck_judge_prompt``. The judge is never told which
  deck is the incumbent.
* **Supermajority gate**: >= 5 of 6 on the same deck. Anything less is
  ``inconclusive``. Discarded (out-of-schema) judgments are NOT excluded
  from the denominator — with two judgments lost, five agreeing is no
  longer reachable and the pairing degrades honestly to ``inconclusive``
  instead of quietly lowering its own bar.
* **The existing verdict vocabulary**, reused verbatim from
  ``_proposer_sim``: ``kept`` / ``reverted`` / ``neutral`` /
  ``inconclusive``. No new labels. Orientation: ``kept`` means DECK B —
  the second deck of the pairing, which in the improve loop is the
  candidate — is the better-built one.
* **Per-dimension scores** so disagreement is diagnosable rather than one
  opaque number. See ``_deck_judge_prompt.DIMENSIONS``.

MODEL TIER (decision D4)
========================
The strongest tier available, not a larger cheap panel: panel size buys
down *variance*, not *bias*, and the failure modes that would sink this
feature (consensus-chasing, shallow Commander reasoning) are systematic
and therefore correlated across panel members. Adding cheap judges
measures the same bias more precisely rather than removing it.

TRANSPORT — the same dual path ``proposer.auto_propose`` already uses
=====================================================================
A non-empty ``ANTHROPIC_API_KEY`` selects the Anthropic SDK (per-token
billing, explicit opt-in); otherwise the subscription ``claude`` CLI via
``proposer._curator_complete_via_cli``, which is the unattended path and
the one that works under a Claude Max plan with no key. The judge runs
automatically inside the improve loop (decision D3), so it is unattended
by construction and inherits that ladder rather than inventing a second
one. Everything is injectable via ``judge_fn`` — no test in this repo
makes a real Claude call.

HONEST LIMITATIONS (from FP-016 §8, kept here on purpose)
=========================================================
* No ground truth. Agreement with the sim is not truth; both instruments
  can be wrong together. The agreement table is informative, never
  confirmatory, and must never be written up as "validated".
* Consensus bias is the likeliest failure — mitigated by the intent
  anchor and the retrieval discipline, not eliminated.
* Non-determinism is why the panel and the supermajority gate exist
  rather than a single call.
* It is an opinion. The strongest honest claim available is "two
  instruments with different blind spots agree".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ._deck_judge_prompt import (
    DIMENSIONS,
    build_judge_prompt,
    changed_cards,
    classify_swap_direction,
    judge_system_prompt,
)
from ._llm_json import LLMJsonError, extract_json_object

# ---------------------------------------------------------------------------
# Flag — default OFF (FP-016 §6 / decision D3)
# ---------------------------------------------------------------------------

#: Opt-in env flag. ``1`` / ``true`` / ``yes``, case-insensitive.
#:
#: Default OFF is the cost control AND the honesty control. D3 settled
#: that Phase 1 runs AUTOMATICALLY whenever it is enabled rather than
#: on demand: the value of Phase 1 is an *unbiased* sample of paired
#: verdicts, and running it on demand would sample exactly the pairings
#: the operator was already curious about — the one sampling rule
#: guaranteed to poison the agreement table. So the only knob is
#: on/off, and while it is off nothing is spent.
DECK_JUDGE_ENV_VAR = "COMMANDER_BUILDER_DECK_JUDGE"


def is_enabled() -> bool:
    """True when the operator has opted into the deck judge.

    Same truthy-value convention as ``card_score.is_enabled`` /
    ``local_model``'s flag.
    """
    return os.environ.get(
        DECK_JUDGE_ENV_VAR, "",
    ).strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Panel geometry (decision D2) — the numbers, in one place
# ---------------------------------------------------------------------------

#: Six independent judgments per pairing.
PANEL_SIZE = 6
#: Three per presentation order. ``PANEL_SIZE == 2 * PER_ORDER`` is what
#: makes the two triads comparable; a test pins the relationship.
PER_ORDER = 3
#: Agreeing judgments required for a verdict. Out of ``PANEL_SIZE``, not
#: out of the valid count — see the module docstring.
SUPERMAJORITY = 5

#: The strongest tier available (decision D4). Used only on the SDK path;
#: the subscription CLI uses its own configured default model, which is
#: why ``_curator_complete_via_cli`` accepts ``model`` for logging parity
#: and does not force ``--model``.
DEFAULT_JUDGE_MODEL = "claude-opus-5"

#: Output budget per judgment. The answer is a small JSON object; this is
#: sized for the object plus the model's reasoning, not for prose.
JUDGE_MAX_TOKENS = 4096

#: Printed by every surface that reports a judge result. One sentence,
#: one source of truth — the CLI, the improve loop's round line and the
#: Phase 2 agreement script must all say the same thing, because they are
#: all reporting the same opinion.
OPINION_CAVEAT = (
    "This is an OPINION panel, blind to game outcomes. It answers "
    "'better built for its stated intent', never 'wins more games'. "
    "Forge decides which deck is better. Only Forge."
)

#: The verdict vocabulary, reused verbatim from ``_proposer_sim`` /
#: ``knowledge_log.update_verdict``. Orientation is deck-B-relative:
#: ``kept`` = deck B is better built, ``reverted`` = deck A is.
JUDGE_VERDICTS: frozenset[str] = frozenset(
    {"kept", "reverted", "neutral", "inconclusive"}
)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Judgment:
    """One panel member's answer, normalized onto the DECK.

    ``order`` is the presentation this judgment saw: ``"ab"`` means deck A
    of the pairing was shown as DECK A, ``"ba"`` means it was shown second.
    ``position_preference`` is the raw answer (which SLOT the judge picked)
    and ``preferred_deck`` is that answer mapped back onto the pairing —
    keeping both is what lets the reconciler tell "these two triads agree
    about the deck" apart from "these two triads agree about the seat",
    which are opposite conclusions.

    ``dimensions`` are signed toward DECK B of the PAIRING (not of the
    presentation), so medians pool across both orders without a second
    sign convention to get wrong.

    An invalid judgment keeps its slot with ``valid=False`` and an
    ``error``: the panel had six chances and the report should show that
    one of them was wasted, not silently shrink to five.
    """

    index: int
    order: str
    valid: bool
    position_preference: Optional[str] = None   # "A" / "B" / "neither"
    preferred_deck: Optional[str] = None        # "a" / "b" / "neither"
    dimensions: dict[str, float] = field(default_factory=dict)
    reasoning: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JudgeReport:
    """Everything one pairing's panel produced. Serializable as-is.

    The three fields the FP-016 §7 kill criteria read:

      ``order_flip`` — G1 (self-consistency). True when the two triads
        preferred the same SEAT, i.e. the panel flipped its answer when
        the decks were transposed. The gate parks the feature at >25% of
        pairings.
      ``all_kept``   — G2 (discrimination). True when this pairing came
        back ``kept``. The gate parks the feature at >80% of pairings.
        (Named for the tally it feeds, not for this single row.)
      ``verdict``    — the pooled answer, in the existing vocabulary.
      ``swap_direction`` — G3 (consensus bias). One of
        ``_deck_judge_prompt.SWAP_DIRECTIONS``. G3 is not a per-pairing
        PASS/FAIL the way G1 and G2 are — it compares the judge's
        approval rate across two POPULATIONS of pairings — so what a row
        carries is its membership, and ``scripts/judge_agreement.py``
        does the comparing. ``swap_label`` keeps the counts and the
        stated reason behind the word so a surprising G3 number can be
        traced back to individual swaps rather than taken on faith.

        Added 2026-08-27. Rows written before that carry neither field and
        are simply not in G3's population; the script counts them as
        unlabeled rather than assuming a direction for them.
    """

    verdict: str
    votes: dict[str, int]
    dimension_medians: dict[str, Optional[float]]
    judgments: list[Judgment] = field(default_factory=list)
    order_flip: bool = False
    all_kept: bool = False
    discarded: int = 0
    panel_size: int = PANEL_SIZE
    per_order: int = PER_ORDER
    supermajority: int = SUPERMAJORITY
    changed: dict[str, object] = field(default_factory=dict)
    #: G3's per-pairing membership. Defaults to ``"unknown"`` — a report
    #: built without the labeling (a hand-constructed one, or an older
    #: pickled row) must not read as a labeled pairing.
    swap_direction: str = "unknown"
    swap_label: dict = field(default_factory=dict)
    intent: Optional[dict] = None
    model: Optional[str] = None
    caveat: str = OPINION_CAVEAT
    notes: list[str] = field(default_factory=list)

    @property
    def valid_count(self) -> int:
        return sum(1 for j in self.judgments if j.valid)

    def to_dict(self) -> dict:
        # ``asdict`` recurses into the nested ``Judgment`` dataclasses, so
        # the whole report is JSON-serializable in one step — which is what
        # ``knowledge_log``'s ``judge_report`` column stores.
        return asdict(self)


# ---------------------------------------------------------------------------
# Transport — the ladder ``proposer.auto_propose`` already established
# ---------------------------------------------------------------------------

def _default_judge_fn(system: str, user: str, *, model: str) -> str:
    """One judgment through the repo's standard Claude ladder.

    SDK when ``ANTHROPIC_API_KEY`` is set to a NON-EMPTY value, else the
    subscription ``claude`` CLI. The truthiness test (not
    membership-in-``os.environ``) is deliberate and copied from
    ``auto_propose``: this environment sets the key to ``''`` on purpose
    to keep the SDK from ever billing, so a present-but-empty key must
    read as "no key".

    Never sets ``temperature``: the panel WANTS the model's own
    non-determinism (that variance is what six judgments measure), and
    the strongest-tier models reject sampling parameters outright.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover — exercised via stub
            raise RuntimeError(
                "deck judge requires `pip install anthropic` (in the "
                "[claude] extras) when ANTHROPIC_API_KEY is set."
            ) from exc
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=JUDGE_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text += block.text
        return text

    from .proposer import _claude_cli_available, _curator_complete_via_cli
    if not _claude_cli_available():
        raise RuntimeError(
            "deck judge needs either ANTHROPIC_API_KEY (SDK path) or the "
            "`claude` CLI on PATH (subscription path). Neither is available."
        )
    return _curator_complete_via_cli(system=system, user_msg=user, model=model)


# ---------------------------------------------------------------------------
# Response validation — strict, and a failure discards ONE judgment
# ---------------------------------------------------------------------------

def _parse_judgment(text: str, *, index: int, order: str) -> Judgment:
    """Validate one raw response into a deck-keyed ``Judgment``.

    Strictness is the point. A malformed judgment is DISCARDED, never
    repaired and never guessed at: the panel's whole claim is that six
    independent answers agreed, and an answer we had to interpret is not
    one of them. The JSON is recovered with the shared ``_llm_json``
    extractor (fences / prose preamble / braces-in-strings) because that
    is a transport artifact, not a schema failure — but once an object is
    in hand, every field must be exactly what the prompt asked for.
    """
    try:
        payload = extract_json_object(
            text, context=f"deck judge (judgment {index}, order {order})",
        )
    except LLMJsonError as exc:
        return Judgment(index=index, order=order, valid=False,
                        error=f"unparseable response: {exc}")

    raw_pref = payload.get("preferred")
    pref = str(raw_pref).strip().upper() if raw_pref is not None else ""
    if pref == "NEITHER":
        position = "neither"
    elif pref in ("A", "B"):
        position = pref
    else:
        return Judgment(index=index, order=order, valid=False,
                        error=f"'preferred' not in A/B/neither: {raw_pref!r}")

    raw_dims = payload.get("dimensions")
    if not isinstance(raw_dims, dict):
        return Judgment(index=index, order=order, valid=False,
                        error="'dimensions' missing or not an object")
    dims: dict = {}
    for name in DIMENSIONS:
        if name not in raw_dims:
            return Judgment(index=index, order=order, valid=False,
                            error=f"dimension {name!r} missing")
        value = raw_dims[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return Judgment(index=index, order=order, valid=False,
                            error=f"dimension {name!r} not numeric: {value!r}")
        # Integers only (2026-09-03, R3 S-4): the prompt asks for
        # ``<integer -2..2>``, and "strict, out-of-schema is discarded"
        # has to mean the schema it printed. ``1.5`` used to pass. A
        # float that IS a whole number (``2.0``, JSON from a client that
        # renders every number that way) is accepted as that integer.
        if isinstance(value, float) and not value.is_integer():
            return Judgment(index=index, order=order, valid=False,
                            error=f"dimension {name!r} not an integer: {value!r}")
        if not -2 <= float(value) <= 2:
            return Judgment(index=index, order=order, valid=False,
                            error=f"dimension {name!r} out of -2..2: {value!r}")
        dims[name] = float(value)

    # Normalize onto the PAIRING. In the "ba" order the judge's DECK B is
    # the pairing's deck A, so both the winner and every signed dimension
    # score flip. This is the single place the two orders are reconciled;
    # everything downstream counts decks, never seats.
    if position == "neither":
        preferred_deck = "neither"
    elif order == "ab":
        preferred_deck = "a" if position == "A" else "b"
    else:
        preferred_deck = "b" if position == "A" else "a"
    if order == "ba":
        dims = {name: -score for name, score in dims.items()}

    reasoning = str(payload.get("reasoning") or "").strip()
    return Judgment(
        index=index, order=order, valid=True,
        position_preference=position,
        preferred_deck=preferred_deck,
        dimensions=dims,
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _median(values: list[float]) -> Optional[float]:
    """Plain median; ``None`` for an empty list (never 0.0).

    "Unavailable != neutral" — the same contract ``card_score`` and
    ``deck_health`` keep. A dimension nobody scored must read as
    unmeasured, not as "the decks tied on it".
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


def _triad_seat_majority(judgments: list[Judgment]) -> Optional[str]:
    """Which SEAT this triad preferred, or None if it had no majority.

    Deliberately counts positions rather than decks: this is the input to
    the order-bias detector, and the detector's whole job is to notice
    when the panel is answering about seats.

    The majority is over the triad's SLOTS, not over the judgments that
    happened to answer. One seat-preferring judge out of three (the other
    two abstained or were discarded) is not a triad that "systematically
    prefers whichever deck was shown first" — declaring a flip on that
    evidence would let a single answer condemn a pairing, which is
    exactly the confident-single-opinion failure the panel exists to
    avoid.
    """
    valid = [j for j in judgments if j.valid and j.position_preference in ("A", "B")]
    if not valid:
        return None
    slots = len(judgments)
    first = sum(1 for j in valid if j.position_preference == "A")
    second = len(valid) - first
    if first > second and first * 2 > slots:
        return "first"
    if second > first and second * 2 > slots:
        return "second"
    return None


def reconcile(judgments: list[Judgment]) -> tuple[str, dict[str, int], bool]:
    """``(verdict, votes, order_flip)`` from a full panel.

    The gate, in order:

    1. **Order-flip first.** If both triads preferred the same SEAT, the
       panel disagreed with itself the moment the decks were transposed.
       That is ``inconclusive`` by definition (FP-016 §3) — not a
       tiebreak, and not something a lucky vote count may override. This
       check runs before the vote tally precisely so it cannot be
       outvoted.
    2. **Supermajority.** >= ``SUPERMAJORITY`` of ``PANEL_SIZE`` on the
       same deck. Discarded judgments count against the denominator.
    3. Otherwise ``inconclusive``.
    """
    votes = {"a": 0, "b": 0, "neither": 0}
    for j in judgments:
        if j.valid and j.preferred_deck in votes:
            votes[j.preferred_deck] += 1

    first_triad = [j for j in judgments if j.order == "ab"]
    second_triad = [j for j in judgments if j.order == "ba"]
    seat_a = _triad_seat_majority(first_triad)
    seat_b = _triad_seat_majority(second_triad)
    order_flip = seat_a is not None and seat_a == seat_b

    if order_flip:
        return "inconclusive", votes, True
    if votes["b"] >= SUPERMAJORITY:
        return "kept", votes, False
    if votes["a"] >= SUPERMAJORITY:
        return "reverted", votes, False
    if votes["neither"] >= SUPERMAJORITY:
        return "neutral", votes, False
    return "inconclusive", votes, False


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

def judge_pairing(
    deck_a_path,
    deck_b_path,
    *,
    intent=None,
    judge_fn: Optional[Callable[..., str]] = None,
    lookup: Optional[Callable[[str], Optional[dict]]] = None,
    model: str = DEFAULT_JUDGE_MODEL,
) -> JudgeReport:
    """Run the blinded six-judgment panel over one deck pairing.

    ``deck_a_path`` / ``deck_b_path`` are the pairing. Orientation matches
    the sim's: in the improve loop deck A is the current base and deck B
    is the candidate, so ``kept`` carries its usual meaning ("the change
    is an improvement"). The judge is never told which is which.

    ``intent`` is an ``intent.Intent`` (or None). Supplied rather than
    learned here so the judge is anchored to the SAME learned intent the
    improve loop protects cards with — one standard, not two.

    ``judge_fn(system, user, *, model) -> str`` is the transport seam.
    Defaults to the repo's SDK-or-subscription-CLI ladder; every test
    injects a stub, so no test in this repo makes a real Claude call.

    Six SEPARATE calls with no shared context, three per presentation
    order. Nothing about judgment *i* is passed to judgment *i+1* — a
    panel whose members can see each other is one judge with extra steps.

    Never raises for a bad judgment: a transport error or an
    out-of-schema answer discards that judgment and the pairing degrades
    honestly (see ``reconcile``). It DOES raise if the deck files cannot
    be read, because that is a caller error rather than a judge outcome.
    """
    deck_a_path = Path(deck_a_path)
    deck_b_path = Path(deck_b_path)
    deck_a_text = deck_a_path.read_text(encoding="utf-8")
    deck_b_text = deck_b_path.read_text(encoding="utf-8")
    call = judge_fn or _default_judge_fn

    system = judge_system_prompt()
    prompts = {
        "ab": build_judge_prompt(
            deck_a_text=deck_a_text, deck_b_text=deck_b_text,
            intent=intent, lookup=lookup,
        ),
        # The transposed prompt is built by swapping the ARGUMENTS, not by
        # editing the rendered text: the two prompts are then structurally
        # identical by construction, so a difference between the triads
        # can only come from the order itself.
        "ba": build_judge_prompt(
            deck_a_text=deck_b_text, deck_b_text=deck_a_text,
            intent=intent, lookup=lookup,
        ),
    }

    judgments: list[Judgment] = []
    notes: list[str] = []
    for index in range(PANEL_SIZE):
        order = "ab" if index < PER_ORDER else "ba"
        try:
            raw = call(system, prompts[order], model=model)
        except Exception as exc:  # noqa: BLE001 — one judgment, not the panel
            judgments.append(Judgment(
                index=index, order=order, valid=False,
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        judgments.append(_parse_judgment(raw or "", index=index, order=order))

    verdict, votes, order_flip = reconcile(judgments)
    discarded = sum(1 for j in judgments if not j.valid)
    if discarded:
        notes.append(
            f"{discarded} of {PANEL_SIZE} judgments were discarded "
            f"(transport failure or out-of-schema response); a verdict "
            f"still needs {SUPERMAJORITY} of {PANEL_SIZE} agreeing."
        )
    if order_flip:
        notes.append(
            "order flip: both triads preferred the deck in the same "
            "position, so this pairing is inconclusive by definition "
            "(FP-016 G1 counter)."
        )

    medians = {
        name: _median([
            j.dimensions[name] for j in judgments
            if j.valid and name in j.dimensions
        ])
        for name in DIMENSIONS
    }
    only_a, only_b, shared = changed_cards(deck_a_text, deck_b_text)
    # G3's input, computed here and only here: this is the one moment the
    # changed-card sets, the cache-resolved oracle text and THE INTENT THE
    # PANEL ACTUALLY JUDGED AGAINST are all in hand. Deriving it later from
    # a knowledge_log row would re-learn intent from today's deck and label
    # an old pairing against a standard it was never judged by.
    #
    # Never lets a labeling failure sink a panel that already ran: six
    # judgments have been spent by this point, and a bad lookup is not a
    # reason to lose them. An unlabeled pairing is simply outside G3's
    # population, which is the honest degradation.
    try:
        swap_label = classify_swap_direction(
            deck_a_text, deck_b_text, intent=intent, lookup=lookup,
        )
    except Exception as exc:  # noqa: BLE001 — see above
        swap_label = {
            "direction": "unknown",
            "reason": f"labeling failed ({type(exc).__name__}: {exc})",
        }
        notes.append(
            f"swap-direction labeling failed ({type(exc).__name__}); this "
            f"pairing is outside the G3 population."
        )
    return JudgeReport(
        verdict=verdict,
        votes=votes,
        dimension_medians=medians,
        judgments=judgments,
        order_flip=order_flip,
        all_kept=(verdict == "kept"),
        discarded=discarded,
        changed={
            "only_in_a": only_a,
            "only_in_b": only_b,
            "shared_count": len(shared),
        },
        swap_direction=str(swap_label.get("direction") or "unknown"),
        swap_label=swap_label,
        intent=intent.to_dict() if hasattr(intent, "to_dict") else None,
        model=model,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# CLI — `commander judge` / `commander-judge`
# ---------------------------------------------------------------------------

def render_report(report: JudgeReport, deck_a: Path, deck_b: Path) -> str:
    """Human-readable report. Honest by construction: the verdict, the
    per-dimension medians, WHERE the panel disagreed, and the caveat.

    The disagreement detail is not optional decoration. A pooled verdict
    with no visible split reads like a measurement; showing the vote
    counts, the discarded judgments and each judge's one-line reasoning
    is what keeps it legible as six opinions.
    """
    lines: list[str] = [
        "Deck judge — blinded panel of "
        f"{report.panel_size} ({report.per_order} per presentation order)",
        "",
        f"  deck A: {deck_a.name}",
        f"  deck B: {deck_b.name}",
        "",
        f"  VERDICT: {report.verdict}"
        f"   (kept = deck B is the better-built one; "
        f"reverted = deck A; neutral = no meaningful difference)",
        f"  votes:   deck A {report.votes.get('a', 0)} / "
        f"deck B {report.votes.get('b', 0)} / "
        f"neither {report.votes.get('neither', 0)}"
        f"   [needs {report.supermajority} of {report.panel_size}]",
    ]
    if report.discarded:
        lines.append(
            f"  discarded: {report.discarded} judgment(s) — see below"
        )
    lines.append(f"  order flip: {'YES' if report.order_flip else 'no'}")
    # Printed with its REASON, never as a bare word: "staple_ward" alone
    # invites the reader to trust a classifier they cannot see, and this
    # one is a heuristic over two name lists and a theme matcher.
    reason = str(report.swap_label.get("reason") or "")
    lines.append(
        f"  swap direction: {report.swap_direction}"
        + (f"  ({reason})" if reason else "")
    )
    lines.append("")
    lines.append("  Per-dimension median (signed toward deck B, -2..+2):")
    for name in DIMENSIONS:
        value = report.dimension_medians.get(name)
        shown = "unmeasured" if value is None else f"{value:+.1f}"
        lines.append(f"    {name:<22} {shown}")
    lines.append("")
    lines.append("  Panel detail:")
    for j in report.judgments:
        if not j.valid:
            lines.append(
                f"    [{j.index}] order {j.order}: DISCARDED — {j.error}"
            )
            continue
        pick = {"a": "deck A", "b": "deck B"}.get(j.preferred_deck, "neither")
        reasoning = j.reasoning or "(no reasoning given)"
        lines.append(f"    [{j.index}] order {j.order}: prefers {pick}")
        lines.append(f"          {reasoning}")
    for note in report.notes:
        lines.append(f"  NOTE: {note}")
    lines.append("")
    lines.append(f"  {OPINION_CAVEAT}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """``commander judge DECK_A DECK_B`` — one ad-hoc panel, printed.

    Ad-hoc invocation is a DIAGNOSTIC surface, not the sampling path:
    decision D3 keeps the Phase 1 sample unbiased by running the judge
    automatically on every round when the flag is on. Judging a pairing
    by hand here is for reading the panel's reasoning; it deliberately
    does not write a ``judge_verdict`` row, so hand-picked pairings can
    never leak into the agreement table.
    """
    parser = argparse.ArgumentParser(
        prog="commander-judge",
        description=(
            "Blinded LLM panel comparing two decks on construction quality "
            "for a deck's stated intent. " + OPINION_CAVEAT
        ),
    )
    parser.add_argument("deck_a", type=Path, help="first deck (.dck)")
    parser.add_argument("deck_b", type=Path, help="second deck (.dck)")
    parser.add_argument(
        "--no-intent", action="store_true",
        help="skip intent.learn_intent (faster; the panel is then judging "
             "against generic construction, which it will say in its "
             "reasoning)",
    )
    parser.add_argument(
        "--model", default=DEFAULT_JUDGE_MODEL,
        help="model id for the SDK path (default %(default)s; the "
             "subscription CLI path uses its own configured model)",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    for path in (args.deck_a, args.deck_b):
        if not path.exists():
            print(f"commander-judge: no such deck: {path}", file=sys.stderr)
            return 2

    learned = None
    if not args.no_intent:
        try:
            from .intent import learn_intent
            learned = learn_intent(args.deck_a)
        except Exception as exc:  # noqa: BLE001 — the anchor is best-effort
            print(f"commander-judge: WARN: could not learn intent "
                  f"({type(exc).__name__}: {exc}); judging without it.",
                  file=sys.stderr)

    try:
        report = judge_pairing(
            args.deck_a, args.deck_b, intent=learned, model=args.model,
        )
    except Exception as exc:  # noqa: BLE001 — report, don't traceback
        print(f"commander-judge: panel failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_report(report, args.deck_a, args.deck_b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
