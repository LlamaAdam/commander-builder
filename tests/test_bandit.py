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
    UCB1,
    make_policy,
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
