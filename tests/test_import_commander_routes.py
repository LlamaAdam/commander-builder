"""Hermetic import and commander-edit regressions using a temporary library."""
from urllib.parse import urlencode
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")

from commander_builder.dck_utils import main_card_quantities, section_card_names
from commander_builder.web.routes_decks import make_decks_blueprint


@pytest.fixture
def library(tmp_path):
    app = flask.Flask(__name__)
    app.register_blueprint(make_decks_blueprint(tmp_path))
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


@pytest.mark.parametrize("paste", ["hello world", "Count,Name\n", "[Main]\n", "0 Forest"])
def test_invalid_import_leaves_library_unchanged(library, paste):
    client, directory = library
    response = client.post("/api/import_deck", json={"name": "Bad", "paste_text": paste})
    assert response.status_code == 400
    assert list(directory.iterdir()) == []


def test_import_selects_partner_pair_without_duplicating_cards(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "Partners", "paste_text": "1 Pako, Arcane Retriever\n1 Haldan, Avid Arcanist\n98 Forest",
        "commander": "Pako, Arcane Retriever", "partner": "Haldan, Avid Arcanist",
    })
    assert response.status_code == 200
    text = next(directory.glob("*.dck")).read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["Pako, Arcane Retriever", "Haldan, Avid Arcanist"]
    assert main_card_quantities(text) == {"Forest": 98}


def test_commander_editor_moves_one_copy_and_preserves_all_other_sections(library):
    client, directory = library
    path = directory / "[USER] Test [B3].dck"
    path.write_text(
        "[metadata]\nName=Test\n[Commander]\n1 Old One+|C21|1\n1 Old Two|C21|2\n"
        "[Main]\n2 New One+|CMM|12a/b\n1 New Two|CMM|3\n95 Forest\n"
        "[Sideboard]\n1 Sol Ring|C21|263\n[Considering]\n1 Arcane Signet\n",
        encoding="utf-8",
    )
    url = "/api/deck_commander?" + urlencode({"deck": path.stem})
    response = client.put(url, json={"commander": "New One", "partner": "New Two"})
    assert response.status_code == 200
    text = path.read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["New One", "New Two"]
    assert main_card_quantities(text) == {"New One": 1, "Forest": 95, "Old One": 1, "Old Two": 1}
    assert "1 New One+|CMM|12a/b" in text
    assert "1 Old One+|C21|1" in text
    assert "[Sideboard]\n1 Sol Ring|C21|263\n[Considering]\n1 Arcane Signet\n" in text
    assert response.json["bracket_tag_unverified"] is True
    summary = client.get(url)
    assert summary.json["commanders"] == ["New One", "New Two"]
    assert "Old One" in summary.json["candidates"]


@pytest.mark.parametrize("fields", [
    {"commander": "Forest\n[Main]\n1 Sol Ring"}, {"commander": ["Forest"]},
    {"commander": "Forest", "partner": "forest"}, {"partner": "Forest"},
])
def test_invalid_commander_selection_does_not_write(library, fields):
    client, directory = library
    response = client.post("/api/import_deck", json={"name": "Bad", "paste_text": "100 Forest", **fields})
    assert response.status_code == 400
    assert list(directory.iterdir()) == []


def test_new_commander_adds_one_card_and_reports_warning(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "New", "paste_text": "99 Forest", "commander": "New Commander",
    })
    assert response.status_code == 200
    assert response.json["card_delta"] == 1
    assert any("added" in warning.lower() for warning in response.json["warnings"])
    text = next(directory.glob("*.dck")).read_text(encoding="utf-8")
    assert main_card_quantities(text) == {"Forest": 99}
    assert section_card_names(text, "Commander") == ["New Commander"]


def test_supplied_ur_dragon_deck_imports_as_100_clean_cards(library):
    client, directory = library
    paste = (Path(__file__).parent / "fixtures" / "ur_dragon_moxfield.txt").read_text(encoding="utf-8")
    response = client.post("/api/import_deck", json={
        "name": "Ur Dragon", "paste_text": paste, "commander": "The Ur-Dragon",
    })
    assert response.status_code == 200
    text = next(directory.glob("*.dck")).read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["The Ur-Dragon"]
    quantities = main_card_quantities(text)
    assert sum(quantities.values()) == 99
    assert all("*F*" not in name and "*E*" not in name and " (" not in name for name in quantities)
    assert quantities["Dragon's Hoard"] == 1
    assert quantities["Nature's Lore"] == 1
    assert quantities["Forest"] == 2
    assert quantities["Klauth, Unrivaled Ancient"] == 1


def test_partner_removed_moves_to_main_and_current_commander_keeps_printing(library):
    client, directory = library
    path = directory / "Test.dck"
    path.write_text("[Commander]\n1 Pako+|C20|7\n1 Haldan|C20|4\n[Main]\n98 Forest\n", encoding="utf-8")
    url = "/api/deck_commander?deck=Test"
    response = client.put(url, json={"commander": "pako", "partner": ""})
    assert response.status_code == 200
    assert response.json["card_delta"] == 0
    text = path.read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["Pako"]
    assert "1 Pako+|C20|7" in text
    assert main_card_quantities(text) == {"Haldan": 1, "Forest": 98}
    summary = client.get(url).json
    assert set(summary["candidates"]) == {"Pako", "Haldan", "Forest"}


@pytest.mark.parametrize("fields", [
    {"commander": "Forest\n[Main]"}, {"commander": "Forest|SET|1"},
    {"commander": "Forest", "partner": "forest"}, {"commander": 4}, {},
])
def test_invalid_editor_request_preserves_original_file(library, fields):
    client, directory = library
    path = directory / "Test.dck"
    original = "[Main]\n100 Forest\n"
    path.write_text(original, encoding="utf-8")
    response = client.put("/api/deck_commander?deck=Test", json=fields)
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == original


def test_front_face_selection_moves_existing_dfc_without_changing_its_name(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "DFC", "paste_text": "1 Esika, God of the Tree // The Prismatic Bridge|KHM|168\n99 Forest",
        "commander": "Esika, God of the Tree",
    })
    assert response.status_code == 200
    assert response.json["card_delta"] == 0
    text = next(directory.glob("*.dck")).read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["Esika, God of the Tree // The Prismatic Bridge"]
    assert main_card_quantities(text) == {"Forest": 99}


def test_same_dfc_with_different_name_forms_is_not_a_distinct_partner(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "DFC", "paste_text": "100 Forest", "commander": "Esika, God of the Tree",
        "partner": "Esika, God of the Tree // The Prismatic Bridge",
    })
    assert response.status_code == 400
    assert list(directory.iterdir()) == []


def test_editor_repairs_old_imported_printing_suffix_without_adding_duplicate(library):
    client, directory = library
    path = directory / "Test.dck"
    path.write_text("[Main]\n1 The Ur-Dragon (PF25) 15 *F*\n99 Forest\n", encoding="utf-8")
    response = client.put("/api/deck_commander?deck=Test", json={"commander": "The Ur-Dragon"})
    assert response.status_code == 200
    assert response.json["card_delta"] == 0
    assert main_card_quantities(path.read_text(encoding="utf-8")) == {"Forest": 99}


@pytest.mark.parametrize("text", [
    "[Main]\n100 Forest\n[Main]\n1 Sol Ring\n",
    "[Commander]\n1 Old\n[Commander]\n1 Other\n[Main]\n98 Forest\n",
    "[Main]\n1 Forest\nnot a card\n", "[Commander]\n1 Old\n",
])
def test_editor_rejects_ambiguous_or_malformed_structure_without_write(library, text):
    client, directory = library
    path = directory / "Test.dck"
    path.write_text(text, encoding="utf-8")
    response = client.put("/api/deck_commander?deck=Test", json={"commander": "New"})
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == text


def test_illegal_commander_is_advisory_and_does_not_discard_cards(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "Advisory", "paste_text": "100 Forest", "commander": "Forest",
    })
    assert response.status_code == 200
    assert response.json["warnings"]
    text = next(directory.glob("*.dck")).read_text(encoding="utf-8")
    assert main_card_quantities(text) == {"Forest": 99}
    assert section_card_names(text, "Commander") == ["Forest"]


@pytest.mark.parametrize("zone", ["Commander", "Main"])
def test_legacy_decorated_get_to_put_selection_never_adds_a_copy(library, zone):
    client, directory = library
    path = directory / "Legacy.dck"
    if zone == "Commander":
        original = "[Commander]\n1 The Ur-Dragon (PF25) 15 *F*\n[Main]\n99 Forest\n"
    else:
        original = "[Main]\n1 The Ur-Dragon (PF25) 15 *F*\n99 Forest\n"
    path.write_text(original, encoding="utf-8")
    url = "/api/deck_commander?deck=Legacy"
    summary = client.get(url).json
    selected = summary["commanders"][0] if zone == "Commander" else next(
        name for name in summary["candidates"] if "Ur-Dragon" in name
    )
    assert selected == "The Ur-Dragon"
    response = client.put(url, json={"commander": selected})
    assert response.status_code == 200
    assert response.json["card_delta"] == 0
    text = path.read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["The Ur-Dragon"]
    assert main_card_quantities(text) == {"Forest": 99}


def test_legacy_decorated_submitted_pair_matches_existing_normalized_cards(library):
    client, directory = library
    path = directory / "Legacy.dck"
    path.write_text(
        "[Commander]\n1 Pako, Arcane Retriever (C20) 9 *F*\n"
        "1 Haldan, Avid Arcanist (C20) 3 *E*\n[Main]\n98 Forest\n",
        encoding="utf-8",
    )
    response = client.put("/api/deck_commander?deck=Legacy", json={
        "commander": "Pako, Arcane Retriever (C20) 9 *F*",
        "partner": "Haldan, Avid Arcanist (C20) 3 *E*",
    })
    assert response.status_code == 200
    assert response.json["card_delta"] == 0
    text = path.read_text(encoding="utf-8")
    assert section_card_names(text, "Commander") == ["Pako, Arcane Retriever", "Haldan, Avid Arcanist"]
    assert main_card_quantities(text) == {"Forest": 98}


def test_printing_decorated_duplicate_partner_rejected_before_write(library):
    client, directory = library
    response = client.post("/api/import_deck", json={
        "name": "Bad pair", "paste_text": "1 The Ur-Dragon\n99 Forest",
        "commander": "The Ur-Dragon", "partner": "The Ur-Dragon (PF25) 15 *F*",
    })
    assert response.status_code == 400
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("method", ["get", "put"])
def test_malformed_legacy_name_returns_client_error_without_write(library, method):
    client, directory = library
    path = directory / "Legacy.dck"
    original = "[Commander]\n1 |SET|12\n[Main]\n99 Forest\n"
    path.write_text(original, encoding="utf-8")
    response = getattr(client, method)("/api/deck_commander?deck=Legacy", json={"commander": "New"})
    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == original
