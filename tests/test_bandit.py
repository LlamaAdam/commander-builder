"""Tests for the FP-012 slice-2 bandit swap-selection core.

Pure logic — policies + the run_bandit loop — driven by scripted reward
functions, so no Forge / Anthropic / disk is touched.
"""
from __future__ import annotations

import random

import pytest

from commander_builder.bandit import (
    Arm,
    BanditResult,
    EpsilonGreedy,
    PullOutcome,
    SKIP_REASON_CLASSES,
    SKIP_STRUCTURAL,
    SKIP_TRANSIENT,
    ThompsonSampling,
    UCB1,
    classify_skip_reason,
    make_policy,
    record_skip,
    run_bandit,
    update_arm,
)


# --- Arm + update ---------------------------------------------------------

def test_arm_mean_zero_when_unpulled():
    assert Arm(key="a").mean == 0.0


def test_update_arm_accumulates():
    a = Arm(key="a")
    update_arm(a, 2.0)
    update_arm(a, 4.0)
    assert a.pulls == 2
    assert a.total_reward == 6.0
    assert a.mean == 3.0


# --- EpsilonGreedy --------------------------------------------------------

def test_epsilon_greedy_samples_untried_first():
    arms = [Arm("a"), Arm("b"), Arm("c")]
    pol = EpsilonGreedy(epsilon=0.0)
    rng = random.Random(1)
    # First selects return untried arms (pulls==0), in order.
    first = pol.select(arms, rng)
    assert first.pulls == 0
    update_arm(first, 1.0)
    second = pol.select(arms, rng)
    assert second is not first and second.pulls == 0


def test_epsilon_greedy_exploits_best_when_epsilon_zero():
    arms = [Arm("a", pulls=1, total_reward=0.0),
            Arm("b", pulls=1, total_reward=5.0)]
    pol = EpsilonGreedy(epsilon=0.0)
    # All tried, epsilon 0 → always the higher-mean arm.
    for _ in range(5):
        assert pol.select(arms, random.Random(_)).key == "b"


def test_epsilon_greedy_explores_when_epsilon_one():
    arms = [Arm("a", pulls=1, total_reward=0.0),
            Arm("b", pulls=1, total_reward=5.0)]
    pol = EpsilonGreedy(epsilon=1.0)
    # epsilon 1 → always a random pick; over many draws we hit "a" too.
    picks = {pol.select(arms, random.Random(s)).key for s in range(20)}
    assert "a" in picks


def test_epsilon_validation():
    with pytest.raises(ValueError):
        EpsilonGreedy(epsilon=1.5)


# --- UCB1 -----------------------------------------------------------------

def test_ucb1_samples_untried_first():
    arms = [Arm("a"), Arm("b")]
    pol = UCB1()
    assert pol.select(arms, random.Random(0)).pulls == 0


def test_ucb1_prefers_under_sampled_arm_with_close_means():
    # Arm a: 10 pulls mean 1.0; arm b: 1 pull mean 1.0. Equal means, but
    # b is under-sampled → UCB bonus favors exploring b.
    a = Arm("a", pulls=10, total_reward=10.0)
    b = Arm("b", pulls=1, total_reward=1.0)
    pol = UCB1(c=1.4)
    assert pol.select([a, b], random.Random(0)).key == "b"


def test_ucb1_exploits_clear_winner():
    # Arm a hugely better mean, both reasonably sampled → pick a.
    a = Arm("a", pulls=5, total_reward=25.0)   # mean 5
    b = Arm("b", pulls=5, total_reward=0.0)    # mean 0
    pol = UCB1(c=1.4)
    assert pol.select([a, b], random.Random(0)).key == "a"


def test_ucb1_validation():
    with pytest.raises(ValueError):
        UCB1(c=-1)


# --- make_policy ----------------------------------------------------------

def test_make_policy():
    assert isinstance(make_policy("epsilon_greedy"), EpsilonGreedy)
    assert isinstance(make_policy("ucb1"), UCB1)
    with pytest.raises(ValueError):
        make_policy("nope")


# --- run_bandit -----------------------------------------------------------

def test_run_bandit_converges_on_best_arm():
    """With a deterministic reward (arm 'good' always pays 3, others 0)
    UCB1 should identify 'good' as the best arm and pull it most."""
    arms = [Arm("good", add="A", cut="X"), Arm("bad1"), Arm("bad2")]
    rewards = {"good": 3.0, "bad1": 0.0, "bad2": 0.0}
    res = run_bandit(
        arms, rounds=30, evaluate=lambda arm: rewards[arm.key],
        policy=UCB1(c=0.5), accept_threshold=1.0, rng=random.Random(42),
    )
    assert res.best_arm_key == "good"
    assert res.rounds_run == 30
    # 'good' was exploited far more than either bad arm.
    good_pulls = next(a["pulls"] for a in res.arm_stats if a["key"] == "good")
    assert good_pulls >= 20
    # Accepted rounds = those where reward >= 1.0 (every 'good' pull).
    assert res.accepted == good_pulls


def test_run_bandit_history_and_totals():
    arms = [Arm("a"), Arm("b")]
    seq = iter([2.0, 0.0, 2.0, 0.0])
    res = run_bandit(
        arms, rounds=4, evaluate=lambda arm: next(seq),
        policy=EpsilonGreedy(epsilon=0.0), accept_threshold=1.0,
        rng=random.Random(0),
    )
    assert len(res.history) == 4
    assert res.total_reward == 4.0
    assert res.accepted == 2  # two rewards >= 1.0
    assert all(isinstance(h.reward, float) for h in res.history)


def test_run_bandit_result_json_serializable():
    import json
    arms = [Arm("a", add="A", cut="X")]
    res = run_bandit(arms, rounds=2, evaluate=lambda arm: 1.0,
                     policy=UCB1(), rng=random.Random(0))
    blob = json.loads(json.dumps(res.to_dict()))
    assert blob["best_arm_key"] == "a"
    assert blob["history"][0]["arm_key"] == "a"


def test_run_bandit_validates_inputs():
    with pytest.raises(ValueError):
        run_bandit([Arm("a")], rounds=0, evaluate=lambda a: 0.0, policy=UCB1())
    with pytest.raises(ValueError):
        run_bandit([], rounds=3, evaluate=lambda a: 0.0, policy=UCB1())


# --- ThompsonSampling (FP-012 Slice B1) ----------------------------------

def test_thompson_validation():
    with pytest.raises(ValueError):
        ThompsonSampling(prior_var=0.0)
    with pytest.raises(ValueError):
        ThompsonSampling(obs_var=-1.0)


def test_thompson_selects_from_arms():
    """ThompsonSampling must return one of the supplied arms."""
    arms = [Arm("a"), Arm("b"), Arm("c")]
    pol = ThompsonSampling()
    rng = random.Random(42)
    selected = pol.select(arms, rng)
    assert selected in arms


def test_thompson_no_arms_raises():
    pol = ThompsonSampling()
    with pytest.raises(ValueError):
        pol.select([], random.Random(0))


def test_thompson_cold_start_explores_all(monkeypatch):
    """With no observations every arm's posterior is just the prior;
    over 100 draws each arm should be selected at least once."""
    arms = [Arm(str(i)) for i in range(5)]
    pol = ThompsonSampling(prior_var=1.0, obs_var=1.0)
    rng = random.Random(7)
    selected_keys = {pol.select(arms, rng).key for _ in range(100)}
    assert selected_keys == {a.key for a in arms}


def test_thompson_prefers_high_reward_arm():
    """After many observations the posterior for the high-reward arm
    should dominate; Thompson should select it most of the time."""
    arms = [
        Arm("good", pulls=50, total_reward=150.0),   # mean = 3.0
        Arm("bad",  pulls=50, total_reward=-50.0),   # mean = -1.0
    ]
    pol = ThompsonSampling(prior_var=1.0, obs_var=1.0)
    rng = random.Random(99)
    picks = [pol.select(arms, rng).key for _ in range(200)]
    good_count = picks.count("good")
    # Good arm should dominate — expect at least 75% of selections.
    assert good_count > 150, f"good arm selected only {good_count}/200 times"


def test_thompson_run_bandit_identifies_best_arm():
    """run_bandit with Thompson policy should identify the best-reward arm."""
    arms = [Arm("best"), Arm("mediocre"), Arm("worst")]
    rewards = {"best": 4.0, "mediocre": 1.0, "worst": -1.0}
    result = run_bandit(
        arms, rounds=60,
        evaluate=lambda arm: rewards[arm.key],
        policy=ThompsonSampling(prior_var=2.0, obs_var=1.0),
        accept_threshold=2.0,
        rng=random.Random(12345),
    )
    assert result.best_arm_key == "best"
    assert result.rounds_run == 60
    best_pulls = next(a["pulls"] for a in result.arm_stats if a["key"] == "best")
    # Thompson should have pulled the best arm significantly more than others.
    assert best_pulls > 30


def test_make_policy_thompson():
    pol = make_policy("thompson")
    assert isinstance(pol, ThompsonSampling)


def test_make_policy_thompson_with_hyperparams():
    pol = make_policy("thompson", prior_var=2.0, obs_var=0.5)
    assert isinstance(pol, ThompsonSampling)
    assert pol.prior_var == 2.0
    assert pol.obs_var == 0.5


def test_make_policy_unknown_still_raises():
    with pytest.raises(ValueError):
        make_policy("gp_bo")


# --- skip API + PullOutcome (P03: failures are not observations) ----------

def test_record_skip_leaves_reward_stats_unchanged():
    a = Arm("a", pulls=2, total_reward=1.0)
    record_skip(a, "apply_failed: boom")
    assert a.pulls == 2
    assert a.total_reward == 1.0
    assert a.mean == 0.5
    assert a.skips == 1
    assert a.skip_reason == "apply_failed: boom"


def test_run_bandit_skipped_pull_does_not_update_arm_and_retires_it():
    """Regression (P03): a crashed/apply-failed pull must leave the
    arm's pull count and statistics unchanged — the old code folded
    failures in as 0.0-reward 'measured ties'. A STRUCTURAL skip also
    retires the arm so the budget flows to measurable arms (R2-D6)."""
    outcomes = {
        "fails": PullOutcome.skip("swap_dropped_by_legality"),
        "works": PullOutcome(reward=0.5, accepted=False, verdict="neutral"),
    }
    arms = [Arm("fails"), Arm("works")]
    res = run_bandit(
        arms, rounds=4, evaluate=lambda arm: outcomes[arm.key],
        policy=UCB1(), rng=random.Random(0),
    )
    failing = next(a for a in arms if a.key == "fails")
    working = next(a for a in arms if a.key == "works")
    # The failed pull entered NO statistics...
    assert failing.pulls == 0
    assert failing.total_reward == 0.0
    assert failing.mean == 0.0
    # ...but is recorded as a skip with its reason.
    assert failing.skips == 1
    assert failing.skip_reason == "swap_dropped_by_legality"
    assert failing.retired is True
    assert res.skipped == 1
    skip_rows = [h for h in res.history if h.skipped]
    assert len(skip_rows) == 1
    assert skip_rows[0].reward is None and skip_rows[0].accepted is False
    # The remaining budget went to the arm that produces signal.
    assert working.pulls == 3
    assert res.total_reward == 1.5
    # A skipped pull never counts toward total_reward or best-arm mean.
    assert res.best_arm_key == "works"


def test_run_bandit_none_reward_is_a_skip_and_all_retired_stops_early():
    """A bare ``None`` is an unreasoned skip -> 'no_signal' -> TRANSIENT,
    so the arm stays selectable and the run spends its whole budget.
    Only STRUCTURAL retirement ends a run early (R2-D6)."""
    arms = [Arm("a")]
    res = run_bandit(arms, rounds=5, evaluate=lambda arm: None,
                     policy=UCB1(), rng=random.Random(0))
    assert arms[0].pulls == 0
    assert arms[0].skips == 5
    assert arms[0].retired is False
    assert res.skipped == 5
    assert res.rounds_run == 5
    assert res.best_arm_key is None

    structural = [Arm("a")]
    res2 = run_bandit(
        structural, rounds=5,
        evaluate=lambda arm: PullOutcome.skip("swap_dropped_by_legality"),
        policy=UCB1(), rng=random.Random(0),
    )
    assert structural[0].retired is True
    assert res2.rounds_run == 1  # the only arm retired -> early stop


# --- R2-D6: structural vs transient skip classification -------------------
#
# One skip of ANY kind used to retire an arm forever, on a stated premise
# that skip failures "are typically structural". That was false for most
# of the vocabulary: a crashed JVM and a zero-decisive sim are sampling
# luck, uncorrelated with swap quality, and killing an arm on one
# permanently removes it from a run whose whole purpose is repeated
# measurement.

@pytest.mark.parametrize("reason,expected", [
    # STRUCTURAL — the swap can never apply to this deck.
    ("swap_dropped_by_legality", SKIP_STRUCTURAL),
    # TRANSIENT — the swap is fine, the measurement attempt wasn't.
    ("apply_failed", SKIP_TRANSIENT),
    ("apply_failed: RuntimeError: boom", SKIP_TRANSIENT),
    ("fillers_unavailable", SKIP_TRANSIENT),
    ("fillers_unavailable: need 2 for a 4-player pod, found 1",
     SKIP_TRANSIENT),
    ("sim_failed", SKIP_TRANSIENT),
    ("sim_failed: jvm died", SKIP_TRANSIENT),
    ("sim_skipped", SKIP_TRANSIENT),
    ("sim_pending", SKIP_TRANSIENT),
    ("sim_running", SKIP_TRANSIENT),
    ("sim_loop_unattributed", SKIP_TRANSIENT),
    ("sim_unknown", SKIP_TRANSIENT),
    ("zero_decisive_games", SKIP_TRANSIENT),
    ("no_signal", SKIP_TRANSIENT),
])
def test_every_shipped_skip_reason_is_classified(reason, expected):
    """Each reason the codebase actually produces is pinned to its class
    explicitly — no default bucket decides any of them, and the free-text
    detail after the colon never changes the answer."""
    assert classify_skip_reason(reason) == expected


def test_the_classification_table_covers_the_whole_vocabulary():
    """Every entry is one of the two classes, and exactly one reason is
    structural."""
    assert set(SKIP_REASON_CLASSES.values()) == {
        SKIP_STRUCTURAL, SKIP_TRANSIENT}
    structural = {k for k, v in SKIP_REASON_CLASSES.items()
                  if v == SKIP_STRUCTURAL}
    assert structural == {"swap_dropped_by_legality"}


def test_every_ab_status_the_evaluator_can_format_is_in_the_table(capsys):
    """The evaluator builds its sim skip reason as ``f"sim_{status}"``
    from an ABResult status. Every non-'done' status forge_batch declares
    must therefore already be classified — otherwise a perfectly normal
    JVM outcome trips the unclassified-reason path in production."""
    from commander_builder import forge_batch

    statuses = {
        getattr(forge_batch, name) for name in dir(forge_batch)
        if name.startswith("_AB_STATUS_")
    } - {"done"}
    for status in statuses:
        assert f"sim_{status}" in SKIP_REASON_CLASSES, status
    # ...plus the fallback the evaluator uses for a missing status.
    assert "sim_unknown" in SKIP_REASON_CLASSES
    assert "unclassified" not in capsys.readouterr().err


def test_unknown_skip_reason_is_transient_and_warns_loudly(capsys):
    """A reason nobody classified must NOT silently fall into a default
    bucket. It resolves transient — mis-retiring an arm deletes it from
    the search invisibly, while a needless re-pull shows up in the skip
    count — and says so on stderr so the omission gets fixed."""
    assert classify_skip_reason("sim_timed_out_waiting") == SKIP_TRANSIENT
    err = capsys.readouterr().err
    assert "unclassified skip reason" in err
    assert "sim_timed_out_waiting" in err
    assert "NOT retired" in err


def test_unknown_skip_reason_does_not_retire_the_arm(capsys):
    arms = [Arm("a")]
    run_bandit(arms, rounds=2,
               evaluate=lambda arm: PullOutcome.skip("brand_new_reason"),
               policy=UCB1(), rng=random.Random(0))
    assert arms[0].retired is False
    assert arms[0].skips == 2
    assert "unclassified skip reason" in capsys.readouterr().err


def test_none_reason_classifies_as_transient_without_warning(capsys):
    """``record_skip(arm)`` with no reason stores 'no_signal', which IS
    in the table — it must not trip the unknown-reason warning."""
    a = Arm("a")
    record_skip(a)
    assert a.skip_reason == "no_signal"
    assert a.retired is False
    assert "unclassified" not in capsys.readouterr().err


def test_transient_skips_never_retire_however_many_accumulate():
    """'Never counts toward retirement' means never — there is no
    N-strikes rule hiding behind the classification."""
    a = Arm("a")
    for _ in range(50):
        record_skip(a, "sim_failed: jvm died")
    assert a.skips == 50
    assert a.retired is False
    assert a.retire_reason is None


def _broken_and_fine(policy, rounds):
    """One arm that always skips transiently, one that always measures."""
    def evaluate(arm):
        if arm.key == "broken":
            return PullOutcome.skip("sim_failed: jvm died")
        return PullOutcome(reward=0.5, accepted=False, verdict="neutral")

    arms = [Arm("broken"), Arm("fine")]
    res = run_bandit(arms, rounds=rounds, evaluate=evaluate, policy=policy,
                     rng=random.Random(0))
    return arms[0], arms[1], res


def test_a_transient_skipped_arm_does_not_starve_its_siblings():
    """Cold-start identifies an unpulled arm by ``pulls == 0``, which a
    skip leaves untouched. With transient skips no longer retiring, a
    naive ``untried[0]`` would hand every round to the same broken arm
    forever and no sibling would ever get its first pull."""
    broken, fine, res = _broken_and_fine(UCB1(), rounds=4)
    assert broken.pulls == 0 and broken.retired is False
    assert fine.pulls >= 1          # it got measured despite going second
    assert res.best_arm_key == "fine"


@pytest.mark.parametrize("policy", [UCB1(), EpsilonGreedy(epsilon=0.0)])
def test_a_transient_skipped_arm_cannot_monopolize_the_budget(policy):
    """The other half of the starvation problem: once every OTHER arm has
    been measured, the failing arm is the only one left with pulls == 0,
    so an unguarded cold-start would hand it every remaining round of an
    overnight budget.

    The fairness rule interleaves retries with evidence-gathering — an
    arm that has failed k times waits until every measured arm has k
    pulls — so a broken arm costs about one round in len(arms) instead of
    all of them, and is still never retired."""
    broken, fine, res = _broken_and_fine(policy, rounds=20)
    assert broken.retired is False              # never retired...
    assert broken.skips >= 2                    # ...and genuinely retried
    assert broken.skips <= 20 // 2 + 1          # ...but not monopolizing
    assert fine.pulls >= 20 // 2 - 1            # the real evidence got made
    assert res.rounds_run == 20


def test_selection_never_scores_an_arm_that_has_no_measurement():
    """UCB1 divides by ``pulls``; an arm that has only ever skipped still
    has pulls == 0. Before transient skips existed the exhaustive
    cold-start made that unreachable — now the scoring phase has to
    exclude it explicitly rather than raise ZeroDivisionError."""
    seq = {
        "a": PullOutcome.skip("sim_failed: jvm died"),
        "b": PullOutcome(reward=0.4, accepted=False, verdict="neutral"),
        "c": PullOutcome(reward=-0.9, accepted=False, verdict="reverted"),
    }
    arms = [Arm("a"), Arm("b"), Arm("c")]
    res = run_bandit(arms, rounds=25, evaluate=lambda arm: seq[arm.key],
                     policy=UCB1(), rng=random.Random(0))
    assert res.rounds_run == 25                  # no crash, full budget
    # ...and the never-measured arm never wins "best", despite its
    # placeholder mean of 0.0 outranking c's real -0.9.
    assert res.best_arm_key == "b"


def test_cold_start_keeps_list_order_when_nothing_skipped():
    """The skip-aware ordering must be a no-op in the normal case: with
    no skips anywhere, cold-start still walks the arms in list order."""
    arms = [Arm("first"), Arm("second"), Arm("third")]
    picked: list[str] = []

    def evaluate(arm):
        picked.append(arm.key)
        return 0.5

    run_bandit(arms, rounds=3, evaluate=evaluate, policy=UCB1(),
               rng=random.Random(0))
    assert picked == ["first", "second", "third"]


def test_arm_stats_distinguish_retired_from_merely_skipped():
    """An arm with skips but ``retired=False`` was hit by transient
    failures only and stayed in the search; the JSON has to say which."""
    import json
    arms = [Arm("transient"), Arm("structural")]
    outcomes = {
        "transient": PullOutcome.skip("sim_failed: jvm died"),
        "structural": PullOutcome.skip("swap_dropped_by_legality"),
    }
    res = run_bandit(arms, rounds=4, evaluate=lambda arm: outcomes[arm.key],
                     policy=UCB1(), rng=random.Random(0))
    stats = {s["key"]: s for s in json.loads(json.dumps(res.to_dict()))[
        "arm_stats"]}
    assert stats["transient"]["skips"] >= 1
    assert stats["transient"]["retired"] is False
    assert stats["transient"]["retire_reason"] is None
    assert stats["structural"]["retired"] is True
    assert stats["structural"]["retire_reason"] == "swap_dropped_by_legality"


def test_pull_outcome_accept_decouples_from_reward_threshold():
    """Acceptance rides the significance verdict the evaluator reports,
    not a raw reward-vs-threshold comparison: a small-but-significant
    reward can accept while a large-but-insignificant one must not."""
    seq = iter([
        PullOutcome(reward=0.1, accepted=True, verdict="kept"),
        PullOutcome(reward=0.9, accepted=False, verdict="inconclusive"),
    ])
    arms = [Arm("a")]
    res = run_bandit(arms, rounds=2, evaluate=lambda arm: next(seq),
                     policy=UCB1(), accept_threshold=1.0,
                     rng=random.Random(0))
    assert res.accepted == 1
    assert [h.accepted for h in res.history] == [True, False]
    assert [h.verdict for h in res.history] == ["kept", "inconclusive"]
    assert arms[0].pulls == 2  # both were real measurements


def test_bare_float_evaluator_keeps_threshold_semantics():
    """Back-compat: scripted float rewards still accept via
    accept_threshold, and never skip."""
    arms = [Arm("a")]
    res = run_bandit(arms, rounds=3, evaluate=lambda arm: 2.0,
                     policy=UCB1(), accept_threshold=1.0,
                     rng=random.Random(0))
    assert res.accepted == 3
    assert res.skipped == 0
    assert arms[0].pulls == 3


def test_unskipped_outcome_without_reward_raises():
    bad = PullOutcome(reward=None, accepted=True)  # contract violation
    with pytest.raises(ValueError):
        run_bandit([Arm("a")], rounds=1, evaluate=lambda arm: bad,
                   policy=UCB1(), rng=random.Random(0))


def test_skip_fields_json_serializable():
    import json
    arms = [Arm("a"), Arm("b")]
    seq = iter([PullOutcome.skip("sim_failed: jvm died"),
                PullOutcome(reward=0.25, accepted=False, verdict="neutral")])
    res = run_bandit(arms, rounds=2, evaluate=lambda arm: next(seq),
                     policy=UCB1(), rng=random.Random(0))
    blob = json.loads(json.dumps(res.to_dict()))
    assert blob["skipped"] == 1
    assert blob["history"][0]["skipped"] is True
    assert "sim_failed" in blob["history"][0]["skip_reason"]
    stats_by_key = {s["key"]: s for s in blob["arm_stats"]}
    assert stats_by_key["a"]["skips"] == 1
    assert stats_by_key["b"]["pulls"] == 1


def test_thompson_result_json_serializable():
    """A bandit run with Thompson should produce a JSON-safe result."""
    import json
    arms = [Arm("a", add="A", cut="X"), Arm("b", add="B", cut="Y")]
    result = run_bandit(
        arms, rounds=10,
        evaluate=lambda arm: 1.0 if arm.key == "a" else 0.0,
        policy=ThompsonSampling(),
        rng=random.Random(0),
    )
    blob = json.loads(json.dumps(result.to_dict()))
    assert blob["best_arm_key"] in ("a", "b")
