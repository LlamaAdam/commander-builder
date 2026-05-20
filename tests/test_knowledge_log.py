"""knowledge_log SQLite store tests.

Each test uses a tmp_path-scoped DB so they don't pollute the real history.
"""
import pytest

from commander_builder.knowledge_log import (
    Iteration,
    get_iteration,
    init_db,
    iterations_for_deck,
    pricing_series_for_deck,
    recent_iterations,
    record_iteration,
    set_milestone,
    stats_summary,
    update_iteration_sim,
    update_verdict,
    verdict_breakdown_for_deck,
)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "test_knowledge_log.sqlite"
    init_db(p)
    return p


def test_init_db_creates_schema(db):
    # Re-running init is idempotent.
    init_db(db)
    init_db(db)
    summary = stats_summary(db_path=db)
    assert summary["total"] == 0


# ---------------------------------------------------------------------------
# update_iteration_sim -- folds A/B-sim outcome into a pending row
# ---------------------------------------------------------------------------

def test_update_iteration_sim_writes_verdict_and_sim_report(db):
    """One atomic UPDATE writes verdict + sim_report + win rates +
    margin together. The pending row becomes a finalized row after a
    successful A/B sim."""
    it = Iteration(
        deck_id="d", deck_name="d", bracket=3,
        audit_manifest={"added": [], "removed": []},
        verdict="pending",
    )
    new_id = record_iteration(it, db_path=db)

    update_iteration_sim(
        iteration_id=new_id,
        verdict="kept",
        sim_report={"wins_a": 1, "wins_b": 3, "games": 4},
        win_rate_old=0.25, win_rate_new=0.75, margin=2,
        notes="A/B sim: new won 3-1",
        db_path=db,
    )

    refreshed = get_iteration(new_id, db_path=db)
    assert refreshed.verdict == "kept"
    assert refreshed.win_rate_old == 0.25
    assert refreshed.win_rate_new == 0.75
    assert refreshed.margin == 2
    assert refreshed.sim_report["wins_b"] == 3
    assert refreshed.verdict_notes == "A/B sim: new won 3-1"


def test_update_iteration_sim_preserves_unset_columns(db):
    """When the sim only produces a verdict (e.g. status=skipped),
    ``sim_report`` / win_rate / margin args are None and the update
    leaves those columns at their pre-existing values rather than
    nulling them out."""
    it = Iteration(
        deck_id="d", deck_name="d", bracket=3,
        win_rate_old=0.4, win_rate_new=0.6, margin=2,
        verdict="pending",
    )
    new_id = record_iteration(it, db_path=db)

    update_iteration_sim(
        iteration_id=new_id,
        verdict="pending",
        notes="sim skipped: no fillers",
        db_path=db,
        # sim_report / win_rate / margin omitted
    )
    refreshed = get_iteration(new_id, db_path=db)
    assert refreshed.verdict == "pending"
    # Original values preserved -- not overwritten with NULL.
    assert refreshed.win_rate_old == 0.4
    assert refreshed.win_rate_new == 0.6
    assert refreshed.margin == 2


def test_update_iteration_sim_rejects_invalid_verdict(db):
    """Same whitelist as update_verdict -- prevents arbitrary strings
    from polluting the column."""
    it = Iteration(
        deck_id="d", deck_name="d", bracket=3, verdict="pending",
    )
    new_id = record_iteration(it, db_path=db)
    with pytest.raises(ValueError, match="verdict must be"):
        update_iteration_sim(
            iteration_id=new_id, verdict="BANANA", db_path=db,
        )


def test_record_and_get_iteration(db):
    it = Iteration(
        deck_id="abc-XYZ",
        deck_name="Test Deck",
        bracket=3,
        audit_version="v3",
        audit_manifest={"added": ["Sol Ring"], "removed": ["Mind Stone"]},
        win_rate_old=0.4,
        win_rate_new=0.55,
        margin=3,
        deck_snapshot="[Main]\n1 Sol Ring",
    )
    new_id = record_iteration(it, db_path=db)
    assert new_id > 0
    assert it.id == new_id

    fetched = get_iteration(new_id, db_path=db)
    assert fetched is not None
    assert fetched.deck_name == "Test Deck"
    assert fetched.audit_manifest == {"added": ["Sol Ring"], "removed": ["Mind Stone"]}
    assert fetched.win_rate_old == 0.4
    assert fetched.win_rate_new == 0.55


def test_iterations_for_deck_returns_oldest_first(db):
    for i in range(3):
        record_iteration(
            Iteration(deck_id="same-deck", deck_name=f"v{i}", bracket=3),
            db_path=db,
        )
    record_iteration(
        Iteration(deck_id="other-deck", deck_name="other", bracket=4),
        db_path=db,
    )

    same = iterations_for_deck("same-deck", db_path=db)
    assert [it.deck_name for it in same] == ["v0", "v1", "v2"]
    other = iterations_for_deck("other-deck", db_path=db)
    assert len(other) == 1


def test_recent_iterations_returns_newest_first(db):
    for i in range(5):
        record_iteration(
            Iteration(deck_id=f"deck-{i}", deck_name=f"d{i}", bracket=3),
            db_path=db,
        )
    recent = recent_iterations(limit=3, db_path=db)
    assert [it.deck_name for it in recent] == ["d4", "d3", "d2"]


def test_update_verdict_persists(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    update_verdict(new_id, "kept", "win rate +12% over 20 games", db_path=db)

    fetched = get_iteration(new_id, db_path=db)
    assert fetched.verdict == "kept"
    assert fetched.verdict_notes == "win rate +12% over 20 games"


def test_update_verdict_rejects_invalid_value(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    with pytest.raises(ValueError):
        update_verdict(new_id, "garbage", db_path=db)


def test_default_verdict_is_pending(db):
    it = Iteration(deck_id="d", deck_name="Test", bracket=3)
    new_id = record_iteration(it, db_path=db)
    fetched = get_iteration(new_id, db_path=db)
    assert fetched.verdict == "pending"


def test_stats_summary_counts_each_verdict(db):
    for _ in range(2):
        rid = record_iteration(Iteration(deck_id="d", deck_name="x", bracket=3), db_path=db)
        update_verdict(rid, "kept", db_path=db)
    rid = record_iteration(Iteration(deck_id="d", deck_name="x", bracket=3), db_path=db)
    update_verdict(rid, "reverted", db_path=db)
    record_iteration(Iteration(deck_id="d2", deck_name="x", bracket=3), db_path=db)  # pending

    s = stats_summary(db_path=db)
    assert s["total"] == 4
    assert s["kept"] == 2
    assert s["reverted"] == 1
    assert s["pending"] == 1
    assert s["unique_decks"] == 2


def test_get_iteration_returns_none_for_missing(db):
    assert get_iteration(99999, db_path=db) is None


def test_iteration_round_trips_complex_manifest(db):
    manifest = {
        "added": ["Foo", "Bar"],
        "removed": ["Baz"],
        "rationale": "Tightened removal package; replaced redundant counterspell",
        "step_4_5_sweep_catches": ["Quux"],
    }
    sim_report = {
        "old_wins": 4, "new_wins": 7, "draws": 9, "total_games": 20,
        "card_diff": {"added": ["Foo", "Bar"], "removed": ["Baz"]},
    }
    rid = record_iteration(
        Iteration(
            deck_id="round-trip-test",
            deck_name="RT Deck",
            bracket=4,
            audit_manifest=manifest,
            sim_report=sim_report,
        ),
        db_path=db,
    )
    fetched = get_iteration(rid, db_path=db)
    assert fetched.audit_manifest == manifest
    assert fetched.sim_report == sim_report


def test_parent_id_chain(db):
    """A v2 iteration points back to its v1 parent. Used by Phase 3 to
    reconstruct the full lineage of each deck for training data."""
    v1_id = record_iteration(
        Iteration(deck_id="lineage", deck_name="v1", bracket=3),
        db_path=db,
    )
    v2_id = record_iteration(
        Iteration(deck_id="lineage", deck_name="v2", bracket=3, parent_id=v1_id),
        db_path=db,
    )
    fetched = get_iteration(v2_id, db_path=db)
    assert fetched.parent_id == v1_id


# --- migrate_legacy_deck_ids -----------------------------------------------

def test_migrate_legacy_deck_ids_updates_filename_style_rows(db):
    """GAP-024: rows with deck_id like '[USER] Foo [B3].dck' should be
    rewritten to use the publicId from the snapshot's Moxfield= line."""
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    snapshot = (
        "[metadata]\n"
        "Name=Foo\n"
        "Moxfield=abc-XYZ\n"
        "[Commander]\n1 Test\n"
    )
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Foo [B3].dck",  # legacy filename-style id
            deck_name="[USER] Foo [B3].dck",
            bracket=3,
            deck_snapshot=snapshot,
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["scanned"] == 1
    assert result["updated"] == 1
    assert result["details"][0]["new_deck_id"] == "abc-XYZ"

    fetched = get_iteration(rid, db_path=db)
    assert fetched.deck_id == "abc-XYZ"


def test_migrate_dry_run_reports_without_writing(db):
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Foo [B3].dck",
            deck_name="x",
            bracket=3,
            deck_snapshot="[metadata]\nMoxfield=abc-XYZ\n[Main]\n1 X",
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db, dry_run=True)
    assert result["dry_run"] is True
    assert result["would_update"] == 1
    assert result["updated"] == 0  # no actual writes

    # Row unchanged on disk.
    assert get_iteration(rid, db_path=db).deck_id == "[USER] Foo [B3].dck"


def test_migrate_skips_rows_without_moxfield_metadata(db):
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    rid = record_iteration(
        Iteration(
            deck_id="[USER] Old Deck [B3].dck",
            deck_name="x",
            bracket=3,
            deck_snapshot="[Commander]\n1 Test\n",  # No Moxfield= line
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["updated"] == 0
    assert len(result["skipped"]) == 1
    assert "no Moxfield" in result["skipped"][0]["reason"]
    assert get_iteration(rid, db_path=db).deck_id == "[USER] Old Deck [B3].dck"


def test_migrate_leaves_already_migrated_rows_alone(db):
    """Rows whose deck_id is already a publicId (no `.dck` suffix) shouldn't
    be touched."""
    from commander_builder.knowledge_log import migrate_legacy_deck_ids
    record_iteration(
        Iteration(
            deck_id="abc-123",  # already publicId-style
            deck_name="x",
            bracket=3,
            deck_snapshot="Moxfield=def-456",
        ),
        db_path=db,
    )
    result = migrate_legacy_deck_ids(db_path=db)
    assert result["updated"] == 0
    assert result["details"] == []


# ---------------------------------------------------------------------------
# verdict_breakdown_for_deck — per-audit-version verdict ratios
# ---------------------------------------------------------------------------
# Backlog item #6: when the user has ≥5 iterations for a deck, the UI
# should show "kept verdict in 4/5 v3 swaps, kept in 2/3 v4 swaps."
# Group iterations by audit_version, count each verdict per group.


def test_verdict_breakdown_empty_deck_returns_empty(db):
    """No iterations → empty dict, not an error."""
    out = verdict_breakdown_for_deck("never-saved", db_path=db)
    assert out == {}


def test_verdict_breakdown_groups_by_audit_version(db):
    """Two audit versions, mixed verdicts. Each group reports its own
    {kept, reverted, neutral, pending, total}."""
    for _ in range(4):
        record_iteration(
            Iteration(deck_id="d1", deck_name="d1", bracket=3,
                      audit_version="v3", verdict="kept"),
            db_path=db,
        )
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_version="v3", verdict="reverted"),
        db_path=db,
    )
    for _ in range(2):
        record_iteration(
            Iteration(deck_id="d1", deck_name="d1", bracket=3,
                      audit_version="v4", verdict="kept"),
            db_path=db,
        )
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_version="v4", verdict="reverted"),
        db_path=db,
    )

    out = verdict_breakdown_for_deck("d1", db_path=db)
    assert out["v3"]["kept"] == 4
    assert out["v3"]["reverted"] == 1
    assert out["v3"]["total"] == 5
    assert out["v4"]["kept"] == 2
    assert out["v4"]["reverted"] == 1
    assert out["v4"]["total"] == 3


def test_verdict_breakdown_unknown_audit_version_bucketed_as_unknown(db):
    """A row with NULL audit_version (legacy import, missing manifest)
    gets bucketed as 'unknown' instead of crashing the report."""
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_version=None, verdict="kept"),
        db_path=db,
    )
    out = verdict_breakdown_for_deck("d1", db_path=db)
    assert "unknown" in out
    assert out["unknown"]["kept"] == 1


def test_verdict_breakdown_scoped_to_one_deck(db):
    """Iterations under a different deck_id don't leak into the report."""
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_version="v3", verdict="kept"),
        db_path=db,
    )
    record_iteration(
        Iteration(deck_id="d2", deck_name="d2", bracket=3,
                  audit_version="v3", verdict="reverted"),
        db_path=db,
    )
    out = verdict_breakdown_for_deck("d1", db_path=db)
    assert out["v3"]["kept"] == 1
    assert out["v3"]["reverted"] == 0  # d2's row excluded


def test_pricing_series_returns_empty_for_no_iterations(db):
    """No iterations → empty list. Caller renders 'no data' state."""
    assert pricing_series_for_deck("no-such-deck", db_path=db) == []


def test_pricing_series_extracts_from_audit_manifest_pricing(db):
    """Each iteration with audit_manifest.pricing.total_price_usd
    contributes a point ordered by iteration id (chronological)."""
    record_iteration(
        Iteration(
            deck_id="d1", deck_name="d1", bracket=3,
            audit_version="v3", verdict="pending",
            audit_manifest={"pricing": {
                "total_price_usd": 142.37,
                "captured_at": "2026-05-13T20:04:00+00:00",
            }},
        ),
        db_path=db,
    )
    record_iteration(
        Iteration(
            deck_id="d1", deck_name="d1", bracket=3,
            audit_version="v3", verdict="kept",
            audit_manifest={"pricing": {
                "total_price_usd": 95.00,
                "captured_at": "2026-05-14T20:04:00+00:00",
            }},
        ),
        db_path=db,
    )
    series = pricing_series_for_deck("d1", db_path=db)
    assert len(series) == 2
    assert series[0]["total_price_usd"] == 142.37
    assert series[0]["captured_at"] == "2026-05-13T20:04:00+00:00"
    assert series[1]["total_price_usd"] == 95.00
    # Each point also carries iteration_id so the UI can link back to
    # the row for inspection.
    assert isinstance(series[0]["iteration_id"], int)


def test_pricing_series_skips_iterations_without_pricing(db):
    """An iteration with no audit_manifest or no pricing block doesn't
    contribute a point — the chart only shows data points we actually
    captured."""
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_manifest=None),  # no manifest
        db_path=db,
    )
    record_iteration(
        Iteration(
            deck_id="d1", deck_name="d1", bracket=3,
            audit_manifest={"added": [], "removed": []},  # no pricing key
        ),
        db_path=db,
    )
    record_iteration(
        Iteration(
            deck_id="d1", deck_name="d1", bracket=3,
            audit_manifest={"pricing": {
                "total_price_usd": 50.0,
                "captured_at": "2026-05-15T00:00:00+00:00",
            }},
        ),
        db_path=db,
    )
    series = pricing_series_for_deck("d1", db_path=db)
    assert len(series) == 1
    assert series[0]["total_price_usd"] == 50.0


def test_pricing_series_scoped_to_one_deck(db):
    """Other decks' pricing data must not leak into this deck's series."""
    record_iteration(
        Iteration(
            deck_id="d1", deck_name="d1", bracket=3,
            audit_manifest={"pricing": {
                "total_price_usd": 100.0,
                "captured_at": "2026-05-13T00:00:00+00:00",
            }},
        ),
        db_path=db,
    )
    record_iteration(
        Iteration(
            deck_id="d2", deck_name="d2", bracket=3,
            audit_manifest={"pricing": {
                "total_price_usd": 999.0,
                "captured_at": "2026-05-13T00:00:00+00:00",
            }},
        ),
        db_path=db,
    )
    series = pricing_series_for_deck("d1", db_path=db)
    assert len(series) == 1
    assert series[0]["total_price_usd"] == 100.0


def test_verdict_breakdown_includes_all_verdicts_zeroed(db):
    """Every group reports counts for all four verdicts (zero-padded)
    so the UI doesn't have to guard against KeyError when displaying
    'kept / reverted / neutral / pending'."""
    record_iteration(
        Iteration(deck_id="d1", deck_name="d1", bracket=3,
                  audit_version="v3", verdict="kept"),
        db_path=db,
    )
    out = verdict_breakdown_for_deck("d1", db_path=db)
    bucket = out["v3"]
    assert bucket["kept"] == 1
    assert bucket["reverted"] == 0
    assert bucket["neutral"] == 0
    assert bucket["pending"] == 0
    assert bucket["total"] == 1


# ---------------------------------------------------------------------------
# milestone column (schema v2 / AGENT_BACKLOG #012)
# ---------------------------------------------------------------------------

def _add_row(db, deck_id="d"):
    """Insert a minimal iteration row and return its id."""
    it = Iteration(
        deck_id=deck_id, deck_name=deck_id, bracket=3,
        verdict="pending",
    )
    return record_iteration(it, db_path=db)


def test_set_milestone_writes_label(db):
    """Happy path: set_milestone tags an existing iteration with
    the given label, readable on the next get_iteration."""
    it_id = _add_row(db)
    set_milestone(it_id, "baseline", db_path=db)
    row = get_iteration(it_id, db_path=db)
    assert row.milestone == "baseline"


def test_set_milestone_truncates_long_labels(db):
    """Labels longer than 64 chars truncate silently — guards
    against accidental novella-length pastes from the UI."""
    it_id = _add_row(db)
    set_milestone(it_id, "x" * 200, db_path=db)
    row = get_iteration(it_id, db_path=db)
    assert row.milestone is not None
    assert len(row.milestone) == 64


def test_set_milestone_strips_whitespace(db):
    it_id = _add_row(db)
    set_milestone(it_id, "   spaced   ", db_path=db)
    row = get_iteration(it_id, db_path=db)
    assert row.milestone == "spaced"


def test_set_milestone_none_clears_label(db):
    """Passing None clears the milestone — the UI's "untag" action."""
    it_id = _add_row(db)
    set_milestone(it_id, "baseline", db_path=db)
    set_milestone(it_id, None, db_path=db)
    row = get_iteration(it_id, db_path=db)
    assert row.milestone is None


def test_set_milestone_empty_string_clears_label(db):
    """Whitespace-only labels also clear — same as passing None."""
    it_id = _add_row(db)
    set_milestone(it_id, "baseline", db_path=db)
    set_milestone(it_id, "   ", db_path=db)
    row = get_iteration(it_id, db_path=db)
    assert row.milestone is None


def test_set_milestone_unknown_id_is_silent(db):
    """Following the ``update_verdict`` fail-quiet pattern: an
    UPDATE against a missing id is a no-op, not an exception.
    The web layer relies on this for optimistic UI updates."""
    set_milestone(9_999_999, "anything", db_path=db)  # must not raise


def test_record_iteration_persists_milestone_via_dataclass(db):
    """If a caller constructs an Iteration with milestone preset,
    record_iteration persists it through the INSERT path."""
    it = Iteration(
        deck_id="x", deck_name="x", bracket=3,
        milestone="initial-snapshot",
    )
    new_id = record_iteration(it, db_path=db)
    row = get_iteration(new_id, db_path=db)
    assert row.milestone == "initial-snapshot"


def test_init_db_migrates_v1_to_v2_adds_milestone_column(tmp_path):
    """Pre-migration: create a v1-shaped database by hand (old
    schema without the milestone column + schema_version=1).
    Running init_db must add the column non-destructively and bump
    the recorded version."""
    import sqlite3
    p = tmp_path / "legacy_v1.sqlite"
    # Hand-build a v1 database.
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
        CREATE TABLE iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id TEXT NOT NULL, deck_name TEXT NOT NULL,
            bracket INTEGER NOT NULL, parent_id INTEGER,
            audit_version TEXT, audit_manifest TEXT, sim_report TEXT,
            verdict TEXT NOT NULL DEFAULT 'pending', verdict_notes TEXT,
            win_rate_old REAL, win_rate_new REAL, margin INTEGER,
            created_at TEXT NOT NULL, deck_snapshot TEXT
        );
        INSERT INTO schema_version (version) VALUES (1);
        INSERT INTO iterations (deck_id, deck_name, bracket, created_at, verdict)
        VALUES ('legacy', 'legacy', 3, '2026-01-01', 'kept');
    """)
    conn.commit()
    conn.close()

    # Run the migration via init_db.
    init_db(p)

    # Confirm: column added, existing row preserved, version bumped.
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("PRAGMA table_info(iterations)")
    cols = {r["name"] for r in cur.fetchall()}
    assert "milestone" in cols
    cur = conn.execute("SELECT * FROM iterations WHERE deck_id = 'legacy'")
    row = cur.fetchone()
    assert row["verdict"] == "kept"   # legacy row intact
    assert row["milestone"] is None   # new column NULL by default
    cur = conn.execute("SELECT version FROM schema_version")
    assert cur.fetchone()["version"] == 2
    conn.close()


def test_init_db_migration_is_idempotent(tmp_path):
    """Running init_db twice on a freshly-migrated v2 database is
    a no-op — the migration's column-add is guarded by a pragma
    check, and the version update is a single-row update."""
    p = tmp_path / "idempotent.sqlite"
    init_db(p)
    init_db(p)  # second call should not raise
    it_id = _add_row(p)
    set_milestone(it_id, "still-works", db_path=p)
    row = get_iteration(it_id, db_path=p)
    assert row.milestone == "still-works"
