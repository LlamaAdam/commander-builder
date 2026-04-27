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
