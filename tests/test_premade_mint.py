"""Tests for commander_builder.premade_mint — batch heuristic v2 minting
for `[PREMADE]` decks (FP-002 pair supply).

Fully offline: the advisor is injected through the ``advise_fn`` seam
(never the real EDHREC-backed heuristic), Scryfall printing resolution
is stubbed to its plain-line fallback, and decks live in a tmp dir.
Staging goes to a tmp directory — no UNC path anywhere.
"""
from __future__ import annotations

from pathlib import Path

from commander_builder._advisor_models import SwapRecommendation
from commander_builder.log_parser import _normalize
from commander_builder.premade_mint import (
    main,
    mint_v2_for_premades,
    premade_bases_without_v2,
)
from commander_builder.web._helpers import _count_main_cards

BASE_NAME = "[PREMADE] Test Deck [B4].dck"
V2_NAME = "[PREMADE] Test Deck v2 [B4].dck"


def _write_premade(deck_dir: Path, name: str = BASE_NAME) -> Path:
    """A legal 100-card premade: 1 commander + 99 main."""
    deck_dir.mkdir(parents=True, exist_ok=True)
    path = deck_dir / name
    path.write_text(
        "[metadata]\n"
        f"Name={Path(name).stem}\n"
        "Moxfield=abc123\n"
        "Source=moxfield\n"
        "[Commander]\n"
        "1 Atraxa, Praetors' Voice\n"
        "[Main]\n"
        "95 Forest\n"
        "1 Sol Ring\n"
        "1 Arcane Signet\n"
        "1 Cultivate\n"
        "1 Kodama's Reach\n",
        encoding="utf-8",
    )
    return path


def _swap(cut: str, add: str) -> list[SwapRecommendation]:
    return [
        SwapRecommendation(card=add, action="add", reason="test"),
        SwapRecommendation(card=cut, action="cut", reason="test"),
    ]


def _stub_scryfall(monkeypatch) -> None:
    """Force _format_added_line's offline fallback (plain `1 <name>`)."""
    import commander_builder.scryfall_client as sc
    monkeypatch.setattr(sc, "lookup_card", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# base discovery
# --------------------------------------------------------------------------- #
def test_discovery_finds_premade_bases_only(tmp_path):
    _write_premade(tmp_path)
    # Non-premade roles never mint: user, pool.
    (tmp_path / "[USER] Mine [B3].dck").write_text(
        "[Commander]\n1 X\n", encoding="utf-8")
    (tmp_path / "Pool Deck [B3].dck").write_text(
        "[Commander]\n1 X\n", encoding="utf-8")

    bases = premade_bases_without_v2(tmp_path)
    assert [p.name for p in bases] == [BASE_NAME]


def test_discovery_skips_bases_with_existing_v2(tmp_path):
    _write_premade(tmp_path)
    (tmp_path / V2_NAME).write_text("[Commander]\n1 X\n", encoding="utf-8")

    assert premade_bases_without_v2(tmp_path) == []


def test_discovery_groups_lineage_across_bracket_drift(tmp_path):
    # A drift-renamed base ([B4] -> [B3]) still owns its old-tag v2:
    # lineage identity strips the bracket tag, so no re-mint.
    _write_premade(tmp_path, "[PREMADE] Drifted [B3].dck")
    (tmp_path / "[PREMADE] Drifted v2 [B4].dck").write_text(
        "[Commander]\n1 X\n", encoding="utf-8")

    assert premade_bases_without_v2(tmp_path) == []


# --------------------------------------------------------------------------- #
# minting
# --------------------------------------------------------------------------- #
def test_mint_writes_legal_v2_with_stamped_name(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path)

    seen = []

    def advise_fn(path, bracket):
        seen.append((path.name, bracket))
        return _swap(cut="Sol Ring", add="Lightning Greaves")

    rows = mint_v2_for_premades(tmp_path, advise_fn=advise_fn)

    # The advisor ran at the filename-tagged bracket.
    assert seen == [(BASE_NAME, 4)]
    (row,) = rows
    assert row["v2"] == V2_NAME and row["swaps"] == 1 and row["skipped"] is None

    v2 = tmp_path / V2_NAME
    text = v2.read_text(encoding="utf-8")
    # Swap landed; mainboard is exactly 100 - commanders = 99.
    assert "Lightning Greaves" in text
    assert "Sol Ring" not in text
    assert _count_main_cards(text) == 99
    # Name= is restamped to the v2's own stem, and filename/Name=
    # normalize identically (the log_parser._normalize invariant).
    assert f"Name={v2.stem}" in text
    assert _normalize(v2.name) == _normalize(v2.stem)
    # The metadata block passes through: same Moxfield id = one lineage.
    assert "Moxfield=abc123" in text
    # Base file untouched.
    assert "Sol Ring" in (tmp_path / BASE_NAME).read_text(encoding="utf-8")


def test_mint_skips_zero_swap_decks_without_writing(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path)

    rows = mint_v2_for_premades(tmp_path, advise_fn=lambda p, b: [])

    (row,) = rows
    assert row["v2"] is None
    assert row["skipped"].startswith("zero swaps")
    assert not (tmp_path / V2_NAME).exists()


def test_mint_skips_when_validation_drops_every_pair(tmp_path, monkeypatch):
    # Adds without cuts balance down to zero applied swaps — same skip.
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path)
    recs = [SwapRecommendation(card="Lightning Greaves", action="add",
                               reason="test")]

    rows = mint_v2_for_premades(tmp_path, advise_fn=lambda p, b: recs)

    (row,) = rows
    assert row["skipped"].startswith("zero swaps")
    assert not (tmp_path / V2_NAME).exists()


def test_mint_survives_one_bad_deck_and_continues(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path, "[PREMADE] Bad Deck [B3].dck")
    _write_premade(tmp_path)

    def advise_fn(path, bracket):
        if "Bad" in path.name:
            raise RuntimeError("advisor exploded")
        return _swap(cut="Sol Ring", add="Lightning Greaves")

    rows = mint_v2_for_premades(tmp_path, advise_fn=advise_fn)

    by_deck = {r["deck"]: r for r in rows}
    assert by_deck["[PREMADE] Bad Deck [B3].dck"]["skipped"].startswith(
        "advise failed")
    assert by_deck[BASE_NAME]["v2"] == V2_NAME
    assert (tmp_path / V2_NAME).exists()


# --------------------------------------------------------------------------- #
# inbox staging
# --------------------------------------------------------------------------- #
def test_stage_dir_receives_minted_base_and_v2(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    deck_dir = tmp_path / "decks"
    stage = tmp_path / "inbox" / "new_decks"
    _write_premade(deck_dir)

    rows = mint_v2_for_premades(
        deck_dir,
        advise_fn=lambda p, b: _swap(cut="Sol Ring", add="Lightning Greaves"),
        stage_dir=stage,
    )

    (row,) = rows
    assert row["staged"] is True
    assert (stage / BASE_NAME).exists()
    assert (stage / V2_NAME).exists()
    # Staged copy is the minted v2, byte for byte.
    assert ((stage / V2_NAME).read_text(encoding="utf-8")
            == (deck_dir / V2_NAME).read_text(encoding="utf-8"))


def test_skipped_decks_are_not_staged(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    deck_dir = tmp_path / "decks"
    stage = tmp_path / "inbox"
    _write_premade(deck_dir)

    rows = mint_v2_for_premades(
        deck_dir, advise_fn=lambda p, b: [], stage_dir=stage)

    assert rows[0]["staged"] is False
    assert not stage.exists()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_main_mints_via_injected_default_advise(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path)
    import commander_builder.premade_mint as pm
    monkeypatch.setattr(
        pm, "_default_advise",
        lambda path, bracket, deck_dir: _swap(
            cut="Sol Ring", add="Lightning Greaves"),
    )

    rc = main(["--deck-dir", str(tmp_path)])

    assert rc == 0
    assert (tmp_path / V2_NAME).exists()


def test_main_returns_zero_on_empty_candidate_list(tmp_path):
    assert main(["--deck-dir", str(tmp_path)]) == 0


def test_main_returns_one_when_batch_fully_errors(tmp_path, monkeypatch):
    _write_premade(tmp_path)
    import commander_builder.premade_mint as pm

    def boom(path, bracket, deck_dir):
        raise RuntimeError("advisor down")

    monkeypatch.setattr(pm, "_default_advise", boom)

    assert main(["--deck-dir", str(tmp_path)]) == 1


def test_main_limit_caps_attempted_decks(tmp_path, monkeypatch):
    _stub_scryfall(monkeypatch)
    _write_premade(tmp_path, "[PREMADE] Deck A [B3].dck")
    _write_premade(tmp_path, "[PREMADE] Deck B [B3].dck")
    import commander_builder.premade_mint as pm
    monkeypatch.setattr(
        pm, "_default_advise",
        lambda path, bracket, deck_dir: _swap(
            cut="Sol Ring", add="Lightning Greaves"),
    )

    rc = main(["--deck-dir", str(tmp_path), "--limit", "1"])

    assert rc == 0
    minted = sorted(p.name for p in tmp_path.glob("* v2 *.dck"))
    assert minted == ["[PREMADE] Deck A v2 [B3].dck"]
