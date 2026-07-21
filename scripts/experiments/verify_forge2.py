"""FP-003 verification: confirm a Forge sim runs from an isolated profile dir.

Run one A/B sim with a ForgeRunner pointed at a sibling profile (default
vendor/forge2) to prove Forge resolves res/ + userdata from the given cwd,
not from the jar's location. Prints the verdict + which forge.log was written.

Usage:
  python scripts/verify_forge2.py [profile_dir] [games]
"""
from __future__ import annotations

import sys
from pathlib import Path

from commander_builder.forge_runner import (
    ForgeRunner,
    VENDOR_FORGE,
    VENDOR_JRE,
    run_ab_simulation,
)

REPO = Path(__file__).resolve().parents[2]


def _runner_for(profile_dir: Path) -> ForgeRunner:
    base = ForgeRunner.locate()  # resolves java + jar (absolute paths)
    return ForgeRunner(java_path=base.java_path, forge_jar=base.forge_jar,
                       forge_dir=profile_dir)


def main() -> int:
    profile = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "vendor" / "forge2"
    games = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    deck_dir = profile / "userdata" / "decks" / "commander"

    decks = sorted(p for p in deck_dir.glob("*.dck") if "[USER]" not in p.name)
    if len(decks) < 4:
        print(f"need >=4 decks in {deck_dir}, found {len(decks)}")
        return 2
    deck_a, deck_b, f1, f2 = decks[0], decks[1], decks[2], decks[3]

    runner = _runner_for(profile)
    print(f"profile cwd : {profile}")
    print(f"java        : {runner.java_path}")
    print(f"jar         : {runner.forge_jar}")
    print(f"deck A      : {deck_a.name}")
    print(f"deck B      : {deck_b.name}")
    print(f"fillers     : {f1.name} | {f2.name}")
    print(f"games       : {games}")
    print("running...")

    res = run_ab_simulation(
        deck_a, deck_b, games=games, runner=runner,
        fillers=[f1.name, f2.name],
    )
    log = profile / "forge.log"
    print(f"\nstatus      : {res.status}")
    print(f"wins_a/b    : {res.wins_a}/{res.wins_b}  (games={res.games})")
    print(f"duration    : {res.duration_sec}s")
    if res.error:
        print(f"error       : {res.error}")
    print(f"forge.log   : {'exists' if log.exists() else 'MISSING'} "
          f"({log.stat().st_size if log.exists() else 0} bytes) at {log}")
    return 0 if res.status == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
