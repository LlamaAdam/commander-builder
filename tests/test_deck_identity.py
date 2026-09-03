"""R3 C-08 (2026-09-03): one stable ``deck_id`` per deck across versions.

``resolve_deck_id`` used to read only ``Moxfield=`` and fall back to the
RAW filename stem, and the unattended writers passed the NEW deck's
stem — which carries `` v2``, `` v3`` … after every accepted round — so
every hand-built and every Archidekt-lane deck fragmented into one-row
"decks" in every per-deck knowledge_log surface, and the auto-curate
writer (which looks up prior rows by the just-bumped stem) wrote
``parent_id = None`` every round. Pure filesystem + SQLite in tmp_path.
"""
from __future__ import annotations

import pytest

from commander_builder.deck_identity import (
    ARCHIDEKT_ID_PREFIX,
    deck_id_from_text,
    is_filename_shaped_deck_id,
    resolve_deck_id,
    stable_deck_id_for_row,
    stable_deck_stem,
)


@pytest.mark.parametrize("name, expected", [
    ("[USER] Foo v3 [B3].dck", "[USER] Foo [B3]"),
    ("[USER] Foo v3 [B3]", "[USER] Foo [B3]"),
    ("[USER] Foo v2 [B3].dck", "[USER] Foo [B3]"),
    ("[USER] Foo [B3].dck", "[USER] Foo [B3]"),
    ("[USER] Foo [B3]", "[USER] Foo [B3]"),
    ("Foo v2.dck", "Foo"),
    ("Foo v2", "Foo"),
    ("MyDeck.dck", "MyDeck"),
    ("[USER] LegacyDeck [B3]", "[USER] LegacyDeck [B3]"),
])
def test_stable_stem_strips_only_the_version_suffix(name, expected):
    """Every version of a local deck shares one stem; a deck with no
    version passes through unchanged (existing stem-keyed rows stay
    valid)."""
    assert stable_deck_stem(name) == expected


def test_stable_stem_agrees_with_the_proposer_bump():
    """The suffix stripped here is exactly the one the proposer appends,
    through the same regexes — the two cannot drift."""
    from commander_builder.proposer import _bump_version_filename
    name = "[USER] Chain [B3].dck"
    bumped = _bump_version_filename(name)
    assert bumped == "[USER] Chain v2 [B3].dck"
    assert stable_deck_stem(_bump_version_filename(bumped)) == stable_deck_stem(name)


def test_archidekt_provenance_is_read_and_namespaced():
    text = "[metadata]\nName=X\nArchidekt=12345\nSource=archidekt\n[Main]\n1 Forest\n"
    assert deck_id_from_text(text) == f"{ARCHIDEKT_ID_PREFIX}12345"


def test_moxfield_wins_over_archidekt_and_stays_bare():
    """Existing rows are keyed by the bare publicId; that lane must not
    change shape."""
    text = "[metadata]\nMoxfield=abc-XYZ\nArchidekt=12345\n"
    assert deck_id_from_text(text) == "abc-XYZ"


def test_resolve_deck_id_stem_fallback_is_version_stripped(tmp_path):
    """The critic's E13: hand-built v1/v2 used to resolve to two ids."""
    v1 = tmp_path / "[USER] Hand Deck [B3].dck"
    v2 = tmp_path / "[USER] Hand Deck v2 [B3].dck"
    for p in (v1, v2):
        p.write_text("[Commander]\n1 Test\n", encoding="utf-8")
    assert resolve_deck_id(v1) == resolve_deck_id(v2) == "[USER] Hand Deck [B3]"


def test_resolve_deck_id_archidekt_pair_shares_one_id(tmp_path):
    v1 = tmp_path / "[USER] Ark Deck [B3].dck"
    v2 = tmp_path / "[USER] Ark Deck v2 [B3].dck"
    for p in (v1, v2):
        p.write_text("[metadata]\nArchidekt=777\nSource=archidekt\n[Main]\n1 Forest\n",
                     encoding="utf-8")
    assert resolve_deck_id(v1) == resolve_deck_id(v2) == "archidekt:777"


def test_explicit_fallback_still_passes_through(tmp_path):
    """An explicit id is the caller's business; callers that want a
    filename-derived fallback pass ``stable_deck_stem`` themselves."""
    p = tmp_path / "[USER] Foo v2 [B3].dck"
    p.write_text("[Commander]\n1 Test\n", encoding="utf-8")
    assert resolve_deck_id(p, fallback="my-explicit-id") == "my-explicit-id"


@pytest.mark.parametrize("deck_id, shaped", [
    ("[USER] Foo v2 [B3]", True),
    ("[USER] Foo [B3].dck", True),
    ("[USER] Foo [B3]", True),
    ("Foo v3", True),
    ("abc-XYZ_123", False),
    ("test-public-id", False),
    ("archidekt:12345", False),
])
def test_only_filename_shaped_ids_are_backfill_candidates(deck_id, shaped):
    assert is_filename_shaped_deck_id(deck_id) is shaped


def test_stable_id_for_row_prefers_the_snapshot_provenance():
    assert stable_deck_id_for_row(
        "[USER] X v2 [B3]", "[metadata]\nMoxfield=pid\n") == "pid"
    assert stable_deck_id_for_row(
        "[USER] X v2 [B3]", "[metadata]\nArchidekt=55\n") == "archidekt:55"
    assert stable_deck_id_for_row("[USER] X v2 [B3]", None) == "[USER] X [B3]"


def test_stable_id_for_row_is_none_when_nothing_changes():
    assert stable_deck_id_for_row("[USER] X [B3]", None) is None
    assert stable_deck_id_for_row("explicit-id", "[metadata]\nMoxfield=pid\n") is None


# --- the two unattended writers -------------------------------------------

def test_auto_curate_writer_keeps_one_id_and_threads_parent(tmp_path):
    """The critic's E13 auto-curate rows: v2 then v3 of a hand-built deck
    landed as two deck_ids with parent=None each. One id, chained."""
    from commander_builder._proposer_sim import _log_auto_curate_iteration
    from commander_builder.knowledge_log import get_iteration, iterations_for_deck
    from commander_builder.proposer import Proposal

    db = tmp_path / "kl.sqlite"
    src = tmp_path / "[USER] Hand Deck [B3].dck"
    v2 = tmp_path / "[USER] Hand Deck v2 [B3].dck"
    v3 = tmp_path / "[USER] Hand Deck v3 [B3].dck"
    for p in (src, v2, v3):
        p.write_text(f"[metadata]\nName={p.stem}\n[Commander]\n1 Test\n[Main]\n1 Forest\n",
                     encoding="utf-8")
    proposal = Proposal(adds=["A"], cuts=["B"], rationale="x")
    proposal.applied_adds, proposal.applied_cuts = ["A"], ["B"]

    first = _log_auto_curate_iteration(src, v2, 3, proposal, db_path=db)
    second = _log_auto_curate_iteration(v2, v3, 3, proposal, db_path=db)

    rows = iterations_for_deck("[USER] Hand Deck [B3]", db_path=db)
    assert [r.id for r in rows] == [first, second]
    assert get_iteration(second, db_path=db).parent_id == first
