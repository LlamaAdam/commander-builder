"""Tests for ``deck_library_analyzer`` — bulk static analysis of
a deck directory against a Forge card-script corpus.

These tests use synthetic fixtures (a tiny .dck file or two + a
matching synthetic Forge corpus) so they run fast and stay
deterministic. The real production usage iterates ~345 decks
against ~32k card scripts; that flow is exercised separately
via the CLI wrapper at ``scripts/analyze_deck_library.py``
which doesn't ship with a runtime test (would need a live
Forge install).
"""
from __future__ import annotations

import json
from pathlib import Path

from commander_builder.deck_library_analyzer import (
    LibraryReport,
    analyze_library,
    iter_deck_cards,
    iter_deck_files,
)
from commander_builder.forge_cards_loader import CardsLoader


def _write_deck(path: Path, commander: str, main: list[str]) -> None:
    """Write a minimal .dck file the analyzer can iterate."""
    body = ["[metadata]", "Name=Test", "[Commander]", f"1 {commander}",
            "[Main]"]
    for c in main:
        body.append(f"1 {c}")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _scaffold_corpus(root: Path, scripts: dict[str, str]) -> None:
    """Create the unzipped Forge layout under ``root``."""
    for slug, text in scripts.items():
        (root / slug[0]).mkdir(parents=True, exist_ok=True)
        (root / slug[0] / f"{slug}.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# iter_deck_cards — deck-file walker
# ---------------------------------------------------------------------------

def test_iter_deck_cards_yields_commander_and_main_only():
    """Sideboard / Considering / metadata are skipped."""
    text = (
        "[metadata]\nName=X\n"
        "[Commander]\n1 Krenko, Mob Boss\n"
        "[Main]\n1 Sol Ring\n2 Mountain\n"
        "[Sideboard]\n1 Ignore Me\n"
    )
    out = list(iter_deck_cards(text))
    assert out == [
        (1, "Krenko, Mob Boss"),
        (1, "Sol Ring"),
        (2, "Mountain"),
    ]


def test_iter_deck_cards_strips_edition_codes():
    """`1 Sol Ring|CLB|871` → ("Sol Ring", qty=1). Edition tail dropped."""
    text = "[Main]\n1 Sol Ring|CLB|871\n3 Mountain|EXP|123\n"
    out = list(iter_deck_cards(text))
    assert out == [(1, "Sol Ring"), (3, "Mountain")]


def test_iter_deck_cards_handles_empty_deck():
    assert list(iter_deck_cards("")) == []
    assert list(iter_deck_cards("[metadata]\nName=X\n")) == []


def test_iter_deck_files_sorts(tmp_path):
    (tmp_path / "z.dck").touch()
    (tmp_path / "a.dck").touch()
    (tmp_path / "m.dck").touch()
    (tmp_path / "ignore.txt").touch()  # non-.dck filtered out
    out = [p.name for p in iter_deck_files(tmp_path)]
    assert out == ["a.dck", "m.dck", "z.dck"]


# ---------------------------------------------------------------------------
# analyze_library — end-to-end aggregation
# ---------------------------------------------------------------------------

def test_analyze_library_resolves_known_cards_and_aggregates_effects(tmp_path):
    """Two decks, two real Forge-style scripts; the report rolls up
    effect kinds, keywords, and deck hints across the corpus."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "krenko.dck", "Krenko, Mob Boss",
                ["Sol Ring", "Lightning Bolt"])
    _write_deck(decks / "other.dck", "Krenko, Mob Boss",
                ["Sol Ring", "Goblin King"])

    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": (
            "Name:Krenko, Mob Boss\n"
            "ManaCost:2 R R\n"
            "Types:Legendary Creature Goblin Warrior\n"
            "PT:3/3\n"
            "A:AB$ Token | Cost$ T | TokenAmount$ X | TokenScript$ r_1_1_goblin\n"
            "SVar:X:Count$Valid Goblin.YouCtrl\n"
            "DeckHints:Type$Goblin\n"
        ),
        "sol_ring": (
            "Name:Sol Ring\n"
            "ManaCost:1\n"
            "Types:Artifact\n"
            "A:AB$ Mana | Cost$ T | Produced$ C | Amount$ 2\n"
        ),
        "lightning_bolt": (
            "Name:Lightning Bolt\n"
            "ManaCost:R\n"
            "Types:Instant\n"
            "A:SP$ DealDamage | NumDmg$ 3\n"
        ),
        "goblin_king": (
            "Name:Goblin King\n"
            "ManaCost:1 R R\n"
            "Types:Creature Goblin\n"
            "PT:2/2\n"
            "K:Mountainwalk\n"
            "S:Mode$ Continuous | Affected$ Goblin.Other+YouCtrl "
            "| AddPower$ 1 | AddToughness$ 1\n"
        ),
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader)

    assert report.decks_scanned == 2
    # Distinct cards: Krenko + Sol Ring + Lightning Bolt + Goblin King.
    # (Krenko is in BOTH decks' [Commander] and Sol Ring is in both
    # [Main] sections, so distinct < total.)
    assert report.distinct_cards == 4
    assert report.resolved_cards == 4
    assert report.unresolved_cards == []

    # Effect-kind histogram across all distinct cards: Token (Krenko),
    # Mana (Sol Ring), DealDamage (Bolt), Continuous (Goblin King static).
    assert report.effect_kinds["Token"] == 1
    assert report.effect_kinds["Mana"] == 1
    assert report.effect_kinds["DealDamage"] == 1
    assert report.effect_kinds["Continuous"] == 1

    # Ability category histogram: AB (Krenko's Token, Sol Ring's Mana),
    # SP (Bolt), Mode (Goblin King static).
    assert report.ability_categories["AB"] == 2
    assert report.ability_categories["SP"] == 1
    assert report.ability_categories["Mode"] == 1

    # Keywords: just Goblin King's Mountainwalk.
    assert report.keywords["Mountainwalk"] == 1

    # SVar names: Krenko's X.
    assert report.svar_names["X"] == 1

    # DeckHints: Krenko's Type$Goblin.
    assert report.deck_hints["Type$Goblin"] == 1


def test_analyze_library_records_unresolved_cards(tmp_path):
    """Cards Forge doesn't ship (typos / custom / new set we haven't
    pulled yet) land in ``unresolved_cards`` instead of crashing."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "d.dck", "Krenko, Mob Boss", ["Made-Up Card"])

    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": (
            "Name:Krenko, Mob Boss\nManaCost:2 R R\nTypes:Creature Goblin\n"
            "PT:3/3\n"
        ),
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader)
    assert report.resolved_cards == 1   # Krenko
    assert "Made-Up Card" in report.unresolved_cards


def test_analyze_library_max_decks_caps_scan(tmp_path):
    """`max_decks=1` only scans the first deck (sorted order)."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "a.dck", "Krenko, Mob Boss", ["Sol Ring"])
    _write_deck(decks / "z.dck", "Krenko, Mob Boss", ["Lightning Bolt"])

    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": "Name:Krenko, Mob Boss\nManaCost:2 R R\nTypes:Creature\nPT:3/3\n",
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\n",
        "lightning_bolt": "Name:Lightning Bolt\nManaCost:R\nTypes:Instant\n",
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader, max_decks=1)
    assert report.decks_scanned == 1
    # Only Krenko + Sol Ring from the first deck.
    assert report.distinct_cards == 2


def test_analyze_library_include_per_deck_breakdown(tmp_path):
    """``include_per_deck=True`` populates per-deck card counts so
    callers can drill down."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "one.dck", "Krenko, Mob Boss",
                ["Sol Ring", "Sol Ring"])  # 2x for the count

    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": "Name:Krenko, Mob Boss\nManaCost:2 R R\nTypes:Creature\nPT:3/3\n",
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\n",
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader, include_per_deck=True)
    assert len(report.per_deck) == 1
    counts = report.per_deck[0]
    assert counts.deck_path.name == "one.dck"
    # Sol Ring listed twice in the .dck → qty sum 2.
    assert counts.cards["Sol Ring"] == 2
    assert counts.cards["Krenko, Mob Boss"] == 1


def test_analyze_library_report_to_dict_is_json_safe(tmp_path):
    """``to_dict`` projects the report into JSON-serializable shape
    so the CLI wrapper can dump it without bespoke encoders."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "d.dck", "Krenko, Mob Boss", ["Sol Ring"])
    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": "Name:Krenko, Mob Boss\nManaCost:2 R R\nTypes:Creature\nPT:3/3\n",
        "sol_ring": "Name:Sol Ring\nManaCost:1\nTypes:Artifact\nA:AB$ Mana | Cost$ T | Produced$ C\n",
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader, include_per_deck=True)
    blob = json.dumps(report.to_dict())  # raises on non-serializable
    payload = json.loads(blob)
    assert payload["decks_scanned"] == 1
    assert payload["effect_kinds"].get("Mana") == 1
    assert payload["per_deck"][0]["cards"]["Sol Ring"] == 1


def test_analyze_library_dfc_card_counts_both_faces(tmp_path):
    """DFC: front face's parser includes ``faces[0]`` for the back.
    The aggregator must walk faces so a Bala-Ged-Recovery-style card
    contributes both the spell-face's draw effect AND the land-face's
    mana ability."""
    decks = tmp_path / "decks"
    decks.mkdir()
    _write_deck(decks / "d.dck", "Krenko, Mob Boss",
                ["Bala Ged Recovery"])

    corpus = tmp_path / "corpus"
    _scaffold_corpus(corpus, {
        "krenko_mob_boss": (
            "Name:Krenko, Mob Boss\nManaCost:2 R R\nTypes:Creature\nPT:3/3\n"
        ),
        "bala_ged_recovery": (
            "Name:Bala Ged Recovery\n"
            "ManaCost:2 G\n"
            "Types:Sorcery\n"
            "A:SP$ ChangeZone | NumCards$ 2\n"
            "AlternateMode:DoubleFaced\n"
            "Name:Bala Ged Sanctuary\n"
            "ManaCost:no cost\n"
            "Types:Land\n"
            "A:AB$ Mana | Cost$ T | Produced$ G\n"
        ),
    })

    loader = CardsLoader(directory=corpus)
    report = analyze_library(decks, loader)
    # Both the spell-face's ChangeZone AND the land-face's Mana
    # show up in the effect-kind histogram.
    assert report.effect_kinds["ChangeZone"] == 1
    assert report.effect_kinds["Mana"] == 1
