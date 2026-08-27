"""FP-016 Phase 1 — the blinded LLM deck judge.

OFFLINE ONLY. Every test injects ``judge_fn``; nothing here can reach the
Anthropic SDK or the ``claude`` CLI, and a test that tried would fail on
the stub's call counter rather than quietly spending money.

The load-bearing tests are the ones that pin the design's two hardest
claims: that agreement is counted on the DECK and never on the position
(``test_same_deck_winning_in_both_orders_is_agreement`` vs
``test_position_consistent_answers_are_an_order_flip``), and that the
prompt is genuinely blind (``test_prompt_never_names_the_decks``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commander_builder import deck_judge
from commander_builder._deck_judge_prompt import (
    DIMENSIONS,
    build_judge_prompt,
    changed_cards,
)
from commander_builder.deck_judge import (
    PANEL_SIZE,
    PER_ORDER,
    SUPERMAJORITY,
    JudgeReport,
    judge_pairing,
    reconcile,
)


# --------------------------------------------------------------------------- #
# Fixtures / doubles
# --------------------------------------------------------------------------- #

def _deck(name: str, cards: list) -> str:
    lines = [
        "[metadata]",
        f"Name={name}",
        "[Commander]",
        "1 Hazel of the Rootbloom",
        "[Main]",
    ]
    lines += [f"1 {c}" for c in cards]
    return "\n".join(lines) + "\n"


_SHARED = [f"Shared Card {i}" for i in range(1, 6)]


@pytest.fixture
def pairing(tmp_path):
    """Two decks differing by one card. ``a`` is the incumbent, ``b`` the
    candidate — a fact the judge must never be able to recover."""
    a = tmp_path / "[USER] Hazel INCUMBENT v1 [B3].dck"
    b = tmp_path / "[USER] Hazel CANDIDATE v2 [B3].dck"
    a.write_text(_deck("[USER] Hazel INCUMBENT v1 [B3]", _SHARED + ["Old Card"]),
                 encoding="utf-8")
    b.write_text(_deck("[USER] Hazel CANDIDATE v2 [B3]", _SHARED + ["New Card"]),
                 encoding="utf-8")
    return a, b


def _answer(preferred: str, *, scores=None, reasoning="fine") -> str:
    """A schema-valid judgment, as the model would return it."""
    scores = scores if scores is not None else {d: 0 for d in DIMENSIONS}
    return json.dumps({
        "preferred": preferred,
        "dimensions": scores,
        "reasoning": reasoning,
    })


def _scripted(answers: list):
    """judge_fn double: hands back one scripted response per call and
    records (system, user, model) so tests can assert on the prompt."""
    calls: list = []

    def fn(system, user, *, model=None):
        calls.append((system, user, model))
        return answers[len(calls) - 1]

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def _no_lookup(name):
    """Oracle resolver double: nothing resolves, so tests never touch the
    Scryfall cache (or the network) and the prompt exercises the
    unresolvable-card branch."""
    return None


# --------------------------------------------------------------------------- #
# Panel geometry
# --------------------------------------------------------------------------- #

def test_panel_splits_evenly_across_two_orders():
    """Decision D2's whole reason for 6 over 5: an odd panel cannot split
    evenly across two orders, which confounds position bias with judge
    variance."""
    assert PANEL_SIZE == 2 * PER_ORDER
    assert SUPERMAJORITY == 5 and SUPERMAJORITY <= PANEL_SIZE


def test_six_independent_calls_three_per_order(pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    assert len(fn.calls) == PANEL_SIZE
    assert [j.order for j in report.judgments] == ["ab"] * 3 + ["ba"] * 3
    # Independence: exactly two distinct user prompts (one per order) and
    # nothing from a previous judgment is threaded into a later one.
    prompts = {user for _sys, user, _m in fn.calls}
    assert len(prompts) == 2
    systems = {sysmsg for sysmsg, _u, _m in fn.calls}
    assert len(systems) == 1


def test_the_two_orders_are_transposes_of_each_other(pairing):
    a, b = pairing
    fn = _scripted([_answer("neither")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    ab_prompt = fn.calls[0][1]
    ba_prompt = fn.calls[PER_ORDER][1]

    assert "ONLY IN DECK A (1 card(s)" in ab_prompt
    assert "Old Card" in ab_prompt.split("ONLY IN DECK B")[0]
    # Transposed: the same card is now on the other side.
    assert "New Card" in ba_prompt.split("ONLY IN DECK B")[0]
    assert "Old Card" in ba_prompt.split("ONLY IN DECK B")[1]


# --------------------------------------------------------------------------- #
# Blinding
# --------------------------------------------------------------------------- #

_INCUMBENT_MARKERS = (
    "incumbent", "candidate", "current", "proposed", "baseline",
    "original", "before", "after", "v1", "v2", "old deck", "new deck",
)


def test_prompt_never_names_the_decks(pairing):
    """Blinding, the hard way: the deck FILENAMES carry 'INCUMBENT' /
    'CANDIDATE' / 'v1' / 'v2', and so does each deck's [metadata] Name=
    line. None of it may reach the model — which is why the prompt is
    built from card names and never from raw .dck text."""
    a, b = pairing
    fn = _scripted([_answer("A")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    for _system, user, _model in fn.calls:
        lowered = user.lower()
        assert a.name.lower() not in lowered
        assert b.name.lower() not in lowered
        assert "[metadata]" not in lowered
        assert "name=" not in lowered
        for marker in _INCUMBENT_MARKERS:
            assert marker not in lowered, f"blinding leak: {marker!r}"


def test_system_prompt_forbids_the_outcome_question(pairing):
    """FP-016 §1: the judge must never answer 'which deck wins more
    games', and the refusal has to live in the judge's own instructions
    rather than only in our documentation."""
    a, b = pairing
    fn = _scripted([_answer("A")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    system = fn.calls[0][0]

    assert "may NOT answer" in system
    assert "which deck wins more games" in system
    assert "no simulation results" in system
    assert "Do not write" in system
    # And it demands the strict object.
    assert '"preferred"' in system and '"dimensions"' in system
    for dim in DIMENSIONS:
        assert dim in system


def test_prompt_is_diff_focused_not_a_deck_dump(pairing):
    """Decision D5: full oracle text for CHANGED cards only, role-tagged
    names for the rest."""
    a, b = pairing
    fn = _scripted([_answer("neither")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    user = fn.calls[0][1]

    assert "SHARED BY BOTH DECKS (5 card(s), names + role tags)" in user
    for shared in _SHARED:
        # Named once, in the compact list — never with an oracle block.
        assert f"  {shared} [" in user


def test_unresolvable_cards_are_named_not_dropped(pairing):
    """A silent drop would leave the judge free to fill the gap from
    memory — the exact failure the retrieval discipline exists to stop."""
    a, b = pairing
    fn = _scripted([_answer("neither")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    user = fn.calls[0][1]
    assert "ORACLE TEXT UNAVAILABLE" in user
    assert "Do not reason about it from memory" in user


def test_oracle_text_is_supplied_when_the_snapshot_has_it(pairing):
    a, b = pairing
    oracles = {
        "New Card": {
            "type_line": "Enchantment",
            "oracle_text": "Creatures can't attack you unless their "
                           "controller pays {2}.",
            "mana_cost": "{2}{U}",
        },
    }
    fn = _scripted([_answer("B")] * 6)
    judge_pairing(a, b, judge_fn=fn, lookup=oracles.get)
    user = fn.calls[0][1]
    assert "Creatures can't attack you unless their controller pays {2}." in user
    assert "Enchantment" in user


def test_intent_is_the_standard_and_is_stated(pairing, tmp_path):
    """FP-016 §4: without the intent anchor an LLM panel converges every
    deck toward the EDHREC average."""
    from commander_builder.intent import Intent

    a, b = pairing
    intent = Intent(archetype="combo", themes=["squirrels", "sacrifice"],
                    key_wincons=["Chatterfang, Squirrel General"],
                    tribal_type="Squirrel")
    fn = _scripted([_answer("B")] * 6)
    report = judge_pairing(a, b, intent=intent, judge_fn=fn, lookup=_no_lookup)

    user = fn.calls[0][1]
    assert "STATED INTENT" in user
    assert "archetype: combo" in user
    assert "squirrels" in user
    assert "Chatterfang, Squirrel General" in user
    assert report.intent["archetype"] == "combo"


def test_missing_intent_says_so_rather_than_inventing_one(pairing):
    a, b = pairing
    fn = _scripted([_answer("neither")] * 6)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert "not supplied" in fn.calls[0][1]
    assert report.intent is None


# --------------------------------------------------------------------------- #
# Deck-keyed reconciliation — the heart of the design
# --------------------------------------------------------------------------- #

def test_same_deck_winning_in_both_orders_is_agreement(pairing):
    """The candidate deck wins whichever seat it occupies. In the 'ab'
    triad it is DECK B; in the transposed triad it is DECK A. Six
    POSITION answers that look split are one unanimous DECK answer."""
    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    assert report.votes == {"a": 0, "b": 6, "neither": 0}
    assert report.order_flip is False
    assert report.verdict == "kept"
    assert report.all_kept is True


def test_position_consistent_answers_are_an_order_flip(pairing):
    """Every judge picks whatever was shown FIRST. Deck-keyed that is 3-3,
    but the honest reading is that the panel reported its seating chart —
    inconclusive by definition, and the G1 counter."""
    a, b = pairing
    fn = _scripted([_answer("A")] * 6)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    assert report.order_flip is True
    assert report.verdict == "inconclusive"
    assert report.votes == {"a": 3, "b": 3, "neither": 0}
    assert any("order flip" in n for n in report.notes)


def test_second_position_bias_is_also_an_order_flip(pairing):
    """The mirror case: everyone prefers whatever was shown SECOND."""
    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    # Both triads prefer seat B, so deck-keyed this is 3 for b and 3 for a.
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.order_flip is True
    assert report.verdict == "inconclusive"


def test_reverted_when_the_first_deck_wins_in_both_orders(pairing):
    a, b = pairing
    fn = _scripted([_answer("A")] * 3 + [_answer("B")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.votes["a"] == 6
    assert report.verdict == "reverted"
    assert report.all_kept is False


def test_unanimous_neither_is_neutral(pairing):
    a, b = pairing
    fn = _scripted([_answer("neither")] * 6)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.verdict == "neutral"
    assert report.order_flip is False


def test_dimension_scores_are_signed_toward_deck_b_in_both_orders(pairing):
    """A judge that says 'the deck in slot B is +2 on politics' means
    opposite things in the two orders. Medians only pool if the sign is
    normalized onto the pairing first."""
    a, b = pairing
    pro_b = {d: 2 for d in DIMENSIONS}
    # 'ab' triad: +2 toward slot B == +2 toward deck b.
    # 'ba' triad: +2 toward slot B == +2 toward deck a == -2 toward deck b,
    # so a panel that consistently likes deck b must answer -2 there.
    anti_b_seat = {d: -2 for d in DIMENSIONS}
    fn = _scripted(
        [_answer("B", scores=pro_b)] * 3
        + [_answer("A", scores=anti_b_seat)] * 3
    )
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    for dim in DIMENSIONS:
        assert report.dimension_medians[dim] == 2.0
    assert report.verdict == "kept"


def test_unmeasured_dimension_is_none_not_zero():
    """"Unavailable != neutral" — the contract card_score / deck_health
    keep. A dimension nobody scored must not read as a tie."""
    report = JudgeReport(verdict="inconclusive", votes={}, dimension_medians={})
    assert deck_judge._median([]) is None
    assert report.dimension_medians == {}


# --------------------------------------------------------------------------- #
# Supermajority boundary
# --------------------------------------------------------------------------- #

def test_five_of_six_clears_the_supermajority(pairing):
    a, b = pairing
    # 5 for deck b, 1 for deck a; kept without an order flip (the 'ab'
    # triad prefers seat B, the 'ba' triad prefers seat A).
    fn = _scripted(
        [_answer("B"), _answer("B"), _answer("B"),
         _answer("A"), _answer("A"), _answer("B")]
    )
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.votes == {"a": 1, "b": 5, "neither": 0}
    assert report.order_flip is False
    assert report.verdict == "kept"


def test_four_of_six_is_inconclusive(pairing):
    a, b = pairing
    fn = _scripted(
        [_answer("B"), _answer("B"), _answer("neither"),
         _answer("A"), _answer("A"), _answer("neither")]
    )
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.votes == {"a": 0, "b": 4, "neither": 2}
    assert report.verdict == "inconclusive"


def test_supermajority_is_out_of_the_panel_not_the_survivors(pairing):
    """Two judgments lost, four agreeing. Five is no longer reachable, so
    the pairing degrades honestly instead of quietly lowering its bar to
    'four of the four that answered'."""
    a, b = pairing
    fn = _scripted(
        [_answer("B"), _answer("B"), "not json at all",
         _answer("A"), _answer("A"), "{}"]
    )
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.discarded == 2
    assert report.valid_count == 4
    assert report.votes["b"] == 4
    assert report.verdict == "inconclusive"


# --------------------------------------------------------------------------- #
# Out-of-schema handling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad, why", [
    ("total garbage, no object here", "unparseable"),
    (json.dumps({"dimensions": {d: 0 for d in DIMENSIONS}}), "no preferred"),
    (json.dumps({"preferred": "maybe",
                 "dimensions": {d: 0 for d in DIMENSIONS}}), "bad preferred"),
    (json.dumps({"preferred": "A"}), "no dimensions"),
    (json.dumps({"preferred": "A", "dimensions": "nope"}), "dimensions not obj"),
    (json.dumps({"preferred": "A",
                 "dimensions": {d: 0 for d in DIMENSIONS[:-1]}}), "missing dim"),
    (json.dumps({"preferred": "A",
                 "dimensions": {**{d: 0 for d in DIMENSIONS},
                                DIMENSIONS[0]: 9}}), "out of range"),
    (json.dumps({"preferred": "A",
                 "dimensions": {**{d: 0 for d in DIMENSIONS},
                                DIMENSIONS[0]: "high"}}), "non-numeric"),
])
def test_out_of_schema_judgment_is_discarded_with_a_reason(pairing, bad, why):
    a, b = pairing
    fn = _scripted([bad] + [_answer("B")] * 2 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    assert report.discarded == 1
    bad_judgment = report.judgments[0]
    assert bad_judgment.valid is False
    assert bad_judgment.error, f"{why}: a discarded judgment must say why"
    # It keeps its slot: the panel had six chances and one was wasted.
    assert len(report.judgments) == PANEL_SIZE
    assert any("discarded" in n for n in report.notes)


def test_fenced_json_still_counts(pairing):
    """A markdown fence is a transport artifact, not a schema failure —
    the shared _llm_json extractor handles it and the judgment stands."""
    a, b = pairing
    fenced = "Here you go:\n```json\n" + _answer("B") + "\n```\nhope that helps"
    fn = _scripted([fenced] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.discarded == 0
    assert report.verdict == "kept"


def test_a_transport_failure_discards_one_judgment_not_the_panel(pairing):
    a, b = pairing
    calls = {"n": 0}

    def flaky(system, user, *, model=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("rate limited")
        return _answer("B") if calls["n"] <= 3 else _answer("A")

    report = judge_pairing(a, b, judge_fn=flaky, lookup=_no_lookup)
    assert calls["n"] == PANEL_SIZE          # the panel kept going
    assert report.discarded == 1
    assert "RuntimeError" in report.judgments[1].error
    assert report.votes["b"] == 5
    assert report.verdict == "kept"


# --------------------------------------------------------------------------- #
# reconcile() directly — the gate order matters
# --------------------------------------------------------------------------- #

def _j(index, order, position):
    """A minimal valid Judgment for reconcile() unit tests."""
    from commander_builder.deck_judge import Judgment
    if position == "neither":
        deck = "neither"
    elif order == "ab":
        deck = "a" if position == "A" else "b"
    else:
        deck = "b" if position == "A" else "a"
    return Judgment(index=index, order=order, valid=True,
                    position_preference=position, preferred_deck=deck,
                    dimensions={d: 0.0 for d in DIMENSIONS})


def test_order_flip_is_checked_before_the_vote_tally():
    """It is inconclusive BY DEFINITION, not a tiebreak — so it must not
    be possible for a vote count to overrule it."""
    judgments = [_j(i, "ab", "A") for i in range(3)]
    judgments += [_j(i, "ba", "A") for i in range(3, 6)]
    verdict, votes, flip = reconcile(judgments)
    assert flip is True and verdict == "inconclusive"
    assert votes == {"a": 3, "b": 3, "neither": 0}


def test_a_triad_with_no_majority_cannot_declare_a_flip():
    """One-of-three is not a triad majority, so the detector stays quiet
    rather than calling a flip on a single judge's seat preference."""
    judgments = [_j(0, "ab", "A"), _j(1, "ab", "B"), _j(2, "ab", "neither")]
    judgments += [_j(3, "ba", "A"), _j(4, "ba", "B"), _j(5, "ba", "neither")]
    _verdict, _votes, flip = reconcile(judgments)
    assert flip is False


# --------------------------------------------------------------------------- #
# Flag
# --------------------------------------------------------------------------- #

def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(deck_judge.DECK_JUDGE_ENV_VAR, raising=False)
    assert deck_judge.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
def test_flag_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(deck_judge.DECK_JUDGE_ENV_VAR, value)
    assert deck_judge.is_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "maybe"])
def test_flag_everything_else_is_off(monkeypatch, value):
    monkeypatch.setenv(deck_judge.DECK_JUDGE_ENV_VAR, value)
    assert deck_judge.is_enabled() is False


def test_flag_off_means_zero_calls(monkeypatch, pairing):
    """The cost control: while the flag is off nothing is spent. The judge
    entry point the loop uses must not call the transport at all."""
    from commander_builder.improve import _default_judge_round

    monkeypatch.delenv(deck_judge.DECK_JUDGE_ENV_VAR, raising=False)
    a, b = pairing

    def explode(*args, **kwargs):  # pragma: no cover — asserted not to run
        raise AssertionError("judge_pairing called with the flag off")

    monkeypatch.setattr(deck_judge, "judge_pairing", explode)
    import argparse
    args = argparse.Namespace(db_path=None, intent=None)
    assert _default_judge_round(a, b, args, 1) is None


# --------------------------------------------------------------------------- #
# Vocabulary + report shape
# --------------------------------------------------------------------------- #

def test_verdicts_reuse_the_existing_vocabulary():
    """No new labels — the same discipline the replication work followed,
    and the same set knowledge_log's verdict column accepts."""
    assert deck_judge.JUDGE_VERDICTS == {
        "kept", "reverted", "neutral", "inconclusive",
    }


def test_report_is_json_serializable(pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    blob = json.dumps(report.to_dict())
    round_tripped = json.loads(blob)
    assert round_tripped["verdict"] == "kept"
    assert len(round_tripped["judgments"]) == PANEL_SIZE
    assert round_tripped["caveat"] == deck_judge.OPINION_CAVEAT


def test_report_carries_the_kill_criteria_counters(pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.order_flip is False        # G1's counter
    assert report.all_kept is True           # G2's counter
    assert report.changed["only_in_a"] == ["Old Card"]
    assert report.changed["only_in_b"] == ["New Card"]
    assert report.changed["shared_count"] == 5


def test_changed_cards_ignores_shared_and_commander():
    a_text = _deck("A", ["X", "Y", "Z"])
    b_text = _deck("B", ["X", "Y", "W"])
    only_a, only_b, shared = changed_cards(a_text, b_text)
    assert only_a == ["Z"] and only_b == ["W"]
    assert shared == ["X", "Y"]


def test_prompt_builder_takes_text_not_paths():
    """Structural blinding: there is no code path that hands a filename to
    the prompt builder, because it does not accept one."""
    prompt = build_judge_prompt(
        deck_a_text=_deck("A", ["X"]), deck_b_text=_deck("B", ["Y"]),
        lookup=_no_lookup,
    )
    assert "DECK A" in prompt and "DECK B" in prompt
    with pytest.raises(TypeError):
        build_judge_prompt(deck_a_path=Path("a.dck"))  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Storage — schema v4
# --------------------------------------------------------------------------- #

def test_judge_columns_round_trip(tmp_path, pairing):
    from commander_builder.knowledge_log import (
        Iteration, get_iteration, record_iteration, update_iteration_judge,
    )

    db = tmp_path / "kl.sqlite"
    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)

    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    # Fresh row: NULL until the judge runs.
    assert get_iteration(row_id, db_path=db).judge_verdict is None
    assert get_iteration(row_id, db_path=db).judge_report is None

    update_iteration_judge(row_id, report.verdict, report.to_dict(), db_path=db)
    stored = get_iteration(row_id, db_path=db)
    assert stored.judge_verdict == "kept"
    assert stored.judge_report["votes"]["b"] == 6
    assert len(stored.judge_report["judgments"]) == PANEL_SIZE


def test_judge_write_never_touches_the_sim_verdict(tmp_path):
    from commander_builder.knowledge_log import (
        Iteration, get_iteration, record_iteration, update_iteration_judge,
        update_iteration_sim,
    )

    db = tmp_path / "kl.sqlite"
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    update_iteration_sim(row_id, "reverted", sim_report={"wins_a": 20},
                         margin=-8, notes="A/B sim", db_path=db)
    update_iteration_judge(row_id, "kept", {"verdict": "kept"}, db_path=db)

    stored = get_iteration(row_id, db_path=db)
    # Two instruments, two columns. The judge's opinion sits BESIDE the
    # sim's verdict and did not overwrite any of it.
    assert stored.verdict == "reverted"
    assert stored.sim_report == {"wins_a": 20}
    assert stored.margin == -8
    assert stored.verdict_notes == "A/B sim"
    assert stored.judge_verdict == "kept"


def test_judge_verdict_vocabulary_is_enforced(tmp_path):
    from commander_builder.knowledge_log import (
        Iteration, record_iteration, update_iteration_judge,
    )

    db = tmp_path / "kl.sqlite"
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3), db_path=db,
    )
    with pytest.raises(ValueError):
        update_iteration_judge(row_id, "excellent", db_path=db)
    # 'pending' is deliberately NOT a judge verdict: a panel that did not
    # run leaves NULL rather than claiming a state.
    with pytest.raises(ValueError):
        update_iteration_judge(row_id, "pending", db_path=db)


def test_migration_is_idempotent_and_backfills_nothing(tmp_path):
    """Same check-then-add shape as v2/v3, and NO backfill: a judge
    verdict cannot be derived from anything, so historical rows keep NULL
    rather than gaining a fabricated opinion."""
    import sqlite3

    from commander_builder.knowledge_log import (
        SCHEMA_VERSION, Iteration, init_db, record_iteration,
    )

    db = tmp_path / "kl.sqlite"
    init_db(db)
    row_id = record_iteration(
        Iteration(deck_id="d", deck_name="D", bracket=3, verdict="kept"),
        db_path=db,
    )
    # Re-running every migration must be a no-op, not a duplicate-column
    # error.
    for _ in range(3):
        init_db(db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(iterations)")}
    assert {"judge_verdict", "judge_report"} <= cols
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 4
    row = conn.execute(
        "SELECT judge_verdict, judge_report, verdict FROM iterations WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["judge_verdict"] is None and row["judge_report"] is None
    assert row["verdict"] == "kept"      # untouched by the migration
    indexes = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_iterations_judge_verdict" in indexes
    conn.close()


def test_migration_upgrades_a_v3_database_in_place(tmp_path):
    """The real migration case: a database created before the columns
    existed keeps its rows and gains the columns as NULL."""
    import sqlite3

    from commander_builder.knowledge_log import get_iteration, init_db

    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        INSERT INTO schema_version (version) VALUES (3);
        CREATE TABLE iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id TEXT NOT NULL, deck_name TEXT NOT NULL,
            bracket INTEGER NOT NULL, parent_id INTEGER,
            audit_version TEXT, audit_manifest TEXT, sim_report TEXT,
            verdict TEXT NOT NULL DEFAULT 'pending', verdict_notes TEXT,
            win_rate_old REAL, win_rate_new REAL, margin INTEGER,
            created_at TEXT NOT NULL, deck_snapshot TEXT,
            milestone TEXT, measurement_era INTEGER
        );
        INSERT INTO iterations (deck_id, deck_name, bracket, verdict, created_at)
        VALUES ('legacy', 'Legacy', 3, 'neutral', '2026-08-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    init_db(db)
    stored = get_iteration(1, db_path=db)
    assert stored.verdict == "neutral"
    assert stored.judge_verdict is None
    assert stored.judge_report is None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_prints_the_verdict_dimensions_disagreement_and_caveat(
    monkeypatch, capsys, pairing,
):
    a, b = pairing
    fn = _scripted(
        [_answer("B", reasoning="tighter curve"),
         _answer("B", reasoning="more redundancy"),
         _answer("neither", reasoning="a wash"),
         _answer("A", reasoning="better politics"),
         _answer("A", reasoning="more resilient"),
         _answer("A", reasoning="seat bias maybe")]
    )
    monkeypatch.setattr(
        deck_judge, "_default_judge_fn",
        lambda system, user, *, model: fn(system, user, model=model),
    )
    rc = deck_judge.main([str(a), str(b), "--no-intent"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "VERDICT:" in out
    for dim in DIMENSIONS:
        assert dim in out
    # Disagreement detail: the split, and every judge's own line.
    assert "votes:" in out
    assert "tighter curve" in out and "better politics" in out
    assert "order flip" in out
    # The standing caveat, verbatim.
    assert deck_judge.OPINION_CAVEAT in out
    assert "Forge decides which deck is better. Only Forge." in out


def test_cli_json_mode_emits_the_report(monkeypatch, capsys, pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    monkeypatch.setattr(
        deck_judge, "_default_judge_fn",
        lambda system, user, *, model: fn(system, user, model=model),
    )
    rc = deck_judge.main([str(a), str(b), "--no-intent", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["verdict"] == "kept"
    assert payload["caveat"] == deck_judge.OPINION_CAVEAT


def test_cli_missing_deck_is_a_clean_error(capsys, tmp_path):
    rc = deck_judge.main([str(tmp_path / "nope.dck"), str(tmp_path / "also.dck")])
    assert rc == 2
    assert "no such deck" in capsys.readouterr().err


def test_cli_does_not_write_a_judge_row(monkeypatch, capsys, tmp_path, pairing):
    """Decision D3: hand-picked pairings must never leak into the
    agreement table, so the ad-hoc CLI is read-only by construction."""
    from commander_builder import knowledge_log

    a, b = pairing
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    monkeypatch.setattr(
        deck_judge, "_default_judge_fn",
        lambda system, user, *, model: fn(system, user, model=model),
    )

    def explode(*args, **kwargs):  # pragma: no cover — asserted not to run
        raise AssertionError("the ad-hoc CLI wrote a judge verdict")

    monkeypatch.setattr(knowledge_log, "update_iteration_judge", explode)
    assert deck_judge.main([str(a), str(b), "--no-intent"]) == 0
    capsys.readouterr()


def test_model_default_is_the_strongest_tier():
    """Decision D4 (owner call): panel size buys down variance, not bias,
    so the honest lever is a stronger judge rather than more cheap ones."""
    assert deck_judge.DEFAULT_JUDGE_MODEL == "claude-opus-5"


# --------------------------------------------------------------------------- #
# Transport routing — the ladder is reused, not reinvented
# --------------------------------------------------------------------------- #

def test_no_key_routes_to_the_subscription_cli(monkeypatch):
    """The same auth ladder ``proposer.auto_propose`` established: no
    (non-empty) key means the subscription `claude` CLI, because the judge
    runs unattended inside the improve loop and must work under a Claude
    Max plan."""
    from commander_builder import proposer

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")   # present-but-empty == no key
    seen = {}

    def fake_cli(*, system, user_msg, model):
        seen.update(system=system, user=user_msg, model=model)
        return "{}"

    monkeypatch.setattr(proposer, "_claude_cli_available", lambda: True)
    monkeypatch.setattr(proposer, "_curator_complete_via_cli", fake_cli)
    out = deck_judge._default_judge_fn("SYS", "USER", model="m")

    assert out == "{}"
    assert seen == {"system": "SYS", "user": "USER", "model": "m"}


def test_no_key_and_no_cli_is_a_named_failure(monkeypatch):
    from commander_builder import proposer

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(proposer, "_claude_cli_available", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        deck_judge._default_judge_fn("SYS", "USER", model="m")
    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert "`claude` CLI" in str(exc.value)


def test_a_key_routes_to_the_sdk_with_no_sampling_params(monkeypatch):
    """The panel WANTS the model's own non-determinism — that variance is
    exactly what six judgments measure — and the strongest-tier models
    reject sampling parameters outright, so none are sent."""
    import sys as _sys
    import types

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-byo-12345")
    seen = {}

    class _Block:
        text = '{"ok": true}'

    class _Messages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(content=[_Block()])

    class _Anthropic:
        def __init__(self, api_key=None):
            seen["api_key"] = api_key
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Anthropic
    monkeypatch.setitem(_sys.modules, "anthropic", fake)

    out = deck_judge._default_judge_fn("SYS", "USER", model="claude-opus-5")
    assert out == '{"ok": true}'
    assert seen["api_key"] == "sk-test-byo-12345"
    assert seen["model"] == "claude-opus-5"
    assert seen["system"] == "SYS"
    assert seen["messages"] == [{"role": "user", "content": "USER"}]
    assert "temperature" not in seen and "top_p" not in seen


# --------------------------------------------------------------------------- #
# The real stated-intent capture — and the Phase 1 boundary around it
# --------------------------------------------------------------------------- #

_PRIMER = Path(__file__).parent / "fixtures" / "hazel_primer.md"


def test_the_real_primer_is_a_quill_delta_not_prose():
    """``tests/fixtures/hazel_primer.md`` is the owner's real stated
    intent, kept for this feature. It carries the trap that any future
    primer-reading code has to survive: Archidekt's ``description`` is a
    Quill Delta JSON *string*, and the readable section of the fixture is
    labeled DERIVED so nobody mistakes it for the field."""
    text = _PRIMER.read_text(encoding="utf-8")
    delta_line = next(
        line for line in text.splitlines() if line.startswith('{"ops"')
    )
    delta = json.loads(delta_line)
    assert isinstance(delta["ops"][0]["insert"], str)
    assert "DERIVED" in text
    # The player's own words name the plan the judge would be measured
    # against — sacrifice + tokens + drain, not generic Golgari goodstuff.
    rendered = delta["ops"][0]["insert"]
    assert "sacrifice theme" in rendered and "Squirrel" in rendered


def test_phase_1_anchors_on_learned_intent_not_the_written_primer(pairing):
    """The Phase 1 boundary, pinned so it is a decision rather than an
    oversight: FP-016 §4 makes ``intent.learn_intent`` the standard, so
    the prompt carries the DERIVED intent and no free-text primer. When
    someone wires the richer anchor, this test is the thing that should
    change."""
    from commander_builder.intent import Intent

    a, b = pairing
    learned = Intent(
        archetype="combo",
        themes=["sacrifice", "tokens"],
        key_wincons=["Chatterfang, Squirrel General"],
        tribal_type="Squirrel",
    )
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    judge_pairing(a, b, intent=learned, judge_fn=fn, lookup=_no_lookup)
    user = fn.calls[0][1]

    assert "archetype: combo" in user
    assert "themes: sacrifice, tokens" in user
    # No slice of the owner's written primer reaches the prompt today.
    assert "Precon" not in user and "cryptolith rite" not in user.lower()


# --------------------------------------------------------------------------- #
# Swap-direction labeling — G3's missing input (added 2026-08-27)
# --------------------------------------------------------------------------- #
#
# These pin the LABEL, not the gate; the gate that reads it lives in
# tests/test_judge_agreement.py. The two properties that matter here are
# that the label is conservative (``unknown`` whenever it cannot honestly
# decide) and that the judge never sees it.

from commander_builder._deck_judge_prompt import (  # noqa: E402
    SWAP_LABEL_DOMINANCE,
    SWAP_LABEL_MIN_CARDS,
    classify_swap_direction,
)

_TOKENS_ORACLE = "Create a 1/1 green Squirrel creature token."
_SPELLS_ORACLE = "Whenever you cast an instant or sorcery spell, draw a card."
_PLAIN_ORACLE = "Flying. When this creature dies, you gain 1 life."


def _squirrel_intent(**over):
    from commander_builder.intent import Intent
    kwargs = dict(archetype="midrange", themes=["tokens"],
                  key_wincons=[], tribal_type=None)
    kwargs.update(over)
    return Intent(**kwargs)


def _swap(added: list, removed=("Old Card",), oracles=None):
    """Label a synthetic swap: ``added`` goes into deck B, ``removed``
    into deck A, on top of a shared remainder."""
    a_text = _deck("A", _SHARED + list(removed))
    b_text = _deck("B", _SHARED + list(added))
    table = oracles or {}
    return a_text, b_text, table.get


def test_swap_is_staple_ward_when_the_adds_are_generic(tmp_path):
    a_text, b_text, lookup = _swap(
        ["Sol Ring", "Arcane Signet", "Command Tower"],
        oracles={"Sol Ring": {"type_line": "Artifact",
                              "oracle_text": "{T}: Add {C}{C}."}},
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "staple_ward"
    assert label["added"]["staple"] == 3
    assert label["staple_share"] == 1.0


def test_game_changers_count_as_staple_ward(tmp_path):
    """FP-016 §7's own example of the failure is 'it just recommends
    Rhystic Study to everyone'. Rhystic Study is on the Game Changers list
    and on no other list this repo ships, so a labeling built only on
    UNIVERSAL_STAPLES_LC would be blind to the exact case G3 exists for."""
    a_text, b_text, lookup = _swap(
        ["Rhystic Study", "Smothering Tithe"],
        oracles={
            "Rhystic Study": {
                "type_line": "Enchantment",
                "oracle_text": "Whenever an opponent casts a spell, you may "
                               "draw a card unless that player pays {1}.",
            },
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "staple_ward"


def test_swap_is_intent_ward_when_the_adds_match_the_themes():
    a_text, b_text, lookup = _swap(
        ["Squirrel Nest", "Chatter of the Squirrel"],
        oracles={
            "Squirrel Nest": {"type_line": "Enchantment",
                              "oracle_text": _TOKENS_ORACLE},
            "Chatter of the Squirrel": {"type_line": "Sorcery",
                                        "oracle_text": _TOKENS_ORACLE},
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(themes=["tokens"]),
        lookup=lookup,
    )
    assert label["direction"] == "intent_ward"
    assert label["added"]["intent"] == 2


def test_tribal_type_is_an_intent_signal():
    a_text, b_text, lookup = _swap(
        ["Squirrel Lord", "Acorn Catapult"],
        oracles={
            "Squirrel Lord": {"type_line": "Creature — Squirrel",
                              "oracle_text": "Other Squirrels get +1/+1."},
            "Acorn Catapult": {"type_line": "Artifact",
                               "oracle_text": "Create a Squirrel token."},
        },
    )
    label = classify_swap_direction(
        a_text, b_text,
        intent=_squirrel_intent(themes=[], tribal_type="Squirrel"),
        lookup=lookup,
    )
    assert label["direction"] == "intent_ward"


def test_a_card_that_is_both_is_evidence_for_neither():
    """Rhystic Study in a spellslinger deck is a generic staple AND an
    intent match. Crediting it to whichever test ran first would let the
    implementation order decide the label."""
    a_text, b_text, lookup = _swap(
        ["Rhystic Study", "Mystical Tutor"],
        oracles={
            "Rhystic Study": {"type_line": "Enchantment",
                              "oracle_text": _SPELLS_ORACLE},
            "Mystical Tutor": {"type_line": "Instant",
                               "oracle_text": _SPELLS_ORACLE},
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(themes=["spellslinger"]),
        lookup=lookup,
    )
    assert label["added"]["both"] == 2
    assert label["added"]["staple"] == 0
    assert label["added"]["intent"] == 0
    assert label["direction"] == "mixed"


def test_an_ordinary_swap_is_neither_not_intent_ward():
    """The common case, and the one that would poison G3 if it were
    quietly rounded into an arm."""
    a_text, b_text, lookup = _swap(
        ["Plain Bird", "Plain Bear"],
        oracles={
            "Plain Bird": {"type_line": "Creature — Bird",
                           "oracle_text": _PLAIN_ORACLE},
            "Plain Bear": {"type_line": "Creature — Bear",
                           "oracle_text": _PLAIN_ORACLE},
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "neither"
    assert label["added"]["neither"] == 2


def test_a_split_swap_is_mixed():
    a_text, b_text, lookup = _swap(
        ["Sol Ring", "Squirrel Nest"],
        oracles={
            "Squirrel Nest": {"type_line": "Enchantment",
                              "oracle_text": _TOKENS_ORACLE},
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "mixed"
    assert label["added"]["staple"] == 1 and label["added"]["intent"] == 1


def test_no_intent_means_no_label_not_staple_ward():
    """Without an intent the intent-fit test can never fire, so every swap
    would come back staple-ward or neither — a fabricated result pointing
    at exactly the bias G3 tests for."""
    a_text, b_text, lookup = _swap(["Sol Ring", "Arcane Signet"])
    label = classify_swap_direction(a_text, b_text, intent=None, lookup=lookup)
    assert label["direction"] == "unknown"
    assert "no intent" in label["reason"]
    assert label["staple_share"] is None


def test_a_single_card_swap_is_unknown_not_a_100_percent_share():
    a_text, b_text, lookup = _swap(["Sol Ring"])
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "unknown"
    assert str(SWAP_LABEL_MIN_CARDS) in label["reason"]


def test_unresolvable_cards_are_excluded_not_called_plain():
    """'We could not test it' must not read as 'we tested it and it was
    ordinary' — the second would be evidence, the first is not."""
    a_text, b_text, lookup = _swap(["Ghost Card One", "Ghost Card Two"])
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["added"]["unresolved"] == 2
    assert label["added"]["neither"] == 0
    assert label["direction"] == "unknown"


def test_a_named_staple_still_labels_without_oracle_text():
    """Staple membership is a NAME test; intent fit needs the text. So an
    unresolvable Sol Ring is still a staple, while an unresolvable unknown
    card is not testable at all."""
    a_text, b_text, lookup = _swap(["Sol Ring", "Arcane Signet"])
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["added"]["staple"] == 2
    assert label["direction"] == "staple_ward"


def test_the_removed_set_is_recorded_but_does_not_drive_the_label():
    """Recorded for the natural next refinement (a swap that CUTS the theme
    is staple-ward in effect); deliberately not folded into the word before
    anyone has looked at a pairing."""
    a_text, b_text, lookup = _swap(
        ["Plain Bird", "Plain Bear"],
        removed=["Sol Ring", "Arcane Signet"],
        oracles={
            "Plain Bird": {"type_line": "Creature — Bird",
                           "oracle_text": _PLAIN_ORACLE},
            "Plain Bear": {"type_line": "Creature — Bear",
                           "oracle_text": _PLAIN_ORACLE},
        },
    )
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["removed"]["staple"] == 2
    assert label["direction"] == "neither"


def test_dominance_threshold_is_pinned():
    """Pre-registered 2026-08-27, before any pairings existed. Retuning it
    once data lands is moving the goalposts."""
    assert SWAP_LABEL_DOMINANCE == 0.60
    assert SWAP_LABEL_MIN_CARDS == 2


def test_labeling_makes_no_network_call(monkeypatch):
    """It runs on the judge's cache-only-always path, inside the improve
    loop's round. The Game Changers list must come from the offline
    accessor, never from the live scrape."""
    def explode(*a, **kw):
        raise AssertionError("swap-direction labeling opened a socket")
    monkeypatch.setattr("urllib.request.urlopen", explode)

    a_text, b_text, lookup = _swap(["Sol Ring", "Arcane Signet"])
    label = classify_swap_direction(
        a_text, b_text, intent=_squirrel_intent(), lookup=lookup,
    )
    assert label["direction"] == "staple_ward"


# --- the label on the report ----------------------------------------------

def test_report_carries_the_swap_direction(pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    report = judge_pairing(a, b, intent=_squirrel_intent(),
                           judge_fn=fn, lookup=_no_lookup)
    assert report.swap_direction in {
        "staple_ward", "intent_ward", "mixed", "neither", "unknown",
    }
    assert report.swap_label["direction"] == report.swap_direction
    # And it survives the trip through the judge_report column.
    assert json.loads(json.dumps(report.to_dict()))["swap_direction"] == \
        report.swap_direction


def test_report_without_intent_is_unlabeled(pairing):
    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    report = judge_pairing(a, b, judge_fn=fn, lookup=_no_lookup)
    assert report.swap_direction == "unknown"


def test_the_judge_is_never_shown_the_label(pairing):
    """A panel told 'this swap is staple-ward' would be answering a leading
    question, and G3 would then measure the label rather than the bias."""
    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    judge_pairing(a, b, intent=_squirrel_intent(),
                  judge_fn=fn, lookup=_no_lookup)
    for system, user, _model in fn.calls:
        for banned in ("staple_ward", "intent_ward", "staple-ward",
                       "intent-ward", "swap_direction"):
            assert banned not in system
            assert banned not in user


def test_a_labeling_failure_does_not_sink_a_panel_that_ran(pairing, monkeypatch):
    """Six judgments have already been spent by the time labeling runs. A
    bad lookup is not a reason to lose them — the pairing just falls out of
    G3's population."""
    def boom(*a, **kw):
        raise RuntimeError("oracle store on fire")
    monkeypatch.setattr(
        "commander_builder.deck_judge.classify_swap_direction", boom,
    )
    a, b = pairing
    # Deck B in both orders (so: no order flip, a real ``kept``).
    fn = _scripted([_answer("B")] * 3 + [_answer("A")] * 3)
    report = judge_pairing(a, b, intent=_squirrel_intent(),
                           judge_fn=fn, lookup=_no_lookup)
    assert report.verdict == "kept"          # the panel still counted
    assert report.swap_direction == "unknown"
    assert any("G3 population" in n for n in report.notes)


def test_cli_prints_the_swap_direction_with_its_reason(monkeypatch, capsys,
                                                       pairing):
    """Never a bare word: the classifier is a heuristic over two name lists
    and a theme matcher, and a reader who cannot see why cannot audit it."""
    from commander_builder.deck_judge import render_report

    a, b = pairing
    fn = _scripted([_answer("B")] * 6)
    report = judge_pairing(a, b, intent=_squirrel_intent(),
                           judge_fn=fn, lookup=_no_lookup)
    text = render_report(report, a, b)
    assert "swap direction:" in text
    assert report.swap_direction in text
    assert report.swap_label["reason"] in text
