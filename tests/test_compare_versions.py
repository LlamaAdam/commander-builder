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
    _is_decisive,
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


# ---------------------------------------------------------------------------
# Sprint 1A — parallel pod execution
# ---------------------------------------------------------------------------

def _staged_deck_min(path):
    """Lightweight .dck stub used by the parallelism tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[metadata]\nName=" + path.stem + "\n[Commander]\n1 X\n[Main]\n1 Forest\n",
        encoding="utf-8",
    )


def _make_pod_stdout(old_wins: int, new_wins: int) -> str:
    """Build Forge stdout that the log_parser will read as the given W/L."""
    head = (
        f"Match Result: Ai(1)-Old: {old_wins} Ai(2)-New: {new_wins} "
        "Ai(3)-FillerA: 0 Ai(4)-FillerB: 0\n"
    )
    games = []
    for _ in range(old_wins):
        games.append(
            "Game Result: Game ended in 60000 ms. Ai(1)-Old has won!\n"
        )
    for _ in range(new_wins):
        games.append(
            "Game Result: Game ended in 60000 ms. Ai(2)-New has won!\n"
        )
    return head + "".join(games)


def _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=2):
    from commander_builder import compare_versions
    deck_dir = tmp_path / "decks" / "commander"
    decks = ["[USER] Old [B3].dck", "[USER] New [B3].dck"]
    for i in range(num_filler_pairs * 2):
        decks.append(f"Filler{i} [B3].dck")
    for n in decks:
        _staged_deck_min(deck_dir / n)
    monkeypatch.setattr(compare_versions, "DECK_DIR", deck_dir)
    monkeypatch.setattr(compare_versions, "COMPARE_OUT_DIR", tmp_path / "_compare")
    monkeypatch.setattr("commander_builder.run_match.DECK_DIR", deck_dir)
    monkeypatch.setattr(
        "commander_builder.compare_versions._load_pool",
        lambda bracket: [],
    )
    return compare_versions, deck_dir


def test_compare_parallel_aggregates_pod_results_in_input_order(
    tmp_path, monkeypatch,
):
    """Parallel pods complete in arbitrary order. The aggregated report
    must still list pods in their original input order so
    `report.pods[i]` lines up with `report.filler_pairs_used[i]`."""
    import time
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=2)

    # Pod 0 sleeps long, pod 1 sleeps short — pod 1 will FINISH first
    # but must still land in slot 1 of report.pods.
    sleeps = [0.3, 0.05]
    pod_outputs = [_make_pod_stdout(2, 0), _make_pod_stdout(0, 2)]
    call_count = {"n": 0}

    class FakeRunner:
        def run(self, pod, *args, **kwargs):
            # The deck list identifies which pod we're in: pod 0 has
            # Filler0/Filler1, pod 1 has Filler2/Filler3.
            idx = 0 if "Filler0 [B3].dck" in pod else 1
            call_count["n"] += 1
            time.sleep(sleeps[idx])
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=sleeps[idx],
                stdout=pod_outputs[idx], stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3,
        games_per_pod=2,
        filler_pairs=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=True,
    )

    assert call_count["n"] == 2
    assert len(report.pods) == 2
    # Pod 0 (Old wins both) and Pod 1 (New wins both) → final tie.
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 2
    # Order check: pod 0 in report.pods uses Filler0/Filler1.
    assert "Filler0 [B3].dck" in report.pods[0]["pod"]
    assert "Filler2 [B3].dck" in report.pods[1]["pod"]
    # Pod indexes are 1-based and ordered.
    assert report.pods[0]["pod_index"] == 1
    assert report.pods[1]["pod_index"] == 2


def test_compare_parallel_runs_pods_concurrently(tmp_path, monkeypatch):
    """Two pods that each sleep S seconds should complete in roughly
    S seconds total when run in parallel, not 2S. Use a generous
    margin (1.6×) to keep this stable on slow CI."""
    import time
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=2)

    SLEEP = 0.4
    stdout = _make_pod_stdout(1, 1)

    class FakeRunner:
        def run(self, *args, **kwargs):
            time.sleep(SLEEP)
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=SLEEP,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    t0 = time.monotonic()
    cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=2, filler_pairs=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=True,
    )
    elapsed_parallel = time.monotonic() - t0

    # Sequential would be ~2× SLEEP. Parallel should be much closer to SLEEP.
    # Allow generous slack for thread-pool overhead.
    assert elapsed_parallel < SLEEP * 1.6, (
        f"parallel wall-time {elapsed_parallel:.2f}s, expected < "
        f"{SLEEP * 1.6:.2f}s — pods may not be running concurrently"
    )


def test_compare_sequential_fallback_used_when_parallel_false(
    tmp_path, monkeypatch,
):
    """parallel=False forces sequential execution; useful for
    deterministic logging / debug. Verify by checking elapsed time
    grows linearly with pod count."""
    import time
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=2)

    SLEEP = 0.2
    stdout = _make_pod_stdout(1, 1)

    class FakeRunner:
        def run(self, *args, **kwargs):
            time.sleep(SLEEP)
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=SLEEP,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    t0 = time.monotonic()
    cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=2, filler_pairs=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    elapsed_seq = time.monotonic() - t0
    # Sequential 2 pods × SLEEP each → expect ~2 × SLEEP, allow slop.
    assert elapsed_seq >= SLEEP * 1.8, (
        f"sequential wall-time {elapsed_seq:.2f}s, expected >= "
        f"{SLEEP * 1.8:.2f}s — pods seem to have run concurrently"
    )


# ---------------------------------------------------------------------------
# Sprint 1B — adaptive early-stop
# ---------------------------------------------------------------------------

def test_is_decisive_locks_when_margin_exceeds_remaining_games():
    # 5 wins to 0 with 4 games remaining → can't be flipped → decisive.
    assert _is_decisive(old_wins=5, new_wins=0, games_remaining=4) is True


def test_is_decisive_not_decisive_when_remaining_could_flip():
    # 5 wins to 0 with 5 games remaining → could end 5-5 (tie). Not decisive.
    assert _is_decisive(old_wins=5, new_wins=0, games_remaining=5) is False


def test_is_decisive_close_match_is_not_decisive():
    assert _is_decisive(old_wins=3, new_wins=2, games_remaining=10) is False


def test_is_decisive_zero_remaining_is_always_decisive():
    assert _is_decisive(old_wins=0, new_wins=0, games_remaining=0) is True


def test_compare_early_stops_when_first_two_pods_decisive(tmp_path, monkeypatch):
    """Run 4 pods sequentially. After pod 2, cumulative is 10-0 with 10
    games remaining (2 pods × 5 games) — that's NOT yet decisive
    (margin equals remaining). After pod 3 it would be 15-0 with 5
    remaining, decisive. Test that pod 4 is skipped."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=4)

    # Each pod: Old wins all 5 games. After 3 pods cumulative is 15-0
    # with 5 remaining → margin 15 > 5 → decisive → cancel pod 4.
    stdout = _make_pod_stdout(5, 0)
    calls = {"n": 0}

    class FakeRunner:
        def run(self, *args, **kwargs):
            calls["n"] += 1
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.01,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=4,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
        early_stop=True,
    )
    # Sequential + early-stop: should stop after pod 3 (not run pod 4).
    assert calls["n"] == 3
    assert report.stopped_early is True
    assert report.pods_planned == 4
    assert len(report.pods) == 3
    assert report.old_stats.wins == 15
    assert report.new_stats.wins == 0


def test_compare_does_not_early_stop_on_close_results(tmp_path, monkeypatch):
    """3-2 split per pod, 4 pods. Margin never exceeds remaining games
    so no early stop fires."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=4)

    stdout = _make_pod_stdout(3, 2)
    calls = {"n": 0}

    class FakeRunner:
        def run(self, *args, **kwargs):
            calls["n"] += 1
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.01,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=4,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
        early_stop=True,
    )
    # All 4 pods ran.
    assert calls["n"] == 4
    assert report.stopped_early is False
    assert len(report.pods) == 4


def test_compare_early_stop_disabled_runs_all_pods(tmp_path, monkeypatch):
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=4)
    stdout = _make_pod_stdout(5, 0)
    calls = {"n": 0}

    class FakeRunner:
        def run(self, *args, **kwargs):
            calls["n"] += 1
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.01,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=4,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
        early_stop=False,
    )
    # Even though decisive after pod 3, all 4 pods ran because
    # early_stop=False.
    assert calls["n"] == 4
    assert report.stopped_early is False
    assert len(report.pods) == 4


def test_compare_never_stops_on_first_pod_alone(tmp_path, monkeypatch):
    """Even if pod 1 is 5-0, we don't early-stop after a single pod —
    too noisy to trust without a second sample."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=4)
    # First pod massively decisive (5-0), but 3 pods × 5 = 15 games
    # remaining; margin is 5, so the "can it be flipped" check is True
    # (5 < 15). Even if it weren't, the explicit "skip first-pod" guard
    # in _check_early_stop() would block early-stop. Verify pod 2
    # always runs at minimum.
    stdout = _make_pod_stdout(5, 0)
    calls = {"n": 0}

    class FakeRunner:
        def run(self, *args, **kwargs):
            calls["n"] += 1
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.01,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=4,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
        early_stop=True,
    )
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Sprint 1C — per-pod adaptive game-stop (intra-pod abort)
# ---------------------------------------------------------------------------

def test_pod_abort_check_fires_when_in_pod_margin_uncatchable():
    """Once in-pod margin > games-remaining, the abort callback returns
    True so the runner can kill the JVM."""
    from commander_builder.compare_versions import _make_pod_abort_check
    pod = ["[USER] Old [B3].dck", "[USER] New [B3].dck",
           "Filler1.dck", "Filler2.dck"]
    abort_check, state = _make_pod_abort_check(
        pod, pod[0], pod[1], games_per_pod=5,
    )
    # Game 1: New wins.
    assert abort_check(
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-New has won!"
    ) is False
    # Game 2: New wins.
    assert abort_check(
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-New has won!"
    ) is False
    # Game 3: New wins. After this margin=3, games_remaining=2 →
    # 3 > 2 → abort.
    assert abort_check(
        "Game Result: Game 3 ended in 60000 ms. Ai(2)-New has won!"
    ) is True
    assert state["new_wins"] == 3
    assert state["old_wins"] == 0
    assert state["aborted"] is True


def test_pod_abort_check_doesnt_fire_when_close():
    from commander_builder.compare_versions import _make_pod_abort_check
    pod = ["[USER] Old [B3].dck", "[USER] New [B3].dck"]
    abort_check, state = _make_pod_abort_check(
        pod, pod[0], pod[1], games_per_pod=5,
    )
    # Three games with 2-1 split → margin 1, remaining 2 → not decisive.
    assert abort_check(
        "Game Result: Game 1 ended in 60000 ms. Ai(1)-Old has won!"
    ) is False
    assert abort_check(
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-New has won!"
    ) is False
    assert abort_check(
        "Game Result: Game 3 ended in 60000 ms. Ai(1)-Old has won!"
    ) is False
    assert state["aborted"] is False


def test_pod_abort_check_ignores_non_game_lines():
    from commander_builder.compare_versions import _make_pod_abort_check
    pod = ["[USER] Old [B3].dck", "[USER] New [B3].dck"]
    abort_check, _ = _make_pod_abort_check(pod, pod[0], pod[1], games_per_pod=5)
    assert abort_check("Phase: Ai(1)-Old A Untap") is False
    assert abort_check("Turn: Turn 5 (Ai(2)-New)") is False
    assert abort_check("") is False


def test_pod_abort_check_handles_filler_wins():
    """Filler wins consume games-remaining but don't shift the
    old-vs-new margin; verify the math stays consistent."""
    from commander_builder.compare_versions import _make_pod_abort_check
    pod = ["[USER] Old [B3].dck", "[USER] New [B3].dck",
           "Filler1.dck", "Filler2.dck"]
    abort_check, state = _make_pod_abort_check(
        pod, pod[0], pod[1], games_per_pod=5,
    )
    # 3 filler wins, 1 New win. Margin 1, remaining 1 → still possible
    # to flip (Old could win the last one) → not aborted.
    abort_check("Game Result: Game 1 ended in 60000 ms. Ai(3)-Filler1 has won!")
    abort_check("Game Result: Game 2 ended in 60000 ms. Ai(3)-Filler1 has won!")
    abort_check("Game Result: Game 3 ended in 60000 ms. Ai(3)-Filler1 has won!")
    res = abort_check("Game Result: Game 4 ended in 60000 ms. Ai(2)-New has won!")
    assert res is False
    assert state["games_seen"] == 4
    assert state["new_wins"] == 1
    assert state["old_wins"] == 0


def test_synthesize_match_result_builds_parseable_line():
    """When abort kills Forge before the trailing Match Result, the
    synthesized one must be in the format log_parser.parse() expects."""
    from commander_builder.compare_versions import _synthesize_match_result
    from commander_builder.log_parser import parse
    state = {
        "wins_by_seat_name": {
            (1, "Old"): 0, (2, "New"): 3,
            (3, "Filler1"): 0, (4, "Filler2"): 0,
        },
    }
    line = _synthesize_match_result(state)
    assert line.startswith("Match Result:")
    parsed = parse(line)
    by_name = {dr.name: dr.wins for dr in parsed.deck_results}
    assert by_name == {"Old": 0, "New": 3, "Filler1": 0, "Filler2": 0}


def test_compare_intra_pod_abort_synthesizes_results_when_killed(
    tmp_path, monkeypatch,
):
    """Full-pod simulation: stub a runner that emits per-game winner
    lines via on_line/abort_check, gets killed mid-pod, and verify
    the report counts the games that were actually played."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=1)

    # Build streaming output that the runner would produce: 3 games
    # where New wins all. After game 3 the abort_check fires (margin
    # 3 > remaining 2 in a 5-game pod).
    per_game_lines = [
        f"Game Result: Game {i} ended in 60000 ms. Ai(2)-New has won!\n"
        for i in (1, 2, 3)
    ]

    class StreamingFakeRunner:
        def run(self, pod, num_games, game_format="commander",
                timeout_sec=None, stream=False, on_line=None,
                abort_check=None):
            # Stream each per-game line through abort_check; stop when
            # it returns True. Don't append a Match Result line —
            # compare_versions._run_one_pod should synthesize one.
            stdout_emitted = ""
            killed = False
            for line in per_game_lines:
                stdout_emitted += line
                if abort_check is not None and abort_check(line.rstrip("\n")):
                    killed = True
                    break
            return SimResult(
                cmd=["x"], returncode=0 if not killed else -9,
                duration_sec=0.5,
                stdout=stdout_emitted, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=1,
        runner=StreamingFakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    # New won 3 games and the pod was aborted; report should reflect it.
    assert report.pods[0]["intra_pod_aborted"] is True
    assert report.pods[0]["games_actually_played"] == 3
    assert report.new_stats.wins == 3
    assert report.old_stats.wins == 0
    # total_games counts what Forge actually played, not what we
    # asked for.
    assert report.total_games == 3


def test_compare_intra_pod_abort_disabled_runs_full_pod(tmp_path, monkeypatch):
    """When intra-pod abort doesn't fire (close match), the full pod
    runs and the trailing Match Result line is honored."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=1)

    full_stdout = (
        "Game Result: Game 1 ended in 60000 ms. Ai(1)-Old has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 3 ended in 60000 ms. Ai(1)-Old has won!\n"
        "Game Result: Game 4 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 5 ended in 60000 ms. Ai(2)-New has won!\n"
        "Match Result: Ai(1)-Old: 2 Ai(2)-New: 3 Ai(3)-Filler0: 0 Ai(4)-Filler1: 0\n"
    )

    class StreamingFakeRunner:
        def run(self, pod, num_games, game_format="commander",
                timeout_sec=None, stream=False, on_line=None,
                abort_check=None):
            for line in full_stdout.splitlines(True):
                if abort_check is not None:
                    abort_check(line.rstrip("\n"))
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.5,
                stdout=full_stdout, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=1,
        runner=StreamingFakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    assert report.pods[0]["intra_pod_aborted"] is False
    assert report.pods[0]["games_actually_played"] == 5
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 3


# ---------------------------------------------------------------------------
# Sprint 1E — auto-tuned filler_pairs by CPU count
# ---------------------------------------------------------------------------

def test_auto_filler_pairs_scales_with_cores(monkeypatch):
    from commander_builder.compare_versions import auto_filler_pairs
    monkeypatch.setattr("commander_builder.compare_versions.os.cpu_count",
                        lambda: 8)
    # 8 cores → cap at 4.
    assert auto_filler_pairs() == 4


def test_auto_filler_pairs_caps_at_four():
    """Capped to avoid spawning more JVMs than reasonable on a beefy box."""
    from commander_builder.compare_versions import auto_filler_pairs
    # 16 cores still returns 4 — pods past the cap just queue up.
    import unittest.mock as _m
    with _m.patch(
        "commander_builder.compare_versions.os.cpu_count", return_value=16,
    ):
        assert auto_filler_pairs() == 4


def test_auto_filler_pairs_floors_at_two(monkeypatch):
    from commander_builder.compare_versions import auto_filler_pairs
    # 1-core box should still get 2 pairs so the filler-pair averaging
    # has something to average. The pods will run sequentially.
    monkeypatch.setattr("commander_builder.compare_versions.os.cpu_count",
                        lambda: 1)
    assert auto_filler_pairs() == 2


def test_auto_filler_pairs_handles_none_cpu_count(monkeypatch):
    """os.cpu_count() returns None on some platforms; we should still
    produce a sane default."""
    from commander_builder.compare_versions import auto_filler_pairs
    monkeypatch.setattr("commander_builder.compare_versions.os.cpu_count",
                        lambda: None)
    assert auto_filler_pairs() == 2


def test_compare_single_pod_skips_threadpool(tmp_path, monkeypatch):
    """1v1 mode runs a single pod. We don't need a threadpool for that;
    verify it still works and the threadpool short-circuits."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch)

    stdout = _make_pod_stdout(2, 0)

    class FakeRunner:
        def __init__(self):
            self.calls = 0
        def run(self, *args, **kwargs):
            self.calls += 1
            return SimResult(
                cmd=["x"], returncode=0, duration_sec=0.01,
                stdout=stdout, stderr="", timed_out=False, error=None,
            )

    fr = FakeRunner()
    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=2,
        mode="1v1",
        runner=fr,
        out_dir=tmp_path / "_compare",
        parallel=True,
    )
    assert fr.calls == 1
    assert len(report.pods) == 1
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 0


# ---------------------------------------------------------------------------
# End-to-end Name= alignment — snapshot writer -> Forge names -> attribution
# ---------------------------------------------------------------------------

def test_compare_attributes_wins_from_snapshot_decks_internal_names(
    tmp_path, monkeypatch,
):
    """Regression for the snapshot-Name= misattribution.

    Real Forge reports each seat's [metadata] Name= field in its Match
    Result lines — NOT the filename. snapshot() used to be a plain copy,
    so '[USER] My Deck v1 [B3].dck' and '... v2 ...' both reported the
    SOURCE deck's 'Name=My Deck'; _aggregate_pod (which keys on the
    normalized filenames 'My Deck v1' / 'My Deck v2') matched neither and
    every snapshot A/B scored 0-0.

    This test drives the REAL snapshot writer, then fakes only the Forge
    boundary: the fake runner reads each pod deck's Name= from disk (as
    Forge would) and reports those names. With the plain-copy writer the
    assertions below read 0-0; with the Name=-stamping writer the wins
    land on the correct versions.
    """
    import re as _re

    from commander_builder import compare_versions as cv
    from commander_builder.forge_runner import SimResult
    from commander_builder.snapshot_deck import snapshot

    deck_dir = tmp_path / "decks" / "commander"
    deck_dir.mkdir(parents=True)
    src = deck_dir / "[USER] My Deck [B3].dck"
    # Name= as moxfield_import writes it: the RAW deck name, not the stem.
    src.write_text(
        "[metadata]\nName=My Deck\n[Commander]\n1 Cmdr\n[Main]\n1 Forest\n",
        encoding="utf-8",
    )
    for f in ("Filler0 [B3].dck", "Filler1 [B3].dck"):
        p = deck_dir / f
        p.write_text(
            f"[metadata]\nName={p.stem}\n[Commander]\n1 X\n[Main]\n1 Forest\n",
            encoding="utf-8",
        )

    # Stage v1/v2 through the real writer under test.
    v1 = snapshot("[USER] My Deck [B3].dck", "v1", base=deck_dir)
    v2 = snapshot("[USER] My Deck [B3].dck", "v2", base=deck_dir)
    assert v1.name == "[USER] My Deck v1 [B3].dck"
    assert v2.name == "[USER] My Deck v2 [B3].dck"

    monkeypatch.setattr(cv, "DECK_DIR", deck_dir)
    monkeypatch.setattr(
        cv, "_load_pool",
        lambda bracket: ["Filler0 [B3].dck", "Filler1 [B3].dck"],
    )

    def _name_of(deck_filename: str) -> str:
        """What Forge would report for this seat: the internal Name=."""
        text = (deck_dir / deck_filename).read_text(encoding="utf-8")
        m = _re.search(r"^Name=(.+)$", text, _re.MULTILINE)
        assert m, f"{deck_filename} has no Name= line"
        return m.group(1)

    class FakeRunner:
        """Replays a fixed 2-1-0-0 pod using the decks' INTERNAL names."""

        def run(self, pod, num_games=0, **kwargs):
            names = [_name_of(f) for f in pod]
            wins = [2, 1, 0, 0]
            match = " ".join(
                f"Ai({i + 1})-{n}: {w}"
                for i, (n, w) in enumerate(zip(names, wins))
            )
            games = "".join(
                f"Game Result: Game ended in 60000 ms. "
                f"Ai({i + 1})-{n} has won!\n"
                for i, (n, w) in enumerate(zip(names, wins))
                for _ in range(w)
            )
            return SimResult(
                cmd=["fake"], returncode=0, duration_sec=0.01,
                stdout=f"Match Result: {match}\n{games}",
                stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck=v1.name,
        new_deck=v2.name,
        bracket=3,
        games_per_pod=3,
        filler_pairs=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )

    # Seat 1 (v1) won 2, seat 2 (v2) won 1. Pre-fix both stats read 0
    # because 'My Deck' normalized to neither 'My Deck v1' nor 'My Deck v2'.
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 1


# ---------------------------------------------------------------------------
# Pod-failure surfacing — crashed / timed-out pods must not dilute stats
# ---------------------------------------------------------------------------

def test_salvage_wins_from_stdout_parses_per_game_lines():
    """Post-hoc salvage must recover the same per-game winner tallies the
    streaming abort-check would have accumulated."""
    from commander_builder.compare_versions import (
        _salvage_wins_from_stdout,
        _synthesize_match_result,
    )
    from commander_builder.log_parser import parse

    stdout = (
        "Turn: Turn 3 (Ai(1)-Old)\n"
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(1)-Old has won!\n"
        "Game Result: Game 3 ended in 60000 ms. Ai(2)-New has won!\n"
    )
    state = _salvage_wins_from_stdout(stdout)
    assert state["games_seen"] == 3
    line = _synthesize_match_result(state)
    parsed = parse(line)
    by_name = {dr.name: dr.wins for dr in parsed.deck_results}
    assert by_name == {"Old": 1, "New": 2}


def test_compare_excludes_crashed_pod_and_flags_report(
    tmp_path, monkeypatch, capsys,
):
    """A pod whose JVM died at startup (nonzero rc, no games) used to
    contribute 0 games with no warning; a pod that crashed mid-run with
    per-game lines but no Match Result silently DILUTED win rates. Both
    must now be excluded and surfaced."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=2)
    good_stdout = _make_pod_stdout(2, 0)

    class FakeRunner:
        def run(self, pod, *args, **kwargs):
            if "Filler0 [B3].dck" in pod:
                return SimResult(
                    cmd=["x"], returncode=0, duration_sec=1.0,
                    stdout=good_stdout, stderr="", timed_out=False, error=None,
                )
            # Pod 2: JVM crash — nonzero rc, nothing usable on stdout.
            return SimResult(
                cmd=["x"], returncode=1, duration_sec=0.1,
                stdout="", stderr="java.lang.NoClassDefFoundError",
                timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=2, filler_pairs=2,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
        early_stop=False,
    )

    # Only the healthy pod's games count.
    assert report.total_games == 2
    assert report.old_stats.wins == 2
    assert report.new_stats.wins == 0
    # The failure is flagged everywhere a consumer might look.
    assert report.failed_pods == 1
    assert len(report.pod_failures) == 1
    assert report.pod_failures[0]["reason"] == "Forge exited with code 1"
    # The failed pod still appears in the pods list (post-mortem data)
    # with its failure flag set.
    assert len(report.pods) == 2
    failed_entries = [p for p in report.pods if p["pod_failed"]]
    assert len(failed_entries) == 1
    assert failed_entries[0]["failure_reason"] == "Forge exited with code 1"
    # to_dict carries the new fields for the web dashboard / analyst.
    d = report.to_dict()
    assert d["failed_pods"] == 1
    assert d["pod_failures"][0]["returncode"] == 1
    # Loud warning on the console.
    out = capsys.readouterr().out
    assert "FAILED" in out and "EXCLUDED" in out


def test_compare_crashed_pod_partial_games_not_counted_as_dilution(
    tmp_path, monkeypatch,
):
    """The exact dilution vector from the bug report: a killed pod streamed
    N per-game winner lines but no trailing Match Result. parse() yields
    games_completed=N with deck_results=[] — pre-fix those N games entered
    total_games with 0 wins for both sides. Crashes are NOT salvaged
    (consistent with forge_runner's A/B policy) so the games must be
    excluded entirely."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=1)
    partial = (
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(2)-New has won!\n"
    )

    class FakeRunner:
        def run(self, *args, **kwargs):
            return SimResult(
                cmd=["x"], returncode=137, duration_sec=5.0,
                stdout=partial, stderr="", timed_out=False, error=None,
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    # Pre-fix: total_games == 2 with 0 wins each (silent dilution).
    assert report.total_games == 0
    assert report.failed_pods == 1
    assert report.excluded_games == 2
    assert report.pod_failures[0]["unattributed_games"] == 2


def test_compare_timeout_salvages_partial_games_via_synthesis(
    tmp_path, monkeypatch, capsys,
):
    """A timed-out pod that streamed per-game winner lines but was killed
    before the trailing Match Result gets a synthesized one (same as the
    intra-pod abort path): the finished games are attributed and counted,
    the truncation is flagged, and nothing is booked as a phantom loss."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=1)
    partial = (
        "Game Result: Game 1 ended in 60000 ms. Ai(2)-New has won!\n"
        "Game Result: Game 2 ended in 60000 ms. Ai(1)-Old has won!\n"
    )

    class FakeRunner:
        def run(self, *args, **kwargs):
            return SimResult(
                cmd=["x"], returncode=None, duration_sec=600.0,
                stdout=partial, stderr="", timed_out=True,
                error="Timed out after 600s",
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    # Pre-fix: total_games == 2 with 0 wins for both sides. Post-fix the
    # two finished games are attributed 1-1 via the synthesized summary.
    assert report.failed_pods == 0
    assert report.timed_out_pods == 1
    assert report.old_stats.wins == 1
    assert report.new_stats.wins == 1
    assert report.total_games == 2
    assert report.pods[0]["timeout_salvaged"] is True
    out = capsys.readouterr().out
    assert "TIMED OUT" in out


def test_compare_timeout_with_nothing_salvageable_is_excluded(
    tmp_path, monkeypatch, capsys,
):
    """A pod that hung before ANY game finished (no per-game winner lines)
    has nothing to salvage: it is a failed pod, excluded and warned."""
    from commander_builder.forge_runner import SimResult

    cv, _ = _setup_compare_world(tmp_path, monkeypatch, num_filler_pairs=1)

    class FakeRunner:
        def run(self, *args, **kwargs):
            return SimResult(
                cmd=["x"], returncode=None, duration_sec=600.0,
                stdout="Turn: Turn 12 (Ai(1)-Old)\n", stderr="",
                timed_out=True, error="Timed out after 600s",
            )

    report = cv.compare(
        old_deck="[USER] Old [B3].dck",
        new_deck="[USER] New [B3].dck",
        bracket=3, games_per_pod=5, filler_pairs=1,
        runner=FakeRunner(),
        out_dir=tmp_path / "_compare",
        parallel=False,
    )
    assert report.total_games == 0
    assert report.failed_pods == 1
    assert report.timed_out_pods == 0
    assert report.pod_failures[0]["timed_out"] is True
    assert "Timed out" in report.pod_failures[0]["reason"]
    out = capsys.readouterr().out
    assert "FAILED" in out and "EXCLUDED" in out


def test_format_summary_surfaces_pod_failures():
    r = ComparisonReport(
        old_deck="old.dck", new_deck="new.dck", bracket=3, timestamp="x",
        mode="pod", games_per_pod=10, total_games=10, draws=0,
        old_stats=VersionStats(deck_filename="old.dck", wins=6),
        new_stats=VersionStats(deck_filename="new.dck", wins=4),
        failed_pods=1, excluded_games=3,
        pod_failures=[{
            "pod_index": 2, "pod": ["a", "b", "c", "d"],
            "reason": "Forge exited with code 1",
            "returncode": 1, "timed_out": False, "unattributed_games": 3,
        }],
    )
    s = _format_summary(r)
    assert "1 failed pod(s) EXCLUDED" in s
    assert "Forge exited with code 1" in s
