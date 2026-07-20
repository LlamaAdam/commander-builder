"""pool_curator unit tests.

Exercises the pure-Python helpers (no Forge subprocess required):
  - schedule_pods: round-robin coverage and determinism
  - _split_into_slices: rank ordering, swap-on-violation, copy-safety
  - CuratedPool.to_dict: ensures @property fields are persisted
  - _filename_for_match: documents the current matching contract
"""
from commander_builder.pool_curator import (
    INFLATED_WIN_RATE_THRESHOLD,
    CandidateScore,
    CuratedPool,
    _filename_for_match,
    _split_into_slices,
    schedule_pods,
)


def _candidate(name: str, wins: int, played: int, archetype: str = "midrange", colors: str = "") -> CandidateScore:
    return CandidateScore(
        filename=f"{name} [B3].dck",
        games_played=played,
        wins=wins,
        archetype=archetype,
        color_identity=colors,
    )


# --- schedule_pods ---------------------------------------------------------

def test_schedule_pods_requires_at_least_4_candidates():
    import pytest
    with pytest.raises(ValueError):
        schedule_pods(["a", "b", "c"], pods_per_deck=3)


def test_schedule_pods_each_pod_is_size_4():
    candidates = [f"d{i}" for i in range(12)]
    pods = schedule_pods(candidates, pods_per_deck=3)
    for pod in pods:
        assert len(pod) == 4


def test_schedule_pods_evenly_distributes_coverage():
    candidates = [f"d{i}" for i in range(12)]
    pods = schedule_pods(candidates, pods_per_deck=3, seed=0)
    counts = {c: 0 for c in candidates}
    for pod in pods:
        for c in pod:
            counts[c] += 1
    # 12 decks × 3 / 4 = 9 pods, total slots = 36, exactly 3 per deck.
    assert all(v == 3 for v in counts.values())


def test_schedule_pods_is_deterministic_for_fixed_seed():
    candidates = [f"d{i}" for i in range(8)]
    a = schedule_pods(candidates, seed=42)
    b = schedule_pods(candidates, seed=42)
    assert a == b


# --- _split_into_slices ----------------------------------------------------

def test_split_into_slices_basic_rotation():
    # Archetypes chosen so neither slice (indexes 0/2/4 and 1/3/5) repeats —
    # which would trigger the diversity swap and break the rank assertion.
    archetypes = ["aggro", "midrange", "control", "combo", "stax", "control"]
    top6 = [
        _candidate(f"D{i}", wins=10 - i, played=10, archetype=archetypes[i])
        for i in range(6)
    ]
    a, b = _split_into_slices(top6)
    # Pool A = ranks 1, 3, 5 (indexes 0, 2, 4)
    assert a == ["D0 [B3].dck", "D2 [B3].dck", "D4 [B3].dck"]
    # Pool B = ranks 2, 4, 6 (indexes 1, 3, 5)
    assert b == ["D1 [B3].dck", "D3 [B3].dck", "D5 [B3].dck"]


def test_split_into_slices_handles_short_lists():
    archetypes = ["aggro", "midrange", "control", "combo"]
    top4 = [
        _candidate(f"D{i}", wins=4 - i, played=10, archetype=archetypes[i])
        for i in range(4)
    ]
    a, b = _split_into_slices(top4)
    assert a == ["D0 [B3].dck", "D2 [B3].dck"]
    assert b == ["D1 [B3].dck", "D3 [B3].dck"]


def test_split_into_slices_swaps_on_archetype_collision():
    # Ranks 1 and 3 (slice A) share archetype "combo" → trigger swap with rank 4.
    top6 = [
        _candidate("D0", 6, 10, archetype="combo"),
        _candidate("D1", 5, 10, archetype="aggro"),
        _candidate("D2", 4, 10, archetype="combo"),       # collides with D0
        _candidate("D3", 3, 10, archetype="control"),
        _candidate("D4", 2, 10, archetype="aggro"),
        _candidate("D5", 1, 10, archetype="midrange"),
    ]
    a, b = _split_into_slices(top6)
    # After swap of indexes 2 and 3: slice A pulls D0/D3/D4 (combo/control/aggro).
    assert a == ["D0 [B3].dck", "D3 [B3].dck", "D4 [B3].dck"]


def test_split_into_slices_does_not_mutate_caller_list():
    """GAP-006 fix: the new search builds local copies, so the caller's
    `top6` list is never reordered behind their back."""
    top6 = [
        _candidate("D0", 6, 10, archetype="combo"),
        _candidate("D1", 5, 10, archetype="aggro"),
        _candidate("D2", 4, 10, archetype="combo"),
        _candidate("D3", 3, 10, archetype="control"),
        _candidate("D4", 2, 10, archetype="aggro"),
        _candidate("D5", 1, 10, archetype="midrange"),
    ]
    before = [c.filename for c in top6]
    _split_into_slices(top6)
    after = [c.filename for c in top6]
    assert before == after, "caller's top6 was mutated"


def test_split_into_slices_finds_non_violating_via_later_swap():
    """Default and the first swap (2,3) both leave slice A with a combo
    collision; the (3,4) swap is the one that actually works. GAP-006:
    the prior one-shot 3↔4 swap would have stopped at the first attempt
    and shipped a violating split."""
    top6 = [
        _candidate("D0", 6, 10, archetype="combo"),     # slice A rank 1
        _candidate("D1", 5, 10, archetype="aggro"),     # slice B rank 2
        _candidate("D2", 4, 10, archetype="stax"),      # slice A rank 3
        _candidate("D3", 3, 10, archetype="control"),   # slice B rank 4
        _candidate("D4", 2, 10, archetype="combo"),     # slice A rank 5 — collides with D0
        _candidate("D5", 1, 10, archetype="midrange"),  # slice B rank 6
    ]
    a, b = _split_into_slices(top6)
    # Look up archetypes for whatever ended up in each slice.
    by_filename = {c.filename: c for c in top6}
    archs_a = [by_filename[fn].archetype for fn in a]
    archs_b = [by_filename[fn].archetype for fn in b]
    assert len(set(archs_a)) == len(archs_a), f"slice A has duplicates: {archs_a}"
    assert len(set(archs_b)) == len(archs_b), f"slice B has duplicates: {archs_b}"


def test_split_into_slices_warns_when_no_arrangement_works(capsys):
    """GAP-006: if every search candidate violates, log a WARN and ship the
    default rather than silently returning a violating arrangement."""
    # Six decks all the same archetype — no swap can fix it.
    top6 = [
        _candidate(f"D{i}", 10 - i, 10, archetype="midrange")
        for i in range(6)
    ]
    a, b = _split_into_slices(top6)
    captured = capsys.readouterr()
    assert "WARN" in captured.out
    # Default split still ships even when violating.
    assert a == ["D0 [B3].dck", "D2 [B3].dck", "D4 [B3].dck"]
    assert b == ["D1 [B3].dck", "D3 [B3].dck", "D5 [B3].dck"]


# --- CuratedPool.to_dict ---------------------------------------------------

def test_to_dict_includes_computed_properties_for_each_score():
    """Regression: asdict() drops @property fields. We override to_dict to
    re-attach win_rate / confirm_action_per_game / suspected_inflated."""
    high = _candidate("Inflated", wins=9, played=10)        # 0.9 win rate
    high.confirm_action_total = 30
    normal = _candidate("Normal", wins=5, played=10)        # 0.5 win rate
    rejected = _candidate("Bad", wins=0, played=0)
    rejected.rejected_reason = "preflight_crash:timeout"

    pool = CuratedPool(
        bracket=3,
        created_at="2026-04-26T00:00:00+00:00",
        pool_a=["Inflated [B3].dck"],
        pool_b=["Normal [B3].dck"],
        scores=[high, normal],
        rejected=[rejected],
    )
    d = pool.to_dict()
    # Verify each persisted score now has the computed fields.
    assert d["scores"][0]["win_rate"] == 0.9
    assert d["scores"][0]["confirm_action_per_game"] == 3.0
    assert d["scores"][0]["suspected_inflated"] is True   # 0.9 > 0.75 threshold
    assert d["scores"][1]["suspected_inflated"] is False
    assert d["scores"][1]["win_rate"] == 0.5
    # Rejected entries also get the fields (zero-safe).
    assert d["rejected"][0]["win_rate"] == 0.0
    assert d["rejected"][0]["suspected_inflated"] is False


def test_inflated_threshold_is_strict_greater_than():
    # Exactly at threshold should NOT be flagged.
    threshold = _candidate("Edge", wins=int(INFLATED_WIN_RATE_THRESHOLD * 100), played=100)
    pool = CuratedPool(bracket=3, created_at="x", scores=[threshold])
    assert pool.to_dict()["scores"][0]["suspected_inflated"] is False


# --- _filename_for_match ---------------------------------------------------

def test_filename_for_match_basic():
    candidates = ["Foo [B3].dck", "Bar [B3].dck"]
    assert _filename_for_match("Foo", candidates) == "Foo [B3].dck"
    assert _filename_for_match("Bar", candidates) == "Bar [B3].dck"
    assert _filename_for_match("Missing", candidates) is None


def test_sample_candidates_returns_all_when_under_cap():
    from commander_builder.pool_curator import _sample_candidates
    cands = ["a", "b", "c", "d"]
    assert _sample_candidates(cands, max_count=10, seed=0) == cands
    assert _sample_candidates(cands, max_count=0, seed=0) == cands


def test_sample_candidates_returns_n_when_over_cap():
    from commander_builder.pool_curator import _sample_candidates
    cands = [f"deck-{i}" for i in range(50)]
    out = _sample_candidates(cands, max_count=12, seed=0)
    assert len(out) == 12
    # Result is sorted (so the curator output is stable).
    assert out == sorted(out)


def test_sample_candidates_is_deterministic_per_seed():
    from commander_builder.pool_curator import _sample_candidates
    cands = [f"deck-{i}" for i in range(50)]
    a = _sample_candidates(cands, max_count=12, seed=42)
    b = _sample_candidates(cands, max_count=12, seed=42)
    assert a == b


def test_sample_candidates_different_seeds_pick_different_subsets():
    from commander_builder.pool_curator import _sample_candidates
    cands = [f"deck-{i}" for i in range(50)]
    a = _sample_candidates(cands, max_count=12, seed=0)
    b = _sample_candidates(cands, max_count=12, seed=1)
    # Possible but vanishingly unlikely the two subsets are identical.
    assert a != b


def test_filename_for_match_handles_collision_suffix():
    """GAP-004 fixed (2026-04-26): when `_uniquify` appended ` (N)` to a
    filename that collided with another sanitization, the previous
    exact-stem match returned None and the deck's wins silently fell on the
    floor. Now we strip the uniquify suffix before comparing as a fallback."""
    candidates = ["Blue Farm (2) [B3].dck"]
    assert _filename_for_match("Blue Farm", candidates) == "Blue Farm (2) [B3].dck"


def test_filename_for_match_strips_user_prefix():
    """`[USER]`-tagged decks should match against Forge's reported Name=
    (which doesn't include the prefix)."""
    candidates = ["[USER] Hakbal of the Surging Soul [B3].dck"]
    assert (
        _filename_for_match("Hakbal of the Surging Soul", candidates)
        == "[USER] Hakbal of the Surging Soul [B3].dck"
    )


def test_filename_for_match_prefers_exact_over_deuniquified():
    """When BOTH a `Blue Farm [B3].dck` and a `Blue Farm (2) [B3].dck` exist,
    the exact (non-uniquified) match wins. Otherwise the (2) deck's wins
    would spuriously route to the (1) deck."""
    from commander_builder.pool_curator import _filename_for_match
    candidates = ["Blue Farm (2) [B3].dck", "Blue Farm [B3].dck"]
    # Exact match wins even though it appears second in the list.
    assert _filename_for_match("Blue Farm", candidates) == "Blue Farm [B3].dck"


# --- _confirm_actions_for (per-deck confirmAction charging) ----------------

def test_confirm_actions_attributed_zero_is_a_real_zero():
    """When attribution worked for the pod (dict non-empty), a deck with no
    key had ZERO confirmAction events — it must NOT be charged the pod-average
    fallback. Pre-fix, a clean deck in a pod with 200 events was charged
    200 // 4 = 50 of its opponents' noise."""
    from commander_builder.log_parser import ParsedSim
    from commander_builder.pool_curator import _confirm_actions_for
    parsed = ParsedSim(
        confirm_action_cards=["CardX"] * 200,
        confirm_action_by_deck={"Noisy Deck": 200},
    )
    assert _confirm_actions_for(parsed, "Clean Deck", pod_size=4) == 0
    # The noisy deck still gets its full attributed count.
    assert _confirm_actions_for(parsed, "Noisy Deck", pod_size=4) == 200


def test_confirm_actions_fallback_when_attribution_unavailable():
    """Attribution entirely unavailable (empty dict — e.g. Forge build without
    Phase markers): the even-split fallback still produces a number, exactly
    as before the fix."""
    from commander_builder.log_parser import ParsedSim
    from commander_builder.pool_curator import _confirm_actions_for
    parsed = ParsedSim(
        confirm_action_cards=["CardX"] * 40,
        confirm_action_by_deck={},
    )
    assert _confirm_actions_for(parsed, "Any Deck", pod_size=4) == 10


# --- curate_bracket integration (fake runner, no Forge) --------------------

from commander_builder.forge_runner import SimResult
from commander_builder.pool_curator import (
    InsufficientSurvivorsError,
    curate_bracket,
)


def _stems(decks):
    """Filenames -> the Name= stems Forge reports in Match Result lines."""
    return [d.rsplit(" [B", 1)[0] for d in decks]


def _sim_stdout(decks, games=1, unsupported=(), confirm_by=None):
    """Craft Forge stdout that log_parser.parse() reads back as intended.

    confirm_by: {stem: event_count} — emitted with a Phase marker per deck so
    the parser attributes the events (confirm_action_by_deck non-empty)."""
    stems = _stems(decks)
    lines = []
    for card in unsupported:
        lines.append(f"An unsupported card was requested: {card}")
    for stem, count in (confirm_by or {}).items():
        seat = stems.index(stem) + 1
        lines.append(f"Phase: Ai({seat})-{stem} MAIN1")
        lines.extend(
            ["default implementation of confirmAction is used by CardX"] * count
        )
    for g in range(games):
        lines.append(f"Game Result: Game {g + 1} ended in 60000 ms")
    # Seat 1 takes every win; the tests here assert survival/rejection, not
    # ranking, so any consistent tally works.
    payload = " ".join(
        f"Ai({i + 1})-{s}: {games if i == 0 else 0}" for i, s in enumerate(stems)
    )
    lines.append(f"Match Result: {payload}")
    return "\n".join(lines) + "\n"


class _FakeRunner:
    """Duck-typed ForgeRunner. `bad_decks` inject one unsupported-card line
    into any pod containing them (pod-global, like real Forge output);
    `noisy_decks` inject 300 attributed confirmAction events; `crash_all`
    makes every sim time out. Records every call for assertions."""

    def __init__(self, bad_decks=(), noisy_decks=(), crash_all=False):
        self.bad_decks = set(bad_decks)
        self.noisy_decks = set(noisy_decks)
        self.crash_all = crash_all
        self.calls = []  # (decks, num_games)

    def run(self, decks, num_games=1, **kwargs):
        self.calls.append((list(decks), num_games))
        if self.crash_all:
            return SimResult(
                cmd=["x"], returncode=None, duration_sec=600.0,
                stdout="", stderr="", timed_out=True,
                error="Timed out after 600s",
            )
        unsupported = ["Frobnicate the Bear"] if self.bad_decks & set(decks) else []
        confirm_by = {
            stem: 300
            for d, stem in zip(decks, _stems(decks))
            if d in self.noisy_decks
        }
        return SimResult(
            cmd=["x"], returncode=0, duration_sec=60.0,
            stdout=_sim_stdout(
                decks, games=num_games,
                unsupported=unsupported, confirm_by=confirm_by,
            ),
            stderr="", timed_out=False, error=None,
        )


def _curate(candidates, runner, tmp_path, monkeypatch, **kwargs):
    """curate_bracket with the offline seams pinned: no Scryfall, no on-disk
    deck files for the archetype classifier."""
    import commander_builder.pool_curator as pc
    monkeypatch.setattr(pc, "_read_color_identity", lambda p: "")
    archetypes = ["aggro", "midrange", "control", "combo", "stax"]
    return curate_bracket(
        bracket=3,
        candidate_filenames=candidates,
        # Deterministic per-name archetype spread (hash() is salted per
        # process, which would make diversity-swap behavior flaky).
        classifier=lambda p: archetypes[sum(map(ord, p.name)) % len(archetypes)],
        runner=runner,
        pool_dir=tmp_path / "_pools",
        **kwargs,
    )


def _twelve(bad_index=None):
    names = [f"c{i:02d} [B3].dck" for i in range(12)]
    bad = names[bad_index] if bad_index is not None else None
    return names, bad


def test_preflight_bad_filler_triggers_retry_not_rejection(tmp_path, monkeypatch, capsys):
    """A candidate whose FIRST smoke pod contained a bad filler must not be
    rejected outright: it gets one retry with different fillers and survives.
    With rotated fillers, c05 fills the pods of c02/c03/c04 only — pre-fix
    (alphabetical fillers + no retry) a bad deck poisoned every pod."""
    candidates, bad = _twelve(bad_index=5)  # bad = "c05 [B3].dck"
    runner = _FakeRunner(bad_decks={bad})
    pool = _curate(candidates, runner, tmp_path, monkeypatch)

    scores = {s.filename: s for s in pool.scores + pool.rejected}
    # c02's first pod was [c03, c04, c05] — poisoned. It must have been
    # retried (two preflight pods recorded) and NOT rejected.
    c02 = scores.get("c02 [B3].dck")
    # c02 may have missed top-6 by win rate; find its record via the runner's
    # preflight calls instead if absent from pool.scores.
    preflight_calls = [c for c in runner.calls if c[1] == 1]
    c02_calls = [c for c in preflight_calls if c[0][0] == "c02 [B3].dck"]
    assert len(c02_calls) == 2, "poisoned candidate should get exactly one retry"
    first_fillers, retry_fillers = c02_calls[0][0][1:], c02_calls[1][0][1:]
    assert bad in first_fillers
    assert bad not in retry_fillers, "retry must use different, clean fillers"
    assert set(retry_fillers).isdisjoint(set(first_fillers))
    if c02 is not None:
        assert c02.rejected_reason is None

    # The genuinely bad deck fails twice and its rejection record carries the
    # pod context of BOTH attempts.
    bad_score = next(s for s in pool.rejected if s.filename == bad)
    assert "unsupported_cards=1" in bad_score.rejected_reason
    assert "pod=" in bad_score.rejected_reason
    assert len(bad_score.preflight_pods) == 2

    # Bounded, printed smoke cost: 12 first-pass + 4 retries (c02/c03/c04/c05).
    out = capsys.readouterr().out
    assert "Preflight cost: 16 smoke game(s)" in out


def test_one_bad_deck_cannot_zero_the_pool(tmp_path, monkeypatch):
    """THE invariant: a 12-candidate pool with one unsupported-card deck must
    survive without it — 11 survivors, exactly 1 rejection, non-empty pool.
    Pre-fix this scenario rejected all 12 candidates and then crashed."""
    candidates, bad = _twelve(bad_index=5)
    runner = _FakeRunner(bad_decks={bad})
    pool = _curate(candidates, runner, tmp_path, monkeypatch)

    assert [s.filename for s in pool.rejected] == [bad]
    assert len(pool.pool_a) == 3 and len(pool.pool_b) == 3
    assert bad not in pool.pool_a + pool.pool_b
    # All six pool decks played qualifier games.
    for s in pool.scores:
        assert s.games_played > 0


def test_clean_deck_not_charged_for_noisy_podmates(tmp_path, monkeypatch):
    """A deck with ZERO confirmAction events, podded with a deck that emits
    300 attributed events per pod, must be charged 0 and pass the
    AI-pilotability gate. The noisy deck itself (100 events/game > cap of 50)
    is rejected. Pre-fix the clean decks were charged 300 // 4 = 75 per pod
    (25/game) of pure opponent noise."""
    candidates = [f"c{i:02d} [B3].dck" for i in range(8)]
    noisy = "c03 [B3].dck"
    runner = _FakeRunner(noisy_decks={noisy})
    pool = _curate(candidates, runner, tmp_path, monkeypatch)

    all_scores = {s.filename: s for s in pool.scores + pool.rejected}
    noisy_score = all_scores[noisy]
    assert noisy_score.rejected_reason is not None
    assert "ai_pilotability" in noisy_score.rejected_reason

    # Every clean deck: zero charged events, not rejected.
    for f, s in all_scores.items():
        if f == noisy:
            continue
        assert s.confirm_action_total == 0, (
            f"{f} charged {s.confirm_action_total} events of podmate noise"
        )
        assert s.rejected_reason is None


def test_under_four_survivors_raises_clean_error_and_persists(tmp_path, monkeypatch):
    """When preflight leaves <4 survivors, curate_bracket must raise the typed
    error (not schedule_pods' bare ValueError), list every rejection with its
    reason, and persist the preflight records so the smoke spend isn't lost."""
    import pytest
    candidates = [f"c{i:02d} [B3].dck" for i in range(6)]
    runner = _FakeRunner(crash_all=True)

    with pytest.raises(InsufficientSurvivorsError) as exc_info:
        _curate(candidates, runner, tmp_path, monkeypatch)

    err = exc_info.value
    msg = str(err)
    # Actionable message: every candidate listed with its reason.
    for f in candidates:
        assert f in msg
    assert "preflight_crash:timeout" in msg
    assert "need >=4" in msg
    # Preflight results persisted.
    assert err.preflight_path is not None and err.preflight_path.exists()
    import json
    data = json.loads(err.preflight_path.read_text(encoding="utf-8"))
    assert data["survivors"] == []
    assert len(data["rejected"]) == 6
    assert all(r["rejected_reason"] for r in data["rejected"])
    # The structured records ride on the exception too.
    assert len(err.rejected) == 6


def test_main_returns_distinct_exit_code_on_insufficient_survivors(monkeypatch):
    """CLI convention: 0 = success, 2 = not enough decks on disk,
    3 = preflight rejected the pool. No traceback."""
    import commander_builder.pool_curator as pc

    monkeypatch.setattr(
        pc, "_list_bracket_candidates",
        lambda bracket: [f"c{i:02d} [B3].dck" for i in range(6)],
    )

    def _raise(*args, **kwargs):
        raise InsufficientSurvivorsError("preflight wiped the pool", rejected=[])

    monkeypatch.setattr(pc, "curate_bracket", _raise)
    assert pc.main(["--bracket", "3"]) == 3
