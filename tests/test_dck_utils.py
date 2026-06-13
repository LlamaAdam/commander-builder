"""Unit tests for dck_utils — the canonical .dck section/line primitives.

These primitives back the thin wrappers in knowledge_log, deck_health,
compare_versions, intent, archetype, meta_test, improvement_advisor,
scryfall_client, and web/routes_audit, so the edge cases here (quantity
summing, |set|cn stripping, case-insensitive headers, malformed lines,
section boundaries) are load-bearing for all of them.
"""
from commander_builder.dck_utils import (
    count_main_cards,
    iter_main_cards,
    iter_section_lines,
    main_card_names,
    main_card_quantities,
    parse_card_line,
    section_card_names,
)

SAMPLE_DCK = """\
[metadata]
Name=Test Deck

[Commander]
1 Atraxa, Praetors' Voice|CMM|1

[Main]
1 Sol Ring|C21|263
12 Forest
2 Arcane Signet|CMR
1 Fire // Ice|MH2|290

[Sideboard]
1 Counterspell
"""


# --- iter_section_lines -----------------------------------------------------

def test_iter_section_lines_yields_only_main_lines():
    lines = list(iter_section_lines(SAMPLE_DCK, "Main"))
    assert lines == [
        "1 Sol Ring|C21|263",
        "12 Forest",
        "2 Arcane Signet|CMR",
        "1 Fire // Ice|MH2|290",
    ]


def test_iter_section_lines_header_match_is_case_insensitive():
    text = "[MAIN]\n1 Sol Ring\n"
    assert list(iter_section_lines(text, "Main")) == ["1 Sol Ring"]
    assert list(iter_section_lines(text, "MAIN")) == ["1 Sol Ring"]


def test_iter_section_lines_stops_at_next_header():
    text = "[Main]\n1 Sol Ring\n[Sideboard]\n1 Counterspell\n"
    assert list(iter_section_lines(text, "Main")) == ["1 Sol Ring"]


def test_iter_section_lines_skips_blank_lines_and_strips_whitespace():
    text = "[Main]\n\n   1 Sol Ring   \n\n"
    assert list(iter_section_lines(text, "Main")) == ["1 Sol Ring"]


def test_iter_section_lines_resumes_when_section_reappears():
    text = "[Main]\n1 Sol Ring\n[Sideboard]\n1 Foo\n[Main]\n1 Forest\n"
    assert list(iter_section_lines(text, "Main")) == ["1 Sol Ring", "1 Forest"]


def test_iter_section_lines_missing_section_yields_nothing():
    assert list(iter_section_lines(SAMPLE_DCK, "Avatar")) == []


def test_iter_section_lines_empty_or_none_text_yields_nothing():
    assert list(iter_section_lines("", "Main")) == []
    assert list(iter_section_lines(None, "Main")) == []


def test_iter_section_lines_reads_commander_section():
    lines = list(iter_section_lines(SAMPLE_DCK, "Commander"))
    assert lines == ["1 Atraxa, Praetors' Voice|CMM|1"]


# --- parse_card_line --------------------------------------------------------

def test_parse_card_line_strips_set_and_collector_number():
    assert parse_card_line("1 Sol Ring|C21|263") == (1, "Sol Ring")


def test_parse_card_line_handles_set_only_suffix():
    assert parse_card_line("2 Arcane Signet|CMR") == (2, "Arcane Signet")


def test_parse_card_line_without_suffix_keeps_full_name():
    assert parse_card_line("12 Forest") == (12, "Forest")


def test_parse_card_line_strips_space_before_pipe():
    assert parse_card_line("1 Card Name |SET") == (1, "Card Name")


def test_parse_card_line_keeps_split_card_names():
    assert parse_card_line("3 Fire // Ice|MH2|290") == (3, "Fire // Ice")


def test_parse_card_line_returns_none_for_malformed_lines():
    assert parse_card_line("Name=Test Deck") is None
    assert parse_card_line("[Main]") is None
    assert parse_card_line("1") is None
    assert parse_card_line("10x Card") is None
    assert parse_card_line("") is None
    assert parse_card_line("Sol Ring") is None


# --- count_main_cards -------------------------------------------------------

def test_count_main_cards_sums_quantity_prefixes():
    # 1 + 12 + 2 + 1 from [Main]; commander and sideboard excluded.
    assert count_main_cards(SAMPLE_DCK) == 16


def test_count_main_cards_handles_none_and_empty_text():
    assert count_main_cards(None) == 0
    assert count_main_cards("") == 0


def test_count_main_cards_ignores_malformed_lines():
    text = "[Main]\n1 Sol Ring\nnot a card line\nName=Oops\n"
    assert count_main_cards(text) == 1


def test_count_main_cards_accepts_uppercase_header():
    assert count_main_cards("[MAIN]\n4 Island\n") == 4


# --- iter_main_cards --------------------------------------------------------

def test_iter_main_cards_yields_qty_name_pairs_in_deck_order():
    cards = list(iter_main_cards(SAMPLE_DCK))
    assert cards == [
        (1, "Sol Ring"),
        (12, "Forest"),
        (2, "Arcane Signet"),
        (1, "Fire // Ice"),
    ]


def test_iter_main_cards_keeps_duplicate_names_separate():
    text = "[Main]\n1 Forest\n2 Forest|M21\n"
    assert list(iter_main_cards(text)) == [(1, "Forest"), (2, "Forest")]


def test_iter_main_cards_skips_empty_name_degenerates():
    # "1   |x" matches the card regex but strips to an empty name.
    text = "[Main]\n1   |x\n1 Sol Ring\n"
    assert list(iter_main_cards(text)) == [(1, "Sol Ring")]


# --- main_card_quantities ---------------------------------------------------

def test_main_card_quantities_merges_duplicates_across_printings():
    text = "[Main]\n1 Forest|M21|266\n2 Forest|NEO\n1 Sol Ring\n"
    assert main_card_quantities(text) == {"Forest": 3, "Sol Ring": 1}


def test_main_card_quantities_handles_none_and_empty_text():
    assert main_card_quantities(None) == {}
    assert main_card_quantities("") == {}


def test_main_card_quantities_ignores_other_sections():
    assert main_card_quantities(SAMPLE_DCK) == {
        "Sol Ring": 1,
        "Forest": 12,
        "Arcane Signet": 2,
        "Fire // Ice": 1,
    }


# --- section_card_names / main_card_names -----------------------------------

def test_main_card_names_strips_qty_and_set_suffix():
    assert main_card_names(SAMPLE_DCK) == [
        "Sol Ring",
        "Forest",
        "Arcane Signet",
        "Fire // Ice",
    ]


def test_main_card_names_keeps_duplicates_in_order():
    text = "[Main]\n1 Forest\n1 Forest|M21\n"
    assert main_card_names(text) == ["Forest", "Forest"]


def test_section_card_names_reads_commander_section():
    names = section_card_names(SAMPLE_DCK, "Commander")
    assert names == ["Atraxa, Praetors' Voice"]


def test_section_card_names_skips_lines_without_qty_prefix():
    text = "[Commander]\nAtraxa, Praetors' Voice\n1 Tymna the Weaver\n"
    assert section_card_names(text, "Commander") == ["Tymna the Weaver"]


def test_section_card_names_case_insensitive_header():
    text = "[COMMANDER]\n1 Atraxa, Praetors' Voice|CMM|1\n"
    assert section_card_names(text, "Commander") == ["Atraxa, Praetors' Voice"]
