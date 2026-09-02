"""Dashboard legality must use the shared validator without a second fetch pass."""
from __future__ import annotations

from collections import Counter

import pytest

from commander_builder.deck_dashboard import build_dashboard


@pytest.fixture
def dashboard_cards(monkeypatch):
    """Resolve complete card evidence locally; all external probes are offline."""
    cards = {
        "Test Commander": {
            "name": "Test Commander",
            "type_line": "Legendary Creature — Elf",
            "oracle_text": "",
            "color_identity": ["G"],
            "legalities": {"commander": "legal"},
        },
        "Forest": {
            "name": "Forest",
            "type_line": "Basic Land — Forest",
            "oracle_text": "{T}: Add {G}.",
            "color_identity": ["G"],
            "legalities": {"commander": "legal"},
        },
        "Test Artifact": {
            "name": "Test Artifact",
            "type_line": "Artifact",
            "oracle_text": "",
            "color_identity": [],
            "legalities": {"commander": "legal"},
        },
    }
    monkeypatch.setattr("commander_builder.deck_dashboard.lookup_card", cards.get)
    monkeypatch.setattr(
        "commander_builder.scryfall_client.lookup_card", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "commander_builder.game_changers.load_game_changers", lambda: set(),
    )
    monkeypatch.setattr(
        "commander_builder.edhrec_client.fetch_salt_list", lambda: {},
    )
    monkeypatch.setattr(
        "commander_builder.bracket_estimator.estimate_bracket",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "commander_builder.oracle_store.snapshot_age_days", lambda name: None,
    )
    return cards


def _deck(tmp_path, main="98 Forest\n1 Test Artifact\n", commander="1 Test Commander\n"):
    path = tmp_path / "legality.dck"
    path.write_text(f"[Commander]\n{commander}[Main]\n{main}", encoding="utf-8")
    return path


def test_dashboard_unknown_card_is_unverified_not_legal(tmp_path, dashboard_cards):
    result = build_dashboard(_deck(tmp_path, "98 Forest\n1 Fictional Card\n"))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "unverified"
    assert result.legality["verified"] is False
    assert result.legality["violations"] == []
    assert result.legality["lookup_failures"] == 1
    assert {v["code"] for v in result.legality["unverified"]} == {
        "UNVERIFIED_COLOR_IDENTITY", "UNVERIFIED_BANNED",
    }
    assert all(v["cards"] == ["Fictional Card"] for v in result.legality["unverified"])


@pytest.mark.parametrize("card_name", ["Test Artifact", "Test Commander"])
def test_dashboard_explicitly_banned_card_is_illegal(
    tmp_path, dashboard_cards, card_name,
):
    dashboard_cards[card_name]["legalities"]["commander"] = "banned"
    result = build_dashboard(_deck(tmp_path))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "illegal"
    assert result.legality["violations"] == [{
        "code": "BANNED_CARD", "message": "Banned in Commander.",
        "cards": [card_name],
    }]
    assert result.legality["illegal_cards"] == [card_name]
    assert result.legality["n_illegal"] == 1


def test_dashboard_color_identity_violation_is_not_a_legal_deck(
    tmp_path, dashboard_cards,
):
    dashboard_cards["Test Artifact"]["color_identity"] = ["U"]
    result = build_dashboard(_deck(tmp_path))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "illegal"
    assert [v["code"] for v in result.legality["violations"]] == ["COLOR_IDENTITY"]
    assert result.legality["violations"][0]["cards"] == ["Test Artifact (U)"]


def test_dashboard_missing_legality_field_is_unverified(tmp_path, dashboard_cards):
    del dashboard_cards["Test Artifact"]["legalities"]
    result = build_dashboard(_deck(tmp_path))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "unverified"
    assert result.legality["lookup_failures"] == 0  # Card resolved, field did not.
    assert result.legality["violations"] == []
    assert [v["code"] for v in result.legality["unverified"]] == ["UNVERIFIED_BANNED"]


@pytest.mark.parametrize("section", ["Main", "Commander"])
def test_dashboard_malformed_line_cannot_claim_verified_legal(
    tmp_path, dashboard_cards, section,
):
    main = "99 Forest\n"
    commander = "1 Test Commander\n"
    if section == "Main":
        main += "not a card line\n"
    else:
        commander += "not a card line\n"

    result = build_dashboard(_deck(tmp_path, main, commander))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "illegal"
    assert result.legality["card_count"] == 100
    assert [v["code"] for v in result.legality["violations"]] == ["MALFORMED_CARD_LINE"]


@pytest.mark.parametrize(
    ("main", "commander", "expected_code", "expected_count"),
    [
        ("99 Forest\n", "", "COMMANDER_MISSING", 99),
        ("1 Forest\n", "1 Test Commander\n", "DECK_SIZE", 2),
        ("97 Forest\n2 Test Artifact\n", "1 Test Commander\n", "DUPLICATE_CARD", 100),
        ("99 Forest\n", "1 Test Artifact\n", "COMMANDER_INELIGIBLE", 100),
        ("98 Forest\n", "2 Test Commander\n", "COMMANDER_PAIR", 100),
    ],
)
def test_dashboard_invalid_construction_reports_shared_rule(
    tmp_path, dashboard_cards, main, commander, expected_code, expected_count,
):
    result = build_dashboard(_deck(tmp_path, main, commander))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "illegal"
    assert expected_code in {v["code"] for v in result.legality["violations"]}
    assert result.legality["card_count"] == expected_count
    assert result.legality["deck_total"] == expected_count
    assert result.deck_progress["current"] == expected_count


def test_dashboard_verified_legal_reuses_resolved_card_evidence(
    tmp_path, dashboard_cards, monkeypatch,
):
    calls = Counter()

    def resolve_once(name):
        calls[name] += 1
        if calls[name] > 1:
            raise AssertionError("The legality pass must reuse card evidence")
        return dashboard_cards.get(name)

    monkeypatch.setattr("commander_builder.deck_dashboard.lookup_card", resolve_once)
    result = build_dashboard(_deck(tmp_path))

    assert result.legality.get("status") == "legal"
    assert result.legality["all_legal"] is True
    assert result.legality["verified"] is True
    assert result.legality["violations"] == []
    assert result.legality["unverified"] == []
    assert result.legality["lookup_failures"] == 0
    assert result.legality["data_age_days"] is None
    assert result.legality["data_warning"] is None
    assert calls == {"Test Commander": 1, "Forest": 1, "Test Artifact": 1}


@pytest.mark.parametrize("printing_suffix", ["+|SET|1", "+"])
def test_dashboard_forge_foil_names_share_verified_card_evidence(
    tmp_path, dashboard_cards, monkeypatch, printing_suffix,
):
    calls = Counter()

    def resolve_card(name):
        calls[name] += 1
        # Both spellings resolve, isolating the evidence-key mismatch
        # from any upstream failure to recognize a foil-marked name.
        return dashboard_cards.get(name.rstrip("+"))

    monkeypatch.setattr("commander_builder.deck_dashboard.lookup_card", resolve_card)
    deck = _deck(
        tmp_path, f"99 Forest{printing_suffix}\n",
        f"1 Test Commander{printing_suffix}\n",
    )
    original_text = deck.read_text(encoding="utf-8")

    result = build_dashboard(deck)

    assert result.legality["status"] == "legal"
    assert result.legality["all_legal"] is True
    assert result.legality["verified"] is True
    assert result.legality["unverified"] == []
    assert result.legality["lookup_failures"] == 0
    assert result.deck_progress == {"current": 100, "target": 100}
    assert result.stat_tiles["lands"] == 99
    assert calls == {"Test Commander": 1, "Forest": 1}
    assert deck.read_text(encoding="utf-8") == original_text


def test_dashboard_lookup_outage_is_unverified_not_legal(
    tmp_path, dashboard_cards, monkeypatch,
):
    def unavailable(name):
        raise OSError("offline")

    monkeypatch.setattr("commander_builder.deck_dashboard.lookup_card", unavailable)
    result = build_dashboard(_deck(tmp_path))

    assert result.legality["all_legal"] is False
    assert result.legality["status"] == "unverified"
    assert result.legality["violations"] == []
    assert result.legality["lookup_failures"] == 3
    assert {v["code"] for v in result.legality["unverified"]} == {
        "UNVERIFIED_COMMANDER", "UNVERIFIED_COLOR_IDENTITY", "UNVERIFIED_BANNED",
    }


def test_dashboard_stale_evidence_exposes_refresh_warning(
    tmp_path, dashboard_cards, monkeypatch,
):
    monkeypatch.setattr(
        "commander_builder.oracle_store.snapshot_age_days", lambda name: 80.0,
    )
    result = build_dashboard(_deck(tmp_path))

    assert result.legality.get("data_age_days") == 80.0
    assert "80 days old" in result.legality["data_warning"]
    assert "commander-oracle-refresh" in result.legality["data_warning"]
    assert result.legality["status"] == "legal"  # Freshness is advisory.


@pytest.mark.parametrize("partner_available", [True, False])
def test_dashboard_partner_uses_cache_only_and_reports_available_evidence(
    tmp_path, dashboard_cards, monkeypatch, partner_available,
):
    dashboard_cards["Test Commander"]["oracle_text"] = "Partner"
    if partner_available:
        dashboard_cards["Test Partner"] = {
            **dashboard_cards["Test Commander"], "name": "Test Partner",
        }
    calls = []

    def lookup(name, *, cache_only=False):
        calls.append((name, cache_only))
        return dashboard_cards.get(name)

    monkeypatch.setattr("commander_builder.deck_dashboard.lookup_card", lookup)
    result = build_dashboard(_deck(
        tmp_path, "98 Forest\n", "1 Test Commander\n1 Test Partner\n",
    ))

    assert result.legality["all_legal"] is partner_available
    if partner_available:
        assert result.legality["status"] == "legal"
        assert result.legality["unverified"] == []
    else:
        assert result.legality["status"] == "unverified"
        assert "UNVERIFIED_PAIR" in {v["code"] for v in result.legality["unverified"]}
    assert ("Test Partner", True) in calls
    assert ("Test Partner", False) not in calls
