"""commander-status tests.

Pure-offline: each test sets up a tmp_path fake DECK_DIR / pool dir / etc and
verifies `collect_status` reports the right shape.
"""
import json
from pathlib import Path

import pytest

from commander_builder.status import (
    DeckStatusReport,
    StatusReport,
    _count_decks,
    _list_pools,
    _list_recent,
    collect_deck_status,
    collect_status,
    collect_user_decks_summary,
    format_deck_text,
    format_text,
    format_user_decks_summary,
)
from commander_builder.knowledge_log import Iteration, record_iteration


def _touch(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- _count_decks ----------------------------------------------------------

def test_count_decks_groups_by_bracket(tmp_path):
    _touch(tmp_path / "Allies [B3].dck")
    _touch(tmp_path / "[USER] My Deck [B3].dck")
    _touch(tmp_path / "Some Other [B4].dck")
    counts = _count_decks(tmp_path)
    assert counts[3]["total"] == 2
    assert counts[3]["user"] == 1
    assert counts[3]["filler"] == 1
    assert counts[4]["total"] == 1
    assert counts[4]["filler"] == 1


def test_count_decks_ignores_non_bracket_files(tmp_path):
    """Files without a recognized `[B<n>]` suffix are silently ignored.
    (The `[B?]` placeholder some imports use isn't tested here because
    Windows rejects `?` in filenames.)"""
    _touch(tmp_path / "Random Name.dck")        # no bracket suffix
    _touch(tmp_path / "Bracket 6 Stuff [B6].dck")  # B6 isn't a real bracket
    _touch(tmp_path / "notes.txt")
    assert _count_decks(tmp_path) == {}


def test_count_decks_missing_dir(tmp_path):
    assert _count_decks(tmp_path / "ghost") == {}


# --- _list_pools -----------------------------------------------------------

def test_list_pools_skips_analysis_files(tmp_path):
    _touch(tmp_path / "B3.json", json.dumps({
        "bracket": 3, "pool_a": ["a", "b"], "pool_b": ["c"], "rejected": [],
    }))
    _touch(tmp_path / "B3_analysis.json", "{}")  # paired analysis blob
    pools = _list_pools(tmp_path)
    assert len(pools) == 1
    assert pools[0]["bracket"] == 3
    assert pools[0]["pool_a_size"] == 2
    assert pools[0]["pool_b_size"] == 1


def test_list_pools_handles_corrupt_json(tmp_path):
    _touch(tmp_path / "B3.json", "{ not json")
    pools = _list_pools(tmp_path)
    assert pools == []


def test_list_pools_missing_dir(tmp_path):
    assert _list_pools(tmp_path / "ghost") == []


# --- _list_recent ----------------------------------------------------------

def test_list_recent_returns_newest_first(tmp_path):
    import time
    a = _touch(tmp_path / "old.json", json.dumps({"bracket": 3, "winner": "old"}))
    time.sleep(0.05)
    b = _touch(tmp_path / "new.json", json.dumps({"bracket": 4, "winner": "new"}))
    out = _list_recent(tmp_path)
    assert out[0]["path"] == "new.json"
    assert out[1]["path"] == "old.json"


def test_list_recent_respects_limit(tmp_path):
    for i in range(10):
        _touch(tmp_path / f"r{i}.json", json.dumps({"bracket": 3}))
    assert len(_list_recent(tmp_path, limit=3)) == 3


def test_list_recent_handles_corrupt_json(tmp_path):
    _touch(tmp_path / "good.json", json.dumps({"bracket": 3, "winner": "new"}))
    _touch(tmp_path / "broken.json", "{ broken")
    out = _list_recent(tmp_path)
    assert len(out) == 1
    assert out[0]["path"] == "good.json"


# --- collect_status (integration) ------------------------------------------

def test_collect_status_full_report(tmp_path):
    deck_dir = tmp_path / "decks"
    pool_dir = tmp_path / "pools"
    match_dir = tmp_path / "matches"
    compare_dir = tmp_path / "compares"
    db = tmp_path / "kl.sqlite"

    _touch(deck_dir / "Allies [B3].dck")
    _touch(deck_dir / "[USER] Mine [B3].dck")
    _touch(pool_dir / "B3.json", json.dumps({
        "bracket": 3, "pool_a": ["a"], "pool_b": ["b"], "rejected": [],
    }))
    _touch(match_dir / "match1.json", json.dumps({
        "bracket": 3, "win_rate": 0.5, "games_played": 4,
    }))
    _touch(compare_dir / "cmp1.json", json.dumps({
        "bracket": 3, "winner": "tie", "total_games": 20,
    }))

    report = collect_status(
        deck_dir=deck_dir,
        pool_dir=pool_dir,
        match_dir=match_dir,
        compare_dir=compare_dir,
        db_path=db,
    )
    assert isinstance(report, StatusReport)
    assert report.deck_dir_present
    assert report.decks_by_bracket[3]["total"] == 2
    assert len(report.pools_on_disk) == 1
    assert len(report.recent_matches) == 1
    assert len(report.recent_compares) == 1
    # Empty knowledge log = total 0, no error.
    assert report.knowledge_log["stats"]["total"] == 0


def test_collect_status_handles_missing_directories(tmp_path):
    """All directories absent — report should still come back coherently
    rather than crash."""
    report = collect_status(
        deck_dir=tmp_path / "ghost1",
        pool_dir=tmp_path / "ghost2",
        match_dir=tmp_path / "ghost3",
        compare_dir=tmp_path / "ghost4",
        db_path=tmp_path / "kl.sqlite",
    )
    assert not report.deck_dir_present
    assert report.decks_by_bracket == {}
    assert report.pools_on_disk == []


# --- format_text -----------------------------------------------------------

def test_format_text_renders_each_section(tmp_path):
    deck_dir = tmp_path / "decks"
    _touch(deck_dir / "Allies [B3].dck")
    report = collect_status(
        deck_dir=deck_dir,
        pool_dir=tmp_path / "pools",
        match_dir=tmp_path / "matches",
        compare_dir=tmp_path / "compares",
        db_path=tmp_path / "kl.sqlite",
    )
    text = format_text(report)
    assert "Commander Builder" in text
    assert "Decks on disk" in text
    assert "Curated pools" in text
    assert "Knowledge log" in text
    assert "B3:" in text


def test_to_json_round_trips(tmp_path):
    report = collect_status(
        deck_dir=tmp_path / "ghost",
        pool_dir=tmp_path / "ghost",
        match_dir=tmp_path / "ghost",
        compare_dir=tmp_path / "ghost",
        db_path=tmp_path / "kl.sqlite",
    )
    parsed = json.loads(report.to_json())
    assert "timestamp" in parsed
    assert "decks_by_bracket" in parsed


# --- Per-deck status helpers ----------------------------------------------

_WYRM_DCK = (
    "[metadata]\n"
    "Name=Wyrm Sovereign\n"
    "Moxfield=ABC123\n"
    "[Commander]\n"
    "1 The Ur-Dragon|CMM|361\n"
    "[Main]\n"
    "1 Sol Ring|CMR|1\n"
    "1 Arcane Signet|CMR|2\n"
)


def test_collect_deck_status_parses_filename_and_dck(tmp_path):
    """Deck name, bracket, and commander come from the filename and the
    .dck file's [metadata] / [Commander] sections. Missing iteration
    history is reported as a count of zero, not crashed on."""
    deck = _touch(tmp_path / "[USER] Wyrm Sovereign [B4].dck", _WYRM_DCK)
    db = tmp_path / "kl.sqlite"

    report = collect_deck_status(deck, db_path=db)

    assert isinstance(report, DeckStatusReport)
    assert report.deck_name == "Wyrm Sovereign"
    assert report.commander_name == "The Ur-Dragon"
    assert report.bracket == 4
    assert report.deck_id == "ABC123"
    assert report.iteration_count == 0
    assert report.last_audit is None
    assert report.last_modified  # ISO-format timestamp


def test_collect_deck_status_unknown_bracket(tmp_path):
    """Decks without a [Bn] suffix still parse — bracket is None."""
    body = _WYRM_DCK.replace("Wyrm Sovereign", "Untagged Deck")
    deck = _touch(tmp_path / "Untagged Deck.dck", body)
    report = collect_deck_status(deck, db_path=tmp_path / "kl.sqlite")
    assert report.bracket is None
    assert report.deck_name == "Untagged Deck"


def test_collect_deck_status_prefers_display_name_over_stamped_name(tmp_path):
    """Display-decision pin: importers now stamp Name= with the filename
    stem (so Forge win attribution works for non-ASCII/':' deck names) and
    park the pretty Moxfield name in DisplayName=. The status CLI must show
    the pretty name, not the bracketed stem."""
    body = (
        "[metadata]\n"
        "Name=[USER] Chatterfang_ Squirrel Tribal [B3]\n"
        "DisplayName=Chatterfang: Squirrel Tribal \U0001f43f\n"
        "Moxfield=ABC123\n"
        "[Commander]\n"
        "1 Chatterfang, Squirrel General|MH2|151\n"
        "[Main]\n"
        "1 Sol Ring|CMR|1\n"
    )
    deck = _touch(tmp_path / "[USER] Chatterfang_ Squirrel Tribal [B3].dck", body)
    report = collect_deck_status(deck, db_path=tmp_path / "kl.sqlite")
    assert report.deck_name == "Chatterfang: Squirrel Tribal \U0001f43f"
    # Decks written before the stamping change (Name= only, no DisplayName=)
    # keep their old display verbatim — covered by
    # test_collect_deck_status_parses_filename_and_dck above.


def test_collect_deck_status_aggregates_iteration_history(tmp_path):
    """Iteration count + recent iterations come from knowledge_log
    rows keyed on the deck's Moxfield publicId."""
    deck = _touch(tmp_path / "[USER] Wyrm Sovereign [B4].dck", _WYRM_DCK)
    db = tmp_path / "kl.sqlite"

    # Three iterations: two kept, one reverted. Newest last.
    manifests = [
        {"added": ["Cyclonic Rift"], "removed": ["Bad Card"],
         "audit_version": "v3",
         "pricing": {"total_price_usd": 200.0, "captured_at": "2026-05-01"}},
        {"added": ["Rhystic Study", "Mystic Remora"],
         "removed": ["Underwhelming", "Suboptimal"],
         "audit_version": "v3",
         "pricing": {"total_price_usd": 230.0, "captured_at": "2026-05-05"}},
        {"added": ["Smothering Tithe", "Esper Sentinel", "Land Tax"],
         "removed": ["Old Ramp", "Old Draw", "Old Removal"],
         "audit_version": "v3",
         "pricing": {"total_price_usd": 280.0, "captured_at": "2026-05-12"}},
    ]
    verdicts = ["kept", "kept", "reverted"]
    for m, v in zip(manifests, verdicts):
        record_iteration(
            Iteration(
                deck_id="ABC123",
                deck_name="[USER] Wyrm Sovereign [B4].dck",
                bracket=4,
                audit_version="v3",
                audit_manifest=m,
                verdict=v,
                win_rate_old=0.5,
                win_rate_new=0.55,
                margin=2,
            ),
            db_path=db,
        )

    report = collect_deck_status(deck, db_path=db)

    assert report.iteration_count == 3
    # Last 3 in chronological order, with diff sizes and price deltas.
    assert len(report.recent_iterations) == 3
    last = report.recent_iterations[-1]
    assert last["verdict"] == "reverted"
    assert last["adds"] == 3
    assert last["cuts"] == 3
    # Price delta from prior iteration: 280 - 230 = +50.
    assert last["price_delta_usd"] == pytest.approx(50.0)

    # The newest iteration is the "last audit" surface.
    assert report.last_audit is not None
    assert report.last_audit["top_adds"][:3] == [
        "Smothering Tithe", "Esper Sentinel", "Land Tax",
    ]
    assert report.last_audit["top_cuts"][:3] == [
        "Old Ramp", "Old Draw", "Old Removal",
    ]
    assert report.last_audit["suggested_count"] == 6
    assert report.last_audit["verdict"] == "reverted"


def test_collect_deck_status_meta_win_rate(tmp_path):
    """When a meta-test report exists in the deck-dir's _meta folder
    for this deck, the latest win_rate is surfaced."""
    deck_dir = tmp_path / "decks"
    deck = _touch(deck_dir / "[USER] Wyrm Sovereign [B4].dck", _WYRM_DCK)

    meta_dir = deck_dir / "_meta"
    older = _touch(
        meta_dir / "USER_Wyrm_Sovereign_B4_meta_20260501T000000Z.json",
        json.dumps({"user_record": {"win_rate": 0.40}}),
    )
    import time
    time.sleep(0.05)
    newer = _touch(
        meta_dir / "USER_Wyrm_Sovereign_B4_meta_20260512T000000Z.json",
        json.dumps({"user_record": {"win_rate": 0.62}}),
    )

    report = collect_deck_status(
        deck, db_path=tmp_path / "kl.sqlite", meta_dir=meta_dir,
    )

    assert report.meta_win_rate == pytest.approx(0.62)


def test_format_deck_text_renders_core_sections(tmp_path):
    deck = _touch(tmp_path / "[USER] Wyrm Sovereign [B4].dck", _WYRM_DCK)
    db = tmp_path / "kl.sqlite"
    record_iteration(
        Iteration(
            deck_id="ABC123",
            deck_name="[USER] Wyrm Sovereign [B4].dck",
            bracket=4,
            audit_version="v3",
            audit_manifest={
                "added": ["Rhystic Study"],
                "removed": ["Bad Card"],
                "audit_version": "v3",
            },
            verdict="kept",
            margin=3,
        ),
        db_path=db,
    )
    report = collect_deck_status(deck, db_path=db)
    text = format_deck_text(report)

    assert "Wyrm Sovereign" in text
    assert "The Ur-Dragon" in text
    assert "B4" in text or "Bracket 4" in text
    assert "Iterations" in text
    assert "Last audit" in text
    assert "Rhystic Study" in text


def test_format_deck_text_no_iterations_message(tmp_path):
    """Brand-new deck with zero iterations — output is still coherent."""
    deck = _touch(tmp_path / "[USER] Fresh Deck [B3].dck", _WYRM_DCK)
    report = collect_deck_status(deck, db_path=tmp_path / "kl.sqlite")
    text = format_deck_text(report)
    assert "no iterations" in text.lower() or "0 iterations" in text.lower()


# --- list_user_decks ------------------------------------------------------

def _dck_with_name(name: str, mox_id: str = "ABC123") -> str:
    """Build a minimal .dck body with a custom metadata Name and
    Moxfield id so the listing function can distinguish test decks."""
    return (
        "[metadata]\n"
        f"Name={name}\n"
        f"Moxfield={mox_id}\n"
        "[Commander]\n"
        "1 The Ur-Dragon|CMM|361\n"
        "[Main]\n"
        "1 Sol Ring|CMR|1\n"
    )


def test_collect_user_decks_summary_filters_to_user_prefix(tmp_path):
    """Only files starting with `[USER]` are returned. Filler bracket
    decks are excluded so the dashboard stays focused on the user's
    own decks."""
    deck_dir = tmp_path / "decks"
    _touch(deck_dir / "[USER] Alpha [B3].dck", _dck_with_name("Alpha", "AAA"))
    _touch(deck_dir / "[USER] Beta [B4].dck", _dck_with_name("Beta", "BBB"))
    _touch(deck_dir / "Filler [B3].dck", _dck_with_name("Filler", "FFF"))
    _touch(deck_dir / "[USER] No Bracket.dck", _dck_with_name("No Bracket", "NNN"))

    summaries = collect_user_decks_summary(
        deck_dir=deck_dir, db_path=tmp_path / "kl.sqlite",
    )

    names = [s["deck_name"] for s in summaries]
    assert "Alpha" in names
    assert "Beta" in names
    assert "No Bracket" in names
    assert "Filler" not in names
    assert len(summaries) == 3


def test_collect_user_decks_summary_counts_iterations_per_deck(tmp_path):
    """Iteration counts are per-deck from knowledge_log, not aggregated."""
    deck_dir = tmp_path / "decks"
    _touch(deck_dir / "[USER] Alpha [B3].dck", _dck_with_name("Alpha", "AAA"))
    _touch(deck_dir / "[USER] Beta [B4].dck", _dck_with_name("Beta", "BBB"))
    db = tmp_path / "kl.sqlite"

    # 2 iterations for Alpha, 1 for Beta.
    for _ in range(2):
        record_iteration(
            Iteration(deck_id="AAA", deck_name="Alpha", bracket=3, verdict="kept"),
            db_path=db,
        )
    record_iteration(
        Iteration(deck_id="BBB", deck_name="Beta", bracket=4, verdict="kept"),
        db_path=db,
    )

    summaries = {s["deck_name"]: s for s in
                 collect_user_decks_summary(deck_dir=deck_dir, db_path=db)}
    assert summaries["Alpha"]["iteration_count"] == 2
    assert summaries["Beta"]["iteration_count"] == 1


def test_collect_user_decks_summary_missing_dir(tmp_path):
    """Missing deck dir → empty list, not a crash."""
    summaries = collect_user_decks_summary(
        deck_dir=tmp_path / "ghost", db_path=tmp_path / "kl.sqlite",
    )
    assert summaries == []


def test_format_user_decks_summary_renders_one_line_per_deck(tmp_path):
    deck_dir = tmp_path / "decks"
    _touch(deck_dir / "[USER] Alpha [B3].dck", _dck_with_name("Alpha", "AAA"))
    _touch(deck_dir / "[USER] Beta [B4].dck", _dck_with_name("Beta", "BBB"))

    summaries = collect_user_decks_summary(
        deck_dir=deck_dir, db_path=tmp_path / "kl.sqlite",
    )
    text = format_user_decks_summary(summaries)

    assert "Alpha" in text
    assert "Beta" in text
    assert "B3" in text
    assert "B4" in text
    # One line per deck (header lines OK; key thing is each deck name
    # appears on its own line, not concatenated).
    alpha_line = [ln for ln in text.splitlines() if "Alpha" in ln]
    beta_line = [ln for ln in text.splitlines() if "Beta" in ln]
    assert len(alpha_line) == 1
    assert len(beta_line) == 1


def test_format_user_decks_summary_empty(tmp_path):
    """No decks → still produces output (no crash, no garbage)."""
    text = format_user_decks_summary([])
    assert isinstance(text, str)
    assert text  # not empty
