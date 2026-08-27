"""/api/save_iteration margin column — NULL-on-no-decisive semantics.

Companion to test_web_app.py's save_iteration suite (owned separately);
this file covers the 2026-08-16 fix: when a sim_report carries win counts
but ZERO head-to-head decisive games (all draws, or fillers swept the
pod), the ``margin`` column must land NULL — matching the win-rate
columns' own no-fabricated-zero rule — instead of a fabricated 0 that
reads as an observed dead-even split in cross-run margin analyses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

flask = pytest.importorskip("flask")  # skip if [web] extra not installed

from commander_builder.web.app import create_app


def _write_deck(deck_dir: Path, name: str) -> Path:
    p = deck_dir / f"{name}.dck"
    p.write_text(
        "[metadata]\n"
        f"Name={name}\n\n"
        "[Commander]\n"
        "1 Test Cmdr\n\n"
        "[Main]\n" + "1 Forest\n" * 40,
        encoding="utf-8",
    )
    return p


@pytest.fixture
def margin_client(tmp_path):
    """Flask test client over a fresh deck dir + empty knowledge_log."""
    deck_dir = tmp_path / "decks"
    deck_dir.mkdir()
    _write_deck(deck_dir, "Alpha")
    db = tmp_path / "margin_klog.sqlite"
    app = create_app(deck_dir=deck_dir, knowledge_db=db)
    app.config["TESTING"] = True
    return app.test_client()


def _save(client, sim_report, verdict="inconclusive"):
    resp = client.post("/api/save_iteration", json={
        "deck_id": "Alpha", "deck_name": "Alpha", "bracket": 3,
        "verdict": verdict, "sim_report": sim_report,
    })
    assert resp.status_code == 200, resp.get_json()
    return client.get(f"/api/iteration/{resp.get_json()['id']}").get_json()


def test_margin_null_when_all_attributed_games_drew(margin_client):
    """old_wins=0, new_wins=0, everything drew: decisive == 0 → the win
    rates are already NULL and margin must match, not record a fake 0."""
    detail = _save(margin_client, {
        "old_wins": 0, "new_wins": 0, "draws": 4, "total_games": 4,
    })
    assert detail["win_rate_old"] is None
    assert detail["win_rate_new"] is None
    assert detail["margin"] is None


def test_margin_null_when_fillers_swept_the_pod(margin_client):
    """No draws at all — the two filler seats took every attributed game.
    Head-to-head decisive is still 0, so margin stays NULL."""
    detail = _save(margin_client, {
        "old_wins": 0, "new_wins": 0, "draws": 0, "total_games": 6,
    })
    assert detail["margin"] is None


def test_margin_ignores_fabricated_payload_margin_at_zero_decisive(margin_client):
    """Even when the payload carries a margin key (the propose_swap
    response always does), zero decisive games must land NULL — the
    payload margin is never trusted."""
    detail = _save(margin_client, {
        "old_wins": 0, "new_wins": 0, "draws": 2, "total_games": 8,
        "margin": 0, "winner": "tie",
    })
    assert detail["margin"] is None


def test_margin_still_signed_when_games_were_decisive(margin_client):
    """Regression guard for the NULL fix: a real decisive split keeps the
    signed new_wins - old_wins convention (payload's absolute margin
    ignored)."""
    detail = _save(margin_client, {
        "old_wins": 12, "new_wins": 4, "draws": 0, "total_games": 20,
        "margin": 8, "winner": "old",
    }, verdict="reverted")
    assert detail["margin"] == -8
    assert detail["win_rate_old"] == round(12 / 16, 4)
    assert detail["win_rate_new"] == round(4 / 16, 4)
