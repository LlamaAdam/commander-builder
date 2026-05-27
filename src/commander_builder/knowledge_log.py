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

SCHEMA_VERSION = 2

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
    milestone       TEXT,            -- v2 (#012): user-chosen tag (e.g. "baseline", "PR-ready")
    FOREIGN KEY (parent_id) REFERENCES iterations(id)
);

CREATE INDEX IF NOT EXISTS idx_iterations_deck_id ON iterations(deck_id);
CREATE INDEX IF NOT EXISTS idx_iterations_created_at ON iterations(created_at);
CREATE INDEX IF NOT EXISTS idx_iterations_verdict ON iterations(verdict);
"""
# Note: the ``milestone`` partial index lives in ``_migrate_to_v2``
# so the base schema script stays runnable against a pre-migration
# v1 table (which doesn't have the column yet). The migration runs
# unconditionally on every init_db call so both fresh databases
# and v1 → v2 upgrades pick up the index.


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2 migration: add the ``milestone`` column to existing
    iterations tables. Idempotent — checks pragma_table_info first
    so a second call doesn't error on the duplicate ADD COLUMN.

    SQLite doesn't support adding a column with WHERE-indexed
    constraints in one statement, so the partial index is added
    separately after the column lands.
    """
    cur = conn.execute("PRAGMA table_info(iterations)")
    cols = {row["name"] for row in cur.fetchall()}
    if "milestone" not in cols:
        conn.execute("ALTER TABLE iterations ADD COLUMN milestone TEXT")
    # Partial index — safe to re-run via IF NOT EXISTS.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_iterations_milestone "
        "ON iterations(milestone) WHERE milestone IS NOT NULL"
    )


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
    milestone: Optional[str] = None  # v2: user-chosen tag (#012)
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
            # Milestone added in schema v2 (#012). ``row["milestone"]``
            # raises IndexError on a v1 SQLite Row if the migration
            # didn't run for some reason — guard with ``in row.keys()``
            # so legacy databases don't break read paths.
            milestone=(
                row["milestone"] if "milestone" in row.keys() else None
            ),
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
    """Create the schema if missing, run any pending migrations,
    mark the version. Idempotent — safe to call from every entry
    point (CLIs, web routes, tests).

    Migration flow:
      v0 → v1: initial schema (executed by ``_SCHEMA_SQL`` for new
               databases; existing tables already match).
      v1 → v2: add ``milestone`` column + partial index
               (``_migrate_to_v2``, AGENT_BACKLOG #012).
    """
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)
        # Run migrations unconditionally — they're each individually
        # idempotent (check-then-add pattern via pragma_table_info),
        # so calling them on a fresh DB just adds the v2 column +
        # index that aren't in the base _SCHEMA_SQL.
        _migrate_to_v2(conn)
        cur = conn.execute("SELECT version FROM schema_version")
        row = cur.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        elif row["version"] < SCHEMA_VERSION:
            conn.execute(
                "UPDATE schema_version SET version = ?",
                (SCHEMA_VERSION,),
            )


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


def set_milestone(
    iteration_id: int,
    label: Optional[str],
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Tag (or clear) an iteration with a user-chosen milestone label
    (e.g. ``"baseline"``, ``"PR-ready"``, ``"reference build"``).

    Pass ``label=None`` or empty string to clear the milestone.
    Labels are free-form strings; max 64 chars (truncated to avoid
    accidental novella-length pastes). The UI uses milestones to
    flag reference baselines in the iteration graph; longer-term
    they're filterable in ``/api/iterations``.

    AGENT_BACKLOG #012. Idempotent; no-op on unknown iteration_id
    (matches ``update_verdict``'s fail-quiet contract).
    """
    init_db(db_path)
    normalized: Optional[str]
    if label is None or not label.strip():
        normalized = None
    else:
        normalized = label.strip()[:64]
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE iterations SET milestone = ? WHERE id = ?",
            (normalized, iteration_id),
        )


def update_verdict(
    iteration_id: int,
    verdict: str,
    notes: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Mark an iteration's verdict (Phase 2 analyst writes this after sim)."""
    if verdict not in {"kept", "reverted", "neutral", "inconclusive", "pending"}:
        raise ValueError(f"verdict must be one of kept/reverted/neutral/inconclusive/pending, got {verdict!r}")
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE iterations SET verdict = ?, verdict_notes = ? WHERE id = ?",
            (verdict, notes, iteration_id),
        )


def update_iteration_sim(
    iteration_id: int,
    verdict: str,
    sim_report: Optional[dict] = None,
    win_rate_old: Optional[float] = None,
    win_rate_new: Optional[float] = None,
    margin: Optional[int] = None,
    notes: Optional[str] = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Fold the A/B-sim outcome into a pending iteration row.

    Distinct from ``update_verdict`` because the auto-curate path runs
    the full sim atomically -- one UPDATE writes verdict + sim_report
    + win rates + margin together. Splitting them would leave the row
    in an inconsistent 'verdict=kept but sim_report=NULL' state
    if the second update failed mid-way.

    Verdict must be one of kept/reverted/neutral/pending so an "I
    don't know yet" caller can pass 'pending' and still record the
    sim_report for diagnosis.

    All non-verdict args are optional -- pass only what the sim
    produced. ``None`` values preserve the existing column value
    (SQLite COALESCE-style update; we just skip those fields in
    the SET clause).
    """
    if verdict not in {"kept", "reverted", "neutral", "inconclusive", "pending"}:
        raise ValueError(
            f"verdict must be one of kept/reverted/neutral/inconclusive/pending, "
            f"got {verdict!r}"
        )
    set_clauses = ["verdict = ?"]
    params: list = [verdict]
    if notes is not None:
        set_clauses.append("verdict_notes = ?")
        params.append(notes)
    if sim_report is not None:
        set_clauses.append("sim_report = ?")
        params.append(json.dumps(sim_report))
    if win_rate_old is not None:
        set_clauses.append("win_rate_old = ?")
        params.append(float(win_rate_old))
    if win_rate_new is not None:
        set_clauses.append("win_rate_new = ?")
        params.append(float(win_rate_new))
    if margin is not None:
        set_clauses.append("margin = ?")
        params.append(int(margin))
    params.append(iteration_id)
    sql = (
        f"UPDATE iterations SET {', '.join(set_clauses)} "
        f"WHERE id = ?"
    )
    with _connect(db_path) as conn:
        conn.execute(sql, params)


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
                "kept": 0, "reverted": 0, "neutral": 0,
                "inconclusive": 0, "pending": 0, "total": 0,
            })
            verdict = row["verdict"] or "pending"
            if verdict in bucket:
                bucket[verdict] += 1
            bucket["total"] += 1
    return out


# ---------------------------------------------------------------------------
# Iteration graph projection — feeds the SVG dashboard view
# ---------------------------------------------------------------------------
#
# The iteration table is a self-referencing tree (parent_id → another
# row in the same table). Each row carries the DIFF that produced it
# from its parent (audit_manifest.added / .removed). For visualization
# we want a nodes+edges projection the client can render directly as
# an SVG flow chart.
#
# Shape:
#   {
#     "nodes": [{id, iteration_n, bracket, verdict, created_at,
#                card_count, price_usd, audit_version, milestone}, ...],
#     "edges": [{from_id, to_id, applied_adds, applied_cuts, rationale,
#                price_delta_usd, bracket_delta}, ...]
#   }
#
# The helper does no rendering — just the projection. UI choices like
# layout, sorting within the visual, or cap on adds/cuts displayed live
# in the client.


import re as _re

_MAIN_LINE_RE = _re.compile(r"^(\d+)\s+([^|]+?)(\s*\|.*)?$")


def _count_main_cards(deck_snapshot: Optional[str]) -> int:
    """Count quantity-summed [Main] cards in a .dck snapshot.

    Walks line-by-line; only counts lines inside the [Main] section.
    Commander, sideboard, considering sections are excluded — they
    don't change between iterations in a way that's worth surfacing
    on the graph. Returns 0 for None / empty snapshot.
    """
    if not deck_snapshot:
        return 0
    total = 0
    in_main = False
    for raw in deck_snapshot.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            continue
        if in_main:
            m = _MAIN_LINE_RE.match(stripped)
            if m:
                try:
                    total += int(m.group(1))
                except (TypeError, ValueError):
                    total += 1
    return total


def _parse_main_cards(deck_snapshot: Optional[str]) -> dict:
    """Parse a .dck snapshot's [Main] section into {card_name: quantity}.

    Card names are normalized to their base name (the bit before any `|set|n`
    suffix), so the same card across two snapshots compares equal regardless
    of printing. Quantities are summed. Non-[Main] sections are ignored, to
    match `_count_main_cards`."""
    cards: dict = {}
    if not deck_snapshot:
        return cards
    in_main = False
    for raw in deck_snapshot.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_main = stripped.lower() == "[main]"
            continue
        if not in_main:
            continue
        m = _MAIN_LINE_RE.match(stripped)
        if not m:
            continue
        try:
            qty = int(m.group(1))
        except (TypeError, ValueError):
            qty = 1
        name = m.group(2).strip()
        if name:
            cards[name] = cards.get(name, 0) + qty
    return cards


def audit_card_diff(from_snapshot: Optional[str], to_snapshot: Optional[str]) -> dict:
    """Card-level delta between two .dck snapshots (#013 audit diff).

    Compares the [Main] sections as quantity-maps and returns::

        {"added":   [{"name", "qty"}, ...],   # net-added (to has more)
         "removed": [{"name", "qty"}, ...],   # net-removed (from had more)
         "unchanged": <int>,                  # cards with identical quantity
         "from_total": <int>, "to_total": <int>}

    `qty` is the magnitude of the change (e.g. a 1->3 basic-land bump is
    `added qty=2`). Added/removed are sorted by name. Pure + snapshot-only,
    so it's safe to unit-test without the DB or web layer."""
    a = _parse_main_cards(from_snapshot)
    b = _parse_main_cards(to_snapshot)
    added, removed, unchanged = [], [], 0
    for name in set(a) | set(b):
        delta = b.get(name, 0) - a.get(name, 0)
        if delta > 0:
            added.append({"name": name, "qty": delta})
        elif delta < 0:
            removed.append({"name": name, "qty": -delta})
        else:
            unchanged += 1
    added.sort(key=lambda c: c["name"].lower())
    removed.sort(key=lambda c: c["name"].lower())
    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "from_total": sum(a.values()),
        "to_total": sum(b.values()),
    }


def _node_price_from_manifest(manifest: Optional[dict]) -> Optional[float]:
    """Pull total_price_usd out of audit_manifest.pricing if present.

    Mirrors the lookup pricing_series_for_deck does. Returns None
    when the manifest is missing, the pricing block is missing, or
    the price is non-numeric — never crashes, never invents a 0.
    """
    if not isinstance(manifest, dict):
        return None
    pricing = manifest.get("pricing")
    if not isinstance(pricing, dict):
        return None
    price = pricing.get("total_price_usd")
    if isinstance(price, (int, float)):
        return float(price)
    return None


def iteration_graph_for_deck(
    deck_id: str, db_path: Path = DEFAULT_DB_PATH,
) -> dict:
    """Project one deck's iteration chain as a JSON-friendly graph.

    Returns ``{"nodes": [...], "edges": [...]}`` ready for the SVG
    renderer. Empty graph (both lists empty) when the deck has no
    iterations — caller can hide the panel rather than crash on
    null.

    Nodes are ordered by iteration id (== chronological). Edges
    come from parent_id; iterations without a parent contribute no
    edge (chain roots). Forked chains (rare but possible) render as
    separate components — the renderer can lay them out side-by-side.

    Edge fields:
      applied_adds / applied_cuts — the child's audit_manifest's
        added/removed lists. Empty when the manifest is missing
        or the row pre-dates the enrichment.
      rationale — the child's audit_manifest.rationale, empty on miss.
      price_delta_usd — child.price - parent.price. None if either
        side lacks pricing; treating absence as 0 would lie.
      bracket_delta — child.bracket - parent.bracket. Signed int.
    """
    init_db(db_path)
    nodes_by_id: dict[int, dict] = {}
    iterations: list[Iteration] = []
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE deck_id = ? ORDER BY id ASC",
            (deck_id,),
        )
        iterations = [Iteration.from_row(r) for r in cur.fetchall()]

    if not iterations:
        return {"nodes": [], "edges": []}

    nodes: list[dict] = []
    for idx, it in enumerate(iterations):
        node = {
            "id": it.id,
            "iteration_n": idx + 1,
            "bracket": it.bracket,
            "verdict": it.verdict,
            "created_at": it.created_at,
            "audit_version": it.audit_version,
            "card_count": _count_main_cards(it.deck_snapshot),
            "price_usd": _node_price_from_manifest(it.audit_manifest),
            "milestone": getattr(it, "milestone", None),
        }
        nodes.append(node)
        nodes_by_id[it.id] = node

    edges: list[dict] = []
    for it in iterations:
        if it.parent_id is None or it.parent_id not in nodes_by_id:
            continue
        parent_node = nodes_by_id[it.parent_id]
        child_node = nodes_by_id[it.id]
        manifest = it.audit_manifest if isinstance(it.audit_manifest, dict) else {}

        parent_price = parent_node.get("price_usd")
        child_price = child_node.get("price_usd")
        if isinstance(parent_price, (int, float)) and isinstance(child_price, (int, float)):
            price_delta: Optional[float] = round(child_price - parent_price, 2)
        else:
            price_delta = None

        edges.append({
            "from_id": it.parent_id,
            "to_id": it.id,
            "applied_adds": list(manifest.get("added") or []),
            "applied_cuts": list(manifest.get("removed") or []),
            "rationale": str(manifest.get("rationale") or ""),
            "price_delta_usd": price_delta,
            "bracket_delta": (child_node["bracket"] or 0) - (parent_node["bracket"] or 0),
        })

    return {"nodes": nodes, "edges": edges}


if __name__ == "__main__":
    # Smoke entry: print stats for the default DB.
    s = stats_summary()
    print(json.dumps(s, indent=2))
