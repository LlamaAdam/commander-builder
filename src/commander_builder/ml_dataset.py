"""Phase 3 dataset shape — the bridge from knowledge_log rows to ML training.

Phase 2 records every iteration as a `knowledge_log.Iteration`. Phase 3 trains
a model that predicts swap outcomes (`kept` / `reverted` / `neutral`) from
deck features + swap features. This module defines:

  - the **feature schema** — columns the model will see
  - **feature extraction** — turn a knowledge_log row into a feature vector
  - the **train/eval split** convention

It is *not* a trainer. Sklearn / PyTorch live elsewhere and will import these
helpers. Building this scaffolding now keeps the knowledge_log columns stable
— Phase 2 schema changes that break feature extraction surface immediately.

Minimum viable dataset (= when training is worth attempting):
  - 200+ logged iterations
  - At least 3 verdicts each of "kept" and "reverted" (to learn separation)
  - 5+ unique decks (otherwise the model overfits to one deck's quirks)

We're nowhere near that yet. This module is forward scaffolding.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from .knowledge_log import Iteration


# Feature schema — the columns a Phase 3 model trains against.
FEATURE_NAMES: list[str] = [
    # Iteration metadata
    "bracket",
    # Sim signal
    "total_games",
    "draws",
    "decisive_games",
    "draw_rate",
    "old_wins",
    "new_wins",
    "margin",
    "win_rate_old",
    "win_rate_new",
    "win_rate_delta",
    # Per-version stats (averaged across the comparison)
    "old_avg_ending_life",
    "new_avg_ending_life",
    "old_avg_damage_taken",
    "new_avg_damage_taken",
    "old_avg_turns_when_won",
    "new_avg_turns_when_won",
    "old_avg_turns_when_lost",
    "new_avg_turns_when_lost",
    "old_eliminations",
    "new_eliminations",
    # Swap shape
    "cards_added",
    "cards_removed",
    "swap_size",                # added + removed
    # Audit context
    "audit_version_v3",         # 1 if this version, 0 otherwise (scales as we add v4, v5)
]


@dataclass
class FeatureRow:
    """One training example. The label and identifying metadata travel
    alongside the feature vector — the trainer slices them off."""
    iteration_id: int
    deck_id: str
    label: str                          # "kept" | "reverted" | "neutral"
    features: dict[str, float] = field(default_factory=dict)
    raw_lessons: list[str] = field(default_factory=list)

    def feature_vector(self, names: Optional[list[str]] = None) -> list[float]:
        """Render the feature dict as an ordered list. Columns missing from
        `self.features` get 0.0 — same convention as sklearn one-hot encoders
        on unseen categoricals."""
        names = names or FEATURE_NAMES
        return [float(self.features.get(name, 0.0)) for name in names]

    def to_dict(self) -> dict:
        return asdict(self)


def extract_features(it: Iteration) -> Optional[FeatureRow]:
    """Turn one Iteration into a FeatureRow.

    Returns None if the iteration is too incomplete to feature (no sim_report,
    or verdict is still 'pending'). The caller decides whether to skip or to
    re-queue these for completion."""
    if it.id is None or it.sim_report is None or it.verdict == "pending":
        return None

    sim = it.sim_report
    old = sim.get("old_stats", {})
    new = sim.get("new_stats", {})
    manifest = it.audit_manifest or {}

    # The A/B sim writer (forge_runner.ABResult.to_dict) uses wins_a/wins_b/
    # games. An earlier schema used old_stats/new_stats/total_games/draws.
    # Read the real schema first, fall back to the legacy keys, so feature
    # rows aren't silently zeroed (which they were before this fix).
    total = int(sim.get("total_games", sim.get("games", 0)) or 0)
    old_wins = int(sim.get("wins_a", old.get("wins", 0)) or 0)
    new_wins = int(sim.get("wins_b", new.get("wins", 0)) or 0)
    draws = int(sim.get("draws", max(0, total - old_wins - new_wins)) or 0)
    decisive = max(0, total - draws)
    draw_rate = draws / total if total else 0.0

    # Prefer the per-iteration authoritative columns (the analyst computed and
    # persisted these next to the verdict) when present; else derive.
    win_rate_old = (it.win_rate_old if it.win_rate_old is not None
                    else (old_wins / decisive if decisive else 0.0))
    win_rate_new = (it.win_rate_new if it.win_rate_new is not None
                    else (new_wins / decisive if decisive else 0.0))

    cards_added = len(manifest.get("added", []))
    cards_removed = len(manifest.get("removed", []))

    features: dict[str, float] = {
        "bracket": float(it.bracket),
        "total_games": float(total),
        "draws": float(draws),
        "decisive_games": float(decisive),
        "draw_rate": draw_rate,
        "old_wins": float(old_wins),
        "new_wins": float(new_wins),
        "margin": float(new_wins - old_wins),
        "win_rate_old": win_rate_old,
        "win_rate_new": win_rate_new,
        "win_rate_delta": win_rate_new - win_rate_old,
        "old_avg_ending_life": float(old.get("avg_ending_life", 0)),
        "new_avg_ending_life": float(new.get("avg_ending_life", 0)),
        "old_avg_damage_taken": float(old.get("avg_damage_taken", 0)),
        "new_avg_damage_taken": float(new.get("avg_damage_taken", 0)),
        "old_avg_turns_when_won": float(old.get("avg_turns_when_won", 0)),
        "new_avg_turns_when_won": float(new.get("avg_turns_when_won", 0)),
        "old_avg_turns_when_lost": float(old.get("avg_turns_when_lost", 0)),
        "new_avg_turns_when_lost": float(new.get("avg_turns_when_lost", 0)),
        "old_eliminations": float(old.get("eliminations", 0)),
        "new_eliminations": float(new.get("eliminations", 0)),
        "cards_added": float(cards_added),
        "cards_removed": float(cards_removed),
        "swap_size": float(cards_added + cards_removed),
        "audit_version_v3": 1.0 if it.audit_version == "v3" else 0.0,
    }
    return FeatureRow(
        iteration_id=it.id,
        deck_id=it.deck_id,
        label=it.verdict,
        features=features,
        raw_lessons=[],  # Populated when analyst.lessons hits the log; future field on Iteration.
    )


def build_dataset(
    iterations: list[Iteration],
    skip_neutral: bool = False,
) -> list[FeatureRow]:
    """Convert a list of iterations into a list of feature rows.

    `skip_neutral=True` drops iterations whose verdict is 'neutral' — useful
    when training a binary kept-vs-reverted classifier where the noise-band
    examples don't carry signal."""
    rows: list[FeatureRow] = []
    for it in iterations:
        row = extract_features(it)
        if row is None:
            continue
        if skip_neutral and row.label == "neutral":
            continue
        rows.append(row)
    return rows


def split_train_eval(
    rows: list[FeatureRow],
    eval_fraction: float = 0.2,
    seed: int = 0,
) -> tuple[list[FeatureRow], list[FeatureRow]]:
    """Group-aware split: all iterations of the same deck stay in the same
    split, so the model isn't evaluated on a deck it trained on (which would
    leak meta-information).

    `eval_fraction` is approximate — actual split depends on how iterations
    are distributed across deck_ids."""
    import random
    rng = random.Random(seed)
    by_deck: dict[str, list[FeatureRow]] = {}
    for r in rows:
        by_deck.setdefault(r.deck_id, []).append(r)
    deck_ids = sorted(by_deck.keys())
    rng.shuffle(deck_ids)
    n_eval = max(1, int(len(deck_ids) * eval_fraction))
    eval_decks = set(deck_ids[:n_eval])
    train, eval_ = [], []
    for deck_id, deck_rows in by_deck.items():
        (eval_ if deck_id in eval_decks else train).extend(deck_rows)
    return train, eval_


def dataset_summary(rows: list[FeatureRow]) -> dict:
    """One-glance statistics on a built dataset. Useful before training to
    catch obvious problems (class imbalance, no eval set, etc.)."""
    from collections import Counter
    label_counts = Counter(r.label for r in rows)
    deck_counts = Counter(r.deck_id for r in rows)
    return {
        "total_rows": len(rows),
        "label_distribution": dict(label_counts),
        "unique_decks": len(deck_counts),
        "rows_per_deck_min": min(deck_counts.values()) if deck_counts else 0,
        "rows_per_deck_max": max(deck_counts.values()) if deck_counts else 0,
        "feature_count": len(FEATURE_NAMES),
    }


if __name__ == "__main__":
    # Smoke entry: pull recent iterations from the default DB and report shape.
    from .knowledge_log import recent_iterations
    its = recent_iterations(limit=200)
    rows = build_dataset(its)
    print(json.dumps(dataset_summary(rows), indent=2))
