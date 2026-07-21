"""Calibration canary: prove the A/B harness detects an obvious problem.

Runs a known-good deck against a known-bad control (a do-nothing
99-Wastes deck — see make_control_decks.py) and checks the good deck
clearly wins. This is a sanity check on the *measurement*: base-vs-v2
sims are noise-dominated, so if you ever doubt a verdict, run this — a
PASS means the pipeline can resolve a clear difference; a FAIL means the
harness/scoring is broken (or fillers are dominating), independent of any
deck-quality question.

PASS criteria (default): the good deck wins strictly more games than the
control AND the control wins at most ``--max-control-wins`` (default 1)
of ``--games`` — i.e. the obviously-broken deck is clearly losing.

Usage:
  python scripts/calibration_check.py --good "<good.dck>" --games 6
  python scripts/calibration_check.py            # auto-pick a good deck + build a control
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from commander_builder.forge_runner import VENDOR_FORGE, run_ab_simulation
from commander_builder._proposer_sim import _pick_filler_decks
from commander_builder.web._helpers import _bracket_from_filename

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"


def _build_controls(good_path: Path, n: int) -> list[Path]:
    """Build ``n`` distinct do-nothing controls (different commanders, so
    Forge accepts them as separate decks) matching the good deck's bracket.
    These fill the whole rest of the pod so the good deck faces ONLY broken
    opponents and should win ~every game."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_control_decks import make_do_nothing
    b = _bracket_from_filename(good_path.name) or 3
    # Borrow commanders from distinct [USER] decks for variety/legality.
    srcs = [p for p in sorted(DECK_DIR.glob("[[]USER[]]*.dck"))
            if " v2 " not in p.name and "-detune" not in p.name
            and "SPDET" not in p.name and p != good_path]
    out: list[Path] = []
    for i in range(n):
        src = srcs[i % len(srcs)] if srcs else good_path
        p = DECK_DIR / f"[CONTROL] do-nothing calib{i} [B{b}].dck"
        p.write_text(make_do_nothing(src.read_text(encoding="utf-8"),
                                     f"CONTROL do-nothing calib{i} [B{b}]"),
                     encoding="utf-8")
        out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calibration_check")
    ap.add_argument("--good", default=None, help="Known-good .dck (deck A).")
    ap.add_argument("--games", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=360)
    args = ap.parse_args(argv)

    if args.good:
        good = Path(args.good)
    else:
        cand = [p for p in sorted(DECK_DIR.glob("[[]USER[]]*.dck"))
                if " v2 " not in p.name and "-detune" not in p.name
                and "SPDET" not in p.name]
        if not cand:
            raise SystemExit("no [USER] decks to use as the good deck")
        good = cand[0]

    # Pod = good deck + 3 do-nothing controls. The only functional deck is
    # ``good``, so it should win ~every game and the broken decks win 0 —
    # an unambiguous signal that isolates harness correctness from filler
    # strength (the bug in the first version of this check).
    controls = _build_controls(good, 3)
    control_b, fillers = controls[0], [c.name for c in controls[1:]]

    print(f"calibration: GOOD={good.name}  vs a pod of 3 do-nothing controls")
    print(f"  control(deck B)={control_b.name}  fillers={fillers}")
    print(f"  {args.games} games", flush=True)
    res = run_ab_simulation(deck_a_path=good, deck_b_path=control_b,
                            games=args.games, fillers=fillers,
                            timeout_per_game=args.timeout)
    print(f"  status={res.status} good(a)={res.wins_a} control(b)={res.wins_b} "
          f"games={res.games} err={res.error}")

    if res.status != "done":
        print("RESULT: INCONCLUSIVE (sim did not complete)")
        return 2
    # The broken deck must win 0; the good deck (only functional one) must
    # win at least one. If a do-nothing deck ever scores a win, the scoring
    # is broken.
    passed = (res.wins_b == 0) and (res.wins_a >= 1)
    if passed:
        print(f"RESULT: PASS — broken decks won 0; the good deck won "
              f"{res.wins_a}/{res.games}. The harness clearly resolves an "
              f"obviously-broken deck.")
        return 0
    print(f"RESULT: FAIL — expected broken deck=0 wins, good deck>=1. Got "
          f"good={res.wins_a}, control={res.wins_b}. Measurement suspect "
          f"(scoring bug) — or the good deck can't close out a do-nothing "
          f"pod within the turn limit (try --games higher / a faster deck).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
