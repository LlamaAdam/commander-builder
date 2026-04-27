"""Tournament-style opponent pool curator.

Layer 1 of the testing strategy (see PROJECT.md "Testing strategy"). Takes a
candidate pool of ~12 decks at one bracket, runs a round-robin qualifier, and
emits the canonical top-6 (split into two 3-deck slices) as the cached opponent
pool that user-deck matchups compete against.

Public entry point:

    from commander_builder.pool_curator import curate_bracket
    pool = curate_bracket(bracket=3, candidate_filenames=[...], games_per_pod=3)
    # pool is a CuratedPool dataclass; persisted to _pools/B<n>.json

Curation cost is roughly:

    pods = ceil(len(candidates) * 3 / 4)         # each deck plays ~3 pods
    wall = pods * games_per_pod * ~80s/game       # B3; ~120s/game for B5

So 12 candidates × 3 pods × 3 games ≈ 9 pods × 3 = 27 games. ~35 min B3, ~55 min B5.

This module does NOT harvest candidates (that's `moxfield_import.harvest_bracket`)
and does NOT persist user-matchup results (that's `run_match.py`). It just turns
"a bag of decks at bracket N" into "a ranked, diversity-filtered, persistent pool".
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .forge_runner import ForgeRunner, VENDOR_FORGE
from .game_analyzer import MatchAnalysis, analyze
from .log_parser import ParsedSim, parse

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
POOL_DIR = DECK_DIR / "_pools"

# Curator rule thresholds (from PROJECT.md). Promoted to constants so a tuning
# pass surfaces them in one place rather than chasing magic numbers.
MAX_CONFIRM_ACTION_PER_GAME = 50.0      # AI-pilotability gate
MAX_UNSUPPORTED_CARDS_SMOKE = 0          # pre-flight rejects any unsupported card
INFLATED_WIN_RATE_THRESHOLD = 0.75       # tag, don't auto-drop
MAX_COLOR_OVERLAP_PER_SLICE = 2          # ≤2 shared identity colors between slice-mates
TOP_N = 6                                 # final pool size before slice split
DEFAULT_GAMES_PER_POD = 3
DEFAULT_PODS_PER_DECK = 3


# An archetype classifier is a function that returns one of:
#   "aggro" | "midrange" | "control" | "combo" | "stax"
# Default is the heuristic in `archetype.py` (real signal — was a stub before
# GAP-001 landed). Tests can pin a deterministic classifier by injection.
Archetype = str
ArchetypeClassifier = Callable[[Path], Archetype]


def _default_classifier(deck_path: Path) -> Archetype:
    """Heuristic classifier from `archetype.classify`. Wrapped here so the
    callable signature matches `ArchetypeClassifier` and so tests can swap in
    a stub without touching the archetype module."""
    from .archetype import classify
    return classify(deck_path)


# Kept for backward-compat with any tests that imported the old stub name.
_stub_classifier = _default_classifier


@dataclass
class CandidateScore:
    filename: str
    games_played: int = 0
    wins: int = 0
    confirm_action_total: int = 0
    unsupported_cards: int = 0
    archetype: Archetype = "midrange"
    color_identity: str = ""  # e.g. "WUB"; populated post-classification
    rejected_reason: Optional[str] = None

    @property
    def win_rate(self) -> float:
        return self.wins / self.games_played if self.games_played else 0.0

    @property
    def confirm_action_per_game(self) -> float:
        return self.confirm_action_total / self.games_played if self.games_played else 0.0

    @property
    def suspected_inflated(self) -> bool:
        return (
            self.games_played > 0
            and self.win_rate > INFLATED_WIN_RATE_THRESHOLD
        )


@dataclass
class CuratedPool:
    bracket: int
    created_at: str
    pool_a: list[str] = field(default_factory=list)   # filenames, ranks 1/3/5
    pool_b: list[str] = field(default_factory=list)   # filenames, ranks 2/4/6
    scores: list[CandidateScore] = field(default_factory=list)
    rejected: list[CandidateScore] = field(default_factory=list)

    def to_dict(self) -> dict:
        # asdict() drops @property fields, so the persisted pool would silently
        # lose win_rate / suspected_inflated — the very signals downstream
        # consumers need. Re-attach them per CandidateScore record.
        d = asdict(self)
        for s in d["scores"] + d["rejected"]:
            played = s["games_played"]
            wins = s["wins"]
            confirm = s["confirm_action_total"]
            wr = wins / played if played else 0.0
            s["win_rate"] = wr
            s["confirm_action_per_game"] = confirm / played if played else 0.0
            s["suspected_inflated"] = played > 0 and wr > INFLATED_WIN_RATE_THRESHOLD
        return d

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), indent=2, **kwargs)


def schedule_pods(
    candidates: list[str],
    pods_per_deck: int = DEFAULT_PODS_PER_DECK,
    seed: int = 0,
) -> list[list[str]]:
    """Build a round-robin pod schedule where each deck plays ~`pods_per_deck` pods.

    Forge's commander sim requires exactly 4 decks per pod, so we generate pods
    of size 4 and accept that the last pod may pull a deck twice if 4 doesn't
    divide evenly. The naive approach (random sample) over-concentrates some
    decks; this uses a deterministic shuffle then walks a cursor with stride 1
    to spread coverage. Good enough for 12-deck pools; replace with a proper
    BIBD if we ever scale past 20 candidates."""
    if len(candidates) < 4:
        raise ValueError(f"need at least 4 candidates, got {len(candidates)}")
    rng = random.Random(seed)
    target_pods = math.ceil(len(candidates) * pods_per_deck / 4)
    pods: list[list[str]] = []
    deck_pod_count: dict[str, int] = {c: 0 for c in candidates}
    for _ in range(target_pods):
        # Greedy: pick the 4 decks with the fewest pods so far, breaking ties
        # by deterministic shuffle.
        order = sorted(
            candidates,
            key=lambda c: (deck_pod_count[c], rng.random()),
        )
        pod = order[:4]
        for c in pod:
            deck_pod_count[c] += 1
        pods.append(pod)
    return pods


@dataclass
class PreflightResult:
    parsed: ParsedSim
    unsupported: int
    crashed: bool
    crash_reason: Optional[str]


def preflight_candidate(
    runner: ForgeRunner,
    filename: str,
    fillers: list[str],
) -> PreflightResult:
    """Run a 1-game smoke sim for one candidate. `fillers` are 3 known-good
    decks that pad the pod to 4. Reports both the parsed result and a crash
    signal — a timeout / non-zero returncode / empty parse means the candidate
    can't even survive a single game and shouldn't enter the qualifier."""
    decks = [filename, *fillers]
    result = runner.run(decks, num_games=1)
    parsed = parse(result.stdout)
    crashed = False
    crash_reason: Optional[str] = None
    if result.timed_out:
        crashed = True
        crash_reason = "timeout"
    elif result.returncode not in (0, None):
        crashed = True
        crash_reason = f"returncode={result.returncode}"
    elif parsed.games_completed == 0:
        # Forge exited cleanly but no game finished — usually a deck-load failure
        # that doesn't surface as a non-zero exit code.
        crashed = True
        crash_reason = "no_games_completed"
    return PreflightResult(
        parsed=parsed,
        unsupported=len(parsed.unsupported_cards),
        crashed=crashed,
        crash_reason=crash_reason,
    )


def _color_overlap(a: str, b: str) -> int:
    return len(set(a) & set(b))


def _slice_violates(slice_: list[CandidateScore]) -> bool:
    """A slice violates if any pair shares an archetype OR exceeds the
    color-overlap cap. Used by both the diversity check and the swap search."""
    for i, x in enumerate(slice_):
        for y in slice_[i + 1:]:
            if _color_overlap(x.color_identity, y.color_identity) > MAX_COLOR_OVERLAP_PER_SLICE:
                return True
            if x.archetype == y.archetype:
                return True
    return False


def _split_into_slices(top6: list[CandidateScore]) -> tuple[list[str], list[str]]:
    """Split the top 6 ranked candidates into ranks 1/3/5 (Pool A) and 2/4/6
    (Pool B) — the rotation specified in the curator rules.

    If the default split violates diversity, search a small set of low-rank
    swaps for a non-violating arrangement. Order tried (least-disruptive first):

      1. default (no swap)
      2. swap ranks 3↔4 — preserves rank-1/rank-2 leaders
      3. swap ranks 4↔5
      4. swap ranks 3↔5
      5. swap ranks 2↔3 — touches the first slice
      6. swap ranks 5↔6

    The first non-violating arrangement wins. If all six swaps still violate,
    return the default split and log a WARN — the curator caller can decide
    whether to revise the candidate pool or accept the imperfection. This
    closes GAP-006 (the prior one-shot 3↔4 swap could leave both slices
    violating with no further check)."""
    if len(top6) < 4:
        # Too short for the rotation to mean anything.
        a = [top6[i] for i in (0, 2, 4) if i < len(top6)]
        b = [top6[i] for i in (1, 3, 5) if i < len(top6)]
        return [s.filename for s in a], [s.filename for s in b]

    # Each tuple is (i, j) — indices to swap. () is the no-swap default.
    swap_candidates: list[tuple[int, ...]] = [
        (),
        (2, 3),
        (3, 4),
        (2, 4),
        (1, 2),
        (4, 5),
    ]

    for swap in swap_candidates:
        ranking = list(top6)  # local copy — never mutate caller's list
        if swap:
            i, j = swap
            if i < len(ranking) and j < len(ranking):
                ranking[i], ranking[j] = ranking[j], ranking[i]
        a = [ranking[i] for i in (0, 2, 4) if i < len(ranking)]
        b = [ranking[i] for i in (1, 3, 5) if i < len(ranking)]
        if not _slice_violates(a) and not _slice_violates(b):
            return [s.filename for s in a], [s.filename for s in b]

    # All search candidates violated. Log and ship the default.
    a = [top6[i] for i in (0, 2, 4) if i < len(top6)]
    b = [top6[i] for i in (1, 3, 5) if i < len(top6)]
    print(
        f"  WARN: _split_into_slices found no non-violating arrangement; "
        f"shipping default (slice A archetype overlap or color overlap exceeds threshold). "
        f"Pool A archetypes={[s.archetype for s in a]}, "
        f"Pool B archetypes={[s.archetype for s in b]}",
        flush=True,
    )
    return [s.filename for s in a], [s.filename for s in b]


def curate_bracket(
    bracket: int,
    candidate_filenames: list[str],
    games_per_pod: int = DEFAULT_GAMES_PER_POD,
    pods_per_deck: int = DEFAULT_PODS_PER_DECK,
    classifier: ArchetypeClassifier = _default_classifier,
    runner: Optional[ForgeRunner] = None,
    seed: int = 0,
    pool_dir: Path = POOL_DIR,
) -> CuratedPool:
    """Run the full curation pipeline for one bracket and persist the result.

    Pipeline:
      1. Pre-flight each candidate (1-game smoke). Reject any with
         unsupported cards.
      2. Classify each surviving candidate's archetype + color identity.
      3. Round-robin: each deck plays `pods_per_deck` pods × `games_per_pod` games.
      4. Aggregate wins / games_played / confirmAction per candidate.
      5. Apply AI-pilotability gate (reject confirmAction > MAX/game).
      6. Sort by win rate; take TOP_N; split into Pool A/B with diversity rules.
      7. Tag suspected_inflated; persist to `_pools/B<n>.json`.
    """
    runner = runner or ForgeRunner.locate()

    scores: dict[str, CandidateScore] = {
        f: CandidateScore(filename=f) for f in candidate_filenames
    }

    # 1. Pre-flight smoke
    for f in candidate_filenames:
        fillers = [c for c in candidate_filenames if c != f][:3]
        if len(fillers) < 3:
            scores[f].rejected_reason = "insufficient_fillers"
            continue
        pre = preflight_candidate(runner, f, fillers)
        scores[f].unsupported_cards = pre.unsupported
        if pre.crashed:
            scores[f].rejected_reason = f"preflight_crash:{pre.crash_reason}"
        elif pre.unsupported > MAX_UNSUPPORTED_CARDS_SMOKE:
            scores[f].rejected_reason = f"unsupported_cards={pre.unsupported}"

    survivors = [f for f, s in scores.items() if s.rejected_reason is None]

    # 2. Classify (archetype + color identity)
    for f in survivors:
        scores[f].archetype = classifier(DECK_DIR / f)
        scores[f].color_identity = _read_color_identity(DECK_DIR / f)

    # 3. Round-robin qualifier
    pods = schedule_pods(survivors, pods_per_deck=pods_per_deck, seed=seed)
    pod_analyses: list[dict] = []  # one entry per pod, persisted to analysis JSON
    for pod_idx, pod in enumerate(pods):
        print(f"  Pod {pod_idx + 1}/{len(pods)}: {pod}", flush=True)
        result = runner.run(pod, num_games=games_per_pod)
        parsed = parse(result.stdout)
        ma = analyze(result.stdout)
        pod_analyses.append({
            "pod_index": pod_idx + 1,
            "pod": pod,
            "duration_sec": round(result.duration_sec, 1),
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "match": ma.to_dict(),
        })
        for d in parsed.deck_results:
            # Match Match-Result names back to filenames via _normalize.
            fname = _filename_for_match(d.normalized_name, survivors)
            if not fname:
                continue
            scores[fname].games_played += parsed.games_completed
            scores[fname].wins += d.wins
            # log_parser now attributes confirmAction events to the deck whose
            # Phase line was active when the event fired. If attribution failed
            # (e.g. older Forge build without Phase markers), fall back to the
            # even-split stopgap so the metric still produces a number.
            attributed = parsed.confirm_action_by_deck.get(d.normalized_name)
            if attributed is not None:
                scores[fname].confirm_action_total += attributed
            else:
                scores[fname].confirm_action_total += (
                    len(parsed.confirm_action_cards) // max(1, len(pod))
                )

    # 4. AI-pilotability gate
    for f in survivors:
        if scores[f].confirm_action_per_game > MAX_CONFIRM_ACTION_PER_GAME:
            scores[f].rejected_reason = (
                f"ai_pilotability cap_per_game={scores[f].confirm_action_per_game:.1f}"
            )

    qualified = [s for s in scores.values() if s.rejected_reason is None]
    qualified.sort(key=lambda s: s.win_rate, reverse=True)
    top6 = qualified[:TOP_N]
    rejected = [s for s in scores.values() if s.rejected_reason is not None]

    pool_a, pool_b = _split_into_slices(top6) if top6 else ([], [])

    pool = CuratedPool(
        bracket=bracket,
        created_at=datetime.now(timezone.utc).isoformat(),
        pool_a=pool_a,
        pool_b=pool_b,
        scores=top6,
        rejected=rejected,
    )

    # Surface inflation suspects to caller via the score record; persist.
    pool_dir.mkdir(parents=True, exist_ok=True)
    out = pool_dir / f"B{bracket}.json"
    out.write_text(pool.to_json(), encoding="utf-8")
    print(f"Wrote curated pool: {out}")

    # Persist the per-pod game analysis alongside the pool. Two files (pool +
    # analysis) keep the pool JSON small enough to skim while the analysis JSON
    # carries the full per-game telemetry for deeper post-mortem.
    analysis_out = pool_dir / f"B{bracket}_analysis.json"
    analysis_out.write_text(
        json.dumps({"bracket": bracket, "pods": pod_analyses}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote game analysis: {analysis_out}")
    return pool


def _read_color_identity(deck_path: Path) -> str:
    """Resolve a deck's color identity from its commander(s) via Scryfall.

    Returns a WUBRG-ordered string ('WUBG' for Atraxa, '' for colorless or
    when the lookup fails). On Scryfall network errors, returns '' so the
    diversity rule falls back to archetype-only filtering rather than crashing
    the whole curation."""
    try:
        from .scryfall_client import color_identity_for_commander
        return color_identity_for_commander(deck_path)
    except Exception:
        # Scryfall outage / cache corruption — degrade gracefully.
        return ""


_UNIQUIFY_SUFFIX = re.compile(r"\s+\(\d+\)$")


def _candidate_match_keys(filename: str) -> list[str]:
    """Yield the strings the curator should accept as a match for `filename`.

    Forge's Match Result reports the deck's internal `Name=` field, not the
    filename. Three transforms can have happened on the round-trip from
    Moxfield → .dck → Forge → Match Result:

      1. `[USER]` prefix added by `moxfield_import` (display-only).
      2. ` [B<n>].dck` suffix added by `moxfield_import`.
      3. `_uniquify` may have appended ` (2)` / ` (3)` etc. before the
         bracket suffix when two decks sanitized to the same filename.

    We strip 1+2 first (existing behavior), then optionally strip 3. Both the
    stripped-and-non-stripped forms are valid match keys — return both so
    callers can compare against the most specific match available."""
    # Drop `[B<n>].dck` suffix.
    stem = filename.rsplit(" [B", 1)[0]
    # Drop `[USER] ` prefix so it aligns with Forge's reported Name=.
    if stem.startswith("[USER] "):
        stem = stem[len("[USER] "):]
    keys = [stem]
    # Drop ` (N)` uniquify suffix if present.
    deuniquified = _UNIQUIFY_SUFFIX.sub("", stem)
    if deuniquified != stem:
        keys.append(deuniquified)
    return keys


def _filename_for_match(normalized_match_name: str, candidate_filenames: list[str]) -> Optional[str]:
    """Map a Forge Match-Result name back to a candidate filename.

    Match strategy (most-specific first):
      1. Exact match against any candidate's stripped stem
         (handles plain decks + decks with `[USER]` prefix).
      2. Match against the de-uniquified stem
         (handles `_uniquify`'d collision suffixes — closes GAP-004).

    Returns the FIRST matching filename. If multiple candidates de-uniquify
    to the same stem (genuinely two different decks with the same internal
    Name=), the first one wins by alphabetical sort, but the curator's score
    accumulation will spread wins across both rows in that case which is
    incorrect by design — flagged in BACKLOG as a future hardening."""
    # Pass 1: prefer exact stem match (no uniquify normalization).
    for f in candidate_filenames:
        keys = _candidate_match_keys(f)
        if keys[0] == normalized_match_name:
            return f
    # Pass 2: allow de-uniquified match.
    for f in candidate_filenames:
        keys = _candidate_match_keys(f)
        if len(keys) > 1 and keys[1] == normalized_match_name:
            return f
    return None


def _list_bracket_candidates(bracket: int, deck_dir: Path = DECK_DIR) -> list[str]:
    """Return all non-[USER] .dck filenames at this bracket, alphabetized.

    Note: globbing `*[B<n>].dck` doesn't work — pathlib treats the brackets
    as a character class. We glob `*.dck` and filter by suffix instead."""
    suffix = f" [B{bracket}].dck"
    return sorted(
        f.name for f in deck_dir.glob("*.dck")
        if f.name.endswith(suffix) and not f.name.startswith("[USER]")
    )


def _sample_candidates(candidates: list[str], max_count: int, seed: int) -> list[str]:
    """Pick `max_count` candidates deterministically. Sorted-then-shuffled so
    the result is reproducible per seed but doesn't always pick alphabetical
    leaders (which would skew toward decks starting with `$`, `-`, etc.)."""
    if max_count <= 0 or max_count >= len(candidates):
        return candidates
    rng = random.Random(seed)
    chosen = list(candidates)
    rng.shuffle(chosen)
    return sorted(chosen[:max_count])


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for pool curation.

    Default --max-candidates is 12 because pool_curator is designed for ~12
    candidates (see schedule_pods). With 90+ B3 decks on disk, no cap means
    a 4.7-hour run; the cap keeps a curation under ~40 minutes."""
    import argparse
    p = argparse.ArgumentParser(prog="pool_curator")
    p.add_argument("--bracket", type=int, required=True)
    p.add_argument("--games-per-pod", type=int, default=DEFAULT_GAMES_PER_POD)
    p.add_argument("--pods-per-deck", type=int, default=DEFAULT_PODS_PER_DECK)
    p.add_argument("--seed", type=int, default=0,
                   help="Seed for both candidate sampling AND pod scheduling.")
    p.add_argument("--max-candidates", type=int, default=12,
                   help="Max candidates to curate (default 12). Use 0 for no cap "
                        "— but expect hours of wall time at 50+.")
    args = p.parse_args(argv)

    all_candidates = _list_bracket_candidates(args.bracket)
    candidates = _sample_candidates(all_candidates, args.max_candidates, args.seed)

    if len(candidates) < 4:
        print(f"Only {len(candidates)} B{args.bracket} candidates available "
              f"(of {len(all_candidates)} on disk) — need >=4. "
              f"Run `commander-import --harvest {args.bracket}` first.")
        return 2

    print(f"Curating B{args.bracket}: {len(candidates)} of "
          f"{len(all_candidates)} candidates "
          f"(seed={args.seed}, games_per_pod={args.games_per_pod}, "
          f"pods_per_deck={args.pods_per_deck})")
    pool = curate_bracket(
        args.bracket,
        candidates,
        games_per_pod=args.games_per_pod,
        pods_per_deck=args.pods_per_deck,
        seed=args.seed,
    )
    print(f"Pool A ({len(pool.pool_a)}): {pool.pool_a}")
    print(f"Pool B ({len(pool.pool_b)}): {pool.pool_b}")
    print(f"Rejected: {len(pool.rejected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
