"""compare_versions unit tests for offline helpers (no Forge subprocess).

Covers card-diff parsing, filler-pair selection, and the ComparisonReport
property surface. The actual `compare()` loop hits Forge — exercised live, not
in unit tests.
"""
import json

import pytest

from commander_builder.compare_versions import (
    ComparisonReport,
    VersionStats,
    _format_summary,
    _pick_filler_pairs,
    _read_main_section,
    diff_decks,
)


# --- _read_main_section / diff_decks ---------------------------------------

def _write_dck(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_read_main_section_extracts_card_lines(tmp_path):
    p = _write_dck(tmp_path, "deck.dck", [
        "[metadata]",
        "Name=Test",
        "Moxfield=abc",
        "[Commander]",
        "1 Atraxa, Praetors' Voice|CMM|1",
        "[Main]",
        "1 Sol Ring|CMM|1",
        "1 Mana Crypt|2X2|2",
        "",
        "1 Forest|UNF|451",
    ])
    main = _read_main_section(p)
    assert main == [
        "1 Sol Ring|CMM|1",
        "1 Mana Crypt|2X2|2",
        "1 Forest|UNF|451",
    ]


def test_read_main_section_missing_file():
    from pathlib import Path
    assert _read_main_section(Path("/does/not/exist.dck")) == []


def test_read_main_section_stops_at_next_section(tmp_path):
    p = _write_dck(tmp_path, "deck.dck", [
        "[Main]",
        "1 A",
        "[Sideboard]",
        "1 B",
        "[Main]",
        "1 C",
    ])
    main = _read_main_section(p)
    # Both [Main] blocks contribute; [Sideboard] is excluded.
    assert "1 A" in main
    assert "1 C" in main
    assert "1 B" not in main


def test_diff_decks_added_and_removed(tmp_path):
    old = _write_dck(tmp_path, "old.dck", [
        "[Main]",
        "1 Sol Ring|CMM|1",
        "1 Mana Crypt|2X2|2",
        "1 Forest|UNF|451",
    ])
    new = _write_dck(tmp_path, "new.dck", [
        "[Main]",
        "1 Sol Ring|CMM|1",       # unchanged
        "1 Arcane Signet|CMM|3",  # added
        "1 Forest|UNF|451",       # unchanged
        # Mana Crypt removed
    ])
    diff = diff_decks(old, new)
    assert diff["added"] == ["1 Arcane Signet|CMM|3"]
    assert diff["removed"] == ["1 Mana Crypt|2X2|2"]


def test_diff_decks_quantity_change_shows_as_swap(tmp_path):
    """A quantity change is encoded as remove-old + add-new since the full
    line is the diff key. This is intentional — quantity matters for sims."""
    old = _write_dck(tmp_path, "old.dck", ["[Main]", "1 Foo"])
    new = _write_dck(tmp_path, "new.dck", ["[Main]", "2 Foo"])
    diff = diff_decks(old, new)
    assert "1 Foo" in diff["removed"]
    assert "2 Foo" in diff["added"]


# --- diff_deck_text — string-form diff for in-memory snapshots -------------

def test_diff_deck_text_basic_add_and_remove():
    from commander_builder.compare_versions import diff_deck_text
    old = "[metadata]\nName=A\n\n[Main]\n1 Forest\n1 Cultivate\n"
    new = "[metadata]\nName=B\n\n[Main]\n1 Forest\n1 Lotus Cobra\n"
    diff = diff_deck_text(old, new)
    assert "1 Lotus Cobra" in diff["added"]
    assert "1 Cultivate" in diff["removed"]


def test_diff_deck_text_handles_empty():
    from commander_builder.compare_versions import diff_deck_text
    diff = diff_deck_text("", "")
    assert diff["added"] == []
    assert diff["removed"] == []


def test_diff_deck_text_skips_non_main_sections():
    from commander_builder.compare_versions import diff_deck_text
    old = "[Commander]\n1 Edgar\n[Main]\n1 Forest\n"
    new = "[Commander]\n1 Edgar\n[Main]\n1 Mountain\n"
    diff = diff_deck_text(old, new)
    # Edgar in Commander section is excluded; only [Main] differs.
    assert diff["added"] == ["1 Mountain"]
    assert diff["removed"] == ["1 Forest"]


# --- _pick_filler_pairs ----------------------------------------------------

def test_pick_filler_pairs_uses_pool_when_available(tmp_path, monkeypatch):
    # Fake _load_pool to return a known list, isolating from on-disk state.
    pool = ["a.dck", "b.dck", "c.dck", "d.dck", "e.dck", "f.dck"]
    monkeypatch.setattr(
        "commander_builder.compare_versions._load_pool",
        lambda bracket: list(pool),
    )
    pairs = _pick_filler_pairs(bracket=3, exclude=["x.dck", "y.dck"], num_pairs=2)
    assert len(pairs) == 2
    for pair in pairs:
        assert len(pair) == 2
    # No pair contains the excluded decks.
    flat = [d for pair in pairs for d in pair]
    assert "x.dck" not in flat and "y.dck" not in flat


def test_pick_filler_pairs_excludes_versions_under_test(tmp_path, monkeypatch):
    pool = ["old.dck", "new.dck", "a.dck", "b.dck"]
    monkeypatch.setattr(
        "commander_builder.compare_versions._load_pool",
        lambda bracket: list(pool),
    )
    pairs = _pick_filler_pairs(bracket=3, exclude=["old.dck", "new.dck"], num_pairs=1)
    flat = [d for pair in pairs for d in pair]
    assert "old.dck" not in flat
    assert "new.dck" not in flat


def test_pick_filler_pairs_raises_when_too_few_candidates(monkeypatch):
    monkeypatch.setattr(
        "commander_builder.compare_versions._load_pool",
        lambda bracket: ["only_one.dck"],
    )
    monkeypatch.setattr(
        "commander_builder.compare_versions._fallback_opponents",
        lambda bracket, exclude, n: ["only_one.dck"],
    )
    with pytest.raises(RuntimeError):
        _pick_filler_pairs(bracket=3, exclude=["x.dck"], num_pairs=2)


# --- ComparisonReport.winner / .margin -------------------------------------

def _stats(name: str, wins: int) -> VersionStats:
    return VersionStats(deck_filename=name, wins=wins)


def test_winner_old_when_old_has_more_wins():
    r = ComparisonReport(
        old_deck="o", new_deck="n", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10,
        old_stats=_stats("o", 7), new_stats=_stats("n", 3),
    )
    assert r.winner == "old"
    assert r.margin == 4


def test_winner_new_when_new_has_more_wins():
    r = ComparisonReport(
        old_deck="o", new_deck="n", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10,
        old_stats=_stats("o", 2), new_stats=_stats("n", 8),
    )
    assert r.winner == "new"
    assert r.margin == 6


def test_winner_tie_when_equal():
    r = ComparisonReport(
        old_deck="o", new_deck="n", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10,
        old_stats=_stats("o", 4), new_stats=_stats("n", 4),
    )
    assert r.winner == "tie"
    assert r.margin == 0


def test_to_dict_includes_winner_and_margin():
    r = ComparisonReport(
        old_deck="o", new_deck="n", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10,
        old_stats=_stats("o", 4), new_stats=_stats("n", 6),
    )
    d = r.to_dict()
    assert d["winner"] == "new"
    assert d["margin"] == 2


def test_to_json_round_trips():
    r = ComparisonReport(
        old_deck="o", new_deck="n", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10,
        old_stats=_stats("o", 4), new_stats=_stats("n", 6),
    )
    d = json.loads(r.to_json())
    assert d["old_deck"] == "o"
    assert d["winner"] == "new"


# --- compare() — full integration with mocked Forge runner ---------------

def _staged_deck(path, body: str = "[Commander]\n1 Test\n"):
    """Write a minimal .dck at `path` so file-existence checks pass."""
    path.write_text(body, encoding="utf-8")
    return path


def test_compare_with_mocked_runner(tmp_path, monkeypatch):
    """compare() runs the full path: filler-pair selection → 2 pods → log_parser
    → game_analyzer → aggregation → JSON write. Mock at the runner boundary
    so the test stays offline."""
    from commander_builder import compare_versions
    from commander_builder.forge_runner import SimResult

    # Stage decks under a fake DECK_DIR.
    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    for name in [
        "[USER] Old [B3].dck",
        "[USER] New [B3].dck",
        "FillerA [B3].dck",
        "FillerB [B3].dck",
        "FillerC [B3].dck",
        "FillerD [B3].dck",
    ]:
        _staged_deck(deck_dir / name)
    monkeypatch.setattr(compare_versions, "DECK_DIR", deck_dir)
    monkeypatch.setattr(compare_versions, "COMPARE_OUT_DIR", tmp_path / "_compare")
    # Force fallback opponents to use our deck_dir.
    monkeypatch.setattr(
        "commander_builder.run_match.DECK_DIR", deck_dir,
    )
    # No curated pool present → fallback path picks alphabetical.
    monkeypatch.setattr(
        "commander_builder.compare_versions._load_pool",
        lambda bracket: [],
    )

    # Hand-crafted Forge stdout: New wins both games of pod 1, Old wins pod 2.
    pod1_stdout = (
        "Match Result: Ai(1)-Old: 0 Ai(2)-New: 2 Ai(3)-FillerA: 0 Ai(4)-FillerB: 0\n"
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-New has won!\n"
    )
    pod2_stdout = (
        "Match Result: Ai(1)-Old: 2 Ai(2)-New: 0 Ai(3)-FillerC: 0 Ai(4)-FillerD: 0\n"
        "Game Result: Game 1 ended in 60000 ms. Ai(1)-Old has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(1)-Old has won!\n"
    )
    pod_results = iter([
        SimResult(cmd=["x"], returncode=0, duration_sec=120,
                  stdout=pod1_stdout, stderr="", timed_out=False, error=None),
        SimResult(cmd=["x"], returncode=0, duration_sec=120,
                  stdout=pod2_stdout, stderr="", timed_out=False, error=None),
    ])

    class FakeRunner:
        def run(self, *args, **kwargs):
            return next(pod_results)

    report = compare_versions.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3,
        games_per_pod=2,
        filler_pairs=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
    )

    # Equal wins across the two pods → tie.
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 2
    assert report.winner == "tie"
    assert report.margin == 0
    assert report.total_games == 4
    assert len(report.pods) == 2
    # JSON was persisted.
    out_files = list((tmp_path / "_compare").glob("*.json"))
    assert len(out_files) == 1


def test_compare_rejects_same_old_and_new(tmp_path, monkeypatch):
    from commander_builder import compare_versions
    monkeypatch.setattr(compare_versions, "DECK_DIR", tmp_path)
    _staged_deck(tmp_path / "Same [B3].dck")

    class FakeRunner:
        def run(self, *a, **kw):
            raise AssertionError("should fail before runner.run")

    with pytest.raises(ValueError):
        compare_versions.compare(
            old_deck="Same [B3].dck",
            new_deck="Same [B3].dck",
            bracket=3,
            runner=FakeRunner(),
        )


def test_compare_rejects_missing_old(tmp_path, monkeypatch):
    from commander_builder import compare_versions
    monkeypatch.setattr(compare_versions, "DECK_DIR", tmp_path)
    _staged_deck(tmp_path / "New [B3].dck")

    class FakeRunner:
        def run(self, *a, **kw):
            raise AssertionError("should fail before runner.run")

    with pytest.raises(FileNotFoundError):
        compare_versions.compare(
            old_deck="DoesNotExist [B3].dck",
            new_deck="New [B3].dck",
            bracket=3,
            runner=FakeRunner(),
        )


def test_compare_rejects_invalid_mode(tmp_path, monkeypatch):
    from commander_builder import compare_versions
    monkeypatch.setattr(compare_versions, "DECK_DIR", tmp_path)
    _staged_deck(tmp_path / "Old [B3].dck")
    _staged_deck(tmp_path / "New [B3].dck")

    class FakeRunner:
        def run(self, *a, **kw):
            raise AssertionError("should fail before runner.run")

    with pytest.raises(ValueError):
        compare_versions.compare(
            old_deck="Old [B3].dck",
            new_deck="New [B3].dck",
            bracket=3,
            mode="invalid",
            runner=FakeRunner(),
        )


def test_format_summary_shows_head_to_head_line():
    r = ComparisonReport(
        old_deck="old.dck", new_deck="new.dck", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10, total_games=20, draws=2,
        old_stats=VersionStats(deck_filename="old.dck", wins=8, avg_ending_life=15.0),
        new_stats=VersionStats(deck_filename="new.dck", wins=10, avg_ending_life=22.0),
    )
    s = _format_summary(r)
    assert "OLD 8 - 10 NEW" in s
    assert "winner: NEW" in s
    assert "margin 2" in s
