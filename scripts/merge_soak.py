"""Merge soak JSONL outputs (from one or more machines) and report; with
``--to-knowledge-log``, fold the completed sims into knowledge_log
single-threaded so they count toward the FP-002 dataset.

Each soak row is an independent A/B sim, so merging is just concatenation
+ dedup of identical lines (guards against passing the same file twice).

Usage:
  python scripts/merge_soak.py m1_soak.jsonl m2_soak.jsonl
  python scripts/merge_soak.py "C:/Users/*/soak_throughput*.jsonl" --to-knowledge-log
"""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path


def load_rows(paths: list[str]) -> list[dict]:
    """Parse JSONL files; dedup byte-identical lines (same file twice)."""
    seen: set[str] = set()
    rows: list[dict] = []
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def fold_to_knowledge_log(rows: list[dict], db_path, margin: int) -> int:
    """Write each completed sim as a knowledge_log iteration.

    deck_b is the curated/v2 deck under evaluation (deck_a is the baseline);
    verdict is margin-thresholded on (wins_b - wins_a). Tagged
    audit_version='soak-ab' so these rows are identifiable.
    """
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
        deck_id = Path(deck_b).stem
        it = Iteration(
            deck_id=deck_id,
            deck_name=deck_id,
            bracket=_bracket_from_filename(deck_b) or 3,
            audit_version="soak-ab",
            audit_manifest={"deck_a": r.get("deck_a"), "deck_b": deck_b,
                            "source": "soak", "games": g},
            sim_report={"wins_a": wa, "wins_b": wb, "games": g,
                        "duration_sec": r.get("duration_sec")},
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
    ap.add_argument("inputs", nargs="+", help="JSONL files or globs.")
    ap.add_argument("--out", type=Path, default=Path("combined_soak.jsonl"),
                    help="Combined JSONL output (default combined_soak.jsonl).")
    ap.add_argument("--to-knowledge-log", action="store_true",
                    help="Also fold completed sims into knowledge_log.")
    ap.add_argument("--db-path", default=None, help="Override knowledge_log path.")
    ap.add_argument("--margin", type=int, default=1,
                    help="kept/reverted margin threshold (default 1).")
    args = ap.parse_args(argv)

    paths: list[str] = []
    for pat in args.inputs:
        paths.extend(glob.glob(pat))
    if not paths:
        raise SystemExit("no input files matched")

    rows = load_rows(paths)
    args.out.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    done = [r for r in rows if r.get("status") == "done"]
    failed = sum(1 for r in rows if r.get("status") != "done")
    games = sum(r.get("games") or 0 for r in done)
    print(f"merged {len(paths)} file(s): {len(rows)} rows "
          f"({len(done)} done, {failed} failed, {games} games) -> {args.out}")

    if args.to_knowledge_log:
        n = fold_to_knowledge_log(rows, args.db_path, args.margin)
        print(f"wrote {n} iterations to knowledge_log "
              f"(audit_version='soak-ab')")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
