"""Tests for import_formats: MTGA/Arena + CSV paste parsers and the
conservative auto-detector.

The load-bearing invariant throughout: every new format must converge
on the SAME normalized .dck intermediate an equivalent plain-lines
paste produces, because everything downstream (Name= stamping, role
prefixes, count_main_cards, the swap splicer) consumes that shape.
Round-trip tests therefore compare through the web dispatcher
``_normalize_pasted_deck`` using ``dck_utils`` accessors rather than
string equality — aggregation/ordering may differ line-wise while the
deck content is identical.
"""
from __future__ import annotations

import pytest

from commander_builder.dck_utils import (
    main_card_quantities,
    section_card_names,
)
from commander_builder.import_formats import (
    ImportFormatError,
    arena_to_dck,
    csv_to_lines,
    detect_paste_format,
)
from commander_builder.web.deck_text_ops import _normalize_pasted_deck


# ---------------------------------------------------------------------------
# Auto-detection matrix — one test per format signature.
# ---------------------------------------------------------------------------


def test_detect_dck_sections_win():
    # Any [section] header → .dck, even if lines also look Arena-ish.
    text = "[Main]\n1 Sol Ring (C21) 263\n1 Arcane Signet (C21) 250\n"
    assert detect_paste_format(text) == "dck"


def test_detect_arena_by_deck_header():
    text = "Deck\n1 Sol Ring\n1 Arcane Signet\n"
    assert detect_paste_format(text) == "arena"


def test_detect_arena_by_about_header():
    text = "About\nName My Brew\n\n1 Sol Ring\n1 Forest\n"
    assert detect_paste_format(text) == "arena"


def test_detect_arena_by_printing_tails_majority():
    # No headers, but most lines carry (SET) CN tails.
    text = (
        "1 Lightning Bolt (M21) 159\n"
        "1 Shock (M21) 160\n"
        "1 Opt\n"
    )
    assert detect_paste_format(text) == "arena"


def test_detect_csv_comma_header():
    text = "Count,Name,Set\n1,Sol Ring,C21\n"
    assert detect_paste_format(text) == "csv"


def test_detect_csv_semicolon_header():
    text = "Quantity;Card Name;Price\n2;Forest;0.10\n"
    assert detect_paste_format(text) == "csv"


def test_detect_plain_lines():
    text = "1 Sol Ring\n1 Arcane Signet\n30 Forest\n"
    assert detect_paste_format(text) == "plain"


# ---------------------------------------------------------------------------
# Conservative detection — ambiguous shapes must fall back to plain.
# ---------------------------------------------------------------------------


def test_detect_comma_in_card_name_not_csv():
    # "1 Krenko, Mob Boss" contains a comma but its fields match no
    # recognized column name — must stay a plain paste.
    text = "1 Krenko, Mob Boss\n1 Goblin Matron\n"
    assert detect_paste_format(text) == "plain"


def test_detect_single_tailed_line_stays_plain():
    # One decorated line among many plain ones is not "most lines" —
    # and 2+ tailed lines are required regardless.
    text = (
        "1 Sol Ring (C21) 263\n"
        "1 Arcane Signet\n"
        "1 Command Tower\n"
        "1 Forest\n"
    )
    assert detect_paste_format(text) == "plain"


def test_detect_bare_commander_header_alone_stays_plain():
    # Moxfield-ish pastes carry commander-ish words; a bare
    # "Commander" line without Arena's Deck/About headers or printing
    # tails must not flip the whole paste to Arena.
    text = "Commander\n1 Edgar Markov\n1 Sol Ring\n"
    assert detect_paste_format(text) == "plain"


def test_detect_empty_text_is_plain():
    assert detect_paste_format("") == "plain"


# ---------------------------------------------------------------------------
# Arena parser
# ---------------------------------------------------------------------------

ARENA_FULL = (
    "About\n"
    "Name Boros Burn\n"
    "\n"
    "Commander\n"
    "1 Feather, the Redeemed (WAR) 197\n"
    "\n"
    "Deck\n"
    "1 Lightning Bolt (M21) 159\n"
    "1 Shock\n"
    "30 Mountain (ANA) 114\n"
    "\n"
    "Sideboard\n"
    "1 Deflecting Palm (KTK) 173\n"
)


def test_arena_sections_map_to_dck_sections():
    dck = arena_to_dck(ARENA_FULL)
    assert section_card_names(dck, "Commander") == ["Feather, the Redeemed"]
    assert main_card_quantities(dck) == {
        "Lightning Bolt": 1, "Shock": 1, "Mountain": 30,
    }
    # Sideboard preserved as its own section — same convention as a
    # .dck paste (downstream already ignores it for main-count math).
    assert section_card_names(dck, "Sideboard") == ["Deflecting Palm"]
    # The About section's "Name Boros Burn" line is metadata, not a card.
    assert "Boros Burn" not in dck


def test_arena_tails_stripped_quantities_kept():
    dck = arena_to_dck("Deck\n4 Lightning Bolt (M21) 159\n")
    assert main_card_quantities(dck) == {"Lightning Bolt": 4}
    assert "(M21)" not in dck and "159" not in dck


def test_arena_set_without_collector_number():
    dck = arena_to_dck("Deck\n1 Opt (XLN)\n")
    assert main_card_quantities(dck) == {"Opt": 1}


def test_arena_headerless_all_cards_to_main():
    dck = arena_to_dck("1 Lightning Bolt (M21) 159\n1 Shock (M21) 160\n")
    assert main_card_quantities(dck) == {"Lightning Bolt": 1, "Shock": 1}
    assert "[Commander]" not in dck and "[Sideboard]" not in dck


def test_arena_duplicate_lines_aggregate():
    dck = arena_to_dck("Deck\n1 Shock (M21) 160\n2 Shock\n1 Shock (STA) 44\n")
    assert main_card_quantities(dck) == {"Shock": 4}


def test_arena_blank_line_does_not_reroute_kept_section():
    # A cosmetic blank inside Sideboard must not silently move the
    # cards after it back into [Main].
    dck = arena_to_dck(
        "Deck\n1 Shock\n\nSideboard\n1 Negate\n\n1 Duress\n"
    )
    assert main_card_quantities(dck) == {"Shock": 1}
    assert section_card_names(dck, "Sideboard") == ["Negate", "Duress"]


def test_arena_companion_lands_in_sideboard():
    # The companion sits outside the 100 — same meaning as sideboard.
    dck = arena_to_dck("Companion\n1 Lurrus of the Dream-Den (IKO) 226\nDeck\n1 Shock\n")
    assert section_card_names(dck, "Sideboard") == ["Lurrus of the Dream-Den"]


def test_arena_split_card_name_with_tail():
    dck = arena_to_dck("Deck\n1 Fire // Ice (MH2) 290\n")
    assert main_card_quantities(dck) == {"Fire // Ice": 1}


def test_arena_malformed_line_names_the_line():
    bad = "Deck\n1 Shock (M21) 160\nLightning Bolt\n"
    with pytest.raises(ImportFormatError) as ei:
        arena_to_dck(bad)
    msg = str(ei.value)
    assert "line 3" in msg
    assert "Lightning Bolt" in msg


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------


def test_csv_basic_comma():
    lines = csv_to_lines("Count,Name\n1,Sol Ring\n2,Forest\n")
    assert lines == "1 Sol Ring\n2 Forest\n"


def test_csv_semicolon_delimiter():
    lines = csv_to_lines("Quantity;Name;Set\n3;Island;C21\n")
    assert lines == "3 Island\n"


def test_csv_header_names_case_insensitive_and_variant():
    lines = csv_to_lines("QTY,CARD NAME,Foil\n1,Sol Ring,No\n")
    assert lines == "1 Sol Ring\n"


def test_csv_extra_columns_tolerated():
    # Collection-export shape: set / price / foil columns are ignored.
    text = (
        "Count,Name,Edition,Price,Foil\n"
        '1,"Krenko, Mob Boss",DOM,0.50,No\n'
        "1,Goblin Matron,M20,0.25,Yes\n"
    )
    lines = csv_to_lines(text)
    assert lines == "1 Krenko, Mob Boss\n1 Goblin Matron\n"


def test_csv_quoted_name_with_delimiter():
    # The csv module (not naive splitting) must handle quoted commas.
    lines = csv_to_lines('Count,Name\n1,"Borrowing 100,000 Arrows"\n')
    assert lines == "1 Borrowing 100,000 Arrows\n"


def test_csv_missing_count_column_defaults_to_one():
    lines = csv_to_lines("Name,Set\nSol Ring,C21\nForest,C21\n")
    assert lines == "1 Sol Ring\n1 Forest\n"


def test_csv_duplicate_rows_aggregate():
    # Collection exports emit one row per printing of the same card.
    text = "Count,Name,Edition\n1,Shock,M21\n2,Shock,STA\n"
    assert csv_to_lines(text) == "3 Shock\n"


def test_csv_blank_rows_skipped():
    text = "Count,Name\n\n1,Sol Ring\n\n"
    assert csv_to_lines(text) == "1 Sol Ring\n"


def test_csv_bom_on_header_tolerated():
    text = "\ufeffCount,Name\n1,Sol Ring\n"
    assert detect_paste_format(text) == "csv"
    assert csv_to_lines(text) == "1 Sol Ring\n"


def test_csv_non_integer_count_names_the_line():
    with pytest.raises(ImportFormatError) as ei:
        csv_to_lines("Count,Name\nlots,Sol Ring\n")
    assert "whole number" in str(ei.value)
    assert "Sol Ring" in str(ei.value)


def test_csv_nonpositive_count_rejected():
    with pytest.raises(ImportFormatError):
        csv_to_lines("Count,Name\n0,Sol Ring\n")


def test_csv_missing_name_names_the_line():
    with pytest.raises(ImportFormatError) as ei:
        csv_to_lines("Count,Name,Set\n1,,C21\n")
    assert "missing a card name" in str(ei.value)


# ---------------------------------------------------------------------------
# Round-trips through the web dispatcher — each format must normalize
# to the same deck as an equivalent plain list.
# ---------------------------------------------------------------------------

# The reference plain paste and its expected normalized contents.
PLAIN_EQUIV = "1 Lightning Bolt\n1 Shock\n30 Mountain\n"


def test_roundtrip_arena_equals_plain():
    arena = (
        "Deck\n"
        "1 Lightning Bolt (M21) 159\n"
        "1 Shock (M21) 160\n"
        "30 Mountain (ANA) 114\n"
    )
    assert (
        main_card_quantities(_normalize_pasted_deck(arena))
        == main_card_quantities(_normalize_pasted_deck(PLAIN_EQUIV))
        == {"Lightning Bolt": 1, "Shock": 1, "Mountain": 30}
    )


def test_roundtrip_csv_equals_plain():
    csv_text = (
        "Count,Name,Set\n"
        "1,Lightning Bolt,M21\n"
        "1,Shock,M21\n"
        "30,Mountain,ANA\n"
    )
    # CSV routes through the plain-lines wrap, so here the FULL .dck
    # text is identical, not just the parsed quantities — CSV has
    # exactly the plain path's (non-)commander behavior by design.
    assert _normalize_pasted_deck(csv_text) == _normalize_pasted_deck(PLAIN_EQUIV)


def test_roundtrip_arena_commander_section():
    arena = (
        "Commander\n1 Feather, the Redeemed (WAR) 197\n\n"
        "Deck\n1 Shock (M21) 160\n"
    )
    # Detection: majority printing tails (2 of 2 qty-lines).
    dck = _normalize_pasted_deck(arena)
    assert section_card_names(dck, "Commander") == ["Feather, the Redeemed"]
    assert main_card_quantities(dck) == {"Shock": 1}


def test_roundtrip_ambiguous_falls_back_to_plain():
    # One tail among four lines: not Arena. The line passes through the
    # plain wrap VERBATIM (tail kept) — the historical behavior.
    text = (
        "1 Sol Ring (C21) 263\n"
        "1 Arcane Signet\n"
        "1 Command Tower\n"
        "1 Forest\n"
    )
    dck = _normalize_pasted_deck(text)
    assert dck.startswith("[Main]\n")
    assert "1 Sol Ring (C21) 263" in dck


def test_roundtrip_dck_paste_still_verbatim():
    # Regression guard: the dispatcher's "dck" branch must keep the
    # historical trust-the-user passthrough byte-for-byte (plus the
    # trailing newline it has always added).
    text = "[metadata]\nName=X\n[Commander]\n1 Edgar Markov\n[Main]\n1 Sol Ring"
    assert _normalize_pasted_deck(text) == text + "\n"


def test_roundtrip_csv_empty_body_yields_empty():
    # Header-only CSV → no cards → same "" the empty-paste path returns.
    assert _normalize_pasted_deck("Count,Name\n") == ""
