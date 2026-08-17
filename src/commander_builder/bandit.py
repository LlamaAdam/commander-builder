"""Multi-armed-bandit swap selection (FP-012, slice 2).

The next slice past ``commander-improve``'s fixed-N greedy loop (A2):
instead of blindly accepting whatever the curator proposes each round,
treat the candidate card *swaps* as bandit arms and learn — across A/B
sims — which swaps actually move the win rate. Each arm is a concrete
``(add, cut)`` swap; pulling it applies that swap to the current best
deck and sims it; the reward is the NORMALIZED seat-attributed decisive
margin ``(wins_b - wins_a) / decisive`` ∈ [-1, +1] (see
``improve._signed_margin_reward``). Normalizing at the integration
boundary is what makes the policies' O(1)-reward assumptions actually
hold: UCB1's ``c ≈ sqrt(2)`` bonus and Thompson's unit ``obs_var`` are
calibrated for O(1) rewards, and the pre-2026-08-16 raw win margins
(O(±20) at 45-game pulls) dwarfed the exploration term, collapsing both
policies to greedy-on-one-noisy-pull.

This module is the **pure core**: the arm model, two policies
(epsilon-greedy + UCB1), and a ``run_bandit`` loop driven by an injected
``evaluate`` callable. It has no Forge / Anthropic / disk dependency, so
the search logic is fully unit-testable; ``improve.py`` supplies the real
evaluator (apply swap → ``run_ab_simulation`` → normalized margin +
significance verdict) when wired to the ``commander-improve --strategy
bandit`` CLI.

Failed pulls are NOT observations: an ``evaluate`` that couldn't
measure anything (apply error, missing fillers, crashed sim, zero
decisive games) returns a skip (``PullOutcome.skip(reason)`` or bare
``None``) and ``run_bandit`` records it via ``record_skip`` — the arm's
``pulls`` / ``total_reward`` stay untouched, so a crashed sim can never
enter the statistics as a measured tie.

State compatibility: nothing in this module is persisted or reloaded —
``BanditResult.to_dict`` feeds one-shot CLI JSON/display only and
``improve``'s ``state`` dict holds just the current deck path — so the
2026-08-16 reward-scale change (raw margin → normalized) required no
stored-state migration or versioning.

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
    ``skips`` counts pulls that produced NO usable measurement (apply /
    sim failure); they deliberately do not touch ``pulls`` or
    ``total_reward`` so failures can never masquerade as measured ties.
    """

    key: str
    add: Optional[str] = None
    cut: Optional[str] = None
    pulls: int = 0
    total_reward: float = 0.0
    skips: int = 0
    skip_reason: Optional[str] = None  # most recent skip's reason

    @property
    def mean(self) -> float:
        return self.total_reward / self.pulls if self.pulls else 0.0


def update_arm(arm: Arm, reward: float) -> None:
    """Fold a reward into an arm's running stats (policy-independent)."""
    arm.pulls += 1
    arm.total_reward += reward


def record_skip(arm: Arm, reason: Optional[str] = None) -> None:
    """Record a failed pull WITHOUT polluting the arm's reward stats.

    The explicit skip API for evaluators whose pull produced no usable
    signal (apply exception, missing fillers, sim didn't complete, zero
    decisive games). ``pulls`` / ``total_reward`` — and therefore
    ``mean`` — are untouched; only the ``skips`` counter and the latest
    ``skip_reason`` move, so the failure is visible in the arm stats
    while the policy's estimate of the arm stays evidence-only.
    """
    arm.skips += 1
    arm.skip_reason = reason or "no_signal"


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
    n_arm)`` so under-sampled arms keep an exploration bonus.

    The regret analysis behind the default ``c = 1.4 ≈ sqrt(2)``
    assumes O(1)-bounded rewards; callers must normalize (see the
    module docstring) or the exploration term is dwarfed by the means
    and selection degenerates to greedy-on-one-noisy-pull.
    """

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
class PullOutcome:
    """What one ``evaluate(arm)`` call actually observed.

    The rich return type for evaluators (``run_bandit`` also still
    accepts a bare float, coerced via ``accept_threshold``, and bare
    ``None`` as an unreasoned skip — back-compat for scripted tests):

    ``reward``
        The normalized observation to fold into the arm's stats
        (``None`` only when ``skipped``).
    ``accepted``
        Whether the pull advanced the base deck. Decoupled from any
        reward threshold on purpose: the integration layer decides
        acceptance via ``_proposer_sim._verdict_from_ab`` (exact
        binomial significance + decisive-games gate + --sim-margin
        pre-filter), not via a raw margin comparison.
    ``skipped`` / ``skip_reason``
        The pull produced no usable measurement; ``run_bandit`` records
        it with ``record_skip`` and does NOT update the arm.
    ``verdict``
        Optional verdict label ('kept'/'neutral'/...) for the history
        row, so JSON output can explain each accept/reject.
    """

    reward: Optional[float] = None
    accepted: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None
    verdict: Optional[str] = None

    @classmethod
    def skip(cls, reason: str) -> "PullOutcome":
        return cls(reward=None, accepted=False, skipped=True,
                   skip_reason=reason)


def _coerce_outcome(raw, accept_threshold: float) -> PullOutcome:
    """Normalize an evaluator's return value into a ``PullOutcome``.

    Back-compat: a bare float keeps the historical threshold-accept
    semantics; ``None`` is a skip with no stated reason.
    """
    if isinstance(raw, PullOutcome):
        if not raw.skipped and raw.reward is None:
            raise ValueError(
                "PullOutcome with reward=None must be marked skipped "
                "(use PullOutcome.skip(reason))")
        return raw
    if raw is None:
        return PullOutcome.skip("no_signal")
    reward = float(raw)
    return PullOutcome(reward=reward, accepted=reward >= accept_threshold)


@dataclass
class BanditRound:
    """Record of one pull."""

    round: int
    arm_key: str
    reward: Optional[float]  # None = skipped pull (no measurement)
    accepted: bool  # the pull advanced the base deck (verdict 'kept')
    skipped: bool = False
    skip_reason: Optional[str] = None
    verdict: Optional[str] = None


@dataclass
class BanditResult:
    rounds_run: int
    accepted: int
    best_arm_key: Optional[str]
    best_arm_mean: float
    total_reward: float
    skipped: int = 0  # pulls that produced no measurement (see record_skip)
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
        default is appropriate for the normalized signed-margin
        rewards in [-1, 1] that ``improve._signed_margin_reward``
        supplies (raw win margins O(±20) would need obs_var on the
        order of margin² — exactly the mis-scaling the boundary
        normalization removes).

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
    evaluate: Callable[[Arm], "PullOutcome | float | None"],
    policy: BanditPolicy,
    *,
    accept_threshold: float = 1.0,
    rng: Optional[random.Random] = None,
) -> BanditResult:
    """Run up to ``rounds`` bandit pulls over ``arms``.

    Each round: ``policy.select`` chooses an arm and ``evaluate(arm)``
    reports what it observed — ideally a ``PullOutcome`` (normalized
    reward + verdict-based ``accepted`` + skip signaling); a bare float
    (accepted iff ``reward >= accept_threshold``) or ``None`` (skip)
    are accepted for back-compat. The integration layer's ``evaluate``
    owns all side effects (applying the swap, advancing the base deck
    on a significance-passing 'kept' verdict, logging). The core stays
    pure so it's testable with scripted rewards.

    Skips are not observations: a skipped pull calls ``record_skip``
    (the arm's ``pulls``/``mean`` are untouched) and RETIRES the arm
    from further selection — the same conservative kill-on-no-signal
    choice ``improve_search.SearchArm`` makes, since these failures
    (illegal apply, broken sim) are typically structural and re-pulling
    would burn budget re-measuring garbage. When every arm is retired
    the loop stops early (``rounds_run`` < ``rounds``).

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
    skipped = 0
    total_reward = 0.0

    for r in range(1, rounds + 1):
        eligible = [a for a in arms if a.skips == 0]
        if not eligible:
            break  # every arm retired on failures — nothing left to measure
        arm = policy.select(eligible, rng)
        outcome = _coerce_outcome(evaluate(arm), accept_threshold)
        if outcome.skipped:
            record_skip(arm, outcome.skip_reason)
            skipped += 1
            history.append(BanditRound(
                round=r, arm_key=arm.key, reward=None, accepted=False,
                skipped=True, skip_reason=arm.skip_reason,
            ))
            continue
        update_arm(arm, outcome.reward)
        total_reward += outcome.reward
        if outcome.accepted:
            accepted += 1
        history.append(BanditRound(
            round=r, arm_key=arm.key, reward=outcome.reward,
            accepted=outcome.accepted, verdict=outcome.verdict,
        ))

    pulled = [a for a in arms if a.pulls > 0]
    best = max(pulled, key=lambda a: a.mean) if pulled else None
    arm_stats = sorted(
        ({"key": a.key, "add": a.add, "cut": a.cut,
          "pulls": a.pulls, "mean": round(a.mean, 4),
          "skips": a.skips, "skip_reason": a.skip_reason} for a in arms),
        key=lambda d: d["mean"], reverse=True,
    )
    return BanditResult(
        rounds_run=len(history),
        accepted=accepted,
        best_arm_key=best.key if best else None,
        best_arm_mean=round(best.mean, 4) if best else 0.0,
        total_reward=round(total_reward, 4),
        skipped=skipped,
        arm_stats=arm_stats,
        history=history,
    )
