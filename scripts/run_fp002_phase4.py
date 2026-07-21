"""FP-002 Phase-4 runner -- analyze the gauntlet soak result.

Invokes ``scripts/margin_analysis.py --mode gauntlet --min-games 40`` over
the shared soak inbox (which already holds BOTH box1's ``Llama_gauntlet.jsonl``
and box2's ``box2b_gauntlet.jsonl`` -- no manual merge needed), saves the
output to ``docs/fp002-phase4-results-<timestamp>.txt`` for the record, and
prints the summary so a watcher / morning brief can pick it up.

Run this once the box1 + box2 gauntlet soaks complete (or at any interim
point if you want a snapshot).

Usage:
  python scripts/run_fp002_phase4.py
  python scripts/run_fp002_phase4.py --min-games 40 --inbox <path>
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_fp002_phase4")
    ap.add_argument("--min-games", type=int, default=40,
                    help="exclude pairs with fewer than N decisive games per side (default 40)")
    ap.add_argument("--inbox", default=r"C:\Users\pilot\soak_inbox",
                    help=r"shared soak inbox (default C:\Users\pilot\soak_inbox)")
    args = ap.parse_args(argv)

    cmd = [
        sys.executable, str(REPO / "scripts" / "margin_analysis.py"),
        "--mode", "gauntlet",
        "--min-games", str(args.min_games),
        "--inbox", args.inbox,
    ]
    print("[fp002-phase4] " + " ".join(cmd), flush=True)
    # errors="replace" matters alongside encoding="utf-8": the default is
    # STRICT, so a single mojibake byte in the child's output (deck names
    # with emoji/non-Latin characters reach margin_analysis output) would
    # raise UnicodeDecodeError inside subprocess.run and lose the whole run.
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")

    ts = _dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out_path = REPO / "docs" / f"fp002-phase4-results-{ts}.txt"
    body = (res.stdout or "") + (res.stderr or "")
    out_path.write_text(body, encoding="utf-8")

    sys.stdout.write(res.stdout or "")
    if res.stderr:
        sys.stderr.write(res.stderr)
    print(f"\n[fp002-phase4] saved -> {out_path}  (rc={res.returncode})", flush=True)
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
