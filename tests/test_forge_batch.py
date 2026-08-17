"""Cross-invocation Forge-profile locking (forge_batch, 2026-08-16).

The pool orchestrators always refused to hand one cwd-isolated Forge profile
to two chunks — they'd collide on the deck dir, cache and forge.log — but that
promise was only ever kept WITHIN one invocation: the free-queue is a per-call
local and ``_discover_profiles`` re-enumerates ``vendor/forge*`` every call. The
web UI runs sims on background threads (``/api/propose_swap_async``), so a
second job (or a CLI run started alongside one) could double-book a profile a
live JVM was writing.

These tests pin the advisory-lockfile fence that closes that hole. Everything
here mocks the sim through the ``_sim_fn`` seam — no JVM, no network — and uses
REAL temp dirs as profiles, because the lock is a real file on disk.
"""
import os
import time
from pathlib import Path

import pytest

from commander_builder.forge_batch import (
    ABJob,
    ABResult,
    ProfileLock,
    ProfileLockError,
    _PROFILE_LOCK_NAME,
    _PROFILE_LOCK_STALE_SEC,
    _try_acquire_profile,
    acquire_profile_pool,
    is_profile_locked,
    release_profile_pool,
    run_ab_batch,
    run_ab_parallel,
)


# --- helpers ---------------------------------------------------------------


def _profiles(tmp_path, n):
    """n REAL profile dirs (the lock is a real file, so these must exist)."""
    out = []
    for i in range(n):
        p = tmp_path / ("forge" if i == 0 else f"forge{i + 1}")
        p.mkdir()
        out.append(p)
    return out


def _lock_file(profile: Path) -> Path:
    return profile / _PROFILE_LOCK_NAME


def _foreign_lock(profile: Path, *, age_sec: float = 0.0) -> Path:
    """Simulate ANOTHER process holding ``profile``, optionally backdated."""
    lp = _lock_file(profile)
    lp.write_text("pid=999999\nhost=other-box\n", encoding="utf-8")
    if age_sec:
        when = time.time() - age_sec
        os.utime(lp, (when, when))
    return lp


def _decks(tmp_path):
    a = tmp_path / "[USER] DeckA [B3].dck"
    b = tmp_path / "[USER] DeckB [B3].dck"
    a.write_text("[Main]\n", encoding="utf-8")
    b.write_text("[Main]\n", encoding="utf-8")
    return a, b


def _stub_runner_for(monkeypatch, seen=None):
    """``run_ab_parallel`` late-binds ``_runner_for`` through forge_runner, so
    that's the name to patch. Records which profile each runner was built for."""
    def _fake(profile):
        if seen is not None:
            seen.append(Path(profile))
        return f"runner:{profile}"

    monkeypatch.setattr("commander_builder.forge_runner._runner_for", _fake)


def _ok_chunk(games):
    return ABResult(wins_a=games, games=games, status="done")


class _ProfileRunner:
    """Minimal ForgeRunner stand-in — only ``forge_dir`` matters to the lock."""

    def __init__(self, forge_dir):
        self.forge_dir = forge_dir


# --- primitive: acquire / release ------------------------------------------


def test_acquire_creates_lockfile_with_pid_and_release_removes_it(tmp_path):
    (profile,) = _profiles(tmp_path, 1)

    lock = _try_acquire_profile(profile)
    assert lock is not None
    assert lock.path == _lock_file(profile)
    assert lock.path.exists()
    body = lock.path.read_text(encoding="utf-8")
    assert f"pid={os.getpid()}" in body          # diagnostics for a human
    assert "acquired_at=" in body
    assert is_profile_locked(profile)

    lock.release()
    assert not lock.path.exists()
    assert not is_profile_locked(profile)
    lock.release()  # idempotent — a double release must not raise


def test_acquire_is_exclusive_across_invocations(tmp_path):
    (profile,) = _profiles(tmp_path, 1)
    first = _try_acquire_profile(profile)
    assert first is not None
    # A "second process" (this call is the moral equivalent) must be refused.
    assert _try_acquire_profile(profile) is None
    first.release()
    second = _try_acquire_profile(profile)
    assert second is not None
    second.release()


def test_missing_profile_dir_gets_a_noop_lock(tmp_path):
    """No dir -> no Forge instance to collide with. Usable, just unfenced —
    this is what keeps every offline fake-pool test working."""
    lock = _try_acquire_profile(tmp_path / "nope")
    assert lock is not None
    assert lock.path is None
    lock.release()


def test_profile_lock_is_a_context_manager(tmp_path):
    (profile,) = _profiles(tmp_path, 1)
    with _try_acquire_profile(profile) as lock:
        assert isinstance(lock, ProfileLock)
        assert _lock_file(profile).exists()
    assert not _lock_file(profile).exists()


# --- stale reclaim ---------------------------------------------------------


def test_backdated_lock_is_reclaimed(tmp_path):
    """A crashed JVM must not brick a profile forever: a lock older than the
    max-sim window is deleted and re-acquired (mtime policy, not pid liveness)."""
    (profile,) = _profiles(tmp_path, 1)
    stale = _foreign_lock(profile, age_sec=_PROFILE_LOCK_STALE_SEC + 60)

    lock = _try_acquire_profile(profile)
    assert lock is not None
    assert lock.reclaimed_stale is True
    # The abandoned payload is gone — this is OUR lock now.
    assert f"pid={os.getpid()}" in stale.read_text(encoding="utf-8")
    lock.release()


def test_lock_just_under_the_stale_window_is_respected(tmp_path):
    """Boundary: a long-but-plausible sim (5h) still owns its profile."""
    (profile,) = _profiles(tmp_path, 1)
    _foreign_lock(profile, age_sec=_PROFILE_LOCK_STALE_SEC - 600)
    assert _try_acquire_profile(profile) is None
    assert is_profile_locked(profile)


def test_stale_lock_is_not_reported_as_locked_by_discovery_peek(tmp_path):
    (profile,) = _profiles(tmp_path, 1)
    _foreign_lock(profile, age_sec=_PROFILE_LOCK_STALE_SEC + 1)
    assert is_profile_locked(profile) is False


# --- pool acquisition ------------------------------------------------------


def test_pool_skips_a_locked_profile_and_takes_the_next_free_one(tmp_path):
    p1, p2, p3 = _profiles(tmp_path, 3)
    _foreign_lock(p1)

    locks = acquire_profile_pool([p1, p2, p3], 2)
    try:
        assert [lk.profile for lk in locks] == [p2, p3]
    finally:
        release_profile_pool(locks)
    assert not _lock_file(p2).exists()
    assert not _lock_file(p3).exists()
    assert _lock_file(p1).exists()  # somebody else's lock, left alone


def test_pool_all_locked_raises_actionable_error(tmp_path):
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    older = _foreign_lock(p2, age_sec=1800)

    with pytest.raises(ProfileLockError) as exc:
        acquire_profile_pool([p1, p2])
    msg = str(exc.value)
    assert "2 Forge profile(s), all locked" in msg
    assert "another sim running?" in msg
    assert str(older) in msg                 # names the file to inspect
    assert "delete if no sim is active" in msg
    assert "6h" in msg                       # the reclaim window, stated


def test_pool_count_caps_how_many_locks_are_taken(tmp_path):
    p1, p2, p3 = _profiles(tmp_path, 3)
    locks = acquire_profile_pool([p1, p2, p3], 2)
    try:
        assert len(locks) == 2
        assert not _lock_file(p3).exists()   # untouched, still free
    finally:
        release_profile_pool(locks)


# --- run_ab_parallel -------------------------------------------------------


def test_run_ab_parallel_holds_the_lock_during_the_run_and_releases_after(
    tmp_path, monkeypatch,
):
    deck_a, deck_b = _decks(tmp_path)
    (profile,) = _profiles(tmp_path, 1)
    _stub_runner_for(monkeypatch)

    held = []

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        held.append(_lock_file(profile).exists())
        return _ok_chunk(games)

    result = run_ab_parallel(
        deck_a, deck_b, games=4, fillers=["f1.dck", "f2.dck"],
        profiles=[profile], max_workers=1, _sim_fn=fake_sim,
    )

    assert result.status == "done"
    assert result.games == 4
    assert held == [True]                      # locked for the whole sim
    assert not _lock_file(profile).exists()    # released on the way out


def test_run_ab_parallel_skips_a_profile_locked_by_another_run(
    tmp_path, monkeypatch,
):
    """The busy profile is passed over; the chunk lands on the next free one."""
    deck_a, deck_b = _decks(tmp_path)
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    seen = []
    _stub_runner_for(monkeypatch, seen)

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        return _ok_chunk(games)

    result = run_ab_parallel(
        deck_a, deck_b, games=4, fillers=["f1.dck", "f2.dck"],
        profiles=[p1, p2], max_workers=2, _sim_fn=fake_sim,
    )

    assert result.status == "done"
    assert seen == [p2]                        # never built a runner for p1
    assert _lock_file(p1).exists()             # the other run still owns it


def test_run_ab_parallel_all_locked_fails_fast_with_actionable_error(
    tmp_path, monkeypatch,
):
    """Fail fast rather than queue forever behind a multi-hour soak."""
    deck_a, deck_b = _decks(tmp_path)
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    _foreign_lock(p2)
    _stub_runner_for(monkeypatch)

    def boom_sim(*a, **k):
        raise AssertionError("no sim may run while every profile is locked")

    result = run_ab_parallel(
        deck_a, deck_b, games=4, fillers=["f1.dck", "f2.dck"],
        profiles=[p1, p2], max_workers=2, _sim_fn=boom_sim,
    )

    assert result.status == "failed"
    assert "all locked" in (result.error or "")
    assert "delete if no sim is active" in (result.error or "")


def test_run_ab_parallel_reclaims_a_stale_lock(tmp_path, monkeypatch):
    """A profile left locked by a crashed JVM is reclaimed, not abandoned."""
    deck_a, deck_b = _decks(tmp_path)
    (profile,) = _profiles(tmp_path, 1)
    _foreign_lock(profile, age_sec=_PROFILE_LOCK_STALE_SEC + 60)
    seen = []
    _stub_runner_for(monkeypatch, seen)

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        return _ok_chunk(games)

    result = run_ab_parallel(
        deck_a, deck_b, games=2, fillers=["f1.dck", "f2.dck"],
        profiles=[profile], max_workers=1, _sim_fn=fake_sim,
    )

    assert result.status == "done"
    assert seen == [profile]
    assert not _lock_file(profile).exists()


def test_run_ab_parallel_releases_the_lock_when_a_chunk_explodes(
    tmp_path, monkeypatch,
):
    """An unexpected (non-ABResult) exception still propagates — but it must
    never leave the profile fenced for the next six hours."""
    deck_a, deck_b = _decks(tmp_path)
    (profile,) = _profiles(tmp_path, 1)
    _stub_runner_for(monkeypatch)

    def blowing_sim(*a, **k):
        raise RuntimeError("JVM went sideways")

    with pytest.raises(RuntimeError):
        run_ab_parallel(
            deck_a, deck_b, games=2, fillers=["f1.dck", "f2.dck"],
            profiles=[profile], max_workers=1, _sim_fn=blowing_sim,
        )

    assert not _lock_file(profile).exists()


def test_run_ab_parallel_releases_the_lock_when_runner_construction_fails(
    tmp_path, monkeypatch,
):
    """Same guarantee one step earlier: _runner_for raising (no Forge jar)
    happens AFTER checkout, so the finally still has to fire."""
    deck_a, deck_b = _decks(tmp_path)
    (profile,) = _profiles(tmp_path, 1)

    def _boom(profile_dir):
        raise FileNotFoundError("forge jar missing")

    monkeypatch.setattr("commander_builder.forge_runner._runner_for", _boom)

    with pytest.raises(FileNotFoundError):
        run_ab_parallel(
            deck_a, deck_b, games=2, fillers=["f1.dck", "f2.dck"],
            profiles=[profile], max_workers=1, _sim_fn=_ok_chunk,
        )

    assert not _lock_file(profile).exists()


def test_run_ab_parallel_fake_profiles_stay_unfenced(tmp_path, monkeypatch):
    """Regression guard for every existing offline test: profile dirs that
    don't exist can't host a JVM, so they're usable without a lockfile."""
    deck_a, deck_b = _decks(tmp_path)
    profiles = [tmp_path / "ghost1", tmp_path / "ghost2"]
    seen = []
    _stub_runner_for(monkeypatch, seen)

    def fake_sim(da, db, *, games, runner, fillers, game_format, timeout_per_game):
        return _ok_chunk(games)

    result = run_ab_parallel(
        deck_a, deck_b, games=4, fillers=["f1.dck", "f2.dck"],
        profiles=profiles, max_workers=2, _sim_fn=fake_sim,
    )

    assert result.status == "done"
    assert seen == profiles
    assert not any(_lock_file(p).exists() for p in profiles)


# --- discovery -------------------------------------------------------------


def test_discover_profiles_hides_locked_ones(tmp_path, monkeypatch):
    p1, p2 = _profiles(tmp_path, 2)
    monkeypatch.setattr("commander_builder.forge_runner.VENDOR_FORGE", p1)
    from commander_builder import forge_batch as fb

    assert fb._discover_profiles() == [p1, p2]
    _foreign_lock(p1)
    assert fb._discover_profiles() == [p2]
    # …but the raw layout is still inspectable, which is how run_ab_parallel
    # tells "no Forge installed" apart from "everything is busy".
    assert fb._discover_profiles(skip_locked=False) == [p1, p2]


def test_run_ab_parallel_discovery_all_locked_reports_busy_not_missing(
    tmp_path, monkeypatch,
):
    deck_a, deck_b = _decks(tmp_path)
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    _foreign_lock(p2)
    monkeypatch.setattr("commander_builder.forge_runner.VENDOR_FORGE", p1)
    _stub_runner_for(monkeypatch)

    result = run_ab_parallel(
        deck_a, deck_b, games=4, fillers=["f1.dck", "f2.dck"],
        _sim_fn=_ok_chunk,
    )

    assert result.status == "failed"
    assert "all locked" in (result.error or "")
    assert "no Forge profiles found" not in (result.error or "")


# --- run_ab_batch ----------------------------------------------------------


def _batch_sim(da, db, *, games, runner, fillers, game_format):
    return ABResult(deck_a=da.name, deck_b=db.name, games=games, status="done")


def test_run_ab_batch_locks_each_runner_profile_and_releases(tmp_path):
    p1, p2 = _profiles(tmp_path, 2)
    deck_a, deck_b = _decks(tmp_path)
    jobs = [ABJob(deck_a=deck_a, deck_b=deck_b)] * 2

    locked_during = []

    def sim(da, db, *, games, runner, fillers, game_format):
        locked_during.append(_lock_file(Path(runner.forge_dir)).exists())
        return ABResult(games=games, status="done")

    results = run_ab_batch(
        jobs, [_ProfileRunner(p1), _ProfileRunner(p2)], _sim_fn=sim,
    )

    assert len(results) == 2
    assert all(locked_during)
    assert not _lock_file(p1).exists()
    assert not _lock_file(p2).exists()


def test_run_ab_batch_drops_a_runner_whose_profile_is_busy(tmp_path):
    """Rather than double-booking, the batch runs narrower — every job still
    completes, just serialized over the profiles that were actually free."""
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    deck_a, deck_b = _decks(tmp_path)
    jobs = [ABJob(deck_a=deck_a, deck_b=deck_b) for _ in range(3)]

    used = []

    def sim(da, db, *, games, runner, fillers, game_format):
        used.append(Path(runner.forge_dir))
        return ABResult(games=games, status="done")

    results = run_ab_batch(
        jobs, [_ProfileRunner(p1), _ProfileRunner(p2)], _sim_fn=sim,
    )

    assert len(results) == 3
    assert set(used) == {p2}
    assert _lock_file(p1).exists()             # the other run keeps its lock


def test_run_ab_batch_all_profiles_locked_raises(tmp_path):
    p1, p2 = _profiles(tmp_path, 2)
    _foreign_lock(p1)
    _foreign_lock(p2)
    deck_a, deck_b = _decks(tmp_path)

    def boom(*a, **k):
        raise AssertionError("no sim may run while every profile is locked")

    with pytest.raises(ProfileLockError) as exc:
        run_ab_batch(
            [ABJob(deck_a=deck_a, deck_b=deck_b)],
            [_ProfileRunner(p1), _ProfileRunner(p2)],
            _sim_fn=boom,
        )
    assert "all locked" in str(exc.value)


def test_run_ab_batch_releases_locks_when_a_job_explodes(tmp_path):
    (p1,) = _profiles(tmp_path, 1)
    deck_a, deck_b = _decks(tmp_path)

    def blowing(*a, **k):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run_ab_batch(
            [ABJob(deck_a=deck_a, deck_b=deck_b)], [_ProfileRunner(p1)],
            _sim_fn=blowing,
        )
    assert not _lock_file(p1).exists()


def test_run_ab_batch_runners_without_forge_dir_are_unfenced(tmp_path):
    """Test doubles (and any runner not bound to a vendor profile) keep
    working exactly as before — nothing to lock, nothing to skip."""
    deck_a, deck_b = _decks(tmp_path)
    jobs = [ABJob(deck_a=deck_a, deck_b=deck_b)]
    results = run_ab_batch(jobs, [object(), object()], _sim_fn=_batch_sim)
    assert len(results) == 1
    assert results[0].status == "done"
