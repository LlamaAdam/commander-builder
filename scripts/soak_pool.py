"""Continuous, CPU-autoscaling sim-throughput pool (FP-003 stress test).

Improves on ``soak_throughput.py`` (which used the batch-barrier
``run_ab_batch`` — fast lanes idled waiting for the slowest sim each
batch). Here every runner is an independent worker thread that pulls the
next A/B job the instant its previous sim finishes, so there's no barrier
and lanes stay saturated.

It also self-tunes concurrency: a controller thread samples CPU every
~45s and adds a runner when CPU is below the target band or retires one
when above it, between ``--min`` and ``--max`` (each runner needs its own
cwd-isolated Forge profile, so ``--max`` is capped by how many profiles
exist: vendor/forge + vendor/forge2..N).

Output (rewritten every ~20s, append-per-sim, so the in-app viewer can
read it live — point ``--summary`` / ``--out`` inside the session folder):
  summary.json  — totals, games/hr, active_runners, cpu%, projections
  *.jsonl       — one line per completed sim

Usage:
  python scripts/soak_pool.py --hours 24 --min 4 --max 12 --start 8 --games 10
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from commander_builder.forge_runner import ForgeRunner, VENDOR_FORGE, run_ab_simulation
from commander_builder._proposer_sim import _pick_filler_decks
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def _profiles(max_n: int) -> list[Path]:
    """vendor/forge, vendor/forge2 .. vendor/forge{max_n}; only existing."""
    out = [VENDOR_FORGE]
    for i in range(2, max_n + 1):
        p = VENDOR_FORGE.parent / f"forge{i}"
        if p.is_dir():
            out.append(p)
    return out


def _runner_for(profile: Path) -> ForgeRunner:
    return ForgeRunner.locate() if profile == VENDOR_FORGE else ForgeRunner.for_profile(profile)


def _deck_pairs() -> list[tuple[Path, Path]]:
    names = {p.name for p in DECK_DIR.glob("*.dck")}
    pairs = []
    for n in sorted(names):
        if n.startswith("[USER]") and " v2 " in n:
            base = n.replace(" v2 ", " ")
            if base in names:
                pairs.append((DECK_DIR / base, DECK_DIR / n))
    return pairs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Soak:
    def __init__(self, args):
        self.args = args
        self.pairs = _deck_pairs()
        if not self.pairs:
            raise SystemExit("no (base, v2) deck pairs found")
        self.profiles = _profiles(args.max)
        if len(self.profiles) < args.min:
            raise SystemExit(f"only {len(self.profiles)} profiles; need >= --min {args.min}")
        self.max = min(args.max, len(self.profiles))
        self.deadline = time.time() + args.hours * 3600.0
        self.start_t = time.time()

        self.lock = threading.Lock()
        self.rng = random.Random(20260523)
        self._job_i = 0

        # Counters.
        self.sims_done = 0
        self.sims_failed = 0
        self.games_done = 0
        self.wins_a = 0
        self.wins_b = 0
        self.last_cpu = 0.0

        # Worker bookkeeping: free profiles + active worker registry.
        self.free_profiles = list(self.profiles)
        self.workers: dict[Path, dict] = {}  # profile -> {"thread", "retire"}

        self.args.out.parent.mkdir(parents=True, exist_ok=True)
        self.args.out.write_text("", encoding="utf-8")
        self.stop = threading.Event()

    # --- job feed ---------------------------------------------------------
    def next_job(self):
        with self.lock:
            base, v2 = self.pairs[self._job_i % len(self.pairs)]
            self._job_i += 1
            rng = random.Random(self.rng.random())
        bracket = _bracket_from_filename(base.name) or 3
        fillers = _pick_filler_decks(DECK_DIR, exclude_paths=[base, v2],
                                     count=2, target_bracket=bracket, rng=rng)
        return base, v2, fillers

    # --- one worker -------------------------------------------------------
    def worker(self, profile: Path):
        runner = _runner_for(profile)
        while not self.stop.is_set() and time.time() < self.deadline:
            with self.lock:
                if self.workers.get(profile, {}).get("retire"):
                    break
            base, v2, fillers = self.next_job()
            if len(fillers) < 2:
                time.sleep(1)
                continue
            try:
                res = run_ab_simulation(deck_a_path=base, deck_b_path=v2,
                                        games=self.args.games, fillers=fillers,
                                        runner=runner)
            except Exception as exc:  # noqa: BLE001
                self._record(None, f"{type(exc).__name__}: {exc}", base, v2)
                continue
            self._record(res, None, base, v2)
        with self.lock:
            self.workers.pop(profile, None)
            self.free_profiles.append(profile)

    def _record(self, res, err, base, v2):
        with self.lock:
            if res is not None and getattr(res, "status", None) == "done":
                self.sims_done += 1
                self.games_done += res.games or 0
                self.wins_a += res.wins_a or 0
                self.wins_b += res.wins_b or 0
            else:
                self.sims_failed += 1
            line = json.dumps({
                "ts": _now(),
                "deck_a": base.name, "deck_b": v2.name,
                "games": getattr(res, "games", None),
                "wins_a": getattr(res, "wins_a", None),
                "wins_b": getattr(res, "wins_b", None),
                "status": getattr(res, "status", "error"),
                "duration_sec": getattr(res, "duration_sec", None),
                "error": err,
            })
            with self.args.out.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    # --- scaling ----------------------------------------------------------
    def _spawn(self):
        if not self.free_profiles:
            return False
        profile = self.free_profiles.pop(0)
        t = threading.Thread(target=self.worker, args=(profile,), daemon=True)
        self.workers[profile] = {"thread": t, "retire": False}
        t.start()
        return True

    def _retire_one(self):
        for prof, info in self.workers.items():
            if not info["retire"]:
                info["retire"] = True
                return True
        return False

    def active_count(self) -> int:
        return sum(1 for i in self.workers.values() if not i["retire"])

    def write_summary(self, final=False):
        with self.lock:
            elapsed = time.time() - self.start_t
            gph = self.games_done / elapsed * 3600 if elapsed else 0
            sph = self.sims_done / elapsed * 3600 if elapsed else 0
            summary = {
                "updated": _now(), "final": final,
                "config": {"hours": self.args.hours, "games_per_sim": self.args.games,
                           "min": self.args.min, "max": self.max},
                "active_runners": self.active_count(),
                "cpu_pct": round(self.last_cpu, 1),
                "elapsed_hours": round(elapsed / 3600, 3),
                "sims_done": self.sims_done, "sims_failed": self.sims_failed,
                "games_done": self.games_done,
                "wins_a_total": self.wins_a, "wins_b_total": self.wins_b,
                "games_per_hour": round(gph, 1), "sims_per_hour": round(sph, 1),
                "projected_hours_for_200_rows": round(200 / sph, 2) if sph else None,
                "projected_hours_for_2000_rows": round(2000 / sph, 2) if sph else None,
                "eta_24h_games": round(gph * 24) if gph else None,
                "eta_24h_sims": round(sph * 24) if sph else None,
            }
        self.args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # --- run --------------------------------------------------------------
    def run(self):
        print(f"[soak] start: {self.args.start} runners (min {self.args.min}, "
              f"max {self.max}), {self.args.games} games/sim, "
              f"{len(self.pairs)} pairs, budget {self.args.hours}h", flush=True)
        for _ in range(min(self.args.start, self.max)):
            self._spawn()

        last_summary = 0.0
        while time.time() < self.deadline:
            cpu = psutil.cpu_percent(interval=2.0)
            with self.lock:
                self.last_cpu = cpu
                active = self.active_count()
            # Autoscale toward the target band.
            if cpu < self.args.cpu_low and active < self.max and self.free_profiles:
                with self.lock:
                    self._spawn()
                print(f"[soak] cpu {cpu:.0f}% < {self.args.cpu_low} → +1 runner "
                      f"({active + 1})", flush=True)
            elif cpu > self.args.cpu_high and active > self.args.min:
                with self.lock:
                    self._retire_one()
                print(f"[soak] cpu {cpu:.0f}% > {self.args.cpu_high} → -1 runner "
                      f"({active - 1})", flush=True)

            if time.time() - last_summary > 20:
                self.write_summary()
                last_summary = time.time()
                with self.lock:
                    el = time.time() - self.start_t
                    print(f"[soak] {self.active_count()} runners | cpu {cpu:.0f}% | "
                          f"sims {self.sims_done} games {self.games_done} | "
                          f"{self.games_done/el*3600:.0f} games/hr "
                          f"{self.sims_done/el*3600:.1f} sims/hr", flush=True)
            time.sleep(max(0, self.args.control_interval - 2.0))

        self.stop.set()
        # Let in-flight sims finish; they exit at the deadline check.
        time.sleep(2)
        self.write_summary(final=True)
        el = time.time() - self.start_t
        print(f"[soak] DONE: {self.sims_done} sims / {self.games_done} games in "
              f"{el/3600:.2f}h = {self.games_done/el*3600:.0f} games/hr, "
              f"{self.sims_done/el*3600:.1f} sims/hr", flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="soak_pool")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--min", type=int, default=4)
    p.add_argument("--max", type=int, default=12)
    p.add_argument("--start", type=int, default=8)
    p.add_argument("--games", type=int, default=10)
    p.add_argument("--cpu-low", type=float, default=78.0, help="Add a runner below this CPU%%.")
    p.add_argument("--cpu-high", type=float, default=92.0, help="Retire a runner above this CPU%%.")
    p.add_argument("--control-interval", type=float, default=45.0)
    p.add_argument("--out", type=Path, default=Path("C:/Users/pilot/soak_throughput.jsonl"))
    p.add_argument("--summary", type=Path, default=Path("C:/Users/pilot/soak_summary.json"))
    args = p.parse_args(argv)
    Soak(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
