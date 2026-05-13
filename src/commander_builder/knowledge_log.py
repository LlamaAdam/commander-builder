"""SQLite-backed iteration history.

Phase 2's durable memory: every audit→sim→verdict cycle writes one row.
Phase 3 (the learned predictor) reads this same table as its training set, so
the schema is defined here once and held stable. Schema changes go through
explicit migrations rather than ad-hoc ALTER.

Schema rationale:

  iterations
    id              autoincrement primary key
    deck_id         Moxfield publicId or local stem (string is fine)
    deck_name       human-readable label
    bracket         1-5
    parent_id       FK to the previous iteration of THIS deck, or NULL for v1
    audit_version   prompt version that generated this iteration (e.g. "v3")
    audit_manifest  JSON blob: {added: [...], removed: [...], rationale: "..."}
    sim_report      JSON blob: ComparisonReport (or MatchupReport) full body
    verdict         "kept" | "reverted" | "neutral" | "pending"
    verdict_notes   free-text reasoning from the analyst (Phase 2)
    win_rate_old    float, 0-1, NULL if not measured
    win_rate_new    float, 0-1, NULL if not measured
    margin          int, new_wins - old_wins
    created_at      ISO timestamp
    deck_snapshot   .dck text content (full deck preserved for reproducibility)

`deck_snapshot` keeps a copy of the .dck text so we can rebuild any historical
state without depending on Moxfield not deleting the deck. The blobs are small
(~2-5KB) so even hundreds of iterations stay well under a MB.

Public API stays thin — `record_iteration()`, `get_iteration()`, `iterations_for_deck()`,
`recent_iterations()`, plus migration. Anything richer is a query against the
plain SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .forge_runner import VENDOR_FORGE

DEFAULT_DB_PATH = VENDOR_FORGE.parent.parent / "knowledge_log.sqlite"

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS iterations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id         TEXT NOT NULL,
    deck_name       TEXT NOT NULL,
    bracket         INTEGER NOT NULL,
    parent_id       INTEGER,
    audit_version   TEXT,
    audit_manifest  TEXT,            -- JSON
    sim_report      TEXT,            -- JSON
    verdict         TEXT NOT NULL DEFAULT 'pending',
    verdict_notes   TEXT,
    win_rate_old    REAL,
    win_rate_new    REAL,
    margin          INTEGER,
    created_at      TEXT NOT NULL,
    deck_snapshot   TEXT,            -- .dck file contents
    FOREIGN KEY (parent_id) REFERENCES iterations(id)
);

CREATE INDEX IF NOT EXISTS idx_iterations_deck_id ON iterations(deck_id);
CREATE INDEX IF NOT EXISTS idx_iterations_created_at ON iterations(created_at);
CREATE INDEX IF NOT EXISTS idx_iterations_verdict ON iterations(verdict);
"""


@dataclass
class Iteration:
    """One row of the iteration history. Fields default to None so callers can
    record partial state (e.g. a 'pending' iteration before sim runs)."""
    deck_id: str
    deck_name: str
    bracket: int
    audit_version: Optional[str] = None
    audit_manifest: Optional[dict] = None
    sim_report: Optional[dict] = None
    verdict: str = "pending"
    verdict_notes: Optional[str] = None
    win_rate_old: Optional[float] = None
    win_rate_new: Optional[float] = None
    margin: Optional[int] = None
    parent_id: Optional[int] = None
    created_at: Optional[str] = None
    deck_snapshot: Optional[str] = None
    id: Optional[int] = None  # Set after insert.

    def to_row(self) -> dict:
        d = asdict(self)
        d["audit_manifest"] = json.dumps(self.audit_manifest) if self.audit_manifest is not None else None
        d["sim_report"] = json.dumps(self.sim_report) if self.sim_report is not None else None
        d["created_at"] = self.created_at or datetime.now(timezone.utc).isoformat()
        d.pop("id", None)
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Iteration":
        manifest = json.loads(row["audit_manifest"]) if row["audit_manifest"] else None
        sim = json.loads(row["sim_report"]) if row["sim_report"] else None
        return cls(
            id=row["id"],
            deck_id=row["deck_id"],
            deck_name=row["deck_name"],
            bracket=row["bracket"],
            parent_id=row["parent_id"],
            audit_version=row["audit_version"],
            audit_manifest=manifest,
            sim_report=sim,
            verdict=row["verdict"],
            verdict_notes=row["verdict_notes"],
            win_rate_old=row["win_rate_old"],
            win_rate_new=row["win_rate_new"],
            margin=row["margin"],
            created_at=row["created_at"],
            deck_snapshot=row["deck_snapshot"],
        )


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with row-factory for column access by name."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """Create the schema if missing, mark the version. Idempotent."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))


def record_iteration(it: Iteration, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Insert one Iteration. Returns the new row id. Mutates `it.id`."""
    init_db(db_path)
    row = it.to_row()
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO iterations ({','.join(cols)}) VALUES ({placeholders})"
    with _connect(db_path) as conn:
        cur = conn.execute(sql, [row[c] for c in cols])
        it.id = cur.lastrowid
    return it.id


def update_verdict(
    iteration_id: int,
    verdict: str,
    notes: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Mark an iteration's verdict (Phase 2 analyst writes this after sim)."""
    if verdict not in {"kept", "reverted", "neutral", "pending"}:
        raise ValueError(f"verdict must be one of kept/reverted/neutral/pending, got {verdict!r}")
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE iterations SET verdict = ?, verdict_notes = ? WHERE id = ?",
            (verdict, notes, iteration_id),
        )


def get_iteration(iteration_id: int, db_path: Path = DEFAULT_DB_PATH) -> Optional[Iteration]:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM iterations WHERE id = ?", (iteration_id,))
        row = cur.fetchone()
    return Iteration.from_row(row) if row else None


def iterations_for_deck(deck_id: str, db_path: Path = DEFAULT_DB_PATH) -> list[Iteration]:
    """All iterations of a deck, oldest first. Useful for reconstructing the
    full v1→v2→...→vN chain when training the Phase 3 model."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE deck_id = ? ORDER BY id ASC",
            (deck_id,),
        )
        return [Iteration.from_row(r) for r in cur.fetchall()]


def recent_iterations(limit: int = 50, db_path: Path = DEFAULT_DB_PATH) -> list[Iteration]:
    """Most recent N iterations across all decks. Sized to fit in one screen
    by default; bump for analytics queries."""
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM iterations ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [Iteration.from_row(r) for r in cur.fetchall()]


def migrate_legacy_deck_ids(
    db_path: Path = DEFAULT_DB_PATH,
    dry_run: bool = False,
) -> dict:
    """Walk `iterations` and update rows whose `deck_id` looks like a filename
    to use the Moxfield publicId instead. (GAP-024.)

    A row's `deck_id` is treated as legacy-filename-style if it contains the
    `[B<n>].dck` suffix; the publicId we want is the `Moxfield=` line in the
    `deck_snapshot` blob (preserved on insert). Rows without `Moxfield=`
    metadata in their snapshot are skipped — they pre-date the publicId
    convention and there's nothing reliable to migrate them to.

    Returns a dict with `scanned`, `updated`, `skipped`, and `details`. Pass
    `dry_run=True` to report what would change without writing."""
    import re
    legacy_re = re.compile(r"\[B[0-9?]\]\.dck$")
    moxfield_re = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)

    init_db(db_path)
    scanned = 0
    updated = 0
    skipped: list[dict] = []
    details: list[dict] = []

    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, deck_id, deck_snapshot FROM iterations ORDER BY id"
        )
        rows = cur.fetchall()
        for row in rows:
            scanned += 1
            current = row["deck_id"] or ""
            # Looks like a filename (e.g. "[USER] Foo [B3].dck")?
            if not legacy_re.search(current):
                continue
            snapshot = row["deck_snapshot"] or ""
            m = moxfield_re.search(snapshot)
            if not m:
                skipped.append({
                    "id": row["id"],
                    "deck_id": current,
                    "reason": "no Moxfield= metadata in snapshot",
                })
                continue
            new_id = m.group(1).strip()
            details.append({
                "id": row["id"],
                "old_deck_id": current,
                "new_deck_id": new_id,
            })
            if not dry_run:
                conn.execute(
                    "UPDATE iterations SET deck_id = ? WHERE id = ?",
                    (new_id, row["id"]),
                )
                updated += 1

    return {
        "scanned": scanned,
        "updated": updated if not dry_run else 0,
        "would_update": len(details) if dry_run else 0,
        "skipped": skipped,
        "details": details,
        "dry_run": dry_run,
    }


def stats_summary(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Aggregate counts useful as a one-glance sanity check on the log.
    Cheap query — runs every time the loop starts."""
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = {
            "total": conn.execute("SELECT COUNT(*) FROM iterations").fetchone()[0],
            "kept": conn.execute("SELECT COUNT(*) FROM iterations WHERE verdict = 'kept'").fetchone()[0],
            "reverted": conn.execute("SELECT COUNT(*) FROM iterations WHERE verdict = 'reverted'").fetchone()[0],
            "neutral": conn.execute("SELECT COUNT(*) FROM iterations WHERE verdict = 'neutral'").fetchone()[0],
            "pending": conn.execute("SELECT COUNT(*) FROM iterations WHERE verdict = 'pending'").fetchone()[0],
            "unique_decks": conn.execute("SELECT COUNT(DISTINCT deck_id) FROM iterations").fetchone()[0],
        }
    return rows


def pricing_series_for_deck(
    deck_id: str, db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Walk one deck's iterations chronologically and extract the
    pricing snapshots saved on each.

    Each iteration's ``audit_manifest.pricing`` (added by the
    ``save_iteration`` enrichment in 2026-05-12) carries
    ``{total_price_usd, captured_at}``. This function pulls those
    points out for charting deck-cost evolution over time. Iterations
    without a pricing block are skipped (the chart only shows points
    we actually captured).

    Returns ``[{iteration_id, captured_at, total_price_usd}, ...]``
    in iteration-id order (== chronological).
    """
    init_db(db_path)
    series: list[dict] = []
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, audit_manifest FROM iterations "
            "WHERE deck_id = ? ORDER BY id ASC",
            (deck_id,),
        )
        for row in cur.fetchall():
            manifest_raw = row["audit_manifest"]
            if not manifest_raw:
                continue
            try:
                manifest = json.loads(manifest_raw)
            except (ValueError, TypeError):
                continue
            pricing = (manifest or {}).get("pricing")
            if not isinstance(pricing, dict):
                continue
            price = pricing.get("total_price_usd")
            if not isinstance(price, (int, float)):
                continue
            series.append({
                "iteration_id": row["id"],
                "captured_at": pricing.get("captured_at"),
                "total_price_usd": float(price),
            })
    return series


def verdict_breakdown_for_deck(
    deck_id: str, db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """Per-audit-version verdict counts for one deck.

    Returns ``{audit_version: {kept, reverted, neutral, pending, total}}``.
    Rows with NULL ``audit_version`` bucket under ``"unknown"`` so the
    report doesn't crash on legacy / partial saves. Every bucket is
    zero-padded across all four verdict labels so the UI can index
    directly without guarding against KeyError.

    Backlog #6: once a deck has ≥5 iterations the UI shows "kept 4/5
    v3 swaps, 2/3 v4 swaps" so the user can spot which audit prompt
    (or advisor source) is producing landings vs. reverts.
    """
    init_db(db_path)
    out: dict[str, dict[str, int]] = {}
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT audit_version, verdict FROM iterations "
            "WHERE deck_id = ?",
            (deck_id,),
        )
        for row in cur.fetchall():
            key = row["audit_version"] or "unknown"
            bucket = out.setdefault(key, {
                "kept": 0, "reverted": 0,
                "neutral": 0, "pending": 0, "total": 0,
            })
            verdict = row["verdict"] or "pending"
            if verdict in bucket:
                bucket[verdict] += 1
            bucket["total"] += 1
    return out


if __name__ == "__main__":
    # Smoke entry: print stats for the default DB.
    s = stats_summary()
    print(json.dumps(s, indent=2))
