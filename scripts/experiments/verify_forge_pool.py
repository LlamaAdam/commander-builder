"""FP-003 integration check: run 2 A/B sims serially, then concurrently across
the forge + forge2 profiles, and compare.

Confirms:
  * both profiles' forge.log files are written (distinct, isolated)
  * no deck-dir contention (both sims complete with sane verdicts)
  * concurrent wall-clock < serial wall-clock (the actual payoff)

Usage:
  python scripts/verify_forge_pool.py [games_per_job]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from commander_builder.forge_runner import (
    ABJob,
    ForgeRunner,
    run_ab_batch,
    run_ab_simulation,
)

REPO = Path(__file__).resolve().parents[2]
FORGE1 = REPO / "vendor" / "forge"
FORGE2 = REPO / "vendor" / "forge2"


def _pick_decks(n: int) -> list[Path]:
    deck_dir = FORGE1 / "userdata" / "decks" / "commander"
    decks = sorted(p for p in deck_dir.glob("*.dck") if "[USER]" not in p.name)
    if len(decks) < n:
        raise SystemExit(f"need >={n} decks, found {len(decks)}")
    return decks[:n]


def _log_state(profile: Path) -> tuple[bool, int, float]:
    log = profile / "userdata" / "forge.log"
    if not log.exists():
        return False, 0, 0.0
    st = log.stat()
    return True, st.st_size, st.st_mtime


def main() -> int:
    games = int(sys.argv[1]) if len(sys.argv) > 1 else 2

    # 8 distinct decks → two independent 4-deck pods.
    d = _pick_decks(8)
    jobs = [
        ABJob(deck_a=d[0], deck_b=d[1], fillers=[d[2].name, d[3].name], games=games),
        ABJob(deck_a=d[4], deck_b=d[5], fillers=[d[6].name, d[7].name], games=games),
    ]

    r1 = ForgeRunner.for_profile(FORGE1)
    r2 = ForgeRunner.for_profile(FORGE2)

    print(f"games/job   : {games}")
    print(f"job 1 pod   : {d[0].name} vs {d[1].name}")
    print(f"job 2 pod   : {d[4].name} vs {d[5].name}")

    # --- serial baseline (both on profile 1) ---
    print("\n=== SERIAL baseline (one profile) ===")
    t0 = time.monotonic()
    serial = [
        run_ab_simulation(j.deck_a, j.deck_b, games=j.games, runner=r1,
                          fillers=j.fillers)
        for j in jobs
    ]
    serial_sec = time.monotonic() - t0
    for i, r in enumerate(serial):
        print(f"  job {i+1}: {r.status}  {r.wins_a}-{r.wins_b} ({r.games}g)")
    print(f"  serial wall-clock: {serial_sec:.1f}s")

    # --- concurrent (profile 1 + profile 2) ---
    print("\n=== CONCURRENT (2 profiles via run_ab_batch) ===")
    pre = {"forge": _log_state(FORGE1), "forge2": _log_state(FORGE2)}
    t0 = time.monotonic()
    par = run_ab_batch(jobs, [r1, r2], games=games)
    par_sec = time.monotonic() - t0
    post = {"forge": _log_state(FORGE1), "forge2": _log_state(FORGE2)}
    for i, r in enumerate(par):
        print(f"  job {i+1}: {r.status}  {r.wins_a}-{r.wins_b} ({r.games}g)")
    print(f"  concurrent wall-clock: {par_sec:.1f}s")

    # --- checks ---
    print("\n=== CHECKS ===")
    both_done = all(r.status == "done" for r in par)
    print(f"  both concurrent jobs done : {both_done}")
    f1_touched = post["forge"][2] > pre["forge"][2]
    f2_touched = post["forge2"][2] > pre["forge2"][2]
    print(f"  forge/userdata/forge.log updated  : {f1_touched} ({post['forge'][1]}B)")
    print(f"  forge2/userdata/forge.log updated : {f2_touched} ({post['forge2'][1]}B)")
    speedup = serial_sec / par_sec if par_sec else 0
    faster = par_sec < serial_sec
    print(f"  speedup (serial/concurrent): {speedup:.2f}x  faster={faster}")

    ok = both_done and f1_touched and f2_touched and faster
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
