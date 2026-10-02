"""iteration_loop unit tests.

The deterministic helper `resolve_deck_id` is unit-tested directly. The
orchestrator `run_one_iteration` hits Forge subprocess via
`compare_versions.compare`; we mock at that boundary so the test stays
offline while still exercising the wiring (compare → analyst → knowledge_log).
"""
from dataclasses import dataclass, field
from typing import Any

import pytest

from commander_builder.compare_versions import ComparisonReport, VersionStats
from commander_builder.iteration_loop import (
    _materialize_proposed_deck,
    main as iteration_loop_main,
    propose_then_iterate,
    resolve_deck_id,
    run_one_iteration,
)
from commander_builder.knowledge_log import (
    get_iteration,
    iterations_for_deck,
    stats_summary,
)
from commander_builder.proposer import ProposerOutput


def _write_dck(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- resolve_deck_id -------------------------------------------------------

def test_resolve_deck_id_uses_moxfield_metadata(tmp_path):
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck", "\n".join([
        "[metadata]",
        "Name=Foo",
        "Moxfield=abc-XYZ_123",
        "[Commander]",
        "1 Sol Ring",
    ]))
    assert resolve_deck_id(p) == "abc-XYZ_123"


def test_resolve_deck_id_falls_back_to_filename_when_no_metadata(tmp_path):
    p = _write_dck(tmp_path, "[USER] LegacyDeck [B3].dck", "\n".join([
        "[Commander]",
        "1 Atraxa, Praetors' Voice",
    ]))
    # No Moxfield= line + no fallback supplied → use the filename stem.
    out = resolve_deck_id(p)
    # Stem includes the [B3] suffix; that's fine — the goal is just stability.
    assert out == "[USER] LegacyDeck [B3]"


def test_resolve_deck_id_uses_explicit_fallback_over_stem(tmp_path):
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck", "[Commander]\n1 Test")
    assert resolve_deck_id(p, fallback="my-explicit-id") == "my-explicit-id"


def test_resolve_deck_id_metadata_wins_over_fallback(tmp_path):
    """The whole point: Moxfield= is the durable id, even if the caller
    supplies a filename-based fallback."""
    p = _write_dck(tmp_path, "[USER] Renamed Deck [B3].dck", "\n".join([
        "[metadata]",
        "Moxfield=stable-id",
        "[Commander]",
        "1 Test",
    ]))
    assert resolve_deck_id(p, fallback="stale-filename-id") == "stable-id"


def test_resolve_deck_id_strips_trailing_whitespace(tmp_path):
    """Some Moxfield= lines have trailing spaces from the .dck render."""
    p = _write_dck(tmp_path, "[USER] Foo [B3].dck",
                   "[metadata]\nMoxfield=abc-123   \n[Commander]\n1 Test")
    assert resolve_deck_id(p) == "abc-123"


def test_resolve_deck_id_raises_on_missing_file_with_no_fallback(tmp_path):
    with pytest.raises(ValueError):
        resolve_deck_id(tmp_path / "ghost.dck")


def test_resolve_deck_id_uses_fallback_for_missing_file(tmp_path):
    assert resolve_deck_id(tmp_path / "ghost.dck", fallback="emergency-id") == "emergency-id"


# --- run_one_iteration (full orchestrator with mocked compare) -------------

def _make_canned_comparison(
    *,
    old_wins: int,
    new_wins: int,
    draws: int,
    total: int,
) -> ComparisonReport:
    """Build a ComparisonReport that compare_versions.compare would have
    produced. Only the fields run_one_iteration reads matter; everything else
    can stay default."""
    return ComparisonReport(
        old_deck="old.dck",
        new_deck="new.dck",
        bracket=3,
        timestamp="2026-04-26T00:00:00Z",
        mode="pod",
        games_per_pod=10,
        total_games=total,
        draws=draws,
        old_stats=VersionStats(deck_filename="old.dck", wins=old_wins,
                               avg_ending_life=20.0, avg_damage_taken=15.0),
        new_stats=VersionStats(deck_filename="new.dck", wins=new_wins,
                               avg_ending_life=25.0, avg_damage_taken=12.0),
        card_diff={"added": ["NewCard"], "removed": ["OldCard"], "unchanged_count": ["98"]},
    )


@pytest.fixture
def staged_decks(tmp_path, monkeypatch):
    """Stage two .dck files in a fake DECK_DIR + redirect run_one_iteration's
    DECK_DIR to point at it. Both decks share the same Moxfield publicId so
    lineage chains correctly."""
    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)

    v1 = deck_dir / "[USER] Test Deck v1 [B3].dck"
    v1.write_text("\n".join([
        "[metadata]",
        "Name=Test Deck",
        "Moxfield=stable-public-id",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 Sol Ring",
        "1 OldCard",
    ]) + "\n", encoding="utf-8")

    v2 = deck_dir / "[USER] Test Deck v2 [B3].dck"
    v2.write_text("\n".join([
        "[metadata]",
        "Name=Test Deck",
        "Moxfield=stable-public-id",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 Sol Ring",
        "1 NewCard",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr("commander_builder.iteration_loop.DECK_DIR", deck_dir)
    return {"deck_dir": deck_dir, "v1": v1.name, "v2": v2.name}


def test_run_one_iteration_persists_kept_verdict(tmp_path, staged_decks, monkeypatch):
    """Strong improvement (16-4 over 20 decisive, p ~= 0.012 — at the
    aligned 20-decisive floor) → kept verdict → next_action='continue'."""
    canned = _make_canned_comparison(old_wins=4, new_wins=16, draws=0, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"], "audit_version": "v3"},
        db_path=db,
    )

    assert result.verdict.label == "kept"
    assert result.next_action == "continue"
    assert result.iteration_id > 0

    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched is not None
    assert fetched.deck_id == "stable-public-id"  # publicId, not filename
    assert fetched.verdict == "kept"
    assert fetched.margin == 12
    # One-convention precision (2026-07-19): all knowledge_log win-rate
    # writers round to 4 places via knowledge_log.decisive_win_rate.
    assert fetched.win_rate_old == round(4 / 20, 4)
    assert fetched.win_rate_new == round(16 / 20, 4)
    assert fetched.audit_manifest["added"] == ["NewCard"]
    # Sim report is the full ComparisonReport.to_dict()
    assert fetched.sim_report["winner"] == "new"


def test_run_one_iteration_win_rates_exclude_filler_wins(tmp_path, staged_decks, monkeypatch):
    """Pinned values for a FILLER-HEAVY comparison (2026-07-20 convention):
    30 attributed games — old won 4, new won 8, 2 drew, fillers took the
    other 16. Denominator is head-to-head decisive (4 + 8 = 12), NOT
    total - draws (28, which counts the filler wins): the rates must be
    4/12 and 8/12. Under 611feff this writer recorded 4/28 and 8/28,
    ~2x low versus the AB-shaped writers for the same outcome."""
    canned = _make_canned_comparison(old_wins=4, new_wins=8, draws=2, total=30)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )
    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.win_rate_old == round(4 / 12, 4)
    assert fetched.win_rate_new == round(8 / 12, 4)
    # Margin stays a raw head-to-head game delta, untouched by fillers.
    assert fetched.margin == 4


def test_run_one_iteration_persists_reverted_verdict(tmp_path, staged_decks, monkeypatch):
    """Strong regression → reverted → next_action='revert'."""
    canned = _make_canned_comparison(old_wins=16, new_wins=4, draws=0, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )
    assert result.verdict.label == "reverted"
    assert result.next_action == "revert"


def test_run_one_iteration_handles_inconclusive_draw_heavy_sim(tmp_path, staged_decks, monkeypatch):
    """The Hakbal-vs-Hash case: 18 of 20 games drew. Heuristic returns
    'inconclusive' (low confidence), iteration_loop must map that to
    'stop' so the caller knows to ask the user. (Re-pinned 2026-09-03,
    R3 C-01: the persisted label was 'neutral', which the schema defines
    as a trustworthy near-tie; a default commander-iterate run landed in
    every per-deck tally looking decided.)"""
    canned = _make_canned_comparison(old_wins=1, new_wins=1, draws=18, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": [], "removed": []},
        db_path=db,
    )
    assert result.verdict.label == "inconclusive"
    assert result.next_action == "stop"
    assert "decks_drew_too_often" in str(result.verdict.lessons)
    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.verdict == "inconclusive"


def test_run_one_iteration_chains_via_parent_id(tmp_path, staged_decks, monkeypatch):
    """A v2 → v3 iteration should record parent_id pointing at the v1 → v2
    iteration. Lineage reconstruction is the whole point of GAP-003 + this
    test."""
    canned = _make_canned_comparison(old_wins=2, new_wins=8, draws=0, total=10)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    first = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["X"], "removed": ["Y"]},
        db_path=db,
    )
    second = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["Z"], "removed": ["W"]},
        parent_iteration_id=first.iteration_id,
        db_path=db,
    )

    history = iterations_for_deck("stable-public-id", db_path=db)
    assert len(history) == 2
    assert history[0].id == first.iteration_id
    assert history[1].parent_id == first.iteration_id
    # stats_summary reflects both rows under one deck.
    s = stats_summary(db_path=db)
    assert s["total"] == 2
    assert s["unique_decks"] == 1


def test_run_one_iteration_writes_deck_snapshot_blob(tmp_path, staged_decks, monkeypatch):
    """The .dck text content is preserved in deck_snapshot for reproducibility.
    This is what lets Phase 3 rebuild any historical state without depending
    on Moxfield not deleting the deck."""
    canned = _make_canned_comparison(old_wins=2, new_wins=8, draws=0, total=10)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )

    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.deck_snapshot is not None
    assert "NewCard" in fetched.deck_snapshot
    assert "Moxfield=stable-public-id" in fetched.deck_snapshot


# --- propose_then_iterate (auto-propose materializes the deck) -------------

@pytest.fixture
def auto_propose_deck(tmp_path, monkeypatch):
    """Stage ONLY the v1 deck — auto-propose must materialize v2 itself.
    The mainboard is a full 99 cards so apply's basic-land padding stays
    out of the diff under test, and Scryfall lookups are stubbed so the
    appended add-line stays a plain `1 <name>` (offline, deterministic)."""
    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)

    v1 = deck_dir / "[USER] Test Deck v1 [B3].dck"
    v1.write_text("\n".join([
        "[metadata]",
        "Name=[USER] Test Deck v1 [B3]",
        "Moxfield=stable-public-id",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 Sol Ring",
        "1 OldCard",
        "97 Forest",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr("commander_builder.iteration_loop.DECK_DIR", deck_dir)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, cache=True: None,
    )
    return {"deck_dir": deck_dir, "v1": v1.name,
            "v2": "[USER] Test Deck v2 [B3].dck"}


def _main_card_names(text: str) -> set:
    """Card names in [Main], edition tails stripped — enough to diff the
    two on-disk versions against the recorded manifest."""
    names = set()
    in_main = False
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            in_main = s.lower() == "[main]"
            continue
        if in_main and s:
            _, _, name = s.partition(" ")
            names.add(name.split("|")[0].strip())
    return names


def test_propose_then_iterate_materializes_proposed_deck(
    tmp_path, auto_propose_deck, monkeypatch,
):
    """THE BUG (2026-08-13): propose_then_iterate never applied the LLM
    proposal to disk — it proposed against the v2 path (which had to
    pre-exist) and then simmed the two PRE-EXISTING files, so the
    recorded manifest and the simmed diff were unrelated, poisoning the
    knowledge log. Auto-propose must (a) propose against the OLD deck,
    (b) materialize the v2 deck FROM the proposal, and (c) persist a
    manifest that matches the on-disk diff."""
    seen = {}

    def _fake_propose(input_, config):
        seen["deck_path"] = input_.deck_path
        return ProposerOutput(
            added=["NewCard"], removed=["OldCard"],
            rationale="swap the dud", source="claude",
        )

    monkeypatch.setattr(
        "commander_builder.iteration_loop.propose", _fake_propose,
    )
    canned = _make_canned_comparison(old_wins=4, new_wins=16, draws=0, total=20)
    monkeypatch.setattr(
        "commander_builder.iteration_loop.compare", lambda **kw: canned,
    )

    old_path = auto_propose_deck["deck_dir"] / auto_propose_deck["v1"]
    new_path = auto_propose_deck["deck_dir"] / auto_propose_deck["v2"]
    db = tmp_path / "kl.sqlite"
    result = propose_then_iterate(
        deck_filename=auto_propose_deck["v1"],
        new_deck_filename=auto_propose_deck["v2"],
        bracket=3,
        db_path=db,
    )

    # (a) The proposer audited the OLD deck, not the then-nonexistent v2.
    assert seen["deck_path"] == old_path

    # (b) The proposed deck was materialized on disk, Name= restamped to
    # its own stem (the dck_meta invariant Forge's match log depends on).
    assert new_path.exists()
    new_text = new_path.read_text(encoding="utf-8")
    assert "NewCard" in new_text
    assert "OldCard" not in new_text
    assert "Name=[USER] Test Deck v2 [B3]" in new_text

    # (c) The persisted manifest IS the on-disk diff.
    fetched = get_iteration(result.iteration_id, db_path=db)
    old_names = _main_card_names(old_path.read_text(encoding="utf-8"))
    new_names = _main_card_names(new_text)
    assert set(fetched.audit_manifest["added"]) == new_names - old_names == {"NewCard"}
    assert set(fetched.audit_manifest["removed"]) == old_names - new_names == {"OldCard"}
    # The LLM's full intent survives alongside what landed.
    assert fetched.audit_manifest["requested_adds"] == ["NewCard"]
    assert fetched.audit_manifest["requested_cuts"] == ["OldCard"]
    assert result.verdict.label == "kept"


def test_propose_then_iterate_refuses_pre_existing_new_file(
    tmp_path, auto_propose_deck, monkeypatch,
):
    """A pre-existing --new file is exactly the poisoned-log setup the
    fix closes: fail fast, and BEFORE the proposer runs so no LLM spend
    is wasted on a run that can't land."""
    (auto_propose_deck["deck_dir"] / auto_propose_deck["v2"]).write_text(
        "[Main]\n1 Stale\n", encoding="utf-8",
    )

    def _no_propose(*a, **kw):
        raise AssertionError("propose() must not run when --new already exists")
    monkeypatch.setattr(
        "commander_builder.iteration_loop.propose", _no_propose,
    )

    with pytest.raises(FileExistsError, match="already exists"):
        propose_then_iterate(
            deck_filename=auto_propose_deck["v1"],
            new_deck_filename=auto_propose_deck["v2"],
            bracket=3,
            db_path=tmp_path / "kl.sqlite",
        )


def test_propose_then_iterate_requires_old_deck(tmp_path, auto_propose_deck):
    with pytest.raises(FileNotFoundError, match="old deck not found"):
        propose_then_iterate(
            deck_filename="[USER] Ghost [B3].dck",
            new_deck_filename=auto_propose_deck["v2"],
            bracket=3,
            db_path=tmp_path / "kl.sqlite",
        )


def test_main_auto_propose_fails_fast_when_new_exists(
    auto_propose_deck, capsys,
):
    """CLI wrapper: --auto-propose with a pre-existing --new file exits 2
    with an actionable ERROR line instead of silently comparing the two
    pre-existing files (or dumping a traceback)."""
    (auto_propose_deck["deck_dir"] / auto_propose_deck["v2"]).write_text(
        "[Main]\n1 Stale\n", encoding="utf-8",
    )
    rc = iteration_loop_main([
        "--old", auto_propose_deck["v1"],
        "--new", auto_propose_deck["v2"],
        "--bracket", "3",
        "--auto-propose",
    ])
    assert rc == 2
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "already exists" in out


def test_propose_then_iterate_refuses_when_every_pair_is_dropped(
    tmp_path, auto_propose_deck, monkeypatch,
):
    """L3: apply-time validation can drop EVERY proposed pair (a cut
    naming a card that isn't in the decklist takes its paired add down
    with it). The materialized v2 is then content-identical to v1, and
    simming it burns 10+ Forge games comparing a deck against itself
    and writes a pure-noise row into the knowledge log. Refuse BEFORE
    compare(), and delete the just-written file so the next run doesn't
    trip the pre-existing-file guard on our own leftover."""
    def _fake_propose(input_, config):
        # "GhostCard" is not in the [Main] list → unmatched cut → the
        # NewCard/GhostCard PAIR is dropped, leaving nothing applied.
        return ProposerOutput(
            added=["NewCard"], removed=["GhostCard"],
            rationale="swap a card that isn't there", source="claude",
        )

    monkeypatch.setattr(
        "commander_builder.iteration_loop.propose", _fake_propose,
    )

    def _no_compare(**kw):
        raise AssertionError("compare() must not run on a no-op swap")
    monkeypatch.setattr(
        "commander_builder.iteration_loop.compare", _no_compare,
    )

    new_path = auto_propose_deck["deck_dir"] / auto_propose_deck["v2"]
    with pytest.raises(RuntimeError) as excinfo:
        propose_then_iterate(
            deck_filename=auto_propose_deck["v1"],
            new_deck_filename=auto_propose_deck["v2"],
            bracket=3,
            db_path=tmp_path / "kl.sqlite",
        )

    msg = str(excinfo.value)
    # Actionable: names the requested pair AND why it didn't survive.
    assert "no proposed swap survived" in msg
    assert "GhostCard" in msg
    assert "NewCard" in msg
    # The half-written artifact is gone — a re-run must not hit the
    # FileExistsError guard on a file THIS call created.
    assert not new_path.exists()


def test_propose_then_iterate_no_op_swap_logs_nothing(
    tmp_path, auto_propose_deck, monkeypatch,
):
    """The point of the guard is the knowledge log: a refused run must
    leave zero rows behind (a noise row is worse than no row — it
    dilutes every win-rate summary computed over the deck)."""
    monkeypatch.setattr(
        "commander_builder.iteration_loop.propose",
        lambda input_, config: ProposerOutput(
            added=["NewCard"], removed=["GhostCard"],
            rationale="no-op", source="claude",
        ),
    )
    monkeypatch.setattr(
        "commander_builder.iteration_loop.compare",
        lambda **kw: _make_canned_comparison(
            old_wins=4, new_wins=16, draws=0, total=20,
        ),
    )
    db = tmp_path / "kl.sqlite"
    with pytest.raises(RuntimeError):
        propose_then_iterate(
            deck_filename=auto_propose_deck["v1"],
            new_deck_filename=auto_propose_deck["v2"],
            bracket=3,
            db_path=db,
        )
    assert iterations_for_deck("stable-public-id", db_path=db) == []


def test_main_auto_propose_exits_2_when_every_pair_is_dropped(
    auto_propose_deck, monkeypatch, capsys,
):
    """CLI wrapper: the no-op-swap guard exits 2 with an ERROR line,
    same clean-exit treatment as the pre-existing-file guard — not a
    traceback."""
    monkeypatch.setattr(
        "commander_builder.iteration_loop.propose",
        lambda input_, config: ProposerOutput(
            added=["NewCard"], removed=["GhostCard"],
            rationale="no-op", source="claude",
        ),
    )
    monkeypatch.setattr(
        "commander_builder.iteration_loop.compare",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("compare() must not run"),
        ),
    )
    rc = iteration_loop_main([
        "--old", auto_propose_deck["v1"],
        "--new", auto_propose_deck["v2"],
        "--bracket", "3",
        "--auto-propose",
    ])
    assert rc == 2
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "no proposed swap survived" in out


# --- _materialize_proposed_deck: manifest ↔ on-disk-diff invariant ---------

def test_materialize_records_padding_in_manifest(tmp_path, monkeypatch):
    """L2: the applier pads a SHORT source deck with basic lands so
    Forge will load it. Those basics are part of the on-disk diff the
    sim measures, so the persisted manifest has to carry them — without
    ``padded_count`` / ``padded_breakdown`` the manifest silently
    under-describes the deck that was actually simmed."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, cache=True: None,
    )
    old_path = _write_dck(tmp_path, "[USER] Short v1 [B3].dck", "\n".join([
        "[metadata]",
        "Name=[USER] Short v1 [B3]",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 OldCard",
        "40 Forest",
    ]) + "\n")
    new_path = tmp_path / "[USER] Short v2 [B3].dck"
    manifest = {
        "added": ["NewCard"], "removed": ["OldCard"],
        "rationale": "swap", "source": "claude",
    }
    _materialize_proposed_deck(old_path, new_path, manifest)

    # 1 OldCard + 40 Forest = 41 main; the swap keeps 40 + adds 1 = 41,
    # so 58 basics are synthesized to reach the 99-card target.
    assert manifest["padded_count"] == 58
    assert manifest["padded_breakdown"] == {"Forest": 58}
    # And the padding is REAL — the manifest number matches the file
    # (the padder appends its own line rather than merging counts).
    new_text = new_path.read_text(encoding="utf-8")
    forests = sum(
        int(line.split(" ", 1)[0])
        for line in new_text.splitlines()
        if line.strip().endswith("Forest")
    )
    assert forests == 40 + manifest["padded_count"] == 98


def test_materialize_records_pair_drops_in_manifest(tmp_path, monkeypatch):
    """Same invariant from the other side: pairs the applier refused
    are recorded under the SAME key names ``_proposer_sim`` uses (the
    other writer of this manifest shape), so knowledge-log consumers
    read one schema regardless of which path produced the row."""
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card",
        lambda name, cache=True: None,
    )
    old_path = _write_dck(tmp_path, "[USER] Drop v1 [B3].dck", "\n".join([
        "[metadata]",
        "Name=[USER] Drop v1 [B3]",
        "[Commander]",
        "1 Test Commander",
        "[Main]",
        "1 OldCard",
        "98 Forest",
    ]) + "\n")
    new_path = tmp_path / "[USER] Drop v2 [B3].dck"
    manifest = {
        "added": ["NewCard"], "removed": ["GhostCard"],
        "rationale": "swap", "source": "claude",
    }
    _materialize_proposed_deck(old_path, new_path, manifest)

    assert manifest["added"] == []
    assert manifest["removed"] == []
    assert manifest["dropped_unmatched_cut"] == [
        {"cut": "GhostCard", "add": "NewCard"},
    ]
    # Every reason bucket _proposer_sim persists is present (empty is a
    # fact, absent is a hole a consumer has to guess at).
    for key in (
        "dropped_for_bracket", "dropped_for_protection",
        "dropped_for_color_identity", "dropped_for_balance",
        "dropped_duplicate_add", "dropped_commander_add",
        "padded_count", "padded_breakdown",
    ):
        assert key in manifest
    # Intent is preserved alongside what landed.
    assert manifest["requested_adds"] == ["NewCard"]
    assert manifest["requested_cuts"] == ["GhostCard"]


def test_run_one_iteration_refuses_verdict_on_zero_attributed_games(
    tmp_path, staged_decks, monkeypatch, capsys,
):
    """The bug: a fully-failed sim (every pod crashed/timed out → 0
    attributed games) recorded win_rate_old=0.0 / win_rate_new=0.0 via the
    max(1, ...) clamp — a fabricated 'empirical neutral' in the knowledge
    log. It must instead land as 'pending' (the failed-sim label
    _proposer_sim._verdict_from_ab uses) with NULL win rates, and the
    analyst must not even be consulted."""
    canned = _make_canned_comparison(old_wins=0, new_wins=0, draws=0, total=0)
    canned.failed_pods = 2
    canned.pods_planned = 2
    canned.excluded_games = 7
    monkeypatch.setattr(
        "commander_builder.iteration_loop.compare", lambda **kw: canned,
    )

    # The analyst has no business rendering a verdict on an empty sim —
    # fail the test if it's called.
    def _no_analyst(*a, **kw):
        raise AssertionError("analyze() must not be called on 0 attributed games")
    monkeypatch.setattr("commander_builder.iteration_loop.analyze", _no_analyst)

    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"],
        new_deck_filename=staged_decks["v2"],
        bracket=3,
        audit_manifest={"added": ["NewCard"], "removed": ["OldCard"]},
        db_path=db,
    )

    assert result.verdict.label == "pending"
    assert result.next_action == "stop"

    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.verdict == "pending"
    # NULL, not a fake 0.0/0.0 empirical neutral.
    assert fetched.win_rate_old is None
    assert fetched.win_rate_new is None
    assert fetched.margin is None
    assert "no attributed games" in (fetched.verdict_notes or "")
    # Loud warning on the console.
    assert "WARNING" in capsys.readouterr().out


# --- R3 C-08 (2026-09-03): legacy decks share one deck_id across versions --

@pytest.fixture
def legacy_decks(tmp_path, monkeypatch):
    """Two versions of a hand-built deck: NO Moxfield= line."""
    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    names = ["[USER] Legacy v1 [B3].dck", "[USER] Legacy v2 [B3].dck",
             "[USER] Legacy v3 [B3].dck"]
    for n in names:
        (deck_dir / n).write_text(
            f"[metadata]\nName={n[:-4]}\n[Commander]\n1 Cmdr\n[Main]\n1 Sol Ring\n",
            encoding="utf-8")
    monkeypatch.setattr("commander_builder.iteration_loop.DECK_DIR", deck_dir)
    return names


def test_run_one_iteration_keys_a_legacy_deck_stably(tmp_path, legacy_decks, monkeypatch):
    """v1->v2 and v2->v3 used to be two deck_ids ('... v1 [B3].dck' and
    '... v2 [B3].dck'); the version-stripped stem keys both."""
    canned = _make_canned_comparison(old_wins=4, new_wins=16, draws=0, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)
    db = tmp_path / "kl.sqlite"
    first = run_one_iteration(
        deck_filename=legacy_decks[0], new_deck_filename=legacy_decks[1],
        bracket=3, audit_manifest={"added": [], "removed": []}, db_path=db)
    second = run_one_iteration(
        deck_filename=legacy_decks[1], new_deck_filename=legacy_decks[2],
        bracket=3, audit_manifest={"added": [], "removed": []}, db_path=db)
    a = get_iteration(first.iteration_id, db_path=db)
    b = get_iteration(second.iteration_id, db_path=db)
    assert a.deck_id == b.deck_id == "[USER] Legacy [B3]"


# --- R3 C-09 (2026-09-03): the analyst writer stamps verdict provenance ----

def test_run_one_iteration_stamps_verdict_provenance(tmp_path, staged_decks, monkeypatch):
    from commander_builder.analyst import AnalystConfig
    from commander_builder.knowledge_log import SIM_REPORT_VERDICT_PARAMS_KEY
    canned = _make_canned_comparison(old_wins=4, new_wins=16, draws=0, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)
    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"], new_deck_filename=staged_decks["v2"],
        bracket=3, audit_manifest={"added": ["A"], "removed": ["B"]}, db_path=db,
        analyst_config=AnalystConfig(alpha=0.01),
    )
    params = get_iteration(result.iteration_id, db_path=db).sim_report[
        SIM_REPORT_VERDICT_PARAMS_KEY]
    assert params["alpha"] == 0.01
    assert params["min_decisive"] == 20
    assert params["margin"] == 1
    assert params["rule"].startswith("analyst.heuristic")


def test_zero_game_row_carries_no_provenance(tmp_path, staged_decks, monkeypatch):
    """No verdict rule ran on an empty sim — nothing to claim."""
    from commander_builder.knowledge_log import SIM_REPORT_VERDICT_PARAMS_KEY
    canned = _make_canned_comparison(old_wins=0, new_wins=0, draws=0, total=0)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)
    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"], new_deck_filename=staged_decks["v2"],
        bracket=3, audit_manifest={"added": [], "removed": []}, db_path=db)
    assert SIM_REPORT_VERDICT_PARAMS_KEY not in get_iteration(
        result.iteration_id, db_path=db).sim_report


# --- R3 C-14 (2026-09-03): a 0-0 compare stores NULL margin ---------------

def test_run_one_iteration_null_margin_when_no_game_was_decisive(
    tmp_path, staged_decks, monkeypatch,
):
    """Every game went to a filler (total 20, pair won 0): this writer
    stored margin=0 while the web writer stored NULL for the same
    outcome. The verdict is 'inconclusive' (0 < 20 decisive)."""
    canned = _make_canned_comparison(old_wins=0, new_wins=0, draws=0, total=20)
    monkeypatch.setattr("commander_builder.iteration_loop.compare", lambda **kw: canned)
    db = tmp_path / "kl.sqlite"
    result = run_one_iteration(
        deck_filename=staged_decks["v1"], new_deck_filename=staged_decks["v2"],
        bracket=3, audit_manifest={"added": [], "removed": []}, db_path=db)
    fetched = get_iteration(result.iteration_id, db_path=db)
    assert fetched.verdict == "inconclusive"
    assert fetched.margin is None
    assert fetched.win_rate_old is None and fetched.win_rate_new is None
