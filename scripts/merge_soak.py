"""Merge soak JSONL outputs from one or more machines — separate *and* together.

Each machine writes its own ``soak_throughput.jsonl`` (so the sets are
inherently tracked separately, per file). This tool combines them into one
dataset while **preserving provenance**: every row is tagged with a
``source`` label, and the run prints a per-source breakdown plus the
grand total — so you see machine-1 vs machine-2 counts AND the sum.

Label each input explicitly with ``LABEL=PATH`` (recommended), or pass a
bare path (source = the file's stem). Rows are independent samples, so
combining is concatenation; byte-identical lines are de-duplicated
(guards against passing the same file twice).

Usage:
  python scripts/merge_soak.py box1=C:/Users/pilot/soak_throughput.jsonl box2=D:/in/box2.jsonl
  python scripts/merge_soak.py *.jsonl --to-knowledge-log
"""
from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path


def _parse_input(arg: str) -> tuple[str | None, str]:
    """``LABEL=PATH`` -> (LABEL, PATH); bare ``PATH`` -> (None, PATH).

    Splits on the FIRST ``=`` only. Windows drive paths use ``:`` (e.g.
    ``C:/Users/...``), not ``=``, so this never mis-splits a path."""
    if "=" in arg:
        label, _, pat = arg.partition("=")
        if label and pat:
            return label, pat
    return None, arg


def load_tagged(inputs: list[str]) -> list[dict]:
    """Load rows from all inputs, tagging each with a ``source`` label
    (explicit LABEL > file stem). Keeps any embedded ``host`` the soak
    wrote. De-dups byte-identical lines across all inputs."""
    seen: set[str] = set()
    rows: list[dict] = []
    for arg in inputs:
        label, pat = _parse_input(arg)
        matched = glob.glob(pat)
        if not matched:
            print(f"WARNING: no files matched {pat!r}")
        for path in matched:
            src = label or Path(path).stem
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                print(f"WARNING: cannot read {path}: {exc}")
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line in seen:
                    continue
                seen.add(line)
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r.setdefault("host", None)   # soak_pool may embed this
                r["source"] = src            # explicit per-file provenance
                rows.append(r)
    return rows


def report(rows: list[dict]) -> dict:
    """Print per-source rows/done/games + the combined TOTAL."""
    agg: dict[str, dict] = defaultdict(lambda: {"rows": 0, "done": 0, "games": 0})
    for r in rows:
        a = agg[r["source"]]
        a["rows"] += 1
        if r.get("status") == "done":
            a["done"] += 1
            a["games"] += r.get("games") or 0
    print(f"{'source':<28}{'rows':>7}{'done':>7}{'games':>9}")
    print("-" * 51)
    tot = {"rows": 0, "done": 0, "games": 0}
    for src in sorted(agg):
        a = agg[src]
        print(f"{src:<28}{a['rows']:>7}{a['done']:>7}{a['games']:>9}")
        for k in tot:
            tot[k] += a[k]
    print("-" * 51)
    print(f"{'TOTAL':<28}{tot['rows']:>7}{tot['done']:>7}{tot['games']:>9}")
    return tot


def fold_to_knowledge_log(rows: list[dict], db_path, margin: int) -> int:
    """Write completed sims to knowledge_log. ``source``/``host`` are
    carried into each row's audit_manifest so the machines stay
    distinguishable while all counting toward one dataset."""
    from commander_builder.knowledge_log import (
        DEFAULT_DB_PATH, Iteration, record_iteration,
    )
    from commander_builder.web._helpers import _bracket_from_filename

    db = Path(db_path) if db_path else DEFAULT_DB_PATH
    n = 0
    for r in rows:
        if r.get("status") != "done":
            continue
        wa = r.get("wins_a") or 0
        wb = r.get("wins_b") or 0
        g = r.get("games") or 0
        delta = wb - wa
        verdict = ("kept" if delta >= margin
                   else "reverted" if delta <= -margin else "neutral")
        deck_b = r.get("deck_b") or "?.dck"
        it = Iteration(
            deck_id=Path(deck_b).stem,
            deck_name=Path(deck_b).stem,
            bracket=_bracket_from_filename(deck_b) or 3,
            audit_version="soak-ab",
            audit_manifest={"deck_a": r.get("deck_a"), "deck_b": deck_b,
                            "source": r.get("source"), "host": r.get("host"),
                            "games": g},
            sim_report={"wins_a": wa, "wins_b": wb, "games": g},
            verdict=verdict,
            win_rate_old=round(wa / g, 4) if g else None,
            win_rate_new=round(wb / g, 4) if g else None,
            margin=delta,
        )
        record_iteration(it, db_path=db)
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="merge_soak")
    ap.add_argument("inputs", nargs="+",
                    help="JSONL paths or LABEL=PATH (globs ok).")
    ap.add_argument("--out", type=Path, default=Path("combined_soak.jsonl"))
    ap.add_argument("--to-knowledge-log", action="store_true",
                    help="Also fold completed sims into knowledge_log.")
    ap.add_argument("--db-path", default=None)
    ap.add_argument("--margin", type=int, default=1)
    args = ap.parse_args(argv)

    rows = load_tagged(args.inputs)
    if not rows:
        raise SystemExit("no rows loaded")
    args.out.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"combined -> {args.out}\n")
    report(rows)
    if args.to_knowledge_log:
        n = fold_to_knowledge_log(rows, args.db_path, args.margin)
        print(f"\nwrote {n} iterations to knowledge_log "
              f"(audit_version='soak-ab'; source/host in manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
