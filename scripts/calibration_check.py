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


def _ensure_control(good_path: Path) -> Path:
    """Build a do-nothing control matching the good deck's bracket."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_control_decks import make_do_nothing
    b = _bracket_from_filename(good_path.name) or 3
    out = DECK_DIR / f"[CONTROL] do-nothing [B{b}].dck"
    if not out.exists():
        out.write_text(
            make_do_nothing(good_path.read_text(encoding="utf-8"),
                            f"CONTROL do-nothing [B{b}]"),
            encoding="utf-8")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="calibration_check")
    ap.add_argument("--good", default=None, help="Known-good .dck (deck A).")
    ap.add_argument("--control", default=None, help="Known-bad .dck (deck B). "
                    "Omit to auto-build a do-nothing control.")
    ap.add_argument("--games", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=360)
    ap.add_argument("--max-control-wins", type=int, default=1)
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
    control = Path(args.control) if args.control else _ensure_control(good)

    br = _bracket_from_filename(good.name) or 3
    fillers = _pick_filler_decks(DECK_DIR, exclude_paths=[good, control],
                                 count=2, target_bracket=br, rng=random.Random(1))
    if len(fillers) < 2:
        raise SystemExit("need 2 filler decks for a 4-player pod")

    print(f"calibration: GOOD={good.name}  vs  CONTROL={control.name}")
    print(f"  {args.games} games, fillers={fillers}", flush=True)
    res = run_ab_simulation(deck_a_path=good, deck_b_path=control,
                            games=args.games, fillers=fillers,
                            timeout_per_game=args.timeout)
    print(f"  status={res.status} good(a)={res.wins_a} control(b)={res.wins_b} "
          f"games={res.games} err={res.error}")

    if res.status != "done":
        print("RESULT: INCONCLUSIVE (sim did not complete)")
        return 2
    passed = (res.wins_a > res.wins_b) and (res.wins_b <= args.max_control_wins)
    if passed:
        print(f"RESULT: PASS — the harness clearly resolves the broken deck "
              f"(good {res.wins_a} > control {res.wins_b}).")
        return 0
    print(f"RESULT: FAIL — the do-nothing control was NOT clearly beaten "
          f"(good {res.wins_a}, control {res.wins_b}). The measurement is "
          f"suspect (scoring bug, or fillers dominating).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
