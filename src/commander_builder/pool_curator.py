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
    # Filler sets used for each preflight smoke attempt (1 normally, 2 when the
    # first pod failed and the candidate got a second chance). Persisted so a
    # post-mortem on a rejected candidate can spot a shared culprit filler —
    # Forge's unsupported-card / crash output is pod-global, not per-deck.
    preflight_pods: list[list[str]] = field(default_factory=list)

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
    """Run a 1-game smoke sim for one candidate. `fillers` are 3 pod-mates
    (fellow UNVETTED candidates — see _preflight_pool) that pad the pod to 4.
    Reports both the parsed result and a crash signal — a timeout / non-zero
    returncode / empty parse means SOMETHING in the pod can't survive a single
    game. IMPORTANT: every failure signal here is pod-global. Forge's
    "An unsupported card was requested" line names only the card (never the
    deck), and a crash takes down all 4 decks — so the caller must not blame
    the candidate for a single failed pod; see _preflight_pool for the
    blame-isolation protocol."""
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


class InsufficientSurvivorsError(RuntimeError):
    """Preflight left fewer than 4 survivors, so no qualifier pod can be
    scheduled. Raised INSTEAD of letting schedule_pods' bare ValueError escape
    with a traceback after the full smoke spend. Carries the per-candidate
    rejection records and the path of the persisted preflight JSON so callers
    (CLI, tests) can report each rejection without re-deriving anything."""

    def __init__(
        self,
        message: str,
        rejected: list[CandidateScore],
        preflight_path: Optional[Path] = None,
    ):
        super().__init__(message)
        self.rejected = rejected
        self.preflight_path = preflight_path


def _preflight_failure_reason(pre: PreflightResult) -> Optional[str]:
    """Map a PreflightResult to a rejection reason string, or None if clean.
    Shared by both preflight passes so first-attempt and retry failures are
    judged by identical rules."""
    if pre.crashed:
        return f"preflight_crash:{pre.crash_reason}"
    if pre.unsupported > MAX_UNSUPPORTED_CARDS_SMOKE:
        return f"unsupported_cards={pre.unsupported}"
    return None


def _preflight_pool(
    runner: ForgeRunner,
    candidates: list[str],
    scores: dict[str, CandidateScore],
) -> int:
    """Smoke-test every candidate with blame isolation. Mutates `scores`
    (rejected_reason / unsupported_cards / preflight_pods) and returns the
    number of smoke games spent.

    Why this exists (bug fix): Forge's unsupported-card output names only the
    CARD, never the deck (see log_parser._UNSUPPORTED), and a crash takes down
    the whole 4-deck pod — so every smoke failure is pod-global. The old code
    padded EVERY candidate's pod with the same 3 alphabetically-first unvetted
    candidates, meaning one bad filler sat in essentially every pod and zeroed
    the entire pool. The invariant enforced here: one bad deck must not be
    able to zero the pool.

    Blame isolation, two mechanisms:
      1. Rotated fillers — candidate i is padded with the 3 candidates that
         follow it in cyclic order, so no single deck appears in every pod
         (each deck fills exactly 3 pods for pools >= 4).
      2. Second chance — a candidate whose pod failed is NOT rejected yet; it
         gets ONE retry in a pod with a different filler set (first-pass clean
         survivors preferred, first-pod fillers excluded whenever the pool is
         big enough). A clean retry exonerates it: the first failure belonged
         to a pod-mate. Only a candidate that fails BOTH pods is rejected, and
         its rejection reason + preflight_pods record both filler sets so a
         post-mortem can spot a shared culprit.

    Cost is bounded: N first-pass games + at most one retry per first-pass
    failure => <= 2N smoke games. The total is printed at the end.
    """
    ordered = list(candidates)
    games = 0
    # (candidate, first-pod fillers, first failure reason) — deferred verdicts.
    suspects: list[tuple[str, list[str], str]] = []

    # Pass 1: rotated-filler smoke for every candidate.
    for i, f in enumerate(ordered):
        # Cyclic rotation: the 3 candidates after f, wrapping around. Contrast
        # with the old `[c for c in candidates if c != f][:3]`, which put the
        # same alphabetical leaders in every single pod.
        others = ordered[i + 1:] + ordered[:i]
        fillers = others[:3]
        if len(fillers) < 3:
            scores[f].rejected_reason = "insufficient_fillers"
            continue
        scores[f].preflight_pods.append(list(fillers))
        pre = preflight_candidate(runner, f, fillers)
        games += 1
        scores[f].unsupported_cards = pre.unsupported
        reason = _preflight_failure_reason(pre)
        if reason:
            # Do NOT reject yet — the failure may belong to a filler.
            suspects.append((f, fillers, reason))

    # Decks that passed their own first-pass pod cleanly. Preferred as retry
    # fillers because a pod of proven-clean decks makes the retry verdict
    # attributable to the candidate alone.
    clean = [
        f for f in ordered
        if scores[f].rejected_reason is None
        and f not in {s[0] for s in suspects}
    ]

    # Pass 2: one retry per suspect, with a different filler set.
    for f, first_fillers, first_reason in suspects:
        preferred = [c for c in clean if c != f and c not in first_fillers]
        backup = [
            c for c in ordered
            if c != f and c not in first_fillers and c not in preferred
        ]
        alt = (preferred + backup)[:3]
        if len(alt) < 3:
            # Pool too small for a fully-fresh set of 3 — pad with first-pod
            # fillers (least-bad option; the retry still differs if ANY slot
            # changed).
            alt = (alt + [c for c in first_fillers if c not in alt])[:3]
        if set(alt) == set(first_fillers):
            # e.g. exactly 4 candidates: the pod composition is forced, so a
            # retry proves nothing. Reject, but flag that blame is unresolved
            # so the operator doesn't over-trust the verdict.
            scores[f].rejected_reason = (
                f"{first_reason} (pod={first_fillers}; "
                f"no alternate fillers available — blame unresolved)"
            )
            continue
        scores[f].preflight_pods.append(list(alt))
        print(
            f"  Preflight retry: {f} (first failure: {first_reason}; "
            f"new fillers: {alt})",
            flush=True,
        )
        pre2 = preflight_candidate(runner, f, alt)
        games += 1
        scores[f].unsupported_cards = pre2.unsupported
        second_reason = _preflight_failure_reason(pre2)
        if second_reason is None:
            # Exonerated: with different pod-mates the candidate is clean, so
            # the first failure was a pod-mate's fault. No rejection.
            print(
                f"  Preflight exonerated: {f} — first failure was pod-global "
                f"(fillers were {first_fillers})",
                flush=True,
            )
            continue
        # Failed twice with different pods — reject, carrying both pod
        # contexts for post-mortem.
        scores[f].rejected_reason = (
            f"{second_reason} (failed twice: first {first_reason} with "
            f"pod={first_fillers}, then retry with pod={alt})"
        )

    print(
        f"  Preflight cost: {games} smoke game(s) for {len(ordered)} "
        f"candidate(s) ({len(suspects)} retried)",
        flush=True,
    )
    return games


def _confirm_actions_for(parsed: ParsedSim, normalized_name: str, pod_size: int) -> int:
    """Confirm-action events chargeable to one deck for one pod's parse.

    Why the emptiness check matters (bug fix): log_parser only creates a key
    in confirm_action_by_deck on a deck's FIRST attributed event, so a missing
    key means two very different things depending on whether the dict has ANY
    entries:

      - dict NON-empty → Phase attribution worked for this pod, and a missing
        key is a real zero: the deck simply never triggered confirmAction.
      - dict empty     → attribution unavailable (e.g. an older Forge build
        without Phase markers); fall back to the even-split stopgap.

    The old code used the fallback whenever the deck's OWN key was missing,
    charging a clean deck a quarter of the whole pod's noise — with noisy
    pod-mates that could breach MAX_CONFIRM_ACTION_PER_GAME and reject a deck
    for its opponents' behavior."""
    if parsed.confirm_action_by_deck:
        return parsed.confirm_action_by_deck.get(normalized_name, 0)
    return len(parsed.confirm_action_cards) // max(1, pod_size)


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

    # 1. Pre-flight smoke — blame-isolated (rotated fillers + one retry per
    #    failed pod; see _preflight_pool for the full protocol and cost bound).
    _preflight_pool(runner, candidate_filenames, scores)

    survivors = [f for f, s in scores.items() if s.rejected_reason is None]

    if len(survivors) < 4:
        # schedule_pods needs >= 4 decks. Letting its bare ValueError escape
        # here (the old behavior) produced an uncaught traceback AFTER the
        # full smoke spend, with no record of why each candidate died. Persist
        # the preflight verdicts first so the spend isn't wasted, then raise a
        # typed error the CLI turns into an actionable non-zero exit.
        rejected = [s for s in scores.values() if s.rejected_reason is not None]
        pool_dir.mkdir(parents=True, exist_ok=True)
        preflight_out = pool_dir / f"B{bracket}_preflight.json"
        preflight_out.write_text(
            json.dumps(
                {
                    "bracket": bracket,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "survivors": survivors,
                    "rejected": [asdict(s) for s in rejected],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        lines = [
            f"Preflight left {len(survivors)} survivor(s) of "
            f"{len(candidate_filenames)} candidate(s) — need >=4 to schedule "
            f"qualifier pods.",
        ]
        for s in rejected:
            lines.append(f"  REJECTED {s.filename}: {s.rejected_reason}")
        lines.append(
            f"Preflight results persisted to {preflight_out} "
            f"(smoke spend not wasted)."
        )
        lines.append(
            "Fix or remove the rejected decks (or harvest more candidates "
            "with `commander-import --harvest`) and re-run."
        )
        message = "\n".join(lines)
        print(message, flush=True)
        raise InsufficientSurvivorsError(
            message, rejected=rejected, preflight_path=preflight_out
        )

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
            # log_parser attributes confirmAction events to the deck whose
            # Phase line was active when the event fired. _confirm_actions_for
            # decides between the attributed count (zero-events == real zero)
            # and the pod-average fallback (only when attribution was
            # unavailable for the WHOLE pod) — see its docstring for why the
            # per-deck .get(...) is None check was a bug.
            scores[fname].confirm_action_total += _confirm_actions_for(
                parsed, d.normalized_name, len(pod)
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
    """Return all pool-candidate .dck filenames at this bracket, alphabetized.

    Excluded prefixes:
      [USER]    — the user's own decks are never pool candidates.
      [CONTROL] — calibration_check leaves "[CONTROL] do-nothing calibN
                  [B<n>].dck" files in the same deck dir. They carry the
                  bracket suffix, so the glob would happily seat them as
                  candidates — burning smoke games on decks DESIGNED to do
                  nothing (and, worse, letting a do-nothing deck occupy one
                  of the limited --max-candidates slots). Mirrors
                  _proposer_sim's filler exclusion ("never use a calibration
                  deck as filler").
    [REF] decks (meta_test's imported community references) are deliberately
    KEPT: they are real, playable community builds at the bracket — exactly
    the population the curator exists to rank.

    Note: globbing `*[B<n>].dck` doesn't work — pathlib treats the brackets
    as a character class. We glob `*.dck` and filter by suffix instead."""
    suffix = f" [B{bracket}].dck"
    return sorted(
        f.name for f in deck_dir.glob("*.dck")
        if f.name.endswith(suffix)
        and not f.name.startswith("[USER]")
        and not f.name.startswith("[CONTROL]")
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

    # Pool hygiene (ManaFoundry parity): warn — NEVER reject — when a
    # candidate's estimated bracket differs from its [Bn] tag by >= 2.
    # A mislabeled deck poisons the pool it joins (a de-facto B4 list
    # tagged [B2] farms wins off genuine B2 decks and skews every
    # ranking downstream), but the estimator is a heuristic so the
    # human decides. Low-confidence estimates come back as a NOTE
    # ("unavailable/low-signal"), not a WARN — insufficient signal is
    # not a mismatch. mismatch_warning never raises; the per-deck read
    # is guarded so one unreadable file can't abort curation.
    from .bracket_estimator import mismatch_warning
    for cand in candidates:
        try:
            text = (DECK_DIR / cand).read_text(encoding="utf-8")
        except OSError:
            continue
        warning = mismatch_warning(cand, text, args.bracket)
        if warning:
            print(f"  {warning}", flush=True)
    try:
        pool = curate_bracket(
            args.bracket,
            candidates,
            games_per_pod=args.games_per_pod,
            pods_per_deck=args.pods_per_deck,
            seed=args.seed,
        )
    except InsufficientSurvivorsError:
        # curate_bracket already printed the per-candidate rejection report
        # and persisted the preflight JSON. Exit 3 distinguishes "preflight
        # rejected the pool" from 2 ("not enough decks on disk") — no
        # traceback, the message above is the whole story.
        return 3
    print(f"Pool A ({len(pool.pool_a)}): {pool.pool_a}")
    print(f"Pool B ({len(pool.pool_b)}): {pool.pool_b}")
    print(f"Rejected: {len(pool.rejected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
