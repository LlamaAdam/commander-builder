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


class ThompsonSampling(BanditPolicy):
    """Thompson sampling via a Gaussian (normal-normal) posterior per arm.

    Each arm maintains a running Bayesian estimate of its true mean
    reward using a conjugate Gaussian model with a known (estimated)
    variance.  At each pull the policy samples a value from each arm's
    posterior, then picks the arm with the highest sampled value.

    Model details (pure stdlib, no numpy/scipy)
    -------------------------------------------
    Prior: N(0, prior_var).  After ``n`` observations with mean
    ``x_bar`` the posterior is N(mu_n, sigma_n^2) where::

        precision_prior = 1.0 / prior_var
        precision_obs   = n / obs_var     (obs_var defaults to 1.0)
        sigma_n^2       = 1 / (precision_prior + precision_obs)
        mu_n            = sigma_n^2 * (precision_obs * x_bar)

    Sampling from N(mu, sigma^2) without numpy: the Box-Muller
    transform on two uniform samples gives a standard normal; we
    scale and shift to the posterior.

    Hyperparameters
    ---------------
    prior_var:
        Variance of the prior (default 1.0).  Larger values mean a
        wider prior, giving early pulls more uncertainty and hence
        more exploration.
    obs_var:
        Assumed observation noise variance (default 1.0).  Should
        be set to the rough squared scale of the reward signal; the
        default is appropriate for win-margin rewards O(1-5).

    Cold-start: untried arms have no posterior mean -- we sample
    from the prior directly so they're explored with the same
    probability as any other uncertain arm (unlike epsilon-greedy /
    UCB1 which force exhaustive cold-start via the ``untried`` list).
    """

    name = "thompson"

    def __init__(self, prior_var: float = 1.0, obs_var: float = 1.0):
        if prior_var <= 0:
            raise ValueError(f"prior_var must be > 0, got {prior_var}")
        if obs_var <= 0:
            raise ValueError(f"obs_var must be > 0, got {obs_var}")
        self.prior_var = prior_var
        self.obs_var = obs_var

    @staticmethod
    def _sample_normal(mu: float, sigma: float, rng: random.Random) -> float:
        """Sample one value from N(mu, sigma^2) via Box-Muller (stdlib only)."""
        import math
        # Box-Muller: two uniform samples → one standard normal.
        # u1 must not be exactly 0 to avoid log(0).
        while True:
            u1 = rng.random()
            if u1 > 0.0:
                break
        u2 = rng.random()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        return mu + sigma * z

    def _posterior_sample(self, arm: Arm, rng: random.Random) -> float:
        """Draw one sample from ``arm``'s posterior."""
        n = arm.pulls
        if n == 0:
            # No data: sample from prior N(0, prior_var).
            return self._sample_normal(0.0, self.prior_var ** 0.5, rng)
        x_bar = arm.mean  # running mean reward
        # Conjugate Gaussian update.
        precision_prior = 1.0 / self.prior_var
        precision_obs = n / self.obs_var
        sigma_n2 = 1.0 / (precision_prior + precision_obs)
        mu_n = sigma_n2 * (precision_obs * x_bar)
        return self._sample_normal(mu_n, sigma_n2 ** 0.5, rng)

    def select(self, arms: list[Arm], rng: random.Random) -> Arm:
        if not arms:
            raise ValueError("no arms to select from")
        # Sample each arm's posterior and pick the highest.
        samples = [(self._posterior_sample(a, rng), i) for i, a in enumerate(arms)]
        _, best_idx = max(samples)
        return arms[best_idx]


def make_policy(name: str, *, epsilon: float = 0.2, c: float = 1.4,
                prior_var: float = 1.0, obs_var: float = 1.0) -> BanditPolicy:
    """Factory: ``"epsilon_greedy"``, ``"ucb1"``, or ``"thompson"``."""
    if name == "epsilon_greedy":
        return EpsilonGreedy(epsilon=epsilon)
    if name == "ucb1":
        return UCB1(c=c)
    if name == "thompson":
        return ThompsonSampling(prior_var=prior_var, obs_var=obs_var)
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
