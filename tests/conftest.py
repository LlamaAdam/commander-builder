"""Pytest config: ensure `src/` is on sys.path so `commander_builder.*` imports
without an editable install.

Also defends against the kind of bug we hit on 2026-05-15: a test that
exercises a CLI path producing knowledge_log side effects without
passing ``--db-path`` ends up writing rows into the production
``vendor/knowledge_log.sqlite``. The autouse fixture below redirects
``knowledge_log.DEFAULT_DB_PATH`` to a per-test temp file so a
careless test can't leak into production state again.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolate_knowledge_log_default_path(tmp_path, monkeypatch):
    """Point ``knowledge_log.DEFAULT_DB_PATH`` at a per-test temp file.

    Tests that explicitly pass ``--db-path <somewhere>`` are unaffected
    — the override is only consulted when a caller doesn't supply one.
    Belt-and-suspenders against tests leaking iteration rows into the
    production ``vendor/knowledge_log.sqlite``.

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
