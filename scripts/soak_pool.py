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

Failure-storm protection (``StormBreaker``): instant launch failures
back off per-runner, ~10 consecutive ones across the pool open a
circuit breaker (canary probe every 15 min), and rows are suppressed at
source while it's open — see the class docstring for the 2026-07-24
incident this guards against.

Usage:
  python scripts/soak_pool.py --hours 24 --min 4 --max 12 --start 8 --games 10
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr so a stray non-ASCII char in a log line can
# never crash the run on a cp1252 Windows console (this killed a prior
# launch). errors="replace" makes encoding failures non-fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

from commander_builder.forge_runner import (
    ForgeRunner, VENDOR_FORGE, run_ab_simulation, run_gauntlet_simulation)
from commander_builder._proposer_sim import _pick_filler_decks
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"

# Fixed 3-deck gauntlet for --mode gauntlet. Held CONSTANT across every test
# deck AND across machines (committed here) so a v1-vs-v2 win-rate delta is
# attributable to the deck edit alone, not a shifting field. box1 and box2
# MUST run the IDENTICAL gauntlet for merged verdicts to be valid. These are
# the MH3 Commander precons: a balanced, distinct-strategy mid-power field
# (colorless Eldrazi ramp / graveyard value / artifact-energy).
GAUNTLET = [
    "Eldrazi Incursion [M3C] [2024].dck",
    "Graveyard Overdrive [M3C] [2024].dck",
    "Creative Energy [M3C] [2024].dck",
]


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


# --- failure-storm protection ------------------------------------------
INSTANT_FAIL_SEC = 5.0       # a run failing faster than this never really started
BACKOFF_BASE_SEC = 5.0       # first per-runner retry delay after an instant failure
BACKOFF_CAP_SEC = 300.0      # per-runner backoff ceiling
BREAKER_OPEN_AFTER = 10      # consecutive pool-wide instant failures to open
CANARY_INTERVAL_SEC = 900.0  # one probe attempt per this many seconds while open
OPEN_ROW_CAP = 5             # failure rows written per open episode before suppression


class StormBreaker:
    """Failure-storm protection for the runner pool.

    Guards against the 2026-07-24 incident: the Windows session hosting
    the soak was logged off, every Forge JVM launch died instantly (exit
    3221225794 / 0xC0000142, DLL init failure in a non-interactive
    session), and with no backoff the pool looped flat-out for ~2 days
    writing ~85M sub-second ``"status": "failed"`` rows (39 GB) to the
    throughput JSONL. Three layers make a repeat impossible by
    construction:

      1. Per-runner backoff: an INSTANT failure (< ``instant_sec``, the
         JVM never really started) makes that runner sleep with
         exponential backoff (``backoff_base`` doubling up to
         ``backoff_cap``) before retrying. Any success resets it.
      2. Circuit breaker: ``open_after`` CONSECUTIVE instant failures
         across the whole pool open the breaker — no new runs launch;
         one canary probe goes out every ``canary_interval`` seconds and
         a successful canary (or any success) closes it again.
      3. Row suppression at source: while open, at most ``open_row_cap``
         failure rows are written per open episode; beyond that, each
         canary interval gets ONE ``storm_suppressed`` summary row
         carrying the running suppressed-failure count.

    ``now``/``sleep``/``log`` are injectable seams so the tests drive a
    fake clock with no real sleeps (and no Forge).
    """

    def __init__(self, *, instant_sec: float = INSTANT_FAIL_SEC,
                 backoff_base: float = BACKOFF_BASE_SEC,
                 backoff_cap: float = BACKOFF_CAP_SEC,
                 open_after: int = BREAKER_OPEN_AFTER,
                 canary_interval: float = CANARY_INTERVAL_SEC,
                 open_row_cap: int = OPEN_ROW_CAP,
                 now=time.monotonic, sleep=time.sleep, log=print):
        self.instant_sec = instant_sec
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.open_after = open_after
        self.canary_interval = canary_interval
        self.open_row_cap = open_row_cap
        self._now = now
        self._sleep = sleep
        self._log = log

        self.lock = threading.Lock()
        self._runner_fails: dict = {}  # profile -> consecutive instant failures
        self._consecutive = 0          # pool-wide consecutive instant failures
        self._open = False
        self._opened_at = 0.0
        self._next_canary = 0.0
        self._canary_inflight = False
        self._open_rows = 0            # failure rows written this open episode
        self._last_summary = 0.0
        # Storm counters, surfaced in summary.json and the DONE line.
        self.total_failures = 0
        self.suppressed_rows = 0
        self.open_count = 0
        self.open_seconds = 0.0        # closed episodes only; see open_time()

    @property
    def is_open(self) -> bool:
        with self.lock:
            return self._open

    def open_time(self) -> float:
        """Total seconds the breaker has been open, live span included."""
        with self.lock:
            t = self.open_seconds
            if self._open:
                t += self._now() - self._opened_at
            return t

    def _backoff(self, fails: int) -> float:
        if fails <= 0:
            return 0.0
        return min(self.backoff_cap, self.backoff_base * 2 ** (fails - 1))

    def _sleep_chunked(self, delay: float, should_stop) -> None:
        """Sleep ``delay`` seconds in <=5s slices so a shutting-down pool
        never waits out a full backoff or canary interval."""
        end = self._now() + delay
        while True:
            if should_stop is not None and should_stop():
                return
            remaining = end - self._now()
            if remaining <= 0:
                return
            self._sleep(min(5.0, remaining))

    def pre_run_wait(self, profile, should_stop=None) -> bool:
        """Block until this runner may attempt a run.

        Serves the runner's backoff when closed; while open, parks every
        runner except the single canary probe per interval. Returns True
        when the green-lit attempt is a canary probe, False for a normal
        run (including an early return when ``should_stop()`` goes true).
        """
        served_backoff = False
        while True:
            if should_stop is not None and should_stop():
                return False
            with self.lock:
                if not self._open:
                    if served_backoff:
                        return False
                    fails = self._runner_fails.get(profile, 0)
                    delay = self._backoff(fails)
                    if delay <= 0:
                        return False
                    wait = None  # serve the backoff outside the lock
                else:
                    now = self._now()
                    if now >= self._next_canary and not self._canary_inflight:
                        self._canary_inflight = True
                        self._log(f"[soak] breaker: canary probe "
                                  f"(open {now - self._opened_at:.0f}s)")
                        return True
                    wait = (5.0 if self._canary_inflight
                            else min(5.0, max(0.1, self._next_canary - now)))
            if wait is None:
                self._log(f"[soak] {getattr(profile, 'name', profile)}: "
                          f"{fails} instant failure(s) -> backoff {delay:.0f}s")
                self._sleep_chunked(delay, should_stop)
                served_backoff = True  # re-check open state, don't re-sleep
            else:
                self._sleep(wait)

    def record(self, profile, *, ok: bool, duration_sec) -> str:
        """Book one run result and return the row action for the recorder:
        'write' (normal row), 'suppress' (write nothing), or 'summary'
        (write one storm-summary row in place of the failure row)."""
        with self.lock:
            now = self._now()
            if ok:
                self._runner_fails.pop(profile, None)
                self._consecutive = 0
                self._canary_inflight = False
                if self._open:
                    self._close(now)
                return "write"
            self.total_failures += 1
            instant = duration_sec is not None and duration_sec < self.instant_sec
            if instant:
                self._runner_fails[profile] = self._runner_fails.get(profile, 0) + 1
                self._consecutive += 1
            else:
                # The JVM genuinely ran (mid-game crash, timeout): not a
                # launch storm — break the consecutive-instant chain.
                self._runner_fails.pop(profile, None)
                self._consecutive = 0
            if self._open:
                self._canary_inflight = False
                self._next_canary = now + self.canary_interval
                self._open_rows += 1
                if self._open_rows <= self.open_row_cap:
                    return "write"
                self.suppressed_rows += 1
                if now - self._last_summary >= self.canary_interval:
                    self._last_summary = now
                    return "summary"
                return "suppress"
            if self._consecutive >= self.open_after:
                self._open = True
                self.open_count += 1
                self._opened_at = now
                self._open_rows = 1  # the row for THIS failure counts
                self._next_canary = now + self.canary_interval
                self._last_summary = now
                self._log(f"[soak] BREAKER OPEN: {self._consecutive} consecutive "
                          f"instant failures (<{self.instant_sec:.0f}s) -> pausing "
                          f"all runs; one canary probe every "
                          f"{self.canary_interval:.0f}s")
            return "write"

    def _close(self, now: float) -> None:
        span = now - self._opened_at
        self.open_seconds += span
        self._open = False
        self._runner_fails.clear()  # environment healed; forget old backoffs
        self._log(f"[soak] BREAKER CLOSED: probe succeeded after {span:.0f}s open "
                  f"({self.suppressed_rows} failure rows suppressed so far); "
                  f"resuming normal operation")


class Soak:
    def __init__(self, args):
        self.args = args
        self.mode = getattr(args, "mode", "ab")
        self.pairs = _deck_pairs()
        if not self.pairs:
            raise SystemExit("no (base, v2) deck pairs found")
        if self.mode == "gauntlet":
            missing = [g for g in GAUNTLET if not (DECK_DIR / g).exists()]
            if missing:
                raise SystemExit(
                    f"gauntlet decks missing from {DECK_DIR}: {missing}")
            # Each base AND each v2 is tested individually against the fixed
            # gauntlet; comparing a base's win rate to its v2's (same field)
            # is the verdict.
            self.test_decks = [d for pair in self.pairs for d in pair]
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
        # Phase 1 runs at args.games (fast, to bank the FP-002 row gate);
        # phase 2 switches to args.phase2_games (high-confidence verdicts)
        # once sims_done crosses args.phase2_after.
        self.current_games = args.games
        self.phase = 1

        # Worker bookkeeping: free profiles + active worker registry.
        self.free_profiles = list(self.profiles)
        self.workers: dict[Path, dict] = {}  # profile -> {"thread", "retire"}

        # Failure-storm protection (backoff + circuit breaker + row
        # suppression); see StormBreaker for the incident it prevents.
        self.breaker = StormBreaker(log=lambda m: print(m, flush=True))

        self.args.out.parent.mkdir(parents=True, exist_ok=True)
        # By default a fresh run truncates its output. --append keeps the
        # existing rows so a restart (e.g. switching game count to chase a
        # row-count gate) accumulates instead of wiping prior data.
        if not getattr(self.args, "append", False):
            self.args.out.write_text("", encoding="utf-8")
        elif not self.args.out.exists():
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

    def next_gauntlet_job(self) -> Path:
        with self.lock:
            test = self.test_decks[self._job_i % len(self.test_decks)]
            self._job_i += 1
        return test

    # --- one worker -------------------------------------------------------
    def _halted(self) -> bool:
        return self.stop.is_set() or time.time() >= self.deadline

    @staticmethod
    def _run_duration(res, t0: float) -> float:
        """Wall time of one attempt; res.duration_sec when the sim
        measured itself, our own clock on the exception-free-but-early
        paths where it didn't."""
        dur = getattr(res, "duration_sec", None)
        return dur if dur is not None else time.monotonic() - t0

    def worker(self, profile: Path):
        runner = _runner_for(profile)
        while not self._halted():
            with self.lock:
                if self.workers.get(profile, {}).get("retire"):
                    break
            # Storm gate: serves this runner's failure backoff, and while
            # the breaker is open parks everyone except the one canary
            # probe per interval.
            self.breaker.pre_run_wait(profile, should_stop=self._halted)
            if self._halted():
                break
            t0 = time.monotonic()
            if self.mode == "gauntlet":
                test = self.next_gauntlet_job()
                try:
                    res = run_gauntlet_simulation(
                        test, GAUNTLET, games=self.current_games,
                        runner=runner, timeout_per_game=self.args.timeout)
                except Exception as exc:  # noqa: BLE001
                    action = self.breaker.record(
                        profile, ok=False, duration_sec=time.monotonic() - t0)
                    self._record_gauntlet(None, f"{type(exc).__name__}: {exc}",
                                          test, action=action)
                    continue
                ok = getattr(res, "status", None) in ("done", "loop_unattributed")
                action = self.breaker.record(
                    profile, ok=ok, duration_sec=self._run_duration(res, t0))
                self._record_gauntlet(res, None, test, action=action)
                continue
            base, v2, fillers = self.next_job()
            if len(fillers) < 2:
                time.sleep(1)
                continue
            try:
                res = run_ab_simulation(deck_a_path=base, deck_b_path=v2,
                                        games=self.current_games, fillers=fillers,
                                        runner=runner,
                                        timeout_per_game=self.args.timeout)
            except Exception as exc:  # noqa: BLE001
                action = self.breaker.record(
                    profile, ok=False, duration_sec=time.monotonic() - t0)
                self._record(None, f"{type(exc).__name__}: {exc}", base, v2,
                             action=action)
                continue
            ok = getattr(res, "status", None) == "done"
            action = self.breaker.record(
                profile, ok=ok, duration_sec=self._run_duration(res, t0))
            self._record(res, None, base, v2, action=action)
        with self.lock:
            self.workers.pop(profile, None)
            self.free_profiles.append(profile)

    def _write_storm_summary_locked(self):
        """One bounded ``storm_suppressed`` row instead of a row per
        failure while the breaker is open — the 39 GB storm file is
        impossible by construction. Downstream folds skip it (status is
        never 'done'/'loop_unattributed')."""
        line = json.dumps({
            "ts": _now(),
            "host": self.args.label,
            "status": "storm_suppressed",
            "suppressed_failures": self.breaker.suppressed_rows,
            "breaker_open_sec": round(self.breaker.open_time(), 1),
        })
        with self.args.out.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _record(self, res, err, base, v2, action="write"):
        with self.lock:
            if res is not None and getattr(res, "status", None) == "done":
                self.sims_done += 1
                self.games_done += res.games or 0
                self.wins_a += res.wins_a or 0
                self.wins_b += res.wins_b or 0
            else:
                self.sims_failed += 1
            if action != "write":
                # Breaker open past the per-episode row cap: counters
                # above stay accurate, but the failure row itself is
                # suppressed at source.
                if action == "summary":
                    self._write_storm_summary_locked()
                return
            line = json.dumps({
                "ts": _now(),
                "host": self.args.label,
                "deck_a": base.name, "deck_b": v2.name,
                "games": getattr(res, "games", None),
                "wins_a": getattr(res, "wins_a", None),
                "wins_b": getattr(res, "wins_b", None),
                "status": getattr(res, "status", "error"),
                "duration_sec": getattr(res, "duration_sec", None),
                # err is set on the exception path; on the non-exception path
                # the worker passes None, so fall back to res.error (e.g.
                # "Forge exited with code N" / "Timed out after Ns") instead of
                # logging a blank — otherwise failed sims are undiagnosable.
                "error": err or getattr(res, "error", None),
            })
            with self.args.out.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _record_gauntlet(self, res, err, test: Path, action="write"):
        with self.lock:
            # 'loop_unattributed' is a legitimate SHORT row, not a failure:
            # the batch was cut by a looping game that no seat could be
            # credited for (Forge prints the game log only after a game
            # completes, so a hung game has no Turn line to attribute), but
            # every game counted in res.games DID complete and is real data.
            if res is not None and getattr(res, "status", None) in (
                    "done", "loop_unattributed"):
                self.sims_done += 1
                self.games_done += res.games or 0
                # Reuse the wins_a/wins_b summary counters as test-wins /
                # test-losses so the live summary stays meaningful; the JSONL
                # row below is the authoritative per-deck record.
                self.wins_a += res.wins or 0
                self.wins_b += res.losses or 0
            else:
                self.sims_failed += 1
            if action != "write":
                # Breaker open past the per-episode row cap: counters
                # above stay accurate, but the failure row itself is
                # suppressed at source.
                if action == "summary":
                    self._write_storm_summary_locked()
                return
            name = test.name
            role = "v2" if " v2 " in name else "base"
            pair_base = name.replace(" v2 ", " ") if role == "v2" else name
            line = json.dumps({
                "ts": _now(),
                "host": self.args.label,
                "mode": "gauntlet",
                "test_deck": name,
                "role": role,            # base | v2
                "pair_base": pair_base,  # join key: base name for both halves
                "gauntlet": GAUNTLET,
                "games": getattr(res, "games", None),
                "wins": getattr(res, "wins", None),
                "losses": getattr(res, "losses", None),
                "draws": getattr(res, "draws", None),
                "status": getattr(res, "status", "error"),
                "duration_sec": getattr(res, "duration_sec", None),
                "error": err or getattr(res, "error", None),
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
                "config": {"mode": self.mode,
                           "hours": self.args.hours,
                           "phase1_games": self.args.games,
                           "phase2_games": self.args.phase2_games,
                           "phase2_after_rows": self.args.phase2_after,
                           "min": self.args.min, "max": self.max},
                "phase": self.phase,
                "current_games_per_sim": self.current_games,
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
                "storm": {
                    "total_failures": self.breaker.total_failures,
                    "suppressed_rows": self.breaker.suppressed_rows,
                    "breaker_open": self.breaker.is_open,
                    "breaker_opens": self.breaker.open_count,
                    "breaker_open_hours": round(self.breaker.open_time() / 3600, 3),
                },
            }
        self.args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # --- run --------------------------------------------------------------
    def run(self):
        # Deferred so importing this module (e.g. the test suite exercising
        # _record_gauntlet) doesn't require psutil — it's only needed by the
        # CPU-autoscaling control loop below, and only on a soak host.
        import psutil
        if self.mode == "gauntlet":
            units = f"{len(self.test_decks)} test decks vs {len(GAUNTLET)}-deck gauntlet"
        else:
            units = f"{len(self.pairs)} pairs"
        print(f"[soak] start ({self.mode}): {self.args.start} runners "
              f"(min {self.args.min}, max {self.max}), "
              f"{self.args.games} games/sim, {units}, "
              f"budget {self.args.hours}h", flush=True)
        for _ in range(min(self.args.start, self.max)):
            self._spawn()

        last_summary = 0.0
        while time.time() < self.deadline:
            cpu = psutil.cpu_percent(interval=2.0)
            with self.lock:
                self.last_cpu = cpu
                active = self.active_count()
            # Phase 2: once enough phase-1 rows are banked, switch new sims
            # to the high-confidence game count. In-flight phase-1 sims
            # finish as-is; subsequent sims pick up self.current_games.
            if self.phase == 1 and self.sims_done >= self.args.phase2_after:
                with self.lock:
                    self.current_games = self.args.phase2_games
                    self.phase = 2
                print(f"[soak] PHASE 2: {self.sims_done} rows banked -> "
                      f"switching to {self.args.phase2_games} games/sim for "
                      f"high-confidence verdicts", flush=True)
            # Autoscale toward the target band.
            if cpu < self.args.cpu_low and active < self.max and self.free_profiles:
                with self.lock:
                    self._spawn()
                print(f"[soak] cpu {cpu:.0f}% < {self.args.cpu_low} -> +1 runner "
                      f"({active + 1})", flush=True)
            elif cpu > self.args.cpu_high and active > self.args.min:
                with self.lock:
                    self._retire_one()
                print(f"[soak] cpu {cpu:.0f}% > {self.args.cpu_high} -> -1 runner "
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
        b = self.breaker
        print(f"[soak] storm: {b.total_failures} failures total, "
              f"{b.suppressed_rows} rows suppressed, breaker open "
              f"{b.open_time()/3600:.2f}h across {b.open_count} episode(s)",
              flush=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="soak_pool")
    p.add_argument("--mode", choices=["ab", "gauntlet"], default="ab",
                   help="ab (default): legacy v1-vs-v2 in ONE pod with random "
                        "fillers (the two decks race each other; noisy field). "
                        "gauntlet: each test deck vs a FIXED 3-deck gauntlet, "
                        "rotating all 4 seats — isolates the deck change so a "
                        "v1-vs-v2 win-rate delta is attributable to the edit "
                        "(4-player, baseline 25%%).")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--min", type=int, default=4)
    p.add_argument("--max", type=int, default=6)
    p.add_argument("--start", type=int, default=6)
    p.add_argument("--games", type=int, default=5,
                   help="Phase-1 games/sim (fast, banks the FP-002 row gate).")
    p.add_argument("--phase2-games", type=int, default=40,
                   help="Phase-2 games/sim (high-confidence verdict pass).")
    p.add_argument("--phase2-after", type=int, default=200,
                   help="Switch to phase-2 game count after this many completed sims.")
    p.add_argument("--cpu-low", type=float, default=78.0, help="Add a runner below this CPU%%.")
    p.add_argument("--cpu-high", type=float, default=92.0, help="Retire a runner above this CPU%%.")
    p.add_argument("--control-interval", type=float, default=45.0)
    p.add_argument("--timeout", type=int, default=360,
                   help="Per-game Forge timeout in seconds (default 360). "
                        "Generous so the occasional long Commander game "
                        "isn't killed under lane contention.")
    p.add_argument("--append", action="store_true",
                   help="Append to the output JSONL instead of truncating on "
                        "start — preserves prior rows across a restart (e.g. "
                        "when switching game count to chase a row-count gate).")
    p.add_argument("--label", default=socket.gethostname(),
                   help="Provenance tag written as 'host' on every row "
                        "(default: this machine's hostname). Lets merge_soak "
                        "keep machines separate while summing the total.")
    # Default to the running user's home dir (portable across machines,
    # and usually inside the Claude Code session folder so the in-app
    # viewer can open it). Override with --out / --summary.
    p.add_argument("--out", type=Path, default=Path.home() / "soak_throughput.jsonl")
    p.add_argument("--summary", type=Path, default=Path.home() / "soak_summary.json")
    args = p.parse_args(argv)
    Soak(args).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
