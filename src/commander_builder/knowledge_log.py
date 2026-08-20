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
    measurement_era small int naming which measurement convention produced
                    this row's numbers (see MEASUREMENT_ERAS); NULL = unknown

Win-rate convention (2026-07-20): ``win_rate_old`` / ``win_rate_new`` are
wins / HEAD-TO-HEAD DECISIVE games, where decisive = wins_old + wins_new —
the games one of the two compared versions actually won (draws,
unattributed games, and filler-won pod games are all excluded; see
``decisive_win_rate``). When decisive == 0 the columns are NULL, never a
fabricated 0.0.

Convention history — cross-run analyses that pool these columns must
bucket rows by ``measurement_era`` (2026-08-17; before that column
existed, by write date and id, which is what the backfill below
mechanizes):

  * Before 2026-07-19 the three writers used three different denominators
    (all-games-including-draws, decisive-only, per-version-games).
  * 2026-07-19 (611feff) unified the writers on "attributed-winner"
    denominators — but the compare-shaped writers (iteration_loop,
    save_iteration on total_games payloads) counted FILLER-won games in
    their denominator (total_games - draws) while the AB-shaped writers
    (_proposer_sim, merge_soak) counted only head-to-head wins
    (wins_a + wins_b). Fillers take roughly half the games in a 4-player
    pod, so rows written between 2026-07-19 and 2026-07-20 are a MIXED
    population whose two halves differ by ~2x scale — do NOT pool them
    as one convention.
  * From 2026-07-20 all writers use head-to-head decisive
    (wins_old + wins_new), the denominator every verdict gate counts.

`deck_snapshot` keeps a copy of the .dck text so we can rebuild any historical
state without depending on Moxfield not deleting the deck. The blobs are small
(~2-5KB) so even hundreds of iterations stay well under a MB.

Public API stays thin — `record_iteration()`, `get_iteration()`, `iterations_for_deck()`,
`recent_iterations()`, plus migration. Anything richer is a query against the
plain SQLite file.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import dck_utils
from .forge_runner import VENDOR_FORGE

DEFAULT_DB_PATH = VENDOR_FORGE.parent.parent / "knowledge_log.sqlite"


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    """Resolve ``db_path=None`` to the CURRENT ``DEFAULT_DB_PATH``.

    Public functions take ``db_path: Optional[Path] = None`` and route
    through this helper instead of using ``DEFAULT_DB_PATH`` as a
    default parameter value. A ``def f(db_path=DEFAULT_DB_PATH)``
    default is evaluated once at import time, which silently defeats
    the test-suite's autouse fixture that monkeypatches
    ``knowledge_log.DEFAULT_DB_PATH`` — every call would still hit the
    production database. Reading the module attribute here, at call
    time, makes that patch actually take effect."""
    return db_path if db_path is not None else DEFAULT_DB_PATH


def decisive_win_rate(wins: int, decisive: int) -> Optional[float]:
    """Canonical win-rate for the ``win_rate_old`` / ``win_rate_new`` columns.

    ONE convention (2026-07-20): wins / HEAD-TO-HEAD DECISIVE games, rounded
    to 4 places, where ``decisive`` = wins_old + wins_new — the count of
    games one of the TWO COMPARED VERSIONS actually won. Draws, unattributed
    games, and FILLER-won pod games are all excluded. Head-to-head decisive
    is the quantity every verdict gate counts (analyst margin checks,
    ``_verdict_from_ab``'s MIN_DECISIVE_GAMES_FOR_VERDICT) and the only
    attributed-winner count EVERY writer's sim shape can compute — an
    ABResult never attributes filler wins, while a compare() report does, so
    any denominator that includes filler wins (e.g. total_games - draws)
    exists only for compare-shaped writers and differs from the AB writers'
    by roughly 2x in a 4-player pod.

    Returns ``None`` when ``decisive <= 0`` so callers persist NULL rather
    than a fabricated 0.0 that would read as an observed "never wins" result.

    Every writer of those columns MUST pass decisive = wins_old + wins_new
    through this helper so the values stay cross-run comparable (FP-002-style
    row gates read them as one population):

      - ``_proposer_sim._ab_to_iteration_fields``  (wins_a + wins_b)
      - ``iteration_loop.run_one_iteration``       (old_stats.wins + new_stats.wins)
      - ``web.routes_sim.save_iteration``          (old_wins + new_wins)
      - ``scripts/merge_soak`` soak-fold           (wins_a + wins_b)
    """
    if decisive <= 0:
        return None
    return round(wins / decisive, 4)


def canonical_content_hash(row: dict, exclude: frozenset = frozenset()) -> str:
    """Canonical sha256 over a row-shaped dict, minus ``exclude`` keys.

    Shared identity primitive for "have I already stored this content?"
    checks. It originated as ``export._content_hash`` (99e8b53, the
    import-merge dedupe) and was promoted here (2026-07-19) because
    ``scripts/merge_soak.py``'s knowledge-log fold needs the exact same
    canonicalization for its own idempotence check — sharing the one
    implementation beats two copies that could drift and silently stop
    hashing the same content to the same digest.

    Canonicalization details that make cross-machine / cross-run hashes
    comparable:
      * ``sort_keys=True`` — nested dicts (audit_manifest, sim_report)
        hash identically regardless of insertion order;
      * ``default=str`` — non-JSON types (e.g. ``Path``) degrade to their
        string form instead of crashing the dump.

    Callers choose what identity MEANS by what dict they pass and which
    keys they exclude: export/import excludes the machine-local
    ``id``/``parent_id`` columns; merge_soak passes an explicit stable
    subset of soak-row facts (no exclusions needed).
    """
    semantic = {k: v for k, v in row.items() if k not in exclude}
    blob = json.dumps(semantic, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Measurement eras (2026-08-17)
# ---------------------------------------------------------------------------
# This log spans three mutually incompatible measurement conventions and,
# until now, nothing on a row said which one produced its numbers. Pooling
# them is not "noisy data", it is comparing different quantities: an era-1
# margin was attributed to the wrong deck, an era-2 win rate has a
# denominator up to ~2x an era-3 one, and an era-3 'kept' means
# |margin| >= 4 where an era-4 'kept' means a significant binomial test.
# Every new row now carries its era; historical rows are backfilled where
# the boundary is KNOWN and left NULL where it isn't.
#
#: era -> what changed. The value stored in ``iterations.measurement_era``.
MEASUREMENT_ERAS: dict[int, str] = {
    1: (
        "pre-seat-attribution (…2026-05-21, local ids < 314). A/B wins "
        "were credited by deck NAME, and A/B decks routinely share a "
        "Name=, so wins funnelled to one side (e8777b6). margin, "
        "win_rate_* and verdict are all measurement artifacts — archive "
        "only, never training data."
    ),
    2: (
        "seat-attributed, mixed win-rate denominators (2026-05-23 … "
        "2026-07-18; rows dated 2026-05-21/22 join this era only when "
        "id >= 314 breaks the tie, since the seat fix landed mid-session "
        "— see _SEAT_FIX_START/_SETTLED). Wins are correctly attributed, but the three "
        "writers used three different denominators "
        "(all-games-including-draws / decisive-only / per-version-games), "
        "so win_rate_old and win_rate_new are not comparable across rows. "
        "margin and verdict are usable; the rates are not."
    ),
    3: (
        "head-to-head decisive denominator, margin-threshold verdicts "
        "(2026-07-20 … 2026-08-13). Every writer computes win rates over "
        "wins_old + wins_new (``decisive_win_rate``), so the rates pool "
        "cleanly. Verdicts do NOT: kept/reverted came from a "
        "game-count-invariant |margin| >= 4, which labels ~half of "
        "NEUTRAL swaps confidently at 20 decisive games."
    ),
    4: (
        "significance-based verdicts (2026-08-14 …). Same decisive "
        "denominator as era 3, plus kept/reverted now requires an exact "
        "two-sided binomial test vs p=0.5 at alpha 0.05 over >= 20 "
        "decisive head-to-head games; everything else is 'inconclusive'. "
        "Rates AND verdicts pool cleanly."
    ),
}

#: Stamped on every row written from now on.
CURRENT_MEASUREMENT_ERA = 4

#: Lowest era whose ``win_rate_old`` / ``win_rate_new`` may be pooled or
#: differenced with a current row's. Era 3 is where every writer landed on
#: the head-to-head decisive denominator (see MEASUREMENT_ERAS[3]) — below
#: it a "win rate" is a different quantity (era 2's denominators vary by
#: writer, era 1's wins are attributed to the wrong deck), so a trajectory
#: baselined on one is a subtraction of unlike units, not a measurement.
#: 2026-08-20: added because report.py's trajectory was doing exactly that.
MIN_COMPARABLE_RATE_ERA = 3

#: Lowest era whose ``verdict`` may be pooled with a current row's. Era 4
#: is where kept/reverted started meaning "statistically significant"
#: instead of "|margin| >= 4" — an era-3 'kept' and an era-4 'kept' are
#: different claims, so verdict tallies have to name the era they counted.
#: 2026-08-20: added for verdict_breakdown_for_deck's era split.
MIN_COMPARABLE_VERDICT_ERA = 4

#: Key under which a sim_report carries the verdict parameters that
#: produced its row's verdict (see ``verdict_provenance``).
SIM_REPORT_VERDICT_PARAMS_KEY = "verdict_params"

#: Key under which a sim_report carries a confirming (replication) run's
#: own split — structured, not prose (see ``update_iteration_sim``).
SIM_REPORT_REPLICATION_KEY = "replication"


def verdict_provenance(
    *,
    margin: int,
    alpha: float,
    min_decisive: int,
    rule: str = "binomial_two_sided_p < alpha over >= min_decisive decisive",
) -> dict:
    """The verdict parameters a writer actually used, for its sim_report.

    2026-08-20. ``measurement_era`` says which *convention* labeled a
    row, but era 4 has a tunable inside it: ``--sim-margin`` is a
    minimum-effect pre-filter, so a run with ``--sim-margin 15`` calls a
    significant 27-13 'neutral' while the default (1) calls it 'kept'.
    Both rows are stamped era 4 and pool as if the label meant one thing.
    Until now the only record of the margin used was a free-text note.

    Storing the triple makes a row auditable on its own terms: a
    re-scoring pass or a pooled analysis can see what bar this verdict
    actually cleared without knowing which code version (or which CLI
    flags) wrote it. A plain dict inside sim_report rather than a column
    because this is verdict *provenance*, not a queryable measurement,
    and sim_report is already the row's "everything the sim knew" blob.
    """
    return {
        "margin": int(margin),
        "alpha": float(alpha),
        "min_decisive": int(min_decisive),
        "rule": rule,
    }

# Era boundaries. Dates are the ISO prefixes of ``created_at``; the id
# boundary is the one recorded in STATUS.md for THIS repo's log.
PRE_SEAT_ATTRIBUTION_MAX_ID = 314   # ids < 314 are era 1 (STATUS.md, e8777b6)
_SEAT_FIX_START = "2026-05-21"      # fix landed over the 2026-05-21/22 session
_SEAT_FIX_SETTLED = "2026-05-23"    # first date that is unambiguously post-fix
_DECISIVE_MIXED_START = "2026-07-19"  # writers unified, denominators still mixed
_DECISIVE_SETTLED = "2026-07-20"    # every writer on head-to-head decisive
_SIGNIFICANCE_START = "2026-08-14"  # significance-based verdicts land


def measurement_era_for(
    created_at: Optional[str], iteration_id: Optional[int] = None,
) -> Optional[int]:
    """Which ``MEASUREMENT_ERAS`` key a row belongs to, or None if unknown.

    Two signals, and ``created_at`` DECIDES:

      * ``created_at`` — the ISO timestamp, compared against the era
        boundaries above as strings (ISO-8601 sorts chronologically, so
        ``>=`` on the raw string is a date compare).
      * ``iteration_id`` — only a tie-breaker, for the one window a
        date can't resolve (the fix landed mid-session on 2026-05-21/22)
        and for rows with no usable timestamp at all. STATUS.md pins the
        pre-fix boundary at ``id < 314``, but ids are MACHINE-LOCAL: on
        the owner's log they're chronological and agree with the dates,
        while a fresh database (or an imported row) restarts at 1. That
        is why the date leads — letting ``id < 314`` fire first labeled
        every low-id row in a brand-new database as a 2026-era
        measurement artifact.

    Returns None — "unknown", stored as NULL — rather than guessing, in
    the three cases where the honest answer is that we cannot tell:
    a missing/unparseable timestamp with no id to fall back on; a row
    written during the 2026-05-21/22 fix session with no id to
    disambiguate; and a row written in the 2026-07-19 → 2026-07-20
    window, which is a MIXED population by writer (compare-shaped
    writers counted filler-won games, AB-shaped writers didn't) and
    therefore has no single era.
    """
    stamp = created_at.strip() if isinstance(created_at, str) else ""
    if not stamp:
        # No date. The id boundary is all that's left, and it can only
        # ever establish era 1 — "id >= 314" means "not era 1", not
        # "era 2" (that needs a date).
        if (
            iteration_id is not None
            and iteration_id < PRE_SEAT_ATTRIBUTION_MAX_ID
        ):
            return 1
        return None
    if stamp >= _SIGNIFICANCE_START:
        return 4
    if stamp >= _DECISIVE_SETTLED:
        return 3
    if stamp >= _DECISIVE_MIXED_START:
        return None  # the mixed window — see the docstring
    if stamp >= _SEAT_FIX_SETTLED:
        return 2
    if stamp >= _SEAT_FIX_START:
        # Inside the fix session: the id is the only disambiguator, and
        # without one there is nothing to disambiguate with.
        if iteration_id is None:
            return None
        return 1 if iteration_id < PRE_SEAT_ATTRIBUTION_MAX_ID else 2
    return 1


SCHEMA_VERSION = 3

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
    measurement_era INTEGER,         -- v3: MEASUREMENT_ERAS key, NULL = unknown/mixed
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


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3 migration: add ``measurement_era`` and backfill it.

    Same check-then-add shape as ``_migrate_to_v2`` (pragma_table_info
    guard, ``IF NOT EXISTS`` index) so re-running init_db is a no-op.

    The backfill classifies rows through ``measurement_era_for``, the
    SAME function the insert path stamps with — one boundary
    definition, not two that can drift. It touches ONLY rows whose era
    is still NULL, and only sets a value where the era is KNOWN:
    unclassifiable rows (no timestamp, the 2026-05-21/22 fix session,
    the 2026-07-19/20 mixed-denominator window) keep NULL, which reads
    as "unknown" and not as a fourth era. No other column is read or
    written — this migration must never touch the numbers whose
    provenance it is describing.

    Because unknown rows stay NULL, the scan re-runs on every init_db.
    That is deliberate (it is how a hand-inserted or imported legacy
    row eventually gets classified) and cheap at this log's scale
    (hundreds of rows); ``idx_iterations_measurement_era`` — plain, not
    partial, unlike the milestone index — serves both this ``IS NULL``
    lookup and the era-filtered analysis queries the column exists for.
    """
    cur = conn.execute("PRAGMA table_info(iterations)")
    cols = {row["name"] for row in cur.fetchall()}
    if "measurement_era" not in cols:
        conn.execute(
            "ALTER TABLE iterations ADD COLUMN measurement_era INTEGER"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_iterations_measurement_era "
        "ON iterations(measurement_era)"
    )
    cur = conn.execute(
        "SELECT id, created_at FROM iterations WHERE measurement_era IS NULL"
    )
    updates: list[tuple[int, int]] = []
    for row in cur.fetchall():
        era = measurement_era_for(row["created_at"], row["id"])
        if era is not None:
            updates.append((era, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE iterations SET measurement_era = ? WHERE id = ?", updates,
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
    # v3: which measurement convention produced this row's numbers.
    # Left None by callers; ``to_row`` derives it from created_at so
    # every writer stamps it without having to know it exists.
    measurement_era: Optional[int] = None
    id: Optional[int] = None  # Set after insert.

    def to_row(self) -> dict:
        d = asdict(self)
        d["audit_manifest"] = json.dumps(self.audit_manifest) if self.audit_manifest is not None else None
        d["sim_report"] = json.dumps(self.sim_report) if self.sim_report is not None else None
        d["created_at"] = self.created_at or datetime.now(timezone.utc).isoformat()
        # Derive the era from the row's OWN timestamp rather than
        # hardcoding CURRENT_MEASUREMENT_ERA: a live write stamps now ->
        # the current era, while a backdated row (an import of an older
        # export, a merge_soak fold) is classified by when it was
        # actually measured. An era the caller set explicitly wins; None
        # means "you work it out", and an unclassifiable timestamp
        # stays None (NULL) rather than being rounded to an era.
        if d.get("measurement_era") is None:
            d["measurement_era"] = measurement_era_for(d["created_at"])
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
            # Same legacy guard as milestone: schema v3 added this
            # column, so a Row from a database the migration hasn't
            # touched yet has no such key.
            measurement_era=(
                row["measurement_era"]
                if "measurement_era" in row.keys() else None
            ),
        )


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context-managed connection with row-factory for column access by name.

    Hardened for concurrent local writers (the web app, a CLI run, and
    parallel A/B pods can all touch the same file): WAL journaling lets
    readers coexist with a writer, and busy_timeout makes a second writer
    wait-and-retry instead of failing immediately with "database is
    locked". Rolls back explicitly on error so a partially applied write
    never reaches commit.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    """Create the schema if missing, run any pending migrations,
    mark the version. Idempotent — safe to call from every entry
    point (CLIs, web routes, tests).

    Migration flow:
      v0 → v1: initial schema (executed by ``_SCHEMA_SQL`` for new
               databases; existing tables already match).
      v1 → v2: add ``milestone`` column + partial index
               (``_migrate_to_v2``, AGENT_BACKLOG #012).
      v2 → v3: add ``measurement_era`` column + index, and backfill
               historical rows from their id/created_at where the era
               is known (``_migrate_to_v3``, 2026-08-17).
    """
    with _connect(_resolve_db_path(db_path)) as conn:
        # Per-statement execute, NOT executescript(): executescript()
        # issues an implicit COMMIT before running, which detaches the
        # schema from the surrounding transaction and could leave a
        # v1 -> v2 upgrade half-applied if a later migration step fails.
        for statement in _SCHEMA_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)
        # Run migrations unconditionally — they're each individually
        # idempotent (check-then-add pattern via pragma_table_info),
        # so calling them on a fresh DB just adds the v2 column +
        # index that aren't in the base _SCHEMA_SQL.
        _migrate_to_v2(conn)
        _migrate_to_v3(conn)
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


def record_iteration(it: Iteration, db_path: Optional[Path] = None) -> int:
    """Insert one Iteration. Returns the new row id. Mutates `it.id`."""
    db_path = _resolve_db_path(db_path)
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
    db_path: Optional[Path] = None,
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
    db_path = _resolve_db_path(db_path)
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
    db_path: Optional[Path] = None,
) -> None:
    """Mark an iteration's verdict (Phase 2 analyst writes this after sim)."""
    if verdict not in {"kept", "reverted", "neutral", "inconclusive", "pending"}:
        raise ValueError(f"verdict must be one of kept/reverted/neutral/inconclusive/pending, got {verdict!r}")
    with _connect(_resolve_db_path(db_path)) as conn:
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
    db_path: Optional[Path] = None,
    notes_append: bool = False,
    sim_report_merge: Optional[dict] = None,
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

    Second-writer semantics (2026-08-20)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    ``notes`` and ``sim_report`` REPLACE by default, which is right for
    the first writer (the sim that produced the row) and wrong for a
    second one. improve.py's replication writer is a second writer: it
    was destroying run 1's "A/B sim: old won X, new won Y (N games,
    margin=M)" note by passing a fresh string, so a confirmed row's only
    surviving record of what run 1 measured was the sim_report blob.

      * ``notes_append=True`` -- read the row's current ``verdict_notes``
        and append this call's ``notes`` on a new line instead of
        overwriting. A NULL/blank existing note degrades to a plain set.
      * ``sim_report_merge`` -- shallow top-level merge into the row's
        EXISTING sim_report (or into ``sim_report`` when that is also
        passed), so a second writer can add a key
        (``SIM_REPORT_REPLICATION_KEY``, ``SIM_REPORT_VERDICT_PARAMS_KEY``)
        without clobbering the measured record it is annotating. A row
        with no sim_report yet gets the merge dict as its sim_report.

    Both read-modify-write inside the same connection as the UPDATE, so
    the row never lands half-written (the same reason this function
    exists at all).
    """
    if verdict not in {"kept", "reverted", "neutral", "inconclusive", "pending"}:
        raise ValueError(
            f"verdict must be one of kept/reverted/neutral/inconclusive/pending, "
            f"got {verdict!r}"
        )
    db_path = _resolve_db_path(db_path)
    with _connect(db_path) as conn:
        # Read-modify-write for the append/merge modes happens on THIS
        # connection, so the SELECT and the UPDATE share one transaction
        # and the row can't be half-written -- the same reason this
        # function writes every sim field in one statement.
        existing: Optional[sqlite3.Row] = None
        if notes_append or sim_report_merge is not None:
            cur = conn.execute(
                "SELECT verdict_notes, sim_report FROM iterations WHERE id = ?",
                (iteration_id,),
            )
            existing = cur.fetchone()
        if notes is not None and notes_append and existing is not None:
            prior = (existing["verdict_notes"] or "").strip()
            if prior:
                notes = f"{prior}\n{notes}"
        if sim_report_merge is not None:
            base_report = sim_report
            if (
                base_report is None
                and existing is not None
                and existing["sim_report"]
            ):
                try:
                    base_report = json.loads(existing["sim_report"])
                except (TypeError, ValueError):
                    # A corrupt blob is not worth losing the merge over,
                    # but it is also not ours to silently reinterpret --
                    # start from the merge dict alone.
                    base_report = None
            merged = dict(base_report) if isinstance(base_report, dict) else {}
            merged.update(sim_report_merge)
            sim_report = merged
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
        conn.execute(
            f"UPDATE iterations SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )


def get_iteration(iteration_id: int, db_path: Optional[Path] = None) -> Optional[Iteration]:
    db_path = _resolve_db_path(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM iterations WHERE id = ?", (iteration_id,))
        row = cur.fetchone()
    return Iteration.from_row(row) if row else None


def iterations_for_deck(deck_id: str, db_path: Optional[Path] = None) -> list[Iteration]:
    """All iterations of a deck, oldest first. Useful for reconstructing the
    full v1→v2→...→vN chain when training the Phase 3 model."""
    db_path = _resolve_db_path(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM iterations WHERE deck_id = ? ORDER BY id ASC",
            (deck_id,),
        )
        return [Iteration.from_row(r) for r in cur.fetchall()]


def all_iterations(db_path: Optional[Path] = None) -> list[Iteration]:
    """Every iteration in the log, oldest first (id ASC).

    Added for the export/import path: the full export previously faked
    "all" through ``recent_iterations(limit=10_000)``, which silently
    dropped everything past 10k rows while still reporting success.
    Rows are a few KB each, so even a very large personal log is only
    tens of MB — there is no memory reason for a cap. "All" means all.
    """
    db_path = _resolve_db_path(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM iterations ORDER BY id ASC")
        return [Iteration.from_row(r) for r in cur.fetchall()]


def recent_iterations(limit: int = 50, db_path: Optional[Path] = None) -> list[Iteration]:
    """Most recent N iterations across all decks. Sized to fit in one screen
    by default; bump for analytics queries."""
    db_path = _resolve_db_path(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM iterations ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [Iteration.from_row(r) for r in cur.fetchall()]


def migrate_legacy_deck_ids(
    db_path: Optional[Path] = None,
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
    legacy_re = re.compile(r"\[B[0-9?]\]\.dck$")
    moxfield_re = re.compile(r"^Moxfield=(.+)$", re.MULTILINE)

    db_path = _resolve_db_path(db_path)
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


def stats_summary(db_path: Optional[Path] = None) -> dict:
    """Aggregate counts useful as a one-glance sanity check on the log.
    Cheap query — runs every time the loop starts."""
    db_path = _resolve_db_path(db_path)
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


# FP-013 gate: promote the project-tuned-LLM spike when the live log holds
# this many high-confidence curator iterations (see docs/archive/fp013-scope.md).
FP013_GATE_TARGET = 1000
FP013_MIN_GAMES = 40

#: Measurement-era floor for training rows (2026-08-17). The fine-tune
#: learns the VERDICT, so the era that matters is the one that produced
#: the LABEL, not the one that produced the raw counts. Era 4 is the
#: first whose kept/reverted comes from the significance test; era 3's
#: came from a game-count-invariant ``|margin| >= 4`` that labels ~half
#: of neutral swaps confidently at 20 decisive games (see
#: MEASUREMENT_ERAS[3]). Training on those teaches the model to be
#: confident exactly where the evidence isn't.
FP013_MIN_TRAINING_ERA = 4

#: Era 3 is not lost, just mislabelled: its measurement is sound (same
#: decisive denominator as era 4), so a row can be recovered by
#: re-scoring its stored ``sim_report`` with the current significance
#: test. Reported separately so the backlog is visible rather than
#: silently discarded. Eras 1-2 are NOT recoverable — era 1's wins are
#: attributed to the wrong deck, era 2's rates aren't comparable.
FP013_RELABELABLE_ERA = 3


def fp013_gate_progress(
    db_path: Optional[Path] = None,
    *,
    min_games: int = FP013_MIN_GAMES,
    target: int = FP013_GATE_TARGET,
    min_era: int = FP013_MIN_TRAINING_ERA,
) -> dict:
    """Count high-confidence curator iterations toward the FP-013 gate.

    A row qualifies when it carries the full training triple the
    fine-tune needs: an ``audit_manifest`` (the question), a decided
    verdict (the label — kept/reverted/neutral, not pending), and a
    ``sim_report`` with at least ``min_games`` games (ABResult stores
    the count as ``games``; compare_versions' ComparisonReport as
    ``total_games``). Soak rows live outside this DB and never count —
    they are labels without the question they answered.

    Since 2026-08-17 the row must ALSO come from measurement era
    ``min_era`` or later. The triple's shape was never the whole
    question: a pre-e8777b6 row can carry a manifest, a verdict and a
    60-game sim report and still be worthless, because its wins were
    credited to the wrong deck. Counting those toward a training gate
    reports readiness the data doesn't have. An unstamped row (era
    NULL) fails closed — unknown provenance is not evidence of good
    provenance, and every row written from now on carries a stamp.

    The returned dict discloses what the floor removed rather than
    quietly shrinking the number:

    ``relabelable``
        Rows that meet the triple and come from era
        ``FP013_RELABELABLE_ERA``, whose measurement is sound but whose
        verdict came from the old margin threshold. Re-scoring their
        stored ``sim_report`` with the current significance test
        promotes them; they are a backlog, not a loss.
    ``excluded_by_era``
        Rows that meet the triple but whose labels are unrecoverable
        (eras 1-2) or unknown (NULL).
    """
    db_path = _resolve_db_path(db_path)
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT sim_report, measurement_era FROM iterations "
            "WHERE audit_manifest IS NOT NULL "
            "AND verdict IN ('kept', 'reverted', 'neutral') "
            "AND sim_report IS NOT NULL"
        ).fetchall()
    count = 0
    relabelable = 0
    excluded_by_era = 0
    for row in rows:
        try:
            report = json.loads(row["sim_report"])
        except (TypeError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        games = report.get("games") or report.get("total_games") or 0
        if not (isinstance(games, (int, float)) and games >= min_games):
            continue
        era = row["measurement_era"]
        if isinstance(era, int) and era >= min_era:
            count += 1
        elif era == FP013_RELABELABLE_ERA:
            relabelable += 1
        else:
            excluded_by_era += 1
    return {
        "count": count,
        "target": target,
        "min_games": min_games,
        "min_era": min_era,
        "relabelable": relabelable,
        "excluded_by_era": excluded_by_era,
        "pct": round(100.0 * count / target, 1) if target else 0.0,
    }


def pricing_series_for_deck(
    deck_id: str, db_path: Optional[Path] = None,
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
    db_path = _resolve_db_path(db_path)
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
    deck_id: str, db_path: Optional[Path] = None,
) -> dict:
    """Per-audit-version verdict counts for one deck, split by era.

    Returns ``{audit_version: {kept, reverted, neutral, inconclusive,
    pending, total, by_era: {<era>: {...same six...}}}}``. Rows with NULL
    ``audit_version`` bucket under ``"unknown"`` so the report doesn't
    crash on legacy / partial saves. Every bucket is zero-padded across
    all five verdict labels so the UI can index directly without guarding
    against KeyError.

    Backlog #6: once a deck has ≥5 iterations the UI shows "kept 4/5
    v3 swaps, 2/3 v4 swaps" so the user can spot which audit prompt
    (or advisor source) is producing landings vs. reverts.

    Era split (2026-08-20)
    ~~~~~~~~~~~~~~~~~~~~~~
    This function used to SELECT verdict alone and pool every row, which
    is the one thing this module's schema docstring forbids: an era-1
    'kept' is a seat-attribution artifact, an era-3 'kept' means
    |margin| >= 4, and an era-4 'kept' means a significant binomial test
    (see MEASUREMENT_ERAS). Counting them in one pile invents a "kept
    rate" out of three incompatible labels.

    The flat per-version totals are KEPT as-is for back-compat (the
    dashboard's existing pills index them directly) and ``by_era`` is
    added beside them, keyed by the era as a string with ``"unknown"``
    for NULL. Consumers that want a comparable number read
    ``by_era[str(MIN_COMPARABLE_VERDICT_ERA)]``; consumers that want the
    old pooled number can still have it, now with the evidence that it
    is pooled sitting next to it.
    """
    db_path = _resolve_db_path(db_path)
    init_db(db_path)

    def _empty() -> dict:
        return {
            "kept": 0, "reverted": 0, "neutral": 0,
            "inconclusive": 0, "pending": 0, "total": 0,
        }

    out: dict[str, dict] = {}
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT audit_version, verdict, measurement_era FROM iterations "
            "WHERE deck_id = ?",
            (deck_id,),
        )
        for row in cur.fetchall():
            key = row["audit_version"] or "unknown"
            bucket = out.setdefault(key, {**_empty(), "by_era": {}})
            era_key = (
                str(row["measurement_era"])
                if row["measurement_era"] is not None else "unknown"
            )
            era_bucket = bucket["by_era"].setdefault(era_key, _empty())
            verdict = row["verdict"] or "pending"
            if verdict in era_bucket:
                bucket[verdict] += 1
                era_bucket[verdict] += 1
            bucket["total"] += 1
            era_bucket["total"] += 1
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


# Kept for backwards compatibility; canonical copy lives in dck_utils.
_MAIN_LINE_RE = dck_utils.CARD_LINE_RE


def _count_main_cards(deck_snapshot: Optional[str]) -> int:
    """Count quantity-summed [Main] cards in a .dck snapshot.

    Walks line-by-line; only counts lines inside the [Main] section.
    Commander, sideboard, considering sections are excluded — they
    don't change between iterations in a way that's worth surfacing
    on the graph. Returns 0 for None / empty snapshot.

    Thin wrapper over ``dck_utils.count_main_cards``.
    """
    return dck_utils.count_main_cards(deck_snapshot)


def _parse_main_cards(deck_snapshot: Optional[str]) -> dict:
    """Parse a .dck snapshot's [Main] section into {card_name: quantity}.

    Card names are normalized to their base name (the bit before any `|set|n`
    suffix), so the same card across two snapshots compares equal regardless
    of printing. Quantities are summed. Non-[Main] sections are ignored, to
    match `_count_main_cards`.

    Thin wrapper over ``dck_utils.main_card_quantities``."""
    return dck_utils.main_card_quantities(deck_snapshot)


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
    deck_id: str, db_path: Optional[Path] = None,
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
    db_path = _resolve_db_path(db_path)
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
