#!/usr/bin/env python
"""One-off feasibility spike for AGENT_BACKLOG #016 (concurrent Forge sims).

Question: can two ``forge sim`` JVMs run in parallel from the SAME cwd
(vendor/forge/) without deadlocking, file-locking, or corrupting each
other's logs? If yes, the implementation is simple (just queue sims to
a ThreadPoolExecutor). If no, we need per-worker cwd isolation with
copied/symlinked profile properties.

The hard requirements pinned in forge_runner.py:7-12 say cwd MUST be
the install dir or Forge crashes during init. So naive isolation
(arbitrary cwd) is off the table; we're asking whether the install
dir is itself parallel-safe.

Spike protocol:
1. Pick 8 small B2/B3 decks (lowest-power-bracket so games end faster).
2. Spawn two ``java -jar forge sim -f commander -n 1 -d <4 decks>``
   subprocesses simultaneously via Popen, both with cwd=vendor/forge/.
3. Wait up to 6 min total (each sim is typically 30-90s + ~5s JVM
   warmup; parallel wall time should be roughly max() of the two).
4. Report:
   - Both completed?
   - Both returncode 0?
   - Either produced lock-error or init-exception strings?
   - Combined forge.log smells correct (no interleaved corruption)?

Run from project root:
    python scripts/_spike_concurrent_forge.py

Side effects: spawns two JVMs that play 1 Commander game each, writes
to vendor/forge/forge.log (and forge0.log / forge1.log rolling logs).
No knowledge_log writes, no .dck file changes.

After the spike, this script is disposable — leave it in scripts/ as
documentation for the next time someone questions whether the
isolation strategy is necessary.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FORGE_DIR = REPO_ROOT / "vendor" / "forge"
DECK_DIR = FORGE_DIR / "userdata" / "decks" / "commander"


def _pick_decks(n: int = 8) -> list[str]:
    """Pick ``n`` distinct .dck filenames from the deck library, preferring
    low-bracket decks because they end faster. We don't care which decks
    win — only whether the JVMs co-exist."""
    candidates = sorted(p.name for p in DECK_DIR.glob("*.dck"))
    # Prefer B1/B2 (faster games) for the spike.
    low_bracket = [n for n in candidates if "[B2]" in n or "[B1]" in n]
    pool = low_bracket if len(low_bracket) >= n else candidates
    if len(pool) < n:
        raise SystemExit(f"need {n} decks in {DECK_DIR}, found {len(pool)}")
    return pool[:n]


def _locate_java() -> str:
    """Prefer vendor/jre/bin/java, fall back to PATH java."""
    for candidate in (
        FORGE_DIR.parent / "jre" / "bin" / "java.exe",
        FORGE_DIR.parent / "jre" / "bin" / "java",
    ):
        if candidate.exists():
            return str(candidate)
    return "java"


def _locate_jar() -> str:
    jars = sorted(FORGE_DIR.glob("forge-gui-desktop-*.jar"))
    if not jars:
        raise SystemExit(f"no forge-gui-desktop-*.jar under {FORGE_DIR}")
    return str(jars[-1])


def _build_cmd(jar: str, decks: list[str]) -> list[str]:
    return [
        _locate_java(), "-jar", jar, "sim",
        "-f", "commander", "-n", "1",
        "-d", *decks,
    ]


def main() -> int:
    decks = _pick_decks(8)
    print(f"Spike: 2 parallel sims, 4 decks each, 1 game per sim.")
    print(f"Forge dir: {FORGE_DIR}")
    print(f"Decks for sim A: {decks[:4]}")
    print(f"Decks for sim B: {decks[4:]}")
    print()

    jar = _locate_jar()
    cmd_a = _build_cmd(jar, decks[:4])
    cmd_b = _build_cmd(jar, decks[4:])
    cwd = str(FORGE_DIR)

    t0 = time.time()
    proc_a = subprocess.Popen(
        cmd_a, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    proc_b = subprocess.Popen(
        cmd_b, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    print(f"Spawned PIDs {proc_a.pid} and {proc_b.pid} at t=0...")

    TIMEOUT_SEC = 360  # 6 min wall budget
    try:
        out_a, err_a = proc_a.communicate(timeout=TIMEOUT_SEC)
        rc_a = proc_a.returncode
        t_a = time.time() - t0
    except subprocess.TimeoutExpired:
        proc_a.kill()
        out_a, err_a = proc_a.communicate()
        rc_a = None
        t_a = TIMEOUT_SEC

    remaining = max(60, TIMEOUT_SEC - int(time.time() - t0))
    try:
        out_b, err_b = proc_b.communicate(timeout=remaining)
        rc_b = proc_b.returncode
        t_b = time.time() - t0
    except subprocess.TimeoutExpired:
        proc_b.kill()
        out_b, err_b = proc_b.communicate()
        rc_b = None
        t_b = remaining

    print("\n=== Results ===")
    print(f"Sim A: rc={rc_a} wall={t_a:.1f}s stdout={len(out_a)}b stderr={len(err_a)}b")
    print(f"Sim B: rc={rc_b} wall={t_b:.1f}s stdout={len(out_b)}b stderr={len(err_b)}b")

    SUSPECT = (
        "ExceptionInInitializerError",
        "FileNotFoundException",
        "FileLockInterruptionException",
        "OverlappingFileLockException",
        "Could not acquire lock",
        "java.nio.file.FileSystemException",
        "java.io.IOException: Stream closed",
        "java.lang.IllegalStateException",
    )
    flagged_a = [s for s in SUSPECT if s in out_a or s in err_a]
    flagged_b = [s for s in SUSPECT if s in out_b or s in err_b]

    print(f"\nSim A flagged errors: {flagged_a or '(none)'}")
    print(f"Sim B flagged errors: {flagged_b or '(none)'}")

    print("\n=== Verdict ===")
    if rc_a == 0 and rc_b == 0 and not flagged_a and not flagged_b:
        print("PASS: both JVMs co-existed in the same cwd cleanly.")
        print("Implication: #016 can use a ThreadPoolExecutor with NO")
        print("            cwd isolation. Simplest possible design works.")
        return 0
    if rc_a == 0 and rc_b == 0:
        print("PARTIAL: both rc=0 but suspect strings appeared in logs.")
        print("        Review flagged errors; isolation might still be needed.")
        return 1
    print("FAIL: at least one JVM crashed or timed out.")
    print("     Implication: cwd-isolated profile dirs are required.")
    print("\n--- Sim A stderr tail ---")
    print(err_a[-2000:] if err_a else "(empty)")
    print("\n--- Sim B stderr tail ---")
    print(err_b[-2000:] if err_b else "(empty)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
