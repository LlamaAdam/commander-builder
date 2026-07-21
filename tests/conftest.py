"""Pytest config: ensure `src/` is on sys.path so `commander_builder.*` imports
without an editable install.

Also defends against the kind of bug we hit on 2026-05-15: a test that
exercises a CLI path producing knowledge_log side effects without
passing ``--db-path`` ends up writing rows into the production
repo-root ``knowledge_log.sqlite``. The autouse fixture below redirects
``knowledge_log.DEFAULT_DB_PATH`` to a per-test temp file so a
careless test can't leak into production state again. The patch is
effective because ``knowledge_log`` resolves ``db_path=None`` against
the module attribute at CALL time (``_resolve_db_path``) — consumers
must never freeze the constant via ``from ... import DEFAULT_DB_PATH``
at module level or use it as a def-time parameter default.

## Fast/slow lane split (Tier-3, 2026-05-19)

Tests tagged ``@pytest.mark.slow`` are skipped by default so the
inner-loop ``pytest`` run takes ~30s instead of ~3min. Run the full
suite via ``pytest --run-slow`` (or ``pytest -m "slow or not slow"``
if you prefer pure marker syntax). CI runs everything implicitly via
``--run-slow``.

Tag a test ``slow`` when it:
- exercises the full ``advise()`` pipeline (EDHREC fixtures, multi-
  source dispatch, role classification) — each costs ~3-15s.
- shells out to the auto-curate CLI through argparse + Anthropic
  stubs — each costs ~2-4s.
- otherwise dominates the ``--durations=20`` list with >1s runtime.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_addoption(parser):
    """Add ``--run-slow`` so devs can opt into the long-running
    integration tests without having to remember the marker syntax."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help=(
            "Run tests marked @pytest.mark.slow (advisor + auto-curate "
            "integration). Off by default; the fast lane keeps inner-"
            "loop iteration under ~30s. CI runs with this flag set."
        ),
    )


_AUTO_SLOW_NAME_PREFIXES = (
    # Every test_auto_curate_main_* test exercises argparse + the full
    # curator pipeline (~1-4s each); ~27 tests collectively cost
    # 30-60s of the ~3min suite. Auto-mark them rather than decorating
    # individually so adding a new CLI test inherits the slow tag.
    "test_auto_curate_main_",
)


def pytest_collection_modifyitems(config, items):
    """Auto-tag known-slow test families, then skip ``slow`` tests
    unless ``--run-slow`` was passed.

    Auto-tagging runs before the skip pass so a name-prefixed test
    picks up the marker regardless of whether the author remembered
    to add ``@pytest.mark.slow``.

    Skipping is implemented as a collection modifier (not a ``-m``
    default) so the skip reason is visible in the report and so
    users can still override with their own ``-m`` expression when
    debugging a specific slow test (e.g.
    ``pytest -m slow tests/test_proposer.py``).
    """
    # Pass 1: auto-mark by name prefix.
    slow_marker = pytest.mark.slow
    for item in items:
        for prefix in _AUTO_SLOW_NAME_PREFIXES:
            if item.name.startswith(prefix):
                item.add_marker(slow_marker)
                break

    # Pass 2: skip slow unless opted in.
    if config.getoption("--run-slow"):
        return
    marker_expr = config.getoption("-m") or ""
    if "slow" in marker_expr:
        return
    skip_slow = pytest.mark.skip(
        reason="slow test skipped by default; pass --run-slow to include",
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(autouse=True)
def _isolate_knowledge_log_default_path(tmp_path, monkeypatch):
    """Point ``knowledge_log.DEFAULT_DB_PATH`` at a per-test temp file.

    Tests that explicitly pass ``--db-path <somewhere>`` are unaffected
    — the override is only consulted when a caller doesn't supply one.
    Belt-and-suspenders against tests leaking iteration rows into the
    production repo-root ``knowledge_log.sqlite``.

    This only works because ``knowledge_log`` functions default to
    ``db_path=None`` and resolve it against the module attribute at
    call time (``_resolve_db_path``). Before 2026-07-19 the constant
    was baked into def-time defaults (``db_path: Path =
    DEFAULT_DB_PATH``) and import-time copies (``from .knowledge_log
    import DEFAULT_DB_PATH`` in doctor/status/export/report/revert_to),
    so this patch was silently a no-op and e.g. ``run_doctor()`` still
    init_db'd the production file. ``test_doctor.py::
    test_run_doctor_does_not_touch_production_knowledge_log`` guards
    against regressing that.

    A targeted leak was caught on 2026-05-15 in
    ``test_auto_curate_main_writes_versioned_file_without_dry_run``,
    which ran the full auto-curate pipeline without passing
    ``--db-path``. Four rows landed in production state and surfaced
    during a live-server probe of the iteration-graph endpoint. The
    test now passes ``--no-log`` explicitly, but this autouse fixture
    prevents the next slip-up from polluting state again.
    """
    from commander_builder import knowledge_log as _kl
    monkeypatch.setattr(
        _kl, "DEFAULT_DB_PATH", tmp_path / "_isolated_knowledge_log.sqlite",
    )


@pytest.fixture(autouse=True)
def _isolate_collection_path(tmp_path, monkeypatch):
    """Point the card-collection file at a per-test (nonexistent) temp
    path so tests never read the developer's real
    ``~/.commander-builder/collection.txt``.

    Same hazard class as ``_isolate_knowledge_log_default_path`` above:
    the ownership filters are contractually INERT when no collection
    file exists, and most tests assert on that inert baseline. A
    developer who has registered a real collection would otherwise see
    advisor/proposer tests fail (or worse, pass for the wrong reason)
    because their personal card list leaked into the pipeline under
    test.

    Works because ``collection.collection_path()`` consults the
    ``COMMANDER_BUILDER_COLLECTION`` env var AT CALL TIME (the
    DEFAULT_DB_PATH lesson — no import-time path constants). Tests
    that want a real collection write to this tmp path (or set the
    env var themselves / pass an explicit ``path=``).
    """
    monkeypatch.setenv(
        "COMMANDER_BUILDER_COLLECTION",
        str(tmp_path / "_isolated_collection.txt"),
    )
