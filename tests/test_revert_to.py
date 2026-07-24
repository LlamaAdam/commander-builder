"""revert_to tests with isolated knowledge_log DBs."""
import re
from pathlib import Path

import pytest

from commander_builder.knowledge_log import (
    Iteration,
    iterations_for_deck,
    record_iteration,
)
from commander_builder.revert_to import (
    revert_deck_to_version,
    revert_to_iteration,
)


_SNAPSHOT_V1 = "\n".join([
    "[metadata]",
    "Name=Test Deck",
    "Moxfield=stable-id",
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 Old Card",
])

_SNAPSHOT_V2 = "\n".join([
    "[metadata]",
    "Name=Test Deck",
    "Moxfield=stable-id",
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 New Card",
])


def _seed_two_iterations(db: Path) -> tuple[int, int]:
    v1 = record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            audit_version="v3", audit_manifest={"added": [], "removed": []},
            sim_report={"total_games": 0}, verdict="kept",
            deck_snapshot=_SNAPSHOT_V1,
        ), db_path=db,
    )
    v2 = record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            parent_id=v1, audit_version="v3",
            audit_manifest={"added": ["New Card"], "removed": ["Old Card"]},
            sim_report={"total_games": 10},
            verdict="reverted", verdict_notes="bad swap",
            deck_snapshot=_SNAPSHOT_V2,
        ), db_path=db,
    )
    return v1, v2


def test_revert_to_iteration_writes_snapshot_to_disk(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)

    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text("contains v2 content", encoding="utf-8")  # current state

    result = revert_to_iteration(v1, deck_path=target, db_path=db)
    assert result.iteration_id == v1
    assert "Old Card" in target.read_text(encoding="utf-8")
    assert "Moxfield=stable-id" in target.read_text(encoding="utf-8")
    # Push blob is the textarea form.
    assert "1 Sol Ring" in result.push_blob


def test_revert_records_revert_iteration_by_default(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text("v2", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db)
    assert result.revert_iteration_id is not None
    history = iterations_for_deck("stable-id", db_path=db)
    assert len(history) == 3   # v1, v2, revert-record
    assert history[-1].audit_version == "revert"
    assert history[-1].audit_manifest["reverted_to_iteration_id"] == v1


def test_revert_skip_record_flag(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    result = revert_to_iteration(v1, deck_path=target, db_path=db, record_revert=False)
    assert result.revert_iteration_id is None
    history = iterations_for_deck("stable-id", db_path=db)
    assert len(history) == 2  # no extra row


def test_revert_to_missing_iteration_raises(tmp_path):
    db = tmp_path / "kl.sqlite"
    with pytest.raises(ValueError):
        revert_to_iteration(99999, deck_path=tmp_path / "x.dck", db_path=db)


def test_revert_to_iteration_without_snapshot_raises(tmp_path):
    db = tmp_path / "kl.sqlite"
    rid = record_iteration(
        Iteration(
            deck_id="d", deck_name="x", bracket=3,
            audit_manifest={"added": [], "removed": []},
            verdict="kept", deck_snapshot=None,
        ), db_path=db,
    )
    with pytest.raises(ValueError, match="no deck_snapshot"):
        revert_to_iteration(rid, deck_path=tmp_path / "x.dck", db_path=db)


def test_revert_deck_to_version_picks_correct_iteration(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    result = revert_deck_to_version(
        "stable-id", version=1, deck_path=target, db_path=db, record_revert=False,
    )
    assert result.iteration_id == v1


def test_revert_deck_to_version_out_of_range(tmp_path):
    db = tmp_path / "kl.sqlite"
    _seed_two_iterations(db)
    with pytest.raises(ValueError, match="out of range"):
        revert_deck_to_version(
            "stable-id", version=99, deck_path=tmp_path / "x.dck",
            db_path=db, record_revert=False,
        )


def test_revert_deck_to_version_unknown_deck(tmp_path):
    db = tmp_path / "kl.sqlite"
    with pytest.raises(ValueError, match="no iteration history"):
        revert_deck_to_version(
            "nonexistent", version=1, deck_path=tmp_path / "x.dck",
            db_path=db, record_revert=False,
        )


# --- pre-revert backup ------------------------------------------------------
# revert overwrites the live .dck with a knowledge_log snapshot. If the
# on-disk content was never recorded (manual edit, out-of-band re-pull), the
# overwrite is the ONLY copy being destroyed — these tests pin the copy-aside
# safety net.


def test_revert_backs_up_previous_content(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # Content that exists nowhere in the knowledge log — the unrecoverable case.
    target.write_text("manual edit never recorded", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)

    # Deck file now holds the snapshot...
    assert "Old Card" in target.read_text(encoding="utf-8")
    # ...and the exact pre-revert content survived in the backup.
    assert result.backup_path is not None
    assert result.backup_path.parent == target.parent
    assert (result.backup_path.read_text(encoding="utf-8")
            == "manual edit never recorded")
    # Structured output carries it too (as a string, like restored_path).
    assert result.to_dict()["backup_path"] == str(result.backup_path)


def test_backup_filename_invisible_to_deck_listing_filters(tmp_path):
    """Every deck-listing consumer globs *.dck then (for bracket-aware ones)
    filters on the ` [B<n>].dck` suffix. The backup must match NEITHER, or it
    would show up as a playable deck / pollute counts."""
    from commander_builder.moxfield_import import _existing_moxfield_ids
    from commander_builder.status import _count_decks

    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # Pre-revert content WITH deck-shaped metadata, so if a filter ever did
    # pick the backup up, the assertions below would genuinely fail.
    target.write_text(_SNAPSHOT_V2 + "\n1 Extra Card", encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)
    backup = result.backup_path
    assert backup is not None

    # The universal first-stage filter: pathlib glob("*.dck").
    assert backup not in set(tmp_path.glob("*.dck"))
    assert not backup.name.endswith(".dck")
    assert not backup.name.endswith("[B3].dck")

    # Prove it against the real helpers: with the deck file gone, the backup
    # alone must contribute nothing to deck counts or Moxfield-id dedupe
    # (its content DOES contain `Moxfield=stable-id`).
    target.unlink()
    assert _count_decks(tmp_path) == {}
    # (id → path dict since the same-id-anywhere fix; empty either way.)
    assert _existing_moxfield_ids(tmp_path, 3) == {}


def test_identical_content_revert_skips_backup(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # "Already at v1 state" means: identical to what the revert will WRITE —
    # which since the restamp fix is the snapshot with Name= rewritten to
    # the destination stem, not the raw snapshot bytes. (A real pre-revert
    # file always carries Name=<its own stem>: every writer stamps it.)
    from commander_builder.dck_meta import rewrite_name
    target.write_text(rewrite_name(_SNAPSHOT_V1, target.stem),
                      encoding="utf-8")

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)

    # Nothing would have been lost by the overwrite → no backup file created.
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []
    assert result.to_dict()["backup_path"] is None


def test_backup_when_deck_file_absent_is_skipped(tmp_path):
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"  # never written

    result = revert_to_iteration(v1, deck_path=target, db_path=db,
                                 record_revert=False)
    assert result.backup_path is None
    assert list(tmp_path.glob("*.bak")) == []


def test_back_to_back_reverts_produce_distinct_backups(tmp_path):
    """Two reverts inside the same wall-clock second must not collide on the
    timestamped backup name — the second would silently overwrite the first,
    which is the exact failure mode the backup exists to prevent."""
    db = tmp_path / "kl.sqlite"
    v1, v2 = _seed_two_iterations(db)
    target = tmp_path / "[USER] Test [B3].dck"

    target.write_text("manual state A", encoding="utf-8")
    r1 = revert_to_iteration(v1, deck_path=target, db_path=db,
                             record_revert=False)
    target.write_text("manual state B", encoding="utf-8")
    r2 = revert_to_iteration(v1, deck_path=target, db_path=db,
                             record_revert=False)

    assert r1.backup_path is not None and r2.backup_path is not None
    assert r1.backup_path != r2.backup_path
    # BOTH pre-revert states survived, in the right files.
    assert r1.backup_path.read_text(encoding="utf-8") == "manual state A"
    assert r2.backup_path.read_text(encoding="utf-8") == "manual state B"


def test_cli_prints_backup_path(tmp_path, monkeypatch, capsys):
    """The backup is only useful if the user is told where it is — pin the
    CLI line without needing a real DB/clipboard behind main()."""
    from commander_builder import revert_to as rt

    fake = rt.RevertResult(
        iteration_id=7,
        restored_path=tmp_path / "deck.dck",
        push_blob="",
        backup_path=tmp_path / "deck.pre-revert-20260719_120000.dck.bak",
    )
    monkeypatch.setattr(rt, "revert_to_iteration",
                        lambda *a, **k: fake)
    monkeypatch.setattr(rt, "prepare_push", lambda *a, **k: None)

    assert rt.main(["--to-iteration", "7", "--no-clipboard"]) == 0
    out = capsys.readouterr().out
    assert str(fake.backup_path) in out

    # And the "nothing to back up" case says so explicitly.
    fake.backup_path = None
    assert rt.main(["--to-iteration", "7", "--no-clipboard"]) == 0
    out = capsys.readouterr().out
    assert "No backup needed" in out


# --- Name= restamp on restore ----------------------------------------------
# iteration_loop records deck_snapshot by READING the on-disk v2 file, which
# (dck_meta invariant: every writer stamps Name=<its own filename stem>)
# carries Name=<v2 stem>, e.g. "Name=[USER] Test v2 [B3]". Writing that blob
# verbatim over the BASE filename re-created the exact mismatch dck_meta
# exists to fix: Forge reports "Test v2", run_match/compare_versions key on
# _normalize("[USER] Test [B3]") → the reverted deck scores 0 wins and every
# decisive game books as a loss. The revert writer must restamp Name= to the
# DESTINATION stem — and leave a real DisplayName= alone.

_SNAPSHOT_V2_STAMPED = "\n".join([
    "[metadata]",
    "Name=[USER] Test v2 [B3]",  # stem of the v2 FILE the snapshot came from
    "DisplayName=Test Deck",     # pretty name — must survive the restamp
    "Moxfield=stable-id",
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 Old Card",
])


def _seed_v2_stamped_iteration(db: Path) -> int:
    """One iteration whose snapshot carries a versioned-stem Name= — the
    shape iteration_loop actually records."""
    return record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            audit_version="v3", audit_manifest={"added": [], "removed": []},
            verdict="kept", deck_snapshot=_SNAPSHOT_V2_STAMPED,
        ), db_path=db,
    )


def test_revert_restamps_name_to_destination_stem(tmp_path):
    from commander_builder.log_parser import _normalize

    db = tmp_path / "kl.sqlite"
    rid = _seed_v2_stamped_iteration(db)
    target = tmp_path / "[USER] Test [B3].dck"
    # Un-logged live content: the backup must still happen (ordering: backup
    # BEFORE the restamped write).
    target.write_text("live content never recorded", encoding="utf-8")

    result = revert_to_iteration(rid, deck_path=target, db_path=db)

    text = target.read_text(encoding="utf-8")
    # Exactly one Name= line, equal to the DESTINATION stem — not the v2
    # stem the snapshot arrived with.
    name_values = re.findall(r"^Name=(.*)$", text, re.MULTILINE)
    assert name_values == [target.stem]
    # The invariant every name-keyed consumer relies on: the filename and
    # the written Name= normalize to the same attribution key.
    assert _normalize(target.name) == _normalize(name_values[0])
    # Real DisplayName= passes through untouched — no duplicate, no
    # synthesized junk from the discarded v2 machine stem.
    assert re.findall(r"^DisplayName=(.*)$", text, re.MULTILINE) == ["Test Deck"]
    # Deck content itself untouched by the restamp.
    assert "1 Old Card" in text and "Moxfield=stable-id" in text
    # Pre-revert backup still created, with the pre-revert bytes.
    assert result.backup_path is not None
    assert (result.backup_path.read_text(encoding="utf-8")
            == "live content never recorded")
    # The revert record snapshots what's ON DISK (restamped), so a later
    # revert TO the revert record restores identical bytes.
    history = iterations_for_deck("stable-id", db_path=db)
    assert history[-1].audit_version == "revert"
    assert history[-1].deck_snapshot == text


def test_revert_deck_to_version_also_restamps(tmp_path):
    """The by-version entry point delegates to revert_to_iteration — pin
    that the restamp holds through it too."""
    db = tmp_path / "kl.sqlite"
    _seed_v2_stamped_iteration(db)
    target = tmp_path / "[USER] Test [B3].dck"

    revert_deck_to_version("stable-id", version=1, deck_path=target,
                           db_path=db, record_revert=False)

    text = target.read_text(encoding="utf-8")
    assert re.findall(r"^Name=(.*)$", text, re.MULTILINE) == [target.stem]
    assert "DisplayName=Test Deck" in text


# --- resolve-by-id when bracket drift renamed the deck ----------------------
# moxfield_import._rename_for_bracket_drift renames "Foo [B3].dck" ->
# "Foo [B4].dck" when a deck's Moxfield bracket changes on a re-pull. An
# iteration recorded BEFORE that rename still carries the old deck_name. Naive
# by-name reverts rewrite the stale "[B3]" filename, leaving TWO same-role
# files claiming one Moxfield= id — the exact ambiguity the id map warns about
# and bracket filters double-count on. The revert must resolve by Moxfield id
# and restore into the RENAMED (live) file instead.

# Snapshot as iteration_loop recorded it PRE-drift: Name= is the old [B3] stem
# it was read from, and it carries the stable Moxfield= id.
_SNAPSHOT_PRE_DRIFT = "\n".join([
    "[metadata]",
    "Name=[USER] Test [B3]",       # stale stem — the deck was at B3 back then
    "Moxfield=stable-id",          # the identity that survives the rename
    "[Commander]",
    "1 Test Commander",
    "[Main]",
    "1 Sol Ring",
    "1 Old Card",
])


def _seed_pre_drift_iteration(db: Path) -> int:
    return record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            audit_version="v3", audit_manifest={"added": [], "removed": []},
            verdict="kept", deck_snapshot=_SNAPSHOT_PRE_DRIFT,
        ), db_path=db,
    )


def test_revert_after_bracket_drift_restores_into_renamed_file(tmp_path):
    """Deck was re-pulled at a new bracket and renamed [B3] -> [B4] since the
    iteration was recorded. Reverting the pre-drift iteration must land in the
    LIVE [B4] file (found by Moxfield id), not re-create the stale [B3] name."""
    from commander_builder.log_parser import _normalize

    db = tmp_path / "kl.sqlite"
    rid = _seed_pre_drift_iteration(db)

    # The live, post-rename file — same Moxfield id, new bracket tag, holding
    # some current on-disk content that is NOT in the knowledge log.
    renamed = tmp_path / "[USER] Test [B4].dck"
    renamed.write_text("\n".join([
        "[metadata]",
        "Name=[USER] Test [B4]",
        "Moxfield=stable-id",
        "[Main]",
        "1 Live Card",
    ]) + "\n", encoding="utf-8")

    # The old name the iteration recorded — no longer on disk after the rename.
    stale = tmp_path / "[USER] Test [B3].dck"
    assert not stale.exists()

    result = revert_to_iteration(rid, deck_path=stale, db_path=db,
                                 record_revert=False)

    # Restored into the RENAMED file, not the stale name.
    assert result.restored_path == renamed
    text = renamed.read_text(encoding="utf-8")
    assert "1 Old Card" in text          # snapshot content landed here
    assert "Moxfield=stable-id" in text
    # NO stale [B3] file was resurrected — that's the whole double-count bug.
    assert not stale.exists()
    assert set(p.name for p in tmp_path.glob("*.dck")) == {"[USER] Test [B4].dck"}
    # Name= restamped to the LIVE stem so the dck_meta invariant holds for the
    # renamed file (not the stale [B3] stem the snapshot arrived with).
    name_values = re.findall(r"^Name=(.*)$", text, re.MULTILINE)
    assert name_values == [renamed.stem]
    assert _normalize(renamed.name) == _normalize(name_values[0])
    # Pre-revert .bak of whatever we overwrote (the live [B4] content).
    assert result.backup_path is not None
    assert result.backup_path.parent == renamed.parent
    assert "1 Live Card" in result.backup_path.read_text(encoding="utf-8")


def test_revert_drift_resolution_targets_base_not_frozen_v2(tmp_path, capsys):
    """Version-lineage rule inside the revert's drift resolution: the live
    (drift-renamed) base AND a stale-named frozen v2 snapshot both record
    the deck's Moxfield= id — by design, the version writers preserve
    metadata. The id lookup must resolve to the BASE ([B4]), restore into
    it, leave the frozen snapshot byte-identical, and emit NO duplicate-id
    WARN (base + v<N> is a lineage, not an ambiguity)."""
    db = tmp_path / "kl.sqlite"
    rid = _seed_pre_drift_iteration(db)

    # The live, post-drift-rename base.
    renamed = tmp_path / "[USER] Test [B4].dck"
    renamed.write_text("\n".join([
        "[metadata]", "Name=[USER] Test [B4]", "Moxfield=stable-id",
        "[Main]", "1 Live Card",
    ]) + "\n", encoding="utf-8")
    # Frozen v2 snapshot from before the drift rename: OLD [B3] stem (drift
    # renames only the live file), same id.
    frozen = tmp_path / "[USER] Test v2 [B3].dck"
    frozen.write_text("\n".join([
        "[metadata]", "Name=[USER] Test v2 [B3]", "Moxfield=stable-id",
        "[Main]", "1 Frozen Card",
    ]) + "\n", encoding="utf-8")
    frozen_before = frozen.read_text(encoding="utf-8")

    stale = tmp_path / "[USER] Test [B3].dck"  # recorded name; not on disk
    result = revert_to_iteration(rid, deck_path=stale, db_path=db,
                                 record_revert=False)

    assert result.restored_path == renamed  # the base, never the snapshot
    assert "1 Old Card" in renamed.read_text(encoding="utf-8")
    assert frozen.read_text(encoding="utf-8") == frozen_before
    assert not stale.exists()
    # The lineage pair must not have tripped the duplicate-id ambiguity
    # WARN during the id-map build.
    assert "WARN" not in capsys.readouterr().out


def test_revert_no_drift_common_case_restores_by_name(tmp_path):
    """No rename happened: the recorded name still owns the id. The id lookup
    resolves to the SAME file, so the by-name path is taken unchanged (no
    redirect, no surprise second file)."""
    db = tmp_path / "kl.sqlite"
    rid = _seed_pre_drift_iteration(db)

    # On-disk file at the recorded name, carrying the same id (no drift).
    target = tmp_path / "[USER] Test [B3].dck"
    target.write_text("\n".join([
        "[metadata]", "Name=[USER] Test [B3]", "Moxfield=stable-id",
        "[Main]", "1 Current Card",
    ]) + "\n", encoding="utf-8")

    result = revert_to_iteration(rid, deck_path=target, db_path=db,
                                 record_revert=False)

    assert result.restored_path == target
    text = target.read_text(encoding="utf-8")
    assert "1 Old Card" in text
    assert re.findall(r"^Name=(.*)$", text, re.MULTILINE) == [target.stem]
    # Exactly one deck file — the redirect never fired.
    assert set(p.name for p in tmp_path.glob("*.dck")) == {"[USER] Test [B3].dck"}


def test_revert_snapshot_without_moxfield_id_falls_back_to_by_name(tmp_path):
    """A snapshot with no Moxfield= line can't be resolved by id — the revert
    must fall back to the by-name behavior, restoring into the given path even
    when an unrelated same-bracket file happens to sit alongside it."""
    db = tmp_path / "kl.sqlite"
    no_id_snapshot = "\n".join([
        "[metadata]", "Name=[USER] Test [B3]",   # NO Moxfield= line
        "[Commander]", "1 Test Commander",
        "[Main]", "1 Sol Ring", "1 Old Card",
    ])
    rid = record_iteration(
        Iteration(
            deck_id="stable-id", deck_name="[USER] Test [B3].dck", bracket=3,
            audit_version="v3", audit_manifest={"added": [], "removed": []},
            verdict="kept", deck_snapshot=no_id_snapshot,
        ), db_path=db,
    )

    # A same-role file carrying an id sits nearby — it must NOT be hijacked as
    # the target, because the snapshot offers no id to match it against.
    other = tmp_path / "[USER] Test [B4].dck"
    other.write_text("\n".join([
        "[metadata]", "Name=[USER] Test [B4]", "Moxfield=some-id",
        "[Main]", "1 Other Card",
    ]) + "\n", encoding="utf-8")

    target = tmp_path / "[USER] Test [B3].dck"
    result = revert_to_iteration(rid, deck_path=target, db_path=db,
                                 record_revert=False)

    # Restored by name into the given path; the nearby file is untouched.
    assert result.restored_path == target
    assert "1 Old Card" in target.read_text(encoding="utf-8")
    assert "1 Other Card" in other.read_text(encoding="utf-8")
