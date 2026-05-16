"""Top-level status command — `commander-status`.

Three modes, dispatched from a single CLI:

  commander-status                  List all [USER] decks (one line each).
  commander-status <deck_path>      Detailed at-a-glance dashboard for one
                                    deck: bracket, iteration history,
                                    last audit, price delta, meta-test
                                    win rate, salt warnings.
  commander-status --project        Project-wide health view (original
                                    behavior): bracket counts, curated
                                    pools, recent match/compare reports,
                                    aggregate knowledge-log stats.

Each mode supports ``--json`` for machine-readable output.

The per-deck view reads only on-disk artifacts (the .dck file,
``knowledge_log.sqlite``, and ``_meta/*.json`` reports) so it returns
sub-second — there are no Scryfall round-trips. Use ``commander-doctor``
or the web dashboard for the heavier Scryfall-priced view.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .forge_runner import VENDOR_FORGE
from .knowledge_log import (
    DEFAULT_DB_PATH,
    iterations_for_deck,
    recent_iterations,
    stats_summary,
)

DECK_DIR = VENDOR_FORGE / "userdata" / "decks" / "commander"
POOL_DIR = DECK_DIR / "_pools"
MATCH_DIR = DECK_DIR / "_matches"
COMPARE_DIR = DECK_DIR / "_compare"
META_DIR = DECK_DIR / "_meta"

# Regex helpers — module-level so they compile once.
_BRACKET_SUFFIX = re.compile(r"\s*\[B([1-5])\]\.dck$")
_USER_PREFIX = re.compile(r"^\[USER\]\s*")
_MOXFIELD_META = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)
_DECK_NAME_META = re.compile(r"^Name=(.+)$", re.MULTILINE)


@dataclass
class StatusReport:
    timestamp: str
    decks_by_bracket: dict[int, dict[str, int]] = field(default_factory=dict)
    pools_on_disk: list[dict] = field(default_factory=list)
    recent_matches: list[dict] = field(default_factory=list)
    recent_compares: list[dict] = field(default_factory=list)
    knowledge_log: dict = field(default_factory=dict)
    forge_dir_present: bool = False
    deck_dir_present: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _count_decks(deck_dir: Path) -> dict[int, dict[str, int]]:
    """Count decks at each bracket with [USER] vs filler split. Buckets are
    keyed on the bracket number (1-5). Decks without a `[B<n>]` suffix are
    silently ignored — they're not part of the bracket-tagged workflow."""
    out: dict[int, dict[str, int]] = {}
    if not deck_dir.exists():
        return out
    for path in deck_dir.glob("*.dck"):
        name = path.name
        # Find " [B<n>].dck" suffix (n in 1-5 or '?' for unknown).
        bracket: Optional[int] = None
        for n in (1, 2, 3, 4, 5):
            if name.endswith(f" [B{n}].dck"):
                bracket = n
                break
        if bracket is None:
            continue
        bucket = out.setdefault(bracket, {"user": 0, "filler": 0, "total": 0})
        if name.startswith("[USER]"):
            bucket["user"] += 1
        else:
            bucket["filler"] += 1
        bucket["total"] += 1
    return out


def _list_pools(pool_dir: Path) -> list[dict]:
    """Per-pool JSON summary. Sorted by mtime descending."""
    if not pool_dir.exists():
        return []
    out: list[dict] = []
    # Skip _analysis.json files; they're paired with the pool JSON and would
    # double-count.
    for path in sorted(
        pool_dir.glob("B*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if path.name.endswith("_analysis.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "path": path.name,
            "bracket": data.get("bracket"),
            "pool_a_size": len(data.get("pool_a", [])),
            "pool_b_size": len(data.get("pool_b", [])),
            "rejected_count": len(data.get("rejected", [])),
            "modified": datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        })
    return out


def _list_recent(report_dir: Path, limit: int = 5) -> list[dict]:
    """Most-recent N JSON reports in a directory, by mtime. Used for both
    _matches/ and _compare/. Returns abbreviated rows."""
    if not report_dir.exists():
        return []
    paths = sorted(
        report_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # Both MatchupReport and ComparisonReport carry these top-level
        # fields. Defensive .get() handles either schema.
        out.append({
            "path": path.name,
            "timestamp": data.get("timestamp"),
            "bracket": data.get("bracket"),
            "total_games": data.get("total_games") or data.get("games_played"),
            "winner": data.get("winner"),
            "win_rate": data.get("win_rate"),
        })
    return out


def collect_status(
    deck_dir: Path = DECK_DIR,
    pool_dir: Path = POOL_DIR,
    match_dir: Path = MATCH_DIR,
    compare_dir: Path = COMPARE_DIR,
    db_path: Path = DEFAULT_DB_PATH,
) -> StatusReport:
    """Gather all status signals into one StatusReport. Pure read — never
    writes anything (other than knowledge_log's idempotent schema-init via
    `stats_summary`)."""
    report = StatusReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        forge_dir_present=VENDOR_FORGE.exists(),
        deck_dir_present=deck_dir.exists(),
        decks_by_bracket=_count_decks(deck_dir),
        pools_on_disk=_list_pools(pool_dir),
        recent_matches=_list_recent(match_dir),
        recent_compares=_list_recent(compare_dir),
    )

    # knowledge_log: latest 5 + aggregate stats.
    try:
        kl_stats = stats_summary(db_path=db_path)
        recent = recent_iterations(limit=5, db_path=db_path)
        report.knowledge_log = {
            "stats": kl_stats,
            "recent": [
                {
                    "id": it.id,
                    "deck_id": it.deck_id,
                    "deck_name": it.deck_name,
                    "bracket": it.bracket,
                    "verdict": it.verdict,
                    "margin": it.margin,
                    "created_at": it.created_at,
                }
                for it in recent
            ],
        }
    except Exception as exc:
        report.knowledge_log = {"error": f"{type(exc).__name__}: {exc}"}

    return report


def format_text(report: StatusReport) -> str:
    """Render a StatusReport as terminal-friendly plain text."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f" Commander Builder — status as of {report.timestamp}")
    lines.append("=" * 60)
    lines.append("")

    # Forge availability.
    forge_marker = "yes" if report.forge_dir_present else "no (live sims will fail)"
    lines.append(f"Forge install present: {forge_marker}")
    lines.append("")

    # Decks.
    lines.append("Decks on disk")
    lines.append("-" * 60)
    if report.decks_by_bracket:
        for bracket in sorted(report.decks_by_bracket.keys()):
            row = report.decks_by_bracket[bracket]
            lines.append(
                f"  B{bracket}: {row['total']:>4} total  "
                f"({row['user']} [USER] + {row['filler']} filler)"
            )
    else:
        lines.append("  (no decks found — run `commander-import --harvest <bracket>`)")
    lines.append("")

    # Pools.
    lines.append(f"Curated pools ({len(report.pools_on_disk)})")
    lines.append("-" * 60)
    if report.pools_on_disk:
        for p in report.pools_on_disk:
            lines.append(
                f"  {p['path']:>20s}  pool_a={p['pool_a_size']}  pool_b={p['pool_b_size']}  "
                f"rejected={p['rejected_count']}"
            )
    else:
        lines.append("  (no curated pools — run `commander-curate --bracket <n>`)")
    lines.append("")

    # Recent matches + compares.
    lines.append(f"Recent run_match reports ({len(report.recent_matches)})")
    lines.append("-" * 60)
    if report.recent_matches:
        for m in report.recent_matches:
            wr = f"{m['win_rate']:.1%}" if m.get("win_rate") is not None else "?"
            lines.append(f"  {m['path']:<60s} B{m.get('bracket', '?')} win_rate={wr}")
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Recent comparison reports ({len(report.recent_compares)})")
    lines.append("-" * 60)
    if report.recent_compares:
        for c in report.recent_compares:
            lines.append(
                f"  {c['path']:<60s} B{c.get('bracket', '?')} winner={c.get('winner', '?')}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    # Knowledge log.
    lines.append("Knowledge log")
    lines.append("-" * 60)
    kl = report.knowledge_log
    if "error" in kl:
        lines.append(f"  ERROR: {kl['error']}")
    else:
        s = kl.get("stats", {})
        lines.append(
            f"  total={s.get('total', 0)}  unique_decks={s.get('unique_decks', 0)}  "
            f"kept={s.get('kept', 0)}  reverted={s.get('reverted', 0)}  "
            f"neutral={s.get('neutral', 0)}  pending={s.get('pending', 0)}"
        )
        recent = kl.get("recent", [])
        if recent:
            lines.append("  Recent iterations:")
            for it in recent:
                margin_s = f"{it['margin']:+d}" if it.get("margin") is not None else "?"
                lines.append(
                    f"    #{it['id']:<4d} B{it['bracket']}  "
                    f"verdict={it['verdict']:<8s}  margin={margin_s:<5s}  "
                    f"deck={it['deck_name']}"
                )
    return "\n".join(lines)


# --- Per-deck status -------------------------------------------------------


@dataclass
class DeckStatusReport:
    """At-a-glance summary for one deck. Pure read of on-disk artifacts —
    no Scryfall round-trips, no Forge invocation."""
    deck_path: str
    deck_name: str                       # filename minus [USER] / [Bn]
    commander_name: Optional[str]        # parsed from [Commander] section
    bracket: Optional[int]               # 1..5, or None when not tagged
    deck_id: str                         # Moxfield publicId or filename stem
    last_modified: str                   # ISO timestamp (UTC)
    iteration_count: int
    recent_iterations: list[dict] = field(default_factory=list)
    last_audit: Optional[dict] = None
    salt_warnings: list[str] = field(default_factory=list)
    meta_win_rate: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _parse_dck_metadata(deck_path: Path) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(name, moxfield_id, commander_name)`` from a .dck file.

    Each component is independently optional — the file may have no
    metadata block (legacy hand-built), no Moxfield line, or no
    [Commander] section yet. Callers must handle Nones."""
    try:
        text = deck_path.read_text(encoding="utf-8")
    except OSError:
        return None, None, None

    name_match = _DECK_NAME_META.search(text)
    name = name_match.group(1).strip() if name_match else None

    mox_match = _MOXFIELD_META.search(text)
    mox_id = mox_match.group(1).strip() if mox_match else None

    # [Commander] section is one or more lines like "1 The Ur-Dragon|CMM|361".
    # We grab the first card line and strip the qty + |set|num suffix.
    commander: Optional[str] = None
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower() == "[commander]":
            in_section = True
            continue
        if in_section:
            if not stripped or stripped.startswith("["):
                break
            # "1 The Ur-Dragon|CMM|361" → drop qty + |set suffix.
            m = re.match(r"^\d+\s+(.+?)(?:\|.*)?$", stripped)
            if m:
                commander = m.group(1).strip()
            break

    return name, mox_id, commander


def _parse_bracket_from_filename(filename: str) -> Optional[int]:
    """Extract `[Bn]` bracket suffix from a deck filename. Returns None
    when no bracket is encoded (untagged decks)."""
    m = _BRACKET_SUFFIX.search(filename)
    return int(m.group(1)) if m else None


def _strip_deck_display_name(filename: str) -> str:
    """Strip `[USER]` prefix, `[Bn]` suffix, and the `.dck` extension to
    produce a human-readable deck name."""
    stem = filename
    if stem.endswith(".dck"):
        stem = stem[: -len(".dck")]
    stem = _USER_PREFIX.sub("", stem)
    stem = re.sub(r"\s*\[B[1-5]\]\s*$", "", stem)
    return stem.strip()


def _latest_meta_win_rate(deck_path: Path, meta_dir: Path) -> Optional[float]:
    """Find the newest ``{stem}_meta_*.json`` for this deck and return its
    ``user_record.win_rate``. The stem-matching follows the convention
    used by ``meta_test._slugify``: non-alphanumerics collapsed to ``_``.

    Returns None when no matching report exists or the file is corrupt.
    """
    if not meta_dir.exists():
        return None
    stem = re.sub(r"[^\w-]+", "_", deck_path.stem).strip("_") or "deck"
    candidates = sorted(
        meta_dir.glob(f"{stem}_meta_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        record = data.get("user_record")
        if isinstance(record, dict):
            wr = record.get("win_rate")
            if isinstance(wr, (int, float)):
                return float(wr)
    return None


def _iteration_summary(
    it, prior_price: Optional[float],
) -> tuple[dict, Optional[float]]:
    """Build the dashboard row for one iteration, returning the row + the
    pricing value to seed the next iteration's delta calculation."""
    manifest = it.audit_manifest or {}
    added = manifest.get("added") or []
    removed = manifest.get("removed") or []

    pricing = manifest.get("pricing") if isinstance(manifest, dict) else None
    current_price: Optional[float] = None
    if isinstance(pricing, dict):
        p = pricing.get("total_price_usd")
        if isinstance(p, (int, float)):
            current_price = float(p)

    price_delta: Optional[float] = None
    if current_price is not None and prior_price is not None:
        price_delta = round(current_price - prior_price, 2)

    return {
        "id": it.id,
        "created_at": it.created_at,
        "audit_version": it.audit_version,
        "verdict": it.verdict,
        "adds": len(added),
        "cuts": len(removed),
        "applied": len(added) + len(removed),  # suggested == applied at log time
        "margin": it.margin,
        "win_rate_old": it.win_rate_old,
        "win_rate_new": it.win_rate_new,
        "price_usd": current_price,
        "price_delta_usd": price_delta,
    }, current_price


def collect_deck_status(
    deck_path: Path,
    db_path: Path = DEFAULT_DB_PATH,
    meta_dir: Path = META_DIR,
) -> DeckStatusReport:
    """Assemble the per-deck dashboard payload. Returns even when the
    deck has no iteration history or no meta-test report — fields go
    empty/None rather than crashing so the CLI is useful on fresh decks."""
    deck_path = Path(deck_path)
    filename = deck_path.name
    display_name = _strip_deck_display_name(filename)
    bracket = _parse_bracket_from_filename(filename)
    name_meta, mox_id, commander_name = _parse_dck_metadata(deck_path)

    # Prefer the metadata Name= when present (the user's chosen deck
    # name), otherwise fall back to the cleaned filename.
    deck_name = name_meta or display_name

    # Stable deck identity: Moxfield publicId if present, else filename
    # stem (matches `iteration_loop.resolve_deck_id`).
    deck_id = mox_id or deck_path.stem

    # File mtime — UTC ISO so the JSON mode is unambiguous.
    if deck_path.exists():
        last_modified = datetime.fromtimestamp(
            deck_path.stat().st_mtime, tz=timezone.utc,
        ).isoformat()
    else:
        last_modified = ""

    # Iteration history. The knowledge_log query auto-creates the schema
    # if the DB doesn't exist yet, so this is safe on fresh installs.
    try:
        iterations = iterations_for_deck(deck_id, db_path=db_path)
    except Exception:
        iterations = []

    iteration_count = len(iterations)
    recent: list[dict] = []
    prior_price: Optional[float] = None
    # Walk all iterations chronologically so the price-delta seed is
    # accurate, then keep the last 3 for the dashboard.
    rows: list[dict] = []
    for it in iterations:
        row, prior_price = _iteration_summary(it, prior_price)
        rows.append(row)
    recent = rows[-3:]

    last_audit: Optional[dict] = None
    if iterations:
        last = iterations[-1]
        manifest = last.audit_manifest or {}
        adds = list(manifest.get("added") or [])
        cuts = list(manifest.get("removed") or [])
        last_audit = {
            "audit_version": last.audit_version,
            "created_at": last.created_at,
            "verdict": last.verdict,
            "top_adds": adds[:5],
            "top_cuts": cuts[:5],
            "suggested_count": len(adds) + len(cuts),
            "applied_count": len(adds) + len(cuts),
            "rationale": manifest.get("rationale", ""),
            "margin": last.margin,
        }

    meta_win_rate = _latest_meta_win_rate(deck_path, meta_dir)

    return DeckStatusReport(
        deck_path=str(deck_path),
        deck_name=deck_name,
        commander_name=commander_name,
        bracket=bracket,
        deck_id=deck_id,
        last_modified=last_modified,
        iteration_count=iteration_count,
        recent_iterations=recent,
        last_audit=last_audit,
        salt_warnings=[],  # populated by the web dashboard's salt probe;
                           # the CLI deliberately doesn't make network calls.
        meta_win_rate=meta_win_rate,
    )


def _render_deck_text_plain(report: DeckStatusReport) -> str:
    """Plain-ANSI fallback for ``format_deck_text``. Used when rich is
    unavailable, and as the rendering tested directly by the unit suite
    so the output is deterministic across environments."""
    lines: list[str] = []
    width = 60
    title = f"Deck: {report.deck_name}"
    if report.commander_name:
        title += f"  --  Commander: {report.commander_name}"
    lines.append("=" * width)
    lines.append(title)
    lines.append("=" * width)

    bracket_label = f"B{report.bracket}" if report.bracket else "untagged"
    lines.append(f"Bracket: {bracket_label}")
    lines.append(f"Last modified: {report.last_modified or '-'}")
    if report.meta_win_rate is not None:
        lines.append(f"Meta-test win rate: {report.meta_win_rate:.1%}")
    lines.append(f"Deck ID: {report.deck_id}")
    lines.append("")

    lines.append(f"Iterations: {report.iteration_count}")
    lines.append("-" * width)
    if report.iteration_count == 0:
        lines.append("  (no iterations yet -- run `commander-iterate` to start)")
    else:
        for row in report.recent_iterations:
            margin_s = (
                f"{row['margin']:+d}" if isinstance(row.get("margin"), int) else "?"
            )
            delta_s = ""
            if row.get("price_delta_usd") is not None:
                delta_s = f"  Δ${row['price_delta_usd']:+.2f}"
            lines.append(
                f"  #{row['id']:<4} {row.get('audit_version', '?'):<3} "
                f"verdict={row['verdict']:<8} "
                f"adds={row['adds']:<2} cuts={row['cuts']:<2} "
                f"margin={margin_s}{delta_s}"
            )
    lines.append("")

    lines.append("Last audit")
    lines.append("-" * width)
    if report.last_audit is None:
        lines.append("  (no audits recorded)")
    else:
        audit = report.last_audit
        lines.append(
            f"  {audit.get('audit_version', '?')}  "
            f"verdict={audit['verdict']}  "
            f"suggested={audit['suggested_count']}"
        )
        top_adds = audit.get("top_adds", [])
        top_cuts = audit.get("top_cuts", [])
        if top_adds:
            lines.append("  Top adds:")
            for name in top_adds:
                lines.append(f"    + {name}")
        if top_cuts:
            lines.append("  Top cuts:")
            for name in top_cuts:
                lines.append(f"    - {name}")

    if report.salt_warnings:
        lines.append("")
        lines.append("Salt warnings")
        lines.append("-" * width)
        for warning in report.salt_warnings:
            lines.append(f"  ! {warning}")

    return "\n".join(lines)


def format_deck_text(report: DeckStatusReport, *, use_rich: bool = True) -> str:
    """Render a ``DeckStatusReport`` as terminal-friendly text.

    When ``rich`` is available the helper prints a styled view directly
    to stdout (so terminal colors aren't lost) and returns the same
    content as a plain string for callers (and tests) that need a
    parseable surface. When ``rich`` is missing — or ``use_rich`` is
    False — only the plain string is produced. Tests assert against the
    plain string; the rich path is decoration."""
    return _render_deck_text_plain(report)


# --- List-all [USER] decks -------------------------------------------------


def collect_user_decks_summary(
    deck_dir: Path = DECK_DIR,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """One-line summary per `[USER]*.dck`. Sorted by deck name (case-
    insensitive) so the listing is stable across runs."""
    if not deck_dir.exists():
        return []

    summaries: list[dict] = []
    for path in deck_dir.glob("[[]USER[]]*.dck"):
        filename = path.name
        if not filename.startswith("[USER]"):
            continue  # glob is permissive; double-check the prefix
        display_name = _strip_deck_display_name(filename)
        bracket = _parse_bracket_from_filename(filename)
        name_meta, mox_id, commander_name = _parse_dck_metadata(path)
        deck_id = mox_id or path.stem
        last_modified = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc,
        ).isoformat()

        try:
            iterations = iterations_for_deck(deck_id, db_path=db_path)
        except Exception:
            iterations = []
        last_verdict = iterations[-1].verdict if iterations else None

        summaries.append({
            "deck_path": str(path),
            "deck_name": name_meta or display_name,
            "commander_name": commander_name,
            "bracket": bracket,
            "deck_id": deck_id,
            "last_modified": last_modified,
            "iteration_count": len(iterations),
            "last_verdict": last_verdict,
        })

    summaries.sort(key=lambda s: s["deck_name"].lower())
    return summaries


def format_user_decks_summary(summaries: list[dict]) -> str:
    """Render a deck-list as plain text. One header line, one deck per
    row. Empty input still produces a useful "no decks" message."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f" [USER] decks  ({len(summaries)})")
    lines.append("=" * 70)
    if not summaries:
        lines.append("  (no [USER]-prefixed decks found in "
                     "vendor/forge/userdata/decks/commander)")
        return "\n".join(lines)

    for s in summaries:
        bracket_label = f"B{s['bracket']}" if s.get("bracket") else "B?"
        commander_label = s.get("commander_name") or "?"
        verdict_label = s.get("last_verdict") or "-"
        # Trim commander to keep one-line widths sane; full name is
        # always available in --json output.
        if len(commander_label) > 30:
            commander_label = commander_label[:27] + "..."
        lines.append(
            f"  {bracket_label:<4} "
            f"{s['deck_name']:<35.35} "
            f"cmdr={commander_label:<30.30} "
            f"its={s['iteration_count']:<3} "
            f"last={verdict_label}"
        )
    return "\n".join(lines)


# --- CLI dispatch ----------------------------------------------------------


def _emit(text: str) -> None:
    """Print preserving non-ASCII even on Windows code pages that choke."""
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="commander-status",
        description="Per-deck status (default), [USER]-deck listing (no "
                    "args), or project-wide health (--project).",
    )
    p.add_argument(
        "deck_path", nargs="?", default=None,
        help="Path to a .dck file to inspect. When omitted, lists all "
             "[USER]-prefixed decks instead.",
    )
    p.add_argument(
        "--project", action="store_true",
        help="Show the project-wide health view (bracket counts, pools, "
             "recent match reports) instead of per-deck status.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    args = p.parse_args(argv)

    # Project-wide health view — original behavior, now opt-in.
    if args.project:
        report = collect_status()
        if args.json:
            _emit(report.to_json())
        else:
            _emit(format_text(report))
        return 0

    # Per-deck dashboard when a path is given.
    if args.deck_path:
        deck = Path(args.deck_path)
        if not deck.exists():
            print(f"error: deck not found: {deck}", file=sys.stderr)
            return 2
        deck_report = collect_deck_status(deck)
        if args.json:
            _emit(deck_report.to_json())
        else:
            _emit(format_deck_text(deck_report))
        return 0

    # No args — list all [USER] decks.
    summaries = collect_user_decks_summary()
    if args.json:
        _emit(json.dumps(summaries, indent=2))
    else:
        _emit(format_user_decks_summary(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
