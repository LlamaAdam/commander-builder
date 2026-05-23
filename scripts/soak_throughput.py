"""Sim-throughput soak test (answers: "weeks, or hours?").

Runs Forge A/B sims continuously for a wall-clock budget across N
cwd-isolated profiles (FP-003 concurrency), and measures how many
games/sims actually complete per hour on this machine — so we can
project how long it really takes to accumulate the knowledge_log rows
the FP-002 (~200) / FP-013 (~2000) data gates need.

It does NOT write to knowledge_log (sqlite + many concurrent writers =
lock contention); instead it appends every sim result to a JSONL file
and rewrites a small summary JSON after each batch, so throughput is
measurable even if the run is killed early. The JSONL can be folded into
knowledge_log later, single-threaded, if desired.

Usage:
  python scripts/soak_throughput.py --hours 8 --runners 6 --games 5
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from commander_builder.forge_runner import (
    ABJob,
    ForgeRunner,
    VENDOR_FORGE,
    run_ab_batch,
)
from commander_builder._proposer_sim import _pick_filler_decks
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def _build_runners(n: int) -> list:
    """runner 0 → vendor/forge (located), runners 1..n-1 → vendor/forge{i+1}."""
    runners = [ForgeRunner.locate()]
    for i in range(2, n + 1):
        prof = VENDOR_FORGE.parent / f"forge{i}"
        if not prof.is_dir():
            raise SystemExit(f"profile missing: {prof} (run setup_forge_profile.py)")
        runners.append(ForgeRunner.for_profile(prof))
    return runners


def _deck_pairs() -> list[tuple[Path, Path]]:
    """All (base, v2) [USER] deck pairs present on disk."""
    names = {p.name for p in DECK_DIR.glob("*.dck")}
    pairs: list[tuple[Path, Path]] = []
    for n in sorted(names):
        if n.startswith("[USER]") and " v2 " in n:
            base = n.replace(" v2 ", " ")
            if base in names:
                pairs.append((DECK_DIR / base, DECK_DIR / n))
    return pairs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="soak_throughput")
    p.add_argument("--hours", type=float, default=8.0, help="Wall-clock budget.")
    p.add_argument("--runners", type=int, default=6, help="Concurrent Forge profiles.")
    p.add_argument("--games", type=int, default=5, help="Games per A/B sim.")
    p.add_argument("--out", type=Path, default=VENDOR_FORGE.parent / "_soak_throughput.jsonl")
    p.add_argument("--summary", type=Path, default=VENDOR_FORGE.parent / "_soak_summary.json")
    args = p.parse_args(argv)

    pairs = _deck_pairs()
    if not pairs:
        raise SystemExit("no (base, v2) deck pairs found to sim")
    runners = _build_runners(args.runners)
    rng = random.Random(1234)

    deadline = time.time() + args.hours * 3600.0
    start = time.time()
    batches = sims_done = sims_failed = games_done = 0
    wins_a_total = wins_b_total = 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Truncate the JSONL at start of a fresh run.
    args.out.write_text("", encoding="utf-8")

    def write_summary(final: bool) -> None:
        elapsed = time.time() - start
        gph = games_done / elapsed * 3600.0 if elapsed else 0.0
        sph = sims_done / elapsed * 3600.0 if elapsed else 0.0
        summary = {
            "updated": _now(),
            "final": final,
            "config": {"hours": args.hours, "runners": args.runners,
                       "games_per_sim": args.games, "pairs": len(pairs)},
            "elapsed_sec": round(elapsed, 1),
            "batches": batches,
            "sims_done": sims_done,
            "sims_failed": sims_failed,
            "games_done": games_done,
            "wins_a_total": wins_a_total,
            "wins_b_total": wins_b_total,
            "games_per_hour": round(gph, 1),
            "sims_per_hour": round(sph, 1),
            # A sim ≈ one knowledge_log row. Project the data gates.
            "projected_hours_for_200_rows": round(200 / sph, 2) if sph else None,
            "projected_hours_for_2000_rows": round(2000 / sph, 2) if sph else None,
        }
        args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"[soak] start: {args.runners} runners x {args.games} games/sim, "
          f"{len(pairs)} pairs, budget {args.hours}h", flush=True)

    with args.out.open("a", encoding="utf-8") as jf:
        while time.time() < deadline:
            # One job per runner: cycle the pairs, fresh bracket-matched
            # fillers each batch so games aren't byte-identical reruns.
            jobs: list[ABJob] = []
            for k in range(len(runners)):
                base, v2 = pairs[(batches * len(runners) + k) % len(pairs)]
                bracket = _bracket_from_filename(base.name) or 3
                fillers = _pick_filler_decks(
                    DECK_DIR, exclude_paths=[base, v2], count=2,
                    target_bracket=bracket, rng=rng,
                )
                if len(fillers) < 2:
                    continue
                jobs.append(ABJob(deck_a=base, deck_b=v2, fillers=fillers))
            if not jobs:
                break

            t0 = time.time()
            try:
                results = run_ab_batch(jobs, runners, games=args.games)
            except Exception as exc:  # noqa: BLE001 — keep soaking
                print(f"[soak] batch {batches} error: {type(exc).__name__}: {exc}",
                      flush=True)
                results = []
            batch_dt = time.time() - t0
            batches += 1

            for res in results:
                ok = getattr(res, "status", None) == "done"
                if ok:
                    sims_done += 1
                    games_done += res.games or 0
                    wins_a_total += res.wins_a or 0
                    wins_b_total += res.wins_b or 0
                else:
                    sims_failed += 1
                jf.write(json.dumps({
                    "ts": _now(),
                    "batch": batches,
                    "deck_a": res.deck_a,
                    "deck_b": res.deck_b,
                    "games": res.games,
                    "wins_a": res.wins_a,
                    "wins_b": res.wins_b,
                    "status": res.status,
                    "duration_sec": getattr(res, "duration_sec", None),
                }) + "\n")
            jf.flush()
            write_summary(final=False)
            elapsed = time.time() - start
            print(f"[soak] batch {batches}: +{len(results)} sims in "
                  f"{batch_dt:.0f}s | total sims={sims_done} games={games_done} "
                  f"| {games_done/elapsed*3600:.0f} games/hr "
                  f"({sims_done/elapsed*3600:.0f} sims/hr)", flush=True)

    write_summary(final=True)
    el = time.time() - start
    print(f"[soak] DONE: {sims_done} sims / {games_done} games in {el/3600:.2f}h "
          f"= {games_done/el*3600:.0f} games/hr, {sims_done/el*3600:.1f} sims/hr",
          flush=True)
    print(f"[soak] projection: ~200 rows in "
          f"{200/(sims_done/el*3600):.1f}h, ~2000 rows in "
          f"{2000/(sims_done/el*3600):.1f}h", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
