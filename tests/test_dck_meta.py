"""Tests for ``dck_meta`` — the two ``[metadata]`` invariants.

``Name=`` (filename↔win-attribution) has been exercised indirectly for a
long time by the deck writers that call it (test_snapshot_deck,
test_moxfield_import, test_web_app). This file is the direct home for the
``BracketUnverified=`` marker added 2026-08-20, plus the ``Name=``
behaviors the marker rides next to and must not disturb.
"""
from __future__ import annotations

from commander_builder import dck_meta, dck_utils


DECK = (
    "[metadata]\n"
    "Name=[USER] Gamma [B3]\n"
    "\n"
    "[Commander]\n"
    "1 Test Cmdr\n"
    "\n"
    "[Main]\n"
    "60 Forest\n"
    "39 Cultivate\n"
)


# ---------------------------------------------------------------------------
# read / set / clear round trip
# ---------------------------------------------------------------------------

def test_absent_marker_reads_none():
    assert dck_meta.read_bracket_unverified(DECK) is None
    assert dck_meta.read_bracket_unverified("") is None
    assert dck_meta.read_bracket_unverified(None) is None


def test_set_then_read_round_trips_the_bracket():
    out = dck_meta.set_bracket_unverified(DECK, 3)
    assert "BracketUnverified=3" in out
    assert dck_meta.read_bracket_unverified(out) == 3


def test_set_is_idempotent():
    """The editor round-trips the file through a textarea, so an already
    marked deck is re-marked on every save. Two markers would make
    "cleared" depend on which line a reader stopped at."""
    once = dck_meta.set_bracket_unverified(DECK, 3)
    twice = dck_meta.set_bracket_unverified(once, 3)
    assert twice.count("BracketUnverified=") == 1
    assert twice == once


def test_set_overwrites_a_different_bracket():
    out = dck_meta.set_bracket_unverified(
        dck_meta.set_bracket_unverified(DECK, 3), 4,
    )
    assert out.count("BracketUnverified=") == 1
    assert dck_meta.read_bracket_unverified(out) == 4


def test_clear_restores_the_original_text():
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    assert dck_meta.clear_bracket_unverified(marked) == DECK


def test_clear_removes_every_marker_line():
    """Hand-edited files (or a racing writer) can hold more than one."""
    doubled = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\nBracketUnverified=3\nBracketUnverified=4\n",
    )
    cleared = dck_meta.clear_bracket_unverified(doubled)
    assert "BracketUnverified" not in cleared
    assert cleared == DECK


def test_clear_is_a_no_op_without_a_marker():
    assert dck_meta.clear_bracket_unverified(DECK) == DECK


# ---------------------------------------------------------------------------
# Placement + parsing rules (the "Forge must still load this" contract)
# ---------------------------------------------------------------------------

def test_marker_lands_inside_the_metadata_block():
    """Placement rule shared with
    ``moxfield_import._insert_metadata_lines``: after the last metadata
    line, before the first card-section header. Outside the block the
    module's own reader would not see it."""
    out = dck_meta.set_bracket_unverified(DECK, 3)
    lines = out.splitlines()
    assert lines[0] == "[metadata]"
    assert lines[2] == "BracketUnverified=3"
    assert lines.index("BracketUnverified=3") < lines.index("[Commander]")


def test_marker_is_not_a_mainboard_change():
    """THE structural requirement: the PUT route decides "did the
    mainboard change?" with ``main_card_quantities``, and it does that on
    text it is about to stamp the marker into. If the marker registered
    as a card, setting it would make the next save look like an edit."""
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    assert (
        dck_utils.main_card_quantities(marked)
        == dck_utils.main_card_quantities(DECK)
    )
    assert dck_utils.count_main_cards(marked) == 99


def test_marker_does_not_disturb_other_metadata():
    """``Protect=`` / ``PoliticsGuard=`` / ``Moxfield=`` pass through
    byte-identical — the marker is additive, not a rewrite."""
    rich = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\n"
        "Moxfield=abc123\n"
        "Protect=Krenko, Mob Boss\n"
        "PoliticsGuard=off\n",
    )
    out = dck_meta.set_bracket_unverified(rich, 3)
    for line in ("Moxfield=abc123", "Protect=Krenko, Mob Boss",
                 "PoliticsGuard=off"):
        assert line in out
    assert dck_meta.clear_bracket_unverified(out) == rich


def test_name_stamp_and_marker_compose():
    """``rewrite_name`` runs first in the PUT path; the marker must not
    confuse it, and vice versa."""
    marked = dck_meta.set_bracket_unverified(DECK, 3)
    renamed = dck_meta.rewrite_name(marked, "[USER] Delta [B3]")
    assert "Name=[USER] Delta [B3]" in renamed
    assert dck_meta.read_bracket_unverified(renamed) == 3


def test_key_is_case_insensitive():
    """Same rule ``read_protected_cards`` / ``politics_guard_enabled``
    enforce, so there is one [metadata] syntax to learn."""
    hand_typed = DECK.replace(
        "Name=[USER] Gamma [B3]\n",
        "Name=[USER] Gamma [B3]\nbracketunverified=2\n",
    )
    assert dck_meta.read_bracket_unverified(hand_typed) == 2
    assert "bracketunverified" not in (
        dck_meta.clear_bracket_unverified(hand_typed)
    )


def test_marker_outside_the_metadata_block_is_ignored():
    """Only ``[metadata]`` is consulted — a line under [Main] is a deck
    comment, not a directive."""
    stray = DECK.replace("[Main]\n", "[Main]\nBracketUnverified=3\n")
    assert dck_meta.read_bracket_unverified(stray) is None


def test_unparseable_value_reads_as_absent():
    """The only use of the number is comparison against the filename's
    [B<n>] tag; a value that cannot be compared cannot distinguish a live
    marker from one a rename left behind."""
    for bad in ("", "yes", "0", "6", "3.5"):
        text = DECK.replace(
            "Name=[USER] Gamma [B3]\n",
            f"Name=[USER] Gamma [B3]\nBracketUnverified={bad}\n",
        )
        assert dck_meta.read_bracket_unverified(text) is None, bad


def test_deck_without_metadata_header_gets_one():
    """Mirrors ``rewrite_name``: a header-less file would otherwise get a
    directive nothing reads."""
    bare = "[Commander]\n1 Test Cmdr\n\n[Main]\n1 Forest\n"
    out = dck_meta.set_bracket_unverified(bare, 5)
    assert out.startswith("[metadata]\nBracketUnverified=5\n")
    assert dck_meta.read_bracket_unverified(out) == 5


# --- R3 C-04 (2026-09-03): check_compare_name_alignment --------------------

def _pair(tmp_path, old_name, new_name):
    old = tmp_path / "[USER] Foo v1 [B3].dck"
    new = tmp_path / "[USER] Foo v2 [B3].dck"
    for p, name in ((old, old_name), (new, new_name)):
        meta = f"[metadata]\nName={name}\n" if name is not None else "[metadata]\n"
        p.write_text(f"{meta}[Main]\n1 Forest\n", encoding="utf-8")
    return old, new


def test_read_name_returns_none_for_missing_or_empty():
    assert dck_meta.read_name("[Main]\n1 Forest\n") is None
    assert dck_meta.read_name("[metadata]\nName=\n[Main]\n1 Forest\n") is None
    assert dck_meta.read_name("[metadata]\nName=Foo v1  \n") == "Foo v1"


def test_alignment_passes_a_restamped_pair(tmp_path):
    old, new = _pair(tmp_path, "[USER] Foo v1 [B3]", "Foo v2")
    assert dck_meta.check_compare_name_alignment(old, new) == []


def test_alignment_raises_on_a_hand_copied_pair(tmp_path):
    """The copy's Name= is the source's, so it mismatches its own stem."""
    import pytest
    old, new = _pair(tmp_path, "Foo v1", "Foo v1")
    with pytest.raises(ValueError, match="hand-copied"):
        dck_meta.check_compare_name_alignment(old, new)


def test_alignment_raises_when_two_stems_normalize_alike(tmp_path):
    import pytest
    old = tmp_path / "[USER] Foo [B3].dck"
    new = tmp_path / "Foo [B3].dck"
    for p in (old, new):
        p.write_text(f"[metadata]\nName={p.stem}\n[Main]\n1 Forest\n", encoding="utf-8")
    with pytest.raises(ValueError, match="share Name="):
        dck_meta.check_compare_name_alignment(old, new)


def test_alignment_raises_when_name_matches_no_filename(tmp_path):
    import pytest
    old, new = _pair(tmp_path, "Foo v1", "Something Else")
    with pytest.raises(ValueError, match="does not match its filename"):
        dck_meta.check_compare_name_alignment(old, new)


def test_alignment_warns_on_a_nameless_deck(tmp_path):
    old, new = _pair(tmp_path, None, "Foo v2")
    notes = dck_meta.check_compare_name_alignment(old, new)
    assert len(notes) == 1 and "no Name= line" in notes[0]


# ---------------------------------------------------------------------------
# R3 W-10 (2026-09-03) — CRLF decks keep one line-ending convention
# ---------------------------------------------------------------------------

def test_rewrite_name_keeps_the_crlf_of_the_name_line():
    from commander_builder.dck_meta import rewrite_name, set_bracket_unverified

    crlf = "[metadata]\r\nName=Foo\r\nProtect=Sol Ring\r\n\r\n[Main]\r\n1 Sol Ring\r\n"
    out = rewrite_name(crlf, "Foo")
    assert out == crlf
    assert rewrite_name(crlf, "Bar") == crlf.replace("Name=Foo", "Name=Bar")
    # A synthesized Name= and a synthesized marker use the file's ending.
    nameless = "[metadata]\r\nProtect=Sol Ring\r\n\r\n[Main]\r\n1 Sol Ring\r\n"
    assert "Name=Foo\r\n" in rewrite_name(nameless, "Foo")
    marked = set_bracket_unverified(crlf, 3)
    assert "BracketUnverified=3\r\n" in marked
    assert "\n" not in marked.replace("\r\n", "")
    # LF files are untouched by the CRLF fix.
    lf = crlf.replace("\r\n", "\n")
    assert set_bracket_unverified(lf, 3).count("\r") == 0


def test_rewrite_name_to_stem_is_atomic(tmp_path):
    """R3 W-09: the one core writer that still did a bare write_text."""
    from commander_builder.dck_meta import rewrite_name_to_stem

    p = tmp_path / "Foo.dck"
    p.write_text("[metadata]\nName=Old\n[Main]\n1 Sol Ring\n", encoding="utf-8")
    rewrite_name_to_stem(p)
    assert "Name=Foo" in p.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".*.tmp"))


def test_stamp_name_preserving_display_keeps_crlf():
    from commander_builder.dck_meta import stamp_name_preserving_display

    crlf = "[metadata]\r\nName=Pretty: Name\r\n\r\n[Main]\r\n1 Sol Ring\r\n"
    out = stamp_name_preserving_display(crlf, "Pretty_ Name")
    assert out == ("[metadata]\r\nName=Pretty_ Name\r\nDisplayName=Pretty: Name"
                   "\r\n\r\n[Main]\r\n1 Sol Ring\r\n")
