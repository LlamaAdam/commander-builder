"""Track 2 prep — forge_py.combat correlation harness.

Goal: every time the user runs a real Forge A/B sim, also run the same
matchup through forge_py.combat (the lightweight Python head-to-head
simulator) and log both verdicts. Once we have enough paired samples
we can compute Pearson r and decide whether forge_py is good enough
to be a fast pre-filter (or eventual replacement). Until then this
module just collects data — no behavior change to the propose-swap
flow.

Why not a fast path yet?
- forge_py.combat is shallow (single-attacker, no flying/reach/first-strike).
  Storm/control matchups will mis-model.
- Correlation must be empirically validated per-archetype before we
  trust forge_py to gate Forge runs.

What this module does:
- ``run_forge_py_ab(old_path, new_path, games_per_pod, mode, seed)`` —
  pure forge_py simulation, no Forge involvement. Returns
  ``(old_wins, new_wins, draws)``.
- ``log_correlation_row(forge_result, py_result, ctx)`` — appends a CSV
  row to a per-machine correlation log. Survives across sessions so
  the row count grows over weeks/months of normal use.

This is the ONLY place commander_builder imports from forge_py. Kept
isolated so a missing forge_py install never breaks the main flow.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class ForgePyABResult:
    """Mirrors compare_versions.ComparisonReport's headline stats. The
    correlation analysis only cares about wins/draws/total — not the
    full per-game telemetry."""
    old_wins: int
    new_wins: int
    draws: int
    total_games: int
    duration_sec: float
    error: Optional[str] = None  # set when the harness couldn't run


def _maybe_import_forge_py():
    """Import forge_py lazily so a missing install doesn't break
    commander_builder. Returns ``None`` when unavailable."""
    try:
        from forge_py.dck_parser import parse_dck
        from forge_py.card_tagger import tag_cards
        from forge_py.combat import run_multiplayer_game
        return parse_dck, tag_cards, run_multiplayer_game
    except ImportError:
        return None


def run_forge_py_ab(
    old_path: Path,
    new_path: Path,
    games_per_pod: int,
    mode: str = "1v1",
    seed_base: int = 0,
) -> ForgePyABResult:
    """Simulate the same A/B matchup as ``compare_versions.compare`` but
    using forge_py instead of real Forge.

    1v1 mode: 2 decks (old, new). Pod mode: 4 decks (old, new + 2
    fillers chosen by the caller-fed random — for Track 2's first
    pass we just rerun the 2 decks with random seats).

    Returns counts; the caller pairs them with the matching Forge
    result for correlation logging.
    """
    started = datetime.now(timezone.utc)
    deps = _maybe_import_forge_py()
    if deps is None:
        return ForgePyABResult(
            old_wins=0, new_wins=0, draws=0, total_games=0,
            duration_sec=0.0,
            error="forge_py not importable",
        )
    parse_dck, tag_cards, run_multiplayer_game = deps

    try:
        old_deck = parse_dck(old_path)
        new_deck = parse_dck(new_path)
    except Exception as exc:  # noqa: BLE001
        return ForgePyABResult(
            old_wins=0, new_wins=0, draws=0, total_games=0,
            duration_sec=0.0,
            error=f"parse_dck failed: {type(exc).__name__}: {exc}",
        )

    # Tag every unique card across both decks once (cache shared).
    all_names = sorted({
        line.name
        for line in old_deck.all_card_lines() + new_deck.all_card_lines()
    })
    try:
        tagged = tag_cards(all_names, cache=True)
    except Exception as exc:  # noqa: BLE001
        return ForgePyABResult(
            old_wins=0, new_wins=0, draws=0, total_games=0,
            duration_sec=0.0,
            error=f"tag_cards failed: {type(exc).__name__}: {exc}",
        )

    # Build the (name, deck, tagged) tuples forge_py.combat wants. Pod
    # mode uses a single trivial filler (just the new deck again at a
    # different seat) — the harness's first pass doesn't pretend to
    # match Forge's real-meta filler selection. Track 2 will refine.
    decks_arg = [
        ("old", old_deck, tagged),
        ("new", new_deck, tagged),
    ]
    if mode == "pod":
        decks_arg.append(("filler_a", old_deck, tagged))
        decks_arg.append(("filler_b", new_deck, tagged))

    old_wins = new_wins = draws = 0
    rng = random.Random(seed_base)
    for game_i in range(games_per_pod):
        try:
            result = run_multiplayer_game(
                decks_arg,
                seed=seed_base + game_i,
                fixed_seat_order=False,
            )
        except Exception as exc:  # noqa: BLE001
            return ForgePyABResult(
                old_wins=old_wins, new_wins=new_wins, draws=draws,
                total_games=old_wins + new_wins + draws,
                duration_sec=(datetime.now(timezone.utc) - started).total_seconds(),
                error=f"run_multiplayer_game failed: {type(exc).__name__}: {exc}",
            )
        if result.draw or result.winner is None:
            draws += 1
        elif result.winner == "old":
            old_wins += 1
        elif result.winner == "new":
            new_wins += 1
        else:
            # Filler won — counts as a draw for old-vs-new analysis.
            draws += 1

    duration = (datetime.now(timezone.utc) - started).total_seconds()
    return ForgePyABResult(
        old_wins=old_wins,
        new_wins=new_wins,
        draws=draws,
        total_games=old_wins + new_wins + draws,
        duration_sec=duration,
    )


def log_correlation_row(
    log_path: Path,
    *,
    old_deck: str,
    new_deck: str,
    bracket: int,
    mode: str,
    games_per_pod: int,
    forge_old_wins: int,
    forge_new_wins: int,
    forge_draws: int,
    forge_duration_sec: float,
    py_old_wins: int,
    py_new_wins: int,
    py_draws: int,
    py_duration_sec: float,
    py_error: Optional[str] = None,
) -> None:
    """Append one paired (Forge, forge_py) row to the correlation CSV.

    Creates the file with a header on first write. Append-only — never
    rewrites historical rows. The CSV is the corpus a future analysis
    script will read to compute Pearson r.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with log_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow([
                "ts", "old_deck", "new_deck", "bracket", "mode", "games_per_pod",
                "forge_old_wins", "forge_new_wins", "forge_draws",
                "forge_duration_sec",
                "py_old_wins", "py_new_wins", "py_draws", "py_duration_sec",
                "py_error",
            ])
        w.writerow([
            datetime.now(timezone.utc).isoformat(),
            old_deck, new_deck, bracket, mode, games_per_pod,
            forge_old_wins, forge_new_wins, forge_draws,
            round(forge_duration_sec, 2),
            py_old_wins, py_new_wins, py_draws,
            round(py_duration_sec, 2),
            py_error or "",
        ])


DEFAULT_CORRELATION_LOG = Path(
    Path(__file__).resolve().parent.parent.parent
    / "vendor" / "_forge_py_correlation.csv"
)


def pearson_r(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation coefficient of two equal-length series.

    Returns a value in [-1, 1], or ``None`` when it's undefined: fewer
    than 2 paired points, mismatched lengths, or zero variance in either
    series (a flat series has no correlation to measure). Pure + numpy-
    free so it's cheap to unit-test and carries no new dependency.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    cov = sum(a * b for a, b in zip(dx, dy))
    var_x = sum(a * a for a in dx)
    var_y = sum(b * b for b in dy)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def correlation_summary(log_path: Path) -> dict:
    """Read all rows and compute agreement + correlation stats.

    Returns ``{rows, agree, disagree, errors, agreement_rate,
    pearson_r, pearson_n}``:

      - ``agreement_rate`` — how often forge_py's *winner* (old vs new)
        matches Forge's. The headline directional stat.
      - ``pearson_r`` — Pearson r between the two engines' per-row
        *win margins* (``new_wins - old_wins``) across valid rows, or
        ``None`` when undefined (<2 rows or a flat series). This is the
        statistic the 2026-04-28 "flip default only when r ≥ 0.90 across
        ≥30 paired rows" rule is written against. ``pearson_n`` is the
        number of rows it was computed over.
    """
    if not log_path.exists():
        return {"rows": 0, "agree": 0, "disagree": 0,
                "agreement_rate": 0.0, "errors": 0,
                "pearson_r": None, "pearson_n": 0}
    rows = 0
    agree = 0
    errors = 0
    forge_margins: list[float] = []
    py_margins: list[float] = []
    with log_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows += 1
            if r.get("py_error"):
                errors += 1
                continue
            try:
                f_old = int(r["forge_old_wins"])
                f_new = int(r["forge_new_wins"])
                p_old = int(r["py_old_wins"])
                p_new = int(r["py_new_wins"])
            except (KeyError, ValueError):
                errors += 1
                continue
            forge_winner = (
                "old" if f_old > f_new
                else "new" if f_new > f_old
                else "tie"
            )
            py_winner = (
                "old" if p_old > p_new
                else "new" if p_new > p_old
                else "tie"
            )
            if forge_winner == py_winner:
                agree += 1
            # Per-row signal for the Pearson correlation: how decisively
            # did each engine favor the NEW deck over the old one?
            forge_margins.append(f_new - f_old)
            py_margins.append(p_new - p_old)
    valid = max(rows - errors, 0)
    rate = agree / valid if valid else 0.0
    r_value = pearson_r(forge_margins, py_margins)
    return {
        "rows": rows,
        "agree": agree,
        "disagree": valid - agree,
        "errors": errors,
        "agreement_rate": round(rate, 3),
        "pearson_r": round(r_value, 3) if r_value is not None else None,
        "pearson_n": len(forge_margins),
    }


def _cli_main(argv: Optional[list[str]] = None) -> int:
    """``python -m commander_builder.forge_py_correlation`` — print
    summary stats from the correlation log.

    Default log path tracks the web app's persistence location
    (``vendor/_forge_py_correlation.csv`` next to the Forge install).
    Pass ``--log <path>`` to point at a different file.
    """
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="commander-builder-correlate")
    ap.add_argument(
        "--log", type=Path, default=DEFAULT_CORRELATION_LOG,
        help="Path to correlation CSV (default: vendor/_forge_py_correlation.csv)",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Output JSON instead of human-readable text.",
    )
    args = ap.parse_args(argv)
    summary = correlation_summary(args.log)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"Correlation log: {args.log}")
    print(f"Total rows:      {summary['rows']}")
    print(f"Agreement rate:  {summary['agreement_rate']:.1%} "
          f"({summary['agree']} / {summary['agree'] + summary['disagree']} valid pairs)")
    print(f"Errors:          {summary['errors']}")
    if summary["pearson_r"] is not None:
        gate = "✓ ≥ 0.90" if summary["pearson_r"] >= 0.90 else "below 0.90 gate"
        print(f"Pearson r:       {summary['pearson_r']:+.3f} "
              f"(n={summary['pearson_n']}, {gate})")
    else:
        print(f"Pearson r:       n/a (need ≥2 valid rows with variance)")
    if summary["rows"] < 30:
        print()
        print("(Need ~30+ rows before per-archetype Pearson r is meaningful;")
        print(f" turn correlation on with COMMANDER_BUILDER_CORRELATE_FORGE_PY=1")
        print(f" and run propose-swap as usual to grow the sample.)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli_main())
