"""commander-status tests.

Pure-offline: each test sets up a tmp_path fake DECK_DIR / pool dir / etc and
verifies `collect_status` reports the right shape.
"""
import json
from pathlib import Path

import pytest

from commander_builder.status import (
    StatusReport,
    _count_decks,
    _list_pools,
    _list_recent,
    collect_status,
    format_text,
)


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
