"""meta_test tests — pure-helpers verified directly, run_meta_test mocked
at the network + Forge boundary."""
import json
from pathlib import Path

import pytest

from commander_builder.meta_test import (
    CardDiffReport,
    ReferenceDeck,
    _parse_main_card_names,
    _ref_destination,
    compute_card_diff,
    fetch_reference_decks,
    format_report_text,
)


# --- _ref_destination ------------------------------------------------------

def test_ref_destination_uses_REF_prefix(tmp_path):
    p = _ref_destination("moxfield_top_likes", "Some Deck Name", 3, base=tmp_path)
    assert p.name.startswith("[REF] moxfield-top-likes ")
    assert p.name.endswith("[B3].dck")


def test_ref_destination_unknown_bracket(tmp_path):
    p = _ref_destination("edhrec_average", "Foo", 0, base=tmp_path)
    assert "[B?]" in p.name


# --- _import_reference — Name= alignment (regression) ----------------------

def test_import_reference_rewrites_name_to_ref_filename_stem(tmp_path):
    """Regression: ``to_dck`` stamps the raw Moxfield/EDHREC deck name into
    Name= while the file lands as ``[REF] <tag> <name> [Bn].dck``.
    ``log_parser._normalize`` never strips the '[REF] <tag>' prefix, so the
    normalized filename could never equal the normalized Name= — Forge's
    Match Result wins for the reference were attributed to nobody, every
    reference scored 0, and meta-test systematically flattered the user
    deck. The importer must stamp the on-disk Name= to the filename stem."""
    import re

    from commander_builder.log_parser import _normalize
    from commander_builder.meta_test import _import_reference

    deck_json = {
        "name": "Cool Reference Deck", "publicId": "mx-9", "bracket": 3,
        "boards": {
            "commanders": {"cards": {
                "k1": {"quantity": 1, "card": {"name": "Hakbal"}},
            }},
            "mainboard": {"cards": {
                "k2": {"quantity": 1, "card": {"name": "Sol Ring"}},
            }},
        },
    }
    ref = _import_reference(deck_json, "moxfield_top_likes", deck_dir=tmp_path)
    path = tmp_path / ref.deck_filename
    text = path.read_text(encoding="utf-8")

    m = re.search(r"^Name=(.+)$", text, re.MULTILINE)
    assert m, "reference deck must carry a Name= line"
    # Forge reports Name=; compare_versions queries by filename. The two
    # must normalize identically or the reference's wins vanish.
    assert m.group(1) == path.stem
    assert _normalize(m.group(1)) == _normalize(path.stem)
    assert len(re.findall(r"^Name=", text, re.MULTILINE)) == 1
    # Display name (used in the report header) keeps the human label.
    assert ref.name == "Cool Reference Deck"
    # Card content unaffected by the rewrite.
    assert "Sol Ring" in text


# --- _parse_main_card_names ------------------------------------------------

def test_parse_main_card_names_strips_qty_and_set(tmp_path):
    p = tmp_path / "x.dck"
    p.write_text(
        "[Commander]\n1 Atraxa\n[Main]\n1 Sol Ring|CMM|1\n4 Forest|UNF|451\n",
        encoding="utf-8",
    )
    cards = _parse_main_card_names(p)
    assert "Sol Ring" in cards
    assert "Forest" in cards


# --- compute_card_diff -----------------------------------------------------

def _ref(cards: list[str]) -> ReferenceDeck:
    return ReferenceDeck(
        source="test", moxfield_id="x", name="x", bracket=3,
        deck_filename="x.dck", main_cards=cards,
    )


def test_compute_card_diff_empty_when_no_references():
    diff = compute_card_diff(["A", "B"], [])
    assert diff.user_cards == ["A", "B"]
    assert diff.must_add == []
    assert diff.consider == []


def test_must_add_is_intersection_minus_user():
    """Cards in EVERY reference, not in user → must-add."""
    user = ["UserOnly"]
    refs = [_ref(["X", "Y", "Z"]), _ref(["Y", "Z", "W"])]
    diff = compute_card_diff(user, refs)
    # Y and Z are in both refs but not user.
    must_add_cards = sorted(s.card for s in diff.must_add)
    assert must_add_cards == ["Y", "Z"]
    # Each suggestion is in BOTH refs.
    for s in diff.must_add:
        assert s.in_n_references == 2
        assert s.total_references == 2
        assert s.confidence == "ALL"


def test_consider_is_in_some_refs_not_all():
    user = ["UserOnly"]
    refs = [_ref(["X", "Y"]), _ref(["Y", "Z"])]
    diff = compute_card_diff(user, refs)
    must_add_cards = sorted(s.card for s in diff.must_add)
    consider_cards = sorted(s.card for s in diff.consider)
    assert must_add_cards == ["Y"]
    assert consider_cards == ["X", "Z"]
    for s in diff.consider:
        assert s.in_n_references == 1
        assert s.total_references == 2
        assert s.confidence == "1/2"


def test_card_suggestion_frequency_label_unanimous():
    from commander_builder.meta_test import CardSuggestion
    s = CardSuggestion(card="Cyclonic Rift", in_n_references=5, total_references=5)
    assert s.frequency_label == "unanimous (5/5 refs)"
    assert s.confidence_tier == 3


def test_card_suggestion_frequency_label_majority():
    from commander_builder.meta_test import CardSuggestion
    s = CardSuggestion(card="Smothering Tithe", in_n_references=3, total_references=5)
    assert s.frequency_label == "majority (3/5 refs)"
    assert s.confidence_tier == 2


def test_card_suggestion_frequency_label_minority():
    from commander_builder.meta_test import CardSuggestion
    s = CardSuggestion(card="Niche Card", in_n_references=1, total_references=5)
    assert s.frequency_label == "minority (1/5 refs)"
    assert s.confidence_tier == 1


def test_off_meta_is_user_minus_any_ref():
    user = ["UserOnly", "Shared", "AlsoUserOnly"]
    refs = [_ref(["Shared", "OtherRef"])]
    diff = compute_card_diff(user, refs)
    assert sorted(diff.off_meta) == ["AlsoUserOnly", "UserOnly"]


def test_shared_with_user_appears_in_both():
    user = ["A", "B", "C"]
    refs = [_ref(["A", "B"])]
    diff = compute_card_diff(user, refs)
    assert sorted(diff.shared_with_user) == ["A", "B"]


def test_diff_is_case_insensitive_for_set_math():
    """User says 'Sol Bling', ref says 'sol bling' — they should match.
    (Using a non-staple name so the universal-staple filter doesn't drop it.)"""
    user = ["Sol Bling"]
    refs = [_ref(["sol bling", "Other"])]
    diff = compute_card_diff(user, refs)
    assert "Sol Bling" not in diff.off_meta  # ref has it (case-insensitive)
    must_add_cards = {s.card for s in diff.must_add}
    consider_cards = {s.card for s in diff.consider}
    assert "Other" in must_add_cards or "Other" in consider_cards


def test_diff_drops_universal_staples_from_must_add(tmp_path):
    """GAP-029 fix: Sol Ring etc. shouldn't appear in must-add even if the
    user is missing them, because every Commander deck has them anyway."""
    user = ["UserOnly"]
    refs = [_ref(["Sol Ring", "Arcane Signet", "Forest", "Coat of Arms"])]
    diff = compute_card_diff(user, refs)
    must_add_cards = {s.card for s in diff.must_add}
    assert "Sol Ring" not in must_add_cards
    assert "Arcane Signet" not in must_add_cards
    assert "Forest" not in must_add_cards
    # Non-staple still surfaces.
    assert "Coat of Arms" in must_add_cards
    # Excluded list captures the staples we dropped.
    excluded_lc = {c.lower() for c in diff.excluded_universal_staples}
    assert "sol ring" in excluded_lc


def test_diff_drops_universal_staples_from_off_meta():
    """If the user has Sol Ring and the reference doesn't (rare/synthesized
    reference), Sol Ring still shouldn't appear in off-meta."""
    user = ["Sol Ring", "Off-Card"]
    refs = [_ref(["Other"])]   # ref doesn't have Sol Ring or Off-Card
    diff = compute_card_diff(user, refs)
    assert "Sol Ring" not in diff.off_meta
    assert "Off-Card" in diff.off_meta


def test_must_add_sorted_by_frequency_then_name():
    """Higher-frequency suggestions come first; ties break alphabetically."""
    user = []
    refs = [
        _ref(["B", "C", "Y"]),
        _ref(["B", "C", "Z"]),
        _ref(["C", "Z", "A"]),
    ]
    diff = compute_card_diff(user, refs)
    cards_in_order = [s.card for s in diff.must_add]
    # C is in all 3; B in 2; rest in 1. Within ties, alphabetical.
    # Note: only "in ALL refs" goes to must_add, so just C.
    assert cards_in_order == ["C"]


def test_consider_sorted_by_frequency_desc():
    """The 'consider' list (in some refs, not all) ranks by how many refs
    each card is in — highest first."""
    user = []
    refs = [
        _ref(["A", "B"]),
        _ref(["A", "C"]),
        _ref(["A", "D"]),  # A is universal, B/C/D each appear once
    ]
    diff = compute_card_diff(user, refs)
    # A is in all 3 → must_add. B/C/D each in 1 → consider, alphabetical.
    consider_freqs = [s.in_n_references for s in diff.consider]
    # All "consider" entries should have the same frequency (1) here.
    assert all(f == 1 for f in consider_freqs)


def test_must_add_by_role_groups_correctly():
    """Suggestions tagged by role: tutors → tutor bucket, finishers → finisher
    bucket, etc."""
    user = []
    refs = [_ref(["Demonic Tutor", "Craterhoof Behemoth", "Random Card"])]
    diff = compute_card_diff(user, refs)
    groups = diff.must_add_by_role()
    # Each group key maps to a list of CardSuggestion.
    tutor_cards = [s.card for s in groups.get("tutor", [])]
    finisher_cards = [s.card for s in groups.get("finisher", [])]
    other_cards = [s.card for s in groups.get("other", [])]
    assert "Demonic Tutor" in tutor_cards
    assert "Craterhoof Behemoth" in finisher_cards
    assert "Random Card" in other_cards


# --- fetch_reference_decks (HTTP mocked) -----------------------------------

def test_fetch_reference_decks_pulls_top_likes_and_edhrec_avg(tmp_path, monkeypatch):
    """Both auto-references resolve cleanly."""
    fake_mox_top = {
        "name": "Top Likes Deck", "publicId": "mx-1", "bracket": 3,
        "boards": {
            "commanders": {"cards": {"k1": {"quantity": 1, "card": {"name": "Hakbal"}}}},
            "mainboard": {"cards": {
                "k2": {"quantity": 1, "card": {"name": "Coat of Arms", "set": "TSR"}},
                "k3": {"quantity": 1, "card": {"name": "Lord of Atlantis"}},
            }},
        },
    }
    fake_edhrec_avg_url = "https://moxfield.com/decks/edhrec-avg-id"
    fake_edhrec_avg = {
        "name": "EDHREC Average", "publicId": "edhrec-avg-id", "bracket": 3,
        "boards": {
            "commanders": {"cards": {"k1": {"quantity": 1, "card": {"name": "Hakbal"}}}},
            "mainboard": {"cards": {
                "k2": {"quantity": 1, "card": {"name": "Lord of Atlantis"}},
                "k3": {"quantity": 1, "card": {"name": "Kindred Discovery"}},
            }},
        },
    }

    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: fake_mox_top,
    )

    class _FakePage:
        average_deck_url = fake_edhrec_avg_url
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_commander_page",
        lambda name, **kw: _FakePage(),
    )
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_deck",
        lambda deck_id: fake_edhrec_avg,
    )

    refs = fetch_reference_decks(
        "Hakbal of the Surging Soul",
        bracket=3,
        deck_dir=tmp_path,
    )
    assert len(refs) == 2
    sources = {r.source for r in refs}
    assert "moxfield_top_likes" in sources
    assert "edhrec_average" in sources

    # Files written with [REF] prefix.
    files = list(tmp_path.glob("*.dck"))
    assert all(f.name.startswith("[REF]") for f in files)
    assert len(files) == 2


def test_fetch_reference_decks_handles_missing_top_likes(tmp_path, monkeypatch):
    """Moxfield search returns nothing → only EDHREC reference fetched."""
    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: None,
    )
    # New EDHREC fetcher path: fake fetch_average_deck instead of
    # fetch_commander_page + fetch_deck.
    fake_avg_dict = {
        "name": "EDHREC Average — Foo", "publicId": None, "bracket": 3,
        "boards": {"commanders": {"cards": {}}, "mainboard": {"cards": {}}},
    }
    monkeypatch.setattr(
        "commander_builder.meta_test._fetch_edhrec_average_deck",
        lambda commander_name, bracket=None, **kw: fake_avg_dict,
    )
    refs = fetch_reference_decks("Foo", bracket=3, deck_dir=tmp_path)
    assert len(refs) == 1
    assert refs[0].source == "edhrec_average"


def test_fetch_reference_decks_handles_no_edhrec_url(tmp_path, monkeypatch):
    """EDHREC page has no average_deck_url → only Moxfield reference fetched."""
    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: {
            "name": "Top", "publicId": "top", "bracket": 3,
            "boards": {"commanders": {"cards": {}}, "mainboard": {"cards": {}}},
        },
    )
    class _FakePage:
        average_deck_url = None
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_commander_page",
        lambda name, **kw: _FakePage(),
    )
    refs = fetch_reference_decks("Foo", deck_dir=tmp_path)
    assert len(refs) == 1
    assert refs[0].source == "moxfield_top_likes"


def test_fetch_reference_decks_includes_extra_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: None,
    )
    class _FakePage:
        average_deck_url = None
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_commander_page",
        lambda name, **kw: _FakePage(),
    )
    fake_manual = {
        "name": "Manual", "publicId": "manual", "bracket": 4,
        "boards": {"commanders": {"cards": {}}, "mainboard": {"cards": {}}},
    }
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_deck",
        lambda deck_id: fake_manual,
    )
    refs = fetch_reference_decks(
        "Foo",
        extra_urls=["https://moxfield.com/decks/whatever"],
        deck_dir=tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].source == "manual"


# --- format_report_text ----------------------------------------------------

def test_format_report_text_includes_winner_framing():
    from commander_builder.meta_test import CardSuggestion, MetaTestReport
    diff = CardDiffReport(
        must_add=[
            CardSuggestion(card="X", in_n_references=1, total_references=1, role="other"),
            CardSuggestion(card="Y", in_n_references=1, total_references=1, role="other"),
        ],
        consider=[
            CardSuggestion(card="Z", in_n_references=1, total_references=2, role="other"),
        ],
        off_meta=["UserOnly"],
    )
    report = MetaTestReport(
        user_deck="user.dck", bracket=3, timestamp="2026-04-26T00:00:00",
        references=[_ref(["X", "Y", "Z"])],
        comparisons=[],
        card_diff=diff,
        user_record={
            "user_wins": 2, "user_losses": 8, "draws": 0,
            "total_games": 10, "win_rate": 0.2,
        },
    )
    text = format_report_text(report)
    assert "Must-add" in text
    assert "X" in text and "Y" in text
    assert "BEAT your deck" in text  # losses > wins framing


def test_format_report_text_outperform_framing():
    from commander_builder.meta_test import MetaTestReport
    report = MetaTestReport(
        user_deck="user.dck", bracket=3, timestamp="x",
        references=[_ref(["X"])],
        card_diff=CardDiffReport(),
        user_record={
            "user_wins": 8, "user_losses": 2, "draws": 0,
            "total_games": 10, "win_rate": 0.8,
        },
    )
    text = format_report_text(report)
    assert "OUTPERFORMED" in text


def test_format_report_text_all_draws_framing():
    """0-0-N (all draws) should explicitly say neither could close,
    not 'roughly even'."""
    from commander_builder.meta_test import MetaTestReport
    report = MetaTestReport(
        user_deck="user.dck", bracket=3, timestamp="x",
        references=[_ref(["X"])],
        card_diff=CardDiffReport(),
        user_record={
            "user_wins": 0, "user_losses": 0, "draws": 4,
            "total_games": 4, "win_rate": 0.0,
        },
    )
    text = format_report_text(report)
    assert "NEITHER deck could close" in text
    assert "add a finisher" in text


def test_extra_url_routes_edhrec_through_average_fetcher(tmp_path, monkeypatch):
    """--reference-url with an edhrec.com/average-decks/* URL should go
    through fetch_average_deck, not Moxfield's fetch_deck."""
    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: None,
    )
    class _FakePage:
        average_deck_url = None
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_commander_page",
        lambda name, **kw: _FakePage(),
    )

    captured_routes: list[str] = []

    def fake_edhrec(url):
        captured_routes.append(("edhrec", url))
        return {
            "name": "From EDHREC", "publicId": None, "bracket": 3,
            "boards": {"commanders": {"cards": {}}, "mainboard": {"cards": {}}},
        }
    def fake_moxfield(deck_id):
        captured_routes.append(("moxfield", deck_id))
        raise AssertionError("EDHREC URL should not route through Moxfield fetch_deck")

    monkeypatch.setattr(
        "commander_builder.meta_test._fetch_edhrec_deck_by_url", fake_edhrec,
    )
    monkeypatch.setattr("commander_builder.meta_test.fetch_deck", fake_moxfield)

    refs = fetch_reference_decks(
        "Foo",
        extra_urls=["https://edhrec.com/average-decks/foo/upgraded/expensive"],
        deck_dir=tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].source == "manual_edhrec"
    assert captured_routes[0][0] == "edhrec"


def test_extra_url_routes_moxfield_unchanged(tmp_path, monkeypatch):
    """Non-EDHREC URLs still go through Moxfield's fetch_deck."""
    monkeypatch.setattr(
        "commander_builder.meta_test.find_top_liked_deck_for_commander",
        lambda name, bracket=None, **kw: None,
    )
    class _FakePage:
        average_deck_url = None
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_commander_page",
        lambda name, **kw: _FakePage(),
    )
    monkeypatch.setattr(
        "commander_builder.meta_test.fetch_deck",
        lambda deck_id: {
            "name": "From Moxfield", "publicId": "abc", "bracket": 3,
            "boards": {"commanders": {"cards": {}}, "mainboard": {"cards": {}}},
        },
    )
    refs = fetch_reference_decks(
        "Foo",
        extra_urls=["https://moxfield.com/decks/abc"],
        deck_dir=tmp_path,
    )
    assert len(refs) == 1
    assert refs[0].source == "manual"
