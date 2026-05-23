"""Multi-armed-bandit swap selection (FP-012, slice 2).

The next slice past ``commander-improve``'s fixed-N greedy loop (A2):
instead of blindly accepting whatever the curator proposes each round,
treat the candidate card *swaps* as bandit arms and learn — across A/B
sims — which swaps actually move the win rate. Each arm is a concrete
``(add, cut)`` swap; pulling it applies that swap to the current best
deck and sims it; the reward is the seat-attributed win margin.

This module is the **pure core**: the arm model, two policies
(epsilon-greedy + UCB1), and a ``run_bandit`` loop driven by an injected
``evaluate`` callable. It has no Forge / Anthropic / disk dependency, so
the search logic is fully unit-testable; ``improve.py`` supplies the real
evaluator (apply swap → ``run_ab_simulation`` → margin) when wired to the
``commander-improve --strategy bandit`` CLI.

Why a bandit and not just greedy: greedy commits to the first swap that
sims better and never revisits alternatives. A bandit balances
*exploration* (try under-sampled swaps) against *exploitation* (re-pull
swaps that have paid off), so noisy single-sim rewards don't lock the
search onto a lucky-but-mediocre swap. UCB1 is parameter-light (one
exploration constant); epsilon-greedy is the simple baseline.
"""
from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class Arm:
    """One candidate swap the bandit can pull.

    ``add`` / ``cut`` are card names (either may be ``None`` for an
    add-only or cut-only arm). ``pulls`` and ``total_reward`` accumulate
    as the arm is sampled; ``mean`` is the running average reward.
    """

    key: str
    add: Optional[str] = None
    cut: Optional[str] = None
    pulls: int = 0
    total_reward: float = 0.0

    @property
    def mean(self) -> float:
        return self.total_reward / self.pulls if self.pulls else 0.0


def update_arm(arm: Arm, reward: float) -> None:
    """Fold a reward into an arm's running stats (policy-independent)."""
    arm.pulls += 1
    arm.total_reward += reward


class BanditPolicy(ABC):
    """Selects which arm to pull next from the current arm stats."""

    name: str = "bandit"

    @abstractmethod
    def select(self, arms: list[Arm], rng: random.Random) -> Arm:
        ...


class EpsilonGreedy(BanditPolicy):
    """Pull each arm once, then with probability ``epsilon`` explore a
    random arm and otherwise exploit the current best-mean arm."""

    name = "epsilon_greedy"

    def __init__(self, epsilon: float = 0.2):
        if not (0.0 <= epsilon <= 1.0):
            raise ValueError(f"epsilon must be in [0,1], got {epsilon}")
        self.epsilon = epsilon

    def select(self, arms: list[Arm], rng: random.Random) -> Arm:
        if not arms:
            raise ValueError("no arms to select from")
        # Cold-start: sample every arm once before exploiting.
        untried = [a for a in arms if a.pulls == 0]
        if untried:
            return untried[0]
        if rng.random() < self.epsilon:
            return rng.choice(arms)
        return max(arms, key=lambda a: a.mean)


class UCB1(BanditPolicy):
    """UCB1: pull each arm once, then maximize ``mean + c·sqrt(ln N /
    n_arm)`` so under-sampled arms keep an exploration bonus."""

    name = "ucb1"

    def __init__(self, c: float = 1.4):
        if c < 0:
            raise ValueError(f"c must be >= 0, got {c}")
        self.c = c

    def select(self, arms: list[Arm], rng: random.Random) -> Arm:
        if not arms:
            raise ValueError("no arms to select from")
        untried = [a for a in arms if a.pulls == 0]
        if untried:
            return untried[0]
        total = sum(a.pulls for a in arms)
        ln_total = math.log(total)

        def ucb(a: Arm) -> float:
            return a.mean + self.c * math.sqrt(ln_total / a.pulls)

        return max(arms, key=ucb)


@dataclass
class BanditRound:
    """Record of one pull."""

    round: int
    arm_key: str
    reward: float
    accepted: bool  # reward cleared the accept threshold (kept as new base)


@dataclass
class BanditResult:
    rounds_run: int
    accepted: int
    best_arm_key: Optional[str]
    best_arm_mean: float
    total_reward: float
    arm_stats: list[dict] = field(default_factory=list)
    history: list[BanditRound] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def make_policy(name: str, *, epsilon: float = 0.2, c: float = 1.4) -> BanditPolicy:
    """Factory: ``"epsilon_greedy"`` or ``"ucb1"``."""
    if name == "epsilon_greedy":
        return EpsilonGreedy(epsilon=epsilon)
    if name == "ucb1":
        return UCB1(c=c)
    raise ValueError(f"unknown bandit policy: {name!r}")


def run_bandit(
    arms: list[Arm],
    rounds: int,
    evaluate: Callable[[Arm], float],
    policy: BanditPolicy,
    *,
    accept_threshold: float = 1.0,
    rng: Optional[random.Random] = None,
) -> BanditResult:
    """Run ``rounds`` bandit pulls over ``arms``.

    Each round: ``policy.select`` chooses an arm, ``evaluate(arm)`` returns
    its reward (e.g. an A/B sim win margin), the arm's stats update, and
    the round counts as "accepted" when the reward clears
    ``accept_threshold``. The integration layer's ``evaluate`` is
    responsible for any side effects (applying the swap, advancing the
    base deck on accept, logging). The core stays pure so it's testable
    with scripted rewards.

    ``rng`` is injectable for deterministic tests.
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")
    if not arms:
        raise ValueError("no arms to run the bandit over")
    if rng is None:
        rng = random.Random()

    history: list[BanditRound] = []
    accepted = 0
    total_reward = 0.0

    for r in range(1, rounds + 1):
        arm = policy.select(arms, rng)
        reward = evaluate(arm)
        update_arm(arm, reward)
        total_reward += reward
        was_accepted = reward >= accept_threshold
        if was_accepted:
            accepted += 1
        history.append(BanditRound(
            round=r, arm_key=arm.key, reward=reward, accepted=was_accepted,
        ))

    pulled = [a for a in arms if a.pulls > 0]
    best = max(pulled, key=lambda a: a.mean) if pulled else None
    arm_stats = sorted(
        ({"key": a.key, "add": a.add, "cut": a.cut,
          "pulls": a.pulls, "mean": round(a.mean, 4)} for a in arms),
        key=lambda d: d["mean"], reverse=True,
    )
    return BanditResult(
        rounds_run=len(history),
        accepted=accepted,
        best_arm_key=best.key if best else None,
        best_arm_mean=round(best.mean, 4) if best else 0.0,
        total_reward=round(total_reward, 4),
        arm_stats=arm_stats,
        history=history,
    )
